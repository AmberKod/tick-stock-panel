"""港美市场总览装配 (阶段 B-2)。

对齐 A股 ``market_overview_builder.build_market_overview`` 的返回结构,
让前端 A股深度看板组件可复用同一 schema。差异点:

- 数据源: 读独立目录 ``kline_hk_us_enriched/symbol=*`` (compute_enriched 全量列),
  与 A股 ``kline_daily_enriched/date=*`` 完全隔离, 避免污染 A股 as_of。
- 无涨停制度 → "涨停连板" 语义替换为 "强势股" (涨幅 >=5% / 新高的家数),
  字段名保持不变以复用前端组件。
- board 映射: HK 按 08 前缀分主板/创业板; US 统一 "美股"。
- 指数段: 返回核心指数 symbol/name (行情由独立指数源补充, 暂空)。
- 无概念/行业 ext_data → concept_rank/industry_rank 暂空。
"""
from __future__ import annotations

import math
import time
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.markets import get_profile

# 强势股阈值 (替代涨停; 港美无涨跌停制度)
_STRONG_UP_THRESHOLD = 0.05    # 涨幅 >=5%
_STRONG_DOWN_THRESHOLD = -0.05  # 跌幅 <=-5%

# 强势梯队分档 (替代涨停连板梯队)
_TIER_THRESHOLDS = (0.05, 0.10, 0.15, 0.20)


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _score(value: float, low: float, high: float) -> int:
    if high <= low:
        return 50
    return max(0, min(100, round((value - low) / (high - low) * 100)))


def _board(market: str, symbol: str) -> str:
    """港美板块映射。"""
    if market == "HK":
        code = symbol.split(".")[0]
        if code.startswith("08"):
            return "创业板"
        return "主板"
    # 美股无细分交易所信息, 统一归类
    return "美股"


def _market_suffix(market: str) -> str:
    return f".{market.upper()}"


def _core_indices(market: str) -> tuple[Any, ...]:
    return get_profile(market).core_indices


# 指数实时行情缓存: {market: (timestamp, {symbol: {last_price, change_pct, change_amount}})}
_INDEX_QUOTE_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}
_INDEX_QUOTE_TTL = 60.0  # 秒


def _fetch_hk_index_quotes() -> dict[str, dict]:
    """港股指数实时行情 (akshare 新浪源 stock_hk_index_spot_sina)。

    返回 {symbol: {last_price, change_pct, change_amount}}, 失败返回空 dict。
    涨跌幅口径为百分数 (如 -0.39 = -0.39%), 与 A股指数口径一致。
    """
    try:
        import akshare as ak  # type: ignore[import-untyped]
        df = ak.stock_hk_index_spot_sina()
    except Exception:
        return {}
    if df is None or len(df) == 0:
        return {}
    # 新浪源返回裸代码, 映射到内部 symbol
    wanted = {
        "HSI": "HSI.HK",        # 恒生指数
        "HSTECH": "HSTECH.HK",  # 恒生科技指数
        "HSCEI": "HSCEI.HK",    # 恒生中国企业指数
    }
    out: dict[str, dict] = {}
    for r in df.to_dict(orient="records"):
        sym = wanted.get(str(r.get("代码") or "").strip())
        if not sym:
            continue
        out[sym] = {
            "last_price": _finite(r.get("最新价")),
            "change_pct": _finite(r.get("涨跌幅")),
            "change_amount": _finite(r.get("涨跌额")),
        }
    return out


# 新浪美股指数代码 → 内部 symbol
_US_INDEX_CODE_MAP = {
    "gb_inx": "^GSPC.US",   # 标普500
    "gb_ixic": "^IXIC.US",  # 纳斯达克综合
    "gb_dji": "^DJI.US",    # 道琼斯
}


