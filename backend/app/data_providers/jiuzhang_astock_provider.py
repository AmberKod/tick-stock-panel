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
