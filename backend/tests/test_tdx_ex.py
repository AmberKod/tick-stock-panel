"""通达信扩展市场(MAC-EX)分钟K provider —— 单元测试(全程打桩, 不联网)。

不联网的原因不只是速度: 上游是裸 TCP + 有状态登录, 单测里建真连接既不稳定
也会给对方服务器添无谓负担。这里只验证**我们这层的契约**: 参数校验、截断、
量化、缓存、失败语义。
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from app.data_providers import tdx_ex_provider as prov
from app.data_providers.tdx_ex_provider import (
    MAX_BARS_PER_REQUEST,
    TdxExUnavailableError,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    prov.clear_cache()
    yield
    prov.clear_cache()


def _frame(rows: int = 3):
    """上游 DataFrame 的形状: float32 毛刺价格 + datetime64 时间。"""
    return pd.DataFrame({
        # 336.6400146484375 这种毛刺是上游 float32 编码的直接产物
        "datetime": pd.to_datetime([f"2026-10-09 0{i}:00:00" for i in range(rows)]),
        "open": [336.6400146484375] * rows,
        "high": [337.8399963378906] * rows,
        "low": [335.1000061035156] * rows,
        "close": [336.8900146484375] * rows,
        "vol": [1640630.0] * rows,
        "amount": [552856576.0] * rows,
        "float_shares": [1459418.0] * rows,
    })


def _patch_fetch(monkeypatch, frame=None, exc=None):
    """桩掉**单次连接**(不是 _fetch 本身) —— 这样 _fetch 的"逐个节点降级"
    逻辑仍在被测范围内。
    """
    calls: list[tuple] = []

    def fake(host: str, market: str, code: str, period, count: int):
        calls.append((market, code, int(period), count))
        if exc is not None:
            raise exc
        return frame if frame is not None else _frame()

    monkeypatch.setattr(prov, "_fetch_once", fake)
    return calls


# ───────────────────────────────────────────── 参数归一化 / 校验

@pytest.mark.parametrize("symbol,market,code", [
    ("00700.HK", "HK", "00700"),
    ("00700.hk", "HK", "00700"),
    ("AAPL.US", "US", "AAPL"),
    (" 00700.HK ", "HK", "00700"),
])
def test_split(symbol, market, code):
    assert prov._split(symbol) == (market, code)


@pytest.mark.parametrize("bad", ["AAPL", "", "00700", "00700.JP", ".HK"])
def test_split_rejects_bad_symbol(bad):
    with pytest.raises(ValueError, match=r"\.HK/\.US"):
        prov._split(bad)


@pytest.mark.parametrize("period", ["m1", "m5", "m15", "m30", "m60"])
def test_all_periods_accepted(monkeypatch, period):
    calls = _patch_fetch(monkeypatch)
    prov.minute_bars("00700.HK", period=period, limit=5)
    assert len(calls) == 1


def test_bad_period(monkeypatch):
    _patch_fetch(monkeypatch)
    with pytest.raises(ValueError, match="bad_period"):
        prov.minute_bars("00700.HK", period="m99")


@pytest.mark.parametrize("limit", [0, -1, True])
def test_bad_limit(monkeypatch, limit):
    """True 是 bool 不是 int —— 不能让它被当成 1 静默通过。"""
    _patch_fetch(monkeypatch)
    with pytest.raises(ValueError, match="bad_limit"):
        prov.minute_bars("00700.HK", limit=limit)


# ───────────────────────────────────────────── 上游 700 根上限

def test_limit_above_700_is_truncated_and_declared(monkeypatch):
    """请求 800 只会被截断到 700 —— 且必须告诉前端, 让它说明深度有限。"""
    calls = _patch_fetch(monkeypatch)
    body = prov.minute_bars("00700.HK", period="m5", limit=800)
    assert calls[0][3] == MAX_BARS_PER_REQUEST
    assert body["truncated"] is True
    assert body["requested"] == MAX_BARS_PER_REQUEST
    assert body["max_bars"] == MAX_BARS_PER_REQUEST
    assert "上限" in (body["note"] or "")


def test_limit_within_cap_not_truncated(monkeypatch):
    calls = _patch_fetch(monkeypatch)
    body = prov.minute_bars("00700.HK", period="m5", limit=120)
    assert calls[0][3] == 120 and body["truncated"] is False and body["note"] is None


# ───────────────────────────────────────────── 数据清洗

def test_prices_are_quantized(monkeypatch):
    """float32 毛刺必须量化: 336.6400146484375 → 336.64。"""
    _patch_fetch(monkeypatch)
    row = prov.minute_bars("00700.HK", period="m30", limit=5)["rows"][0]
    assert row["close"] == 336.89
    assert row["open"] == 336.64
    assert row["datetime"] == "2026-10-09 00:00:00"


def test_nan_becomes_none_not_zero(monkeypatch):
    """空值填 0 会变成"价格为 0"的假数据 —— 必须 None。"""
    frame = _frame(2)
    frame.loc[0, "close"] = math.nan
    _patch_fetch(monkeypatch, frame=frame)
    rows = prov.minute_bars("00700.HK", period="m30", limit=5)["rows"]
    assert rows[0]["close"] is None
    assert rows[1]["close"] == 336.89


def test_rows_sorted_ascending(monkeypatch):
    frame = _frame(3)
    frame["datetime"] = pd.to_datetime(["2026-10-09 03:00:00", "2026-10-09 01:00:00",
                                        "2026-10-09 02:00:00"])
    _patch_fetch(monkeypatch, frame=frame)
    rows = prov.minute_bars("00700.HK", period="m30", limit=5)["rows"]
    assert [r["datetime"] for r in rows] == ["2026-10-09 01:00:00", "2026-10-09 02:00:00",
                                             "2026-10-09 03:00:00"]


def test_float_shares_dropped(monkeypatch):
    """上游这个字段口径没核过, 不下发 —— 宁缺勿错。"""
    _patch_fetch(monkeypatch)
    row = prov.minute_bars("00700.HK", period="m30", limit=5)["rows"][0]
    assert "float_shares" not in row
    assert set(row) == {"datetime", "open", "high", "low", "close", "volume", "amount"}


# ───────────────────────────────────────────── 缓存

def test_cache_avoids_second_connection(monkeypatch):
    calls = _patch_fetch(monkeypatch)
    prov.minute_bars("00700.HK", period="m30", limit=5)
    body = prov.minute_bars("00700.HK", period="m30", limit=5)
    assert len(calls) == 1
    assert body["cached"] is True


def test_cache_key_includes_period(monkeypatch):
    calls = _patch_fetch(monkeypatch)
    prov.minute_bars("00700.HK", period="m30", limit=5)
    prov.minute_bars("00700.HK", period="m5", limit=5)
    assert len(calls) == 2


def test_empty_result_is_not_cached(monkeypatch):
    """空结果缓存住会把"暂时取不到"钉死一个 TTL —— 不缓存, 下次重试。"""
    calls = _patch_fetch(monkeypatch, frame=_frame(0))
    prov.minute_bars("00700.HK", period="m30", limit=5)
    prov.minute_bars("00700.HK", period="m30", limit=5)
    assert len(calls) == 2


# ───────────────────────────────────────────── 失败语义

def test_all_nodes_down_raises_unavailable(monkeypatch):
    calls = _patch_fetch(monkeypatch, exc=OSError("connection refused"))
    with pytest.raises(TdxExUnavailableError, match="节点均失败"):
        prov.minute_bars("00700.HK", period="m30", limit=5)
    # 第一个节点挂了要继续试下一个 —— 不能"第一个失败就放弃"
    assert len(calls) == len(prov._hosts())


def test_first_node_down_falls_through_to_second(monkeypatch):
    """降级: 第一个节点失败、第二个成功, 对外无感。"""
    state = {"n": 0}

    def fake(host, market, code, period, count):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("first node down")
        return _frame()

    monkeypatch.setattr(prov, "_fetch_once", fake)
    body = prov.minute_bars("00700.HK", period="m30", limit=5)
    assert len(body["rows"]) == 3 and state["n"] == 2


def test_no_hosts_configured(monkeypatch):
    monkeypatch.setattr(prov, "_hosts", lambda: [])
    with pytest.raises(TdxExUnavailableError, match="tdx_ex_hosts 为空"):
        prov.minute_bars("00700.HK", period="m30", limit=5)


def test_hosts_from_settings(monkeypatch):
    """节点列表只从 settings 读 —— 改 .env 应生效(容器需 --force-recreate)。"""
    from app.config import settings

    monkeypatch.setattr(settings, "tdx_ex_hosts", "1.2.3.4, 5.6.7.8")
    assert prov._hosts() == ["1.2.3.4", "5.6.7.8"]


def test_payload_metadata(monkeypatch):
    _patch_fetch(monkeypatch)
    body = prov.minute_bars("00700.HK", period="m30", limit=5)
    assert body["source"] == "tdx-ex"
    assert body["market"] == "HK" and body["symbol"] == "00700.HK"
    # 上游时间是服务器下发的北京时间, 前端不能当本地时间渲染
    assert body["timezone"] == "Asia/Shanghai"


# ───────────────────────────────────────────── 实时快照 / 五档盘口


class _FakeInfo:
    """上游 symbol_info 的记录形状(字段取自 2026-10-12 实测)。"""

    def __init__(self, *, turnover=0.1541, close=425.8, prev=424.8, vol=14011738.0):
        import datetime as _dt

        self.market, self.code, self.name = 31, "00700", "腾讯控股"
        self.time = _dt.datetime(2026, 10, 12, 16, 8, 24)
        self.activity = 23033
        self.pre_close, self.open, self.high, self.low = prev, 428.4, 430.4, 423.4
        self.close, self.momentum = close, 425.8
        self.vol = vol
        self.amount = 5983558144.0
        self.inside_volume, self.outside_volume = 7005907, 7005831
        self.turnover = turnover
        self.avg = 427.0519


class _FakeQuote:
    """上游 GetInstrumentQuote 的形状。levels=0 就是港股那种"档位全 0"。"""

    def __init__(self, levels: int = 1):
        self.market, self.code, self.pre_close = 31, "00700", 424.8
        self.price = 425.8
        self.kaicang = 0
        self.zongliang, self.xianliang = 14011738, 1253000
        self.neipan, self.waipan, self.chicang = 7005907, 7005831, 1067335708
        for tag in ("bid", "ask"):
            for i in range(1, 6):
                live = i <= levels
                setattr(self, f"{tag}{i}", (425.8 + i * 0.1) if live else 0.0)
                setattr(self, f"{tag}_vol{i}", (100 * i) if live else 0)


def _patch_exec(monkeypatch, result=None, exc=None):
    """桩掉**单节点执行**, 保住多节点降级语义。返回被调用的命令列表。"""
    calls: list[object] = []

    def fake(host, cmd_factory):
        if exc is not None:
            raise exc
        cmd = cmd_factory()
        calls.append(cmd)
        return result if result is not None else None

    monkeypatch.setattr(prov, "_exec_once", fake)
    return calls


def test_snapshot_uses_upstream_prev_close(monkeypatch):
    """快照的昨收必须是**上游直给** —— 这是它比 m1 反推值钱的地方。"""
    _patch_exec(monkeypatch, _FakeInfo())
    body = prov.snapshot("00700.HK")
    assert body["prev_close"] == 424.8 and body["last"] == 425.8
    assert body["name"] == "腾讯控股"
    assert body["source"] == "tdx-ex" and body["market"] == "HK"
    assert body["change"] == pytest.approx(1.0)
    assert body["pct"] == pytest.approx(0.2354, abs=1e-3)
    # 推导值必须上报, 前端据此标口径
    assert {"turnover_pct", "change", "pct"} <= set(body["derived"])
    assert body["timezone"] == "Asia/Shanghai"
    assert body["server_time"] == "2026-10-12 16:08:24"
    # 这串 volume 是**股数**(amount/vol 应当等于均价)
    assert body["volume_unit"] == "share"
    assert body["amount"] / body["volume"] == pytest.approx(body["avg_price"], abs=0.5)


def test_snapshot_turnover_is_per_mille_to_percent(monkeypatch):
    """上游 turnover 是千分比: 0.1541 → 0.01541%。换算只做这一层, 不多猜。"""
    _patch_exec(monkeypatch, _FakeInfo(turnover=0.1541))
    assert prov.snapshot("00700.HK")["turnover_pct"] == pytest.approx(0.01541)


def test_snapshot_prev_close_zero_never_divides(monkeypatch):
    _patch_exec(monkeypatch, _FakeInfo(prev=0.0))
    body = prov.snapshot("00700.HK")
    assert body["pct"] is None  # 除零要出 None, 不能出 inf/NaN


def test_snapshot_cache_hit(monkeypatch):
    calls = _patch_exec(monkeypatch, _FakeInfo())
    prov.snapshot("00700.HK")
    second = prov.snapshot("00700.HK")
    assert len(calls) == 1 and second["cached"] is True


def test_snapshot_bad_symbol(monkeypatch):
    _patch_exec(monkeypatch, _FakeInfo())
    with pytest.raises(ValueError, match=r"\.HK/\.US"):
        prov.snapshot("600519")


def test_snapshot_all_nodes_down(monkeypatch):
    """节点全挂属于**故障**(可重试), 不是"没有源" —— 抛的是 UNAVAILABLE 而非 capability。"""
    _patch_exec(monkeypatch, exc=OSError("tcp down"))
    with pytest.raises(TdxExUnavailableError):
        prov.snapshot("00700.HK")


def test_snapshot_empty_upstream_result(monkeypatch):
    """上游返回 None(找不到标的)要 fail-closed, 不许返回一堆 None 字段。"""
    _patch_exec(monkeypatch, result=None)
    with pytest.raises(TdxExUnavailableError, match="上游返回空"):
        prov.snapshot("00700.HK")


def test_orderbook_us_real_levels(monkeypatch):
    _patch_exec(monkeypatch, _FakeQuote(levels=1))
    body = prov.orderbook("AAPL.US")
    assert body["depth_available"] is True
    assert body["bid_levels"] == body["ask_levels"] == 1
    assert body["bids"][0]["price"] > 0 and body["bids"][0]["volume"] > 0
    assert body["bids"][1]["price"] == 0.0        # 第 2 档起没档, 原样给 0(前端不画)
    assert body["note"] is None
    assert body["volume_unit"] == "share"


def test_orderbook_hk_zero_depth_flagged_not_faked(monkeypatch):
    """港股档位恒 0 是最容易出假数据的地方: 必须显式标不可用, 不许画成"0 元挂单"。"""
    _patch_exec(monkeypatch, _FakeQuote(levels=0))
    body = prov.orderbook("00700.HK")
    assert body["depth_available"] is False
    assert body["bid_levels"] == body["ask_levels"] == 0
    assert body["note"] and "不返回买卖档" in body["note"]
    # 总量/内外盘仍是真数据, 所以 overall available 不改
    assert body["total_volume"] > 0


def test_orderbook_cache_hit(monkeypatch):
    calls = _patch_exec(monkeypatch, _FakeQuote())
    prov.orderbook("00700.HK")
    assert prov.orderbook("00700.HK")["cached"] is True and len(calls) == 1


def test_exec_one_command_per_connection(monkeypatch):
    """EX 服务器会对不支持的请求直接断 TCP —— 一条命令一个连接, 不能复用。

    这条测的是架构约束(每次 _exec_once 都要自带 connect/close), 而不是某个数据。
    """
    seen: list[str] = []
    conn = _DummyConn()

    monkeypatch.setattr(prov.ExTdxConnection, "connect", lambda self: seen.append("connect"))
    monkeypatch.setattr(prov.ExTdxConnection, "close", lambda self: seen.append("close"))
    monkeypatch.setattr(prov.ExTdxConnection, "execute", conn.execute)
    _patch(monkeypatch)

    prov._exec_once("1.2.3.4", lambda: "cmd")
    assert seen == ["connect", "close"]
    assert conn.cmds == ["MacExLoginCmd", "cmd"]


class _DummyConn:
    def __init__(self):
        self.cmds: list[str] = []

    def execute(self, cmd):
        self.cmds.append(type(cmd).__name__ if not isinstance(cmd, str) else cmd)
        return "ok"


def _patch(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tdx_ex_hosts", "1.2.3.4")
    monkeypatch.setattr(settings, "tdx_ex_port", 7727)
    monkeypatch.setattr(settings, "tdx_ex_timeout_s", 1.0)


# ───────────────────────────────────────────── 板块(所属 / 成分股 / 榜单)


class _FakeBoard:
    def __init__(self, code, name, close, prev):
        self.board_type = 0
        self.market = 70
        self.board_code, self.board_name = code, name
        self.close, self.pre_close = close, prev


class _FakeBoardRow:
    """BoardListCmd 的行: 板块指数 + 板块内领涨股(实测形状)。"""

    def __init__(self, code, name, close, prev, sym="GRWG", sym_c=1.7446, sym_p=1.5):
        """sym_c/sym_p 取实测: GRWG 1.7446 / 1.5 ⇒ leader_pct 16.3067。"""
        self.market, self.code, self.name = 70, code, name
        self.price, self.rise_speed, self.pre_close = close, 0.0, prev
        self.symbol_market, self.symbol_code, self.symbol_name = 74, sym, sym
        self.symbol_price, self.symbol_rise_speed = sym_c, 0.0
        self.symbol_pre_close = sym_p


class _FakeMember:
    def __init__(self, code, name, close, prev, *, shares_wan=81800.0, dy=3.59):
        self.market, self.code, self.name = 74, code, name
        self.fields = {
            "close": close, "pre_close": prev, "open": prev, "high": close, "low": prev,
            "vol": 38272.0, "amount": 16096128.0, "vol_ratio": 3.3848,
            "total_shares": shares_wan, "float_shares": shares_wan,
            "total_market_cap_ab": 342897426432.0, "eps": 4.09,
            "pe_dynamic": 100.0, "pe_ttm": 102.0, "dividend_yield": dy,
            "net_assets": 33.73,
        }


def test_belong_boards_computes_pct_and_sorts(monkeypatch):
    _patch_exec(monkeypatch, [
        _FakeBoard("HK0279", "港股-远程办公", 1152.46, 1125.81),
        _FakeBoard("HK0215", "港股-网络游戏", 3878.39, 3838.6),
    ])
    body = prov.belong_boards("00700.HK")
    assert [b["board_code"] for b in body["boards"]] == ["HK0279", "HK0215"]  # 按涨跌降序
    assert body["boards"][0]["pct"] == pytest.approx(2.3672, abs=1e-3)
    assert body["market"] == "HK" and body["source"] == "tdx-ex"


def test_belong_boards_rolls_off_null_pct(monkeypatch):
    """板块昨收为 0/None 时 pct 必须是 None —— 不能出 inf, 更不能排到最前。"""
    _patch_exec(monkeypatch, [_FakeBoard("US0001", "x", 10.0, 0.0)])
    assert prov.belong_boards("AAPL.US")["boards"][0]["pct"] is None


def test_belong_boards_empty_is_not_an_error(monkeypatch):
    """未被任何板块收录是**正常情况**(空列表), 不是故障 —— 不许 fail-closed。"""
    _patch_exec(monkeypatch, [])
    assert prov.belong_boards("AAPL.US")["boards"] == []


def test_board_members_converts_wan_shares_and_units(monkeypatch):
    _patch_exec(monkeypatch, [_FakeMember("PANW", "派拓网络", 419.19, 418.78)])
    m = prov.board_members("US0026", limit=5)["members"][0]
    # 上游给万股 → 股市两边核实过: 81800 万股 = 8.18 亿股
    assert m["total_shares"] == pytest.approx(818_000_000)
    assert m["pct"] == pytest.approx(0.0979, abs=1e-3)
    assert m["market_cap"] > 1e11          # 绝对金额, 不是亿为单位
    assert m["dividend_yield"] == pytest.approx(3.59)   # 百分数
    assert "pct" in prov.board_members("US0026")["derived"]


def test_board_members_bad_code(monkeypatch):
    _patch_exec(monkeypatch, [])
    with pytest.raises(ValueError, match="bad_board_code"):
        prov.board_members("不是板块码")


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_board_members_bad_limit(limit, monkeypatch):
    _patch_exec(monkeypatch, [])
    with pytest.raises(ValueError, match="bad_limit"):
        prov.board_members("US0026", limit=limit)


def test_board_members_limit_clamped_to_page_size(monkeypatch):
    _patch_exec(monkeypatch, [])
    assert prov.board_members("US0026", limit=500)["requested"] == prov.MAX_BOARD_MEMBERS_PER_PAGE


def test_board_list_kinds_mapped(monkeypatch):
    for kind in prov.BOARD_KINDS:
        _patch_exec(monkeypatch, [_FakeBoardRow("US0212", "美股-大麻", 434.54, 424.84)])
        body = prov.board_list(kind, limit=3)
        assert body["kind"] == kind and body["boards"][0]["pct"] == pytest.approx(2.2832, abs=1e-3)
        assert body["boards"][0]["leader_pct"] == pytest.approx(16.3067, abs=1e-3)


def test_board_list_bad_kind():
    with pytest.raises(ValueError, match="bad_kind"):
        prov.board_list("NOWHERE")


def test_board_list_bad_limit(monkeypatch):
    _patch_exec(monkeypatch, [])
    for bad in (0, 151):
        with pytest.raises(ValueError, match="bad_limit"):
            prov.board_list("US_HY", limit=bad)


def test_board_list_sorted_by_pct_desc(monkeypatch):
    _patch_exec(monkeypatch, [
        _FakeBoardRow("A", "a", 100.0, 100.0),
        _FakeBoardRow("B", "b", 110.0, 100.0),
    ])
    assert [b["board_code"] for b in prov.board_list("US_HY")["boards"]] == ["B", "A"]


# ───────────────────────────────────────────── 多日分时(仅港股可接)


class _FakeTick:
    def __init__(self, minute, price, avg=None, vol=100):
        import datetime as _dt

        # 复刻上游: minutes 可跨零点累加(美股到 1679), 钟点按 mod 24 记
        self.time = _dt.time((minute // 60) % 24, minute % 60)
        self.minutes = minute
        self.price = price
        self.avg = avg if avg is not None else price
        self.vol = vol


class _FakeDay:
    def __init__(self, day: str, pre_close: float, closes: list[float], start=570):
        import datetime as _dt

        y, m, d = (int(x) for x in day.split("-"))
        self.date = _dt.date(y, m, d)
        self.pre_close = pre_close
        self.ticks = [_FakeTick(start + i, c) for i, c in enumerate(closes)]


class _FakeMulti:
    """港股那种形状: 每天 330 根, **当日收盘 == 次一日的昨收**(自洽链)。"""

    def __init__(self, days):
        self.market, self.code, self.name = 31, "00700", "腾讯控股"
        self.charts = days


def _hk_five_days():
    return [
        _FakeDay("2026-10-12", 424.8, [429.8, 425.8]),      # 收 425.8? 见下方 424.80 对齐
        _FakeDay("2026-10-09", 411.4, [416.8, 424.8]),
        _FakeDay("2026-10-08", 420.6, [422.8, 411.4]),
        _FakeDay("2026-10-07", 428.2, [426.0, 420.6]),
        _FakeDay("2026-10-06", 423.0, [427.8, 428.2]),
    ]


def test_multiday_hk_ok_and_chain_checked(monkeypatch):
    """港股每天一块 + 昨收链自洽 → 完整下发, 日期与点数都对得上。"""
    _patch_exec(monkeypatch, _FakeMulti(_hk_five_days()))
    body = prov.multi_day_ticks("00700.HK")
    assert [d["date"] for d in body["days"]] == [
        "2026-10-12", "2026-10-09", "2026-10-08", "2026-10-07", "2026-10-06"]
    assert body["days"][0]["prev_close"] == 424.8
    assert body["days"][0]["points"][0]["t"] == "09:30"
    assert body["source"] == "tdx-ex" and body["timezone"] == "Asia/Shanghai"


def test_multiday_us_refused_not_faked(monkeypatch):
    """美股不是"没实现", 是**接了就得编日期** —— 必须 fail-closed 且说明清楚。"""
    _patch_exec(monkeypatch, _FakeMulti([]))
    with pytest.raises(TdxExUnavailableError, match="不一一对应"):
        prov.multi_day_ticks("AAPL.US")


def test_multiday_chain_mismatch_is_refused(monkeypatch):
    """自洽守卫: 昨收链对不上(改成美股那种错位)就拒绝, 不许画错曲线的多日图。"""
    broken = _hk_five_days()
    # 篡改中间某天的收盘 ⇒ 它前面那天的昨收立刻对不上
    broken[1].ticks = [_FakeTick(570, 416.8), _FakeTick(959, 500.0)]
    _patch_exec(monkeypatch, _FakeMulti(broken))
    with pytest.raises(TdxExUnavailableError, match="不自洽"):
        prov.multi_day_ticks("00700.HK")


def test_multiday_skips_empty_days(monkeypatch):
    days = _hk_five_days()
    days.append(_FakeDay("2026-10-05", 400.0, []))
    _patch_exec(monkeypatch, _FakeMulti(days))
    body = prov.multi_day_ticks("00700.HK")
    assert all(d["points"] for d in body["days"])


def test_multiday_all_empty_refuses(monkeypatch):
    _patch_exec(monkeypatch, _FakeMulti([]))
    with pytest.raises(TdxExUnavailableError, match="没有可取的分时日"):
        prov.multi_day_ticks("00700.HK")


@pytest.mark.parametrize("days", [0, 6, -1, 2.5])
def test_multiday_bad_days(days, monkeypatch):
    _patch_exec(monkeypatch, _FakeMulti([]))
    with pytest.raises(ValueError, match="bad_days"):
        prov.multi_day_ticks("00700.HK", days=days)


def test_multiday_cache_hit(monkeypatch):
    calls = _patch_exec(monkeypatch, _FakeMulti(_hk_five_days()))
    prov.multi_day_ticks("00700.HK")
    assert prov.multi_day_ticks("00700.HK")["cached"] is True and len(calls) == 1


def test_vendor_tick_survives_cross_midnight_minutes():
    """vendored 解码: minutes > 1439(美股 1678=27:58)不能抛 hour must be in 0..23。"""
    from app._vendor.easy_tdx.mac.models import MacTick

    tk = MacTick(time=__import__("datetime").time(3, 58), price=1.0, avg=1.0, vol=1, minutes=1678)
    assert tk.time.hour == 3 and tk.minutes == 1678


# ───────────────────────────────────────────── 逐笔成交(港股 0x23FC/0x2406 vs 美股 0x122F)


class _FakeExTx:
    """上游 ex 逐笔记录: 价格是**整数**(425000 → 425.0 HKD)。"""

    def __init__(self, hour=15, minute=59, second=0, price=425000, volume=1500, nature=0):
        self.hour, self.minute, self.second = hour, minute, second
        self.price, self.volume = price, volume
        self.zengcang, self.nature = 0, nature


class _FakeMacTx:
    """上游 MAC 逐笔(0x122F): 价格已是浮点, 且带 trade_count。"""

    def __init__(self, hour=21, minute=12, price=337.79998779296875, vol=35, bs=2):
        import datetime as _dt

        self.time = _dt.time(hour, minute, 0)
        self.price, self.vol = price, vol
        self.trade_count, self.bs_flag = 3, bs


def test_transactions_hk_uses_ex_protocol_and_divides_price(monkeypatch):
    """港股逐笔: 走 0x23FC(当日), price_int/1000 = 港元 —— 0x122F 对港股恒空。"""
    calls = _patch_exec(monkeypatch, [_FakeExTx(15, 59), _FakeExTx(9, 30, price=416800)])
    body = prov.transactions("00700.HK", limit=2)
    assert type(calls[0]).__name__ == "GetExTransactionDataCmd"
    assert body["protocol"] == "0x23FC/0x2406"
    # 上游倒序返回, 下发必须升序
    assert [r["time"] for r in body["rows"]] == ["09:30:00", "15:59:00"]
    assert body["rows"][0]["price"] == 416.8 and body["rows"][1]["price"] == 425.0
    assert body["price_unit"].startswith("HKD") and body["volume_unit"] == "share"


def test_transactions_hk_history_uses_2406(monkeypatch):
    calls = _patch_exec(monkeypatch, [_FakeExTx()])
    body = prov.transactions("00700.HK", date="2026-10-09")
    assert type(calls[0]).__name__ == "GetExHistoryTransactionDataCmd"
    assert calls[0].date == 20261009
    assert body["date"] == "20261009"


@pytest.mark.parametrize("given", ["20261009", "2026-10-09", "2026/10/09"])
def test_transactions_date_shapes(given, monkeypatch):
    calls = _patch_exec(monkeypatch, [_FakeExTx()])
    prov.transactions("00700.HK", date=given)
    assert calls[0].date == 20261009


def test_transactions_us_uses_122f_and_keeps_float_price(monkeypatch):
    """美股 0x122F 本来就能用: 价格不除 1000, 且带 trade_count / bs_flag。"""
    calls = _patch_exec(monkeypatch, [_FakeMacTx()])
    body = prov.transactions("AAPL.US", limit=1)
    assert type(calls[0]).__name__ == "SymbolTransactionCmd"
    assert body["protocol"] == "0x122F"
    row = body["rows"][0]
    assert row["price"] == pytest.approx(337.8)
    assert row["trade_count"] == 3 and row["bs_flag"] == 2
    assert "bs_flag" in body["note"]


def test_transactions_hk_does_not_invent_trade_count(monkeypatch):
    """ex 协议没有 trade_count —— 给 None, 不给 0 冒充(0 会被当成"0 笔成交")。"""
    _patch_exec(monkeypatch, [_FakeExTx()])
    assert prov.transactions("00700.HK")["rows"][0]["trade_count"] is None


def test_transactions_empty_is_unavailable_not_empty_table(monkeypatch):
    _patch_exec(monkeypatch, [])
    with pytest.raises(TdxExUnavailableError, match="上游返回空"):
        prov.transactions("00700.HK")


@pytest.mark.parametrize("limit", [0, 1801, -1, 2.5])
def test_transactions_bad_limit(limit, monkeypatch):
    _patch_exec(monkeypatch, [_FakeExTx()])
    with pytest.raises(ValueError, match="bad_limit"):
        prov.transactions("00700.HK", limit=limit)


@pytest.mark.parametrize("bad", ["2026-13-01", "2026100", "abc", "2026-10-32"])
def test_transactions_bad_date(bad, monkeypatch):
    """日期格式不宽容 —— 任何"猜格式"都可能把 9 月读成 10 月。"""
    _patch_exec(monkeypatch, [_FakeExTx()])
    with pytest.raises(ValueError, match="bad_date"):
        prov.transactions("00700.HK", date=bad)


# ───────────────────────────────────────────── 分钟价格线(0x240C 按日循环)


class _FakeMinuteBar:
    """上游 0x240C 一根: 只有 price + avg_price + volume, **没有 OHLC**。"""

    def __init__(self, hour=9, minute=30, price=416.8, avg=415.6, volume=859434):
        self.hour, self.minute = hour, minute
        self.price, self.avg_price = price, avg
        self.volume, self.open_interest = volume, 0


def test_minute_range_loops_by_day(monkeypatch):
    """按日循环: 一天一包, 长度不再受 700 根约束。"""
    calls = _patch_exec(monkeypatch, [_FakeMinuteBar() for _ in range(330)])
    body = prov.minute_range("00700.HK", days=3, period="m1")
    assert body["protocol"] == "0x240C"
    assert len(body["days"]) == 3 and body["bar_count"] == 990
    assert len(calls) >= 3     # 每天一次请求(非交易日会多扫几个候选日)
    assert body["ohlc"] == "single_price" and body["derived"] == []


def test_minute_range_m1_does_not_fake_ohlc_but_flags_it(monkeypatch):
    """m1 档上游只给该分钟价格: 四价同值, 但必须用 ohlc/note 说清楚不是原生蜡烛。"""
    _patch_exec(monkeypatch, [_FakeMinuteBar(9, 30), _FakeMinuteBar(9, 31, price=417.0)])
    row = prov.minute_range("00700.HK", days=1)["days"][0]["bars"][0]
    assert row["open"] == row["high"] == row["low"] == row["close"] == 416.8
    body = prov.minute_range("00700.HK", days=1)
    assert body["ohlc"] == "single_price"
    assert "开高低" in body["note"] or "开/高/低" in body["note"]


def test_minute_range_aggregate_m5_is_declared(monkeypatch):
    """更长周期由 m1 确定性聚合 —— close 取组内末根, 高/低取组内极值, 且必须声明。"""
    bars = [_FakeMinuteBar(9, 30 + i, price=100.0 + i) for i in range(10)]
    _patch_exec(monkeypatch, bars)
    body = prov.minute_range("00700.HK", days=1, period="m5")
    assert body["ohlc"] == "aggregated_from_m1"
    assert body["derived"] == ["open", "high", "low"]
    first, second = body["days"][0]["bars"][0], body["days"][0]["bars"][1]
    assert (first["open"], first["high"], first["low"], first["close"]) == (100.0, 104.0, 100.0, 104.0)
    assert second["open"] == 105.0 and second["close"] == 109.0
    assert first["volume"] == sum(859434 for _ in range(5))


def test_minute_range_us_hour_mod_24(monkeypatch):
    """美股 hour 可到 27(21:30 + 6.5h): mod 24 后落在凌晨段, 与 minute_bars 同口径。"""
    _patch_exec(monkeypatch, [_FakeMinuteBar(27, 59, price=336.64)])
    row = prov.minute_range("AAPL.US", days=1)["days"][0]["bars"][0]
    assert row["datetime"].endswith("03:59:00")


def test_minute_range_skips_non_trading_days(monkeypatch):
    """取到空包 = 非交易日, 跳过而不是当成故障(fail-open 只在"有数据"这件事上)。"""
    state = {"n": 0}

    def fake(host, cmd_factory):
        state["n"] += 1
        cmd_factory()
        return [] if state["n"] == 1 else [_FakeMinuteBar()]

    monkeypatch.setattr(prov, "_exec_once", fake)
    body = prov.minute_range("00700.HK", days=2)
    assert len(body["days"]) == 2


def test_minute_range_all_days_empty_is_unavailable(monkeypatch):
    _patch_exec(monkeypatch, [])
    with pytest.raises(TdxExUnavailableError, match="均无分钟数据"):
        prov.minute_range("00700.HK", days=2)


@pytest.mark.parametrize("days", [0, 21, -1, 2.5])
def test_minute_range_bad_days(days, monkeypatch):
    _patch_exec(monkeypatch, [_FakeMinuteBar()])
    with pytest.raises(ValueError, match="bad_days"):
        prov.minute_range("00700.HK", days=days)


def test_minute_range_bad_period(monkeypatch):
    _patch_exec(monkeypatch, [_FakeMinuteBar()])
    with pytest.raises(ValueError, match="bad_period"):
        prov.minute_range("00700.HK", period="m10")


def test_minute_range_carries_upstream_avg(monkeypatch):
    """0x240C 自带当日累计均价 —— 比我们 m1 自己合成的均价权威, 直接透出。"""
    _patch_exec(monkeypatch, [_FakeMinuteBar(avg=415.5966796875)])
    row = prov.minute_range("00700.HK", days=1)["days"][0]["bars"][0]
    assert row["avg"] == pytest.approx(415.5967)


def test_minute_range_candidates_are_newest_first():
    """回归: 候选日必须**由新到旧**。

    第一版写成自旧向新扫, days=3 拿到的是"最近一段里最老的 3 天"(实测 00700
    拿到 10-02 而漏掉 10-12) —— 图看着像数据好几天没更新。
    """
    from datetime import date, timedelta

    cands = prov._recent_trading_candidates(3)
    assert len(cands) >= 3
    assert cands == sorted(cands, reverse=True)
    today = date.today()
    newest = next(today - timedelta(days=b) for b in range(8)
                  if (today - timedelta(days=b)).weekday() < 5)
    assert cands[0] == newest.year * 10000 + newest.month * 100 + newest.day


def test_minute_range_returns_most_recent_trading_days(monkeypatch):
    """只有最新那天有数据时, 返回的就该是那天, 而不是扫到的第一个老日子。"""
    from datetime import date, timedelta

    state = {"n": 0}

    def fake(host, cmd_factory):
        cmd_factory()
        state["n"] += 1
        return [_FakeMinuteBar()] if state["n"] == 1 else []

    monkeypatch.setattr(prov, "_exec_once", fake)
    body = prov.minute_range("00700.HK", days=2)
    assert len(body["days"]) == 1
    today = date.today()
    newest = next(today - timedelta(days=b) for b in range(8)
                  if (today - timedelta(days=b)).weekday() < 5)
    assert body["days"][0]["date"] == f"{newest:%Y-%m-%d}"
