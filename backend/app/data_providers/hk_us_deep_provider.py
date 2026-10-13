"""港美股「深度 K 线」增量能力: 周期K / 指数日K / 分钟K / 复权系数。

动机: A股个股页已有 day/week/month/year 四档 + 复权三档 + 分钟K, 港美股页只有 day。
本模块负责把"能做对的那一半"补齐, 并把做不了的**显式标不可用**(而不是静默给错)。

━━━ 2026-10-11 实测结论 (ifzq.gtimg.cn, 宿主 + 容器内均验) ━━━

| 能力     | 港股 (hk00700)                  | 美股 (usAAPL)                      |
|----------|--------------------------------|-----------------------------------|
| day      | kline/kline?param=...,day ✓    | 本地落盘日K (yfinance) ✓           |
| week     | **腾讯原生真聚合 ✓**            | 腾讯只回 1 根 ⇒ 由日K聚合(derived) |
| month    | **腾讯原生真聚合 ✓**            | 同上                              |
| quarter  | 腾讯静默只回 1 根 ⇒ 不采信, 改日K聚合 | 同左                              |
| year     | 腾讯静默只回 1 根 ⇒ 不采信, 改日K聚合 | 同左                              |
| 分钟K    | mkline → {"code":-1} ⇒ 无真源   | 同左                              |
| 复权      | fqkline 的 qfq/hfq 与未复权     | fqkline 只回 1 根 ⇒ 无真源        |
|          | **逐日完全相同** ⇒ 腾讯不做港股复权 |                                   |
| 指数日K   | hkHSI ✓                        | us.DJI / us.IXIC / us.INX ✓       |

⚠️ 三个"不报错但给错"的坑, 必须显式拦住, 否则会把错误数据画成图表:

1. **quarter / year**: 腾讯不返回错误, 只给最新 1 根日 K, 且**回归键存在**
   (``data[key]["quarter"]`` 有值) —— 按 取到 1 行 当成 历史不足 就完了,
   实际是把一根日K当一根季K画。这里直接判 CapabilityUnavailableError。
2. **美股所有周期**: 带显式日期区间也只回 1 根。week/month/day 三者返回完全同形,
   无法区分 ⇒ 一律不采信腾讯, 改由**本地真日K聚合**(derived, 同屏标注)。
3. **指数响应 key 带点**: 恒生 ``hkHSI``, 但道指/纳指/标普是 ``us.DJI`` **带点**。
   按 ``usDJI`` 取会静默拿到空 ⇒ 按「去点后比对」找键。

复权为何判定腾讯不可用: 取 hk00939(建行H) / hk00011(恒生银行) 这类**必除净**的高股息票,
``fqkline?...,day,,,250,qfq`` 与未复权 ``kline/kline?...,day,,,250`` 的 OHLC
**250 天逐日完全相同** —— 真有复权就不可能一样。故港股只能用不复权价。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

from app.data_providers import tdx_ex_provider

_TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/kline"

#: 腾讯对无 UA 的请求可能返回空串而不报错, 表现为 rows 恒空 —— 必须带浏览器风格 UA。
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TSP-Deep"

#: 腾讯原生支持周期的**上限**, 按实测逐市场收窄 —— 不是 猜出来的枚举。
_NATIVE_PERIODS: dict[str, tuple[str, ...]] = {
    "HK": ("day", "week", "month"),
    "US": (),  # 实测全部周期只回 1 根, 一个都不采信
}

PERIODS = ("day", "week", "month", "quarter", "year")

#: 指数代码 → (腾讯 param 符号, 标准名)。Response key 可能与 param 不同(见模块注释第 3 点)。
INDICES: dict[str, tuple[str, str]] = {
    "HSI": ("hkHSI", "恒生指数"),
    "HSCEI": ("hkHSCEI", "恒生中国企业指数"),
    "DJI": ("usDJI", "道琼斯工业指数"),
    "IXIC": ("usIXIC", "纳斯达克综合指数"),
    "INX": ("usINX", "标普500指数"),
}

SOURCE_TENCENT = "tencent"
SOURCE_YFINANCE = "yfinance"
#: 通达信扩展市场(MAC-EX 7727) —— 与 tdx_ex_provider.SOURCE 同值, 这里显式重声明
#: 是为了让 capabilities 的 source 字段不必 import 那个模块才能读常量。
SOURCE_TDX_EX = "tdx-ex"

#: m1 分钟线的成交量单位 → 股数换算系数。
#:
#: 2026-10-12 实证(amount/(volume*close) 跨标的验算): 港股恒 ≈ 100, 美股恒 ≈ 1 ——
#:   00700.HK 100.0 / 00939.HK 99.9 / 01211.HK 100.0 / 09988.HK 100.0 (含换手)
#:   AAPL.US 1.0 / NVDA.US 1.0 / MSFT.US 1.0
#: 即港股按"每手 100 股"计数(注意是**固定的 100**, 不是该股的交易单位 —— 00939
#: 实际一手 1000 股, 算出来仍是 100), 美股就是股。系数写错均价线会错两个数量级,
#: 所以取实测值而不是猜; 换算出股数后 amount/shares 才是可核对的均价。
_LOT_SHARES: dict[str, float] = {"HK": 100.0, "US": 1.0}

#: 分时最多回去多少根 m1 —— 与上游单次上限对齐, 再大也只是被截断。
_MAX_INTRADAY_BARS = tdx_ex_provider.MAX_BARS_PER_REQUEST

#: 一个完整常规会话的时间跨度(分钟)。
#: 港股 09:30~16:00(含 12:00~13:00 午休, 休市仍在跨度里)与美股开盘→收盘都是
#: 6.5 小时; 差别只是美股这段跨北京时间零点。判"会话走没走完"用同一条线。
_REGULAR_SPAN_MIN = 390
_COMPLETE_TOLERANCE_MIN = 5


class CapabilityUnavailableError(RuntimeError):
    """该能力**没有真数据源** —— 与取数失败区分开, API 层据此给不同的 UI 文案。"""


# ───────────────────────────────────────────── 基础设施

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


def _timeout() -> float:
    from app.config import settings

    return float(settings.tencent_deep_timeout_s)


def _ttl() -> float:
    from app.config import settings

    return float(settings.tencent_deep_cache_ttl_s)


def _cache_get(key: str) -> object | None:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    return hit[1] if hit and hit[0] > time.time() else None


def _cache_put(key: str, value: object, ttl: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    """测试隔离用: 模块级缓存会跨用例残留, 不清会让 mock 断言假通过。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _request_json(url: str, timeout: float | None = None) -> dict:
    """GET 一个腾讯 JSON 接口。异常一律上抛, 由 API 层转 available:false(不吞)。

    显式清代理: 目标直连可达, 继承 HTTP_PROXY 会绕远路。UA 必须是浏览器风格,
    否则腾讯返回空串而不报错 —— 表现为  “ rows 为空  。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with opener.open(req, timeout=timeout or _timeout()) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ───────────────────────────────────────────── 符号 / 行解析

def _split(symbol: str) -> tuple[str, str]:
    """'00700.HK' / '0700.HK' / '00700' -> ('HK', '00700'); 'AAPL.US' -> ('US','AAPL')。"""
    s = str(symbol or "").strip().upper()
    if s.endswith(".HK"):
        return "HK", s[:-3].strip()
    if s.endswith(".US"):
        return "US", s[:-3].strip()
    raise ValueError(f"symbol 需带 .HK / .US 后缀: {symbol!r}")


def _tx_symbol(symbol: str) -> str:
    """内部 symbol → 腾讯符号: 00700.HK → hk00700; AAPL.US → usAAPL。"""
    market, code = _split(symbol)
    if market == "HK":
        if not code.isdigit():
            raise ValueError(f"港股代码应为数字: {symbol!r}")
        return "hk" + code.zfill(5)
    return "us" + code


def _pick_node(data: dict, tx: str) -> dict:
    """按腾讯符号找响应节点 —— **去点后比对**, 兼容 us.DJI / r_hk00700 这类变体。"""
    want = tx.lower().replace(".", "")
    for key, value in (data or {}).items():
        if isinstance(value, dict) and str(key).lower().replace(".", "").removeprefix("r_") == want:
            return value
    return {}


def _f(value: object) -> float | None:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _parse_tx_row(row: list) -> dict:
    """腾讯 K 线行 = ``[日期, 开, 收, 高, 低, 量]``。

    ⚠️ 第 2 列是**开**第 3 列是**收**(不是常见的 OHLC 顺序), 与 A股
    jiuzhang_astock_provider.adjust_ratios 实测口径同源。写反了图会看着像对的。
    """
    return {
        "date": str(row[0]),
        "open": _f(row[1]),
        "close": _f(row[2]),
        "high": _f(row[3]),
        "low": _f(row[4]),
        "volume": _f(row[5]) if len(row) > 5 else None,
    }


def _tx_bars(symbol: str, period: str, limit: int) -> tuple[list[dict], str]:
    """向腾讯取 K 线。返回 (rows_asc, 实际生效的周期键名)。"""
    tx = _tx_symbol(symbol)
    market, _ = _split(symbol)
    if period not in _NATIVE_PERIODS.get(market, ()):
        raise CapabilityUnavailableError(
            f"{market} {period} 无原生真源: 实测腾讯对该组合只返回 1 根最新日K(不报错), "
            f"采信即为假数据"
        )
    url = _TENCENT_KLINE_URL + "?" + urllib.parse.urlencode({"param": f"{tx},{period},,,{limit}"})
    cached = _cache_get(url)
    if isinstance(cached, list):
        return cached, period
    node = _pick_node((_request_json(url) or {}).get("data") or {}, tx)
    rows: list[dict] = []
    for raw in (node.get(period) or []):
        if isinstance(raw, list) and len(raw) >= 6:
            rows.append(_parse_tx_row(raw))
    rows.sort(key=lambda r: r["date"])
    _cache_put(url, rows, _ttl())
    return rows, period


# ───────────────────────────────────────────── 周期 K

def period_bars(symbol: str, period: str = "week", limit: int = 120) -> dict:
    """港美股周期 K。港股 week/month 走腾讯原生; 其余周期一律由**本地真日K聚合**。

    返回 ``{source, rows, derived_from}`` —— ``derived_from="daily"`` 表示该串是
    由日 K 确定性聚合而来(**不是原始数据**), 调用方必须同屏标注口径。
    """
    period = (period or "week").strip().lower()
    if period not in PERIODS:
        raise ValueError(f"period 必须是 {PERIODS} 之一: {period!r}")
    limit = max(10, min(int(limit or 120), 1000))
    market, _ = _split(symbol)

    if period in _NATIVE_PERIODS.get(market, ()):
        rows, key = _tx_bars(symbol, period, limit)
        if not rows:
            raise ValueError(f"{symbol} {period} 腾讯返回空: 符号无效或已被 WAF 拦截")
        return {"source": SOURCE_TENCENT, "rows": rows[-limit:], "derived_from": key}

    # 到这里: 腾讯没有真聚合 —— 用本地日 K 自己聚合 (确定性推导, 标口径)
    daily, source = _daily_rows(symbol, period, limit)
    rows = aggregate(daily, period)
    if not rows:
        raise ValueError(f"{symbol} 本地无可用日K, 无法聚合 {period}")
    return {"source": source, "rows": rows[-limit:], "derived_from": "daily"}


def _daily_rows(symbol: str, period: str, limit: int) -> tuple[list[dict], str]:
    """取足够长的真日 K 用于聚合, 同时回报它的来源标记。

    优先本地落盘 (港股/美股都有 daily 分区); 落盘为空再走 yfinance 实时取一次 —— 仍是真源, 不落盘
    (落盘是 data sync 的职责, provider 不做写操作)。
    """
    from datetime import timedelta

    market, _ = _split(symbol)
    span = {"week": 7, "month": 31, "quarter": 92, "year": 366, "day": 1}[period]
    days = max(limit * span + 40, 400)

    from app.config import settings
    from app.services.hk_data_adapter import read_hk_daily

    df = read_hk_daily(symbol, settings.data_dir)
    if getattr(df, "height", 0):
        source = _local_daily_source(df, market)
        cols = [c for c in ("date", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
        rows = sorted((_stdlib_row(r) for r in df.select(cols).to_dicts()), key=lambda r: r["date"])
        return rows, source

    # 本地没有 → 实时取一次
    if market == "US":
        from datetime import datetime

        from app.data_providers.yfinance_provider import YFinanceProvider

        end = datetime.now().date()
        df = YFinanceProvider().get_daily(
            [symbol], start_time=datetime.combine(end - timedelta(days=days), datetime.min.time()),
            end_time=datetime.combine(end, datetime.min.time()), asset_type="stock",
        )
        if getattr(df, "height", 0):
            return sorted((_stdlib_row(r) for r in df.to_dicts()), key=lambda r: r["date"]), SOURCE_YFINANCE
    raise ValueError(f"{symbol} 本地与实时均无日K, 无法聚合 {period}")


def _local_daily_source(df, market: str) -> str:
    """本地日 K 的来源必须**从数据里读出来**, 不能写死 —— 写死就是在编造出处。

    - 港股分区带 ``source`` 列 (实测 sina_hk_daily), 也可能多源合并 (用 '+'
      组合, source_gate 要求每段都在白名单才算真);
    - 美股分区由 YFinanceProvider / akshare 同步链路写入, **落盘时丢了 source 列**。
      这两条链路都是白名单真源, 用 yfinance 兜底。
    - 港股分区若哪天丢了 source 列: 宁可失败也不猜 —— 聚合结果要拿去画图的,
      来源不明 = 不可证伪, 按 fail-closed 处理。
    """
    if "source" in df.columns:
        vals = [str(v) for v in df["source"].unique().to_list() if v]
        if vals:
            return vals[0] if len(vals) == 1 else "+".join(sorted(vals))
    if market == "US":
        return SOURCE_YFINANCE
    raise ValueError("港股日K分区缺 source 列 —— 来源不可考, 拒绝聚合(不猜来源)")


def _stdlib_row(row: dict) -> dict:
    return {
        "date": str(row.get("date"))[:10],
        "open": _f(row.get("open")),
        "high": _f(row.get("high")),
        "low": _f(row.get("low")),
        "close": _f(row.get("close")),
        "volume": _f(row.get("volume")),
        "amount": _f(row.get("amount")),
    }


def aggregate(daily: list[dict], period: str) -> list[dict]:
    """日 K → 周/月/季/年。date 取该区间**最后一根**交易日(与腾讯港股 week 口径一致)。

    OHLC: 首开/最高/最低/末收; 量与额: 求和。
    """
    if period == "day":
        return list(daily)

    def bucket(d: str) -> str:
        import datetime as _dt

        day = _dt.date.fromisoformat(d)
        if period == "week":
            y, w, _ = day.isocalendar()
            return f"{y}-W{w:02d}"
        if period == "month":
            return f"{day.year}-M{day.month:02d}"
        if period == "quarter":
            return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
        return f"{day.year}"

    groups: dict[str, list[dict]] = {}
    for row in daily:
        groups.setdefault(bucket(row["date"]), []).append(row)

    out = []
    for rows in sorted(groups.values(), key=lambda rs: rs[0]["date"]):
        tail = rows[-1]
        out.append({
            "date": tail["date"],
            "open": rows[0]["open"],
            "high": max((r["high"] for r in rows if r["high"] is not None), default=None),
            "low": min((r["low"] for r in rows if r["low"] is not None), default=None),
            "close": tail["close"],
            "volume": None if any(r.get("volume") is None for r in rows)
                      else round(sum(r["volume"] for r in rows), 4),
            "amount": None if any(r.get("amount") is None for r in rows)
                      else round(sum(r["amount"] for r in rows), 4),
        })
    return out


# ───────────────────────────────────────────── 指数日 K

def index_bars(code: str, limit: int = 120) -> dict:
    """指数日 K。code 大小写/是否带 ^ 都能吃 (HSI / ^HSI / hsi)。"""
    raw = str(code or "").strip().upper().lstrip("^")
    if raw not in INDICES:
        raise ValueError(f"指数代码必须是 {sorted(INDICES)} 之一: {code!r}")
    tx, name = INDICES[raw]
    limit = max(10, min(int(limit or 120), 1000))

    url = _TENCENT_KLINE_URL + "?" + urllib.parse.urlencode({"param": f"{tx},day,,,{limit}"})
    cached = _cache_get(url)
    if not isinstance(cached, list):
        node = _pick_node((_request_json(url) or {}).get("data") or {}, tx)
        cached = [_parse_tx_row(r) for r in (node.get("day") or [])
                  if isinstance(r, list) and len(r) >= 6]
        cached.sort(key=lambda r: r["date"])
        _cache_put(url, cached, _ttl())
    rows = cached[-limit:]
    if not rows:
        raise ValueError(f"指数 {raw} 腾讯返回空: 已被 WAF 拦截或代码有误")
    return {"source": SOURCE_TENCENT, "code": raw, "name": name, "rows": rows, "derived_from": "day"}


# ───────────────────────────────────────────── 明确不可用的两项

def minute_bars(symbol: str, period: str = "m30", limit: int = 320) -> dict:
    """分钟 K —— 走通达信扩展市场(MAC-EX, TCP 7727)。

    历史: 之前判定"港美股分钟K 无真源", 依据是腾讯 mkline 对 hk/us 一律
    ``{"code":-1,"msg":"param error"}``。2026-10-12 实测发现该结论**只对腾讯源
    成立** —— MAC-EX 协议(7727) 对美股/港股均返回真实分钟K, 且容器内裸 TCP
    直连可达。详见 ``tdx_ex_provider`` 模块头。

    仍然存在的上游硬限制: 单次最多 700 根 ⇒ 只有近日日内深度。这个限制如实
    下发给前端(payload.truncated / max_bars), **不用日K降采样凑数**。
    """
    payload = tdx_ex_provider.minute_bars(symbol, period=period, limit=limit)
    if not payload.get("rows"):
        # 取得到连接但没数据 —— 多半是代码/市场组合不对(如港股创业板走主板码)。
        # 抛错让端点 fail-closed, 不要返回 available:true + 空表冒充"今日无成交"。
        raise ValueError(f"通达信扩展市场无该标的分钟数据: {symbol} {period}")
    return payload


# ───────────────────────────────────────────── 当日分时

def _session_key(dt_str: str, market: str) -> str:
    """一根 m1 bar 属于哪个**交易日**(会话) —— 两个市场都直接取日期部分。

    ⚠️ 别做「美股减 12 小时」那种时区换算(第一版就是这么写的, 错的)。上游给的时间戳
    是**「同一个会话日期 + 北京时间」**: 夏令时会话标 ``D 21:30~D 04:00``(凌晨那段
    的日期**不翻页**, 仍与当晚段同属一个标签 ``D``), 冬令时同理(``D 22:30~D 05:00``)。

    2026-10-12 实证(AAPL / NVDA / MSFT / TSLA, 逐个与腾讯日K对表): 按日期标签分组后,
    同一组的「当晚段 21:31~23:59」+「凌晨段 00:00~04:00」加起来正好 390 根 = 6.5 小时
    一个完整常规会话, 且重排后的首根开盘、末根收盘与当天日K的开/收吻合到分位。
    改按 -12h 换算会把一个会话切成两天, 昨收跟着错整整一个交易日。
    """
    return str(dt_str)[:10]


#: 会话内**真实时间顺序**的排序键几何点 —— 见 ``_session_key``。
#:
#: 因为凌晨段与当晚段共用同一个日期标签, 按 datetime 字符串升序排出来的顺序是
#: "凌晨在前、当晚在后", 而真实成交顺序相反(当晚开盘 → 次日凌晨收盘)。20:00 之后的
#: bar 属该会话的前半段。港股 09:30~16:00 全体落在后半段, 排序不受影响(用了就退化
#: 成按时间升序)。
_US_SESSION_PHASE_SPLIT = 20


def _session_order_key(dt_str: str) -> tuple[int, str]:
    """会话内的真实先后: 当晚段(>=20:00)在前, 凌晨段在后, 段内按时间升序。"""
    stamp = str(dt_str)
    try:
        hour = int(stamp[11:13])
    except (TypeError, ValueError):
        hour = 0
    return (0 if hour >= _US_SESSION_PHASE_SPLIT else 1, stamp)


def intraday(symbol: str, limit: int = _MAX_INTRADAY_BARS) -> dict:
    """当日(最近一段)分时 —— **由真实 1 分钟 K 合成**, 不是另一份数据源。

    ⚠️ 为什么这里重新合成, 而不是直接转发腾讯 ``minute/query``:
       ``app/api/kline.py::get_minute`` 那条路对港股只给价格线, 成交量是**当日累计值**
       且 amount 恒为 0, 昨日收盘也不返回 —— 拿来画分时要么差分还原(多一层猜),
       要么就得再去别处借昨收。而 m1 那一份本身就带每根的量额, 自洽可核对。

    三件必须做对的事:

    1. **会话切分与排序**: 上游只给"最近 N 根", 而且时间戳是「会话日期 + 北京时间」
       (见 ``_session_key`` / ``_session_order_key``) —— 直接按时间排会把美股会话的
       凌晨段摆到当晚段前面。
    2. **昨日收盘**: 优先取窗口内**上一个会话的最后一根收盘**(同源真数据, 与图上
       价格同一口径); 窗口里只有一个会话时回退到落盘真日K, 并把它的 source 写进
       ``prev_close_from`` —— 出处不明的昨收会把涨跌幅整条线带偏。
    3. **量与均价**: 成交量按市场换回**股数**(见 ``_LOT_SHARES``), 均价 =
       累计成交额/累计股数(可核对: 不换单位会错 100 倍)。

    会话没走完就如实标 ``session_complete:false`` + ``as_of``, 不补齐、不外推。
    """
    market, _ = _split(symbol)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _MAX_INTRADAY_BARS
    limit = max(1, min(limit, _MAX_INTRADAY_BARS))

    payload = minute_bars(symbol, period="m1", limit=limit)
    rows = payload.get("rows") or []
    if not rows:  # minute_bars 正常情况下已抛空, 这里是双保险
        raise ValueError(f"通达信扩展市场无该标的分钟数据: {symbol} m1")

    keys = [_session_key(r["datetime"], market) for r in rows]
    session_date = keys[-1]
    # 美股会话的两段共用一个日期标签, 必须按真实成交顺序重排, 否则图会从"凌晨 4 点"
    # 画起、昨晚那半截跑到后面去。
    session_rows = sorted((r for r, key in zip(rows, keys, strict=True) if key == session_date),
                          key=lambda r: _session_order_key(r["datetime"]))

    # ── 昨收: 同源 > 落盘日K; 两个都拿不到就 fail-closed(宁可不画, 不猜一个值) ──
    prior_keys = [k for k in dict.fromkeys(keys) if k < session_date]
    daily_source = ""
    if prior_keys:
        prior_rows = sorted((r for r, key in zip(rows, keys, strict=True) if key == prior_keys[-1]),
                            key=lambda r: _session_order_key(r["datetime"]))
        prev_close = _f(prior_rows[-1]["close"])
        prev_close_from = "prev_session_close"
    else:
        daily, daily_source = _daily_rows(symbol, "day", 40)
        earlier = [r for r in daily if str(r["date"]) < session_date]
        prev_close = _f(earlier[-1]["close"]) if earlier else None
        prev_close_from = f"daily:{daily_source}"
    if not prev_close:
        raise ValueError(
            f"{symbol} {session_date} 取不到昨收: "
            f"{'日K分区没有更早的交易日' if not prior_keys else '上一会话收盘为空'}"
        )

    lot = _LOT_SHARES[market]
    points: list[dict] = []
    cum_shares = 0.0
    cum_amount = 0.0
    for row in session_rows:
        close = _f(row.get("close"))
        if close is None:
            continue
        shares = float(row.get("volume") or 0.0) * lot
        amount = float(row.get("amount") or 0.0)
        cum_shares += shares
        cum_amount += amount
        points.append({
            "dt": str(row["datetime"]),
            "t": str(row["datetime"])[11:16],
            "price": close,
            "avg": round(cum_amount / cum_shares, 4) if cum_shares > 0 else close,
            "pct": round((close - prev_close) / prev_close * 100, 4),
            "volume": round(shares, 2),
            "amount": round(amount, 2),
        })
    if not points:
        raise ValueError(f"{symbol} {session_date} 会话内无有效价格点")

    highs = [r["high"] for r in session_rows if _f(r.get("high")) is not None]
    lows = [r["low"] for r in session_rows if _f(r.get("low")) is not None]
    first_open = _f(session_rows[0].get("open"))
    span_minutes = _span_minutes(session_rows[0]["datetime"], session_rows[-1]["datetime"], market)
    complete = span_minutes >= _REGULAR_SPAN_MIN - _COMPLETE_TOLERANCE_MIN

    source = SOURCE_TDX_EX if prev_close_from == "prev_session_close" \
        else f"{SOURCE_TDX_EX}+{daily_source}"
    return {
        "source": source,
        "market": market,
        "symbol": str(symbol).upper(),
        "session_date": session_date,
        "timezone": payload.get("timezone", "Asia/Shanghai"),
        "prev_close": prev_close,
        "prev_close_from": prev_close_from,
        "open": first_open,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "last": points[-1]["price"],
        "pct": points[-1]["pct"],
        "volume": round(cum_shares, 2),
        "amount": round(cum_amount, 2),
        "volume_unit": "share",
        "as_of": session_rows[-1]["datetime"],
        "bars": len(points),
        "session_complete": complete,
        "covered_minutes": span_minutes,
        "expected_minutes": _REGULAR_SPAN_MIN,
        "cached": bool(payload.get("cached")),
        "note": None if complete else
                (f"该会话截至 {session_rows[-1]['datetime']} 只走了 "
                 f"{span_minutes}/{_REGULAR_SPAN_MIN} 分钟(北京时间), 未收盘 —— 不是完整分时"),
        "points": points,
    }


def _session_clock_minutes(dt_str: str, market: str) -> int:
    """会话内的单调分钟刻度 —— 美股凌晨段按**次日**计(加 24h)。

    不加这一步, 美股会话的首根(当晚 21:3x)与末根(凌晨 04:00)相减会得**负数**,
    跨度判据随之失效(明明是完整会话却报"未收盘")。
    """
    from datetime import datetime

    moment = datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
    minutes = moment.hour * 60 + moment.minute
    if market == "US" and moment.hour < _US_SESSION_PHASE_SPLIT:
        minutes += 24 * 60
    return minutes


def _span_minutes(start: str, end: str, market: str) -> int:
    """首末两根之间的分钟跨度(含终点那根)。"""
    return _session_clock_minutes(end, market) - _session_clock_minutes(start, market) + 1


def adjust_ratios(symbol: str, mode: str = "qfq", limit: int = 640) -> dict:
    """复权系数 —— 港美股**均无真源**。

    港股: fqkline 的 qfq/hfq 结果与未复权日 K **逐日完全相同**(高股息票 hk00939/
    hk00011 实证), 说明腾讯只对 A 股做复权, 港股给的是原价。
    美股: fqkline 只返回 1 根。
    """
    raise CapabilityUnavailableError(
        "复权系数无真源: 腾讯 fqkline 的 qfq/hfq 对港股与未复权价逐日相同(不做港股复权), "
        "对美股只返回 1 根; 使用未经调整的价喂给复权档会给出错误的复权曲线"
    )


# ───────────────────────────────────────────── 能力矩阵(给前端收起/置灰用)

def capabilities(market: str) -> dict:
    """声明每个能力在该市场是否可用 —— UI 据此决定显示还是置灰 + 提示原因。"""
    m = str(market or "").strip().upper()
    if m not in ("HK", "US"):
        raise ValueError(f"market 必须是 HK / US: {market!r}")
    native = _NATIVE_PERIODS[m]
    items: dict[str, dict] = {}
    for p in PERIODS:
        if p == "day":
            items[p] = {"available": True, "source": SOURCE_YFINANCE if m == "US" else SOURCE_TENCENT,
                        "derivation": "native"}
        elif p in native:
            items[p] = {"available": True, "source": SOURCE_TENCENT, "derivation": "native"}
        else:
            items[p] = {"available": True, "source": SOURCE_YFINANCE if m == "US" else SOURCE_TENCENT,
                        "derivation": "aggregated_from_daily",
                        "note": "由真实日K确定性聚合, 非交易所原生周期K"}
    items["minute"] = {"available": True, "source": SOURCE_TDX_EX, "derivation": "native",
                       "note": tdx_ex_provider.capabilities_note()}
    items["intraday"] = {"available": True, "source": SOURCE_TDX_EX,
                         "derivation": "composed_from_m1",
                         "note": "当日分时由同一份 1 分钟K 合成(会话切分/昨收/均价都在后端算好), "
                                 f"窗口上限 {_MAX_INTRADAY_BARS} 根"}
    items["adjratio"] = {"available": False, "source": None, "reason": str(adjust_unavailable_reason())}
    items["index"] = {"available": True, "source": SOURCE_TENCENT, "derivation": "native"}
    items["boards"] = {"available": True, "source": SOURCE_TDX_EX, "derivation": "native",
                       "note": "所属板块/同业成分/板块榜(MAC-EX): 港美股此前没有的归类维度"}
    items["multiday"] = {
        "available": True, "source": SOURCE_TDX_EX, "derivation": "native",
        "note": ("多日分时(MAC-EX, 港美股同一条路): 0x240C 按日循环 + 日K 真源昨收, "
                 "三重守卫(日期在日K 日历内 / 当日末价=日K 收盘 / 相邻日不漏), "
                 f"最多 {_MAX_MULTIDAY_DAYS} 日"),
    }
    items["snapshot"] = {"available": True, "source": SOURCE_TDX_EX, "derivation": "native",
                         "note": ("实时快照(MAC-EX): 上游直给昨收/均价/换手率/内外盘, "
                                  "换手率按千分比口径换算(derived)")}
    items["orderbook"] = {
        "available": True, "source": SOURCE_TDX_EX, "derivation": "native",
        "note": ("五档盘口(MAC-EX): 美股为真五档; 上游对港股买卖档恒返回 0, "
                 "届时 payload 里 depth_available=false 并带 note, 不画空档"),
    }
    items["transactions"] = {
        "available": True, "source": SOURCE_TDX_EX, "derivation": "native",
        "note": ("逐笔成交(MAC-EX): 港股走 ex 协议 0x23FC/0x2406(0x122F 对港股恒空), "
                 "价格整数/1000=港元; 美股走 0x122F, 价格已是浮点"),
    }
    items["minute_range"] = {
        "available": True, "source": SOURCE_TDX_EX, "derivation": "native",
        "note": ("多日分钟蜡烛(0x240D 区间K线, 单次 1500 根 + 按日K 日历倒序分片): "
                 "每根都是上游真实开/高/低/收; 更长周期由真实 m1 聚合, 与交易所原生 "
                 "N 分钟 K 逐根一致(实测偏差 0.000%)。不给均价 —— 均价线走多日分时"),
    }
    return {"market": m, "items": items}


def snapshot(symbol: str) -> dict:
    """实时快照 —— 转发 MAC-EX ``symbol_info``(取数细节见 tdx_ex_provider)。

    编排层故意不加料: 这串数据上游一次给齐(含昨收), 再加工只会多一层口径。
    节点全挂属于**故障**而非"没有源", 故 TdxExUnavailableError 原样上抛
    (端点落到 provider_error, 允许前端重试), 不转成 CapabilityUnavailableError。
    """
    return tdx_ex_provider.snapshot(symbol)


def orderbook(symbol: str) -> dict:
    """五档盘口 —— 转发 MAC-EX ``GetInstrumentQuote``。港股无档时由 payload 自证。"""
    return tdx_ex_provider.orderbook(symbol)


def belong_boards(symbol: str) -> dict:
    """个股所属板块 —— 转发 MAC-EX ``SymbolBelongBoard``。"""
    return tdx_ex_provider.belong_boards(symbol)


def board_members(board_code: str, limit: int = 50, start: int = 0) -> dict:
    """板块成分股报价 —— 转发 MAC-EX ``BoardMembersQuotes``。"""
    return tdx_ex_provider.board_members(board_code, limit=limit, start=start)


def board_list(kind: str = "US_HY", limit: int = 50) -> dict:
    """板块榜 —— 转发 MAC-EX ``BoardList``。"""
    return tdx_ex_provider.board_list(kind=kind, limit=limit)


def multiday(symbol: str, days: int = 5) -> dict:
    """多日分时 —— 转发 MAC-EX(0x240C 按日循环 + 日K 真源昨收, 港美股同一条路)。"""
    return tdx_ex_provider.multi_day_ticks(symbol, days=days)


def transactions(symbol: str, date=None, limit: int = 200) -> dict:
    """逐笔成交 —— 转发 MAC-EX(港股 0x23FC/0x2406, 美股 0x122F)。

    取不到就是取不到, 不降级不兜底 —— 逐笔没有"近似"这回事。
    """
    return tdx_ex_provider.transactions(symbol, date=date, limit=limit)


def minute_range(symbol: str, days: int = 5, period: str = "m1") -> dict:
    """多日分钟蜡烛 —— 转发 MAC-EX ``0x240D``(真实 OHLC, 可突破 700 根上限)。"""
    return tdx_ex_provider.minute_range(symbol, days=days, period=period)


#: 多日分时最多几日(超过 10 条线在图上分不清)。
_MAX_MULTIDAY_DAYS = tdx_ex_provider.MAX_MULTIDAY_DAYS


def adjust_unavailable_reason() -> str:
    try:
        adjust_ratios("dummy.HK")
    except CapabilityUnavailableError as exc:
        return str(exc)
    return ""  # pragma: no cover
