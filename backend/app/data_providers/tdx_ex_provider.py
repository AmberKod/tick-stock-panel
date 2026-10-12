"""通达信扩展市场(MAC-EX, TCP 7727)分钟 K —— 港美股分钟线的唯一真源。

为什么需要它(2026-10-12 实测, 推翻此前"港美股分钟K无真源"的结论):
- 腾讯 ``mkline`` 对港股/美股一律返回 ``{"code":-1,"param error"}`` ⇒ 那条结论
  只针对腾讯源成立, 不应推广成"没有源"。
- vendored easy_tdx 的 ``MacExClient`` 实测可取: 美股/港股 MIN_1 / MIN_5 /
  MIN_15 / MIN_30 / MIN_60 全部有数据。
- 容器内**裸 TCP 直连 7727 可达**(SEC 是 HTTPS + SNI 被挡才逼出 sec_relay,
  通达信没有这层), 因此不需要新增宿主常驻依赖。

上游硬限制(改不了, 端点如实声明, 不用日K降采样凑数):
- **单次请求最多 700 根**: 实测请求 800 / 3200 都只回 700。分钟K 因此只有近日
  日内深度 —— 美股 m1 约 1 个交易日、港股 m1 约 3 个交易日。
- **时间是服务器下发的北京时间**: 美股 AAPL 的 30 分钟线最后一根是 04:00
  (美东 16:00 收盘 + 夏令时 12h), 与港股当日 10:12 同一时区口径。
- **价格是 float32 编码**: ``336.6400146484375`` 这种毛刺必须量化后再下发,
  否则前端会出现"看起来随机"的末位抖动。

法务边界: 只使用上游的**在线协议**部分(声明为独立实现)。离线模块
(``offline/gbbq.py``, XOR 密钥移植自 pytdx, 而 pytdx 无 license 文件)已在 vendor
时整目录删除, 本项目不打包、不引用。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from app._vendor.easy_tdx.ex.mac_client import MacExClient
from app._vendor.easy_tdx.mac.enums import Adjust, ExMarket, Period

logger = logging.getLogger(__name__)

#: 登记在 app/services/source_gate.py 白名单里的源名。
SOURCE = "tdx-ex"

#: 上游单次请求上限(实测 800/3200 均只回 700)。超过就截断并标注, 不多打一次请求。
MAX_BARS_PER_REQUEST = 700

#: 前端周期串 → 上游 Period。与 hk_us_deep 的 /minute 端点参数保持一致。
_PERIODS: dict[str, Period] = {
    "m1": Period.MIN_1,
    "m5": Period.MIN_5,
    "m15": Period.MIN_15,
    "m30": Period.MIN_30,
    "m60": Period.MIN_60,
}

#: 港股主板 vs 创业板在上游是不同市场码(HK_MAIN_BOARD=31 / HK_GEM=48), 但我们
#: 拿不到"某代码属于哪个板"的可靠映射 —— 统一走主板码, 实测 00700/00939/09988
#: 均可取; 万一某天创业板票取不到, 表现是 0 行(端点 fail-closed), 不会串票。


class TdxExUnavailableError(RuntimeError):
    """所有通达信扩展市场节点都连不上 / 取数失败 —— 属故障, 不是"没有源"。"""


# ───────────────────────────────────────────── 缓存(分钟K 实时性高, 短缓存)

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> object | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return None
    expires_at, value = hit
    if time.time() >= expires_at:
        return None
    return value


def _cache_put(key: str, value: list[dict], ttl: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    """测试用: 清空进程内缓存。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ───────────────────────────────────────────── 参数

def _settings():
    from app.config import settings

    return settings


def _hosts() -> list[str]:
    raw = str(_settings().tdx_ex_hosts or "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _split(symbol: str) -> tuple[str, str]:
    """``00700.HK`` / ``AAPL.US`` → ``("HK", "00700")``。

    与 hk_us_deep_provider 同一套后缀约定(该端点的 symbol 由前端整页传入)。
    """
    s = str(symbol or "").strip().upper()
    for suffix, market in ((".HK", "HK"), (".US", "US")):
        if s.endswith(suffix):
            code = s[: -len(suffix)].strip()
            if code:
                return market, code
    raise ValueError(f"symbol 需带 .HK/.US 后缀: {symbol!r}")


def _market_code(market: str) -> int:
    return int(ExMarket.US_STOCK if market == "US" else ExMarket.HK_MAIN_BOARD)


# ───────────────────────────────────────────── 取数

def _round(v: object, ndigits: int) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, ndigits)


