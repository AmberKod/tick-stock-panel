r"""港股深度数据层 —— 公告(港交所披露易) + 财报(东财公告原文解析)。

补齐 A股整页 ``/stock/:symbol`` 已有的「公告 / 财报」两块在港股侧的空白。
两个源都是**真源且已实测**, 不引入任何 demo/随机兜底。

来源说明:
    1. **公告 = 港交所披露易 (HKEXnews)**
       ``https://www1.hkexnews.hk/search/prefix.do`` 取 stockId,
       ``titleSearchServlet.do`` 列公告。两步走是因为披露易只认内部 stockId,
       **不认股票代码** —— 直接拿 00700 当 stockId 会返回空列表(假成功)。
       实测 (2026-10-11): 00700 半年内 106 条 / 00939 56 条 / 09988 52 条。

    2. **财报 = 东财公告原文解析**
       复用 ``hk_financial_provider.HKFinancialProvider``(已存在且带
       逐字段溯源: 原始财报行 + 公式 + 公告URL + content_hash), 这里只做
       **缓存与结构整理**。实测 00700.HK 20 期(含季报)耗时 14.8s ⇒ 必须缓存。

fail-closed 纪律(与 jiuzhang_astock_provider 一致):
    本模块**不吞异常**, 网络/解析错误直接上抛, 由 API 层接住转
    ``available: false``。拿不到就说拿不到, 不返回空壳装成功。

⚠️ 两处刻意不"修"的行为:
    - **00011 恒生银行查不到 stockId**(疑似已私有化退市) ⇒ 抛
      ``HKNewsNotFoundError``, 端点如实返回 ``available:false``, 不猜、不返回空列表。
    - **00011.HK 财报解析出 0 行**(该行公告版式解析不出四大比率) ⇒ 同样
       ``available:false``, 不用别的数字顶上。

用法:
    from app.data_providers.hk_deep_provider import announcements, financials
    d = announcements("00700.HK")   # {"source": "live-hkexnews", "data": {...}}
    d = financials("00700.HK")      # {"source": "eastmoney_hk_announcement", ...}
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import polars as pl

logger = logging.getLogger(__name__)

#: 港交所披露易 —— 已登记 source_gate 白名单。
SOURCE_HKEX = "live-hkexnews"
#: 东财港股公告原文解析(hk_financial_provider 的内置 source 名)。
SOURCE_EM_HK_ANN = "eastmoney_hk_announcement"

_HKEX_WEB = "https://www1.hkexnews.hk"
_HKEX_PREFIX_URL = f"{_HKEX_WEB}/search/prefix.do"
_HKEX_SEARCH_URL = f"{_HKEX_WEB}/search/titleSearchServlet.do"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TSP-Research"

#: 公告列表缓存(秒)。披露易是"公司行动"级数据, 一天内不会变。
_HKEX_TTL_S = 3600.0
#: stockId 映射缓存(秒) —— 基本是静态的, 缓存一天。
_HKEX_STOCKID_TTL_S = 86400.0
#: 财报缓存(秒)。单只抓取实测 2.3~14.8s, 财报是季频数据, 缓存 6h 足够。
_HK_FIN_TTL_S = 21600.0
#: 降级结果(被东财限流、只拿到最新一期)的缓存(秒) —— 比正常短得多, 见 financials 末尾注释。
_HK_FIN_DEGRADED_TTL_S = 600.0

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()

#: 5 位港股代码, 允许 4 位补零(0700.HK 与 00700.HK 是同一只)。
_SYMBOL_RE = re.compile(r"\d{4,5}")


class HKNewsNotFoundError(ValueError):
    """披露易查不到这只票的 stockId(退市 / 代码不存在 / 非主板)。

    与"网络失败"区分开: 这是**确定性的无数据**, API 层要写给前端看的原因,
    而不是笼统的 provider_error。
    """


def _cache_get(key: str) -> object | None:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and hit[0] > now:
        return hit[1]
    return None


def _cache_put(key: str, value: object, ttl: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)


def _norm_symbol(raw: str) -> str:
    """'00700' / '0700.HK' / 'hk00700' / '00700.HK' → '00700.HK'。

    披露易与东财都要 5 位代码, 4 位写法(0700.HK)是同一只票, 补零而不是报错。
    """
    m = _SYMBOL_RE.search((raw or "").strip())
    if not m:
        raise ValueError(f"symbol 不是港股代码: {raw!r}")
    return f"{int(m.group(0)):05d}.HK"


def _request_json(url: str, timeout: float) -> object:
    """GET 一个 JSON 接口。异常直接上抛(不吞, 由 API 层转 available:false)。

    显式清代理: 披露易在国内直连可达, 继承 HTTP_PROXY 反而绕远路。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": f"{_HKEX_WEB}/search/titlesearch.xhtml",
    })
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _hkex_stock_id(code5: str, timeout: float = 20.0) -> int:
    """股票代码 → 披露易内部 stockId。找不到抛 HKNewsNotFoundError。

    ⚠️ prefix.do 是**模糊匹配**(返回 00700 可能带 80700 等), 必须按 code
    精确取值, 拿第一个会串票(80700 是人民币柜台, 不是同一只)。
    """
    key = f"hkex:stockid:{code5}"
    cached = _cache_get(key)
    if isinstance(cached, int):
        return cached

    url = f"{_HKEX_PREFIX_URL}?{urllib.parse.urlencode({'callback': 'cb', 'lang': 'ZH', 'type': 'A', 'name': code5, 'market': 'SEHK'})}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Referer": f"{_HKEX_WEB}/search/titlesearch.xhtml",
    })
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    # 返回体是 JSONP: `callback({...});`
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end <= start:
        raise ValueError(f"披露易 stockId 响应不是 JSONP: {raw[:80]!r}")
    body = json.loads(raw[start + 1:end])

    items = body.get("stockInfo") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ValueError("披露易 stockId 响应结构异常")

    for item in items:
        if isinstance(item, dict) and str(item.get("code", "")).zfill(5) == code5:
            stock_id = item.get("stockId")
            if not isinstance(stock_id, int):
                break
            _cache_put(key, stock_id, _HKEX_STOCKID_TTL_S)
            return stock_id

    raise HKNewsNotFoundError(f"披露易查不到 {code5} 的 stockId(可能已退市或非主板标的)")


