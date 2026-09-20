"""港美异动监控 — 动量口径的接近度统计 (对齐 A股 abnormal_moves 的输出 schema)。

与 A股版的关键差异 (港美无交易所异动披露制度, 规则口径自定):
- A股: N 日累计涨跌幅 **偏离对应指数** 的偏离值 vs 交易所披露阈值。
- 港美: 纯动量口径 (无本地指数基准日K, 偏离退化为绝对动量);
  阈值参考 A股主板并放宽 — 3日 ±25% / 10日 +120%(-60%) / 30日 +240%(-80%)。
- 数据源: kline_hk_us_enriched 全市场最新行 (momentum_10d/30d 已预计算;
  3 日动量从近 3 行 change_pct 复利连乘 (1+x)-1 现算)。
- 无盘中实时叠加 (港美实时行情逐股拉取, 无全市场快照, 盘中增值不覆盖);
  输出即最近已完成交易日的快照。

输出 schema 与 A股 build_overview 一致 (rules/asof/rows/status 常量),
前端 AbnormalMoves 页与个股弹窗信息条可直接消费。
"""
from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

# ── 规则表 (自定口径, 见模块 docstring) ───────────────────

HK_US_THRESHOLDS: dict[int, tuple[float, float]] = {
    3: (0.25, 0.25),
    10: (1.20, 0.60),
    30: (2.40, 0.80),
}

HK_US_RULES_META: list[dict[str, Any]] = [{
    "board": "港美市场",
    "st": False,
    "thresholds": {f"{k}d": {"up": u, "down": d} for k, (u, d) in HK_US_THRESHOLDS.items()},
    "note": "港美无交易所异动披露制度, 阈值自定 (纯动量口径, 参考A股主板放宽): "
            "3日±25% / 10日+120%(-60%) / 30日+240%(-80%)",
}]

_STATUS_TRIGGERED = "triggered"
_STATUS_EDGE = "edge"
_STATUS_WATCH = "watch"


def _status_of(closeness: float) -> str:
    if closeness >= 1.0:
        return _STATUS_TRIGGERED
    if closeness >= 0.7:
        return _STATUS_EDGE
    return _STATUS_WATCH


# ── 快照 (60s 进程内缓存) ──────────────────────────────────

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {}
_CACHE_TTL = 60.0


def _market_suffix(market: str) -> str:
    return {"HK": ".HK", "US": ".US"}.get(market.upper(), ".HK")


