#!/usr/bin/env python
"""港股 stale 补拉入口 (方案 X)。

背景 (2026-09-22 架构师调研 + 实测): 港股热点覆盖率长期 42%, stale 1611 只;
抽样验证显示**不是结构性漏拉** —— 新浪源已自愈补齐到当日, 本地只是"管道没回头
重拉"。因此本脚本**不接新数据源**: 从 enriched 直算 stale 清单 → 逐只重跑
**现役** ``HKDailyProvider`` (新浪主 + 腾讯备 + 东财仲裁全在 provider 里) →
走现役 ``publish_hk_daily_snapshot`` 落盘 (raw + 复权因子 + enriched 原子发布)。

请求窗口是**维护窗口** (起点 = 现有历史最早日) 而非纯增量 ``latest+1``:
现役 publish 对"未核实口径的旧分区"要求 incoming 覆盖旧分区已确认交易日,
纯增量会让 ``missing`` 变成几千天而被 fail-closed 守卫拒 (2026-09-23 实跑
50/50 全失败)。放宽窗口**不增加网络请求** (新浪一次请求返回全历史再在内存
过滤; 腾讯兜底窗口恒为 end-120 天)。详见 ``_maintenance_start``。

stale 基准**不用全市场 as_of** (2026-09-23 实证暴露的移动靶): as_of 是所有
标的最新日期的最大值, 补拉把它推到今天后, 停在"服务停摆日"的标的
(1187 只停在 09-18) 会一夜之间全被算成 stale —— 补得越多 stale 越多
(1611 → 2796)。基准改用**市场当天 − 容差** (默认 7 个自然日, 覆盖周末 +
常规节假日), 或由 ``--stale-before`` 显式指定; 它不随本脚本的写盘移动。

为什么落库不用 ``recompute_market_enriched`` (规格原文建议):
它只"重算既有 bar" —— ``sync_hk_daily_to_enriched`` 不传 factors, HK 分支会走
``_hk_factors_for_window`` 读旧缓存, 而 ``coverage_end < 新 raw 的 max(date)``
直接 raise "覆盖不足或版本冲突"。补拉恰恰把 raw 推到更新的一天 ⇒ 用它必然
全批 fail-closed, 一只也补不进去。现役 ``publish_hk_daily_snapshot`` 会带上
本次拉取的新复权因子 (coverage_end = 本次 actual_end), 才是补拉的正确落库路径
(与 ``kline_sync.sync_and_persist_daily_batch`` 的港股分支同一条链)。

用法:
    python repair_hk_stale.py --data-dir <path> [--market HK] [--limit N]
                              [--stale-before YYYY-MM-DD] [--tolerance-days 7]
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
# stale 基准容差 (自然日): 覆盖周末 + 常规节假日/长假, 与 _MARKET_DAILY_STALENESS_DAYS
# 同思路 —— "服务停了几天没同步"不该被判成源缺口 (2026-09-23: 1187 只停在 09-18)
_DEFAULT_TOLERANCE_DAYS = 7
# 单只耗时经验值 (秒): 2026-09-23 真实旧仓实测 2 只 16.391s (含全历史拉取 + 落盘)。
# 只在 dry-run (没有实测样本) 时用它做估算; 实跑后一律用本次实测值。
_MEASURED_SECONDS_PER_SYMBOL = 8.2


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


def _resolve_stale_before(
    market: str, *, stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS, today: date | None = None,
) -> tuple[date, str, date]:
    """stale 判定基准: 返回 ``(阈值, 基准说明, 市场当天)``; 严格**小于**阈值算 stale。

    基准不能用"全市场 ``as_of`` = max(所有标的最新日)" —— 那是**移动靶**:
    补拉把 as_of 推到今天之后, 停在"服务停摆日"的那批标的会被一夜之间全部算成
    stale。2026-09-23 实证: 补拉前 as_of=09-18 / stale=1611; 只补了 2 只把 as_of
    推到 09-23 后 stale 变 2796 —— 多出来的 1187 只全是"停在 09-18"的标的,
    它们只是服务没启动 (每日同步 cron 没跑), 不是源缺口。**补得越多 stale 越多**。

    稳定基准 = 市场当天 − 容差 (默认 7 个自然日, 覆盖周末 + 常规节假日), 它只随
    日历移动、不随本脚本的写盘移动; ``stale_before`` 给定时它就是阈值本身
    (不再叠加容差), 便于用户精确圈定"只补 09-03 那一批"。
    """
    market_today = today or _market_today(market)
    if stale_before is not None:
        return stale_before, "stale-before", market_today
    days = max(0, int(tolerance_days))
    return market_today - timedelta(days=days), f"market-today-{days}d", market_today


def compute_stale_plan(
    data_dir: Path, market: str = "HK", limit: int | None = None, *,
    stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS,
    today: date | None = None,
) -> dict[str, Any]:
    """从 enriched 直算 stale 清单与补拉计划 (纯读, 不发请求)。

    stale 判定: 该标的最新交易日 **< 基准阈值** (见 ``_resolve_stale_before``;
    默认 = 市场当天 − 容差, 可由 ``stale_before`` 显式指定)。**刻意不用全市场
    as_of** —— 否则补拉本身推高基准、stale 数反弹。返回的 ``as_of`` 只是给
    人看的现状指标, 不参与判定。

    优先级: 按"停在哪个日期"分桶, 桶大的先补 (973 只停在 09-03 的峰值档最优先),
    桶内按 symbol 排序保证可重放。
    """
    latest = scan_latest_dates(data_dir, market)
    threshold, basis, market_today = _resolve_stale_before(
        market, stale_before=stale_before, tolerance_days=tolerance_days, today=today)
    plan: dict[str, Any] = {
        "market": market, "scanned": 0, "as_of": None, "stale": 0,
        "buckets": [], "targets": [],
        "stale_before": threshold.isoformat(), "basis": basis,
        "tolerance_days": max(0, int(tolerance_days)),
        "market_today": market_today.isoformat(),
    }
    if latest.is_empty():
        return plan
    days = [_plain_date(v) for v in latest["latest_date"].to_list()]
    symbols = latest["symbol"].to_list()
    observed = [day for day in days if day is not None]
    as_of = max(observed) if observed else None
    stale_rows = [(s, d) for s, d in zip(symbols, days, strict=True)
                  if d is not None and d < threshold]
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
    plan.update({
        "scanned": latest.height,
        "as_of": as_of.isoformat() if as_of is not None else None,
        "stale": len(stale_rows),
        "buckets": buckets,
        "targets": [{"symbol": symbol, "latest_date": day.isoformat()} for symbol, day in targets],
    })
    return plan


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


def _maintenance_start(
    root: Path, symbol: str, legacy: pl.DataFrame, fallback: date,
) -> date:
    """该标的的**维护窗口**起点 = 现有历史最早日 (无历史则退回 fallback)。

    对齐现役 ``kline_sync`` 港股分支的 ``maintained_start`` (它同样把 start 前
    移到 ``previous["date"].min()``)。

    为什么不能只拉 ``latest+1`` 增量 (2026-09-23 实跑 50/50 全失败的根因):
    现役 publish 走 ``merge_market_daily_frames(old, incoming, replace_legacy=True)``,
    当旧分区**未核实口径** (legacy / 早期分区) 时要求 incoming 覆盖旧分区已确认
    交易日 (``_legacy_gap_tolerable`` 三道护栏)。纯增量只有十几天, ``missing``
    是几千天 ⇒ 被 fail-closed 守卫拒。**守卫本身是对的** (防窄窗口数据把历史洗
    掉), 正确做法是放宽请求窗口, 让 incoming 自带完整历史 —— 不碰守卫。

    代价: 新浪一次请求返回全历史再在内存按 [start,end] 过滤 ⇒ 放宽窗口**不增加
    任何网络请求** (同一 URL); 腾讯兜底窗口恒为 ``max(start, end-120天)``, 同样
    不受 start 前移影响。

    为什么不改成"旧历史行 ∪ 增量"的并集: publish 的 ``is_verified_hk_raw(raw)``
    是**整帧逐行 AND** 判定 (price_schema_version/raw_price_verified/
    price_adjustment/volume_unit/currency 五行全过), 掺入未核实的旧行会让整帧
    判为未核实 → raise "币种、量单位或价格口径未核实"; 要给旧行补这些标记等于
    凭空声明它的来源口径 —— 而旧分区被拦正是因为口径未知 ⇒ 属伪造, 违反不伪造
    铁律。故宁可多拉一次 (网络成本为零), 也不给旧行贴标签。
    """
    from app.tickflow.market_daily import read_market_daily_symbol

    try:
        previous = read_market_daily_symbol(root, symbol, legacy=legacy)
    except Exception as exc:
        logger.warning("维护窗口起点读取失败 %s (退回 latest+1): %s", symbol, exc)
        return fallback
    if previous.is_empty():
        return fallback
    earliest = _plain_date(previous["date"].min())
    return fallback if earliest is None else min(earliest, fallback)


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
    stale_before: date | None = None,
    tolerance_days: int = _DEFAULT_TOLERANCE_DAYS,
    publisher: Callable[..., dict] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """补拉 stale 标的。dry_run=True 时只算计划, 不发请求、不写盘。

    Returns:
        报告 dict: scanned / as_of / stale / stale_before / basis / buckets /
        attempted / succeeded / failed / failures / no_data / elapsed_seconds /
        seconds_per_symbol / estimate_remaining_seconds / estimate_all_seconds。
    """
    started = time.monotonic()
    plan = compute_stale_plan(
        data_dir, market, limit=limit, stale_before=stale_before,
        tolerance_days=tolerance_days, today=today,
    )
    report: dict[str, Any] = {
        **plan, "dry_run": dry_run, "limit": limit or 0,
        "attempted": 0, "succeeded": 0, "failed": 0, "skipped": 0,
        "no_data": 0, "no_data_symbols": [], "failures": [], "elapsed_seconds": 0.0,
    }
    targets = plan["targets"]
    if dry_run or not targets:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        _attach_estimate(report, measured=False)
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
        # 补拉区间 = 维护窗口起点 → 市场当天。窗口必须覆盖 [latest+1, today]
        # 这段缺口; 起点前移到"现有历史最早日"是 publish 的 legacy 合并守卫
        # 要求 (详见 _maintenance_start: 纯增量会被判 missing 几千天而拒)。
        start_day = _maintenance_start(root, symbol, legacy, latest + timedelta(days=1))
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
                # 源侧没有该标的这段窗口的数据 (停牌/退市/源缺) —— 与"发布失败"
                # 是两类问题, 单独归类, 不计入 failed (用户需要区分对待)。
                report["no_data"] += 1
                report["no_data_symbols"].append(symbol)
                logger.info("源未返回 %s 的日线 (停牌/退市/源缺), 跳过", symbol)
                continue
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
    _attach_estimate(report, measured=report["attempted"] > 0)
    return report


def _attach_estimate(report: dict[str, Any], *, measured: bool) -> None:
    """给报告挂上"单只耗时 + 剩余/全量预计" (用户要拿它决定跑多大批量)。

    ``measured=True`` 时用本次实测均值 (有真实样本就不用经验值);
    dry-run 没有样本, 退回 ``_MEASURED_SECONDS_PER_SYMBOL`` 并在报告里标明
    是经验值, 免得用户拿它当 SLA。
    """
    attempted = int(report.get("attempted") or 0)
    if measured and attempted > 0:
        per = round(float(report["elapsed_seconds"]) / attempted, 3)
    else:
        per = float(_MEASURED_SECONDS_PER_SYMBOL)
    report["seconds_per_symbol"] = per
    report["seconds_per_symbol_measured"] = bool(measured and attempted > 0)
    stale = int(report.get("stale") or 0)
    report["estimate_remaining_seconds"] = round(max(0, stale - attempted) * per, 1)
    report["estimate_all_seconds"] = round(stale * per, 1)


def _format_duration(seconds: float) -> str:
    """秒数说人话 (用户要决定批量大小, 别让他自己按计算器)。"""
    value = float(seconds)
    if value < 60:
        return f"约 {value:.0f} 秒"
    if value < 3600:
        return f"约 {value / 60:.1f} 分钟"
    if value < 86400:
        return f"约 {value / 3600:.1f} 小时"
    return f"约 {value / 86400:.1f} 天"


def print_report(report: dict[str, Any]) -> None:
    """打印 before/after 报告 (人读; --json 时给机器读)。"""
    print(f"市场            : {report['market']}")
    print(f"扫描标的数      : {report['scanned']}")
    print(f"全市场 as_of    : {report['as_of']} (现状指标, 不作判定基准)")
    basis = report.get("basis")
    basis_text = {"stale-before": "--stale-before 显式指定"}.get(
        basis, f"市场当天 {report.get('market_today')} - {report.get('tolerance_days')} 天容差")
    print(f"stale 基准      : 最新交易日 < {report.get('stale_before')} ({basis_text})")
    print(f"stale 标的数    : {report['stale']}")
    buckets = report.get("buckets") or []
    if buckets:
        detail = ", ".join(f"{b['symbols']}@{b['latest_date']}" for b in buckets)
        print(f"峰值档 (前 {len(buckets)})   : {detail}")
    print(f"本次拟补拉      : {len(report['targets'])}" + (f" (limit={report['limit']})" if report.get("limit") else ""))
    per = report.get("seconds_per_symbol") or 0.0
    kind = "本次实测" if report.get("seconds_per_symbol_measured") else "经验值"
    print(f"单只耗时        : {per} 秒/只 ({kind})")
    print(f"剩余预计        : {_format_duration(report.get('estimate_remaining_seconds') or 0.0)}"
          f" (还剩 {max(0, int(report.get('stale') or 0) - int(report.get('attempted') or 0))} 只)")
    print(f"全量预计        : {_format_duration(report.get('estimate_all_seconds') or 0.0)}"
          f" (全部 {report.get('stale')} 只)")
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
    if report.get("no_data"):
        # 与发布失败是两类问题: 源侧根本没有这段窗口的数据 (停牌/退市/源缺),
        # 不是我们的管道坏了 —— 单独列出, 别混进 failures 让用户误判。
        print(f"源无数据        : {report['no_data']} (停牌/退市/源缺, 非管道故障)")
        print(f"  {', '.join(report['no_data_symbols'][:20])}"
              + (f" ... 另有 {report['no_data'] - 20} 只" if report['no_data'] > 20 else ""))
    print(f"耗时(秒)        : {report['elapsed_seconds']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="港股 stale 日K 补拉 (方案 X, 手动触发)")
    parser.add_argument("--data-dir", required=True, help="数据根目录 (含 kline_hk_us_enriched/)")
    parser.add_argument("--market", default="HK", help="市场代码 (默认 HK)")
    parser.add_argument("--limit", type=int, default=0, help="本次最多补拉只数 (0=全部)")
    parser.add_argument("--stale-before", default=None, metavar="YYYY-MM-DD",
                        help="只补最新交易日早于该日期的标的 (给定时它就是判定基准, 不再叠加容差)")
    parser.add_argument("--tolerance-days", type=int, default=_DEFAULT_TOLERANCE_DAYS,
                        help=f"未给 --stale-before 时的容差自然日: 基准 = 市场当天 - N "
                             f"(默认 {_DEFAULT_TOLERANCE_DAYS}, 覆盖周末与常规节假日)")
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
    stale_before: date | None = None
    if args.stale_before:
        try:
            stale_before = date.fromisoformat(args.stale_before.strip())
        except ValueError:
            print(f"--stale-before 不是合法日期 (YYYY-MM-DD): {args.stale_before}", file=sys.stderr)
            return 2
    dry_run = not args.yes or args.dry_run
    try:
        report = repair_stale(
            data_dir, args.market, args.limit or None,
            dry_run=dry_run, sleep_seconds=max(0.0, args.sleep_seconds),
            stale_before=stale_before, tolerance_days=max(0, args.tolerance_days),
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
