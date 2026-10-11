"""A股龙虎榜 (九章融合 P2-2 试点) 护栏测试。

核心守护三条:
1. **机构金额不得重复计** —— 东财买入/卖出明细是同一批席位的两视图, NET
   字段值相同。早期参考实现在两个视图各累加一次导致翻倍, 本测试用实测
   数据(002317 众生药业 2026-10-09)锁死单视图口径。
2. **fail-closed** —— 上游异常必须上抛, 绝不允许返回 ``{"source":"fail",
   "records": []}`` 空壳伪装成功。
3. **「未上榜」是真实答案** —— records 为空时 available 仍为 true, 不得被
   误判成 no_data(那样等于把"这只票干净"谎报成"取不到数")。

全部用例 mock 掉网络, 不依赖外网。

后续追加研报/公告两块时另外锁死两条参考实现的坑:
4. **巨潮 orgId 必须查不能拼** —— 参考实现用 ``gssz0{code}`` 拼, 实测真实
   orgId 是 ``9900022016`` 这类与代码无关的编号, 拼出来查询恒返回 0 条。
5. **缺失的数值字段填 None 不填 0** —— 0 会被前端读成"预测 EPS 为 0"这种假事实。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.api import astock_deep as api
from app.data_providers import jiuzhang_astock_provider as prov
from app.main import app

# 实测数据(2026-10-10 取自东财 datacenter, 002317 众生药业 2026-10-09 上榜)。
# BUY/SELL 两视图里同一席位的 NET 完全相同 —— 这正是重复计数的陷阱。
_BUY_ROWS = [
    {"OPERATEDEPT_NAME": "深股通专用", "OPERATEDEPT_CODE": "10634757",
     "BUY": 138253378.39, "SELL": 56668414.61, "NET": 81584963.78},
    {"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0",
     "BUY": 48651674, "SELL": 51569335, "NET": -2917661},
    {"OPERATEDEPT_NAME": "国泰海通证券股份有限公司上海长宁区江苏路证券营业部",
     "OPERATEDEPT_CODE": "10025080",
     "BUY": 37622758, "SELL": 2833000, "NET": 34789748},
    {"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0",
     "BUY": 37404600.28, "SELL": 45113924, "NET": -7709323.72},
]

# 卖出视图: 同一批席位, NET 与买入视图一致(东财就是这样返回的)
_SELL_ROWS = [
    {"OPERATEDEPT_NAME": "深股通专用", "OPERATEDEPT_CODE": "10634757",
     "BUY": 138253378.39, "SELL": 56668414.61, "NET": 81584963.78},
    {"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0",
     "BUY": 48651674, "SELL": 51569335, "NET": -2917661},
    {"OPERATEDEPT_NAME": "机构专用", "OPERATEDEPT_CODE": "0",
     "BUY": 37404600.28, "SELL": 45113924, "NET": -7709323.72},
]

_RECORD_ROWS = [
    {"TRADE_DATE": "2026-10-09 00:00:00", "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
     "BILLBOARD_NET_AMT": 57675159.7, "TURNOVERRATE": 6.4341,
     "CLOSE_PRICE": 25.49, "CHANGE_RATE": 10.0129},
]


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """provider 用模块级 _CACHE, 跨用例/跨测试文件都会残留 —— 每例前后清空。

    不清的话: 上一个用例把 mock 结果写进缓存, 下一个用例直接命中缓存,
    连 _request_json 都不会被调用, 断言会被"假通过"。
    """
    prov._CACHE.clear()
    yield
    prov._CACHE.clear()


def _patch_datacenter(monkeypatch, records=None, buy=None, sell=None):
    """替换 _datacenter, 返回调用记录(断言用了哪些报表)。"""
    calls: list[str] = []

    def fake(report_name, filter_str="", page_size=50, sort_columns="", sort_types="-1"):
        calls.append(report_name)
        if report_name == "RPT_DAILYBILLBOARD_DETAILSNEW":
            return records if records is not None else []
        if report_name == "RPT_BILLBOARD_DAILYDETAILSBUY":
            return buy if buy is not None else []
        if report_name == "RPT_BILLBOARD_DAILYDETAILSSELL":
            return sell if sell is not None else []
        return []

    monkeypatch.setattr(prov, "_datacenter", fake)
    return calls


# ---------------------------------------------------------------- 1. 机构不重复计

def test_institution_amount_not_double_counted(monkeypatch):
    """机构净额只从买入视图统计一次; 两边都加会是 -2125.4(翻倍)。"""
    _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    data = prov.dragon_tiger("002317")["data"]

    inst = data["institution"]
    assert inst["seat_count"] == 2, "买入视图里应有 2 个机构专用席位"
    # 单视图: (-2917661 + -7709323.72)/1e4 = -1062.7
    assert inst["net_amt"] == pytest.approx(-1062.7, abs=0.1)
    # 若误按两视图累加会得到约 -2125.4 —— 显式排除
    assert abs(inst["net_amt"] - (-2125.4)) > 100, "机构净额被重复计入(两视图各加了一次)"


def test_institution_buy_sell_amounts(monkeypatch):
    _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    inst = prov.dragon_tiger("002317")["data"]["institution"]
    # (48651674 + 37404600.28) 元 = 8605.7 万元
    assert inst["buy_amt"] == pytest.approx(8605.7, abs=0.1)
    # (51569335 + 45113924) 元 = 9668.3 万元
    assert inst["sell_amt"] == pytest.approx(9668.3, abs=0.1)


def test_seat_flags_and_units(monkeypatch):
    """席位标记(机构/北向)与元→万元换算。"""
    _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    buy = prov.dragon_tiger("002317")["data"]["seats"]["buy"]

    assert len(buy) == 4
    assert buy[0]["is_northbound"] is True and buy[0]["is_institution"] is False
    assert buy[1]["is_institution"] is True
    assert buy[2]["is_institution"] is False and buy[2]["is_northbound"] is False
    # 元 → 万元, 1 位小数
    assert buy[0]["buy_amt"] == pytest.approx(13825.3, abs=0.1)
    assert buy[0]["net"] == pytest.approx(8158.5, abs=0.1)


def test_northbound_detected(monkeypatch):
    _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    nb = prov.dragon_tiger("002317")["data"]["northbound"]
    assert nb["present"] is True
    assert nb["net_amt"] == pytest.approx(8158.5, abs=0.1)


def test_report_names_used(monkeypatch):
    """确认查的是三张正确的东财报表。"""
    calls = _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    prov.dragon_tiger("002317")
    assert calls[0] == "RPT_DAILYBILLBOARD_DETAILSNEW"
    assert "RPT_BILLBOARD_DAILYDETAILSBUY" in calls
    assert "RPT_BILLBOARD_DAILYDETAILSSELL" in calls


# ---------------------------------------------------------------- 2. fail-closed

def test_upstream_error_propagates(monkeypatch):
    """上游炸了必须上抛, 不许返回空壳装成功。"""
    def boom(*a, **k):
        raise TimeoutError("datacenter timeout")

    monkeypatch.setattr(prov, "_datacenter", boom)
    with pytest.raises(TimeoutError):
        prov.dragon_tiger("002317")


def test_empty_code_rejected(monkeypatch):
    monkeypatch.setattr(prov, "_datacenter", lambda *a, **k: [])
    with pytest.raises(ValueError):
        prov.dragon_tiger("")


def test_source_always_live(monkeypatch):
    """哪怕没上榜, source 也必须是白名单内的真实标记。"""
    monkeypatch.setattr(prov, "_datacenter", lambda *a, **k: [])
    payload = prov.dragon_tiger("600519")
    assert payload["source"] == "live-eastmoney"
    from app.services.source_gate import is_real
    assert is_real(payload) is True


# ---------------------------------------------------------------- 3. API 契约

@pytest.fixture()
def client():
    # 必须声明本机地址, 否则被「未设密码 + 非内网」守卫判 403
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        yield c


def test_api_not_on_board_is_available_true(client, monkeypatch):
    """未上榜是真实结论: available=true + records 为空, 不是 no_data。"""
    monkeypatch.setattr(prov, "_datacenter", lambda *a, **k: [])
    r = client.get("/api/astock/dragon/600519")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True, "未上榜被误判成数据不可用"
    assert body["source"] == "live-eastmoney"
    assert body["data"]["records"] == []


def test_api_available_true_with_data(client, monkeypatch):
    _patch_datacenter(monkeypatch, records=_RECORD_ROWS, buy=_BUY_ROWS, sell=_SELL_ROWS)
    r = client.get("/api/astock/dragon/002317")
    body = r.json()
    assert r.status_code == 200 and body["available"] is True
    assert len(body["data"]["records"]) == 1
    assert body["data"]["records"][0]["reason"] == "日涨幅偏离值达到7%的前5只证券"


def test_api_provider_error_is_fail_closed(client, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("boom")
    monkeypatch.setattr(prov, "_datacenter", boom)
    r = client.get("/api/astock/dragon/002317")
    body = r.json()
    assert r.status_code == 200, "不可用也必须是 HTTP 200(fail-closed 契约)"
    assert body["available"] is False
    assert "provider_error" in body["reason"]


def test_api_bad_code(client):
    body = client.get("/api/astock/dragon/abc").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_bad_days(client):
    body = client.get("/api/astock/dragon/002317?days=999").json()
    assert body["available"] is False and "bad_days" in body["reason"]


def test_api_code_suffix_normalized(monkeypatch):
    """002317.SZ / sz002317 都能归一到 002317。"""
    seen = {}

    def fake(report_name, filter_str="", **k):
        seen["filter"] = filter_str
        return _RECORD_ROWS if report_name == "RPT_DAILYBILLBOARD_DETAILSNEW" else []

    monkeypatch.setattr(prov, "_datacenter", fake)
    prov.dragon_tiger("002317.SZ")
    assert 'SECURITY_CODE="002317"' in seen["filter"]


def test_norm_code_helper():
    assert api._norm_code("002317") == "002317"
    assert api._norm_code("sz002317") == "002317"
    assert api._norm_code("002317.SZ") == "002317"
    assert api._norm_code("abc") is None


# ================================================================ 研报 (东财 reportapi)

# 实测字段(2026-10-10, 300308 中际旭创)。数值一律是字符串。
_RESEARCH_ROW = {
    "publishDate": "2026-09-23 00:00:00.000",
    "orgSName": "群益证券",
    "title": "NPO技术快速发展，预计将成为未来数年主要增长引擎",
    "emRatingName": "增持",
    "lastEmRatingName": "买入",
    "ratingChange": "调低",
    "indvAimPriceT": "1200.0000000000",
    "predictThisYearEps": "30.1300000000",
    "predictNextYearEps": "56.1700000000",
    "infoCode": "AP202609231829795170",
}


def _patch_report(monkeypatch, rows, total_page=1):
    """替换 _request_json, 只接东财 reportapi; 记录被请求的 page。"""
    seen: list[str] = []

    def fake(url, data=None, headers=None, timeout=None):
        assert url.startswith(prov._REPORT_URL), f"请求了非预期地址: {url}"
        page = re.search(r"pageNo=(\d+)", url).group(1)
        seen.append(page)
        return {"data": rows if page == "1" else [], "TotalPage": total_page}

    monkeypatch.setattr(prov, "_request_json", fake)
    return seen


def test_research_fields_mapped(monkeypatch):
    """日期/机构/评级/目标价/EPS/原文链接 都从实测字段正确映射。"""
    _patch_report(monkeypatch, [_RESEARCH_ROW])
    d = prov.research_reports("300308")["data"]

    assert d["code"] == "300308"
    r = d["reports"][0]
    assert r["date"] == "2026-09-23"
    assert r["org"] == "群益证券"
    assert r["rating"] == "增持" and r["rating_prev"] == "买入"
    assert r["rating_change"] == "调低"
    assert r["target_price"] == pytest.approx(1200.0)
    assert r["eps_this_year"] == pytest.approx(30.13)
    assert r["eps_next_year"] == pytest.approx(56.17)
    assert r["url"].endswith("AP202609231829795170.html")


def test_research_missing_number_is_none_not_zero(monkeypatch):
    """预测 EPS 缺失(东财给 '-')必须 None, 不能填 0 —— 0 是假事实。"""
    row = dict(_RESEARCH_ROW, predictThisYearEps="-", indvAimPriceT=None)
    _patch_report(monkeypatch, [row])
    r = prov.research_reports("300308")["data"]["reports"][0]
    assert r["eps_this_year"] is None
    assert r["target_price"] is None


def test_research_pagination_stops_at_total_page(monkeypatch):
    """TotalPage=1 时只打第一页, 不空转 3 次。"""
    seen = _patch_report(monkeypatch, [_RESEARCH_ROW], total_page=1)
    prov.research_reports("300308")
    assert seen == ["1"]


def test_research_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_report(monkeypatch, [])
    payload = prov.research_reports("600519")
    assert payload["source"] == "live-eastmoney"
    assert is_real(payload) is True
    # 没有机构覆盖是真实现象, 不是取不到数
    assert payload["data"]["reports"] == []


def test_api_research_ok(client, monkeypatch):
    _patch_report(monkeypatch, [_RESEARCH_ROW])
    body = client.get("/api/astock/research/300308").json()
    assert body["available"] is True and body["source"] == "live-eastmoney"
    assert len(body["data"]["reports"]) == 1


def test_api_research_bad_limit(client):
    body = client.get("/api/astock/research/300308?limit=999").json()
    assert body["available"] is False and "bad_limit" in body["reason"]


def test_api_research_fail_closed(client, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("reportapi timeout")
    monkeypatch.setattr(prov, "_request_json", boom)
    r = client.get("/api/astock/research/300308")
    assert r.status_code == 200 and r.json()["available"] is False


# ================================================================ 公告 (巨潮 cninfo)

# 实测: announcementTypeName 为 None, 要退回 announcementType;
# isHLtitle=true 时标题里会插 <em> 高亮标签。
_ANN_ROW = {
    "announcementTime": 1791417269000,
    "announcementTitle": "关于<em>控股股东</em>部分股票解除质押的公告",
    "announcementTypeName": None,
    "announcementType": "01010503||010112||010115",
    "adjunctUrl": "finalpage/2026-10-08/1234567890.PDF",
}
_CNINFO_ORG_ID = "9900022016"  # 300308 中际旭创的真实 orgId(实测)


def _patch_cninfo(monkeypatch, anns=None, org_id=_CNINFO_ORG_ID):
    """替换 _request_json, 按 URL 分发 topSearch / hisAnnouncement。"""
    sent: dict[str, object] = {}

    def fake(url, data=None, headers=None, timeout=None):
        if url == prov._CNINFO_SEARCH_URL:
            sent["search_body"] = (data or b"").decode("utf-8")
            return [{"code": "300308", "orgId": org_id, "zwjc": "中际旭创"}]
        if url == prov._CNINFO_ANN_URL:
            sent["ann_body"] = (data or b"").decode("utf-8")
            return {"announcements": anns if anns is not None else [_ANN_ROW],
                    "totalRecordNum": 1}
        raise AssertionError(f"请求了非预期地址: {url}")

    monkeypatch.setattr(prov, "_request_json", fake)
    return sent


def test_announcements_orgid_comes_from_topsearch(monkeypatch):
    """orgId 必须走 topSearch 查出来, 不能由代码拼。"""
    sent = _patch_cninfo(monkeypatch)
    prov.announcements("300308")
    assert "keyWord=300308" in sent["search_body"]
    assert f"stock=300308%2C{_CNINFO_ORG_ID}" in sent["ann_body"], "公告请求没带上真 orgId"


def test_announcements_orgid_never_concatenated(monkeypatch):
    """显式排除参考实现的拼法(gssz0{code}) —— 那样查出来恒 0 条。"""
    sent = _patch_cninfo(monkeypatch)
    prov.announcements("300308")
    assert "gssz0" not in sent["ann_body"], "orgId 被拼成 gssz0+code, 巨潮会返回 0 条"


def test_announcements_title_em_tag_stripped(monkeypatch):
    """isHLtitle=true 会在关键词外包 <em>, 不剥掉前端会显示标签。"""
    _patch_cninfo(monkeypatch)
    a = prov.announcements("300308")["data"]["announcements"][0]
    assert a["title"] == "关于控股股东部分股票解除质押的公告"
    assert "<em>" not in a["title"]


def test_announcements_type_code_not_shown_as_name(monkeypatch):
    """announcementType 是分类编码, 不能当类型名展示(前端会出现一串竖线数字)。

    实测 300308: announcementTypeName=None, announcementType='01010503||010112...'。
    """
    _patch_cninfo(monkeypatch)
    a = prov.announcements("300308")["data"]["announcements"][0]
    assert a["type"] == "", "把编码当类型名展示了"
    # 编码另存, 供后续做映射表
    assert a["type_codes"] == "01010503||010112||010115"


def test_announcements_readable_type_name_used(monkeypatch):
    """接口给了可读名时要用它。"""
    _patch_cninfo(monkeypatch, anns=[dict(_ANN_ROW, announcementTypeName="股东增减持")])
    a = prov.announcements("300308")["data"]["announcements"][0]
    assert a["type"] == "股东增减持"


def test_announcements_url_is_absolute(monkeypatch):
    """adjunctUrl 是相对路径, 必须拼 static 前缀, 否则前端跳不过去。"""
    _patch_cninfo(monkeypatch)
    a = prov.announcements("300308")["data"]["announcements"][0]
    assert a["url"] == "https://static.cninfo.com.cn/finalpage/2026-10-08/1234567890.PDF"


def test_announcements_date_parsed(monkeypatch):
    _patch_cninfo(monkeypatch)
    a = prov.announcements("300308")["data"]["announcements"][0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["date"]), a["date"]


def test_announcements_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_cninfo(monkeypatch)
    payload = prov.announcements("300308")
    assert payload["source"] == "live-cninfo"
    assert is_real(payload) is True, "live-cninfo 未登记进 source_gate 白名单"


def test_announcements_orgid_not_found_raises(monkeypatch):
    """topSearch 查不到 orgId 必须上抛(拿不到就说拿不到)。"""

    def fake(url, data=None, headers=None, timeout=None):
        if url == prov._CNINFO_SEARCH_URL:
            return [{"code": "999999", "orgId": "x"}]  # 代码对不上
        raise AssertionError("不该走到公告查询")

    monkeypatch.setattr(prov, "_request_json", fake)
    with pytest.raises(ValueError):
        prov.announcements("300308")


def test_api_announcements_ok(client, monkeypatch):
    _patch_cninfo(monkeypatch)
    body = client.get("/api/astock/announcements/300308").json()
    assert body["available"] is True and body["source"] == "live-cninfo"
    assert len(body["data"]["announcements"]) == 1


def test_api_announcements_bad_code(client):
    body = client.get("/api/astock/announcements/abc").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_announcements_empty_is_available(client, monkeypatch):
    """窗口内无公告是真实现象, 不是 no_data。"""
    _patch_cninfo(monkeypatch, anns=[])
    body = client.get("/api/astock/announcements/300308").json()
    assert body["available"] is True
    assert body["data"]["announcements"] == []


# ================================================================ 资金流 (新浪)

# 实测(2026-10-10, 300308)。新浪涨跌幅是小数(-0.00984674 = -0.98%)。
_SINA_ROW = {
    "opendate": "2026-10-09", "trade": "775.2900", "changeratio": "-0.00984674",
    "turnover": "251.669", "netamount": "661928256.2800", "r0_net": "637170960.7000",
}


def _patch_sina(monkeypatch, rows=None):
    sent: dict[str, object] = {}

    def fake(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        return rows if rows is not None else [_SINA_ROW]

    monkeypatch.setattr(prov, "_request_json", fake)
    return sent


def test_fundflow_uses_sina_not_eastmoney(monkeypatch):
    """东财 push2/push2his 在我方被墙(连接级 RST), 必须走新浪。"""
    sent = _patch_sina(monkeypatch)
    prov.fund_flow("300308")
    assert "sina" in sent["url"], sent["url"]
    assert "eastmoney" not in sent["url"], "又在打被墙的东财 push2"


def test_fundflow_market_prefix(monkeypatch):
    """6 开头沪市, 8/4 北交所, 其余深市。"""
    sent = _patch_sina(monkeypatch)
    prov.fund_flow("600519")
    assert "daima=sh600519" in sent["url"], sent["url"]
    prov.fund_flow("000001")
    assert "daima=sz000001" in sent["url"]


def test_fundflow_units_and_pct(monkeypatch):
    """金额元→万元; 涨跌幅小数→百分数。"""
    _patch_sina(monkeypatch)
    r = prov.fund_flow("300308", days=1)["data"]["rows"][0]
    assert r["close"] == pytest.approx(775.29)
    assert r["change_pct"] == pytest.approx(-0.98, abs=0.01)
    assert r["net_amount"] == pytest.approx(66192.8, abs=0.1)
    assert r["super_large_net"] == pytest.approx(63717.1, abs=0.1)


def test_fundflow_empty_raises(monkeypatch):
    _patch_sina(monkeypatch, rows=[])
    with pytest.raises(ValueError):
        prov.fund_flow("300308")


def test_fundflow_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_sina(monkeypatch)
    payload = prov.fund_flow("300308")
    assert payload["source"] == "sina"
    assert is_real(payload) is True, "sina 未登记进 source_gate 白名单"


def test_api_fundflow_bad_days(client):
    body = client.get("/api/astock/fundflow/300308?days=999").json()
    assert body["available"] is False and "bad_days" in body["reason"]


def test_api_fundflow_ok(client, monkeypatch):
    _patch_sina(monkeypatch)
    body = client.get("/api/astock/fundflow/300308?days=5").json()
    assert body["available"] is True and len(body["data"]["rows"]) == 1


# ================================================================ 财务摘要 (akshare)

class _FakeAk:
    """伪造 akshare: 返回 pandas DataFrame(真实 akshare 也是 pandas)。"""

    @staticmethod
    def stock_financial_abstract(symbol):
        import pandas as pd
        return pd.DataFrame([
            {"选项": "常用指标", "指标": "归母净利润",
             "20260630": 13651149693.27, "20260331": 5734501526.83, "20251231": None},
            {"选项": "盈利能力", "指标": "净资产收益率(ROE)",
             "20260630": 12.3, "20260331": 5.1, "20251231": 8.8},
        ])


def _patch_akshare(monkeypatch, fake=None):
    import sys
    monkeypatch.setitem(sys.modules, "akshare", fake or _FakeAk)


def test_financials_periods_newest_first(monkeypatch):
    """报告期必须从新到旧 —— 前端横向展示时最新期在最左。"""
    _patch_akshare(monkeypatch)
    d = prov.financials("300308")["data"]
    assert d["periods"] == ["20260630", "20260331", "20251231"]


def test_financials_groups_and_names(monkeypatch):
    _patch_akshare(monkeypatch)
    items = prov.financials("300308")["data"]["indicators"]
    assert [i["name"] for i in items] == ["归母净利润", "净资产收益率(ROE)"]
    assert items[0]["group"] == "常用指标"
    assert items[1]["group"] == "盈利能力"


def test_financials_missing_value_is_none(monkeypatch):
    """缺失期留 None, 不填 0 —— 0 会被读成"该期净利润为 0"。"""
    _patch_akshare(monkeypatch)
    items = prov.financials("300308")["data"]["indicators"]
    assert items[0]["values"][-1]["value"] is None
    assert items[0]["values"][0]["value"] == pytest.approx(13651149693.27)


def test_financials_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_akshare(monkeypatch)
    payload = prov.financials("300308")
    assert payload["source"] == "akshare"
    assert is_real(payload) is True, "akshare 未登记进 source_gate 白名单"


def test_financials_empty_raises(monkeypatch):
    class _Empty:
        @staticmethod
        def stock_financial_abstract(symbol):
            import pandas as pd
            return pd.DataFrame()
    _patch_akshare(monkeypatch, _Empty)
    with pytest.raises(ValueError):
        prov.financials("300308")


def test_api_financials_ok(client, monkeypatch):
    _patch_akshare(monkeypatch)
    body = client.get("/api/astock/financials/300308?periods=4").json()
    assert body["available"] is True and body["source"] == "akshare"
    assert len(body["data"]["indicators"]) == 2


def test_api_financials_bad_periods(client):
    body = client.get("/api/astock/financials/300308?periods=99").json()
    assert body["available"] is False and "bad_periods" in body["reason"]


# ---------------------------------------------------------------- 6. 实时行情(腾讯)

# 实测 sz300308 中际旭创 2026-10-09 收盘的原始字段(GBK 解码后, 按 ~ 切分)。
# 顺序与 provider 注释里的下标口径一一对应。
_TX_ROW = (
    "51~中际旭创~300308~775.29~783.00~775.64~279337~139069~140268~775.29~65~775.28~9~"
    "775.27~1~775.25~2~775.24~2~775.30~10~775.49~1~775.50~11~775.58~1~775.60~9~~"
    "20261009161454~-7.71~-0.98~780.00~730.00~775.29/279337/21091586583~279337~2109159~"
    "2.52~44.65~~780.00~730.00~6.39~8605.23~9132.22~22.91~939.60~626.40~1.30~47~755.06~"
    "33.45~84.58~~~2.30~2109158.6583~387.6450~50~ A A~GP-A-CYB~27.31~-13.46~0.17~51.31~"
    "32.01~1416.88~336.00~-13.47~-4.64~-35.12~1109937003~1177909641~42.34~25.33~"
    "1109937003~~~95.09~0.27~~CNY~0~~775.20~108~"
)


def _patch_tencent(monkeypatch, body=None):
    sent: dict[str, object] = {}

    def fake(url, encoding="utf-8", headers=None, timeout=None):
        sent["url"] = url
        sent["encoding"] = encoding
        # 回包里的 key 必须与请求符号一致, 否则 provider 的正则匹配不到
        sym = url.rsplit("=", 1)[-1]
        return f'v_{sym}="{body if body is not None else _TX_ROW}";'

    monkeypatch.setattr(prov, "_request_text", fake)
    return sent


def test_quote_uses_tencent_and_gbk(monkeypatch):
    """走腾讯(东财 push2 被墙); 腾讯返回 GBK, 解码错了中文名会乱码。"""
    sent = _patch_tencent(monkeypatch)
    prov.realtime_quote("300308")
    assert "qt.gtimg.cn" in sent["url"], sent["url"]
    assert sent["encoding"] == "gbk"


def test_quote_market_prefix(monkeypatch):
    """6/9/5 开头沪市, 4/8 北交所, 其余深市。"""
    sent = _patch_tencent(monkeypatch)
    prov.realtime_quote("600519")
    assert sent["url"].endswith("=sh600519"), sent["url"]


def test_quote_fields_and_units(monkeypatch):
    """金额统一折成元; 比例是百分数; 成交量手→股。"""
    _patch_tencent(monkeypatch)
    d = prov.realtime_quote("300308")["data"]

    assert d["name"] == "中际旭创"
    assert d["price"] == pytest.approx(775.29)
    assert d["prev_close"] == pytest.approx(783.00)
    assert d["change"] == pytest.approx(-7.71)
    assert d["change_pct"] == pytest.approx(-0.98)
    # 37 是万元 → 元
    assert d["amount"] == pytest.approx(21091590000.0, rel=1e-3)
    assert d["turnover_pct"] == pytest.approx(2.52)
    assert d["amplitude_pct"] == pytest.approx(6.39)
    assert d["pe_ttm"] == pytest.approx(44.65)
    assert d["pb"] == pytest.approx(22.91)
    # 6 是手 → 股
    assert d["volume"] == pytest.approx(27933700)
    assert d["limit_up"] == pytest.approx(939.60)
    assert d["limit_down"] == pytest.approx(626.40)


def test_quote_cap_is_not_swapped(monkeypatch):
    """🔴 九章 tx_quote 把 44/45 写反了: 44=流通市值, 45=总市值。

    总市值必须 >= 流通市值。若照抄参考实现(total=p[44], float=p[45])会得到
    总市值 8605 亿 < 流通市值 9132 亿 —— 一眼假。这里锁死正确口径。
    """
    _patch_tencent(monkeypatch)
    d = prov.realtime_quote("300308")["data"]

    # 44=8605.23亿(流通) 45=9132.22亿(总)
    assert d["float_cap"] == pytest.approx(860523000000.0, rel=1e-3)
    assert d["total_cap"] == pytest.approx(913222000000.0, rel=1e-3)
    assert d["total_cap"] > d["float_cap"], "总市值/流通市值写反了(照抄了九章的 bug)"
    # 显式排除被写反的取值
    assert d["total_cap"] != pytest.approx(860523000000.0, rel=1e-3)


def test_quote_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_tencent(monkeypatch)
    payload = prov.realtime_quote("300308")
    assert payload["source"] == "live-tencent"
    assert is_real(payload) is True, "live-tencent 未登记进 source_gate 白名单"


def test_quote_missing_payload_raises(monkeypatch):
    """腾讯没返回该符号 / 字段不足 → 上抛, 绝不返回空壳。"""
    _patch_tencent(monkeypatch, body="")
    with pytest.raises(ValueError):
        prov.realtime_quote("300308")
    _patch_tencent(monkeypatch, body="~".join(["x"] * 10))
    with pytest.raises(ValueError):
        prov.realtime_quote("300308")


def test_api_quote_bad_code(client):
    body = client.get("/api/astock/quote/AAOI").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_quote_ok(client, monkeypatch):
    _patch_tencent(monkeypatch)
    body = client.get("/api/astock/quote/300308").json()
    assert body["available"] is True and body["source"] == "live-tencent"
    assert body["data"]["name"] == "中际旭创"


# ---------------------------------------------------------------- 6.5 当日分时(腾讯)

# 实测 sh688825 2026-10-09 前三分钟(裁剪)。第 3/4 列是**全天累计**量/额。
_MINUTE_ROWS = [
    "0930 49.81 2423975 120738195.00",
    "0931 49.65 9495269 472181035.00",
    "0932 49.45 13666769 679025731.00",
]
# 腾讯标准数组: [1]名称 [3]现价 [4]昨收 [5]今开
_MINUTE_QT = ["1", "长鑫科技", "688825", "51.00", "50.52", "49.81"]


def _patch_minute(monkeypatch, rows=None, qt=None, empty=False):
    sent: dict[str, object] = {}

    def fake_json(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        sym = url.rsplit("=", 1)[-1]
        if empty:
            return {"code": 0, "data": {sym: {"data": {"data": [], "date": "20261009"},
                                              "qt": {sym: qt if qt is not None else _MINUTE_QT}}}}
        return {
            "code": 0,
            "data": {
                sym: {
                    "data": {"data": rows if rows is not None else _MINUTE_ROWS,
                             "date": "20261009"},
                    "qt": {sym: qt if qt is not None else _MINUTE_QT},
                }
            },
        }

    monkeypatch.setattr(prov, "_request_json", fake_json)
    return sent


def test_minute_uses_tencent_minute_endpoint(monkeypatch):
    """当日分时走 web.ifzq.gtimg.cn —— 当日全量, 不需要本地分钟库。"""
    sent = _patch_minute(monkeypatch)
    prov.minute_bars("688825")
    assert "web.ifzq.gtimg.cn" in sent["url"], sent["url"]
    assert "code=sh688825" in sent["url"], sent["url"]


def test_minute_market_prefix(monkeypatch):
    sent = _patch_minute(monkeypatch)
    prov.minute_bars("000001")
    assert sent["url"].endswith("=sz000001"), sent["url"]


def test_minute_volume_is_differential_not_cumulative(monkeypatch):
    """🔴 腾讯第 3/4 列是累计量额, 不是每分钟增量。

    照抄(直接用累计值当分钟量)会得到单调爬升的量柱 —— 图上看着有数据,
    实际每分钟的成交分布全是错的。这里锁死差分口径。
    """
    _patch_minute(monkeypatch)
    pts = prov.minute_bars("688825")["data"]["points"]

    assert pts[0]["volume"] == pytest.approx(2423975)
    assert pts[1]["volume"] == pytest.approx(9495269 - 2423975)
    assert pts[2]["volume"] == pytest.approx(13666769 - 9495269)
    # 差分之和 == 全天累计量(不差分的话会变成 3 倍累计, 一眼假)
    d = prov.minute_bars("688825")["data"]
    assert d["volume"] == pytest.approx(13666769)
    assert pts[0]["amount"] == pytest.approx(120738195.0)
    assert d["amount"] == pytest.approx(679025731.0)


def test_minute_avg_is_cum_amount_over_cum_volume(monkeypatch):
    """均价线 = 累计成交额 / 累计成交量(元/股), 与通达信均价同口径。"""
    _patch_minute(monkeypatch)
    pts = prov.minute_bars("688825")["data"]["points"]

    # provider 按 3 位小数落库(价格精度), 断言给到 1e-2 容差
    assert pts[0]["avg"] == pytest.approx(120738195.0 / 2423975, abs=1e-2)
    assert pts[2]["avg"] == pytest.approx(679025731.0 / 13666769, abs=1e-2)
    # 均价必须落在当日高低价区间内, 越界说明单位/口径错了
    d = prov.minute_bars("688825")["data"]
    assert d["low"] <= pts[2]["avg"] <= d["high"]


def test_minute_pct_uses_prev_close_from_qt(monkeypatch):
    """涨跌幅基准是 qt[4] 昨收(50.52), 不是今开、不是前一点价格。"""
    _patch_minute(monkeypatch)
    d = prov.minute_bars("688825")["data"]

    assert d["prev_close"] == pytest.approx(50.52)
    assert d["points"][0]["pct"] == pytest.approx((49.81 - 50.52) / 50.52 * 100, abs=1e-2)
    assert d["last_pct"] == pytest.approx((49.45 - 50.52) / 50.52 * 100, abs=1e-2)
    assert d["open"] == pytest.approx(49.81)
    assert d["high"] == pytest.approx(49.81) and d["low"] == pytest.approx(49.45)


def test_minute_volume_unit_is_normalized_to_shares(monkeypatch):
    """🔴 主板第 3 列给的是**手**, 科创板/ETF 给的是**股** —— 差 100 倍。

    实测 2026-10-09: sh600000 / sh601318 / sh603288 / sz000001 / sz000651 → 手;
    sh688825 / sh688981 / sh512880 → 股。按代码前缀硬编码会踩空, 这里靠
    ``额 = 量 × 均价`` 且均价必落在 [low, high] 反推单位。输出统一为股。
    """
    # 这组是"手": 额/量 = 1178 ≈ 价格 11.78 的 100 倍
    rows = [
        "0930 11.78 7508 8844424.00",
        "0931 11.80 15000 17680000.00",
        "0932 11.75 22000 25900000.00",
    ]
    _patch_minute(monkeypatch, rows=rows, qt=["1", "浦发银行", "600000", "11.75", "11.78", "11.78"])
    d = prov.minute_bars("600000")["data"]

    assert d["points"][0]["volume"] == pytest.approx(750800)          # 7508 手 → 股
    assert d["points"][1]["volume"] == pytest.approx((15000 - 7508) * 100)
    assert d["volume"] == pytest.approx(22000 * 100)                  # 全天 22000 手
    # 均价必须落在当日价格区间内 —— 若不换算会得到 1178 这种荒谬值
    assert d["low"] <= d["points"][0]["avg"] <= d["high"]
    assert d["points"][0]["avg"] == pytest.approx(8844424.0 / 750800, abs=1e-2)


def test_minute_unit_mismatch_raises(monkeypatch):
    """量额自相矛盾(单位既不是股也不是手) → 上抛, 不猜一个系数硬凑。"""
    # 额/量 = 117.8, 既不是 11.78(股) 也不是 1178(手)
    rows = ["0930 11.78 10000 1178000.00", "0931 11.80 20000 2360000.00"]
    _patch_minute(monkeypatch, rows=rows, qt=["1", "浦发银行", "600000", "11.80", "11.78", "11.78"])
    with pytest.raises(ValueError):
        prov.minute_bars("600000")


def test_minute_time_is_hh_mm(monkeypatch):
    _patch_minute(monkeypatch)
    pts = prov.minute_bars("688825")["data"]["points"]
    assert [p["t"] for p in pts] == ["09:30", "09:31", "09:32"]


def test_minute_no_prev_close_raises(monkeypatch):
    """没有昨收基准就不画分时 —— 宁可 available:false, 不拿今开冒充昨收。"""
    _patch_minute(monkeypatch, qt=["1", "长鑫科技", "688825", "51.00", "", "49.81"])
    # qt 缺昨收时会退回行情接口, 那边也拿不到才上抛
    monkeypatch.setattr(prov, "_request_text", lambda *a, **k: 'v_sh688825="";')
    with pytest.raises(ValueError):
        prov.minute_bars("688825")


def test_minute_empty_raises(monkeypatch):
    """非交易日/未开盘 → 空数组, 上抛; 不补零、不沿用上一交易日。"""
    _patch_minute(monkeypatch, empty=True)
    with pytest.raises(ValueError):
        prov.minute_bars("688825")


def test_minute_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_minute(monkeypatch)
    payload = prov.minute_bars("688825")
    assert payload["source"] == "live-tencent"
    assert is_real(payload) is True


def test_api_minute_bad_code(client):
    body = client.get("/api/astock/minute/AAOI").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_minute_ok(client, monkeypatch):
    _patch_minute(monkeypatch)
    body = client.get("/api/astock/minute/688825.SH").json()
    assert body["available"] is True and body["source"] == "live-tencent"
    assert body["data"]["name"] == "长鑫科技"
    assert len(body["data"]["points"]) == 3


# ---------------------------------------------------------------- 6.6 分钟K(腾讯 mkline)

# 实测 sh688825 m5 前 3 根(裁剪)。行 = [时间, 开, 收, 高, 低, 量, {}, extra]
# ⚠️ 第 2 列是开盘、第 3 列是收盘 —— 不是 OHLC 顺序。
_MK_ROWS = [
    ["202610091440", "50.80", "50.95", "50.99", "50.78", "1200000.00", {}, "0.00"],
    ["202610091445", "50.95", "51.10", "51.15", "50.90", "1500000.00", {}, "0.00"],
    ["202610091500", "51.02", "51.00", "51.10", "51.00", "3805391.00", {}, "8.45"],
]
# qt: [6]=成交量 [37]=成交额(万元)。这里 6/37 的比值 = 51.0 → 单位是股。
_MK_QT_SHARES = ["1", "长鑫科技", "688825", "51.00", "50.52", "49.81", "6505391", "", "",
                 "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                 "", "", "", "", "", "", "", "", "", "", "33177"]
# 同一批行(价区 50.78~51.15), 但 qt 的量按"手"给: 额/量 = 5100 ≈ 100×价格
# → 应判定为手并 ×100。
_MK_QT_HANDS = ["1", "浦发银行", "600000", "9.54", "9.70", "9.68", "65053", "", "", "",
                "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                "", "", "", "", "", "", "", "", "", "33177"]


def _patch_mkline(monkeypatch, rows=None, qt=None, empty=False):
    sent: dict[str, object] = {}

    def fake_json(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        # urlencode 会把逗号编成 %2C, 先还原再切
        sym = url.split("param=")[-1].split("%2C")[0]
        if empty:
            return {"code": 0, "data": {sym: {"qt": {sym: qt or _MK_QT_SHARES}, "m5": []}}}
        return {"code": 0, "data": {sym: {
            "qt": {sym: qt if qt is not None else _MK_QT_SHARES},
            "m5": rows if rows is not None else _MK_ROWS,
        }}}

    monkeypatch.setattr(prov, "_request_json", fake_json)
    return sent


def test_mkline_uses_param_form(monkeypatch):
    """mkline 只认 ``param=sh600000,m5,,320``; 用 code=/type= 会返回 param error。"""
    sent = _patch_mkline(monkeypatch)
    prov.minute_kline("600000", period="m5", limit=320)
    assert "param=sh600000%2Cm5%2C%2C320" in sent["url"], sent["url"]


def test_mkline_open_close_order(monkeypatch):
    """🔴 行内第 2 列是开盘、第 3 列是收盘(不是 OHLC 顺序), 写反了整根 K 就反了。"""
    _patch_mkline(monkeypatch)
    rows = prov.minute_kline("688825", period="m5")["data"]["rows"]

    first = rows[0]
    assert first["date"] == "2026-10-09 14:40"      # 区间结束时刻
    assert first["open"] == pytest.approx(50.80)
    assert first["close"] == pytest.approx(50.95)
    assert first["high"] == pytest.approx(50.99)
    assert first["low"] == pytest.approx(50.78)


def test_mkline_volume_unit_normalized_to_shares(monkeypatch):
    """qt 里量按手给时(额/量 = 100×价格) → 输出必须 ×100 折成股。"""
    _patch_mkline(monkeypatch, qt=_MK_QT_HANDS)
    rows = prov.minute_kline("600000", period="m5")["data"]["rows"]
    assert rows[0]["volume"] == pytest.approx(1200000.0 * 100)

    # 单位是股时不该再放大
    _patch_mkline(monkeypatch, qt=_MK_QT_SHARES)
    rows = prov.minute_kline("688825", period="m5")["data"]["rows"]
    assert rows[0]["volume"] == pytest.approx(1200000.0)


def test_mkline_unit_mismatch_raises(monkeypatch):
    """qt 的额/量既不像股也不像手 → 上抛, 不猜系数。"""
    bad = list(_MK_QT_SHARES)
    bad[37] = "331770"      # 额放大 10 倍 → 均价 510, 不在 50.78~51.15
    bad[6] = "6505391"
    _patch_mkline(monkeypatch, qt=bad)
    with pytest.raises(ValueError):
        prov.minute_kline("688825", period="m5")


def test_mkline_bad_period(monkeypatch):
    _patch_mkline(monkeypatch)
    with pytest.raises(ValueError):
        prov.minute_kline("688825", period="m120")


def test_mkline_empty_raises(monkeypatch):
    _patch_mkline(monkeypatch, empty=True)
    with pytest.raises(ValueError):
        prov.minute_kline("688825", period="m5")


def test_mkline_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_mkline(monkeypatch)
    payload = prov.minute_kline("688825", period="m5")
    assert payload["source"] == "live-tencent"
    assert is_real(payload) is True


def test_api_mkline_bad_code(client):
    body = client.get("/api/astock/mkline/AAOI").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_mkline_bad_period(client, monkeypatch):
    _patch_mkline(monkeypatch)
    body = client.get("/api/astock/mkline/688825?period=m2").json()
    assert body["available"] is False and "bad_period" in body["reason"]


def test_api_mkline_ok(client, monkeypatch):
    _patch_mkline(monkeypatch)
    body = client.get("/api/astock/mkline/688825.SH?period=m5&limit=100").json()
    assert body["available"] is True and body["source"] == "live-tencent"
    assert len(body["data"]["rows"]) == 3
    assert body["data"]["period"] == "m5"


# ---------------------------------------------------------------- 7. 所属板块/题材(东财 F10)

# 实测 300308 中际旭创 2026-10-10 取自东财 F10, 字段裁剪后。
_F10_ORG_ROW = [{
    "SECUCODE": "300308.SZ",
    "SECURITY_CODE": "300308",
    "SECURITY_NAME_ABBR": "中际旭创",
    "EM2016": "信息技术-通信设备-通信传输设备",
    "INDUSTRYCSRC1": "制造业-计算机、通信和其他电子设备制造业",
}]

_F10_BOARD_ROWS = [
    {"BOARD_NAME": "通信技术", "IS_PRECISE": "1", "BOARD_RANK": 31,
     "NEW_BOARD_CODE": "BK0812",
     "SELECTED_BOARD_REASON": "2025年半年报显示，公司主营业务为高端光通信收发模块。"},
    {"BOARD_NAME": "算力概念", "IS_PRECISE": "1", "BOARD_RANK": 33,
     "NEW_BOARD_CODE": "BK0901", "SELECTED_BOARD_REASON": None},
    {"BOARD_NAME": "高市净率", "IS_PRECISE": "0", "BOARD_RANK": 12,
     "NEW_BOARD_CODE": "BK0888", "SELECTED_BOARD_REASON": None},
]


def _patch_f10(monkeypatch, org=None, boards=None):
    calls: list[str] = []

    def fake(report_name, filter_str="", page_size=50, sort_columns="", sort_types="-1"):
        calls.append(report_name)
        if report_name == prov._REPORT_F10_ORG:
            return _F10_ORG_ROW if org is None else org
        if report_name == prov._REPORT_F10_BOARD:
            return _F10_BOARD_ROWS if boards is None else boards
        raise AssertionError(f"未预期的报表: {report_name}")

    monkeypatch.setattr(prov, "_datacenter", fake)
    return calls


def test_sectors_uses_f10_reports(monkeypatch):
    """行业与题材都走东财 F10(不是 akshare —— 它底层 push2 在我方被墙)。"""
    calls = _patch_f10(monkeypatch)
    prov.stock_sectors("300308")
    assert prov._REPORT_F10_ORG in calls and prov._REPORT_F10_BOARD in calls


def test_sectors_secucode_suffix(monkeypatch):
    """F10 按 SECUCODE 过滤: 6/9/5 开头 .SH, 4/8 开头 .BJ, 其余 .SZ。"""
    for code, suffix in (("600519", ".SH"), ("300308", ".SZ"), ("830799", ".BJ")):
        sent: dict[str, str] = {}

        # 默认参数绑定循环变量, 避免闭包延迟取值(每次循环 sent 都是新 dict)
        def fake(report_name, filter_str="", page_size=50, sort_columns="",
                 sort_types="-1", _sent=sent):
            if report_name == prov._REPORT_F10_ORG:
                _sent["filter"] = filter_str
                return _F10_ORG_ROW
            return _F10_BOARD_ROWS

        monkeypatch.setattr(prov, "_datacenter", fake)
        prov.stock_sectors(code)
        assert sent["filter"] == f'(SECUCODE="{code}{suffix}")', (code, sent)


def test_sectors_industry_and_boards(monkeypatch):
    _patch_f10(monkeypatch)
    d = prov.stock_sectors("300308")["data"]

    assert d["name"] == "中际旭创"
    assert d["industry"]["em"] == "信息技术-通信设备-通信传输设备"
    assert d["industry"]["em_parts"] == ["信息技术", "通信设备", "通信传输设备"]
    assert d["industry"]["csrc_parts"][0] == "制造业"

    names = [b["name"] for b in d["boards"]]
    assert names == ["通信技术", "算力概念", "高市净率"]
    # 核心题材(IS_PRECISE=1)带入选理由; 宽泛归类没有理由
    assert d["boards"][0]["precise"] is True
    assert "高端光通信" in (d["boards"][0]["reason"] or "")
    assert d["boards"][2]["precise"] is False
    assert d["boards"][2]["reason"] is None


def test_sectors_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_f10(monkeypatch)
    payload = prov.stock_sectors("300308")
    assert payload["source"] == "live-eastmoney-f10"
    assert is_real(payload) is True, "live-eastmoney-f10 未登记进 source_gate 白名单"


def test_sectors_no_data_raises(monkeypatch):
    """🔴 两报表都空 → 上抛。绝不学九章退回硬编码 _DEMO_SECTORS_MAP 编板块名。"""
    _patch_f10(monkeypatch, org=[], boards=[])
    with pytest.raises(ValueError):
        prov.stock_sectors("300308")


def test_sectors_bad_code(monkeypatch):
    _patch_f10(monkeypatch)
    with pytest.raises(ValueError):
        prov.stock_sectors("AAOI")


def test_api_sectors_ok(client, monkeypatch):
    _patch_f10(monkeypatch)
    body = client.get("/api/astock/sectors/300308").json()
    assert body["available"] is True and body["source"] == "live-eastmoney-f10"
    assert body["data"]["boards"][0]["name"] == "通信技术"


def test_api_sectors_bad_code(client):
    body = client.get("/api/astock/sectors/AAOI").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_sectors_core_boards_first(monkeypatch):
    """核心题材必须排前 —— 东财的多列排序(sortColumns=IS_PRECISE,BOARD_RANK)
    实测不生效, 所以排序在 provider 本地做, 这里用乱序输入锁死。"""
    shuffled = [
        {"BOARD_NAME": "高市净率", "IS_PRECISE": "0", "BOARD_RANK": 6,
         "NEW_BOARD_CODE": "BK0888", "SELECTED_BOARD_REASON": None},
        {"BOARD_NAME": "算力概念", "IS_PRECISE": "1", "BOARD_RANK": 33,
         "NEW_BOARD_CODE": "BK0901", "SELECTED_BOARD_REASON": None},
        {"BOARD_NAME": "通信技术", "IS_PRECISE": "1", "BOARD_RANK": 31,
         "NEW_BOARD_CODE": "BK0812", "SELECTED_BOARD_REASON": "半年报显示"},
    ]
    _patch_f10(monkeypatch, boards=shuffled)
    d = prov.stock_sectors("300308")["data"]
    assert [b["name"] for b in d["boards"]] == ["通信技术", "算力概念", "高市净率"]


# ---------------------------------------------------------------- 6.7 日K复权系数(腾讯 fqkline)

# 实测 sh600519: 2026-06-26 除权。之前 raw=1207.68 / qfq=1179.656,
# 除权日之后两者相等(最新价不动)。行 = [日期, 开, 收, 高, 低, 量]。
_ADJ_RAW = [
    ["2026-06-24", "1222.65", "1207.68", "1241.87", "1207.51", "20000.00"],
    ["2026-06-25", "1205.00", "1199.00", "1210.00", "1190.00", "18000.00"],
    ["2026-06-26", "1168.63", "1168.63", "1180.00", "1160.00", "25000.00"],
]
_ADJ_QFQ = [
    ["2026-06-24", "1194.00", "1179.656", "1212.75", "1179.49", "20000.00"],
    ["2026-06-25", "1177.87", "1171.99", "1182.79", "1163.20", "18000.00"],
    ["2026-06-26", "1168.63", "1168.63", "1180.00", "1160.00", "25000.00"],
]
_ADJ_HFQ = [
    ["2026-06-24", "9000.00", "8890.00", "9140.00", "8888.00", "20000.00"],
    ["2026-06-25", "8872.00", "8826.00", "8908.00", "8760.00", "18000.00"],
    ["2026-06-26", "8600.00", "8600.00", "8684.00", "8536.00", "25000.00"],
]
_ADJ_QT = ["1", "贵州茅台", "600519", "1263.00", "1255.42", "1255.42", "35111", "",
           "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
           "", "", "", "", "", "", "", "", "", "", "", "4414"]


def _patch_adj(monkeypatch, qfq=None, hfq=None, raw=None):
    """按 url 里的 param 返回对应档位的序列 —— 复权档要打两次(自己 + 不复权基准)。"""
    sent: dict[str, object] = {}

    def fake_json(url, data=None, headers=None, timeout=None):
        sent.setdefault("urls", []).append(url)
        param = url.split("param=")[-1]
        parts = param.split("%2C")
        sym = parts[0]
        fq = parts[5] if len(parts) > 5 else ""
        payload = {"qt": {sym: _ADJ_QT}}
        if fq == "qfq":
            payload["qfqday"] = qfq if qfq is not None else _ADJ_QFQ
        elif fq == "hfq":
            payload["hfqday"] = hfq if hfq is not None else _ADJ_HFQ
        else:
            payload["day"] = raw if raw is not None else _ADJ_RAW
        # fqkline 响应里也带一份 day(腾讯实际如此), 但基准一律用 kline/kline 端点
        return {"code": 0, "data": {sym: payload}}

    monkeypatch.setattr(prov, "_request_json", fake_json)
    prov._CACHE.clear()
    return sent


def test_adjratio_qfq_is_ratio_to_raw(monkeypatch):
    """ratio = 复权收盘 / 不复权收盘; 最新一日两者相等 → ratio == 1。"""
    sent = _patch_adj(monkeypatch)
    d = prov.adjust_ratios("600519", mode="qfq")["data"]
    r = {x["date"]: x["ratio"] for x in d["ratios"]}
    assert r["2026-06-24"] == pytest.approx(1179.656 / 1207.68)
    assert r["2026-06-26"] == pytest.approx(1.0)      # 除权日之后不动
    assert d["has_adjust"] is True
    # 复权档要额外取一次不复权序列做基准
    assert sum("kline/kline" in u for u in sent["urls"]) == 1


def test_adjratio_hfq_scales_up(monkeypatch):
    """后复权比值 > 1(把历史价往上抬, 最早一日不动)。"""
    _patch_adj(monkeypatch)
    d = prov.adjust_ratios("600519", mode="hfq")["data"]
    r = {x["date"]: x["ratio"] for x in d["ratios"]}
    assert r["2026-06-24"] > 1.0
    assert r["2026-06-24"] == pytest.approx(8890.00 / 1207.68)


def test_adjratio_none_is_all_ones(monkeypatch):
    """不复权档不打第二次请求, ratio 恒 1, has_adjust=False。"""
    sent = _patch_adj(monkeypatch)
    d = prov.adjust_ratios("600519", mode="none")["data"]
    assert all(x["ratio"] == 1.0 for x in d["ratios"])
    assert d["has_adjust"] is False
    assert len(sent["urls"]) == 1


def test_adjratio_no_ex_rights_has_adjust_false(monkeypatch):
    """没有除权事件时三档结果一致 → has_adjust=False, 前端据此说明"不是开关坏了"。"""
    same = [["2026-06-24", "10.00", "10.10", "10.20", "9.90", "100.00"]]
    _patch_adj(monkeypatch, qfq=same, hfq=same, raw=same)
    d = prov.adjust_ratios("600519", mode="qfq")["data"]
    assert d["has_adjust"] is False


def test_adjratio_bad_mode(monkeypatch):
    _patch_adj(monkeypatch)
    with pytest.raises(ValueError):
        prov.adjust_ratios("600519", mode="bfq")


def test_adjratio_only_overlapping_dates(monkeypatch):
    """复权序列与基准序列只取交集日期 —— 缺一边就不给系数(不猜)。"""
    partial = [["2026-06-25", "1177.87", "1171.99", "1182.79", "1163.20", "18000.00"]]
    _patch_adj(monkeypatch, qfq=partial)
    d = prov.adjust_ratios("600519", mode="qfq")["data"]
    assert [x["date"] for x in d["ratios"]] == ["2026-06-25"]


def test_api_adjratio_bad_mode(client):
    body = client.get("/api/astock/adjratio/600519?mode=bfq").json()
    assert body["available"] is False and "bad_mode" in body["reason"]


def test_api_adjratio_ok(client, monkeypatch):
    _patch_adj(monkeypatch)
    body = client.get("/api/astock/adjratio/600519?mode=qfq").json()
    assert body["available"] is True and body["source"] == "live-tencent"
    assert body["data"]["name"] == "贵州茅台"
    assert len(body["data"]["ratios"]) == 3


def test_adjratio_cache_key_not_polluted(monkeypatch):
    """🔴 不复权档与"取基准"用的是同一个 url(kline/kline?param=sym,day,,,n),
    但两处存的东西不同(结果 dict vs 腾讯原始响应)。共用裸 url 会互相覆盖,
    表现为 mode=none 返回 source_gate_rejected —— 缓存键必须带前缀。"""
    _patch_adj(monkeypatch)
    first = prov.adjust_ratios("600519", mode="qfq")   # 先把原始响应写进共用 url 的缓存
    assert first["source"] == "live-tencent"
    second = prov.adjust_ratios("600519", mode="none")  # 若被污染, 这里拿到的不是结果 dict
    assert second["source"] == "live-tencent"
    assert all(x["ratio"] == 1.0 for x in second["data"]["ratios"])
    assert second["data"]["has_adjust"] is False


def test_api_adjratio_none_after_qfq(client, monkeypatch):
    """同一个进程里先问 qfq 再问 none —— none 必须仍然 available(不被缓存污染)。"""
    _patch_adj(monkeypatch)
    assert client.get("/api/astock/adjratio/600519?mode=qfq").json()["available"] is True
    body = client.get("/api/astock/adjratio/600519?mode=none").json()
    assert body["available"] is True, body
    assert all(x["ratio"] == 1.0 for x in body["data"]["ratios"])


def test_adjratio_falls_back_when_no_qfq_key(monkeypatch):
    """🔴 没除权记录的股票(如新股 688825)腾讯**只回 day、不给 qfqday**。
    这不是失败, 是最权威的"无需复权"信号 —— 必须降级成 ratio 全 1,
    不能 raise(否则个股页复权档对这些票整档 available:false)。"""
    def fake_json(url, data=None, headers=None, timeout=None):
        sym = url.split("param=")[-1].split("%2C")[0]
        # 无论要哪个档位, 都只给 day
        return {"code": 0, "data": {sym: {"qt": {sym: _ADJ_QT}, "day": _ADJ_RAW}}}

    monkeypatch.setattr(prov, "_request_json", fake_json)
    prov._CACHE.clear()
    d = prov.adjust_ratios("688825", mode="qfq")["data"]
    assert [x["date"] for x in d["ratios"]] == ["2026-06-24", "2026-06-25", "2026-06-26"]
    assert all(x["ratio"] == 1.0 for x in d["ratios"])
    assert d["has_adjust"] is False


def test_api_adjratio_new_stock(client, monkeypatch):
    monkeypatch.setattr(prov, "_request_json", lambda url, data=None, headers=None, timeout=None:
                        {"code": 0, "data": {"sh688825": {"qt": {"sh688825": _ADJ_QT},
                                                          "day": _ADJ_RAW}}})
    prov._CACHE.clear()
    body = client.get("/api/astock/adjratio/688825.SH?mode=qfq").json()
    assert body["available"] is True, body
    assert body["data"]["has_adjust"] is False


# ════════════════════════════════════════════════════════════════════
# 指数日K(对比页基准) —— GET /api/astock/index/{code}
# ════════════════════════════════════════════════════════════════════

# [日期, 开, 收, 高, 低, 量] —— 与个股日K同口径: 第 2 列是**收**
_IDX_ROWS = [
    ["2026-09-28", "4500.00", "4510.00", "4520.00", "4490.00", "120000.00"],
    ["2026-09-29", "4510.00", "4530.00", "4540.00", "4500.00", "130000.00"],
    ["2026-09-30", "4530.00", "0", "4540.00", "4500.00", "130000.00"],   # 收盘为 0 → 丢弃
]


def _patch_index(monkeypatch, rows=None, qt=None):
    def fake_json(url, data=None, headers=None, timeout=None):
        sym = url.split("param=")[-1].split("%2C")[0]
        return {"code": 0, "data": {sym: {"qt": {sym: qt or ["1", "沪深300"]},
                                          "day": rows if rows is not None else _IDX_ROWS}}}
    monkeypatch.setattr(prov, "_request_json", fake_json)
    monkeypatch.setattr(prov, "_request_text", lambda *a, **k: 'v_sh000300="1~沪深300~000300~"')
    prov._CACHE.clear()


def test_index_sh_prefix_for_hs300(monkeypatch):
    """🔴 沪深300 是 0 开头但属于**沪市** —— 按个股规则(0→sz)会拼成 sz000300,
    腾讯返回空。指数必须走单独的符号规则。"""
    assert prov._tx_index_symbol("000300.SH") == "sh000300"
    assert prov._tx_index_symbol("000001.SH") == "sh000001"
    assert prov._tx_index_symbol("000905.SH") == "sh000905"
    assert prov._tx_index_symbol("399006.SZ") == "sz399006"
    assert prov._tx_index_symbol("399001.SZ") == "sz399001"


def test_index_uses_tencent_kline(monkeypatch):
    seen: list[str] = []
    def fake_json(url, data=None, headers=None, timeout=None):
        seen.append(url)
        return {"code": 0, "data": {"sh000300": {"day": _IDX_ROWS}}}
    monkeypatch.setattr(prov, "_request_json", fake_json)
    monkeypatch.setattr(prov, "_request_text", lambda *a, **k: "")
    prov._CACHE.clear()
    prov.index_daily("000300.SH", 60)
    assert seen and "kline/kline" in seen[0] and "sh000300%2Cday" in seen[0]


def test_index_open_close_order(monkeypatch):
    """行内第 2 列是开、第 3 列是收(不是 OHLC 顺序) —— 取反了基准线就错了。"""
    _patch_index(monkeypatch)
    row = prov.index_daily("000300.SH")["data"]["rows"][0]
    assert row["open"] == 4500.0 and row["close"] == 4510.0
    assert row["high"] == 4520.0 and row["low"] == 4490.0


def test_index_drops_zero_close(monkeypatch):
    """收盘价 0 的行直接丢 —— 归一化/对数收益遇到 0 会算出 Inf。"""
    _patch_index(monkeypatch)
    rows = prov.index_daily("000300.SH")["data"]["rows"]
    assert [r["date"] for r in rows] == ["2026-09-28", "2026-09-29"]


def test_index_volume_in_shares(monkeypatch):
    """腾讯给的是手, 统一成股(与个股口径一致)。"""
    _patch_index(monkeypatch)
    assert prov.index_daily("000300.SH")["data"]["rows"][0]["volume"] == 12_000_000.0


def test_index_source_is_real_and_name(monkeypatch):
    _patch_index(monkeypatch)
    r = prov.index_daily("000300.SH")
    assert r["source"] == "live-tencent"
    assert r["data"]["name"] == "沪深300"
    from app.services.source_gate import is_real
    assert is_real(r)


def test_index_empty_raises(monkeypatch):
    _patch_index(monkeypatch, rows=[])
    with pytest.raises(ValueError):
        prov.index_daily("000300.SH")


def test_index_limit_bounded(monkeypatch):
    """3年档要 750 根, 上限给到 1000(实测腾讯 day 能给到 1000)。"""
    _patch_index(monkeypatch)
    prov.index_daily("000300.SH", 5000)     # 不应报错
    with pytest.raises(ValueError):
        _patch_index(monkeypatch, rows=[])
        prov.index_daily("000300.SH")


def test_api_index_bad_code(client):
    body = client.get("/api/astock/index/abc").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_index_bad_limit(client):
    body = client.get("/api/astock/index/000300.SH?limit=5").json()
    assert body["available"] is False and "bad_limit" in body["reason"]


def test_api_index_ok(client, monkeypatch):
    _patch_index(monkeypatch)
    body = client.get("/api/astock/index/000300.SH?limit=320").json()
    assert body["available"] is True, body
    assert body["source"] == "live-tencent"
    assert body["data"]["code"] == "000300"
    assert len(body["data"]["rows"]) == 2


# ════════════════════════════════════════════════════════════════════
# 成交明细(3 秒快照) —— GET /api/astock/ticks/{code}
# ════════════════════════════════════════════════════════════════════

def _detail_body(page_no: int, rows: list[str] | None = None) -> str:
    """拼一页腾讯明细响应。rows=None 表示翻过头了(空)。"""
    if rows is None:
        return ""
    return f'v_detail_data_sh600519=[{page_no},"' + "|".join(rows) + '"]'


def _patch_detail(monkeypatch, pages: dict[int, list[str]], quote_time="20261009161459"):
    """按页码返回明细 + 行情(行情只用来取数据日期)。"""
    calls: list[int] = []

    def fake_text(url, encoding="utf-8", headers=None, timeout=None):
        if "qt.gtimg.cn" in url:
            # 按 ~ 切字段, 30 号位是行情时间 —— 用列表拼, 别去数多少个 ~
            parts = ["1", "贵州茅台", "600519", "1263.00", "1255.79", "1255.42", "193"]
            parts += ["0"] * (30 - len(parts))
            parts.append(quote_time)
            parts += ["0"] * 20
            return 'v_sh600519="' + "~".join(parts) + '~"'
        calls.append(url)
        m = re.search(r"[?&]p=(\d+)", url)
        pn = int(m.group(1)) if m else 0
        return _detail_body(pn, pages.get(pn))

    monkeypatch.setattr(prov, "_request_text", fake_text)
    monkeypatch.setattr(prov, "_timeout", lambda: 5.0)
    prov._CACHE.clear()
    return calls


_R0 = [
    "0/09:25:02/1255.42/0.01/195/24480690/B",
    "1/09:30:02/1258.41/2.99/127/15960625/S",
    "2/09:30:05/1260.01/1.60/209/26350708/M",
]


def test_tick_row_price_change_amount_order(monkeypatch):
    """行的第 3/4/6 列是 价/涨跌/额。反过来读会把涨跌当成成交量。"""
    _patch_detail(monkeypatch, {0: _R0})
    row = prov.tick_trades("600519.SH", 10)["data"]["rows"][-1]   # 新在前 → 第一条内容在末尾
    assert row["price"] == 1255.42
    assert row["change"] == 0.01
    assert row["amount"] == 24480690.0
    assert row["time"] == "09:25:02"


def test_tick_shares_derived_from_amount_over_price(monkeypatch):
    """🔴 股数必须由「额 ÷ 价」反推 —— 第 5 列随标的时而手时而股(分时那档已经
    踩过一次: 科创板按手读会差 100 倍)。这里 24480690 / 1255.42 = 19499.98 股。"""
    _patch_detail(monkeypatch, {0: _R0})
    row = prov.tick_trades("600519.SH", 10)["data"]["rows"][-1]
    assert row["shares"] == 19500.0
    assert row["lots"] == 195.0


def test_tick_side_and_session_labels(monkeypatch):
    """B/S/M 三态要分开: M 是"判不出方向", 不能塞进买或卖充数。"""
    _patch_detail(monkeypatch, {0: _R0})
    rows = {r["time"]: r for r in prov.tick_trades("600519.SH", 10)["data"]["rows"]}
    assert rows["09:25:02"]["side"] == "B"
    assert rows["09:30:02"]["side"] == "S"
    assert rows["09:30:05"]["side"] == "M"
    # 09:25 是开盘集合竞价, 与连续竞价不是一回事
    assert rows["09:25:02"]["auction"] == "open"


def test_tick_after_hours_not_counted_as_close_auction(monkeypatch):
    """深市/科创板/ETF 会排到 15:2x —— 那是盘后成交, 算进收盘集合竞价会虚增后者金额。"""
    rows = {
        0: ["0/15:00:02/1263.00/-0.45/385/48696228/M",
            "1/15:17:30/38.83/0.00/2/7766/S"],
    }
    _patch_detail(monkeypatch, rows)
    by_time = {r["time"]: r for r in prov.tick_trades("600519.SH", 10)["data"]["rows"]}
    assert by_time["15:00:02"]["auction"] == "close"
    assert by_time["15:17:30"]["auction"] == "after"


def test_tick_newest_first(monkeypatch):
    """成交明细的阅读习惯是盯最新一笔 —— 返回必须是时间倒序。"""
    _patch_detail(monkeypatch, {0: _R0})
    times = [r["time"] for r in prov.tick_trades("600519.SH", 10)["data"]["rows"]]
    assert times == sorted(times, reverse=True)


def test_tick_summary_neutral_kept_separate(monkeypatch):
    """净额 = 买额 − 卖额, 中性单独计 —— 替交易所判定 M 的方向不是我们的事。"""
    _patch_detail(monkeypatch, {0: _R0})
    s = prov.tick_trades("600519.SH", 10)["data"]["summary"]
    assert s["buy_amount"] == 24480690.0
    assert s["sell_amount"] == 15960625.0
    assert s["neutral_amount"] == 26350708.0
    assert s["net_amount"] == round(24480690.0 - 15960625.0, 2)
    assert s["count"] == 3


def test_tick_bad_rows_dropped(monkeypatch):
    """坏行(字段不足 / 价格 0 / 时间格式不对)整行丢, 不猜不补零。"""
    _patch_detail(monkeypatch, {0: [
        "0/09:25:02/1255.42/0.01/195/24480690/B",
        "1/09:30:02",                                  # 字段不足
        "2/09:30:05/0/1.60/209/26350708/B",            # 价格为 0 → 股数除不出来
        "3/99:99:99/1260.01/1.60/209/26350708/B",      # 时间不是 HH:MM:SS
        "4/09:30:08/1260.23/0.22/171/21581660/B",
    ]})
    rows = prov.tick_trades("600519.SH", 10)["data"]["rows"]
    assert [r["time"] for r in rows] == ["09:30:08", "09:25:02"]


def test_tick_empty_raises(monkeypatch):
    """第一页就是空(停牌/今日无成交/不支持的标的) → fail-closed, 不返回空壳。"""
    _patch_detail(monkeypatch, {})
    with pytest.raises(ValueError):
        prov.tick_trades("600519.SH", 10)


def test_tick_tail_limited(monkeypatch):
    """tail 是"最近 N 行", 超了要截, 不能把整日都塞给前端。"""
    page_rows = [f"{i}/09:{30 + i // 20:02d}:{i % 60:02d}/100.00/0.10/{i + 1}/{100000 + i}/B"
                 for i in range(70)]
    _patch_detail(monkeypatch, {0: page_rows})
    rows = prov.tick_trades("600519.SH", 5)["data"]["rows"]
    assert len(rows) == 5


def test_tick_page_probe_does_not_refetch(monkeypatch):
    """找最后一页是倍增+二分, 同一个页码不该被重复请求。"""
    pages = {i: [f"{i * 70}/14:00:{i:02d}/100.00/0.10/5/500/B"] for i in range(0, 70)}
    calls = _patch_detail(monkeypatch, pages)
    prov.tick_trades("600519.SH", 200)
    urls = [u for u in calls if "detail" in u]
    assert len(urls) == len(set(urls))


def test_tick_date_from_quote_time(monkeypatch):
    """明细接口本身不带日期 —— 从行情时间取, 免得"这是哪天的明细"说不清。"""
    _patch_detail(monkeypatch, {0: _R0}, quote_time="20261009161459")
    assert prov.tick_trades("600519.SH", 10)["data"]["date"] == "2026-10-09"


def test_tick_source_is_real(monkeypatch):
    _patch_detail(monkeypatch, {0: _R0})
    r = prov.tick_trades("600519.SH", 10)
    assert r["source"] == "live-tencent"
    from app.services.source_gate import is_real
    assert is_real(r)


def test_api_ticks_bad_code(client):
    body = client.get("/api/astock/ticks/abc").json()
    assert body["available"] is False and "bad_code" in body["reason"]


def test_api_ticks_bad_tail(client):
    body = client.get("/api/astock/ticks/600519.SH?tail=0").json()
    assert body["available"] is False and "bad_tail" in body["reason"]


def test_api_ticks_ok(client, monkeypatch):
    _patch_detail(monkeypatch, {0: _R0})
    body = client.get("/api/astock/ticks/600519.SH?tail=20").json()
    assert body["available"] is True, body
    assert body["source"] == "live-tencent"
    assert len(body["data"]["rows"]) == 3
    assert body["data"]["summary"]["count"] == 3
