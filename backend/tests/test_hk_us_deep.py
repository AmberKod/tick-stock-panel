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

def _patch_minute(monkeypatch, rows):
    """把通达信扩展市场取数打桩 —— 分钟K 走裸 TCP, 单元测试不应联网。"""
    from app.data_providers import tdx_ex_provider

    def fake(symbol, period="m30", limit=320):
        return {"source": "tdx-ex", "market": "HK", "symbol": symbol, "period": period,
                "requested": limit, "rows": rows, "timezone": "Asia/Shanghai",
                "truncated": False, "max_bars": 700, "cached": False, "note": None}

    monkeypatch.setattr(tdx_ex_provider, "minute_bars", fake)


_MINUTE_ROWS = [
    {"datetime": "2026-10-12 10:30:00", "open": 428.0, "high": 429.0,
     "low": 427.0, "close": 428.5, "volume": 1000.0, "amount": 428500.0},
    {"datetime": "2026-10-12 11:00:00", "open": 428.5, "high": 430.0,
     "low": 428.0, "close": 429.0, "volume": 1200.0, "amount": 514800.0},
]


def test_minute_goes_through_tdx_ex(monkeypatch):
    """分钟K 不再是"无真源": 走通达信扩展市场, 原样透传 payload。"""
    _patch_minute(monkeypatch, _MINUTE_ROWS)
    body = prov.minute_bars("00700.HK", period="m30", limit=10)
    assert body["source"] == "tdx-ex"
    assert body["rows"] == _MINUTE_ROWS
    assert body["timezone"] == "Asia/Shanghai"


def test_minute_empty_rows_is_error_not_empty_table(monkeypatch):
    """取得到连接但 0 行 —— 必须 fail-closed, 不能给 available:true + 空表。"""
    _patch_minute(monkeypatch, [])
    with pytest.raises(ValueError, match="无该标的分钟数据"):
        prov.minute_bars("00700.HK")


def test_minute_node_failure_surfaces(monkeypatch):
    """所有节点连不上是**故障**, 不是"没有源" —— 异常类型必须区分开。"""
    from app.data_providers import tdx_ex_provider

    def boom(symbol, period="m30", limit=320):
        raise tdx_ex_provider.TdxExUnavailableError("全部 2 个节点均失败: timeout")

    monkeypatch.setattr(tdx_ex_provider, "minute_bars", boom)
    with pytest.raises(tdx_ex_provider.TdxExUnavailableError, match="节点均失败"):
        prov.minute_bars("00700.HK")


def test_adjratio_is_unavailable():
    with pytest.raises(CapabilityUnavailableError, match="逐日相同"):
        prov.adjust_ratios("00700.HK")


def test_capabilities_marks_them_false():
    items = prov.capabilities("HK")["items"]
    # 分钟K 2026-10-12 起可用(通达信扩展市场), 复权仍无真源。
    assert items["minute"]["available"] is True
    assert items["minute"]["source"] == "tdx-ex"
    assert "700" in items["minute"]["note"]  # 上限必须写进能力矩阵, 前端据此说明深度
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


# ───────────────────────────────────────────── 当日分时(由 m1 合成)

def _mk_rows(start: str, count: int, *, base: float = 10.0, vol: float = 10.0,
             lot: float = 100.0, minute_step: int = 1):
    """生成一串自洽的分钟 K: amount = 股数 × 价, 便于核对均价。"""
    from datetime import datetime, timedelta

    t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    rows = []
    for i in range(count):
        price = round(base + i * 0.01, 4)
        shares = vol * lot
        rows.append({
            "datetime": (t0 + timedelta(minutes=i * minute_step)).strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(price - 0.01, 4), "high": round(price + 0.02, 4),
            "low": round(price - 0.02, 4), "close": price,
            "volume": vol, "amount": round(shares * price, 4),
        })
    return rows


def test_intraday_takes_last_session_only(monkeypatch):
    """窗口里混着上一个会话的部分 K —— 只能画最后一个会话, 不能连着画线。"""
    prior = _mk_rows("2026-10-09 09:30:00", 3, base=9.0)          # 09:30~09:32
    today = _mk_rows("2026-10-12 09:30:00", 4, base=10.0)         # 09:30~09:33
    _patch_minute(monkeypatch, prior + today)
    d = prov.intraday("00700.HK")
    assert d["session_date"] == "2026-10-12"
    assert d["bars"] == 4 and next(p["t"] for p in d["points"]) == "09:30"
    assert d["prev_close"] == 9.02                                 # 上一会话最后一根收盘
    assert d["prev_close_from"] == "prev_session_close"
    assert d["source"] == "tdx-ex"
    assert d["last"] == 10.03


