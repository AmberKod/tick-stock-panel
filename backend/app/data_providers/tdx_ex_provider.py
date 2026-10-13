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
from datetime import date as _date_cls
from datetime import datetime

from app._vendor.easy_tdx.codec.bitmap import PresetField
from app._vendor.easy_tdx.ex.commands.get_history_bars_range import (
    GetExHistoryInstrumentBarsRangeCmd,
)
from app._vendor.easy_tdx.ex.commands.get_instrument_quote import GetExInstrumentQuoteCmd
from app._vendor.easy_tdx.ex.commands.get_minute_time import GetExHistoryMinuteTimeDataCmd
from app._vendor.easy_tdx.ex.commands.get_transaction import (
    GetExHistoryTransactionDataCmd,
    GetExTransactionDataCmd,
)
from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd
from app._vendor.easy_tdx.ex.mac_client import MacExClient
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection
from app._vendor.easy_tdx.mac.commands import (
    BoardListCmd,
    BoardMembersQuotesCmd,
    SymbolBelongBoardCmd,
    SymbolInfoCmd,
    SymbolTransactionCmd,
)
from app._vendor.easy_tdx.mac.enums import Adjust, ExBoardType, ExMarket, Period

logger = logging.getLogger(__name__)

#: 登记在 app/services/source_gate.py 白名单里的源名。
SOURCE = "tdx-ex"

#: 上游单次请求上限(实测 800/3200 均只回 700)。超过就截断并标注, 不多打一次请求。
#: ⚠️ 这个上限只约束 ``goods_kline``(蜡烛 K 线)。**分时/分钟价格线走 0x240C 不受它限制**
#: (单日一包, 按日循环可拿任意长度, 详见 ``minute_range``)。
MAX_BARS_PER_REQUEST = 700

#: 逐笔: 上游单页上限(实测页大小 1800)。我们一次只取一页 —— 够了, 再深要翻页且
#: 上游没有 total 字段, 翻页终止条件不严谨(实测见模块头注释), 不冒险。
MAX_TRANSACTIONS_PER_REQUEST = 1800

#: 港股逐笔的价格是**整数**: 1 港元 = 1000(实测 00700 返回 425000 → 425.0 HKD,
#: 与港股分时图 float 价格 431.4 同一口径)。美股/标准市场走 0x122F, 已经是浮点数。
_HK_TRANSACTION_PRICE_DIVISOR = 1000.0

#: 分钟价格线按日循环: 多少个自然日作为候选(跳过周末, 取不到=非交易日)。
#: days 个"有数据的交易日" ≈ 需要扫 1.5~2 倍自然日; 上限防止前端传大数把节点打死。
MAX_MINUTE_RANGE_DAYS = 20
_MINUTE_RANGE_SCAN_MULTIPLIER = 2
_MINUTE_RANGE_SCAN_PADDING = 4

#: 多日分时: 图上超过 10 条线就分不清了, 且每天一次上游请求, 上限也是保护节点。
MAX_MULTIDAY_DAYS = 10
#: 日K 日历多取一些, 保证最早那天还有"前一交易日"可以拿昨收。
_MULTIDAY_CALENDAR_PADDING = 10
#: 0x240C 当日末价 vs 日K close 的相对容差(float32 毛刺 / 上游口径微差)。
_MULTIDAY_CLOSE_TOLERANCE = 0.005

#: m1 → N 分钟聚合时 open/high/low 的实测偏差(2026-10-13, HK 00700 / US AAPL 各 66~78 根,
#: **走 0x240D 的真实 m1 OHLC** 与上游原生 MIN_5 逐根对比): 开/高/低/收**全部 0.000%**。
#: 数学上必然如此 —— 上游原生 N 分钟 K 就是交易所用同一套规则从 m1 聚合的。
_AGGREGATE_DEVIATION_NOTE = (
    "由上游 1 分钟真实开高低收聚合(非单点价格的极值推导): 与交易所原生 N 分钟 K "
    "逐根对比, 开/高/低/收偏差全部 0.000%(实测 2026-10-13 HK 00700 66 根 / US AAPL 78 根)"
)

