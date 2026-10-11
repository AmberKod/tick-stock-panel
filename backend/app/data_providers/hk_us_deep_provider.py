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
    """分钟 K —— 港美股**均无真源**。

    实测腾讯 mkline 对 hk/us 一律 ``{"code":-1,"msg":"param error"}``
    (试过 hk00700,m30 / usAAPL,m30 / usAAPL.OQ,m30 三种形态)。
    """
    raise CapabilityUnavailableError(
        "分钟K无真源: 腾讯 mkline 对港股/美股返回 param error, 无替代源; "
        "缺的不补, 界面应显式标不可用而非用日K降采样冒充"
    )


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
    items["minute"] = {"available": False, "source": None, "reason": str(minute_unavailable_reason())}
    items["adjratio"] = {"available": False, "source": None, "reason": str(adjust_unavailable_reason())}
    items["index"] = {"available": True, "source": SOURCE_TENCENT, "derivation": "native"}
    return {"market": m, "items": items}


def minute_unavailable_reason() -> str:
    try:
        minute_bars("dummy.HK")
    except CapabilityUnavailableError as exc:
        return str(exc)
    return ""  # pragma: no cover


def adjust_unavailable_reason() -> str:
    try:
        adjust_ratios("dummy.HK")
    except CapabilityUnavailableError as exc:
        return str(exc)
    return ""  # pragma: no cover
