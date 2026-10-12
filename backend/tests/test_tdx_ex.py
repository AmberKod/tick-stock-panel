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