def _fetch_us_index_quotes() -> dict[str, dict]:
    """美股指数实时行情 (新浪 hq.sinajs.cn 直连)。

    返回 {symbol: {last_price, change_pct, change_amount}}, 失败返回空 dict。
    """
    url = "https://hq.sinajs.cn/list=" + ",".join(_US_INDEX_CODE_MAP)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
        )
        raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].replace("var hq_str_", "").strip()
        if key not in _US_INDEX_CODE_MAP:
            continue
        payload = line.split('"', 2)[1] if '"' in line else ""
        fields = payload.split(",")
        # 新浪美股指数: [1]最新价 [2]涨跌幅(%) [4]涨跌额
        if len(fields) < 5:
            continue
        out[_US_INDEX_CODE_MAP[key]] = {
            "last_price": _finite(fields[1]),
            "change_pct": _finite(fields[2]),
            "change_amount": _finite(fields[4]),
        }
    return out


def _fetch_index_quotes(market: str) -> dict[str, dict]:
    """带缓存的指数实时行情 (失败静默降级为空)。"""
    now = time.time()
    cached = _INDEX_QUOTE_CACHE.get(market)
    if cached and now - cached[0] < _INDEX_QUOTE_TTL:
        return cached[1]
    quotes = _fetch_hk_index_quotes() if market == "HK" else _fetch_us_index_quotes()
    _INDEX_QUOTE_CACHE[market] = (now, quotes)
    return quotes


def _load_latest_rows(data_dir: Path, market: str) -> tuple[pl.DataFrame, date | None]:
    """读取港美 enriched 最新交易日的全市场行 (含 name)。

    Returns:
        (最新日全市场指标行 DataFrame, as_of date)。无数据返回 (空 df, None)。
    """
    enriched_dir = data_dir / "kline_hk_us_enriched"
    if not enriched_dir.exists():
        return pl.DataFrame(), None
    suffix = _market_suffix(market)
    try:
        lf = pl.scan_parquet(
            str(enriched_dir / "symbol=*" / "part.parquet"),
            # 历史分区可能由不同数据源写入 (akshare 新浪源 volume=Float64,
            # yfinance 兜底历史为 Int64), 跨分区 scan 需允许整型向浮点兼容
            # 提升, 否则会报 SchemaError 导致 overview 500。
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
    except Exception:
        return pl.DataFrame(), None

    # 先确定该市场最新交易日
    dates = lf.filter(pl.col("symbol").str.ends_with(suffix)).select("date").collect()
    if dates.is_empty():
        return pl.DataFrame(), None
    as_of = dates["date"].max()
    if hasattr(as_of, "date"):
        as_of = as_of.date()

    df = (
        lf.filter(
            (pl.col("symbol").str.ends_with(suffix))
            & (pl.col("date") == as_of)
        )
        .collect()
    )
    if df.is_empty():
        return df, as_of

    # JOIN instruments 拿 name, 并用 inner join 过滤掉 universe 外的残留标的
    # (美股旧脏清单同步遗留的粉单/OTC 日K 仍在 kline_daily 目录, 需按 universe 收敛)。
    inst_path = data_dir / "instruments" / f"{market.lower()}_instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path)
            inst_cols = ["symbol", "name"]
            # 美股 universe 带 sector/industry (NASDAQ 源), 港股无 → 尽量带上供行业热度使用
            for c in ("sector", "industry"):
                if c in inst.columns:
                    inst_cols.append(c)
            inst = inst.select(inst_cols)
            if "name" not in df.columns:
                df = df.join(inst, on="symbol", how="inner")
        except Exception:
            pass
    return df, as_of


def _pct_band_rows(values: list[float]) -> list[dict]:
    bands = [
        ("<-5%", None, -0.05),
        ("-5~-3%", -0.05, -0.03),
        ("-3~-1%", -0.03, -0.01),
        ("-1~0%", -0.01, 0),
        ("0~1%", 0, 0.01),
        ("1~3%", 0.01, 0.03),
        ("3~5%", 0.03, 0.05),
        (">5%", 0.05, None),
    ]
    total = len(values) or 1
    out = []
    for label, low, high in bands:
        count = 0
        for v in values:
            if (low is None and v < high) or (high is None and v >= low) or (low is not None and high is not None and low <= v < high):
                count += 1
        out.append({"label": label, "count": count, "pct": count / total * 100})
    return out


def _top_rows(rows: list[dict], key: str, descending: bool, market: str, limit: int = 8) -> list[dict]:
    filtered = [r for r in rows if _finite(r.get(key)) is not None]
    # 涨幅/跌幅榜过滤极端脏数据 (退市重组/数据断层导致的 |涨跌幅| > 100%),
    # 如 HOS 退市归零后重组重新上市会算出 8508% 的无效 change_pct。
    if key == "change_pct":
        filtered = [r for r in filtered if abs(_finite(r.get("change_pct")) or 0) <= 1.0]
    filtered.sort(key=lambda r: _finite(r.get(key)) or 0, reverse=descending)
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "turnover_rate": None,
            "vol_ratio_5d": _finite(r.get("vol_ratio_5d")),
            "board": _board(market, str(r.get("symbol") or "")),
        }
        for r in filtered[:limit]
    ]


