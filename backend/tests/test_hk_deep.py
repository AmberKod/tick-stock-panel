"""港股深度数据护栏测试 —— 公告(港交所披露易) + 财报(东财公告原文)。

核心守护五条:
1. **披露易只认 stockId 不认代码** —— 直接拿 00700 当 stockId 会返回空列表
   (假成功)。且 prefix.do 是**模糊匹配**, 会同时返回 80700(人民币柜台),
   必须按 code 精确取值, 否则串票。
2. **退市/查不到 stockId 是确定性无数据** —— 抛 HKNewsNotFoundError(不是空列表),
   API 层转 ``available:false`` + ``not_found``。实测 00011 恒生银行如此。
3. **「窗口内没发过公告」是真实答案** —— rows 为空时 available 仍为 true,
   不得谎报成"取不到数"。
4. **财报解析出 0 行是真失败** —— 不是"这家公司没财报", 实测 00011.HK 如此。
5. **东财限流要降级而不是整块挂掉** —— 完整多期中途 567 时, 必须退回
   "仅最新一期"而不是把整个面板标成不可用。

全部用例 mock 掉网络, 不依赖外网。
"""
from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.api import hk_deep as api
from app.data_providers import hk_deep_provider as prov
from app.main import app

# ── 实测样本 (2026-10-11 取自港交所披露易 / 东财公告) ──────────────────
# prefix.do 是 JSONP, 且**模糊匹配**: 查 00700 会连 80700(人民币柜台)一起返回。
_PREFIX_JSONP = (
    'callback({"more":"1","stockInfo":['
    '{"stockId":99999,"code":"80700","name":"騰訊控股－Ｒ"},'
    '{"stockId":7609,"code":"00700","name":"騰訊控股"}]});'
)

# titleSearchServlet.do: 顶层 result 是**JSON 字符串**, 不是数组(解析踩过坑)。
_SEARCH_ROWS = [
    {
        "FILE_INFO": "125KB", "NEWS_ID": "12368505", "TOTAL_COUNT": "96",
        "SHORT_TEXT": "翌日披露報表 - [股份購回]<br/>",
        "STOCK_NAME": "騰訊控股<br/>騰訊控股－Ｒ", "TITLE": "翌日披露報表",
        "FILE_TYPE": "PDF", "DATE_TIME": "09/10/2026 17:44",
        "LONG_TEXT": "翌日披露報表 - [股份購回]",
        "STOCK_CODE": "00700<br/>80700",
        "FILE_LINK": "/listedco/listconews/sehk/2026/1009/2026100900650_c.pdf",
    },
    {
        "FILE_INFO": "4MB", "NEWS_ID": "12100025", "TOTAL_COUNT": "96",
        "SHORT_TEXT": "財務報表&#x2f;環境、社會及管治資料 - [年報]<br/>",
        "STOCK_NAME": "騰訊控股", "TITLE": "2025 年報",
        "FILE_TYPE": "PDF", "DATE_TIME": "09/04/2026 17:21",
        "LONG_TEXT": "財務報表&#x2f;環境、社會及管治資料 - [年報]",
        "STOCK_CODE": "00700",
        "FILE_LINK": "/listedco/listconews/sehk/2026/0409/2026040901232_c.pdf",
    },
]

_PROV_JSON = json.dumps({
    "net_margin": {
        "formula": "net_profit / revenue * 100",
        "original_lines": {"net_profit": "期內盈利 117,369 105,769 11%",
                           "revenue": "收入 401,243 364,526 10%"},
    },
})


