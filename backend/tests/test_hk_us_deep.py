"""港美股深度 K 线护栏测试。

背景: 上一轮是把「频次/交易日 mock 缺失」这类问题当成规范 - 本轮相反,
这里锁的是 **腾讯会静默给错数据** 的几种形态 —— 它们不报错、不抛异常,
按常规"有返回就是成功"的写法会直接把一根日 K 画成一根季 K。

所有用例 mock 掉网络与本地读取, 不依赖外网。
"""
from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.data_providers import hk_us_deep_provider as prov
from app.data_providers.hk_us_deep_provider import CapabilityUnavailableError
from app.main import app


@pytest.fixture(autouse=True)
def _clean_cache():
    """模块级缓存跨用例残留 ⇒ 上用例的 mock 结果被下用例命中, 断言会假通过。"""
    prov.clear_cache()
    yield
    prov.clear_cache()


@pytest.fixture()
def client():
    # 必须声明本机地址, 否则被「未设密码 + 非内网」守卫判 403
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        yield c


# ───────────────────────────────────────────── 符号 / 解析

def test_tx_symbol_hk_zero_pads():
    """0700.HK → hk00700 (腾讯港股代码固定 5 位)。"""
    assert prov._tx_symbol("0700.HK") == "hk00700"
    assert prov._tx_symbol("00700.HK") == "hk00700"


def test_tx_symbol_us_keeps_dot():
    """BRK.A.US → usBRK.A (腾讯保留点号)。"""
    assert prov._tx_symbol("AAPL.US") == "usAAPL"
    assert prov._tx_symbol("BRK.A.US") == "usBRK.A"


def test_bad_suffix_raises():
    with pytest.raises(ValueError):
        prov._tx_symbol("AAPL")


def test_tx_row_column_order():
    """腾讯行第 2 列是开、第 3 列是收 —— 写成 OHLC 顺序图会看着"像对的"。"""
    row = prov._parse_tx_row(["2026-10-09", "1.0", "9.0", "10.0", "0.5", "100"])
    assert row["open"] == 1.0 and row["close"] == 9.0
    assert row["high"] == 10.0 and row["low"] == 0.5


def test_pick_node_tolerates_dotted_key():
    """指数响应 key 是 us.DJI 带点, 按 usDJI 取会静默拿到空。"""
    node = prov._pick_node({"us.DJI": {"day": [["2026-10-09", 1, 2, 3, 4, 5]]}}, "usDJI")
    assert node == {"day": [["2026-10-09", 1, 2, 3, 4, 5]]}


def test_pick_node_tolerates_r_prefix():
    assert prov._pick_node({"r_hk00700": {"week": []}}, "hk00700") == {"week": []}


# ───────────────────────────────────────────── 周期 K: 腾讯原生路径

def _patch_tx(monkeypatch, rows_by_period: dict, *, node=None):
    """拦截腾讯请求。rows_by_period: {'week': [...]} → 响应挂到同名键下。"""
    sent: dict[str, list] = {"urls": []}

    def fake(url, timeout=None):
        sent["urls"].append(url)
        # URL 是百分号编码的 (param=hk00700%2Cweek%2C%2C%2C10), 必须先解码再切
        param = unquote(url.split("param=")[1])
        tx, period = param.split(",")[0], param.split(",")[1]
        body = node if node is not None else {period: rows_by_period.get(period, [])}
        return {"code": 0, "data": {tx: body}}

    monkeypatch.setattr(prov, "_request_json", fake)
    return sent


_WEEK_ROWS = [
    ["2026-09-25", "10", "11", "12", "9", "100"],
    ["2026-10-02", "11", "13", "14", "10", "120"],
]


def test_hk_week_uses_native_tencent(monkeypatch):
    sent = _patch_tx(monkeypatch, {"week": _WEEK_ROWS})
    d = prov.period_bars("00700.HK", period="week", limit=10)
    assert "hk00700,week" in unquote(sent["urls"][0])
    assert d["source"] == "tencent"
    assert d["derived_from"] == "week"          # 原生, 不是聚合
    assert [r["date"] for r in d["rows"]] == ["2026-09-25", "2026-10-02"]