def _sector_rank(rows: list[dict], limit: int = 5) -> dict[str, list[dict]]:
    """按 sector 聚合行业热度榜 (美股 universe 带 sector/industry 字段, 港股无则留空)。"""
    groups: dict[str, dict] = {}
    for r in rows:
        sector = str(r.get("sector") or "").strip()
        if not sector or sector.lower() in {"nan", "none", "null"}:
            continue
        if sector not in groups:
            groups[sector] = {
                "name": sector, "count": 0, "up": 0, "down": 0,
                "amount": 0.0, "changes": [], "leader": r,
            }
        g = groups[sector]
        raw_change = _finite(r.get("change_pct"))
        if raw_change is None or abs(raw_change) > 1.0:
            # 跳过退市重组/数据断层导致的极端脏 change_pct (如 HOS 8508%),
            # 避免污染行业平均涨幅与 leader。
            continue
        change = raw_change
        g["count"] += 1
        g["changes"].append(change)
        if change > 0:
            g["up"] += 1
        elif change < 0:
            g["down"] += 1
        g["amount"] += _finite(r.get("amount")) or 0
        leader_change = _finite(g["leader"].get("change_pct"))
        if leader_change is None or change > leader_change:
            g["leader"] = r
    items: list[dict] = []
    for g in groups.values():
        changes = g["changes"]
        if not changes:
            continue
        leader = g["leader"]
        items.append({
            "name": g["name"],
            "count": g["count"],
            "avg_pct": sum(changes) / len(changes),
            "up_count": g["up"],
            "down_count": g["down"],
            "amount": g["amount"],
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": _finite(leader.get("change_pct")),
            },
        })
    leading = sorted(items, key=lambda x: x["avg_pct"], reverse=True)[:limit]
    lagging = sorted(items, key=lambda x: x["avg_pct"])[:limit]
    return {"leading": leading, "lagging": lagging}


