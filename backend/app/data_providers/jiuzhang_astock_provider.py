r"""A股深度数据层 —— 龙虎榜（九章融合 P2-2 试点）。

来源说明:
    概念与东财 datacenter 接口结构参考九章量化终端 (jiuzhang-algor)
    ``astock.py`` 的 ``dragon_tiger()`` / ``_eastmoney_datacenter()``
    (MIT License, Copyright (c) 2026 小鱼总的小圈子)。**实现为 TSP 自研**,
    与参考实现有三处刻意不同:

    1. **fail-closed**: 参考实现在 ``except`` 里返回
       ``{"source": "fail", "records": [], ...}`` 并把空壳交给前端渲染;
       本模块**不吞异常**, 网络/解析错误直接上抛, 由 API 层接住转
       ``available: false``。拿不到数据就说拿不到, 不返回空壳装成功。
    2. **机构统计不重复计**: 东财的买入(BUY 视图)与卖出(SELL 视图)明细是
       **同一批席位的两视图** —— 两边 ``NET`` 字段值完全相同。参考实现在
       两个视图里各累加一次 ``OPERATEDEPT_CODE == "0"``, 机构金额会翻倍。
       本模块只从 BUY 视图统计一次。
    3. **金额单位显式化**: 原始是元, 统一转万元并写进字段名语义, 避免前端
       再猜单位(美股侧踩过 amount/volume 口径错乱的坑)。

真实性:
    ``source = "live-eastmoney"`` —— 已登记在 ``services/source_gate.py``
    白名单内, 过闸门判定为真实数据。

连通性实测 (2026-10-10):
    - 宿主直连: 通; 容器内 (TickFlow_Stock_Panel): 通;
    - 抽样 002317 众生药业 2026-10-09 上榜, 净买 5767 万, 买卖席位各 5 条;
    - 机构席位判定: ``OPERATEDEPT_CODE == "0"`` 对应「机构专用」(已实测)。

用法:
    from app.data_providers.jiuzhang_astock_provider import dragon_tiger
    d = dragon_tiger("002317")   # {"source": ..., "data": {...}}
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

SOURCE = "live-eastmoney"
#: 巨潮(cninfo)公告 —— 与东财是不同源, 单独标记, 已登记 source_gate 白名单。
SOURCE_CNINFO = "live-cninfo"
#: 资金流走新浪(东财 push2 被墙); 财报摘要走 akshare(东财 datacenter 财务指标)。
#: 两个标记都已在 source_gate 白名单内。
SOURCE_SINA = "sina"
SOURCE_AKSHARE = "akshare"
#: 腾讯财经实时行情(qt.gtimg.cn)。用它补齐东财 push2(被墙)拿不到的字段:
#: 成交额 / 换手率 / 振幅 / PE(TTM) / PB / 总市值 / 流通市值 / 涨停跌停。
#: 九章个股页的行情条也走这个源。标记已在 source_gate 白名单内。
SOURCE_TENCENT = "live-tencent"
#: 东财 F10(公司资料 / 核心题材)。与 datacenter 行情报表同域名不同报表号,
#: 单独标记以便在 UI 上区分"行情来自哪、题材来自哪"。
SOURCE_EM_F10 = "live-eastmoney-f10"

#: 东财 datacenter 统一入口。与 push2.eastmoney.com 是不同域名(后者被墙)。
_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

#: 东财研报列表(reportapi)。同样是东财域名但与 datacenter 不同服务。
_REPORT_URL = "https://reportapi.eastmoney.com/report/list"

#: 巨潮: 先查 orgId, 再查公告列表。orgId 不能靠代码拼(见 _cninfo_org_id 注释)。
_CNINFO_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
_CNINFO_ANN_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_CNINFO_STATIC_PREFIX = "https://static.cninfo.com.cn/"

#: 东财对无 UA 的请求可能拒答, 实测带 UA 稳定。
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TSP-Research"

#: 新浪资金流(东财 push2/push2his 在我方网络被墙, 见 fund_flow 注释)。
_SINA_FUNDFLOW_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/"
                      "api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs")

#: 腾讯实时行情。返回体是 GBK 编码的 `v_sz300308="...";`, 按 `~` 切字段。
_TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
#: 盘中行情短缓存(秒)。个股页每次渲染都打一次会明显拖慢首屏。
_TENCENT_QUOTE_TTL_S = 10.0

#: 腾讯成交明细(每 3 秒一行的成交快照)。注意域名是 **stock**.gtimg.cn,
#: 与分时的 web.ifzq / 分钟的 ifzq 都不同 —— 拼错域名返回空, 属于假成功。
_TENCENT_DETAIL_URL = "https://stock.gtimg.cn/data/index.php"
#: 明细盘中刷新快, 20s 足够(更快只是重复打同一个快照)。
_TENCENT_DETAIL_TTL_S = 20.0
#: 单次最多返回多少行。再多是白打请求 —— 3000 行 ≈ 2.5 小时的快照。
_DETAIL_MAX_TAIL = 3000
#: 分页探测上限。一个交易日约 4800 行 ≈ 69 页, 上限给 400 页足够且能防死循环。
_DETAIL_MAX_PAGES = 400
#: 单次调用最多回溯几页(防 pathological tail 把整日拉空)。
_DETAIL_MAX_PAGES_PER_CALL = 60

#: 腾讯当日分时。与 qt.gtimg.cn 同厂不同域名(web.ifzq.gtimg.cn), UTF-8 JSON,
#: 一次回传全天 240+ 个分钟点(约 15KB)。
_TENCENT_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
#: 分时盘中变化快但体量大, 15s 足够 —— 再快就是白打腾讯的带宽。
_TENCENT_MINUTE_TTL_S = 15.0

#: 腾讯分钟K(m1/m5/m15/m30/m60)。⚠️ 用 ifzq.gtimg.cn 而不是 web.ifzq.gtimg.cn:
#: 实测容器内后者解析 mkline 路径时 DNS 间歇性失败(分时那个路径没事), 前者稳定。
#: 参数格式是 ``param=sh600000,m5,,320``, 不是 ``code=/type=``(那样返回 param error)。
_TENCENT_MKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
#: 分钟K缓存(秒)。历史K不会变, 只有最后一根在盘中跳动 —— 30s 足够。
_TENCENT_MKLINE_TTL_S = 30.0
#: 支持的分钟档位。腾讯不提供 120 分钟, 别让调用方乱传。
_MKLINE_PERIODS = ("m1", "m5", "m15", "m30", "m60")

#: 复权日K(腾讯 fqkline)。与 mkline 同域(ifzq), 分 qfq/hfq 两个分支;
#: **不复权**走另一个端点 appstock/app/kline/kline, 它只认不带 fq 的 param。
_TENCENT_FQKLINE_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
_TENCENT_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/kline"
#: 复权档位。none = 不复权(原始成交价)。
_ADJUST_MODES = ("qfq", "hfq", "none")
#: 日K复权结果一天内不会变(除权日除外), 缓存 10 分钟足够。
_TENCENT_ADJ_TTL_S = 600.0
#: 腾讯 fqkline 单次最多能要多少根(实测 640 可行, 800 会被截断)。
_ADJ_MAX_LIMIT = 640

#: 「机构专用」席位的营业部代码。实测 002317 买入席位中三条机构席位均为 "0"。
_INSTITUTION_DEPT_CODE = "0"

#: 「深股通专用」/「沪股通专用」= 陆股通(北向)席位, 顺带标出来。
_NORTHBOUND_NAMES = ("深股通专用", "沪股通专用")

_WAN = 1e4  # 元 → 万元

#: orgId 是公司的永久标识, 不会变, 缓存一天足够。
_ORG_TTL_S = 86400.0

# 结果缓存: key -> (expire_at, payload)。东财/巨潮都限流, 个股页刷新不该每次都打。
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


def _timeout() -> float:
    from app.config import settings

    return float(settings.eastmoney_timeout_s)


def _cninfo_timeout() -> float:
    from app.config import settings

    return float(settings.cninfo_timeout_s)


def _ttl() -> float:
    from app.config import settings

    return float(settings.eastmoney_cache_ttl_s)


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


def _norm_code(code: str) -> str:
    """'002317' / 'sz002317' / '002317.SZ' → '002317'。非 A股代码直接抛。"""
    m = re.search(r"\d{6}", (code or "").strip())
    if not m:
        raise ValueError(f"code 不是 A股代码: {code!r}")
    return m.group(0)


def _request_json(url: str, data: bytes | None = None,
                  headers: dict[str, str] | None = None,
                  timeout: float | None = None) -> object:
    """GET/POST 一个 JSON 接口。异常直接上抛(不吞, 由 API 层转 available:false)。

    data 非空时走 POST(巨潮), 否则 GET(东财)。显式清代理: 目标在国内直连可达,
    继承到 HTTP_PROXY 会绕远路甚至失败。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    hdr = {"User-Agent": _UA}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr)
    with opener.open(req, timeout=timeout or _timeout()) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _request_text(url: str, encoding: str = "utf-8",
                  headers: dict[str, str] | None = None,
                  timeout: float | None = None) -> str:
    """GET 一个非 JSON 的文本接口(腾讯行情是 GBK)。异常直接上抛。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    hdr = {"User-Agent": _UA}
    hdr.update(headers or {})
    req = urllib.request.Request(url, headers=hdr)
    with opener.open(req, timeout=timeout or _timeout()) as resp:
        return resp.read().decode(encoding, errors="replace")


def _datacenter(report_name: str, filter_str: str = "", page_size: int = 50,
                sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """查东财 datacenter 报表, 返回 data 列表(可能为空)。异常直接上抛。"""
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    url = _DATACENTER_URL + "?" + urllib.parse.urlencode(params)

    cached = _cache_get(url)
    if isinstance(cached, list):
        return cached

    payload = _request_json(url)

    data = ((payload or {}).get("result") or {}).get("data") or []
    if not isinstance(data, list):
        # 结构变了就当失败, 绝不返回猜出来的东西
        raise ValueError(f"datacenter 返回结构异常: {report_name}")

    _cache_put(url, data, _ttl())
    return data


def _wan(value: object) -> float:
    """元 → 万元, 保留 1 位。None/非数字一律 0.0(金额字段, 非指标, 可安全归零)。"""
    try:
        return round(float(value or 0) / _WAN, 1)
    except (TypeError, ValueError):
        return 0.0


def _seat(row: dict) -> dict:
    """席位明细 → 前端友好结构(金额单位: 万元)。"""
    name = str(row.get("OPERATEDEPT_NAME") or "").strip()
    return {
        "name": name,
        "buy_amt": _wan(row.get("BUY")),
        "sell_amt": _wan(row.get("SELL")),
        "net": _wan(row.get("NET")),
        "is_institution": str(row.get("OPERATEDEPT_CODE") or "") == _INSTITUTION_DEPT_CODE,
        "is_northbound": any(n in name for n in _NORTHBOUND_NAMES),
    }


def dragon_tiger(code: str, days: int = 30) -> dict:
    """龙虎榜: 上榜记录 + 最新一次上榜的买卖席位 TOP5 + 机构/北向动向。

    Args:
        code: 6 位 A股代码, 如 "002317"。
        days: 回溯窗口(交易日自然日近似), 默认 30 天。

    Returns:
        ``{"source": "live-eastmoney", "data": {...}}``。
        **该票窗口内没上过榜是真实答案**, 返回 ``records: []`` 且 source 仍
        是真实标记 —— API 层据此判 ``available: true`` 并让前端显示"近期无
        上榜记录"。

    Raises:
        网络/解析/结构异常一律上抛, 不返回空壳。
    """
    # 归一化: 调用方可能传 "002317" / "sz002317" / "002317.SZ"。东财只认裸
    # 6 位代码, 这里兜住 —— API 层也做了一次, 但 provider 必须自洽(防御)。
    code = _norm_code(code)

    end = date.today()
    start = end - timedelta(days=max(1, int(days)))
    window = f"(TRADE_DATE>='{start:%Y-%m-%d}')(TRADE_DATE<='{end:%Y-%m-%d}')(SECURITY_CODE=\"{code}\")"

    rows = _datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filter_str=window,
        page_size=50,
        sort_columns="TRADE_DATE",
        sort_types="-1",
    )

    records = []
    for row in rows:
        records.append({
            "date": str(row.get("TRADE_DATE") or "")[:10],
            "reason": str(row.get("EXPLANATION") or "").strip(),
            "net_buy": _wan(row.get("BILLBOARD_NET_AMT")),
            "turnover": round(float(row.get("TURNOVERRATE") or 0), 2),
            "close_price": row.get("CLOSE_PRICE"),
            "change_rate": row.get("CHANGE_RATE"),
        })

    seats: dict[str, list[dict]] = {"buy": [], "sell": []}
    institution = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0, "seat_count": 0}
    northbound = {"buy_amt": 0.0, "sell_amt": 0.0, "net_amt": 0.0, "present": False}

    if records:
        latest = records[0]["date"]
        latest_filter = f"(TRADE_DATE='{latest}')(SECURITY_CODE=\"{code}\")"

        buy_rows = _datacenter("RPT_BILLBOARD_DAILYDETAILSBUY",
                               filter_str=latest_filter, page_size=5,
                               sort_columns="BUY", sort_types="-1")
        sell_rows = _datacenter("RPT_BILLBOARD_DAILYDETAILSSELL",
                                filter_str=latest_filter, page_size=5,
                                sort_columns="SELL", sort_types="-1")

        seats["buy"] = [_seat(r) for r in buy_rows[:5]]
        seats["sell"] = [_seat(r) for r in sell_rows[:5]]

        # 机构/北向只从 BUY 视图统计一次: 两个视图是同一批席位, NET 值相同,
        # 两边都加会翻倍(参考实现正是这么错的)。
        for seat in seats["buy"]:
            if seat["is_institution"]:
                institution["buy_amt"] = round(institution["buy_amt"] + seat["buy_amt"], 1)
                institution["sell_amt"] = round(institution["sell_amt"] + seat["sell_amt"], 1)
                institution["net_amt"] = round(institution["net_amt"] + seat["net"], 1)
                institution["seat_count"] += 1
            if seat["is_northbound"]:
                northbound["buy_amt"] = round(northbound["buy_amt"] + seat["buy_amt"], 1)
                northbound["sell_amt"] = round(northbound["sell_amt"] + seat["sell_amt"], 1)
                northbound["net_amt"] = round(northbound["net_amt"] + seat["net"], 1)
                northbound["present"] = True

    return {
        "source": SOURCE,
        "data": {
            "code": code,
            "window_days": max(1, int(days)),
            "records": records,
            "seats": seats,
            "institution": institution,
            "northbound": northbound,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _num(value: object) -> float | None:
    """东财的数值字段是字符串('30.1300000000')且可能为 '-' / None。

    拿不到就返回 None —— **不填 0**: 0 会被前端当成"预测 EPS 为 0"这种假事实。

    ⚠️ pandas 的空单元格实际是 ``float('nan')`` 而不是 None(akshare 返回 pandas),
    不判 nan 会把"该期未披露"透成 NaN, 前端显示成 "NaN"。
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return round(f, 4)