def _unescape_text(value: object) -> str:
    """披露易标题/分类里混着 HTML 实体与 <br/>: '財務報表&#x2f;...[年報]<br/>'。"""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_category(long_text: str) -> tuple[str, str]:
    """'公告及通告 - [發行債務證券]' → ('公告及通告', '發行債務證券')。"""
    head, _, tail = long_text.partition(" - ")
    sub = ""
    m = re.search(r"\[([^\[\]]*)\]", tail)
    if m:
        sub = m.group(1).strip()
    return head.strip(), sub


def _parse_dt(value: object) -> str | None:
    """'09/10/2026 17:21' → '2026-10-09T17:21:00'。披露易是 日/月/年。"""
    text = str(value or "").strip()
    try:
        dt = datetime.strptime(text, "%d/%m/%Y %H:%M")
    except ValueError:
        return None
    return dt.isoformat()


def announcements(symbol: str, days: int = 180, limit: int = 50,
                  timeout: float = 20.0) -> dict:
    """个股公告 / 公司行动(港交所披露易)。

    Args:
        symbol: '00700' / '0700.HK' / '00700.HK' 都收。
        days:   回溯窗口(自然日), 默认 180。
        limit:  最多返回多少条(1..200), 默认 50。

    Returns:
        ``{"source": SOURCE_HKEX, "data": {"symbol", "stock_id", "rows": [...],
        "window_days", "truncated"}}``

    ⚠️ rows 为空 = 窗口内确实没有公告 —— 这是真实答案, available 仍为 true。
       查不到 stockId(退市)才是 HKNewsNotFoundError, 由 API 层转 available:false。
    """
    code5 = _norm_symbol(symbol)[:-3]  # '00700.HK' -> '00700'
    stock_id = _hkex_stock_id(code5, timeout=timeout)

    if days < 1 or days > 730:
        raise ValueError(f"bad_days: {days} (1..730)")
    if limit < 1 or limit > 200:
        raise ValueError(f"bad_limit: {limit} (1..200)")

    today = datetime.now().date()
    start = today - timedelta(days=days)
    params = {
        "sortDir": "0",
        "sortByOptions": "DateTime",
        "category": "0",
        "market": "SEHK",
        "stockId": str(stock_id),
        "documentType": "-1",
        "fromDate": start.strftime("%Y%m%d"),
        "toDate": today.strftime("%Y%m%d"),
        "title": "",
        "searchType": "1",
        # -2 = 全类别。只筛财报(t2code=40100)会漏掉回购/股东大会等重要公告。
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "rowRange": str(min(max(limit, 1), 200)),
        "lang": "ZH",
    }
    url = f"{_HKEX_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    cached = _cache_get(url)
    raw_rows: list[dict] | None = cached if isinstance(cached, list) else None
    if raw_rows is None:
        payload = _request_json(url, timeout)
        # 嵌套 JSON: 顶层 {"result": "[{...}]"}, result 是**字符串**不是数组
        body = payload if isinstance(payload, dict) else None
        result = (body or {}).get("result")
        if not isinstance(result, str):
            raise ValueError("披露易公告响应结构异常(缺少 result 字符串)")
        parsed = json.loads(result)
        if not isinstance(parsed, list):
            raise ValueError("披露易公告 result 不是数组")
        raw_rows = [r for r in parsed if isinstance(r, dict)]
        _cache_put(url, raw_rows, _HKEX_TTL_S)

    rows: list[dict] = []
    for r in raw_rows[:limit]:
        long_text = _unescape_text(r.get("LONG_TEXT") or r.get("SHORT_TEXT"))
        category, sub = _split_category(long_text)
        link = str(r.get("FILE_LINK") or "").strip()
        rows.append({
            "title": _unescape_text(r.get("TITLE")),
            "category": category,
            "sub_category": sub,
            "announce_at": _parse_dt(r.get("DATE_TIME")),
            "file_type": str(r.get("FILE_TYPE") or "").strip() or None,
            "file_size": str(r.get("FILE_INFO") or "").strip() or None,
            "url": f"{_HKEX_WEB}{link}" if link.startswith("/") else (link or None),
            "news_id": str(r.get("NEWS_ID") or "").strip() or None,
        })

    total = 0
    if raw_rows:
        try:
            total = int(str(raw_rows[0].get("TOTAL_COUNT") or "0"))
        except ValueError:
            total = len(raw_rows)

    return {
        "source": SOURCE_HKEX,
        "data": {
            "symbol": f"{code5}.HK",
            "stock_id": stock_id,
            "rows": rows,
            "window_days": days,
            "total_in_window": total,
            "truncated": total > len(rows),
        },
    }