def _fin_frame(n: int = 3) -> pl.DataFrame:
    """构造 n 期财报 frame(升序喂入, 验证 financials() 会排成降序)。"""
    periods = [date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)][:n]
    return pl.DataFrame({
        "period_end": periods,
        "announce_date": [date(2026, 3, 18), date(2026, 5, 13), date(2026, 8, 12)][:n],
        "report_currency": ["CNY"] * n,
        "gross_margin": [55.0, 56.1, 57.2][:n],
        "net_margin": [27.0, 28.2, 29.3][:n],
        "revenue_yoy": [8.0, 9.1, 10.1][:n],
        "net_income_yoy": [7.0, 8.5, 10.3][:n],
        "source_url": ["https://np-cnotice-stock.eastmoney.com/api/content/ann?art_code=AN1"] * n,
        # 该 provider 刻意不产这几个字段(见 hk_financial_provider 模块注释),
        # 测试里保持 None —— 锁死"缺字段透 None, 不补 0"。
        "roe": [None] * n,
        "bps": [None] * n,
        "eps_ttm": [None] * n,
        "total_shares": [None] * n,
        "field_provenance": [_PROV_JSON] * n,
    })


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """provider 用模块级 _CACHE, 跨用例/跨测试文件残留会让断言假通过。"""
    prov._CACHE.clear()
    yield
    prov._CACHE.clear()


@pytest.fixture()
def client():
    """⚠️ ``client=("127.0.0.1", 5555)`` 不能省: 后端鉴权只放行可信来源,
    用默认 TestClient 来源打 API 会拿到 **403**(不是 401), 且与被测逻辑无关
    —— 少了这个参数会让人误以为"端点挂了"。与 test_astock_deep.py 同口径。
    """
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        yield c


# ── 代码归一化 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["00700", "00700.HK", "0700.HK", "hk00700", "00700.hk"])
def test_norm_symbol_variants(raw: str) -> None:
    assert prov._norm_symbol(raw) == "00700.HK"


def test_norm_symbol_rejects_non_hk() -> None:
    with pytest.raises(ValueError):
        prov._norm_symbol("AAPL")


def test_norm_symbol_zero_pads_4_digit() -> None:
    """0700.HK 与 00700.HK 是同一只票 —— 补零而不是报错。"""
    assert prov._norm_symbol("0700.HK") == "00700.HK"


# ── 披露易文本清洗 ────────────────────────────────────────────────────
def test_unescape_text_strips_entities_and_br() -> None:
    """'&#x2f;' 是 '/', '<br/>' 要变空格 —— 不清洗会把 HTML 直接显示给用户。"""
    assert prov._unescape_text("財務報表&#x2f;環境<br/>") == "財務報表/環境"


def test_split_category() -> None:
    assert prov._split_category("公告及通告 - [發行債務證券]") == ("公告及通告", "發行債務證券")


def test_split_category_without_bracket() -> None:
    assert prov._split_category("月報表") == ("月報表", "")


def test_parse_dt_is_day_first() -> None:
    """披露易日期是 dd/mm/yyyy —— 按月在前的美式解析会得到 9 月 10 日(错)。"""
    assert prov._parse_dt("09/10/2026 17:44") == "2026-10-09T17:44:00"


def test_parse_dt_bad_value_returns_none() -> None:
    assert prov._parse_dt("not a date") is None
    assert prov._parse_dt(None) is None


# ── stockId 查询 ──────────────────────────────────────────────────────
def test_stock_id_exact_match_not_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """模糊匹配结果里 80700 排在前面, 取第一个会串到人民币柜台。"""
    monkeypatch.setattr(prov.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_PREFIX_JSONP))
    assert prov._hkex_stock_id("00700") == 7609


def test_stock_id_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_PREFIX_JSONP))
    assert prov._hkex_stock_id("00700") == 7609
    # 第二次连 opener 都不该构造: 命中缓存
    monkeypatch.setattr(prov.urllib.request, "build_opener",
                        lambda *a, **k: pytest.fail("stockId 未命中缓存, 又打了一次网络"))
    assert prov._hkex_stock_id("00700") == 7609


def test_stock_id_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """查不到(退市)必须抛 HKNewsNotFoundError —— 不是返回空列表冒充"没发过公告"。"""
    monkeypatch.setattr(prov.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener('callback({"stockInfo":[]});'))
    with pytest.raises(prov.HKNewsNotFoundError):
        prov._hkex_stock_id("00011")