def _snapshot(data_dir: Path, market: str) -> dict[str, Any]:
    """港美全市场最新行的动量快照 (60s 缓存)。

    Returns: {"_ts", "as_of", "rows": {symbol: {name, close, rt_pct, mom3, mom10, mom30}}}
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(market)
        if cached is not None and now - cached["_ts"] < _CACHE_TTL:
            return cached

    enriched_dir = data_dir / "kline_hk_us_enriched"
    rows: dict[str, dict[str, Any]] = {}
    as_of: date | None = None
    if enriched_dir.exists():
        try:
            lf = pl.scan_parquet(
                str(enriched_dir / "symbol=*" / "part.parquet"),
                cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            )
            suffix = _market_suffix(market)
            scoped = lf.filter(pl.col("symbol").str.ends_with(suffix))
            dates = scoped.select("date").collect()
            if not dates.is_empty():
                latest = dates["date"].max()
                if hasattr(latest, "date"):
                    latest = latest.date()
                as_of = latest

                need_cols = [c for c in (
                    "symbol", "close", "change_pct", "momentum_10d", "momentum_30d",
                ) if c in scoped.collect_schema().names()]
                latest_rows = scoped.filter(pl.col("date") == latest).select(need_cols).collect()

                # 3日动量: 取每只票最近 3 行 change_pct 复利连乘 (1+x1)(1+x2)(1+x3)-1;
                # null 行被 product 忽略 (次新股窗口不足时按实际行数近似)
                if not latest_rows.is_empty():
                    tail3 = (
                        scoped.sort("date").group_by("symbol").tail(3)
                        .group_by("symbol").agg(
                            ((pl.col("change_pct") + 1).product() - 1).alias("mom3"),
                        )
                        .select(["symbol", "mom3"])
                        .collect()
                    )
                    latest_rows = latest_rows.join(tail3, on="symbol", how="left")
                    name_map = _instrument_names(data_dir, market)
                    for r in latest_rows.iter_rows(named=True):
                        sym = str(r["symbol"])
                        rows[sym] = {
                            "name": name_map.get(sym),
                            "close": r.get("close"),
                            "rt_pct": r.get("change_pct"),
                            "mom3": r.get("mom3"),
                            "mom10": r.get("momentum_10d"),
                            "mom30": r.get("momentum_30d"),
                        }
        except Exception:
            rows = {}

    payload = {"_ts": now, "as_of": as_of, "rows": rows}
    with _cache_lock:
        _cache[market] = payload
    return payload


_name_cache_lock = threading.Lock()
_name_cache: dict[str, dict[str, str]] = {}


def _instrument_names(data_dir: Path, market: str) -> dict[str, str]:
    """instruments parquet → {symbol: name} (600s 缓存)。"""
    with _name_cache_lock:
        cached = _name_cache.get(market)
        if cached is not None:
            return cached
    fname = {"HK": "hk_instruments.parquet", "US": "us_instruments.parquet"}.get(market.upper())
    out: dict[str, str] = {}
    if fname:
        path = data_dir / "instruments" / fname
        if path.exists():
            try:
                df = pl.read_parquet(path, columns=["symbol", "name"])
                out = dict(zip(df["symbol"].to_list(), df["name"].to_list(), strict=False))
            except Exception:
                out = {}
    with _name_cache_lock:
        _name_cache[market] = out
    return out


# ── 总览 ───────────────────────────────────────────────────

def build_hk_us_abnormal_overview(
    data_dir: Path,
    market: str,
    *,
    min_closeness: float = 0.5,
    limit: int = 200,
) -> dict[str, Any]:
    """港美异动总览: 与 A股 build_overview 同 schema。"""
    snap = _snapshot(data_dir, market)
    thresholds = HK_US_THRESHOLDS

    out_rows: list[dict[str, Any]] = []
    for symbol, base in snap["rows"].items():
        windows: dict[str, dict[str, Any]] = {}
        max_closeness = 0.0
        for n, mom_key in ((3, "mom3"), (10, "mom10"), (30, "mom30")):
            mom = base.get(mom_key)
            if mom is None:
                continue
            up_t, down_t = thresholds[n]
            threshold = up_t if mom >= 0 else down_t
            closeness = abs(mom) / threshold if threshold > 0 else 0.0
            windows[f"{n}d"] = {
                "value": round(mom, 4),
                "threshold": threshold,
                "closeness": round(closeness, 4),
            }
            max_closeness = max(max_closeness, closeness)
        if not windows or max_closeness < min_closeness:
            continue
        out_rows.append({
            "symbol": symbol,
            "name": base.get("name"),
            "board": "港美市场",
            "st": False,
            "close": base.get("close"),
            "rt_pct": base.get("rt_pct"),
            "windows": windows,
            "max_closeness": round(max_closeness, 4),
            "status": _status_of(max_closeness),
        })

    out_rows.sort(key=lambda r: r["max_closeness"], reverse=True)
    counts = {
        _STATUS_TRIGGERED: sum(1 for r in out_rows if r["status"] == _STATUS_TRIGGERED),
        _STATUS_EDGE: sum(1 for r in out_rows if r["status"] == _STATUS_EDGE),
        _STATUS_WATCH: sum(1 for r in out_rows if r["status"] == _STATUS_WATCH),
    }
    return {
        "asof": time.time(),
        "as_of": snap["as_of"].isoformat() if snap["as_of"] else None,
        "market": market.upper(),
        "rules": HK_US_RULES_META,
        "counts": counts,
        "total": len(out_rows),
        "rows": out_rows[:limit],
    }
