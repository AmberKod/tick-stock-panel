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

from app._vendor.easy_tdx.codec.bitmap import PresetField
from app._vendor.easy_tdx.ex.commands.get_instrument_quote import GetExInstrumentQuoteCmd
from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd
from app._vendor.easy_tdx.ex.mac_client import MacExClient
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection
from app._vendor.easy_tdx.mac.commands import (
    BoardListCmd,
    BoardMembersQuotesCmd,
    SymbolBelongBoardCmd,
    SymbolInfoCmd,
    TickChartsCmd,
)
from app._vendor.easy_tdx.mac.enums import Adjust, ExBoardType, ExMarket, Period

logger = logging.getLogger(__name__)

#: 登记在 app/services/source_gate.py 白名单里的源名。
SOURCE = "tdx-ex"

#: 上游单次请求上限(实测 800/3200 均只回 700)。超过就截断并标注, 不多打一次请求。
MAX_BARS_PER_REQUEST = 700

#: 行情快照/盘口这类"看一眼就过时"的数据用固定的短 TTL: 比分钟K (settings) 短得多,
#: 否则前端轮询到的是一分钟前的价格, 而 K 线那头的口径已经不一样了。
LIVE_CACHE_TTL_S = 10.0

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


def _cache_put(key: str, value: object, ttl: float) -> None:
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


def _exec_once(host: str, cmd_factory) -> object:
    """在一个节点上: 建连 → login → 执行**一条**命令 → 关连接。

    为什么每条命令都重建连接(而不是像 minute_bars 那样复用 client):
    MAC-EX 服务器对不支持/畸形请求的处理是直接**断开 TCP** —— 同一个连接上先
    发一条它被挑到毛病就再也收不到任何响应。实测第一轮探测就是所有命令连着发,
    结果从 capital_flow 往后全报"连接被服务器关闭", 白排了半天。
    """
    settings = _settings()
    conn = ExTdxConnection(host=host, port=int(settings.tdx_ex_port),
                           timeout=float(settings.tdx_ex_timeout_s), mac_ex_mode=True)
    conn.connect()
    try:
        conn.execute(MacExLoginCmd())
        return conn.execute(cmd_factory())
    finally:
        try:
            conn.close()
        except Exception as exc:  # 关连接失败无关于取数结果
            logger.debug("tdx ex close failed: %s", exc)


def _exec(cmd_factory) -> object:
    """逐个节点执行一条 MAC 命令; 全挂则抛 TdxExUnavailableError。"""
    hosts = _hosts()
    if not hosts:
        raise TdxExUnavailableError("tdx_ex_hosts 为空: 未配置通达信扩展市场节点")
    last_error: Exception | None = None
    for host in hosts:
        try:
            return _exec_once(host, cmd_factory)
        except Exception as exc:  # 节点故障是常态, 逐个降级(宽捕获是刻意的)
            last_error = exc
            logger.warning("tdx ex node failed (host=%s cmd=%s): %s", host, cmd_factory, exc)
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


# ───────────────────────────────────────────── 实时快照 / 五档盘口