def _mk_us_session(day: str, *, evening: int = 149, morning: int = 241, base: float = 100.0,
                   evening_start: str = "21:30:00"):
    """模拟上游的**美股**会话形状(2026-10-12 实测):

    同一日期标签下两段 —— 当晚 ``21:30~23:59`` + 次日凌晨 ``00:00~04:00``,
    ``datetime`` 升序时凌晨段在前。别按"-12 小时"去算交易日, 也别直接当跨两天切。
    """
    from datetime import datetime, timedelta

    rows: list[dict] = []
    starts = [f"{day} 00:00:00", f"{day} {evening_start}"]
    counts = [morning, evening]
    price = base
    for start, count in zip(starts, counts, strict=True):
        for i in range(count):
            stamp = (datetime.strptime(start, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=i))
            rows.append({
                "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": price, "high": price + 0.02, "low": price - 0.02,
                "close": round(price + 0.001, 4), "volume": 100.0,
                "amount": round(100.0 * price, 4),
            })
            price += 0.01
    # 上游返回就是 datetime 升序 —— 凌晨段在前, 当晚段在后
    return sorted(rows, key=lambda r: r["datetime"])


def test_intraday_us_session_keeps_midnight_together(monkeypatch):
    """同一个日期标签下的当晚段 + 凌晨段 = **一个**完整会话(390 根 = 6.5 小时)。"""
    _patch_minute(monkeypatch, _mk_us_session("2026-10-09"))
    d = prov.intraday("AAPL.US")
    assert d["session_date"] == "2026-10-09"
    assert d["points"][0]["t"] == "21:30"            # 真实开盘在前
    assert d["points"][-1]["t"] == "04:00"           # 凌晨收盘在后
    assert d["bars"] == 390
    assert d["session_complete"] is True             # 不再被误判成"盘中半截"
    assert d["note"] is None


def test_intraday_us_prev_close_is_previous_session_close(monkeypatch):
    """昨收 = 上一会话**最后一根**(即它凌晨 04:00 那根), 不是按时间排的最后一根。"""
    prior = _mk_us_session("2026-10-08", evening=69, base=90.0)
    today = _mk_us_session("2026-10-09", evening=149, morning=241, base=100.0)
    _patch_minute(monkeypatch, prior + today)
    d = prov.intraday("AAPL.US")
    last_prior = prior[-1]["close"]                  # 10-08 当晚段最后一根(按时间排的末根)
    wanted = max((r for r in prior if r["datetime"][:10] == "2026-10-08"
                  and r["datetime"][11:13] < "20"), key=lambda r: r["datetime"])["close"]
    assert d["prev_close"] == wanted                 # 凌晨段那根才是真收盘
    assert d["prev_close"] != last_prior
    assert d["prev_close_from"] == "prev_session_close"


def test_intraday_winter_session_also_single_session(monkeypatch):
    """冬令时会话(北京时间 22:30~次日05:00)同样是一个会话 —— 最容易判错的一支。"""
    rows = _mk_us_session("2026-01-08", evening=89, morning=301, evening_start="22:30:00")
    _patch_minute(monkeypatch, rows)
    d = prov.intraday("AAPL.US")
    assert d["session_date"] == "2026-01-08"
    assert d["points"][0]["t"] == "22:30" and d["points"][-1]["t"] == "05:00"
    assert d["bars"] == 390 and d["session_complete"] is True


def test_intraday_hk_volume_is_converted_to_shares(monkeypatch):
    """港股成交量单位是"每手100股" —— 不换算, 均价会错两个数量级。"""
    _patch_minute(monkeypatch, _mk_rows("2026-10-12 09:30:00", 2, vol=10.0))
    d = prov.intraday("00700.HK")
    first = d["points"][0]
    assert first["volume"] == 1000.0                  # 10 手 → 1000 股
    assert d["volume_unit"] == "share"
    # 均价 = 累计额 / 累计股数, 这里每根价格不同, 均价应落在两根之间
    assert first["avg"] == pytest.approx(first["price"], abs=0.02)


def test_intraday_us_volume_is_already_shares(monkeypatch):
    rows = _mk_rows("2026-10-09 21:30:00", 2, vol=100.0, lot=1.0)
    _patch_minute(monkeypatch, rows)
    d = prov.intraday("AAPL.US")
    assert d["points"][0]["volume"] == 100.0