def research_reports(code: str, limit: int = 20) -> dict:
    """个股研报列表(东财 reportapi)。

    Args:
        code: 6 位 A股代码。
        limit: 返回条数上限(按发布日期倒序, 接口本身已排序)。

    Returns:
        ``{"source": "live-eastmoney", "data": {...}}``。研报少/没有是真实现象,
        ``reports`` 为空数组但 source 仍是真实标记(与龙虎榜"未上榜"同理)。

    Raises:
        网络/解析异常一律上抛。
    """
    code = _norm_code(code)
    limit = max(1, min(int(limit), 100))

    rows: list[dict] = []
    for page in (1, 2, 3):
        params = {
            "industryCode": "*", "pageSize": "50", "industry": "*", "rating": "*",
            "ratingChange": "*", "beginTime": "2000-01-01", "endTime": "2030-01-01",
            "pageNo": str(page), "fields": "", "qType": "0", "orgCode": "",
            "code": code, "rcode": "", "p": str(page),
            "pageNum": str(page), "pageNumber": str(page),
        }
        cache_key = f"research:{code}:{page}"
        cached = _cache_get(cache_key)
        if not isinstance(cached, dict):
            cached = _request_json(
                _REPORT_URL + "?" + urllib.parse.urlencode(params),
                headers={"Referer": "https://data.eastmoney.com/"},
            )
            if not isinstance(cached, dict):
                raise ValueError("reportapi 返回结构异常")
            _cache_put(cache_key, cached, _ttl())

        batch = cached.get("data") or []
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        try:
            total_page = int(cached.get("TotalPage") or 1)
        except (TypeError, ValueError):
            total_page = 1
        if page >= total_page:
            break

    reports = []
    for x in rows[:limit]:
        info_code = str(x.get("infoCode") or "").strip()
        reports.append({
            "date": str(x.get("publishDate") or "")[:10],
            "org": str(x.get("orgSName") or "").strip(),
            "title": str(x.get("title") or "").strip(),
            "rating": str(x.get("emRatingName") or "").strip(),
            "rating_prev": str(x.get("lastEmRatingName") or "").strip(),
            "rating_change": str(x.get("ratingChange") or "").strip(),
            "target_price": _num(x.get("indvAimPriceT") or x.get("indvAimPriceL")),
            "eps_this_year": _num(x.get("predictThisYearEps")),
            "eps_next_year": _num(x.get("predictNextYearEps")),
            "url": f"https://data.eastmoney.com/report/info/{info_code}.html" if info_code else "",
        })

    return {
        "source": SOURCE,
        "data": {
            "code": code,
            "limit": limit,
            "reports": reports,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _cninfo_org_id(code: str) -> str:
    """巨潮 orgId 查询。

    ⚠️ **不能靠代码拼**: 参考实现九章 ``astock.py:409-414`` 用 ``gssz0{code}``
    拼 orgId, 而巨潮真实的 orgId 是 ``9900022016`` 这类**与代码无关的编号**
    (实测 300308 → 9900022016)。拼出来的 orgId 查询恒返回 0 条, 参考实现的
    公告面板因此永远是空的。这里走官方 topSearch 接口拿真 orgId, 并缓存一天。
    """
    key = f"cninfo-org:{code}"
    cached = _cache_get(key)
    if isinstance(cached, str) and cached:
        return cached

    body = urllib.parse.urlencode({"keyWord": code, "maxNum": "10"}).encode("utf-8")
    payload = _request_json(
        _CNINFO_SEARCH_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        },
        timeout=_cninfo_timeout(),
    )
    if not isinstance(payload, list):
        raise ValueError("cninfo topSearch 返回结构异常")

    org_id = ""
    for item in payload:
        if not isinstance(item, dict):
            continue
        # 精确匹配代码: 搜索会返回名称相近的公司, 取代码一致的那个
        if str(item.get("code") or "").strip() == code:
            org_id = str(item.get("orgId") or "").strip()
            break
    if not org_id:
        raise ValueError(f"cninfo 未找到 {code} 的 orgId")
    _cache_put(key, org_id, _ORG_TTL_S)
    return org_id


def announcements(code: str, limit: int = 20) -> dict:
    """个股公告列表(巨潮 cninfo 全文检索)。

    Returns:
        ``{"source": "live-cninfo", "data": {"announcements": [...]}}``。
        每条含日期/标题/类型/原文 PDF 链接(adjunctUrl 是相对路径, 需拼前缀)。
    """
    code = _norm_code(code)
    limit = max(1, min(int(limit), 100))
    org_id = _cninfo_org_id(code)

    payload = {
        "stock": f"{code},{org_id}", "tabName": "fulltext", "pageSize": str(limit),
        "pageNum": "1", "column": "", "category": "", "plate": "", "seDate": "",
        "searchkey": "", "secid": "", "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    key = f"cninfo-ann:{code}:{limit}"
    cached = _cache_get(key)
    if not isinstance(cached, dict):
        cached = _request_json(
            _CNINFO_ANN_URL,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.cninfo.com.cn/new/disclosure",
                "Origin": "https://www.cninfo.com.cn",
            },
            timeout=_cninfo_timeout(),
        )
        if not isinstance(cached, dict):
            raise ValueError("cninfo hisAnnouncement 返回结构异常")
        _cache_put(key, cached, _ttl())

    out = []
    for x in (cached.get("announcements") or [])[:limit]:
        ts = x.get("announcementTime")
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        else:
            dt = str(ts)[:10]
        adjunct = str(x.get("adjunctUrl") or "").strip()
        # isHLtitle=true 时标题里会插 <em> 高亮标签, 必须剥掉再展示
        title = re.sub(r"</?em>", "", str(x.get("announcementTitle") or "")).strip()
        # ⚠️ 实测(2026-10-10): announcementTypeName 恒为 None, 而 announcementType
        # 是 "01010503||010112||010115" 这类**分类编码**, 不是可读名称。直接把编码
        # 当类型名展示会在前端出现一串竖线数字 —— 这里只取可读名, 拿不到就留空,
        # 编码另存 type_codes 供后续做映射表, 绝不拿编码冒充名称。
        out.append({
            "date": dt,
            "title": title,
            "type": str(x.get("announcementTypeName") or "").strip(),
            "type_codes": str(x.get("announcementType") or "").strip(),
            "url": _CNINFO_STATIC_PREFIX + adjunct if adjunct else "",
        })

    return {
        "source": SOURCE_CNINFO,
        "data": {
            "code": code,
            "limit": limit,
            "announcements": out,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def fund_flow(code: str, days: int = 20) -> dict:
    """个股资金流向(新浪 MoneyFlow 日线)。

    ⚠️ **为什么不用东财**: 参考实现九章 ``astock.py:245-302`` 走东财
    ``push2/push2his`` 的 ``fflow`` 接口, 而 **push2 与 push2his 在我方网络
    均被墙**(实测 2026-10-10: 连接级 RST / RemoteDisconnected, akshare 的
    ``stock_individual_fund_flow`` 同样 ProxyError)。新浪 vip 接口实测可达。

    新浪口径: ``netamount`` = 主力净流入, ``r0_net`` = 超大单净额(该接口只给
    到 r0 一档, 没有 r1/r2/r3; 不编造缺失档位)。

    Returns:
        ``{"source": "sina", "data": {"rows": [...]}}``, 金额单位万元, 按日期倒序。
    """
    code = _norm_code(code)
    days = max(1, min(int(days), 120))
    # 新浪前缀: 6 开头沪市, 8/4/9 开头北交所(用 bj), 其余深市
    if code.startswith("6"):
        prefix = "sh"
    elif code.startswith(("8", "4", "9")):
        prefix = "bj"
    else:
        prefix = "sz"

    url = (_SINA_FUNDFLOW_URL + "?" +
           urllib.parse.urlencode({"daima": f"{prefix}{code}"}))
    key = f"sina-fundflow:{prefix}{code}"
    cached = _cache_get(key)
    if not isinstance(cached, list):
        cached = _request_json(
            url,
            headers={"Referer": "https://finance.sina.com.cn/"},
            timeout=_cninfo_timeout(),
        )
        if not isinstance(cached, list):
            raise ValueError("新浪资金流返回结构异常")
        _cache_put(key, cached, _ttl())
    if not cached:
        raise ValueError(f"新浪资金流无数据: {code}")

    rows = []
    for x in cached[:days]:
        rows.append({
            "date": str(x.get("opendate") or "")[:10],
            "close": _num(x.get("trade")),
            "change_pct": _pct(x.get("changeratio")),
            "turnover": _num(x.get("turnover")),
            # 元 → 万元
            "net_amount": _wan(x.get("netamount")),
            "super_large_net": _wan(x.get("r0_net")),
        })

    return {
        "source": SOURCE_SINA,
        "data": {
            "code": code,
            "days": days,
            "rows": rows,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _pct(value: object) -> float | None:
    """新浪涨跌幅是小数(-0.00984674 = -0.98%), 转百分数并保留 2 位。"""
    v = _num(value)
    return None if v is None else round(v * 100, 2)


def financials(code: str, periods: int = 8) -> dict:
    """A股财务摘要(akshare 东财 datacenter 财务分析指标)。

    返回 7 组(常用指标/每股指标/盈利能力/成长能力/收益质量/财务风险/营运能力)
    共 80 个指标 × 最近 N 个报告期。宽表转长表: 每行一个指标, values 按报告期
    **从新到旧**排列 —— 前端横向展示时最新期在最左。

    ⚠️ akshare 在导入时开销较大, 这里**延迟导入**, 避免拖慢进程启动。
    """
    code = _norm_code(code)
    periods = max(1, min(int(periods), 12))

    key = f"astock-fin:{code}"
    cached = _cache_get(key)
    if not isinstance(cached, dict) or not cached:
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover - 环境缺依赖
            raise RuntimeError(f"akshare 未安装: {exc}") from exc
        df = ak.stock_financial_abstract(symbol=code)
        if df is None or len(df) == 0:
            raise ValueError(f"财务摘要无数据: {code}")
        period_cols = [c for c in df.columns if str(c).isdigit()]
        period_cols = sorted(period_cols, reverse=True)[:periods]
        if not period_cols:
            raise ValueError(f"财务摘要无报告期列: {code}")

        indicators = []
        # akshare 返回的是 **pandas** DataFrame(不是 polars), 没有 .to_dicts()
        for row in df.to_dict(orient="records"):
            name = str(row.get("指标") or "").strip()
            if not name:
                continue
            values = []
            for col in period_cols:
                values.append({"period": str(col), "value": _num(row.get(col))})
            indicators.append({
                "name": name,
                "group": str(row.get("选项") or "").strip(),
                "values": values,
            })
        cached = {"periods": [str(c) for c in period_cols], "indicators": indicators}
        _cache_put(key, cached, _ttl())

    if not cached.get("indicators"):
        raise ValueError(f"财务摘要解析为空: {code}")

    return {
        "source": SOURCE_AKSHARE,
        "data": {
            "code": code,
            "periods": cached["periods"],
            "indicators": cached["indicators"],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _tx_symbol(code: str) -> str:
    """A股 6 位代码 → 腾讯符号(sz300308 / sh600000 / bj830799)。

    沪深北规则: 6/9 开头与 5 开头(沪市基金)归 sh, 4/8 开头(北交所)归 bj, 其余归 sz。
    """
    c = _norm_code(code)
    if c[0] in ("4", "8"):
        return "bj" + c
    if c[0] in ("6", "9") or c.startswith("5"):
        return "sh" + c
    return "sz" + c


def _tx_index_symbol(code: str) -> str:
    """指数 6 位代码 → 腾讯符号(sh000300 / sz399006 / bj899050)。

    ⚠️ 不能复用 ``_tx_symbol``: 那是**个股**规则(0 开头 → sz), 而沪市指数恰恰是
    0 开头(000300 沪深300 / 000001 上证指数)却属于 sh。按个股规则会把沪深300
    拼成 sz000300, 腾讯返回空 —— 这类"拼错也能跑通"的错误最难查。
    规则: 3/399 开头(深证/创业板)→ sz, 8 开头(北证)→ bj, 其余 → sh。
    """
    c = _norm_code(code)
    if c.startswith("399"):
        return "sz" + c
    if c[0] == "8":
        return "bj" + c
    return "sh" + c


def index_daily(code: str, limit: int = 320) -> dict:
    """指数日K(腾讯 kline/kline) —— 对比页「基准指数」的真源。

    为什么单独开一个函数(而不是复用 adjust_ratios / 本地 index_daily 库):
      - **指数不需要复权**: 指数是点位, 不存在除权, 走 kline/kline 拿 day 即可;
      - **本地 index 库不全**: 实测本地只有 399006(创业板指), 沪深300/上证指数
        都没有 —— 若只认本地库, 最常用的两个基准就直接"取不到"了。
        腾讯直连可达, 是真源, 所以走它(与个股日K/分钟K同源同标记)。

    ⚠️ 字段口径与个股日K一致: 行 = ``[日期, 开, 收, 高, 低, 量]`` ——
       **第 2 列是收**, 不是常见 OHLC 顺序的收在最后。指数没有成交额列,
       量是成交量(手), 只用于参考, 对比页算相对强弱只用 close。
    """
    code = _norm_code(code)
    sym = _tx_index_symbol(code)
    # 上限比个股复权那档宽(1000): 对比页最长看 3 年(750 交易日), 基准若只给 640
    # 根, 三年档会被截成两年半 —— 实测腾讯 day 序列给到 1000 根没问题。
    limit = max(20, min(int(limit or 320), 1000))

    url = _TENCENT_KLINE_URL + "?" + urllib.parse.urlencode(
        {"param": f"{sym},day,,,{limit}"})
    cache_key = f"idx|{url}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return cached

    payload = _request_json(url)
    node = ((payload or {}).get("data") or {}).get(sym)
    rows = (node or {}).get("day") if isinstance(node, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"腾讯未返回指数日K: {sym}")

    def _col(r: list, i: int) -> float | None:
        return _num(r[i]) if isinstance(r, (list, tuple)) and len(r) > i else None

    out: list[dict] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 5:
            continue
        d = str(r[0]).strip()[:10]
        o = _col(r, 1)      # 开
        c = _col(r, 2)      # 收
        h = _col(r, 3)      # 高
        lo = _col(r, 4)     # 低
        v = _col(r, 5)
        if not d or c is None or c <= 0:
            continue
        out.append({
            "date": d, "open": o, "close": c, "high": h, "low": lo,
            "volume": None if v is None else round(v * 100, 0),   # 手 → 股
        })
    if not out:
        raise ValueError(f"腾讯指数日K无法解析: {sym}")

    # 名称: 顺手打一次行情接口拿(GBK)。拿不到就退成代码, 不编名字。
    name = None
    try:
        raw = _request_text(_TENCENT_QUOTE_URL + sym, encoding="gbk")
        m = re.search(rf'v_{re.escape(sym)}="([^"]*)"', raw)
        if m:
            p = m.group(1).split("~")
            if len(p) > 1 and p[1].strip():
                name = p[1].strip()
    except Exception:
        name = None

    result = {
        "source": SOURCE_TENCENT,
        "data": {
            "code": code,
            "symbol": sym,
            "name": name,
            "rows": out,
            "coverage": {"from": out[0]["date"], "to": out[-1]["date"], "bars": len(out)},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    _cache_put(cache_key, result, _TENCENT_ADJ_TTL_S)
    return result


def tick_trades(code: str, tail: int = 200) -> dict:
    """A股**成交明细**(腾讯 stock.gtimg.cn) —— 个股页「成交明细」面板的真源。

    连通性实测 (2026-10-11, 宿主机与容器内均通):
        ``https://stock.gtimg.cn/data/index.php?appn=detail&action=data&c=sh600519&p=0``
        返回 ``v_detail_data_sh600519=[0,"<行>|<行>|…"]``

    ⚠️ **这是每 3 秒一行的成交快照, 不是逐笔**: 行的时间戳严格以 3 秒为步长
        (09:30:02 / 09:30:05 / 09:30:08 …), 每行是该时间窗内所有成交的合计。
        真正的逐笔要 Level-2 行情(付费), 免费公开源只能拿到这个粒度。
        UI 必须写清这一点 —— 标着"逐笔"卖 3 秒快照就是误导。

    ⚠️ 字段口径: 行 = ``序号/时间/成交价/涨跌额/成交量/成交额/方向``
        - 方向是 B(主动买)/S(主动卖)/M(中性, 无法判断), 由行情商按成交价与当时
          盘口的比较判定, **不是交易所披露字段**;
        - 09:25:xx 是开盘集合竞价, 14:57 之后含收盘集合竞价, 单独打标。

    🔴 **成交量单位不猜, 直接用额和价算股数**: 量与额同在一行, 而 ``额 = 股数 × 价``
        恒成立, 所以 ``股数 = round(额 / 价)`` 是精确反推(实测 600519 主板 / 000651
        深主板 / 688825 科创板 / 510300 ETF 全部自洽, 价的四舍五入只带来 <0.1% 误差)。
        反过来若信那一列的"手", 科创板与 ETF 会差 100 倍(分时那档已经踩过一次)。

    分页: 每页固定 70 行, 只能从开盘往后翻(p=-1 无效), 所以先倍增+二分找到
    最后一页, 再往前回溯取尾部。整页结果按 (日期, 页码) 缓存, 一次查询里的探测
    不会重复打同一个 URL。
    """
    code = _norm_code(code)
    sym = _tx_symbol(code)
    tail = max(1, min(int(tail or 200), _DETAIL_MAX_TAIL))

    # 数据日期: 明细接口本身不带日期, 从实时行情的行情时间拿(非交易日会是最近
    # 一个交易日的收盘时间)。拿不到日期就不硬编今天 —— 会让"这是哪天的明细"说不清。
    trade_date = None
    try:
        qt = realtime_quote(code)["data"]
        t = (qt.get("quote_time") or "").strip()
        if len(t) >= 8 and t[:8].isdigit():
            trade_date = f"{t[:4]}-{t[4:6]}-{t[6:8]}"
    except Exception:  # 行情挂了不代表明细挂了, 只是日期归零
        trade_date = None

    memo: dict[int, list[dict]] = {}

    def page(pn: int) -> list[dict]:
        if pn in memo:
            return memo[pn]
        rows = _detail_page(sym, pn)
        memo[pn] = rows
        return rows

    first = page(0)
    if not first:
        raise ValueError(f"腾讯成交明细无数据(今日无成交 / 不支持的标的): {sym}")

    last = _detail_last_page(page)

    # 从最后一页往回取, 够 tail 行就停
    out: list[dict] = []
    pages_used: list[int] = []
    pn = last
    fetched = 0
    while pn >= 0 and len(out) < tail and fetched < _DETAIL_MAX_PAGES_PER_CALL:
        rows = page(pn)
        pages_used.append(pn)
        fetched += 1
        out = rows + out
        pn -= 1
    out = out[-tail:]

    if not out:
        raise ValueError(f"腾讯成交明细解析结果为空: {sym}")

    summary = _tick_summary(out)
    result = {
        "source": SOURCE_TENCENT,
        "data": {
            "code": code,
            "symbol": sym,
            "date": trade_date,
            # 新的在前 —— 成交明细的阅读习惯是盯最新一笔
            "rows": list(reversed(out)),
            "summary": summary,
            "coverage": {
                "rows": len(out),
                "requested_tail": tail,
                "pages": pages_used,
                "last_page": last,
                "from": out[0]["time"],
                "to": out[-1]["time"],
            },
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    _cache_put(f"detail:{sym}:{trade_date}:{tail}", result, _TENCENT_DETAIL_TTL_S)
    return result


def _tick_summary(rows: list[dict]) -> dict:
    """对**当前这批行**做汇总(不做全日推算): 主动买/卖/中性拆开, 净额 = 买额 − 卖额。

    中性(M)既不算买也不算卖 —— 硬塞到一边等于替交易所做它没有的判断。
    """
    buy_a = buy_v = sell_a = sell_v = neu_a = neu_v = 0.0
    total_a = total_v = 0.0
    max_amt = 0.0
    max_row: dict | None = None
    for r in rows:
        a = r.get("amount") or 0.0
        v = r.get("shares") or 0.0
        total_a += a
        total_v += v
        if a > max_amt:
            max_amt = a
            max_row = r
        if r.get("side") == "B":
            buy_a += a
            buy_v += v
        elif r.get("side") == "S":
            sell_a += a
            sell_v += v
        else:
            neu_a += a
            neu_v += v
    return {
        "count": len(rows),
        "amount": round(total_a, 2),
        "shares": round(total_v, 0),
        "buy_amount": round(buy_a, 2),
        "buy_shares": round(buy_v, 0),
        "sell_amount": round(sell_a, 2),
        "sell_shares": round(sell_v, 0),
        "neutral_amount": round(neu_a, 2),
        "neutral_shares": round(neu_v, 0),
        "net_amount": round(buy_a - sell_a, 2),
        "max_amount": round(max_amt, 2),
        "max_time": max_row["time"] if max_row else None,
        "max_price": max_row["price"] if max_row else None,
        "from": rows[0]["time"],
        "to": rows[-1]["time"],
    }


def _detail_page(sym: str, page_no: int) -> list[dict]:
    """拉一页成交明细并解析。返回 [] = 这一页没有数据(翻过头了)。"""
    url = _TENCENT_DETAIL_URL + "?" + urllib.parse.urlencode(
        {"appn": "detail", "action": "data", "c": sym, "p": max(0, int(page_no))})
    cached = _cache_get(url)
    if isinstance(cached, list):
        return cached
    raw = _request_text(url, encoding="utf-8", timeout=_timeout())
    m = re.search(rf'v_detail_data_{re.escape(sym)}\s*=\s*\[\s*\d+\s*,\s*"(.*?)"\s*\]', raw, re.S)
    if not m:
        rows: list[dict] = []
        _cache_put(url, rows, _TENCENT_DETAIL_TTL_S)
        return rows
    rows = [r for r in (_detail_row(x) for x in m.group(1).split("|")) if r]
    _cache_put(url, rows, _TENCENT_DETAIL_TTL_S)
    return rows


def _detail_row(seg: str) -> dict | None:
    """解析一行 ``序号/时间/成交价/涨跌额/成交量/成交额/方向``。坏行返回 None(丢, 不猜)。"""
    p = (seg or "").split("/")
    if len(p) < 6:
        return None
    price = _num(p[2])
    amount = _num(p[5])
    if price is None or amount is None or price <= 0 or amount < 0:
        return None
    tmv = (p[1] or "").strip()
    # 🔴 必须是合法时刻: ``\d{2}:\d{2}:\d{2}`` 会放过 99:99:99 这种坏值,
    #    那条"时间"会一路带到 UI 上当真数据(写测试时才抓到)。
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d:[0-5]\d$", tmv):
        return None
    # 股数由「额 ÷ 价」反推 —— 不依赖第 5 列的单位(那列随标的是手还是股并不统一)
    shares = round(amount / price, 0)
    side = (p[6] or "").strip().upper()[:1] if len(p) > 6 else ""
    return {
        "seq": int(_num(p[0]) or 0),
        "time": tmv,
        "price": price,
        "change": _num(p[3]),
        "amount": amount,
        "shares": shares,
        "lots": round(shares / 100.0, 2),
        "side": side if side in ("B", "S", "M") else "M",
        "auction": _detail_session(tmv),
    }


def _detail_session(t: str) -> str | None:
    """把时间归到交易时段 —— 收盘集合竞价与 15:00 之后的成交**不是同一回事**。

    实测: 沪主板末行停在 15:00:02; 深市个股/科创板/ETF 会往后排到 15:2x,
    那是盘后成交(盘后固定价格交易等), 量通常很小。如果把它们也算进"收盘集合竞价",
    会把集合竞价的金额抬高 —— 所以三档分开打标, 让 UI 能区分。
    """
    if t <= "09:25:30":
        return "open"      # 开盘集合竞价
    if "14:57:00" <= t <= "15:00:59":
        return "close"     # 收盘集合竞价(含 15:00 的撮合出清)
    if t > "15:00:59":
        return "after"     # 盘后成交, 不在连续竞价内
    return None


def _detail_last_page(page) -> int:
    """倍增 + 二分找最后一个非空页。``page`` 是带缓存的取页函数(返回非空即存在)。

    明细只能从开盘往后翻, 要"最近 N 行"就得先知道尾巴在哪 —— 但也不想为了取
    200 行去打 70 次请求, 所以先对数级探一次再只回溯需要的页数。
    """
    hi = 1
    while hi <= _DETAIL_MAX_PAGES and page(hi):
        hi *= 2
    lo = max(0, hi // 2)
    hi = min(hi, _DETAIL_MAX_PAGES)
    # 此时 lo 已知非空、hi 已知空(或撞到上限), 二分收窄到最后一个非空页
    if page(hi):
        return hi
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if page(mid):
            lo = mid
        else:
            hi = mid
    return lo


def realtime_quote(code: str) -> dict:
    """A股实时行情(腾讯) —— 补齐行情条被东财 push2 卡住的那一半字段。

    九章的行情条显示 今开/昨收/最高/最低/成交量/成交额/换手/PE/PB/总市值,
    其中后五项它靠腾讯拿。我方东财 push2 被墙, 但腾讯直连可达, 所以同样的
    真数据我方也拿得到 —— 不需要为了"对齐"而显示假数字。

    ⚠️ 字段下标口径(实测 sz300308, 88 个字段):
        1 名称 / 3 现价 / 4 昨收 / 5 今开 / 6 成交量(手) / 30 行情时间 /
        31 涨跌额 / 32 涨跌幅% / 33 最高 / 34 最低 / 37 成交额(万) /
        38 换手率% / 39 PE(TTM) / 43 振幅% / 44 流通市值(亿) / 45 总市值(亿) /
        46 PB / 47 涨停价 / 48 跌停价
    🔴 **44 是流通市值、45 是总市值** —— 九章 `tx_quote()` 把这两个写反了
       (它 p[44]→total_cap、p[45]→float_cap), 导致个股页"总市值"显示的是流通市值。
       这里按实测口径修过来, 并用测试锁死。

    单位: 金额类返回**元**(前端自行换算), 比例类返回百分数。
    """
    code = _norm_code(code)
    sym = _tx_symbol(code)

    url = _TENCENT_QUOTE_URL + sym
    cached = _cache_get(url)
    if isinstance(cached, dict):
        return cached

    raw = _request_text(url, encoding="gbk")

    m = re.search(rf'v_{re.escape(sym)}="([^"]*)"', raw)
    if not m:
        raise ValueError(f"腾讯未返回行情: {sym}")
    p = m.group(1).split("~")
    if len(p) < 47:
        raise ValueError(f"腾讯行情字段不足({len(p)}): {sym}")

    def at(i: int) -> str | None:
        v = p[i].strip() if i < len(p) else ""
        return v or None

    price = _num(at(3))
    if price is None:
        raise ValueError(f"腾讯行情无价格: {sym}")

    # 金额单位: 37 是万元, 44/45 是亿元 —— 统一折算成元
    amount = _num(at(37))
    float_cap = _num(at(44))
    total_cap = _num(at(45))

    payload = {
        "code": code,
        "symbol": sym,
        "name": at(1),
        "price": price,
        "prev_close": _num(at(4)),
        "open": _num(at(5)),
        "high": _num(at(33)),
        "low": _num(at(34)),
        "change": _num(at(31)),
        "change_pct": _num(at(32)),
        # 6 是手 → 股
        "volume": None if _num(at(6)) is None else round((_num(at(6)) or 0) * 100, 0),
        "amount": None if amount is None else round(amount * _WAN, 2),
        "turnover_pct": _num(at(38)),
        "amplitude_pct": _num(at(43)),
        "pe_ttm": _num(at(39)),
        "pb": _num(at(46)),
        "float_cap": None if float_cap is None else round(float_cap * 1e8, 2),
        "total_cap": None if total_cap is None else round(total_cap * 1e8, 2),
        "limit_up": _num(at(47)),
        "limit_down": _num(at(48)),
        "quote_time": at(30),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    result = {"source": SOURCE_TENCENT, "data": payload}
    _cache_put(url, result, _TENCENT_QUOTE_TTL_S)
    return result


def _volume_unit_factor(cum_v: float, cum_a: float, low: float, high: float) -> float | None:
    """反推腾讯分时第 3 列的单位: 返回 1.0(股) / 100.0(手), 判不出来返回 None。

    依据: ``成交额 = 成交量 × 均价``, 而**均价必然落在当日 [low, high] 之间**
    (它就是全天成交的加权平均价)。把累计额/累计量算出来:
      - 落在 [low, high] 内 → 单位已经是股 → 1.0;
      - 除以 100 后落在区间内 → 单位是手 → 100.0;
      - 都不是 → None(数据自相矛盾, 交给调用方 fail-closed, 不猜)。
    留 5% 容差吸收一字板 / 均价贴近边界的情形。
    """
    if cum_v <= 0 or cum_a <= 0 or low <= 0:
        return None

    def within(v: float) -> bool:
        return low * 0.95 <= v <= high * 1.05

    implied = cum_a / cum_v
    if within(implied):
        return 1.0
    if within(implied / 100.0):
        return 100.0
    return None


def _tx_volume_unit_from_qt(qt: object, lo: float, hi: float) -> float | None:
    """从腾讯 ``qt`` 数组反推该标的成交量的单位(1.0=股 / 100.0=手)。

    分钟K(mkline)的行里**只有量没有额**, 没法像分时那样用本行数据自校验,
    但同一个响应体里的 qt 数组有 [6]成交量 与 [37]成交额(万元) —— 两者都是
    **同一时刻的当日累计值**, 比值就是当日均价, 同样必须落在价格区间内。
    """
    if not isinstance(qt, list) or len(qt) < 38:
        return None
    vol = _num(qt[6])
    amt_wan = _num(qt[37])
    if vol is None or amt_wan is None or vol <= 0:
        return None
    return _volume_unit_factor(vol, amt_wan * _WAN, lo, hi)


def minute_kline(code: str, period: str = "m5", limit: int = 320) -> dict:
    """A股**分钟K**(腾讯 mkline): 1分 / 5分 / 15分 / 30分 / 60分。

    九章个股页的周期档里有 1/5/15/30/60 分钟, 我方本地 kline_minute 库对这些标的是
    空的(实测 688825 `source: none`, 且分钟同步要 Pro+ 权限) —— 走腾讯这个端点
    一样拿得到真数据, 不需要等本地库补齐。

    ⚠️ 字段口径(实测 sh688825, m5 320 根):
       行 = ``[时间(YYYYMMDDHHMM), 开, 收, 高, 低, 量, {}, extra]`` —— 注意**第 2 列是
       开、第 3 列是收**(不是 OHLC 顺序), 且与日K接口一样**没有成交额列**。
       时间是该区间的**结束时刻**(15:00 那根是 14:55~15:00)。
       量单位同样随标的不同(主板手 / 科创板股), 用 qt 数组反推(见
       ``_tx_volume_unit_from_qt``)。输出统一为**股**。

    ⚠️ 不补 K: 腾讯给多少根就是多少根, 缺失的区间(停牌/集合竞价)不插零轴。
    """
    code = _norm_code(code)
    sym = _tx_symbol(code)
    period = (period or "").strip().lower()
    if period not in _MKLINE_PERIODS:
        raise ValueError(f"period 必须是 {_MKLINE_PERIODS} 之一: {period!r}")
    limit = max(20, min(int(limit or 320), 640))

    url = _TENCENT_MKLINE_URL + "?" + urllib.parse.urlencode(
        {"param": f"{sym},{period},,{limit}"})
    cached = _cache_get(url)
    if isinstance(cached, dict):
        return cached

    payload = _request_json(url)
    node = ((payload or {}).get("data") or {}).get(sym)
    raw_rows = (node or {}).get(period) if isinstance(node, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError(f"腾讯未返回分钟K: {sym} {period}")

    qt = (node.get("qt") or {}).get(sym) if isinstance(node, dict) else None

    parsed: list[tuple[str, float, float, float, float, float]] = []
    for r in raw_rows:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        ts = str(r[0]).strip()
        o, c = _num(r[1]), _num(r[2])
        h, lo, v = _num(r[3]), _num(r[4]), _num(r[5])
        if o is None or c is None or h is None or lo is None or v is None:
            continue
        if len(ts) == 12:
            ts = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}"
        parsed.append((ts, o, c, h, lo, v))

    if not parsed:
        raise ValueError(f"腾讯分钟K无法解析: {sym} {period}")

    # 单位判定用**最后一天**的价格区间: qt 是当日累计, 拿全历史的 low/high 会错位
    last_day = parsed[-1][0][:10]
    day_rows = [p for p in parsed if p[0].startswith(last_day)]
    lo = min(p[4] for p in day_rows)
    hi = max(p[3] for p in day_rows)
    factor = _tx_volume_unit_from_qt(qt, lo, hi)
    if factor is None:
        raise ValueError(f"腾讯分钟K量单位无法判定: {sym} {period}")

    rows = [{
        "date": p[0],
        "open": p[1],
        "close": p[2],
        "high": p[3],
        "low": p[4],
        "volume": round(p[5] * factor, 0),
    } for p in parsed]

    result = {
        "source": SOURCE_TENCENT,
        "data": {
            "code": code,
            "symbol": sym,
            "name": qt[1] if isinstance(qt, list) and len(qt) > 1 else None,
            "period": period,
            "rows": rows,
            "prev_close": _num(qt[4]) if isinstance(qt, list) and len(qt) > 4 else None,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    _cache_put(url, result, _TENCENT_MKLINE_TTL_S)
    return result


def adjust_ratios(code: str, mode: str = "qfq", limit: int = 640) -> dict:
    """A股日K**复权系数**(腾讯 fqkline) —— 前复权 / 后复权 / 不复权三档的真源。

    为什么是"系数"而不是直接返回复权后的整串 K:
        本地 enriched 里除了 OHLC 还挂着指标/涨跌停信号/成交额/换手/ext 列, 整串换掉
        会把这些一起丢掉。这里只给 ``ratio = 复权价 / 不复权价``, 调用方乘到自己那串
        K 的价格列上 —— 复权是**价格缩放**, 不是换数据源。

    ⚠️ 为什么必须自己算系数(而不是用本地除权因子):
        本地 ``adj_factor`` 表是空的 —— TickFlow 的 ``ex_factors`` 对本账号返回 0 行
        (实测 600519/600000/000001 全空), 所以 ``compute_enriched`` 的"前复权"实际是
        空转, enriched 里存的是**不复权价**。继续拿它当前复权用就是错的。

    ⚠️ 字段口径(实测 sh600519, 250 根):
        行 = ``[日期(YYYY-MM-DD), 开, 收, 高, 低, 量]`` —— 与日K接口一致, **第 2 列开、
        第 3 列收**。三个档位分别取响应里的 ``qfqday`` / ``hfqday`` / ``day`` 键。
        实测 600519: 2026-06-26 除权, 之前 raw=1207.68 / qfq=1179.656 / hfq 更大,
        除权日之后三者相等(最新价不动) —— 符合前/后复权定义。

    ratio 精度: 保留 8 位小数。价格本身 2~3 位, 乘完再 round 到 2 位, 不会累积误差。
    """
    code = _norm_code(code)
    sym = _tx_symbol(code)
    mode = (mode or "qfq").strip().lower()
    if mode not in _ADJUST_MODES:
        raise ValueError(f"mode 必须是 {_ADJUST_MODES} 之一: {mode!r}")
    limit = max(60, min(int(limit or 640), _ADJ_MAX_LIMIT))

    if mode == "none":
        # 不复权: ratio 恒为 1, 不需要打腾讯 —— 但为了拿到名称/覆盖区间仍取一次 day
        url = _TENCENT_KLINE_URL + "?" + urllib.parse.urlencode({"param": f"{sym},day,,,{limit}"})
        key = "day"
    else:
        url = _TENCENT_FQKLINE_URL + "?" + urllib.parse.urlencode(
            {"param": f"{sym},day,,,{limit},{mode}"})
        key = f"{mode}day"

    # ⚠️ 缓存键必须带前缀: 上面这支 url 与下面取"不复权基准"的 url 是同一个
    # (kline/kline?param=sym,day,,,n), 而两处存的东西不一样(这里存结果 dict,
    # 那里存腾讯原始响应)。共用裸 url 会互相覆盖, 表现为 mode=none 返回
    # source_gate_rejected(拿到的是原始响应, 没有 source 字段)。
    cache_key = f"adj|{mode}|{url}"
    cached = _cache_get(cache_key)
    if isinstance(cached, dict):
        return cached

    payload = _request_json(url)
    node = ((payload or {}).get("data") or {}).get(sym)
    rows = (node or {}).get(key) if isinstance(node, dict) else None

    # ⚠️ 没有除权记录的股票(如新股 688825), 腾讯**不给 qfqday/hfqday 键**, 只回 day。
    # 这不是失败 —— 恰恰是最权威的"该股无需复权"信号: 三档数值本就相等。
    # 若在这里 raise, 个股页的复权档对这些票会整档 available:false(假的取不到)。
    no_adjustment_event = False
    if mode != "none" and (not isinstance(rows, list) or not rows):
        rows = (node or {}).get("day") if isinstance(node, dict) else None
        no_adjustment_event = True
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"腾讯未返回日K({mode}): {sym}")

    def _col(r: list, i: int) -> float | None:
        return _num(r[i]) if isinstance(r, (list, tuple)) and len(r) > i else None

    dates: list[str] = []
    closes: list[float] = []
    for r in rows:
        d = str(r[0]).strip() if isinstance(r, (list, tuple)) and r else ""
        c = _col(r, 2)  # 第 3 列 = 收盘
        if not d or c is None or c <= 0:
            continue
        dates.append(d[:10])
        closes.append(c)

    if not dates:
        raise ValueError(f"腾讯日K无法解析({mode}): {sym}")

    # 不复权时 ratios 全 1; 其余档位再取一次原始序列做逐日比值
    raw_closes = list(closes)
    if no_adjustment_event:
        raw_closes = list(closes)     # 腾讯只给了 day(本来就是不复权) → 比值恒 1
    elif mode != "none":
        raw_url = _TENCENT_KLINE_URL + "?" + urllib.parse.urlencode(
            {"param": f"{sym},day,,,{limit}"})
        raw_cache_key = f"raw|{raw_url}"
        raw_payload = _cache_get(raw_cache_key)
        if not isinstance(raw_payload, dict):
            raw_payload = _request_json(raw_url)
            _cache_put(raw_cache_key, raw_payload, _TENCENT_ADJ_TTL_S)
        raw_node = ((raw_payload or {}).get("data") or {}).get(sym)
        raw_rows = (raw_node or {}).get("day") if isinstance(raw_node, dict) else None
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError(f"腾讯未返回不复权日K(用于算复权系数): {sym}")
        raw_map: dict[str, float] = {}
        for r in raw_rows:
            if not isinstance(r, (list, tuple)) or len(r) < 3:
                continue
            c = _col(r, 2)
            if c is None or c <= 0:
                continue
            raw_map[str(r[0]).strip()[:10]] = c
        if not raw_map:
            raise ValueError(f"腾讯不复权日K无法解析: {sym}")
        # 只在两边都有的日期上出系数; 缺一边就不给(宁可该日不缩放, 也不猜)
        pairs = [(d, c, raw_map[d]) for d, c in zip(dates, closes, strict=False) if d in raw_map]
        if not pairs:
            raise ValueError(f"复权序列与不复权序列无重叠日期: {sym}")
        dates = [p[0] for p in pairs]
        closes = [p[1] for p in pairs]
        raw_closes = [p[2] for p in pairs]

    ratios = [
        {"date": d, "ratio": round(c / rc, 8)}
        for d, c, rc in zip(dates, closes, raw_closes, strict=True)
    ]
    # 是否真的发生过除权: 有任何一天 ratio 偏离 1 超过 1e-6 才算。
    # 前端据此判断"三档结果一致", 免得用户以为开关坏了。
    has_adjust = any(abs(r["ratio"] - 1.0) > 1e-6 for r in ratios)

    qt = (node.get("qt") or {}).get(sym) if isinstance(node, dict) else None
    result = {
        "source": SOURCE_TENCENT,
        "data": {
            "code": code,
            "symbol": sym,
            "name": qt[1] if isinstance(qt, list) and len(qt) > 1 else None,
            "mode": mode,
            "ratios": ratios,
            "has_adjust": has_adjust,
            "coverage": {"from": dates[0], "to": dates[-1], "bars": len(dates)},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    _cache_put(cache_key, result, _TENCENT_ADJ_TTL_S)
    return result


def minute_bars(code: str) -> dict:
    """A股**当日分时**(腾讯 web.ifzq.gtimg.cn) —— 分时线 / 均价线 / 分时量。

    九章个股页的「分时」tab 就靠这块。上一轮判断"我方没分钟库、做了就是空图",
    实测下来是错的: 这个端点是**当日全量**, 不需要任何本地分钟库, 且直连可达。

    ⚠️ 字段口径(实测 sh688825, 267 点, 2026-10-09):
        ``data.<sym>.data.data`` 每行 ``"HHMM 价格 累计成交量(股) 累计成交额(元)"``
        —— 第 3/4 列是**全天累计值**, 不是该分钟增量。直接拿去画量柱会得到一根
        单调爬升的阶梯(把累计量当分钟量), 这里按相邻差分还原每分钟量。
        均价线 = 累计成交额 / 累计成交量(元/股), 与通达信「均价」同口径。
        ``data.<sym>.qt.<sym>`` 是腾讯标准数组: [1]名称 [3]现价 **[4]昨收** [5]今开;
        昨收是分时图涨跌幅与上下对称轴的唯一基准, 取不到就不画图(fail-closed)。

    ⚠️ 时间轴**不补齐**: 腾讯给多少点画多少点, 缺的分钟(集合竞价/盘后档位差异)
       不插值、不补零 —— 补出来的点是编的。

    🔴 **成交量单位在 A股内部不统一**(实测 2026-10-09 收盘数据):
       主板 sh600000 / sh601318 / sh603288 / sz000001 / sz000651 第 3 列给**手**,
       科创板 sh688825 / sh688981 与 ETF sh512880 给**股** —— 同一个接口差 100 倍。
       按代码前缀硬编码(如"688 就一定如何")早晚踩空, 这里用数据自身的恒等式
       ``成交额 = 成交量 × 均价`` 反推单位(见 _volume_unit_factor), 且**均价必须
       落在当日 [low, high] 区间内** —— 这是可证伪的校验, 不是猜。
       输出统一为**股 / 元**。
    """
    code = _norm_code(code)
    sym = _tx_symbol(code)

    url = _TENCENT_MINUTE_URL + "?" + urllib.parse.urlencode({"code": sym})
    cached = _cache_get(url)
    if isinstance(cached, dict):
        return cached

    payload = _request_json(url)
    node = ((payload or {}).get("data") or {}).get(sym)
    if not isinstance(node, dict):
        raise ValueError(f"腾讯未返回分时节点: {sym}")

    rows = ((node.get("data") or {}).get("data") or [])
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"腾讯分时数据为空(未开盘或非交易日?): {sym}")

    qt = (node.get("qt") or {}).get(sym)
    prev_close = _num(qt[4]) if isinstance(qt, list) and len(qt) > 4 else None
    if prev_close is None:
        # qt 缺昨收 → 退回行情接口拿(它有独立缓存, 不会重复打)
        prev_close = realtime_quote(code)["data"].get("prev_close")
    if not prev_close or prev_close <= 0:
        raise ValueError(f"腾讯分时无昨收基准: {sym}")

    # ── 先按原始单位解析一遍, 拿到价格区间用于反推量的单位 ──
    raw_rows: list[tuple[str, float, float, float]] = []
    for raw in rows:
        parts = str(raw).split()
        if len(parts) < 4:
            continue
        price, cum_v, cum_a = _num(parts[1]), _num(parts[2]), _num(parts[3])
        if price is None or cum_v is None:
            continue
        raw_rows.append((parts[0], price, cum_v, cum_a or 0.0))
    if not raw_rows:
        raise ValueError(f"腾讯分时无法解析: {sym}")

    lo = min(r[1] for r in raw_rows)
    hi = max(r[1] for r in raw_rows)
    last_cum_v, last_cum_a = raw_rows[-1][2], raw_rows[-1][3]
    factor = _volume_unit_factor(last_cum_v, last_cum_a, lo, hi)
    if factor is None:
        # 量额自相矛盾(单位既不是股也不是手) → 不给量, 也不给靠它算出来的均价
        raise ValueError(f"腾讯分时量额单位无法判定: {sym} (额/量={last_cum_a / last_cum_v:.4f}, 价区 {lo}~{hi})")

    points: list[dict] = []
    prev_cum_v = 0.0
    prev_cum_a = 0.0
    for t, price, cum_v, cum_a in raw_rows:
        # 差分还原每分钟增量; 累计值回退(接口偶发乱序)时按 0 处理, 不造负数
        minute_v = max(cum_v - prev_cum_v, 0.0) * factor
        minute_a = max(cum_a - prev_cum_a, 0.0)
        prev_cum_v, prev_cum_a = cum_v, cum_a
        # 均价 = 累计成交额 / 累计成交量(股), 与通达信均价同口径
        avg = (cum_a / (cum_v * factor)) if cum_v > 0 else price
        points.append({
            "t": f"{t[:2]}:{t[2:4]}" if len(t) == 4 else t,
            "price": round(price, 3),
            "avg": round(avg, 3),
            "pct": round((price - prev_close) / prev_close * 100, 3),
            "volume": round(minute_v, 0),
            "amount": round(minute_a, 2),
        })

    prices = [p["price"] for p in points]
    last = points[-1]
    result = {
        "source": SOURCE_TENCENT,
        "data": {
            "code": code,
            "symbol": sym,
            "name": qt[1] if isinstance(qt, list) and len(qt) > 1 else None,
            "date": (node.get("data") or {}).get("date") or None,
            "prev_close": round(prev_close, 3),
            "open": _num(qt[5]) if isinstance(qt, list) and len(qt) > 5 else None,
            "last_price": last["price"],
            "last_pct": last["pct"],
            "high": round(max(prices), 3),
            "low": round(min(prices), 3),
            "volume": round(sum(p["volume"] for p in points), 0),
            "amount": round(sum(p["amount"] for p in points), 2),
            "points": points,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
    _cache_put(url, result, _TENCENT_MINUTE_TTL_S)
    return result


#: 东财 F10 报表号(与行情报表分开维护, 报表名错了东财会返回 success:false)。
_REPORT_F10_ORG = "RPT_F10_BASIC_ORGINFO"        # 公司基本资料 → 行业
_REPORT_F10_BOARD = "RPT_F10_CORETHEME_BOARDTYPE"  # 核心题材 → 所属板块


def _secucode(code: str) -> str:
    """'300308' → '300308.SZ'。东财 F10 报表按 SECUCODE 过滤, 不是纯 6 位代码。

    与腾讯符号(_tx_symbol)同理但后缀不同: 这里要 '.SH'/'.SZ'/'.BJ'。
    """
    c = _norm_code(code)
    if c[0] in ("4", "8"):
        return f"{c}.BJ"
    if c[0] in ("6", "9") or c.startswith("5"):
        return f"{c}.SH"
    return f"{c}.SZ"


def stock_sectors(code: str, limit: int = 24) -> dict:
    """个股所属板块 / 题材 —— 东财 F10, 全真源。

    返回 data:
      - ``industry``: 东财三级行业(EM2016) + 证监会行业(INDUSTRYCSRC1), 原样字符串
      - ``boards``:   题材列表, ``precise=True``(核心题材)排前, **带东财给的入选理由**

    🔴 与九章同名模块的三点差别(也就是"比它更好"的地方):
      1. 九章的行业靠 akshare ``stock_individual_info_em`` —— 该函数底层走
         ``push2.eastmoney.com``, 在我方网络被墙(实测 ProxyError)。它在此处
         会静默 except 掉, 于是个股页可能一个行业都不显示。我方走东财 F10
         同域名的 datacenter 报表, 实测可达。
      2. 九章的题材来自同花顺强势股 tags, 拿不到时退回一份**硬编码兜底 map**
         (``_DEMO_SECTORS_MAP``)—— 没有数据时依然能显示出板块名。我方不写死
         映射表, 拿不到就是拿不到。
      3. 我方多给一层: 东财 ``SELECTED_BOARD_REASON``(为什么归进这个板块),
         例 "2025年半年报显示, 公司主营业务为高端光通信收发模块"。
         九章只有板块名, 没有任何归因依据。
    """
    code = _norm_code(code)
    filt = f'(SECUCODE="{_secucode(code)}")'

    industry: dict[str, object] = {}
    name: str | None = None
    org_rows = _datacenter(_REPORT_F10_ORG, filt, page_size=1)
    if org_rows:
        row = org_rows[0]
        name = str(row.get("SECURITY_NAME_ABBR") or "").strip() or None
        em = str(row.get("EM2016") or "").strip()
        csrc = str(row.get("INDUSTRYCSRC1") or "").strip()
        if em:
            industry["em"] = em
            industry["em_parts"] = [p for p in em.split("-") if p]
        if csrc:
            industry["csrc"] = csrc
            industry["csrc_parts"] = [p for p in csrc.split("-") if p]

    # 排序只让东财按 BOARD_RANK 升序(它认这一列); IS_PRECISE 的多列排序实测
    # 不生效(sortTypes 按整串解析), 核心题材优先在下面本地排 —— 结果可控。
    boards: list[dict] = []
    for row in _datacenter(_REPORT_F10_BOARD, filt, page_size=50,
                           sort_columns="BOARD_RANK", sort_types="1"):
        bname = str(row.get("BOARD_NAME") or "").strip()
        if not bname:
            continue
        if not name:
            name = str(row.get("SECURITY_NAME_ABBR") or "").strip() or None
        boards.append({
            "name": bname,
            "board_code": (str(row.get("NEW_BOARD_CODE") or row.get("BOARD_CODE")
                               or "").strip() or None),
            # '1'=东财认定为核心题材(精确匹配), 其余是宽泛/衍生归类
            "precise": str(row.get("IS_PRECISE") or "0") == "1",
            "rank": _num(row.get("BOARD_RANK")),
            "reason": str(row.get("SELECTED_BOARD_REASON") or "").strip() or None,
        })

    if not industry and not boards:
        # 两个报表都没数据 —— 这是"取不到", 不是"该股没有板块"。上抛让上游
        # 走 available:false, 绝不返回空壳冒充成功。
        raise ValueError(f"东财 F10 未返回该股板块数据: {code}")

    # 核心题材(IS_PRECISE=1)排前, 同级按东财 BOARD_RANK 升序。
    boards.sort(key=lambda b: (not b["precise"], b["rank"] if b["rank"] is not None else 9999))

    return {
        "source": SOURCE_EM_F10,
        "data": {
            "code": code,
            "name": name,
            "industry": industry or None,
            "boards": boards[:max(1, min(int(limit), 50))],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def warmup() -> None:
    """预热/自测钩子: 打一次最轻的 datacenter 查询, 失败即上抛。"""
    _datacenter("RPT_DAILYBILLBOARD_DETAILSNEW", page_size=1,
                sort_columns="TRADE_DATE", sort_types="-1")