def _fetch_financials(symbol: str, periods: int, timeout: float) -> tuple[pl.DataFrame | None, str | None]:
    """抓财报, 被限流时**降级**而不是直接失败。返回 (frame, degraded_reason|None)。

    🔴 为什么需要降级: 东财公告接口(``np-cnotice-stock``)在短时间多次请求后会
    返回 **567 Unknown Status**(WAF 限流), 而 ``HKFinancialProvider`` 内部是
    **逐份公告取正文**的 —— 5 年窗口要打几十次请求, 中途任何一份 567 都会让
    整批抛异常, 前面已经解析成功的期数**全部丢弃**(实测 00700.HK 就是这个现象:
    首次 14.8s 拿 20 期, 连打几次后整批 567)。

    降级路径: 完整多期失败 → 改抓**最新一期**(只要 2 次请求, 实测限流下仍可用,
    2.2s)。这样"被限流"退化为"期数变少", 而不是"整个面板不可用"。
    两条路都断才把原始异常上抛(由 API 层转 available:false)。
    """
    from app.data_providers.hk_financial_provider import HKFinancialProvider

    provider = HKFinancialProvider(timeout=timeout)
    if periods <= 1:
        # 只要最新一期: 直接走短路径, 没必要为了"以后可能要"去冒限流的险
        return provider.get_financials("metrics", [symbol], latest_only=True), None

    try:
        return provider.get_financials("metrics", [symbol], latest_only=False), None
    except Exception as exc:
        # 先存下来: except 块结束时 `exc` 会被 Python 销毁, 下面还要用它组原因
        first_error = exc
        logger.warning("hk financials full-history failed (%s), degrade to latest: %s", symbol, exc)

    try:
        frame = provider.get_financials("metrics", [symbol], latest_only=True)
    except Exception:
        # 降级也失败 —— 抛出**原始**异常(更能说明真实原因), 不吞
        raise
    # 原因串要短: httpx 的 HTTPStatusError 自带一堆换行 + MDN 链接, 原样塞进
    # 响应体会把前端的原因行撑爆。只留异常类型 + 首行, 再截断。
    brief = f"{type(first_error).__name__}: {first_error}".splitlines()[0][:120]
    return frame, f"东财公告源限流, 已降级为仅最新一期 ({brief})"


