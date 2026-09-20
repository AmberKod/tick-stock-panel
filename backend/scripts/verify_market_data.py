#!/usr/bin/env python
"""三市场数据闭环一键验收 (H6/enriched/regime/梯队 的新鲜度与一致性)。

背景
----
补跑 / 调度 / 手工修数之后, 判断"数据到底好了没有"依赖临时写 polars 片段现查,
容易漏项且口径不一致。本脚本把验收口径固化成一张表: 每个市场给
🟢/🟡/🔴 判定 + 结论行, 退出码 0=全绿 / 1=有红灯。

检查项
------
1. instruments 行数
2. 日 K 层最新日期 + 落后天数 (港美走 H6 symbol 分区抽样, A 股走 date 分区)
3. enriched 分区数 + 最新日期 + **全目录 scan 一致性** (schema 混存是历史大坑)
4. enriched 覆盖率 (enriched symbol 数 / 日 K symbol 数)
5. regime / 强度梯队最新日期与行数

用法
----
    python backend/scripts/verify_market_data.py              # 全部市场
    python backend/scripts/verify_market_data.py --market hk  # 单市场
    python backend/scripts/verify_market_data.py --sample 60  # 加大 H6 抽样
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import polars as pl

# 落后容差 (自然日): 覆盖周末与常规节假日 (周五→周二天然 4 天), 与
# daily_pipeline._MARKET_DAILY_STALENESS_DAYS 的港美口径对齐, A 股同理按周末计。
STALENESS_TOLERANCE = {"cn": 3, "hk": 4, "us": 3}
# 港美 enriched 覆盖率下限 (低于则黄灯): 新上市/缺拉允许少量缺口
COVERAGE_WARN = 0.95

_MARKET_LABEL = {"cn": "A股", "hk": "港股", "us": "美股"}


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _instruments_rows(root: Path, market: str) -> int | None:
    name = {"cn": "instruments.parquet", "hk": "hk_instruments.parquet", "us": "us_instruments.parquet"}[market]
    path = root / "instruments" / name
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path).height
    except Exception:
        return None


def _daily_latest_and_count(root: Path, market: str, sample: int) -> tuple[date | None, int]:
    """日 K 层最新日期 (众数) 与 symbol 数; A 股走 date 分区, 港美走 H6 symbol 分区。"""
    base = root / "kline_daily"
    if market == "cn":
        dirs = sorted(base.glob("date=*"))
        if not dirs:
            return None, 0
        latest = max(_parse_partition_date(d.name) for d in dirs)
        return latest, 0
    parts = sorted(base.glob(f"symbol=*.{market.upper()}"))
    if not parts:
        return None, 0
    step = max(1, len(parts) // sample)
    picked = parts[::step][:sample]
    counts: dict[date, int] = {}
    for part in picked:
        for file in part.glob("*.parquet"):
            try:
                latest = _as_date(pl.scan_parquet(file).select(pl.col("date").max()).collect().item())
            except Exception:
                continue
            if latest is not None:
                counts[latest] = counts.get(latest, 0) + 1
            break
    if not counts:
        return None, len(parts)
    return max(counts.items(), key=lambda kv: kv[1])[0], len(parts)


def _parse_partition_date(name: str) -> date:
    return date.fromisoformat(name.removeprefix("date="))


def _enriched_stats(root: Path, market: str, sample: int) -> dict:
    """enriched 分区数 / 最新日期(众数) / 全目录 scan 是否通过 / schema 基线。"""
    directory = root / "kline_daily_enriched" if market == "cn" else root / "kline_hk_us_enriched"
    if not directory.exists():
        return {"count": 0, "latest": None, "scan_ok": False, "schema": "无目录"}
    if market == "cn":
        dirs = sorted(directory.glob("date=*"))
        latest = max((_parse_partition_date(d.name) for d in dirs), default=None)
    else:
        parts = sorted(directory.glob(f"symbol=*.{market.upper()}"))
        dirs, latest = parts, None
        counts: dict[date, int] = {}
        step = max(1, len(parts) // sample)
        for part in parts[::step][:sample]:
            file = part / "part.parquet"
            if not file.exists():
                continue
            try:
                value = _as_date(pl.scan_parquet(file).select(pl.col("date").max()).collect().item())
            except Exception:
                continue
            if value is not None:
                counts[value] = counts.get(value, 0) + 1
        if counts:
            latest = max(counts.items(), key=lambda kv: kv[1])[0]
    count = len(dirs)
    # 全目录 scan: schema 混存会直接抛 SchemaError, 这是历史大坑的守门检查
    try:
        pl.scan_parquet(directory).select(pl.len()).collect()
    except Exception as exc:
        return {"count": count, "latest": latest, "scan_ok": False, "schema": f"scan 失败: {str(exc)[:60]}"}
    first = next((d / "part.parquet" for d in dirs if (d / "part.parquet").exists()), None)
    if market == "cn" and dirs:
        first = next((d / "part.parquet" for d in dirs if (d / "part.parquet").exists()), None)
    if first is None:
        return {"count": count, "latest": latest, "scan_ok": True, "schema": "空"}
    schema = pl.scan_parquet(first).collect_schema()
    kind = "legacy 64列/Datetime" if schema.get("date") == pl.Datetime("us") else "当前 schema"
    return {"count": count, "latest": latest, "scan_ok": True, "schema": kind}


def _latest_of(root: Path, relative: str, column: str = "date") -> tuple[date | None, int]:
    path = root / relative
    if not path.exists():
        return None, 0
    try:
        frame = pl.read_parquet(path)
    except Exception:
        return None, 0
    if frame.height == 0 or column not in frame.columns:
        return None, frame.height
    return _as_date(frame[column].max()), frame.height


def main() -> int:
    parser = argparse.ArgumentParser(description="三市场数据闭环验收")
    parser.add_argument("--market", choices=["cn", "hk", "us", "all"], default="all")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--sample", type=int, default=30, help="港美 H6/enriched 抽样数")
    args = parser.parse_args()

    root = _ROOT / args.data_dir if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    markets = ["cn", "hk", "us"] if args.market == "all" else [args.market]
    today = date.today()
    red = yellow = 0

    print(f"\n数据闭环验收  数据目录: {root}  今天: {today}\n")
    for market in markets:
        print(f"{'=' * 58}\n{_MARKET_LABEL[market]} ({market})\n{'=' * 58}")
        instruments = _instruments_rows(root, market)
        daily_latest, daily_count = _daily_latest_and_count(root, market, args.sample)
        enriched = _enriched_stats(root, market, args.sample)
        regime_latest, regime_rows = _latest_of(root, f"regime_history/{market}/part.parquet")
        ladder_latest, ladder_rows = (
            _latest_of(root, f"strength_ladder/{market}/part.parquet")
            if market in {"hk", "us"} else (None, 0)
        )

        staleness = (today - daily_latest).days if daily_latest else None
        tolerance = STALENESS_TOLERANCE[market]
        rows: list[tuple[str, str, str]] = []
        rows.append(("instruments", f"{instruments if instruments is not None else '缺失'} 行",
                     "🟢" if instruments else "🔴"))
        rows.append(("日 K 最新", f"{daily_latest} (落后 {staleness} 天, 容差 {tolerance})",
                     "🟢" if staleness is not None and staleness <= tolerance else "🔴"))
        if market != "cn":
            rows.append(("H6 分区数", f"{daily_count}", "🟢" if daily_count else "🔴"))
        rows.append(("enriched 分区", f"{enriched['count']}", "🟢" if enriched["count"] else "🔴"))
        rows.append(("enriched 最新", f"{enriched['latest']}",
                     "🟢" if enriched["latest"] and (today - enriched["latest"]).days <= tolerance else "🔴"))
        rows.append(("enriched schema", f"{enriched['schema']} / scan={'OK' if enriched['scan_ok'] else 'FAIL'}",
                     "🟢" if enriched["scan_ok"] else "🔴"))
        if market != "cn" and daily_count:
            coverage = enriched["count"] / daily_count
            rows.append(("enriched 覆盖率", f"{coverage:.1%}",
                         "🟢" if coverage >= COVERAGE_WARN else "🟡"))
        rows.append(("regime", f"{regime_latest} / {regime_rows} 行",
                     "🟢" if regime_rows and regime_latest and (today - regime_latest).days <= tolerance else "🟡"))
        if market in {"hk", "us"}:
            rows.append(("强度梯队", f"{ladder_latest} / {ladder_rows} 行",
                         "🟢" if ladder_rows and ladder_latest and (today - ladder_latest).days <= tolerance else "🟡"))

        for name, value, flag in rows:
            print(f"  {flag}  {name:<16} {value}")
            if flag == "🔴":
                red += 1
            elif flag == "🟡":
                yellow += 1
        verdict = "🔴 有阻塞项" if any(flag == "🔴" for _, _, flag in rows) else "🟢 正常"
        print(f"  → {verdict}\n")

    print("=" * 58)
    print(f"合计: 🔴 {red} 阻塞 / 🟡 {yellow} 提示")
    if red:
        print("结论: 🔴 No-Go (先修红灯项)")
        return 1
    print("结论: 🟢 Go")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
