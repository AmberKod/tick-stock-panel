#!/usr/bin/env python
"""港股 stale 补拉入口 (方案 X)。

背景 (2026-09-22 架构师调研 + 实测): 港股热点覆盖率长期 42%, stale 1611 只;
抽样验证显示**不是结构性漏拉** —— 新浪源已自愈补齐到当日, 本地只是"管道没回头
重拉"。因此本脚本**不接新数据源**: 从 enriched 直算 stale 清单 → 逐只重跑
**现役** ``HKDailyProvider`` (新浪主 + 腾讯备 + 东财仲裁全在 provider 里) →
走现役 ``publish_hk_daily_snapshot`` 落盘 (raw + 复权因子 + enriched 原子发布)。

为什么落库不用 ``recompute_market_enriched`` (规格原文建议):
它只"重算既有 bar" —— ``sync_hk_daily_to_enriched`` 不传 factors, HK 分支会走
``_hk_factors_for_window`` 读旧缓存, 而 ``coverage_end < 新 raw 的 max(date)``
直接 raise "覆盖不足或版本冲突"。补拉恰恰把 raw 推到更新的一天 ⇒ 用它必然
全批 fail-closed, 一只也补不进去。现役 ``publish_hk_daily_snapshot`` 会带上
本次拉取的新复权因子 (coverage_end = 本次 actual_end), 才是补拉的正确落库路径
(与 ``kline_sync.sync_and_persist_daily_batch`` 的港股分支同一条链)。

用法:
    python repair_hk_stale.py --data-dir <path> [--market HK] [--limit N]
                              [--dry-run | --yes] [--sleep-seconds 0.1]

默认 dry-run (只出计划, 不写盘、不发请求); 只有 ``--yes`` 才真正补拉。
手动触发, **不进 APScheduler**。可重跑幂等: 补完的标的回到 as_of, 下次计划
自然不再包含它; publish 侧自带 unchanged 检测, 重复跑不会重复写盘。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as _time
from pathlib import Path
from typing import Any

import polars as pl

# `python scripts/repair_hk_stale.py` 从任意 cwd 都要能 import app.*
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

logger = logging.getLogger(__name__)

_MARKET_SUFFIX = {"HK": ".HK", "US": ".US"}
# 计划里展示的"峰值档"桶数上限
_BUCKET_TOP_N = 5
# 串行限速: 新浪连发 8 只无阻, 腾讯有 WAF 熔断; 默认给一点间隔别并发过猛
_DEFAULT_SLEEP_SECONDS = 0.1


def _market_suffix(market: str) -> str:
    key = str(market or "").strip().upper()
    if key not in _MARKET_SUFFIX:
        raise ValueError(f"不支持的市场: {market} (可用: {sorted(_MARKET_SUFFIX)})")
    return _MARKET_SUFFIX[key]


def _plain_date(value: Any) -> date | None:
    """enriched 分区的 date 可能是 Date/Datetime/字符串, 统一成 date; 取不到返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def scan_latest_dates(data_dir: Path, market: str = "HK") -> pl.DataFrame:
    """扫 enriched 分区, 返回每只标的的最新交易日 (symbol, latest_date)。

    目录不存在 / 读失败 / 无该市场分区时返回空表 —— **不可用不计入**, 不拿
    空结果冒充"没有 stale"。legacy 分区的 date 是 Datetime('us'), 统一 cast
    成 Date 后再取 max, 与 overview 的 as_of 口径一致。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "latest_date": pl.Date})
    root = Path(data_dir) / "kline_hk_us_enriched"
    if not root.exists():
        return empty
    suffix = _market_suffix(market)
    try:
        lazy = pl.scan_parquet(
            str(root / "symbol=*" / "part.parquet"),
            # 历史分区由不同源写入 (新浪 volume=Float64 / 兜底源 Int64), 跨分区
            # scan 需允许整型向浮点兼容提升, 否则 SchemaError (同 overview 侧)
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        timeline = lazy.select("symbol", "date").collect()
    except Exception as exc:  # 目录损坏/混 schema 时不炸整个脚本
        logger.warning("enriched 扫描失败 (%s): %s", root, exc)
        return empty
    if timeline.is_empty():
        return empty
    dated = (
        timeline.filter(pl.col("symbol").cast(pl.String).str.ends_with(suffix))
        .with_columns(pl.col("date").cast(pl.Date, strict=False))
        .drop_nulls("date")
    )
    if dated.is_empty():
        return empty
    return (
        dated.group_by("symbol")
        .agg(pl.col("date").max().alias("latest_date"))
        .sort("symbol")
    )


def compute_stale_plan(
    data_dir: Path, market: str = "HK", limit: int | None = None,
) -> dict[str, Any]:
    """从 enriched 直算 stale 清单与补拉计划 (纯读, 不发请求)。

    stale 判定: 该标的最新交易日 **<** 全市场 as_of (= 所有标的最新日期的最大值),
    与 ``hk_us_overview_builder._build_coverage`` 的 ``latest_date != as_of``
    同义 (as_of 是最大值, 不可能有更大者)。

    优先级: 按"停在哪个日期"分桶, 桶大的先补 (973 只停在 09-03 的峰值档最优先),
    桶内按 symbol 排序保证可重放。
    """
    latest = scan_latest_dates(data_dir, market)
    if latest.is_empty():
        return {"market": market, "scanned": 0, "as_of": None, "stale": 0,
                "buckets": [], "targets": []}
    days = [_plain_date(v) for v in latest["latest_date"].to_list()]
    symbols = latest["symbol"].to_list()
    as_of = max(day for day in days if day is not None)
    stale_rows = [(s, d) for s, d in zip(symbols, days, strict=True) if d is not None and d < as_of]
    counts = Counter(day for _, day in stale_rows)
    # 桶降序 → 桶内 symbol 升序, 保证同输入同输出 (可重放)
    order = {day: index for index, (day, _) in enumerate(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))}
    stale_rows.sort(key=lambda row: (order[row[1]], row[0]))
    buckets = [
        {"latest_date": day.isoformat(), "symbols": count}
        for day, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_BUCKET_TOP_N]
    ]
    targets = stale_rows[:limit] if limit and limit > 0 else stale_rows
    return {
        "market": market,
        "scanned": latest.height,
        "as_of": as_of.isoformat(),
        "stale": len(stale_rows),
        "buckets": buckets,
        "targets": [{"symbol": symbol, "latest_date": day.isoformat()} for symbol, day in targets],
    }


def _resolve_fetch(market: str) -> tuple[Any, Callable[..., Any]]:
    """取现役 provider 的逐标的报告接口, 缺接口显式报错 (不降级成静默空跑)。

    必须走 ``get_daily_with_report``: 补拉要拿**本次的复权因子** (coverage_end
    = 本次 actual_end) 才能落 enriched, ``get_daily`` 只回 frame、丢因子。
    """
    from app.data_providers.registry import get_default_provider

    provider = get_default_provider(market, dataset="daily")
    fetch = getattr(provider, "get_daily_with_report", None)
    if not callable(fetch):
        raise RuntimeError(
            f"{market} 默认日线源 {type(provider).__name__} 未提供逐标的报告接口 "
            f"get_daily_with_report; 本脚本当前只服务港股补拉"
        )
    return provider, fetch


def publish_hk_snapshot(
    root: Path, symbol: str, frame: pl.DataFrame, *, factors: pl.DataFrame,
    item: dict, legacy: pl.DataFrame, verification_archives: list[dict],
) -> dict:
    """现役落库路径: raw + 复权因子 + enriched 一次性原子发布 (港股分支同款)。"""
    from app.services.hk_data_adapter import publish_hk_daily_snapshot

    return publish_hk_daily_snapshot(
        root, symbol, frame, factors=factors, item=item, legacy=legacy,
        verification_archives=verification_archives,
    )


def _market_today(market: str) -> date:
    """市场当天 (非宿主机当天): 与全站市场时钟口径同源, 模块属性调用便于测试钉值。"""
    from app.markets import registry as market_registry

    return market_registry.get_profile(market).today()


def repair_stale(
    data_dir: Path,
    market: str = "HK",
    limit: int | None = None,
    *,
    dry_run: bool = True,
    sleep_seconds: float = _DEFAULT_SLEEP_SECONDS,
    today: date | None = None,
    publisher: Callable[..., dict] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """补拉 stale 标的。dry_run=True 时只算计划, 不发请求、不写盘。

    Returns:
        报告 dict: scanned / as_of / stale / buckets / attempted / succeeded /
        failed / failures / elapsed_seconds。
    """
    started = time.monotonic()
    plan = compute_stale_plan(data_dir, market, limit=limit)
    report: dict[str, Any] = {
        **plan, "dry_run": dry_run, "limit": limit or 0,
        "attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0,
        "failures": [], "elapsed_seconds": 0.0,
    }
    targets = plan["targets"]
    if dry_run or not targets:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return report

    root = Path(data_dir)
    _, fetch = _resolve_fetch(market)
    from app.services.hk_data_adapter import load_hk_raw_verification_archives
    from app.tickflow.market_daily import read_legacy_market_daily

    symbols = [entry["symbol"] for entry in targets]
    legacy = read_legacy_market_daily(root, market, symbols)
    archives = load_hk_raw_verification_archives(root, symbols)
    end_day = today or _market_today(market)
    publish = publisher or publish_hk_snapshot

    for index, entry in enumerate(targets, start=1):
        symbol = entry["symbol"]
        latest = date.fromisoformat(entry["latest_date"])
        # 补拉区间 = 该标的自身最新日 + 1 天 → 市场当天。只补缺口, 不重取历史。
        start_day = latest + timedelta(days=1)
        if progress is not None:
            progress(index, len(targets), symbol)
        if start_day > end_day:
            report["skipped"] += 1
            continue
        report["attempted"] += 1
        try:
            fetched = fetch(
                [symbol],
                start_time=datetime.combine(start_day, _time.min),
                end_time=datetime.combine(end_day, _time.min),
                asset_type="stock",
                verification_archives=[a for a in archives if a.get("symbol") == symbol],
            )
            frame = (fetched.frame.filter(pl.col("symbol") == symbol)
                     if not fetched.frame.is_empty() else pl.DataFrame())
            if frame.is_empty():
                raise ValueError("现役数据源未返回该标的请求区间内的日线")
            factors = (fetched.adjustments.filter(pl.col("symbol") == symbol)
                       if not fetched.adjustments.is_empty() else pl.DataFrame())
            items = {row["symbol"]: dict(row) for row in fetched.items}
            item = items.get(symbol, {"symbol": symbol})
            published = publish(
                root, symbol, frame, factors=factors, item=item, legacy=legacy,
                verification_archives=[a for a in getattr(fetched, "verification_archives", ())
                                       if a.get("symbol") == symbol],
            )
            if published.get("status") in {"ok", "unchanged"}:
                report["succeeded"] += 1
            else:
                report["failed"] += 1
                report["failures"].append({
                    "symbol": symbol,
                    "reason": published.get("reason") or f"发布状态 {published.get('status')}",
                })
        except Exception as exc:  # 单只失败记录后继续, 不中断整批
            report["failed"] += 1
            report["failures"].append({"symbol": symbol, "reason": str(exc)})
            logger.warning("补拉失败 %s: %s", symbol, exc)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def print_report(report: dict[str, Any]) -> None:
    """打印 before/after 报告 (人读; --json 时给机器读)。"""
    print(f"市场            : {report['market']}")
    print(f"扫描标的数      : {report['scanned']}")
    print(f"全市场 as_of    : {report['as_of']}")
    print(f"stale 标的数    : {report['stale']}")
    buckets = report.get("buckets") or []
    if buckets:
        detail = ", ".join(f"{b['symbols']}@{b['latest_date']}" for b in buckets)
        print(f"峰值档 (前 {len(buckets)})   : {detail}")
    print(f"本次拟补拉      : {len(report['targets'])}" + (f" (limit={report['limit']})" if report.get("limit") else ""))
    if report.get("dry_run"):
        print("模式            : dry-run (未发请求、未写盘; 加 --yes 才真正补拉)")
        return
    print(f"实际请求        : {report['attempted']} (跳过 {report['skipped']})")
    print(f"成功            : {report['succeeded']}")
    print(f"失败            : {report['failed']}")
    for failure in report["failures"][:20]:
        print(f"  - {failure['symbol']}: {failure['reason']}")
    if len(report["failures"]) > 20:
        print(f"  ... 另有 {len(report['failures']) - 20} 条失败")
    print(f"耗时(秒)        : {report['elapsed_seconds']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="港股 stale 日K 补拉 (方案 X, 手动触发)")
    parser.add_argument("--data-dir", required=True, help="数据根目录 (含 kline_hk_us_enriched/)")
    parser.add_argument("--market", default="HK", help="市场代码 (默认 HK)")
    parser.add_argument("--limit", type=int, default=0, help="本次最多补拉只数 (0=全部)")
    parser.add_argument("--dry-run", action="store_true", help="只出计划, 不写盘 (默认)")
    parser.add_argument("--yes", action="store_true", help="真正执行补拉")
    parser.add_argument("--sleep-seconds", type=float, default=_DEFAULT_SLEEP_SECONDS,
                        help=f"每只之间的间隔秒数 (默认 {_DEFAULT_SLEEP_SECONDS}, 串行限速)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"数据目录不存在: {data_dir}", file=sys.stderr)
        return 2
    dry_run = not args.yes or args.dry_run
    try:
        report = repair_stale(
            data_dir, args.market, args.limit or None,
            dry_run=dry_run, sleep_seconds=max(0.0, args.sleep_seconds),
        )
    except Exception as exc:
        print(f"补拉失败: {exc}", file=sys.stderr)
        return 1
    if args.json:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