def test_stock_id_bad_jsonp_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener("<html>出错了</html>"))
    with pytest.raises(ValueError, match="JSONP"):
        prov._hkex_stock_id("00700")


# ── 公告 provider ─────────────────────────────────────────────────────
def test_announcements_full_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """嵌套 JSON(result 是字符串) + 实体清洗 + URL 绝对化 + 分类拆分。"""
    monkeypatch.setattr(prov, "_hkex_stock_id", lambda code, timeout=20.0: 7609)
    monkeypatch.setattr(prov, "_request_json",
                        lambda url, timeout: {"result": json.dumps(_SEARCH_ROWS)})

    payload = prov.announcements("00700.HK", days=180, limit=10)
    assert payload["source"] == prov.SOURCE_HKEX
    data = payload["data"]
    assert data["symbol"] == "00700.HK" and data["stock_id"] == 7609
    assert len(data["rows"]) == 2 and data["total_in_window"] == 96

    first = data["rows"][0]
    assert first["title"] == "翌日披露報表"
    assert first["category"] == "翌日披露報表" and first["sub_category"] == "股份購回"
    assert first["announce_at"] == "2026-10-09T17:44:00"
    assert first["url"].startswith("https://www1.hkexnews.hk/listedco/")
    # 年报那条带 &#x2f; 实体
    assert "/" in data["rows"][1]["category"]


def test_announcements_empty_window_is_real_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口内没公告 = 真实答案, rows 为空但不报错(由 API 层保持 available:true)。"""
    monkeypatch.setattr(prov, "_hkex_stock_id", lambda code, timeout=20.0: 7609)
    monkeypatch.setattr(prov, "_request_json", lambda url, timeout: {"result": "[]"})

    data = prov.announcements("00700.HK")["data"]
    assert data["rows"] == [] and data["total_in_window"] == 0


def test_announcements_bad_payload_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """result 不是字符串(上游改结构) → 上抛, 不静默返回空列表。"""
    monkeypatch.setattr(prov, "_hkex_stock_id", lambda code, timeout=20.0: 7609)
    monkeypatch.setattr(prov, "_request_json", lambda url, timeout: {"result": []})
    with pytest.raises(ValueError, match="结构异常"):
        prov.announcements("00700.HK")


def test_announcements_rejects_bad_range() -> None:
    with pytest.raises(ValueError, match="bad_days"):
        prov.announcements("00700.HK", days=0)
    with pytest.raises(ValueError, match="bad_limit"):
        prov.announcements("00700.HK", limit=0)


# ── 财报 provider ─────────────────────────────────────────────────────
def test_financials_sorted_desc_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_fetch_financials", lambda s, p, t: (_fin_frame(3), None))

    data = prov.financials("00700.HK", periods=3)["data"]
    assert [r["period_end"] for r in data["rows"]] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    top = data["rows"][0]
    assert top["gross_margin"] == 57.2 and top["net_margin"] == 29.3
    # 溯源必须解析成 dict(不是原样字符串), 前端要靠它显示"这个数怎么来的"
    assert isinstance(top["provenance"], dict)
    assert top["provenance"]["net_margin"]["formula"] == "net_profit / revenue * 100"


def test_financials_missing_fields_stay_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """roe/bps/eps/总股本 该 provider 不产 —— 必须是 None, 绝不能补 0
    (0 会被前端读成"ROE 为 0"这种假事实)。"""
    monkeypatch.setattr(prov, "_fetch_financials", lambda s, p, t: (_fin_frame(2), None))

    top = prov.financials("00700.HK", periods=2)["data"]["rows"][0]
    assert top["roe"] is None and top["bps"] is None
    assert top["eps_ttm"] is None and top["total_shares"] is None


def test_financials_degraded_surfaces_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """限流降级必须如实标出来 —— 前端要显示"仅最新一期", 不能装成完整多期。"""
    monkeypatch.setattr(prov, "_fetch_financials",
                        lambda s, p, t: (_fin_frame(1), "东财公告源限流, 已降级为仅最新一期 (x)"))

    data = prov.financials("00700.HK", periods=8)["data"]
    assert len(data["rows"]) == 1
    assert data["degraded"] is not None and "降级" in data["degraded"]
    assert data["requested_periods"] == 8


def test_financials_zero_rows_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 行 = 解析失败(实测 00011.HK), 不是"这家公司没财报"。"""
    monkeypatch.setattr(prov, "_fetch_financials", lambda s, p, t: (_fin_frame(0), None))
    with pytest.raises(ValueError, match="0 行"):
        prov.financials("00011.HK")