def snapshot(symbol: str) -> dict:
    """个股实时快照 —— 上游「SymbolInfo」(0x... symbol_info)。

    值钱的地方: ``pre_close`` 是**上游直接给的昨日收盘**。此前 intraday 的昨收
    要从上一会话最后一根 m1(或本地日K)反推, 绕一圈还可能反推错(session key
    判错就会错一整个交易日); 这里一次拿到, 且与自己的分时行情同源。
    另外还有换手率 / 均价 / 内盘外盘 —— A股那套老面板有, 港美股此前没有。
    """
    market, code = _split(symbol)
    cache_key = f"tdxsnap:{market}:{code}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: SymbolInfoCmd(_market_code(market), code))
    if raw is None:
        raise TdxExUnavailableError(f"snapshot: {symbol} 上游返回空")

    def _f(key: str, nd: int = 4) -> float | None:
        return _round(getattr(raw, key, None), nd)

    prev_close = _f("pre_close")
    last = _f("close")
    # turnover 推断为**千分比**: 00700 上游 0.1541 → 0.01541%, 按成交量/总股本实算
    # 0.01516%; AAPL 上游 0.000769 → 0.0000769%, 实算 0.0000764%。两例都吻合,
    # 但没有三方核对, 所以标 derived 让前端写清口径。
    turnover_raw = _f("turnover", 8)
    moment = getattr(raw, "time", None)
    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "name": getattr(raw, "name", None) or None,
        "prev_close": prev_close,
        "open": _f("open"),
        "high": _f("high"),
        "low": _f("low"),
        "last": last,
        "avg_price": _f("avg"),
        "volume": _f("vol", 2),
        "amount": _f("amount", 2),
        "turnover_pct": None if turnover_raw is None else round(turnover_raw / 10, 6),
        "inside_volume": _f("inside_volume", 2),
        "outside_volume": _f("outside_volume", 2),
        "activity": _f("activity", 2),
        # 这里的 volume **已经是股数**(实测 amount/volume 就等于均价:
        # 00700 → 5983558144/14011738=427.05=均价) —— 与 minute_bars 里的
        # 成交量单位不同(港股那头是手, 要 ×100)。两条线别混看。
        "volume_unit": "share",
        "change": None if (last is None or prev_close is None) else round(last - prev_close, 4),
        "pct": (None if (last is None or not prev_close)
                else round((last - prev_close) / prev_close * 100, 4)),
        "timezone": "Asia/Shanghai",
        "server_time": moment.strftime("%Y-%m-%d %H:%M:%S") if hasattr(moment, "strftime") else None,
        "derived": ["turnover_pct", "change", "pct"],
        "cached": False,
    }
    _cache_put(cache_key, payload, LIVE_CACHE_TTL_S)
    return payload


def orderbook(symbol: str) -> dict:
    """五档盘口 —— 上游「GetInstrumentQuote」。

    实测口径差异: **美股给真五档**(AAPL bid 336.62×5 / ask 336.74×8), **港股档位
    恒为 0**(收盘后/上游不返回) —— 后者不能画成"0 元挂单", 用 depth_available=false
    显式标注由前端写明"上游不返回"。
    """
    market, code = _split(symbol)
    cache_key = f"tdxob:{market}:{code}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: GetExInstrumentQuoteCmd(_market_code(market), code))
    if raw is None:
        raise TdxExUnavailableError(f"orderbook: {symbol} 上游返回空")

    def _levels(prefix: str) -> list[dict]:
        out = []
        for i in range(1, 6):
            price = _round(getattr(raw, f"{prefix}{i}", None), 4)
            vol = _round(getattr(raw, f"{prefix}_vol{i}", None), 2)
            out.append({"price": price, "volume": vol})
        return out

    bids, asks = _levels("bid"), _levels("ask")
    # 只有**价和量都 > 0** 才算真档; 上游对某些市场会把整段填 0 而不是不返回。
    def _filled(levels: list[dict]) -> int:
        return sum(1 for lv in levels if (lv["price"] or 0) > 0 and (lv["volume"] or 0) > 0)

    bid_levels, ask_levels = _filled(bids), _filled(asks)
    prev_close = _round(getattr(raw, "pre_close", None), 4)
    last = _round(getattr(raw, "price", None), 4)
    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "prev_close": prev_close,
        "last": last,
        "bids": bids,
        "asks": asks,
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "depth_available": (bid_levels + ask_levels) > 0,
        "volume_unit": "share",
        "current_volume": _round(getattr(raw, "xianliang", None), 2),
        "total_volume": _round(getattr(raw, "zongliang", None), 2),
        "inside_volume": _round(getattr(raw, "neipan", None), 2),
        "outside_volume": _round(getattr(raw, "waipan", None), 2),
        "timezone": "Asia/Shanghai",
        "note": None if (bid_levels + ask_levels) > 0 else (
            "上游(MAC-EX)对该市场不返回买卖档: 买卖价/量全为 0 —— 不画、不猜, "
            "仅展示已到位的总量/内外盘"),
        "cached": False,
    }
    _cache_put(cache_key, payload, LIVE_CACHE_TTL_S)
    return payload