def test_intraday_prev_close_falls_back_to_local_daily(monkeypatch):
    """窗口里只有一个会话(没有上一会话可参照) → 借落盘真日K, 并把它的 source 带上。"""
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: (
        [{"date": "2026-10-08", "open": 1.0, "high": 1.0, "low": 1.0, "close": 424.8,
          "volume": 1.0, "amount": 1.0},
         {"date": "2026-10-09", "open": 1.0, "high": 1.0, "low": 1.0, "close": 999.0,
          "volume": 1.0, "amount": 1.0}], "sina_hk_daily"))
    _patch_minute(monkeypatch, _mk_rows("2026-10-09 09:30:00", 3))
    d = prov.intraday("00700.HK")
    assert d["session_date"] == "2026-10-09"
    assert d["prev_close"] == 424.8                   # 取**更早**那天的收盘, 不是当天
    assert d["prev_close_from"] == "daily:sina_hk_daily"
    assert d["source"] == "tdx-ex+sina_hk_daily"      # 组合 source 必须在白名单里
    from app.services.source_gate import is_real
    assert is_real(d) is True


def test_intraday_no_prev_close_is_fail_closed(monkeypatch):
    """拿不到昨收就该明确失败 —— 昨收错了, 整条涨跌幅都是假的。"""
    monkeypatch.setattr(prov, "_daily_rows", lambda s, p, lim: ([], "sina_hk_daily"))
    _patch_minute(monkeypatch, _mk_rows("2026-10-09 09:30:00", 3))
    with pytest.raises(ValueError, match="取不到昨收"):
        prov.intraday("00700.HK")


def test_intraday_incomplete_session_is_marked(monkeypatch):
    """盘中(未收盘)必须标出来: 不是"当天只有这么多分钟"。"""
    _patch_minute(monkeypatch, _mk_rows("2026-10-12 09:30:00", 120))   # 只到 11:29
    d = prov.intraday("00700.HK")
    assert d["session_complete"] is False
    assert d["covered_minutes"] == 120 and d["expected_minutes"] == 390
    assert d["as_of"] == "2026-10-12 11:29:00"
    assert "未收盘" in (d["note"] or "")


def test_intraday_empty_rows_raises(monkeypatch):
    _patch_minute(monkeypatch, [])
    with pytest.raises(ValueError, match="无该标的分钟数据"):
        prov.intraday("00700.HK")


def test_intraday_pct_uses_prev_close(monkeypatch):
    prior = _mk_rows("2026-10-09 09:30:00", 1, base=100.0)   # 收 100.00
    today = _mk_rows("2026-10-12 09:30:00", 1, base=101.0)   # 收 101.00
    _patch_minute(monkeypatch, prior + today)
    d = prov.intraday("00700.HK")
    assert d["pct"] == pytest.approx(1.0)
    assert d["points"][0]["pct"] == pytest.approx(1.0)


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