def _rows_from_frame(frame) -> list[dict]:
    """上游 DataFrame → 下发 rows。价格统一量化(上游是 float32, 有毛刺)。"""
    rows: list[dict] = []
    for rec in frame.to_dict("records"):
        ts = rec.get("datetime")
        if isinstance(ts, datetime):
            dt = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt = str(ts)[:19]
        rows.append({
            "datetime": dt,
            "open": _round(rec.get("open"), 4),
            "high": _round(rec.get("high"), 4),
            "low": _round(rec.get("low"), 4),
            "close": _round(rec.get("close"), 4),
            "volume": _round(rec.get("vol"), 2),
            "amount": _round(rec.get("amount"), 2),
        })
    rows.sort(key=lambda r: r["datetime"])
    return rows


def _fetch_once(host: str, market: str, code: str, period: Period, count: int):
    """连一个节点 → 登录 → 取一次 K 线。

    单独抽出来是因为**降级策略在调用方**(_fetch): 节点故障是常态, 必须能逐个
    试下去; 而"单次尝试"是最小的可打桩单元(测试桩掉它即可验证降级语义,
    不需要真的建 TCP 连接)。
    """
    settings = _settings()
    with MacExClient(host=host, port=int(settings.tdx_ex_port),
                     timeout=float(settings.tdx_ex_timeout_s),
                     auto_reconnect=False) as client:
        return client.goods_kline(_market_code(market), code, period,
                                  start=0, count=count, adjust=Adjust.NONE)


def _fetch(market: str, code: str, period: Period, count: int):
    """逐个节点尝试, 第一个成功的返回 DataFrame。全部失败抛 TdxExUnavailableError。"""
    hosts = _hosts()
    if not hosts:
        raise TdxExUnavailableError("tdx_ex_hosts 为空: 未配置通达信扩展市场节点")
    last_error: Exception | None = None
    for host in hosts:
        try:
            return _fetch_once(host, market, code, period, count)
        except Exception as exc:  # 节点故障是常态, 逐个降级(宽捕获是刻意的)
            last_error = exc
            logger.warning("tdx ex node failed (host=%s market=%s code=%s): %s",
                           host, market, code, exc)
    raise TdxExUnavailableError(f"全部 {len(hosts)} 个节点均失败: {last_error}")


def minute_bars(symbol: str, period: str = "m30", limit: int = 320) -> dict:
    """港美股分钟 K。

    ``period`` ∈ {m1,m5,m15,m30,m60}; ``limit`` 1..700(超出截断, 见模块头硬限制)。
    """
    key = str(period or "").strip().lower()
    if key not in _PERIODS:
        raise ValueError(f"bad_period: {period!r} (m1/m5/m15/m30/m60)")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"bad_limit: {limit!r}")
    if limit < 1:
        raise ValueError(f"bad_limit: {limit} (1..{MAX_BARS_PER_REQUEST})")

    market, code = _split(symbol)
    truncated = limit > MAX_BARS_PER_REQUEST
    count = min(limit, MAX_BARS_PER_REQUEST)

    cache_key = f"tdxmin:{market}:{code}:{key}:{count}"
    cached = _cache_get(cache_key)
    if isinstance(cached, list):
        return _payload(market, symbol, key, count, cached, truncated, cached=True)

    frame = _fetch(market, code, _PERIODS[key], count)
    rows = _rows_from_frame(frame) if frame is not None and len(frame) else []
    if not rows:
        # 空不是"没数据", 而是该代码在这个市场/周期确实取不到 —— 交给端点判空
        return _payload(market, symbol, key, count, [], truncated, cached=False)
    _cache_put(cache_key, rows, float(_settings().tdx_ex_cache_ttl_s))
    return _payload(market, symbol, key, count, rows, truncated, cached=False)


def _payload(market: str, symbol: str, period: str, count: int, rows: list[dict],
             truncated: bool, *, cached: bool) -> dict:
    return {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "period": period,
        "requested": count,
        "rows": rows,
        # 时间是服务器下发的北京时间(美股最后一根 04:00 = 美东 16:00 收盘)。
        "timezone": "Asia/Shanghai",
        "truncated": truncated,
        "max_bars": MAX_BARS_PER_REQUEST,
        "cached": cached,
        "note": (f"单次请求上限 {MAX_BARS_PER_REQUEST} 根(上游限制), 已截断; "
                 f"分钟K 仅有近日日内深度") if truncated else None,
    }


def capabilities_note() -> str:
    """给 /capabilities 用的一句话说明。"""
    return (f"分钟K 走通达信扩展市场(MAC-EX 7727), 单次上限 {MAX_BARS_PER_REQUEST} 根, "
            f"时间为北京时间; 深度有限(美股 m1 约 1 个交易日)")