def test_hk_month_native(monkeypatch):
    _patch_tx(monkeypatch, {"month": _WEEK_ROWS})
    assert prov.period_bars("00700.HK", period="month")["derived_from"] == "month"


@pytest.mark.parametrize("period", ["quarter", "year"])
def test_us_periods_never_use_tencent(monkeypatch, period):
    """美股任何周期都不许走腾讯 —— 实测腾讯对该市场一律只回 1 根。"""
    called = {"n": 0}

    monkeypatch.setattr(prov, "_request_json", lambda url, timeout=None: called.update(
        n=called["n"] + 1) or {"data": {}})
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (_DAILY, "sina_hk_daily"))
    prov.period_bars("AAPL.US", period=period, limit=10)
    assert called["n"] == 0


@pytest.mark.parametrize("period", ["quarter", "year"])
def test_hk_quarter_year_not_native(monkeypatch, period):
    """港股 quarter/year 腾讯静默只回 1 根 ⇒ 必须落到日 K 聚合, 不能直接用原返回值。"""
    sent = _patch_tx(monkeypatch, {period: [["2026-10-09", "1", "2", "3", "0.5", "9"]]})
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (_DAILY, "sina_hk_daily"))
    d = prov.period_bars("00700.HK", period=period, limit=10)
    assert not sent["urls"], "腾讯给的 1 根不能被采信"
    assert d["derived_from"] == "daily"


def test_hk_native_empty_raises(monkeypatch):
    _patch_tx(monkeypatch, {"week": []})
    with pytest.raises(ValueError, match="返回空"):
        prov.period_bars("00700.HK", period="week")


def test_bad_period_raises():
    with pytest.raises(ValueError, match="period 必须是"):
        prov.period_bars("00700.HK", period="10min")


# ───────────────────────────────────────────── 聚合

_DAILY = [
    {"date": "2026-09-28", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
     "volume": 100.0, "amount": 1000.0},
    {"date": "2026-09-29", "open": 11.0, "high": 15.0, "low": 8.0, "close": 14.0,
     "volume": 200.0, "amount": 2000.0},
    {"date": "2026-09-30", "open": 14.0, "high": 14.0, "low": 13.0, "close": 13.0,
     "volume": 50.0, "amount": 500.0},
    {"date": "2026-10-06", "open": 20.0, "high": 21.0, "low": 19.0, "close": 21.0,
     "volume": 70.0, "amount": 700.0},
]


def test_aggregate_month_ohlcv():
    rows = prov.aggregate(_DAILY, "month")
    assert len(rows) == 2
    sep = rows[0]
    assert sep["open"] == 10.0 and sep["close"] == 13.0   # 首开 / 末收
    assert sep["high"] == 15.0 and sep["low"] == 8.0
    assert sep["volume"] == 350.0 and sep["amount"] == 3500.0
    assert sep["date"] == "2026-09-30"                     # 区间最后一根交易日


def test_aggregate_missing_volume_is_none_not_zero():
    """缺列的持有 None —— 求和成 0 会被读成"该月零成交"。"""
    daily = [{**r, "volume": None} for r in _DAILY]
    rows = prov.aggregate(daily, "month")
    assert rows[0]["volume"] is None


def test_aggregate_year_and_quarter_bucket():
    assert len(prov.aggregate(_DAILY, "year")) == 1
    assert len(prov.aggregate(_DAILY, "quarter")) == 2   # Q3 + Q4


def test_us_period_source_from_local_rows(monkeypatch):
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (_DAILY, "sina_hk_daily"))
    d = prov.period_bars("00700.HK", period="quarter")
    assert d["source"] == "sina_hk_daily"      # 来源必须穿下来, 不许写死
    assert d["derived_from"] == "daily"