#: 0x240D(GetExHistoryInstrumentBarsRangeCmd) 的**单次根数上限**: 实测请求跨 21 个月的
#: 区间也只回 1500 根 —— 它是**从 ``start_date`` 起正向累计**到 1500 就掐断。所以想要
#: "最近 N 天"必须自己倒推起点分片, 不能直接丢一个大区间过去(会拿到那段里最老的几天)。
MAX_RANGE_BARS_PER_REQUEST = 1500

#: 0x240D 每日分钟根数的**保守上界**(实测 HK 330 / US 390)。用来算"一次请求最多覆盖
#: 几个交易日" —— 取保守值是为了留余量: 真下满了 1500 会被正向上限掐断, 丢掉最近那天。
_MINUTE_BARS_PER_DAY = {"hk": 340, "us": 400}
#: 0x240D 的 hour 字段已由上游 mod 24(美股凌晨段直接是 0~4, 不像 0x240C 那样给 24~27)。
#: 判"换了一天"要用会话内单调钟: 早于开盘小时的那些凌晨时刻 +24。
_SESSION_START_HOUR = {"hk": 9, "us": 21}

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

#: 周期串 → 分钟数(0x240C 的 m1 序列聚合到更长周期时用)。
_PERIOD_MINUTES: dict[str, int] = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60}

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


# ───────────────────────────────────────────── 逐笔成交