# ───────────────────────────────────────────── 板块(所属 / 列表 / 成分股)

#: 板块类型 → 上游 ExBoardType。前端传缩写, 避免把 0..5 这种魔数散落到 UI。
BOARD_KINDS: dict[str, int] = {
    "HK_ALL": int(ExBoardType.HK_ALL),
    "HK_GN": int(ExBoardType.HK_GN),
    "HK_HY": int(ExBoardType.HK_HY),
    "US_ALL": int(ExBoardType.US_ALL),
    "US_GN": int(ExBoardType.US_GN),
    "US_HY": int(ExBoardType.US_HY),
}

#: 单次成分股请求上限(受报价批量限制, 超过要分页)。
MAX_BOARD_MEMBERS_PER_PAGE = 80


def _to_board_code(board_code: str) -> int:
    """``US0026`` / ``HK0211`` → 协议整数码。

    实测只接受 int(``US0026``→30026): 直接把字符串喂进去会静默拿到一批牛头不对
    马嘴的行, 所以这层转换不能省。
    """
    from app._vendor.easy_tdx.mac.client import _convert_board_code

    s = str(board_code or "").strip().upper()
    if not s:
        raise ValueError("bad_board_code: 空板块代码")
    try:
        return int(_convert_board_code(s))
    except Exception as exc:
        raise ValueError(f"bad_board_code: {board_code!r} (应为 US####/HK####)") from exc


def belong_boards(symbol: str) -> dict:
    """个股所属板块 —— 上游 ``SymbolBelongBoard``。

    实测 AAPL 有 17 条(美股-计算机硬件 / 美股-消费电子…), 00700 有 23 条 ——
    这是港美股此前完全没有的"归类 + 同板块涨跌"维度; 板块本身带 close/pre_close,
    所以涨跌可以直接算, 不需要再去别的地方对齐。
    """
    market, code = _split(symbol)
    cache_key = f"tdxboards:{market}:{code}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: SymbolBelongBoardCmd(_market_code(market), code)) or []
    boards = []
    for item in raw:
        close = _round(getattr(item, "close", None), 4)
        prev = _round(getattr(item, "pre_close", None), 4)
        boards.append({
            "board_code": getattr(item, "board_code", None),
            "board_name": getattr(item, "board_name", None),
            "close": close,
            "pre_close": prev,
            "pct": None if (close is None or not prev) else round((close - prev) / prev * 100, 4),
        })
    boards.sort(key=lambda b: b["pct"] if b["pct"] is not None else -1e9, reverse=True)
    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "boards": boards,
        "derived": ["pct"],
        "cached": False,
    }
    _cache_put(cache_key, payload, float(_settings().tdx_ex_cache_ttl_s))
    return payload