def test_day_period_is_passthrough(monkeypatch):
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (_DAILY, "sina_hk_daily"))
    rows = prov.period_bars("AAPL.US", period="day")["rows"]
    assert len(rows) == len(_DAILY)


# ───────────────────────────────────────────── 指数

def test_index_kline_normalizes_code(monkeypatch):
    sent = _patch_tx(monkeypatch, {}, node={"day": _WEEK_ROWS})
    d = prov.index_bars("^hsi")
    assert d["code"] == "HSI" and "hkHSI,day" in unquote(sent["urls"][0])
    assert d["name"] == "恒生指数"


def test_index_unknown_code_raises():
    with pytest.raises(ValueError, match="指数代码必须是"):
        prov.index_bars("XYZ")


def test_index_source_is_real(monkeypatch):
    from app.services.source_gate import is_real
    _patch_tx(monkeypatch, {}, node={"day": _WEEK_ROWS})
    assert is_real(prov.index_bars("DJI")) is True


# ───────────────────────────────────────────── 明确不可用

def test_minute_is_unavailable():
    with pytest.raises(CapabilityUnavailableError, match="param error"):
        prov.minute_bars("00700.HK")


def test_adjratio_is_unavailable():
    with pytest.raises(CapabilityUnavailableError, match="逐日相同"):
        prov.adjust_ratios("00700.HK")


def test_capabilities_marks_them_false():
    items = prov.capabilities("HK")["items"]
    assert items["minute"]["available"] is False and items["minute"]["reason"]
    assert items["adjratio"]["available"] is False and items["adjratio"]["reason"]
    assert items["week"]["available"] is True
    assert items["week"]["derivation"] == "native"
    assert items["quarter"]["derivation"] == "aggregated_from_daily"


def test_capabilities_us_has_no_native_period():
    items = prov.capabilities("US")["items"]
    assert all(items[p]["derivation"] != "native" for p in ("week", "month", "quarter", "year"))


def test_capabilities_bad_market():
    with pytest.raises(ValueError, match="market 必须是"):
        prov.capabilities("JP")


# ───────────────────────────────────────────── API 层

def test_api_hk_period_ok(client, monkeypatch):
    _patch_tx(monkeypatch, {"week": _WEEK_ROWS})
    body = client.get("/api/hk/deep/period/00700.HK?period=week&limit=10").json()
    assert body["available"] is True and body["source"] == "tencent"
    assert len(body["rows"]) == 2


def test_api_us_period_derived(client, monkeypatch):
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (_DAILY, "yfinance"))
    body = client.get("/api/us/deep/period/AAPL.US?period=month").json()
    assert body["available"] is True
    assert body["derived_from"] == "daily"


def test_api_minute_is_http200_with_reason(client):
    """fail-closed: 拿不到也是 200 + available:false, 不抛 5xx。"""
    resp = client.get("/api/us/deep/minute/AAPL.US")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "capability_unavailable" in body["reason"]


def test_api_adjratio_is_http200_with_reason(client):
    body = client.get("/api/hk/deep/adjratio/00700.HK").json()
    assert body["available"] is False and "capability_unavailable" in body["reason"]


def test_api_bad_limit(client):
    body = client.get("/api/hk/deep/period/00700.HK?limit=5").json()
    assert body["available"] is False and "bad_limit" in body["reason"]


def test_api_index_ok(client, monkeypatch):
    _patch_tx(monkeypatch, {}, node={"day": _WEEK_ROWS})
    body = client.get("/api/hk/deep/index/HSI").json()
    assert body["available"] is True and body["code"] == "HSI"


def test_api_capabilities_endpoint(client):
    body = client.get("/api/us/deep/capabilities").json()
    assert body["market"] == "US" and "minute" in body["items"]


def test_api_source_gate_blocks_fake_source(client, monkeypatch):
    """provider 被改成 demo 源时, API 必须拦下而不是照发。"""
    monkeypatch.setattr(prov, "period_bars", lambda *a, **k: {"source": "us_demo", "rows": []})
    body = client.get("/api/us/deep/period/AAPL.US?period=week").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]