def financials(symbol: str, periods: int = 8, timeout: float = 15.0) -> dict:
    """港股财务比率(东财公告原文解析), 逐字段带溯源。

    走 ``HKFinancialProvider``: 从**公告原文**里抓收入/毛利/净利/归母净利的
    本期与同期数, 再算比率 —— 不是东财 F10 聚合口径(F10 的历史行里挂的是
    **当前**股本, 会污染历史比率, 所以那个端点被刻意排除)。

    Args:
        symbol:  '00700' / '0700.HK' / '00700.HK'。
        periods: 返回最近几期(1..20), 默认 8。

    Returns:
        ``{"source": SOURCE_EM_HK_ANN, "data": {"symbol", "rows": [...],
        "indicators": [...]}}``

        每行含 period_end / announce_date / report_currency / 四大比率, 以及
        ``field_provenance``(原始财报行 + 公式 + 公告URL + content_hash)。

    ⚠️ 空 rows 是**真失败**(解析不出), 由 API 层转 available:false ——
       不是"这家公司没有财报"。实测 00011.HK 就是 0 行。
    """
    if periods < 1 or periods > 20:
        raise ValueError(f"bad_periods: {periods} (1..20)")
    norm = _norm_symbol(symbol)

    key = f"hkfin:{norm}:{periods}"
    cached = _cache_get(key)
    if isinstance(cached, dict):
        return cached

    frame, degraded = _fetch_financials(norm, periods, timeout)
    if frame is None or frame.height == 0:
        raise ValueError(f"港股财报解析出 0 行: {norm}(该股公告版式可能不在解析规则内)")

    records = frame.sort("period_end", descending=True).head(periods).to_dicts()

    rows = []
    for r in records:
        provenance = r.get("field_provenance")
        if isinstance(provenance, str):
            try:
                provenance = json.loads(provenance)
            except (ValueError, TypeError):
                provenance = None
        rows.append({
            "period_end": r.get("period_end").isoformat() if r.get("period_end") else None,
            "announce_date": r.get("announce_date").isoformat() if r.get("announce_date") else None,
            "report_currency": r.get("report_currency"),
            "gross_margin": r.get("gross_margin"),
            "net_margin": r.get("net_margin"),
            "revenue_yoy": r.get("revenue_yoy"),
            "net_income_yoy": r.get("net_income_yoy"),
            "source_url": r.get("source_url"),
            # roe / bps / eps_ttm / total_shares 该 provider 刻意不产(见其模块注释),
            # 这里原样透出 None, 前端显示 "—", 不补零。
            "roe": r.get("roe"),
            "bps": r.get("bps"),
            "eps_ttm": r.get("eps_ttm"),
            "total_shares": r.get("total_shares"),
            "provenance": provenance,
        })

    payload = {
        "source": SOURCE_EM_HK_ANN,
        "data": {
            "symbol": norm,
            "rows": rows,
            # 非 None 表示走了降级路径(东财限流), 前端要如实标"仅最新一期"
            "degraded": degraded,
            "requested_periods": periods,
            # 指标清单与口径写死在后端, 前端照抄, 不另编口径
            "indicators": [
                {"key": "gross_margin", "label": "毛利率", "unit": "%"},
                {"key": "net_margin", "label": "净利率", "unit": "%"},
                {"key": "revenue_yoy", "label": "收入同比", "unit": "%"},
                {"key": "net_income_yoy", "label": "归母净利同比", "unit": "%"},
                {"key": "roe", "label": "ROE", "unit": "%"},
                {"key": "bps", "label": "每股净资产", "unit": None},
                {"key": "eps_ttm", "label": "EPS(TTM)", "unit": None},
                {"key": "total_shares", "label": "总股本", "unit": None},
            ],
            "note": "比率由公告原文的本期/同期数直接算出(非 F10 聚合口径, F10 历史行挂的是当前股本会污染历史比率); 逐字段带原文行与公式溯源。",
        },
    }
    # 降级结果只短缓存: 限流是分钟级的(东财 FUSE 窗口), 10 分钟后重试
    # 大概率已恢复成完整多期 —— 长缓存会把"只有 1 期"钉死 6 小时。
    _cache_put(key, payload, _HK_FIN_DEGRADED_TTL_S if degraded else _HK_FIN_TTL_S)
    return payload