def test_financials_rejects_bad_periods() -> None:
    with pytest.raises(ValueError, match="bad_periods"):
        prov.financials("00700.HK", periods=0)
    with pytest.raises(ValueError, match="bad_periods"):
        prov.financials("00700.HK", periods=99)


def test_fetch_financials_degrades_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整多期中途 567 → 自动退到 latest_only, 而不是整块抛掉。"""
    calls: list[bool] = []

    class _FakeProvider:
        def __init__(self, timeout: float) -> None:
            pass

        def get_financials(self, table, symbols, latest_only=False):
            calls.append(latest_only)
            if latest_only:
                return _fin_frame(1)
            raise RuntimeError("Server error '567 Unknown Status'")

    monkeypatch.setattr("app.data_providers.hk_financial_provider.HKFinancialProvider", _FakeProvider)

    frame, degraded = prov._fetch_financials("00700.HK", 8, 15.0)
    assert calls == [False, True]
    assert frame is not None and frame.height == 1
    assert degraded is not None and "567" in degraded


def test_fetch_financials_reraises_when_both_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProvider:
        def __init__(self, timeout: float) -> None:
            pass

        def get_financials(self, table, symbols, latest_only=False):
            raise RuntimeError("down")

    monkeypatch.setattr("app.data_providers.hk_financial_provider.HKFinancialProvider", _FakeProvider)
    with pytest.raises(RuntimeError, match="down"):
        prov._fetch_financials("00700.HK", 8, 15.0)


# ── API 层 fail-closed ────────────────────────────────────────────────
def test_api_announcements_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_hkex_stock_id", lambda code, timeout=20.0: 7609)
    monkeypatch.setattr(prov, "_request_json",
                        lambda url, timeout: {"result": json.dumps(_SEARCH_ROWS)})

    r = client.get("/api/hk/announcements/00700.HK")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["source"] == prov.SOURCE_HKEX
    assert body["symbol"] == "00700.HK"
    assert len(body["data"]["rows"]) == 2


def test_api_announcements_empty_stays_available(client: TestClient,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口内没公告 ≠ 取不到数: available 仍为 true, rows 为空。"""
    monkeypatch.setattr(prov, "_hkex_stock_id", lambda code, timeout=20.0: 7609)
    monkeypatch.setattr(prov, "_request_json", lambda url, timeout: {"result": "[]"})

    body = client.get("/api/hk/announcements/00700.HK").json()
    assert body["available"] is True and body["data"]["rows"] == []