def _ymd(value) -> int:
    """``"2026-10-09"`` / ``"20261009"`` / ``20261009`` / ``date`` → ``20261009``。

    只接受这几种形状, 其余一律 ValueError —— 上游协议要的是 int 日期, 任何"猜格式"
    的宽容解析都可能把 9 月读成 10 月。
    """
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return int(value.year) * 10000 + int(value.month) * 100 + int(value.day)
    s = str(value or "").strip().replace("-", "").replace("/", "")
    if not (len(s) == 8 and s.isdigit()):
        raise ValueError(f"bad_date: {value!r} (YYYY-MM-DD / YYYYMMDD)")
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    if not (1990 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError(f"bad_date: {value!r}")
    return y * 10000 + m * 100 + d


def transactions(symbol: str, date=None, limit: int = 200) -> dict:
    """逐笔成交 —— 港股与美股**不是同一条协议**, 别合并。

    为什么分叉(2026-10-12 实测, 推翻此前"EX 不支持逐笔"的结论):
      - 港股的逐笔在 MAC 协议 ``SymbolTransactionCmd``(0x122F) 下恒返回 **39 字节空响应**
        (count=0) —— 0x122F 的数据源只覆盖沪深京 A 股 + 部分扩展市场, 港股没接入。
      - 港股正确走 ex 扩展行情层: 当日 **0x23FC**, 历史 **0x2406**。
      - 美股 0x122F 本来就能用(实测 AAPL 21:12 / 337.79 / vol 35)。

    价格单位: 港股是整数, ``price_int / 1000 = 港元``; 美股 0x122F 已是浮点。

    ``date`` 为空 = 当日。``limit`` 1..1800(单页上限, 不翻页)。
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"bad_limit: {limit!r}")
    if limit < 1 or limit > MAX_TRANSACTIONS_PER_REQUEST:
        raise ValueError(f"bad_limit: {limit} (1..{MAX_TRANSACTIONS_PER_REQUEST})")

    market, code = _split(symbol)
    ymd = _ymd(date) if date is not None and str(date).strip() != "" else None

    if market == "HK":
        mkt = _market_code(market)
        if ymd is None:
            raw = _exec(lambda: GetExTransactionDataCmd(mkt, code, 0, limit))
        else:
            raw = _exec(lambda: GetExHistoryTransactionDataCmd(mkt, code, ymd, 0, limit))
        rows = _rows_from_ex_transactions(raw)
        price_unit = "HKD(上游整数/1000)"
        # nature 的买卖语义我们没有实测样本支撑(实测全是 0), 原样下发不映射 ——
        # 宁可前端不标颜色, 也不猜一个"0=买" 的口径。
        side_note = "nature 为上游原始字段(买卖映射未实测, 前端勿当方向展示)"
    else:
        qd = None
        if ymd is not None:
            qd = _date_cls(ymd // 10000, (ymd // 100) % 100, ymd % 100)
        raw = _exec(lambda: SymbolTransactionCmd(_market_code(market), code,
                                                 query_date=qd, start=0, count=limit))
        rows = _rows_from_mac_transactions(raw)
        price_unit = "USD(上游浮点)"
        side_note = "bs_flag: 0=买入 / 1=卖出 / 2=中性 / 5=盘后(上游协议注释口径)"

    if not rows:
        raise TdxExUnavailableError(f"transactions: {symbol} 上游返回空(非交易日/代码不匹配)")

    rows.sort(key=lambda r: r["time"])
    return {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "date": str(ymd) if ymd else "today",
        "protocol": "0x23FC/0x2406" if market == "HK" else "0x122F",
        "count": len(rows),
        "rows": rows,
        "price_unit": price_unit,
        "volume_unit": "share",
        "timezone": "Asia/Shanghai",
        "note": (f"逐笔成交(MAC-EX {('0x23FC' + ('/0x2406' if ymd else '')) if market == 'HK' else '0x122F'}); "
                 f"上游按时间倒序返回, 已按时间升序排好; 单次一页不翻页; {side_note}"),
        "cached": False,
    }


def _rows_from_ex_transactions(raw) -> list[dict]:
    """ex 逐笔 → 下发 rows(价格整数 → 港元)。"""
    rows: list[dict] = []
    for rec in list(raw or []):
        price = _round(getattr(rec, "price", None), 4)
        if price is not None:
            price = round(price / _HK_TRANSACTION_PRICE_DIVISOR, 4)
        hour = int(getattr(rec, "hour", 0) or 0) % 24
        minute = int(getattr(rec, "minute", 0) or 0)
        second = int(getattr(rec, "second", 0) or 0)
        rows.append({
            "time": f"{hour:02d}:{minute:02d}:{second:02d}",
            "price": price,
            "volume": int(getattr(rec, "volume", 0) or 0),
            "trade_count": None,   # ex 协议没有这个字段, 不给 0 冒充
            "nature": int(getattr(rec, "nature", 0) or 0),
        })
    return rows


def _rows_from_mac_transactions(raw) -> list[dict]:
    """MAC 逐笔(0x122F) → 下发 rows。价格已是浮点, 字段带 trade_count。"""
    rows: list[dict] = []
    for rec in list(raw or []):
        t = getattr(rec, "time", None)
        ts = t.strftime("%H:%M:%S") if hasattr(t, "strftime") else str(t or "")[:8]
        rows.append({
            "time": ts,
            "price": _round(getattr(rec, "price", None), 4),
            "volume": int(getattr(rec, "vol", 0) or 0),
            "trade_count": int(getattr(rec, "trade_count", 0) or 0),
            "bs_flag": int(getattr(rec, "bs_flag", 0) or 0),
        })
    return rows


# ───────────────────────────────────────────── 多日分钟 K / 分钟价格线


def minute_range(symbol: str, days: int = 5, period: str = "m1") -> dict:
    """多日分钟蜡烛 —— 走 ``GetExHistoryInstrumentBarsRangeCmd``(0x240D), **真实 OHLC**。

    ⚠️ 为什么从 0x240C 换成 0x240D(2026-10-13 实测, 探针归档在
    ``deliverables/tdx-ex-scan/probe_240d_range.py`` / ``probe_240d_detail.py``):
      - 0x240C 每根只给 ``price``(该分钟的一个价格), **没有开/高/低**: m1 只能标
        "四价同值", 更长周期只能拿单点价格的极值去"推"OHLC —— 实测相对交易所原生
        MIN_5 有平均 <0.05% / 最大 <0.45% 的偏差。
      - **0x240D 每根都带真实开/高/低/收**(实测 HK 00700 330/330 根、US AAPL 390/390
        根全部有真实幅度), 且单次 1500 根 = HK 约 4 个交易日 / US 约 3 个, 比按日
        循环少发好几倍请求。
      - 🎁 时间戳口径顺带统一了: 0x240D 是 ``09:31~16:00``(HK) / ``21:31~04:00``(US),
        与 ``goods_kline`` MIN_1(``/minute`` 端点)**逐根一致**; 而 0x240C 早一格
        (``09:30~15:59``)。也就是说这一改, 1 日 / 多日两套分钟图的 x 轴对齐了。
      - 🎁 从真实 m1 OHLC 聚合出的 N 分钟蜡烛, 与交易所原生 MIN_5 **逐根 0.000%
        一致**(实测 HK 66 根 / US 78 根) —— 聚合不再是折衷, 标注详见 ``derived``。

    ⚠️ 它**没有均价**: 0x240C 每根自带 ``avg_price``(当日累计均价), 0x240D 不给。
    所以多日分时 / 当日分时那条要均价线的路**继续走 0x240C**(``_minute_days_240c``),
    本函数专供蜡烛图, ``has_avg`` 如实标 False。

    ⚠️ 1500 根上限是**从 ``start_date`` 起正向累计**的: 直接丢一个大区间过去会拿到
    "那段里最老的几天"。所以先用日K 拿到真交易日历, 再按片倒着请求。
    """
    key = str(period or "").strip().lower()
    if key not in _PERIODS:
        raise ValueError(f"bad_period: {period!r} (m1/m5/m15/m30/m60)")
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > MAX_MINUTE_RANGE_DAYS:
        raise ValueError(f"bad_days: {days} (1..{MAX_MINUTE_RANGE_DAYS})")

    market, code = _split(symbol)
    step = _PERIOD_MINUTES[key]

    cache_key = f"tdxminrange:{market}:{code}:{key}:{days}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    raw_days = _minute_days_240d(market, code, days)
    out_days = [{"date": d["date"], "bars": _aggregate_m1(d["rows"], step)}
                for d in raw_days]

    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "period": key,
        "protocol": "0x240D",
        "requested_days": days,
        "days": out_days,
        "bar_count": sum(len(d["bars"]) for d in out_days),
        # 0x240D 每根都给真开高低收, 不再需要"单点价格"的折衷
        "ohlc": "native" if step == 1 else "aggregated_from_m1",
        "derived": [] if step == 1 else ["open", "high", "low", "close"],
        "has_avg": False,
        "volume_unit": "share",
        "timezone": "Asia/Shanghai",
        "note": ("上游 0x240D 区间K线: 单次上限 1500 根、从起点日正向累计, "
                 "故先由日K 取真实交易日历再倒序分片请求。时间戳为北京时间"
                 "(美股会话跨零点: 21:31~04:00), 与 1 日分钟图口径一致。"
                 + ("" if step == 1
                    else " N 分钟蜡烛: " + _AGGREGATE_DEVIATION_NOTE)),
        "cached": False,
    }
    _cache_put(cache_key, payload, float(_settings().tdx_ex_cache_ttl_s))
    return payload


def _minute_days_240d(market: str, code: str, days: int) -> list[dict]:
    """最近 ``days`` 个交易日的**真实分钟蜡烛** —— 0x240D 分片取 + 按会话切天。

    日期列表**不用猜**: 直接取日K 的交易日历(港美股假期各不相同, 跳过周末猜必错),
    取最后 ``days`` 个; 再按 ``MAX_RANGE_BARS_PER_REQUEST // 每日根数上界`` 分片,
    一片一次请求。
    """
    calendar = _daily_closes(market, code, days + _MULTIDAY_CALENDAR_PADDING)
    if not calendar:
        raise TdxExUnavailableError(f"minute_range: {market}:{code} 日K 取不到, 无法定位交易日")
    targets = [ymd for ymd, _ in calendar][-days:]

    # 🔴 market 是 ``HK``/``US``(大写), 而下面两张表用小写 key —— 不 lower 就会静默
    # 走 fallback: 港股按美股的每日根数上界分片(多发一次请求), 会话起始小时更是
    # 会错成 21(港股会话 09:31~16:00 全被当成"凌晨段", 虽然巧合下单调性仍成立)。
    mkey = market.lower()
    per_day = _MINUTE_BARS_PER_DAY.get(mkey, 400)
    chunk = max(1, MAX_RANGE_BARS_PER_REQUEST // per_day)
    start_hour = _SESSION_START_HOUR.get(mkey, 21)

    merged: dict[str, list[dict]] = {}
    for i in range(0, len(targets), chunk):
        group = targets[i:i + chunk]
        bars = _exec(lambda s=int(group[0]), e=int(group[-1]):
                     GetExHistoryInstrumentBarsRangeCmd(_market_code(market), code, s, e))
        for day in _split_range_days(bars, start_hour):
            # 非交易日/未来日期: 0x240D 回空包(不像 0x240C 那样回全 0 的填充包),
            # 但这里仍然按价格过滤一层 —— 上游格式变了也不至于把 0 元线画出来。
            if not any((r.get("close") or 0) > 0 for r in day["rows"]):
                continue
            key = day["date"].replace("-", "")
            if key not in targets:
                continue        # 防御: 上游越出请求区间的日期一律不要
            merged[key] = day["rows"]

    # 按日K 日历顺序下发(上游可能整块缺失某天 —— 缺就缺, 不做填空)
    out = [{"date": _iso_day(k), "rows": merged[k]} for k in targets if k in merged]
    if not out:
        raise TdxExUnavailableError(
            f"minute_range: {market}:{code} 最近 {days} 个交易日均无分钟数据")
    return out


def _iso_day(ymd: str) -> str:
    """``yyyymmdd`` → ``yyyy-mm-dd``。"""
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


def _split_range_days(bars, start_hour: int) -> list[dict]:
    """0x240D 返回的连续 bar 流 → 按交易日切开。

    🔴 为什么**不能按 datetime 字符串排序**: 0x240D 的 ``hour`` 已经被上游 mod 24
    (美股凌晨段直接是 ``00:00~04:00``, 不像 0x240C 那样给 24~27)。一排序, 凌晨段就
    被甩到开盘段**前面**, 整个会话的顺序错乱(21:31 那根会排在 00:00 之后)。
    这里改用**会话内单调钟**切天: 早于开盘小时的凌晨时刻 +24, 于是 HK 是
    9→16、US 是 21→28 一路递增, 出现回落即换日。

    日期标签直接取每根自带的 ``year/month/day`` —— 真源, 与日K 的美股会话标签一致。
    """
    groups: list[list] = []
    cur: list = []
    clock = -1.0
    for bar in list(bars or []):
        hour = int(getattr(bar, "hour", 0) or 0)
        minute = int(getattr(bar, "minute", 0) or 0)
        mono = (hour + 24 if hour < start_hour else hour) + minute / 60.0
        if cur and mono <= clock:
            groups.append(cur)
            cur = []
        cur.append(bar)
        clock = mono
    if cur:
        groups.append(cur)

    out: list[dict] = []
    for group in groups:
        head = group[0]
        date_s = f"{int(head.year):04d}-{int(head.month):02d}-{int(head.day):02d}"
        rows = [{
            "datetime": f"{date_s} {int(getattr(b, 'hour', 0) or 0):02d}:"
                        f"{int(getattr(b, 'minute', 0) or 0):02d}:00",
            "open": _round(getattr(b, "open", None), 4),
            "high": _round(getattr(b, "high", None), 4),
            "low": _round(getattr(b, "low", None), 4),
            "close": _round(getattr(b, "close", None), 4),
            "price": _round(getattr(b, "close", None), 4),
            "volume": int(getattr(b, "trade", 0) or 0),
            # 0x240D 不给均价。留 None, 不拿成交量去加权"算"一个 —— 那是推导值,
            # 不该和上游直给的 avg_price 混成一个名字。
            "avg": None,
        } for b in group]
        out.append({"date": date_s, "rows": rows})
    return out


def _aggregate_m1(rows: list[dict], step: int) -> list[dict]:
    """真实 m1 OHLC → N 分钟蜡烛(规则与交易所一致: 开取首根的open、高低取极值、收取末根)。

    时间戳取组内**最后一根**的时刻 —— 实测与上游原生 MIN_5 完全对齐(HK 末根 16:00、
    US 末根 04:00)。不做 +step: 美股收盘那组 +5 分钟会越过 04:00 造出不存在的时间点。
    """
    if step <= 1:
        return rows
    out: list[dict] = []
    for i in range(0, len(rows) - step + 1, step):
        grp = rows[i:i + step]
        opens = [g["open"] for g in grp if g["open"] is not None]
        highs = [g["high"] for g in grp if g["high"] is not None]
        lows = [g["low"] for g in grp if g["low"] is not None]
        closes = [g["close"] for g in grp if g["close"] is not None]
        if not (opens and highs and lows and closes):
            continue
        out.append({
            "datetime": grp[-1]["datetime"],
            "open": opens[0],
            "high": max(highs),
            "low": min(lows),
            "close": closes[-1],
            "price": closes[-1],
            "avg": None,
            "volume": sum(int(g["volume"] or 0) for g in grp),
        })
    return out


def _minute_days_240c(symbol: str, days: int) -> list[dict]:
    """0x240C 按日循环的分钟**价格线**(带均价) —— 多日分时 / 当日分时专用。

    与 ``_minute_days_240d`` 的分工: 这条几分钟序列**每根自带当日累计均价**
    (``avg_price``), 但没有 OHLC; 蜡烛图走 0x240D 那份。多日分时的均线、当日分时
    的黄线都必须是这条。

    ⚠️ 0x240C 对"还没发生的日期"回**一整包 price=0 的填充数据**(实测北京 10-13
    00:19 请求 10-13 拿到 HK 330 根 0.0) —— 只判"非空"会把次日当成交易日, 所以
    必须再要求包里至少一根 ``price > 0``。
    """
    market, code = _split(symbol)
    # 由新到旧扫: 要的是**最近** N 个交易日。反过来自旧向新扫会拿到"最近一段里
    # 最老的几天"(实测 days=3 拿到 10-02 而漏掉 10-12), 图看着像数据没更新。
    collected: list[dict] = []
    for ymd in _recent_trading_candidates(days):
        if len(collected) >= days:
            break
        bars = _exec(lambda y=ymd: GetExHistoryMinuteTimeDataCmd(_market_code(market), code, y))
        if not bars:
            continue    # 非交易日: 上游给空包, 不是故障
        if not any((_round(getattr(b, "price", None), 4) or 0) > 0 for b in bars):
            continue
        collected.append({
            "date": f"{ymd // 10000:04d}-{(ymd // 100) % 100:02d}-{ymd % 100:02d}",
            "bars": _rows_from_minute_bars(bars, ymd, 1),
        })
    return list(reversed(collected))    # 下发仍按时间升序


def _rows_from_minute_bars(bars, ymd: int, step: int) -> list[dict]:
    """0x240C 一日的分钟包 → rows。

    时间 = ``会话日期 + (hour mod 24):minute`` —— 美股 hour 可到 27(21:30 开盘 +
    6.5h 会话), mod 24 后落在凌晨段, 与 ``minute_bars`` 那一份口径完全一致。
    """
    rows: list[dict] = []
    for bar in list(bars or []):
        hour = int(getattr(bar, "hour", 0) or 0) % 24
        minute = int(getattr(bar, "minute", 0) or 0)
        rows.append({
            "datetime": f"{ymd // 10000:04d}-{(ymd // 100) % 100:02d}-{ymd % 100:02d} "
                        f"{hour:02d}:{minute:02d}:00",
            "price": _round(getattr(bar, "price", None), 4),
            "avg": _round(getattr(bar, "avg_price", None), 4),
            "volume": int(getattr(bar, "volume", 0) or 0),
        })
    if step <= 1:
        # m1: 四价同值 = 该分钟价格(上游没给 OHLC, 不编)
        for r in rows:
            r["open"] = r["high"] = r["low"] = r["close"] = r["price"]
        return rows

    out: list[dict] = []
    for i in range(0, len(rows) - step + 1, step):
        grp = rows[i:i + step]
        px = [g["price"] for g in grp if g["price"] is not None]
        if not px:
            continue
        out.append({
            # 时间戳取组内**最后一根**的分钟: 上游原生 N 分钟 K 标的是区间结束时刻
            # (比这晚 1 分钟), 但我们不去做 +step —— 美股收盘那组 +5 分钟会越过
            # 04:00 造出一个不存在的时刻。宁可 tooltip 上差 1 分钟, 不编时间点。
            "datetime": grp[-1]["datetime"],
            "open": px[0],
            "high": max(px),
            "low": min(px),
            "close": px[-1],
            "price": px[-1],
            "avg": grp[-1]["avg"],           # 当日累计均价: 取组内最后一根
            "volume": sum(int(g["volume"] or 0) for g in grp),
        })
    return out


def _recent_trading_candidates(days: int) -> list[int]:
    """最近的日候选, **由新到旧**, 跳过周末。

    哪些天真的有数据由上游决定: 取到空包就是非交易日, 循环里跳过。这里只负责
    给出足够多的候选, 不负责"猜"交易日 —— 港美股假期各不相同, 猜必错。
    顺序很重要: 调用方要的是**最近** N 个交易日, 所以必须从今天倒着扫。
    """
    from datetime import date as _d
    from datetime import timedelta

    scan = min(days * _MINUTE_RANGE_SCAN_MULTIPLIER + _MINUTE_RANGE_SCAN_PADDING,
               MAX_MINUTE_RANGE_DAYS * _MINUTE_RANGE_SCAN_MULTIPLIER + _MINUTE_RANGE_SCAN_PADDING)
    today = _d.today()
    out: list[int] = []
    for back in range(scan + 1):
        day = today - timedelta(days=back)
        if day.weekday() < 5:       # 0=周一 .. 4=周五
            out.append(day.year * 10000 + day.month * 100 + day.day)
    return out


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


def _daily_closes(market: str, code: str, count: int) -> list[tuple[str, float]]:
    """日K 收盘序列(升序, ``yyyymmdd``) —— 多日分时的**昨收真源 + 日期基准**。

    0x240C 只给分钟价格, **不带昨收**。而"昨收"的定义就是"上一交易日的收盘价",
    所以从日K 拿才是真源(不是由我们自己推导出来凑一个数)。它同时充当日期
    自洽的基准: 0x240C 取到的每一天都必须落在这份交易日历里。
    """
    frame = _fetch(market, code, Period.DAILY, count)
    out: list[tuple[str, float]] = []
    for row in _rows_from_frame(frame):
        close = row.get("close")
        if close is None:
            continue
        ymd = str(row.get("datetime", ""))[:10].replace("-", "")
        if len(ymd) == 8 and ymd.isdigit():
            out.append((ymd, float(close)))
    out.sort()
    return out


def multi_day_ticks(symbol: str, days: int = 5) -> dict:
    """多日分时 —— 0x240C 按日循环 + **日K 真源昨收**(港股美股同一条路)。

    ⚠️ 为什么不再用上游 ``TickCharts``: 它给美股的**不是"一天一块"**(390 根一页、
    ``dates[]`` 与内容不对应, 详见 git 历史里的实测记录)。要用它画美股多日分时,
    就得由我们给每一块**指定日期** —— 那是编数据, 不做, 所以美股此前是
    fail-closed。

    现在的路子(两个市场统一):
      - 分钟序列走 0x240C **按日循环**: 日期是我们自己传的, 天然自洽;
      - 昨收走日K: ``prev_close = 该日在交易日历里前一个交易日的收盘``, 真源;
      - 三重守卫, 任一不满足就 fail-closed(宁可不画, 不画错):
        ① 0x240C 的每一天必须存在于日K 交易日历;
        ② 当日**最后一根 price == 日K 当日 close**(容差 0.5%);
        ③ 相邻两天在日历里必须是**相邻交易日**(中间不漏)。
        实测 AAPL / 00700 各 5 天: ① ② 全部 0.000% 吻合。
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > MAX_MULTIDAY_DAYS:
        raise ValueError(f"bad_days: {days} (1..{MAX_MULTIDAY_DAYS})")

    market, code = _split(symbol)
    cache_key = f"tdxmultiday:{market}:{code}:{days}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return {**cached, "cached": True}

    # 🔴 多日分时必须有均价线 → 走带 avg_price 的 0x240C 序列(注意不是 minute_range,
    # 那份已经改走 0x240D 拿真实蜡烛了, 而 0x240D **不给均价**)。
    day_bars = _minute_days_240c(symbol, days)
    calendar = _daily_closes(market, code, days * 3 + _MULTIDAY_CALENDAR_PADDING)
    if not calendar:
        raise TdxExUnavailableError(f"multiday: {symbol} 日K 取不到, 没有昨收真源可用")

    cal_dates = [d for d, _ in calendar]
    closes = dict(calendar)

    out_days: list[dict] = []
    seen_idx: list[int] = []
    for day in day_bars:
        ymd = day["date"].replace("-", "")
        # 守卫①: 上游分钟包的日期必须落在日K 交易日历里
        if ymd not in closes:
            raise TdxExUnavailableError(
                f"multiday: {day['date']} 不在日K 交易日历里 —— 分钟与日线口径不一致, 拒绝下发")
        idx = cal_dates.index(ymd)
        bars = day["bars"]
        if not bars:
            continue

        # 守卫②: 当日最后一根价格 = 当日收盘, 必须与日K 对得上
        last_px = bars[-1].get("price")
        ref_close = closes[ymd]
        if last_px is not None and ref_close:
            if abs(last_px - ref_close) / abs(ref_close) > _MULTIDAY_CLOSE_TOLERANCE:
                raise TdxExUnavailableError(
                    f"multiday: {day['date']} 分钟末价 {last_px} 与日K 收盘 {ref_close} "
                    f"对不上, 拒绝下发")

        # 守卫③: 与上一个已取日必须是相邻交易日(中间不漏天)
        if seen_idx and idx - seen_idx[-1] != 1:
            raise TdxExUnavailableError(
                f"multiday: {day['date']} 与前一个交易日在日历里不相邻"
                f"(中间漏了 {idx - seen_idx[-1] - 1} 个交易日), 拒绝下发")
        seen_idx.append(idx)

        out_days.append({
            "date": day["date"],
            "prev_close": _round(closes[cal_dates[idx - 1]], 4) if idx > 0 else None,
            "close": _round(ref_close, 4),
            "points": [{
                "t": b["datetime"][11:16],
                "price": b["price"],
                "avg": b["avg"],
                "volume": b["volume"],
            } for b in bars],
        })

    if not out_days:
        raise TdxExUnavailableError(f"multiday: {symbol} 没有可取的分时日")

    # 最早那天的昨收拿不到 = 相对模式画不出来。宁可整体不给, 不画半截。
    if out_days[0]["prev_close"] is None:
        raise TdxExUnavailableError(
            f"multiday: {out_days[0]['date']} 的前一交易日不在日K 范围内, "
            f"昨收无真源, 拒绝下发")

    payload = {
        "source": SOURCE,
        "market": market,
        "symbol": str(symbol).upper(),
        "protocol": "0x240C + daily_kline",
        "timezone": "Asia/Shanghai",
        "days": out_days,
        "prev_close_source": "daily_kline",
        "has_avg": True,
        "note": ("分钟走 0x240C 按日循环(日期由我们指定, 天然自洽); 昨收取日K 前一交易日"
                 "收盘(真源, 不是推导)。三重守卫: 日期必须在日K 日历内 / 当日末价必须等于"
                 "日K 收盘(容差 0.5%) / 相邻两日必须相邻不漏 —— 任一不满足即不下发。"
                 "钟点为北京时间(美股会话跨零点: 21:30~04:00)。"),
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
    return (f"分钟K 走通达信扩展市场(MAC-EX 7727): 蜡烛(0x122E)单次上限 {MAX_BARS_PER_REQUEST} 根, "
            f"时间为北京时间; 分钟价格线(0x240C)按日循环可拿任意长度(港股 330 根/日、"
            f"美股 390 根/日, 带上游均价)")