def board_members(board_code: str, limit: int = 50, start: int = 0) -> dict:
    """板块成分股报价 —— 上游 ``BoardMembersQuotes`` + PresetField.COMMON。

    单位(2026-10-12 实测核对, 不是猜):
    - ``total_shares`` / ``float_shares``: **万股**(PANW 81800 → 8.18 亿股)
    - ``total_market_cap_ab``: **绝对金额**(342602940416 → 3426 亿美元)
    - ``dividend_yield``: **百分数**(QCOM 3.59 / AAPL 1.05 / WDC 0.5)
    - ``pe_dynamic`` / ``eps``: 动态口径, 与 close/eps 不完全相等是正常的
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"bad_limit: {limit} (1..{MAX_BOARD_MEMBERS_PER_PAGE})")
    limit = min(limit, MAX_BOARD_MEMBERS_PER_PAGE)
    code_int = _to_board_code(board_code)

    cache_key = f"tdxmembers:{code_int}:{start}:{limit}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: BoardMembersQuotesCmd(
        board_code=code_int, start=start, page_size=limit,
        fields=PresetField.COMMON)) or []

    members = []
    for item in raw:
        f = dict(getattr(item, "fields", {}) or {})
        close = _round(f.get("close"), 4)
        prev = _round(f.get("pre_close"), 4)
        # 万股 → 股
        shares = None if f.get("total_shares") is None else round(float(f["total_shares"]) * 1e4, 0)
        members.append({
            "code": getattr(item, "code", None),
            "name": getattr(item, "name", None),
            "close": close,
            "pre_close": prev,
            "pct": None if (close is None or not prev) else round((close - prev) / prev * 100, 4),
            "amount": _round(f.get("amount"), 2),
            "volume": _round(f.get("vol"), 2),
            "vol_ratio": _round(f.get("vol_ratio"), 4),
            "market_cap": _round(f.get("total_market_cap_ab"), 2),
            "total_shares": shares,
            "eps": _round(f.get("eps"), 4),
            "pe_dynamic": _round(f.get("pe_dynamic"), 4),
            "pe_ttm": _round(f.get("pe_ttm"), 4),
            "dividend_yield": _round(f.get("dividend_yield"), 4),
            "net_assets": _round(f.get("net_assets"), 4),
        })
    payload = {
        "source": SOURCE,
        "board_code": str(board_code).strip().upper(),
        "members": members,
        "requested": limit,
        "start": start,
        "derived": ["pct", "total_shares"],
        "note": "总股本由上游万股换算成股; 市值/股息率为绝对值与百分数口径(已交叉核对)",
        "cached": False,
    }
    _cache_put(cache_key, payload, float(_settings().tdx_ex_cache_ttl_s))
    return payload


def board_list(kind: str = "US_HY", limit: int = 50) -> dict:
    """板块列表 + 各板块领涨股 —— 上游 ``BoardList``。

    ``kind`` ∈ BOARD_KINDS。返回每行是一个板块行(指数价 + 板块内领涨股), 用来做
    "概念/行业板块榜": 板块自身带 close/pre_close ⇒ 涨跌自算, 领涨股用于展示代表票。
    """
    key = str(kind or "").strip().upper()
    if key not in BOARD_KINDS:
        raise ValueError(f"bad_kind: {kind!r} ({'/'.join(BOARD_KINDS)})")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 150:
        raise ValueError(f"bad_limit: {limit} (1..150)")

    cache_key = f"tdxboardlist:{key}:{limit}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: BoardListCmd(BOARD_KINDS[key], 0, limit)) or []
    boards = []
    for item in raw:
        close = _round(getattr(item, "price", None), 4)
        prev = _round(getattr(item, "pre_close", None), 4)
        symbol_close = _round(getattr(item, "symbol_price", None), 4)
        symbol_prev = _round(getattr(item, "symbol_pre_close", None), 4)
        boards.append({
            "board_code": getattr(item, "code", None),
            "board_name": getattr(item, "name", None),
            "close": close,
            "pre_close": prev,
            "pct": None if (close is None or not prev) else round((close - prev) / prev * 100, 4),
            "rise_speed": _round(getattr(item, "rise_speed", None), 4),
            "leader_code": getattr(item, "symbol_code", None),
            "leader_name": getattr(item, "symbol_name", None),
            "leader_pct": (None if (symbol_close is None or not symbol_prev)
                           else round((symbol_close - symbol_prev) / symbol_prev * 100, 4)),
        })
    boards.sort(key=lambda b: b["pct"] if b["pct"] is not None else -1e9, reverse=True)
    payload = {
        "source": SOURCE,
        "kind": key,
        "boards": boards,
        "derived": ["pct", "leader_pct"],
        "cached": False,
    }
    _cache_put(cache_key, payload, float(_settings().tdx_ex_cache_ttl_s))
    return payload


def multi_day_ticks(symbol: str, days: int = 5) -> dict:
    """多日分时 —— 上游 ``TickCharts``(**仅港股: 日期与内容严格对应**)。

    ⚠️ 为什么美股不接(不是懒, 是接不了):
    同一条命令在美股拿到的东西语义不同 ——
      - 分块是「390 根一页」而不是「一天一块」(count=5/page_size=390 但 total 少一截);
      - 每块的 minutes 继续累加到 1679+ , mod 24 后钟点不可读;
      - **dates[] 与内容不对应**: 港股 day_i 的收盘正是 pre_closes[i-1](逐日校验过),
        美股这条链对不上(day0 pre=336.640 而 day1 收 340.63)。
    要画美股多日分时就得由我们给每块**指定日期** —— 那是编数据, 不做。
    所以这里按天严格校验一致性: 链一对不上就 fail-closed。
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 5:
        raise ValueError(f"bad_days: {days} (1..5)")

    market, code = _split(symbol)
    if market == "US":
        raise TdxExUnavailableError(
            "multiday: 上游多日分时的数据块与日期不一一对应(详见 provider 注释), "
            "不给数据块编日期, 因此不提供美股多日分时")

    cache_key = f"tdxmultiday:{market}:{code}:{days}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw = _exec(lambda: TickChartsCmd(_market_code(market), code, None, days))
    if raw is None:
        raise TdxExUnavailableError(f"multiday: {symbol} 上游返回空")

    out_days: list[dict] = []
    for item in list(getattr(raw, "charts", []) or []):
        day_pre = _round(getattr(item, "pre_close", None), 4)
        ticks = list(getattr(item, "ticks", []) or [])
        if not ticks:
            continue
        points = [{
            "t": tk.time.strftime("%H:%M") if hasattr(tk.time, "strftime") else str(tk.time)[:5],
            "price": _round(getattr(tk, "price", None), 4),
            "avg": _round(getattr(tk, "avg", None), 4),
            "volume": _round(getattr(tk, "vol", None), 2),
        } for tk in ticks]
        # 自洽守卫: **较晚那天的昨收 == 相邻较早那天的收盘**。
        # 港股实测逐日吻合(10-12 昨收 424.800 = 10-09 收盘 424.80); 美股这条链
        # 就是断的。上游一旦换口径 / 我们误读了块, 这里第一时间炸而不是画错图。
        prev_close_chain = day_pre
        out_days.append({
            "date": getattr(item, "date", None).isoformat() if hasattr(getattr(item, "date", None), "isoformat") else None,
            "prev_close": day_pre,
            "points": points,
        })

    if not out_days:
        raise TdxExUnavailableError(f"multiday: {symbol} 没有可取的分时日")

    for i in range(len(out_days) - 1):
        later, earlier = out_days[i], out_days[i + 1]
        prev_close, earlier_close = later["prev_close"], earlier["points"][-1]["price"]
        if prev_close is None or earlier_close is None:
            continue
        tolerance = max(0.01, abs(prev_close) * 0.005)   # float32 毛刺容差
        if abs(earlier_close - prev_close) > tolerance:
            raise TdxExUnavailableError(
                f"multiday: 日期与分时内容不自洽 —— {later['date']} 的昨收 {prev_close} "
                f"与 {earlier['date']} 的收盘 {earlier_close} 对不上, 拒绝下发"
                f"(宁可不给, 不给错的)")

    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "name": getattr(raw, "name", None) or None,
        "timezone": "Asia/Shanghai",
        "days": out_days,
        "note": "多日分时由上游直接给出(每日昨收自带), 时间的钟点为北京时间",
        "cached": False,
    }
    _cache_put(cache_key, payload, float(_settings().tdx_ex_cache_ttl_s))
    return payload


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