def test_api_minute_ok(client, monkeypatch):
    _patch_minute(monkeypatch, _MINUTE_ROWS)
    resp = client.get("/api/us/deep/minute/AAPL.US?period=m30&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True and body["source"] == "tdx-ex"
    assert len(body["rows"]) == 2


def test_api_minute_failure_is_http200_with_reason(client, monkeypatch):
    """fail-closed: 节点全挂也是 200 + available:false, 不抛 5xx。"""
    from app.data_providers import tdx_ex_provider

    def boom(symbol, period="m30", limit=320):
        raise tdx_ex_provider.TdxExUnavailableError("全部 2 个节点均失败: timeout")

    monkeypatch.setattr(tdx_ex_provider, "minute_bars", boom)
    resp = client.get("/api/us/deep/minute/AAPL.US")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and "provider_error" in body["reason"]


def test_api_minute_rejects_unregistered_source(client, monkeypatch):
    """provider 若谎报 source(如 demo), source_gate 必须拦下。"""
    _patch_minute(monkeypatch, _MINUTE_ROWS)
    from app.data_providers import tdx_ex_provider

    orig = tdx_ex_provider.minute_bars

    def fake(symbol, period="m30", limit=320):
        payload = orig(symbol, period=period, limit=limit)
        payload["source"] = "demo"
        return payload

    monkeypatch.setattr(tdx_ex_provider, "minute_bars", fake)
    body = client.get("/api/us/deep/minute/AAPL.US").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


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


def test_api_intraday_ok(client, monkeypatch):
    prior = _mk_rows("2026-10-09 09:30:00", 2, base=9.0)
    today = _mk_rows("2026-10-12 09:30:00", 3, base=10.0)
    _patch_minute(monkeypatch, prior + today)
    body = client.get("/api/hk/deep/intraday/00700.HK").json()
    assert body["available"] is True and body["source"] == "tdx-ex"
    assert body["session_date"] == "2026-10-12" and len(body["points"]) == 3
    assert body["prev_close_from"] == "prev_session_close"


def test_api_intraday_us_ok(client, monkeypatch):
    _patch_minute(monkeypatch, _mk_us_session("2026-10-09"))
    body = client.get("/api/us/deep/intraday/AAPL.US").json()
    assert body["available"] is True and body["market"] == "US"
    assert body["session_complete"] is True


def test_api_intraday_failure_is_http200_with_reason(client, monkeypatch):
    _patch_minute(monkeypatch, [])
    resp = client.get("/api/us/deep/intraday/AAPL.US")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and "provider_error" in body["reason"]


def test_api_intraday_bad_limit(client):
    body = client.get("/api/hk/deep/intraday/00700.HK?limit=0").json()
    assert body["available"] is False and "bad_limit" in body["reason"]


def test_api_intraday_rejects_unregistered_source(client, monkeypatch):
    """provider 谎报 source 时闸门必须拦 —— 分时是新端点, 同样不许例外。

    为什么直接替换 provider 而不是篡改上游 payload: intraday 会**自己重算**
    source 字段, 上游 minute_bars 里改成假源名传不到响应 —— 必须在合成之后那层动手。
    """
    monkeypatch.setattr(
        prov, "intraday",
        lambda s, limit=700: {"source": "fail-http-500", "points": [{"t": "09:30", "price": 1}]})
    body = client.get("/api/us/deep/intraday/AAPL.US").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


def test_capabilities_includes_intraday():
    for market in ("HK", "US"):
        item = prov.capabilities(market)["items"]["intraday"]
        assert item["available"] is True
        assert item["source"] == "tdx-ex"
        assert item["derivation"] == "composed_from_m1"


def test_api_source_gate_blocks_fake_source(client, monkeypatch):
    """provider 被改成 demo 源时, API 必须拦下而不是照发。"""
    monkeypatch.setattr(prov, "period_bars", lambda *a, **k: {"source": "us_demo", "rows": []})
    body = client.get("/api/us/deep/period/AAPL.US?period=week").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


# ───────────────────────────────────────────── 快照 / 五档盘口 API


def _patch_snapshot(monkeypatch, *, body=None):
    from app.data_providers import tdx_ex_provider

    def snapshot(symbol):
        base = {"source": "tdx-ex", "market": "HK", "symbol": symbol, "name": "腾讯控股",
                "prev_close": 424.8, "open": 428.4, "high": 430.4, "low": 423.4,
                "last": 425.8, "avg_price": 427.05, "volume": 14011738.0,
                "amount": 5983558144.0, "turnover_pct": 0.01541,
                "inside_volume": 7005907.0, "outside_volume": 7005831.0,
                "activity": 23033.0, "change": 1.0, "pct": 0.2354,
                "volume_unit": "share", "timezone": "Asia/Shanghai",
                "server_time": "2026-10-12 16:08:24",
                "derived": ["turnover_pct", "change", "pct"], "cached": False}
        return {**base, **(body or {})}

    def orderbook(symbol):
        mk = {"source": "tdx-ex", "market": "US", "symbol": symbol, "prev_close": 336.64,
              "last": 336.68, "bid_levels": 1, "ask_levels": 1, "depth_available": True,
              "volume_unit": "share", "current_volume": 14.0, "total_volume": 116649.0,
              "inside_volume": 55615.0, "outside_volume": 61034.0,
              "timezone": "Asia/Shanghai", "note": None, "cached": False}
        def level(p: float, v: float) -> dict:
            return {"price": p, "volume": v}

        mk["bids"] = [level(336.67, 6.0)] + [level(0.0, 0.0)] * 4
        mk["asks"] = [level(336.84, 6.0)] + [level(0.0, 0.0)] * 4
        return mk

    monkeypatch.setattr(tdx_ex_provider, "snapshot", snapshot)
    monkeypatch.setattr(tdx_ex_provider, "orderbook", orderbook)


def test_api_snapshot_ok(client, monkeypatch):
    _patch_snapshot(monkeypatch)
    body = client.get("/api/hk/deep/snapshot/00700.HK").json()
    assert body["available"] is True
    assert body["prev_close"] == 424.8 and body["last"] == 425.8
    assert body["source"] == "tdx-ex"


def test_api_snapshot_missing_last_key_is_fail_closed(client, monkeypatch):
    """last 缺失(该标的拿不到价)必须判不可用, 不能 available:true 带着 None。"""
    from app.data_providers import tdx_ex_provider

    def snapshot(symbol):
        return {"source": "tdx-ex", "prev_close": 424.8}

    monkeypatch.setattr(tdx_ex_provider, "snapshot", snapshot)
    body = client.get("/api/hk/deep/snapshot/00700.HK").json()
    assert body["available"] is False and "last" in body["reason"]


def test_api_snapshot_upstream_node_down(client, monkeypatch):
    """节点全挂 = 可重试的故障, 不是能力缺失: reason 走 provider_error。"""
    from app.data_providers import tdx_ex_provider
    from app.data_providers.tdx_ex_provider import TdxExUnavailableError

    def snapshot(symbol):
        raise TdxExUnavailableError("全部 2 个节点均失败: timeout")

    monkeypatch.setattr(tdx_ex_provider, "snapshot", snapshot)
    body = client.get("/api/hk/deep/snapshot/00700.HK").json()
    assert body["available"] is False and body["reason"].startswith("provider_error")


def test_api_orderbook_ok_us(client, monkeypatch):
    _patch_snapshot(monkeypatch)
    body = client.get("/api/us/deep/orderbook/AAPL.US").json()
    assert body["available"] is True
    assert body["depth_available"] is True and body["bids"][0]["price"] > 0


def test_api_orderbook_hk_zero_depth_still_200_with_note(client, monkeypatch):
    """港股五档恒 0: 仍是 200 + available:true(总量/内外盘为真), 靠 note 说清没档。"""
    _patch_snapshot(monkeypatch)

    from app.data_providers import tdx_ex_provider

    def orderbook(symbol):
        return {"source": "tdx-ex", "market": "HK", "symbol": symbol, "prev_close": 424.8,
                "last": 425.8, "bids": [{"price": 0.0, "volume": 0.0}] * 5,
                "asks": [{"price": 0.0, "volume": 0.0}] * 5,
                "bid_levels": 0, "ask_levels": 0, "depth_available": False,
                "volume_unit": "share", "current_volume": 1253000.0,
                "total_volume": 14011738.0, "inside_volume": 7005907.0,
                "outside_volume": 7005831.0, "timezone": "Asia/Shanghai",
                "note": "上游(MAC-EX)对该市场不返回买卖档", "cached": False}

    monkeypatch.setattr(tdx_ex_provider, "orderbook", orderbook)
    body = client.get("/api/hk/deep/orderbook/00700.HK").json()
    assert body["available"] is True
    assert body["depth_available"] is False and "不返回买卖档" in body["note"]


def test_api_snapshot_source_gate_rejects_unregistered(client, monkeypatch):
    _patch_snapshot(monkeypatch, body={"source": "fail-http-500"})
    body = client.get("/api/hk/deep/snapshot/00700.HK").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


def test_capabilities_lists_snapshot_and_orderbook():
    caps = prov.capabilities("US")
    assert caps["items"]["snapshot"]["available"] is True
    assert caps["items"]["orderbook"]["source"] == prov.SOURCE_TDX_EX


# ───────────────────────────────────────────── 板块 API(所属 / 成分股 / 榜单)


def _patch_boards(monkeypatch):
    from app.data_providers import tdx_ex_provider

    def belong_boards(symbol):
        return {"source": "tdx-ex", "market": "HK", "symbol": symbol,
                "boards": [{"board_code": "HK0279", "board_name": "港股-远程办公",
                            "close": 1152.46, "pre_close": 1125.81, "pct": 2.3672}],
                "derived": ["pct"], "cached": False}

    def board_members(board_code, limit=50, start=0):
        return {"source": "tdx-ex", "board_code": board_code,
                "members": [{"code": "PANW", "name": "派拓网络", "close": 419.19,
                             "pre_close": 418.78, "pct": 0.0979, "amount": 16096128.0,
                             "volume": 38272.0, "vol_ratio": 3.3848,
                             "market_cap": 342897426432.0, "total_shares": 818000000.0,
                             "eps": 4.09, "pe_dynamic": 100.0, "pe_ttm": 102.0,
                             "dividend_yield": 3.59, "net_assets": 33.73}],
                "requested": limit, "start": start,
                "derived": ["pct", "total_shares"], "note": None, "cached": False}

    def board_list(kind="US_HY", limit=50):
        return {"source": "tdx-ex", "kind": kind,
                "boards": [{"board_code": "US0212", "board_name": "美股-大麻",
                            "close": 434.54, "pre_close": 424.84, "pct": 2.2832,
                            "rise_speed": 0.0, "leader_code": "GRWG",
                            "leader_name": "GrowGeneration", "leader_pct": 16.3067}],
                "derived": ["pct", "leader_pct"], "cached": False}

    monkeypatch.setattr(tdx_ex_provider, "belong_boards", belong_boards)
    monkeypatch.setattr(tdx_ex_provider, "board_members", board_members)
    monkeypatch.setattr(tdx_ex_provider, "board_list", board_list)


def test_api_boards_ok(client, monkeypatch):
    _patch_boards(monkeypatch)
    body = client.get("/api/hk/deep/boards/00700.HK").json()
    assert body["available"] is True
    assert body["boards"][0]["board_name"] == "港股-远程办公"
    assert body["boards"][0]["pct"] == pytest.approx(2.3672, abs=1e-3)


def test_api_board_members_ok(client, monkeypatch):
    _patch_boards(monkeypatch)
    body = client.get("/api/us/deep/board/US0026/members?limit=5").json()
    assert body["available"] is True
    m = body["members"][0]
    assert m["code"] == "PANW" and m["total_shares"] == 818_000_000


def test_api_boardlist_ok(client, monkeypatch):
    _patch_boards(monkeypatch)
    body = client.get("/api/us/deep/boardlist?kind=US_HY&limit=10").json()
    assert body["available"] is True
    assert body["boards"][0]["leader_pct"] == pytest.approx(16.3067, abs=1e-3)


def test_api_boardlist_bad_kind_is_fail_closed(client, monkeypatch):
    """bad_kind 走 provider 的 ValueError ⇒ available:false + provider_error, 不 5xx。"""
    _patch_boards(monkeypatch)

    from app.data_providers import tdx_ex_provider

    def board_list(kind="US_HY", limit=50):
        raise ValueError("bad_kind: NOWHERE")

    monkeypatch.setattr(tdx_ex_provider, "board_list", board_list)
    body = client.get("/api/us/deep/boardlist?kind=NOWHERE").json()
    assert body["available"] is False and "bad_kind" in body["reason"]


def test_api_boards_source_gate(client, monkeypatch):
    _patch_boards(monkeypatch)

    from app.data_providers import tdx_ex_provider

    def belong_boards(symbol):
        return {"source": "fail-http-500", "boards": []}

    monkeypatch.setattr(tdx_ex_provider, "belong_boards", belong_boards)
    body = client.get("/api/hk/deep/boards/00700.HK").json()
    assert body["available"] is False and "source_gate_rejected" in body["reason"]


# ───────────────────────────────────────────── 逐笔成交 / 多日分钟序列 端点


class _TxRec:
    def __init__(self, hour=15, minute=59, second=0, price=425000, volume=1500):
        self.hour, self.minute, self.second = hour, minute, second
        self.price, self.volume = price, volume
        self.zengcang, self.nature = 0, 0


class _MinuteRec:
    def __init__(self, hour=9, minute=30, price=416.8, avg=415.6, volume=859434):
        self.hour, self.minute = hour, minute
        self.price, self.avg_price = price, avg
        self.volume, self.open_interest = volume, 0


def _patch_exec_result(monkeypatch, rows=None):
    from app.data_providers import tdx_ex_provider as tx

    monkeypatch.setattr(tx, "_exec_once",
                        lambda host, factory: (factory(), rows if rows is not None else [_TxRec()])[1])


def test_api_transactions_hk_ok(client, monkeypatch):
    """港股逐笔: 0x23FC, 价格整数/1000 = 港元。"""
    _patch_exec_result(monkeypatch, [_TxRec(9, 30, price=416800), _TxRec(15, 59, price=425000)])
    body = client.get("/api/hk/deep/transactions/00700.HK?limit=2").json()
    assert body["available"] is True
    assert [r["price"] for r in body["rows"]] == [416.8, 425.0]
    assert body["protocol"] == "0x23FC/0x2406"


def test_api_transactions_bad_limit_is_fail_closed(client):
    body = client.get("/api/hk/deep/transactions/00700.HK?limit=5000").json()
    assert body["available"] is False and "bad_limit" in body["reason"]


def test_api_transactions_bad_date_is_fail_closed(client, monkeypatch):
    _patch_exec_result(monkeypatch)
    body = client.get("/api/hk/deep/transactions/00700.HK?date=2026-13-01").json()
    assert body["available"] is False and "bad_date" in body["reason"]


def test_api_transactions_node_down_is_http200(client, monkeypatch):
    from app.data_providers import tdx_ex_provider as tx

    def boom(host, factory):
        raise tx.TdxExUnavailableError("all nodes down")

    monkeypatch.setattr(tx, "_exec_once", boom)
    body = client.get("/api/hk/deep/transactions/00700.HK").json()
    assert body["available"] is False and "provider_error" in body["reason"]


class _RngRec:
    """上游 0x240D 一根: **真实** open/high/low/close + trade(该分钟成交量)。"""

    def __init__(self, ymd, hour=9, minute=31, open_=100.0, high=101.0,
                 low=99.0, close=100.5, trade=1000):
        self.year, self.month, self.day = ymd // 10000, (ymd // 100) % 100, ymd % 100
        self.hour, self.minute = hour, minute
        self.open, self.high, self.low, self.close = open_, high, low, close
        self.trade, self.position, self.amount = trade, 0, 0.0


class _RngFrame:
    """日K 日历替身 —— `_rows_from_frame` 只用到 to_dict('records')。"""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        return list(self._rows)


def _patch_minuterange(monkeypatch, n_days=2, bars_per_day=10):
    """同时桩掉日K 交易日历与 0x240D, 返回期望下发的日期列表(升序)。"""
    from app.data_providers import tdx_ex_provider as tx

    cands = list(reversed(tx._recent_trading_candidates(40)))
    cal = [{"datetime": f"{d // 10000:04d}-{(d // 100) % 100:02d}-{d % 100:02d} 00:00:00",
            "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
            "close": 100.0 + i, "vol": 1000, "amount": 100000.0}
           for i, d in enumerate(cands)]
    dates = [r["datetime"][:10].replace("-", "") for r in cal][-n_days:]
    monkeypatch.setattr(tx, "_fetch_once", lambda *a, **k: _RngFrame(cal))

    def fake_exec(host, factory):
        cmd = factory()
        out: list[object] = []
        for d in dates:
            if int(cmd.start_date) <= int(d) <= int(cmd.end_date):
                out.extend(
                    _RngRec(int(d), 9, 31 + i, open_=100.0 + i, high=101.0 + i,
                            low=99.0 + i, close=100.5 + i)
                    for i in range(bars_per_day))
        return out

    monkeypatch.setattr(tx, "_exec_once", fake_exec)
    return dates


def test_api_minuterange_ok(client, monkeypatch):
    """多日分钟蜡烛: 0x240D 每根都给真 OHLC; N 分钟档由真实 m1 聚合且必须声明。"""
    _patch_minuterange(monkeypatch, 2)
    body = client.get("/api/hk/deep/minuterange/00700.HK?days=2&period=m5").json()
    assert body["available"] is True
    assert body["protocol"] == "0x240D"
    assert len(body["days"]) == 2
    assert all(len(d["bars"]) == 2 for d in body["days"])     # 10 根 m1 → 2 根 m5
    assert body["ohlc"] == "aggregated_from_m1"
    assert body["derived"] == ["open", "high", "low", "close"]


def test_api_minuterange_m1_is_native_ohlc(client, monkeypatch):
    """m1 档不再有"四价同值"这回事 —— 开高低收各自不同, 但**没有均价**。"""
    _patch_minuterange(monkeypatch, 1)
    body = client.get("/api/hk/deep/minuterange/00700.HK?days=1").json()
    assert body["ohlc"] == "native" and body["derived"] == []
    bar = body["days"][0]["bars"][0]
    assert (bar["open"], bar["high"], bar["low"], bar["close"]) == (100.0, 101.0, 99.0, 100.5)
    assert body["has_avg"] is False and bar["avg"] is None


def test_api_minuterange_bad_days_is_fail_closed(client):
    body = client.get("/api/hk/deep/minuterange/00700.HK?days=99").json()
    assert body["available"] is False and "bad_days" in body["reason"]


def test_capabilities_lists_transactions_and_minute_range():
    caps = prov.capabilities("HK")["items"]
    assert caps["transactions"]["available"] is True
    assert caps["minute_range"]["available"] is True
    assert "0x23FC" in caps["transactions"]["note"]
    assert "0x240D" in caps["minute_range"]["note"]


# ───────────────────────────────────────────── 多日分时端点(0x240C + 日K 昨收)


class _MdBar:
    """上游 0x240C 一根: 只有 price / avg_price / volume。"""

    def __init__(self, hour=9, minute=30, price=100.0, avg=99.5, volume=1000):
        self.hour, self.minute = hour, minute
        self.price, self.avg_price = price, avg
        self.volume, self.open_interest = volume, 0


class _MdFrame:
    """日K DataFrame 替身 —— `_rows_from_frame` 只用到 to_dict('records')。"""

    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient):
        return list(self._rows)


