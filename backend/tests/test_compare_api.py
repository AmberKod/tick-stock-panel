"""多股对比取数 /api/kline/compare 的边界测试。

对比页拿这个接口当唯一数据源, 它的两个上限(标的数 / 天数)直接决定页面会不会
被拖垮 —— 这里锁死: 传再多也只取前 8 只, 天数夹在 [5, 1000]。
"""
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _frame(symbol, n=3):
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": [_dt.date(2026, 7, 20 + i) for i in range(n)],
        "open": [10.0 + i for i in range(n)],
        "high": [10.5 + i for i in range(n)],
        "low": [9.5 + i for i in range(n)],
        "close": [10.2 + i for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    })


def _patch(repo, monkeypatch, syms):
    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch",
                        lambda symbols, start, end, columns=None: pl.concat([_frame(s) for s in symbols]))
    monkeypatch.setattr(repo, "get_name_map", lambda symbols: {s: f"名称-{s}" for s in symbols})


def _req(repo):
    state = type("S", (), {"repo": repo})()
    return type("R", (), {"app": type("A", (), {"state": state})()})()


def test_compare_returns_rows_and_names(repo, monkeypatch):
    from app.api import kline as kline_api

    _patch(repo, monkeypatch, ["600519.SH", "000651.SZ"])
    out = kline_api.get_compare(_req(repo), {"symbols": ["600519.SH", "000651.SZ"], "days": 250})

    assert set(out["data"]) == {"600519.SH", "000651.SZ"}
    assert out["names"]["600519.SH"] == "名称-600519.SH"
    # 只回 OHLCV + date/symbol, 不带 enriched 指标 —— 对比页用不上, 少传省带宽
    assert set(out["data"]["600519.SH"][0]) == {"symbol", "date", "open", "high", "low", "close", "volume"}


def test_compare_caps_symbols_at_eight(repo, monkeypatch):
    from app.api import kline as kline_api

    asked = [f"{60000 + i}.SH" for i in range(20)]
    seen = {}

    def fake_batch(symbols, start, end, columns=None):
        seen["n"] = len(symbols)
        return pl.concat([_frame(s) for s in symbols])

    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", fake_batch)
    monkeypatch.setattr(repo, "get_name_map", lambda symbols: {})

    out = kline_api.get_compare(_req(repo), {"symbols": asked, "days": 250})
    assert seen["n"] == 8
    assert len(out["data"]) == 8


def test_compare_clamps_days(repo, monkeypatch):
    from app.api import kline as kline_api

    caps = {}

    def fake_batch(symbols, start, end, columns=None):
        caps["span_days"] = (end - start).days
        return _frame("600519.SH")

    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", fake_batch)
    monkeypatch.setattr(repo, "get_name_map", lambda symbols: {})

    # 传一个离谱的天数: 必须被夹到 1000, 不能真的去扫 100 年
    kline_api.get_compare(_req(repo), {"symbols": ["600519.SH"], "days": 99999})
    assert caps["span_days"] <= 1000 * 2 + 15

    # 传 0 / 负数: 夹到下限 5, 不能变成"取 0 根"
    kline_api.get_compare(_req(repo), {"symbols": ["600519.SH"], "days": 0})
    assert caps["span_days"] >= 5 * 2


def test_compare_bad_days_falls_back(repo, monkeypatch):
    from app.api import kline as kline_api

    _patch(repo, monkeypatch, ["600519.SH"])
    out = kline_api.get_compare(_req(repo), {"symbols": ["600519.SH"], "days": "abc"})
    assert "600519.SH" in out["data"]


def test_compare_empty_symbols(repo, monkeypatch):
    from app.api import kline as kline_api

    _patch(repo, monkeypatch, [])
    out = kline_api.get_compare(_req(repo), {"symbols": [], "days": 250})
    assert out == {"data": {}, "names": {}}
    out2 = kline_api.get_compare(_req(repo), {"symbols": ["  ", ""], "days": 250})
    assert out2 == {"data": {}, "names": {}}


def test_compare_no_data_still_returns_names(repo, monkeypatch):
    """一只都取不到时也要把名称带回去 —— 前端要提示"库里没有", 而不是空白。"""
    from app.api import kline as kline_api

    monkeypatch.setattr(repo, "resolve_asset_type", lambda s: "stock")
    monkeypatch.setattr(repo, "get_daily_batch", lambda symbols, start, end, columns=None: pl.DataFrame())
    monkeypatch.setattr(repo, "get_name_map", lambda symbols: {s: f"N-{s}" for s in symbols})

    out = kline_api.get_compare(_req(repo), {"symbols": ["999999.SH"], "days": 250})
    assert out["data"] == {}
    assert out["names"] == {"999999.SH": "N-999999.SH"}
