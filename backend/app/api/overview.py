"""市场总览聚合 API。"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import date
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException, Request

from app.markets.cn import CN_PROFILE
from app.services.ext_data import ExtConfig, ExtConfigStore
from app.services.market_posture import ALLOWED_MARKETS, MARKET_LABELS, compute_market_posture

router = APIRouter(prefix="/api/overview", tags=["overview"])

logger = logging.getLogger(__name__)

_CACHE_TTL = 5.0
_cache: dict[str, Any] | None = None
_cache_key: str | None = None
_cache_ts: float = 0.0
# 缓存跨线程读写锁: market_overview 在 FastAPI 线程池读, invalidate 在数据刷新线程清,
# 无锁会读到撕裂/过期状态。用模块级 Lock 守护 check-then-set 与 clear。
_cache_lock = threading.Lock()

# posture 是日级判断, 60s 缓存 (复用上面的 Lock 模式, 独立键位避免与总览互相顶掉)。
_POSTURE_CACHE_TTL = 60.0
_posture_cache: dict[str, Any] | None = None
_posture_cache_key: str | None = None
_posture_cache_ts: float = 0.0
_posture_cache_lock = threading.Lock()


def invalidate_overview_cache() -> None:
    """清空总览聚合结果缓存。

    清除数据后调用, 避免看板在 TTL 窗口内继续返回旧的聚合结果。
    """
    global _cache, _cache_key, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_key = None
        _cache_ts = 0.0


def invalidate_posture_cache() -> None:
    """清空市场态势缓存(数据刷新/regime 重算后调用)。"""
    global _posture_cache, _posture_cache_key, _posture_cache_ts
    with _posture_cache_lock:
        _posture_cache = None
        _posture_cache_key = None
        _posture_cache_ts = 0.0


def _parse_markets(raw: str) -> list[str]:
    """解析 markets=cn,hk,us;去重保序,非法值 400。"""
    wanted: list[str] = []
    for part in (raw or "").split(","):
        market = part.strip().lower()
        if not market:
            continue
        if market not in ALLOWED_MARKETS:
            raise HTTPException(400, f"markets 只能包含 {list(ALLOWED_MARKETS)} 中的值: {market}")
        if market not in wanted:
            wanted.append(market)
    if not wanted:
        raise HTTPException(400, "markets 不能为空")
    return wanted


# 核心指数 (M0: 单一事实源在 app/markets/cn.py)
CORE_INDEX_NAMES = {r.symbol: r.name for r in CN_PROFILE.core_indices}
CORE_INDEX_SYMBOLS = tuple(CORE_INDEX_NAMES.keys())

_DIMENSION_SEP = re.compile(r"[、,，;；|/\s]+")


def _dimension_field(config: ExtConfig, kind: str) -> str | None:
    candidates = ["概念", "concept", "theme"] if kind == "concept" else ["行业", "industry", "sector"]
    for candidate in candidates:
        needle = candidate.lower()
        for field in config.fields:
            haystack = f"{field.name} {field.label}".lower()
            if needle in haystack:
                return field.name
    return None


def _ext_files(data_dir, config: ExtConfig) -> list[str]:
    base = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        root = base / "timeseries"
        return [str(p) for p in sorted(root.rglob("*.parquet")) if p.is_file()]
    return [str(p) for p in sorted(base.glob("*.parquet")) if p.is_file()]


def _read_ext_rows(data_dir, config: ExtConfig, dimension_field: str) -> list[dict]:
    files = _ext_files(data_dir, config)
    if not files:
        return []
    try:
        df = pl.read_parquet(files, hive_partitioning=True)
    except TypeError:
        try:
            df = pl.read_parquet(files)
        except Exception:
            return []
    except Exception:
        return []
    if df.is_empty() or dimension_field not in df.columns:
        return []

    if config.mode == "timeseries" and "date" in df.columns:
        latest = df.get_column("date").max()
        if latest is not None:
            df = df.filter(pl.col("date") == latest)

    symbol_cols = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_cols.append(str(mapping["col"]))
    cols = []
    for col in [dimension_field, *symbol_cols]:
        if col in df.columns and col not in cols:
            cols.append(col)
    return df.select(cols).to_dicts()


def _dimension_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = [v.strip() for v in _DIMENSION_SEP.split(str(raw).strip()) if v.strip()]
    return values


def _symbol_keys(row: dict, config: ExtConfig) -> list[str]:
    fields = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            fields.append(str(mapping["col"]))

    keys: list[str] = []
    for field in fields:
        raw = row.get(field)
        if raw is None:
            continue
        text = str(raw).strip().upper()
        if not text:
            continue
        keys.append(text)
        if "." in text:
            keys.append(text.split(".", 1)[0])
    return keys


def _dimension_rank(rows: list[dict], request: Request, kind: str, limit: int = 5, level: int | None = None) -> dict:
    if not rows:
        return {"leading": [], "lagging": []}

    quote_map: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quote_map[symbol] = row
        quote_map[symbol.split(".", 1)[0]] = row

    store = ExtConfigStore(request.app.state.repo.store.data_dir)
    groups: dict[str, dict[str, dict]] = {}
    for config in store.load_all():
        field = _dimension_field(config, kind)
        if not field:
            continue
        for ext_row in _read_ext_rows(request.app.state.repo.store.data_dir, config, field):
            quote = None
            for key in _symbol_keys(ext_row, config):
                quote = quote_map.get(key)
                if quote:
                    break
            if not quote:
                continue
            symbol = str(quote.get("symbol") or "")
            for value in _dimension_values(ext_row.get(field)):
                # 行业按 "-" 拆分级: "银行-银行-股份制银行" → level=2 取"银行"(二级)
                if level is not None and "-" in value:
                    parts = value.split("-")
                    value = parts[level - 1] if level <= len(parts) else parts[-1]
                groups.setdefault(value, {})[symbol] = quote

    items = []
    for name, by_symbol in groups.items():
        stocks = list(by_symbol.values())
        changes = [_finite(s.get("change_pct")) for s in stocks]
        changes = [v for v in changes if v is not None]
        if not changes:
            continue
        leader = max(stocks, key=lambda s: _finite(s.get("change_pct")) or -999)
        items.append({
            "name": name,
            "count": len(stocks),
            "avg_pct": sum(changes) / len(changes),
            "up_count": sum(1 for v in changes if v > 0),
            "down_count": sum(1 for v in changes if v < 0),
            "amount": sum(_finite(s.get("amount")) or 0 for s in stocks),
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": _finite(leader.get("change_pct")),
            },
        })

    leading = sorted(items, key=lambda x: x["avg_pct"], reverse=True)[:limit]
    lagging = sorted(items, key=lambda x: x["avg_pct"])[:limit]
    return {"leading": leading, "lagging": lagging}


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


def _board(symbol: str) -> str:
    if symbol.endswith(".BJ"):
        return "北交所"
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith(("688", "689")):
        return "科创板"
    if symbol.endswith(".SH"):
        return "沪主板"
    if symbol.endswith(".SZ"):
        return "深主板"
    return "其他"


def _score(value: float, low: float, high: float) -> int:
    if high <= low:
        return 50
    return max(0, min(100, round((value - low) / (high - low) * 100)))


def _quote_status(request: Request) -> dict:
    qs = getattr(request.app.state, "quote_service", None)
    if not qs:
        return {"enabled": False, "running": False, "quote_age_ms": None, "is_trading_hours": False}
    return qs.status()


def _index_quotes(request: Request, as_of: date | None = None) -> list[dict]:
    qs = getattr(request.app.state, "quote_service", None)
    rows: list[dict] = []
    if qs and as_of is None:
        df = qs.get_index_quotes(list(CORE_INDEX_SYMBOLS))
        if not df.is_empty():
            rows = df.to_dicts()

    if not rows:
        repo = getattr(request.app.state, "repo", None)
        if repo:
            placeholders = ", ".join("?" for _ in CORE_INDEX_SYMBOLS)
            try:
                db_rows = repo.execute_all(
                    f"""
                    WITH ranked AS (
                        SELECT symbol, date, close,
                               row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                        FROM kline_index_daily
                        WHERE symbol IN ({placeholders})
                          AND (? IS NULL OR date <= ?)
                    ), latest AS (
                        SELECT symbol,
                               max(CASE WHEN rn = 1 THEN date END) AS date,
                               max(CASE WHEN rn = 1 THEN close END) AS last_price,
                               max(CASE WHEN rn = 2 THEN close END) AS prev_close
                        FROM ranked
                        WHERE rn <= 2
                        GROUP BY symbol
                    )
                    SELECT symbol, date, last_price, prev_close
                    FROM latest
                    """,
                    [*CORE_INDEX_SYMBOLS, as_of, as_of],
                )
            except Exception:
                db_rows = []
            for symbol, dt, last_price, prev_close in db_rows:
                change_amount = None
                change_pct = None
                lp = _finite(last_price)
                pc = _finite(prev_close)
                if lp is not None and pc not in (None, 0):
                    change_amount = lp - pc
                    change_pct = change_amount / pc * 100
                rows.append({
                    "symbol": symbol,
                    "name": CORE_INDEX_NAMES.get(symbol),
                    "date": str(dt) if dt else None,
                    "last_price": lp,
                    "close": lp,
                    "prev_close": pc,
                    "change_amount": change_amount,
                    "change_pct": change_pct,
                })

    by_symbol = {r.get("symbol"): r for r in rows}
    out = []
    for symbol in CORE_INDEX_SYMBOLS:
        r = by_symbol.get(symbol, {"symbol": symbol})
        out.append({
            "symbol": symbol,
            "name": r.get("name") or CORE_INDEX_NAMES[symbol],
            "last_price": _finite(r.get("last_price") if r.get("last_price") is not None else r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "change_amount": _finite(r.get("change_amount")),
        })
    return out


def _top_rows(rows: list[dict], key: str, descending: bool, limit: int = 8) -> list[dict]:
    filtered = [r for r in rows if _finite(r.get(key)) is not None]
    filtered.sort(key=lambda r: _finite(r.get(key)) or 0, reverse=descending)
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "turnover_rate": _finite(r.get("turnover_rate")),
            "board": _board(str(r.get("symbol") or "")),
        }
        for r in filtered[:limit]
    ]


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


def _build_overview(request: Request, as_of: date | None = None) -> dict:
    """装配市场总览(委托给 services.market_overview_builder,保持行为一致)。

    逻辑已抽离至 build_market_overview,以解耦对 Request 的依赖,
    使大盘复盘等无 Request 的调用方可复用同一装配逻辑。
    """
    from app.services.market_overview_builder import build_market_overview
    return build_market_overview(
        repo=request.app.state.repo,
        quote_service=getattr(request.app.state, "quote_service", None),
        depth_service=getattr(request.app.state, "depth_service", None),
        as_of=as_of,
    )


@router.get("/market")
def market_overview(request: Request, as_of: date | None = None):
    """总览页单次请求聚合数据，避免前端拉全市场明细后再计算。"""
    global _cache, _cache_key, _cache_ts
    now = time.time()
    cache_key = as_of.isoformat() if as_of else "latest"
    # 读缓存持锁, 避免与 invalidate 的 clear 竞态读到撕裂状态
    with _cache_lock:
        if _cache is not None and _cache_key == cache_key and (now - _cache_ts) < _CACHE_TTL:
            return _cache
    # 装配在锁外进行 (耗时), 允许并发未命中时各自构建, 不长时间持锁串行化请求
    data = _build_overview(request, as_of)
    with _cache_lock:
        _cache = data
        _cache_key = cache_key
        _cache_ts = now
    return data


@router.get("/posture")
def market_posture(request: Request, markets: str = "cn,hk,us", as_of: date | None = None):
    """三市场态势总览 (面板 1)。

    每个市场按 regime / breadth / hotspots / industry 四个维度投票:
    一票否决 (regime 偏弱弱 或 上涨占比 < 35%) → 直接判防守;
    否则 ≥3 票进攻 → 进攻, ≥3 票防守 → 防守, 否则均衡;
    **不可用的维度不计入分母** (既不当 0 也不当中性), 全不可用 → unknown。

    只读落盘数据, 不触发任何同步; 结果 60s 缓存。
    """
    global _posture_cache, _posture_cache_key, _posture_cache_ts
    wanted = _parse_markets(markets)
    cache_key = f"{','.join(wanted)}|{as_of.isoformat() if as_of else 'latest'}"
    now = time.time()
    with _posture_cache_lock:
        if (
            _posture_cache is not None
            and _posture_cache_key == cache_key
            and (now - _posture_cache_ts) < _POSTURE_CACHE_TTL
        ):
            return _posture_cache

    repo = getattr(request.app.state, "repo", None)
    store = getattr(repo, "store", None)
    data_dir = getattr(store, "data_dir", None)
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)

    items = []
    for market in wanted:
        try:
            items.append(
                compute_market_posture(
                    market,
                    repo=repo,
                    data_dir=data_dir,
                    quote_service=quote_service,
                    depth_service=depth_service,
                    as_of=as_of,
                )
            )
        except Exception as e:
            # 单市场异常不能带崩整屏: 显式降级为 unknown + 原因, 让前端显示"不可用"
            # 而不是渲染一个"风平浪静"的空态。
            logger.warning("posture: %s 判定失败: %s", market, e)
            items.append({
                "market": market,
                "market_label": MARKET_LABELS.get(market, market),
                "posture": "unknown",
                "posture_label": "未知",
                "votes": [],
                "unavailable_dims": ["regime", "breadth", "hotspots", "industry"],
                "evidence": [{"dim": "_error", "text": f"态势计算失败: {e}"}],
                "veto": None,
                "tally": {"attack": 0, "neutral": 0, "defend": 0, "counted": 0},
                "as_of": None,
                "freshness": {"regime_as_of": None, "hotspot_age_hours": None},
                "source_errors": [f"posture: {e}"],
            })

    payload = _json_safe({
        "as_of": next((m.get("as_of") for m in items if m.get("as_of")), None),
        "markets": items,
    })
    with _posture_cache_lock:
        _posture_cache = payload
        _posture_cache_key = cache_key
        _posture_cache_ts = time.time()
    return payload