def _patch_multiday(monkeypatch, n_days=3):
    """桩掉 0x240C 分钟包 + 日K 日历, 返回期望下发的日期列表(升序)。"""
    from app.data_providers import tdx_ex_provider as tx

    # 候选日是**由新到旧**, 必须先切再排: 先 reversed 再切片会拿到最老的一批,
    # 而 minute_range 扫的是最近几天 —— 于是全 miss, 报"均无分钟数据"。
    cands = tx._recent_trading_candidates(n_days + 10)
    dates = sorted(cands[:n_days + 10])          # 最近 n+10 个工作日, 升序 int ymd
    close = {d: 100.0 + i for i, d in enumerate(dates)}
    targets = {str(d) for d in dates[-n_days:]}

    def fake_exec(host, factory):
        cmd = factory()
        ymd = str(getattr(cmd, "date", ""))
        if ymd not in targets:
            return []
        c = close[int(ymd)]
        return [_MdBar(9, 30, c - 1.0), _MdBar(15, 59, c)]

    def fake_fetch(host, market, code, period, count):
        return _MdFrame([
            {"datetime": f"{d // 10000:04d}-{(d // 100) % 100:02d}-{d % 100:02d} 00:00:00",
             "open": close[d], "high": close[d], "low": close[d],
             "close": close[d], "vol": 100, "amount": 1000.0}
            for d in dates])

    monkeypatch.setattr(tx, "_exec_once", fake_exec)
    monkeypatch.setattr(tx, "_fetch_once", fake_fetch)
    return [f"{d // 10000:04d}-{(d // 100) % 100:02d}-{d % 100:02d}" for d in dates[-n_days:]]