def test_api_not_found_is_false(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """退市(00011)必须 available:false + not_found, 不能返回空列表装成功。"""
    monkeypatch.setattr(prov, "_hkex_stock_id",
                        lambda code, timeout=20.0: (_ for _ in ()).throw(prov.HKNewsNotFoundError("查不到")))

    body = client.get("/api/hk/announcements/00011.HK").json()
    assert body["available"] is False and "not_found" in body["reason"]


def test_api_provider_error_is_false(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_hkex_stock_id",
                        lambda code, timeout=20.0: (_ for _ in ()).throw(RuntimeError("timeout")))

    body = client.get("/api/hk/announcements/00700.HK").json()
    assert body["available"] is False and "provider_error" in body["reason"]


def test_api_financials_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_fetch_financials", lambda s, p, t: (_fin_frame(3), None))

    body = client.get("/api/hk/financials/00700.HK?periods=3").json()
    assert body["available"] is True
    assert body["source"] == prov.SOURCE_EM_HK_ANN
    assert len(body["data"]["rows"]) == 3
    assert body["data"]["degraded"] is None


def test_api_financials_zero_rows_is_false(client: TestClient,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "_fetch_financials", lambda s, p, t: (_fin_frame(0), None))

    body = client.get("/api/hk/financials/00011.HK").json()
    assert body["available"] is False and "provider_error" in body["reason"]


def test_api_source_gate_blocks_fake_source(client: TestClient,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门兜底: provider 万一返回未登记 source, API 层必须拦下。"""
    monkeypatch.setattr(api, "announcements",
                        lambda *a, **k: {"source": "hkex_demo", "data": {"symbol": "00700.HK", "rows": []}})

    body = client.get("/api/hk/announcements/00700.HK").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


class _FakeOpener:
    """替代 urllib opener: 直接吐预设文本, 不打网络。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def open(self, req, timeout=None):
        class _Resp:
            def __init__(self, payload: str) -> None:
                self._payload = payload.encode("utf-8")

            def read(self) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> bool:
                return False

        return _Resp(self._text)


# ─────────────────── 券商盈利预测 / 评级 / 目标价 ───────────────────
# 源: akshare stock_hk_profit_forecast_et (2026-10-12 实测 00700 返回 50 行)。
# 守护四条:
#   1. 中文列正确映射到英文键, 且**单位换算**符合东财口径(纯利百万港元→亿港元,
#      每股港仙→港元), raw_* 保留原值供核对;
#   2. 东财用 "--" 表示"该券商没给这一年的评级" —— 必须当 None, 不能当评级文本;
#   3. pandas 空单元格是 NaN 不是 None —— 必须转 None, **绝不补零**;
#   4. 解析出 0 行是真失败(非"无预测"), API 层如实 available:false。


def _fake_forecasts(monkeypatch, records):
    """把 akshare 的港股盈利预测换成预设 pandas DataFrame。"""
    import akshare as ak
    import pandas as pd

    monkeypatch.setattr(ak, "stock_hk_profit_forecast_et", lambda symbol: pd.DataFrame(records))


#: 实测样本(2026-10-12, 00700): 美银给 2026 财年目标价 780「买入」,
#: 2027/2028 财年只有盈利预测没有评级与目标价(东财原样给 "--")。
_FC_RECORDS = [
    {"财政年度": "2026", "纯利/亏损": 221651.0, "每股盈利": 2912.0, "每股派息": 472.0,
     "证券商": "美银", "评级": "买入", "目标价": 780.0, "更新日期": "2026-10-08"},
    {"财政年度": "2027", "纯利/亏损": 218652.0, "每股盈利": 2874.0, "每股派息": 486.0,
     "证券商": "美银", "评级": "--", "目标价": None, "更新日期": "2026-10-08"},
    {"财政年度": "2026", "纯利/亏损": 214511.0, "每股盈利": 2344.2, "每股派息": 451.7,
     "证券商": "花旗", "评级": "买入", "目标价": 755.0, "更新日期": "2026-10-07"},
    {"财政年度": "2026", "纯利/亏损": None, "每股盈利": None, "每股派息": None,
     "证券商": "某券商", "评级": "中性", "目标价": 600.0, "更新日期": "2026-09-30"},
]


class TestAnalystForecasts:
    def test_maps_columns_and_units(self, monkeypatch):
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        d = prov.analyst_forecasts("00700.HK")
        assert d["source"] == prov.SOURCE_HK_FORECAST
        rows = d["data"]["rows"]
        by = {(r["broker"], r["fiscal_year"]): r for r in rows}

        ml = by[("美银", "2026")]
        assert ml["rating"] == "买入"
        assert ml["rating_bucket"] == "buy"
        assert ml["target_price"] == 780.0
        # 单位换算: 百万港元 → 亿港元, 港仙 → 港元
        assert ml["net_profit_yi_hkd"] == pytest.approx(2216.51)
        assert ml["eps_hkd"] == pytest.approx(29.12)
        assert ml["dps_hkd"] == pytest.approx(4.72)
        # raw 原值保留, 口径可核对
        assert ml["net_profit_raw"] == 221651.0
        assert ml["eps_raw"] == 2912.0

    def test_placeholder_rating_is_none(self, monkeypatch):
        """东财 "--" 表示没给评级 —— 不能当评级文本, 也不能归进任何档位。"""
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        rows = prov.analyst_forecasts("00700.HK")["data"]["rows"]
        by = {(r["broker"], r["fiscal_year"]): r for r in rows}
        assert by[("美银", "2027")]["rating"] is None
        assert by[("美银", "2027")]["rating_bucket"] is None

    def test_nan_becomes_none_not_zero(self, monkeypatch):
        """pandas 空单元格是 NaN —— 必须是 None, 补 0 就是造假。"""
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        rows = prov.analyst_forecasts("00700.HK")["data"]["rows"]
        row = next(r for r in rows if r["broker"] == "某券商")
        assert row["net_profit_raw"] is None
        assert row["eps_hkd"] is None
        assert row["dps_hkd"] is None
        assert row["target_price"] == 600.0

    def test_summary_aggregates_real_broker_views(self, monkeypatch):
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        s = prov.analyst_forecasts("00700.HK")["data"]["summary"]
        assert s["count"] == 4
        assert s["brokers"] == 3
        assert s["target_high"] == 780.0
        assert s["target_low"] == 600.0
        assert s["target_median"] == 755.0
        assert s["rating_dist"] == {"buy": 2, "hold": 1}

    def test_zero_rows_is_failure_not_empty_success(self, monkeypatch):
        """0 行 = 该公司不在这份覆盖表里, 是真失败(实测 00011 抛 No tables found)。"""
        _fake_forecasts(monkeypatch, [])
        with pytest.raises(ValueError):
            prov.analyst_forecasts("00011.HK")

    def test_cached(self, monkeypatch):
        calls = []
        import akshare as ak

        def _spy(symbol):
            calls.append(symbol)
            import pandas as pd
            return pd.DataFrame(_FC_RECORDS)

        monkeypatch.setattr(ak, "stock_hk_profit_forecast_et", _spy)
        prov.analyst_forecasts("00700.HK")
        prov.analyst_forecasts("00700.HK")
        assert len(calls) == 1

    def test_symbol_normalized(self, monkeypatch):
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        assert prov.analyst_forecasts("0700.HK")["data"]["symbol"] == "00700.HK"


class TestForecastsEndpoint:
    def test_happy_path(self, client, monkeypatch):
        _fake_forecasts(monkeypatch, _FC_RECORDS)
        body = client.get("/api/hk/forecasts/00700.HK").json()
        assert body["available"] is True
        assert body["source"] == prov.SOURCE_HK_FORECAST
        assert body["data"]["summary"]["target_high"] == 780.0

    def test_bad_limit(self, client):
        for q in ("limit=0", "limit=201"):
            body = client.get(f"/api/hk/forecasts/00700.HK?{q}").json()
            assert body["available"] is False
            assert body["reason"].startswith("bad_limit")

    def test_zero_rows_is_unavailable(self, client, monkeypatch):
        _fake_forecasts(monkeypatch, [])
        body = client.get("/api/hk/forecasts/00011.HK").json()
        assert body["available"] is False
        assert "provider_error" in body["reason"]

    def test_source_passes_gate(self):
        """新 source 必须登记白名单, 否则闸门会把它拦成假数据。"""
        from app.services.source_gate import is_real

        assert is_real({"source": prov.SOURCE_HK_FORECAST}) is True