def build_hk_us_overview(market: str, data_dir: Path, as_of: date | None = None) -> dict:
    """装配港美市场总览 (结构对齐 A股 build_market_overview)。"""
    market = market.upper()
    df, resolved_as_of = _load_latest_rows(data_dir, market)
    as_of = as_of or resolved_as_of

    _quotes = _fetch_index_quotes(market)
    indices = [
        {
            "symbol": r.symbol,
            "name": r.name,
            "last_price": _quotes.get(r.symbol, {}).get("last_price"),
            "change_pct": _quotes.get(r.symbol, {}).get("change_pct"),
            "change_amount": _quotes.get(r.symbol, {}).get("change_amount"),
        }
        for r in _core_indices(market)
    ]

    if df.is_empty():
        return _json_safe({
            "as_of": str(as_of) if as_of else None,
            "market": market,
            "quote_status": {"enabled": False, "running": False},
            "indices": indices,
            "breadth": {"total": 0, "up": 0, "down": 0, "flat": 0, "up_pct": 0, "down_pct": 0, "avg_pct": 0, "median_pct": 0, "strong_up": 0, "strong_down": 0},
            "amount": {"total": 0, "avg": 0},
            "boards": [],
            "limit": {"limit_up": 0, "broken": 0, "failed": 0, "limit_down": 0, "max_boards": 0, "seal_rate": 0, "tiers": []},
            "distribution": [],
            "trend": {"above_ma5": 0, "above_ma20": 0, "above_ma60": 0, "above_ma5_pct": 0, "above_ma20_pct": 0, "above_ma60_pct": 0, "new_high": 0, "new_low": 0},
            "activity": {"avg_turnover": 0, "high_turnover": 0, "high_vol_ratio": 0, "vol_ratio": 1},
            "radar": [],
            "emotion": {"score": 50, "label": "暂无"},
            "top_gainers": [], "top_losers": [], "turnover_leaders": [], "active_leaders": [],
            "concept_rank": {"leading": [], "lagging": []},
            "industry_rank": {"leading": [], "lagging": []},
        })

    rows = df.to_dicts()
    total = len(rows)

    # 涨跌家数 (过滤 |涨跌幅| > 100% 的极端脏数据, 避免污染 avg_pct/广度统计)
    pct_values = [_finite(r.get("change_pct")) for r in rows]
    pct_values = [v for v in pct_values if v is not None and abs(v) <= 1.0]
    up = sum(1 for v in pct_values if v > 0)
    down = sum(1 for v in pct_values if v < 0)
    flat = max(0, total - up - down)
    up_pct = up / total * 100 if total else 0
    down_pct = down / total * 100 if total else 0
    avg_pct = sum(pct_values) / len(pct_values) if pct_values else 0
    median_pct = sorted(pct_values)[len(pct_values) // 2] if pct_values else 0
    strong_up = sum(1 for v in pct_values if v >= _STRONG_UP_THRESHOLD)
    strong_down = sum(1 for v in pct_values if v <= _STRONG_DOWN_THRESHOLD)

    # 成交额
    amounts = [_finite(r.get("amount")) or 0 for r in rows]
    total_amount = sum(amounts)
    avg_amount = total_amount / total if total else 0

    # 强势梯队 (替代涨停梯队)
    tiers_map: dict[int, int] = {}
    tiers_stocks: dict[int, list] = {}
    for r in rows:
        p = _finite(r.get("change_pct"))
        if p is None:
            continue
        tier = next((i + 1 for i, t in enumerate(_TIER_THRESHOLDS) if p >= t), 0)
        if tier > 0:
            tiers_map[tier] = tiers_map.get(tier, 0) + 1
            sym = str(r.get("symbol") or "")
            if sym:
                tiers_stocks.setdefault(tier, []).append({
                    "symbol": sym,
                    "name": r.get("name") or "",
                    "amount": _finite(r.get("amount")) or 0.0,
                })
    tiers = [
        {
            "boards": k,
            "count": v,
            "stocks": sorted(tiers_stocks.get(k, []), key=lambda x: x["amount"], reverse=True)[:5],
        }
        for k, v in sorted(tiers_map.items(), key=lambda item: -item[0])
    ]

    # 均线 / 新高新低
    def above_ma(ma_key: str) -> int:
        return sum(1 for r in rows if _finite(r.get("close")) is not None and _finite(r.get(ma_key)) is not None and (_finite(r.get("close")) or 0) >= (_finite(r.get(ma_key)) or 0))

    above_ma5 = above_ma("ma5")
    above_ma20 = above_ma("ma20")
    above_ma60 = above_ma("ma60")
    new_high = sum(1 for r in rows if bool(r.get("signal_n_day_high")))
    new_low = sum(1 for r in rows if bool(r.get("signal_n_day_low")))

    # 量能 / 波动率
    vol_ratios = [_finite(r.get("vol_ratio_5d")) for r in rows]
    vol_ratios = [v for v in vol_ratios if v is not None]
    avg_vol_ratio = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 1
    high_vol_ratio = sum(1 for v in vol_ratios if v >= 1.5)
    high_vol_pct = high_vol_ratio / total * 100 if total else 0
    annual_vols = [_finite(r.get("annual_vol_20d")) for r in rows]
    annual_vols = [v for v in annual_vols if v is not None]
    avg_annual_vol = sum(annual_vols) / len(annual_vols) if annual_vols else 0

    # 板块
    boards_map: dict[str, dict] = {}
    for r in rows:
        b = _board(market, str(r.get("symbol") or ""))
        item = boards_map.setdefault(b, {"board": b, "count": 0, "up": 0, "down": 0, "amount": 0.0})
        item["count"] += 1
        change = _finite(r.get("change_pct")) or 0
        if change > 0:
            item["up"] += 1
        elif change < 0:
            item["down"] += 1
        item["amount"] += _finite(r.get("amount")) or 0
    boards = sorted(boards_map.values(), key=lambda x: x["amount"], reverse=True)
    for b in boards:
        count = b["count"] or 1
        b["up_pct"] = b["up"] / count * 100

    # 强势股率 (替代封板率)
    strong_rate = strong_up / total * 100 if total else 0
    strong_down_pct = strong_down / total * 100 if total else 0

    # 雷达 (对齐 A股 6 维, 投机维改用波动率+强势股替代涨停)
    radar = [
        {"key": "index", "label": "指数", "value": 50},
        {"key": "profit", "label": "赚钱", "value": round(_score(up_pct, 20, 80) * 0.45 + _score(avg_pct, -0.02, 0.02) * 0.25 + _score(median_pct, -0.02, 0.02) * 0.20 + _score((strong_up - strong_down) / total * 100 if total else 0, -8, 8) * 0.10)},
        {"key": "money", "label": "量能", "value": round(_score(avg_vol_ratio, 0.6, 1.8) * 0.70 + _score(high_vol_pct, 2, 12) * 0.30)},
        {"key": "speculation", "label": "强势", "value": round(_score(strong_up, 3, 60) * 0.40 + _score(avg_annual_vol, 0.15, 0.55) * 0.40 + _score(strong_rate, 2, 20) * 0.20)},
        {"key": "resilience", "label": "抗跌", "value": 100 - round(_score(down_pct, 20, 80) * 0.55 + _score(strong_down_pct, 1, 12) * 0.45)},
        {"key": "mainline", "label": "主线", "value": 50},
    ]
    emotion_score = round(sum(r["value"] for r in radar) / len(radar)) if radar else 50
    if emotion_score >= 70:
        emotion_label = "强势"
    elif emotion_score >= 55:
        emotion_label = "偏暖"
    elif emotion_score >= 45:
        emotion_label = "震荡"
    elif emotion_score >= 30:
        emotion_label = "偏冷"
    else:
        emotion_label = "冰点"

    return _json_safe({
        "as_of": str(as_of),
        "market": market,
        "quote_status": {"enabled": False, "running": False},
        "indices": indices,
        "breadth": {
            "total": total, "up": up, "down": down, "flat": flat,
            "up_pct": up_pct, "down_pct": down_pct,
            "avg_pct": avg_pct, "median_pct": median_pct,
            "strong_up": strong_up, "strong_down": strong_down,
        },
        "amount": {"total": total_amount, "avg": avg_amount},
        "boards": boards,
        "limit": {
            "limit_up": strong_up, "broken": 0, "failed": 0,
            "limit_down": strong_down, "max_boards": len(tiers),
            "seal_rate": 0, "tiers": tiers,
        },
        "distribution": _pct_band_rows(pct_values),
        "trend": {
            "above_ma5": above_ma5, "above_ma20": above_ma20, "above_ma60": above_ma60,
            "above_ma5_pct": above_ma5 / total * 100 if total else 0,
            "above_ma20_pct": above_ma20 / total * 100 if total else 0,
            "above_ma60_pct": above_ma60 / total * 100 if total else 0,
            "new_high": new_high, "new_low": new_low,
        },
        "activity": {
            "avg_turnover": 0, "high_turnover": 0,
            "high_vol_ratio": high_vol_pct, "vol_ratio": avg_vol_ratio,
        },
        "radar": radar,
        "emotion": {"score": emotion_score, "label": emotion_label},
        "top_gainers": _top_rows(rows, "change_pct", True, market),
        "top_losers": _top_rows(rows, "change_pct", False, market),
        "turnover_leaders": _top_rows(rows, "amount", True, market),
        "active_leaders": _top_rows(rows, "vol_ratio_5d", True, market),
        "concept_rank": {"leading": [], "lagging": []},
        "industry_rank": _sector_rank(rows, limit=5),
    })