def test_api_multiday_hk_ok(client, monkeypatch):
    want = _patch_multiday(monkeypatch, n_days=3)
    body = client.get("/api/hk/deep/multiday/00700.HK?days=3").json()
    assert body["available"] is True
    assert [d["date"] for d in body["days"]] == want
    assert body["prev_close_source"] == "daily_kline"
    # 昨收链: 较晚那天的昨收 == 相邻较早那天的收盘
    assert body["days"][1]["prev_close"] == body["days"][0]["close"]


def test_api_multiday_us_is_available_now(client, monkeypatch):
    """美股曾因上游块与日期不对应而 fail-closed; 换 0x240C 按日循环后打通。"""
    want = _patch_multiday(monkeypatch, n_days=3)
    body = client.get("/api/us/deep/multiday/AAPL.US?days=3").json()
    assert body["available"] is True
    assert [d["date"] for d in body["days"]] == want
    assert body["protocol"] == "0x240C + daily_kline"


def test_api_multiday_bad_days_is_fail_closed(client):
    body = client.get("/api/us/deep/multiday/AAPL.US?days=99").json()
    assert body["available"] is False and "bad_days" in body["reason"]


def test_api_multiday_upstream_down_is_http200(client, monkeypatch):
    from app.data_providers import tdx_ex_provider as tx

    tx.clear_cache()   # 同 symbol+days 上一个用例的结果还在, 不清就命中缓存了

    def boom(host, factory):
        raise tx.TdxExUnavailableError("all nodes down")

    monkeypatch.setattr(tx, "_exec_once", boom)
    body = client.get("/api/us/deep/multiday/AAPL.US?days=3").json()
    assert body["available"] is False and "provider_error" in body["reason"]


def test_capabilities_multiday_available_for_both_markets():
    assert prov.capabilities("HK")["items"]["multiday"]["available"] is True
    assert prov.capabilities("US")["items"]["multiday"]["available"] is True
