"""tdx_tick_provider 单测 —— A股真逐笔成交(通达信标准市 0x0fb5)。

全部走本地桩(不打网络)。锁四件事:
  1. tail 必须按**过滤后的真成交**计数 —— 上游夹的 vol=0 行情标记行不能顶掉用户的额度;
  2. bs=8 剔除、bs=2/5 的 session 归类(open/close auction / after_hours);
  3. day_summary 的多页重排必须是 (-page, 页内 idx) —— 页序新→旧但页内正序, 直接拼会乱;
  4. fail-closed: 全标记行/空返回/非法代码 → TdxTickUnavailableError, 不返回占位数据。
"""

from __future__ import annotations

import pytest

from app.data_providers import tdx_tick_provider as prov
from app.data_providers.tdx_tick_provider import (
    TdxTickUnavailableError,
    day_summary,
    recent_trades,
)
from app.services.source_gate import REAL_SOURCES, is_real


def _rec(hour: int, minute: int, price: float, vol: int, bs: int):
    """模仿 vendor 的 TransactionRecord。"""
    return type("R", (), {
        "hour": hour, "minute": minute, "price": price, "vol": vol,
        "buyorsell": bs, "unknown_last": 0,
    })()


@pytest.fixture(autouse=True)
def _clean():
    prov.clear_cache()
    yield
    prov.clear_cache()


def _patch_rows(monkeypatch, pages: dict[int, list]):
    """按 start 给不同的页。pages = {start: [rec, ...]}"""

    def fake(code, market, date_int, start, count):
        rows = pages.get(start, [])
        return [r for r in rows[:count]]

    monkeypatch.setattr(prov, "_fetch_page", fake)


def _patch_daily(monkeypatch, rows: list[dict]):
    monkeypatch.setattr(prov, "_tencent_daily", lambda code, count=10: rows)


# ---------------------------------------------------------------------------
# 字段映射
# ---------------------------------------------------------------------------


def test_source_registered_in_gate():
    """tdx-tick 必须在 source_gate 白名单里 —— 否则面板会被闸门拦掉。"""
    assert prov.SOURCE in REAL_SOURCES
    assert is_real(prov.SOURCE)


@pytest.mark.parametrize(
    "bs,minutes,expected",
    [
        (0, 10 * 60, "continuous"),
        (1, 14 * 60, "continuous"),
        (2, 9 * 60 + 25, "open_auction"),
        (2, 15 * 60, "close_auction"),
        (5, 15 * 60 + 5, "after_hours"),
        (0, 15 * 60 + 1, "after_hours"),  # 时间超过收盘就算盘后, 不管 bs
    ],
)
def test_session_of(bs, minutes, expected):
    assert prov._session_of(bs, minutes) == expected


@pytest.mark.parametrize("bs,side", [(0, "buy"), (1, "sell"), (2, "auction"), (5, "unknown")])
def test_side_of(bs, side):
    assert prov._side_of(bs) == side


@pytest.mark.parametrize("code,expect_market", [("600519", 1), ("000001", 0), ("300750", 0)])
def test_market_for(code, expect_market):
    assert int(prov._market_for(code)) == expect_market


@pytest.mark.parametrize("bad", ["920001", "430090", "12345", "abc123", "6005191"])
def test_market_for_fail_closed(bad):
    with pytest.raises(TdxTickUnavailableError):
        prov._market_for(bad)


def test_norm_date():
    assert prov._norm_date("20261013") == "2026-10-13"
    assert prov._norm_date("2026-10-13") == "2026-10-13"
    assert prov._norm_date(None) is None


# ---------------------------------------------------------------------------
# recent_trades: tail 语义 + 标记行剔除
# ---------------------------------------------------------------------------


def test_tail_counts_real_trades_only(monkeypatch):
    """🔴 核心: 上游 120 行里只有 71 笔真成交, tail=50 必须给 50 笔而不是 50 行。"""
    rows = []
    for i in range(120):
        bs = 8 if i % 3 == 0 else (0 if i % 2 else 1)  # 1/3 是标记行
        rows.append(_rec(14, 30, 10.0 + i / 100, 1, bs))
    _patch_rows(monkeypatch, {0: rows})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.0, "volume": 999,
                                "high": 11.0, "low": 9.0, "open": 9.5}])

    data = recent_trades("600519", "2026-10-14", 50)["data"]
    assert data["count"] == 50
    assert all(t["vol"] > 0 for t in data["trades"])
    assert data["summary"]["marker_rows_dropped"] == 40
    assert data["truncated"] is True


def test_tail_more_than_available_stops_at_max_rows(monkeypatch):
    """要不比上游多就会一直补取到 MAX_TAIL —— 但不能越过协议上限。"""
    seen: list[int] = []

    def fake(code, market, date_int, start, count):
        seen.append(count)
        n = count // 2  # 一半是标记行
        return [_rec(14, 30, 10.0, 1, 8 if i % 2 else 0) for i in range(n)]

    monkeypatch.setattr(prov, "_fetch_page", fake)
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.0, "volume": 999,
                                "high": 11.0, "low": 9.0, "open": 9.5}])
    recent_trades("600519", "2026-10-14", 500)
    assert all(c <= prov.MAX_TAIL for c in seen)


def test_all_marker_rows_is_unavailable(monkeypatch):
    """上游全是标记行 —— 这不等于"当天 0 笔成交", 必须 fail-closed 并说清原因。"""
    _patch_rows(monkeypatch, {0: [_rec(14, 30, 10.0, 0, 8) for _ in range(10)]})
    with pytest.raises(TdxTickUnavailableError) as exc:
        recent_trades("600519", "2026-10-14", 10)
    assert "empty_ticks" in str(exc.value)


def test_empty_upstream_is_unavailable(monkeypatch):
    _patch_rows(monkeypatch, {0: []})
    with pytest.raises(TdxTickUnavailableError):
        recent_trades("600519", "2026-10-14", 10)


def test_cross_check_flags_price_mismatch(monkeypatch):
    """末笔盘中价和日K收盘对不上 → verified=false, 不许静默。"""
    rows = [
        _rec(9, 25, 10.0, 10, 2),
        _rec(14, 30, 12.0, 10, 0),
        _rec(15, 0, 12.5, 10, 2),
        _rec(15, 10, 12.5, 1, 5),
    ]
    _patch_rows(monkeypatch, {0: rows})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 11.0, "volume": 30,
                                "high": 12.5, "low": 10.0, "open": 10.0}])
    data = recent_trades("600519", "2026-10-14", 10)["data"]
    check = data["cross_check"]
    assert check["available"] is True
    assert check["verified"] is False
    # 盘后那笔不算末笔盘中成交
    assert check["last_trade_time"] == "15:00"


def test_cross_check_unavailable_when_daily_missing(monkeypatch):
    rows = [_rec(14, 30, 10.0, 10, 0)]
    _patch_rows(monkeypatch, {0: rows})
    _patch_daily(monkeypatch, [])
    data = recent_trades("600519", "2026-10-14", 10)["data"]
    assert data["cross_check"]["available"] is False
    assert "cross_source_unavailable" in data["cross_check"]["reason"]


def test_payload_is_wrapped_and_cached(monkeypatch):
    rows = [_rec(14, 30, 10.0, 3, 0)]
    _patch_rows(monkeypatch, {0: rows})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 11.0, "volume": 30,
                                "high": 12.0, "low": 9.0, "open": 10.0}])
    first = recent_trades("600519", "2026-10-14", 10)
    second = recent_trades("600519", "2026-10-14", 10)
    assert first["source"] == prov.SOURCE
    assert first == second  # 缓存命中时形状必须完全一致


# ---------------------------------------------------------------------------
# day_summary: 多页重排 + 量校验
# ---------------------------------------------------------------------------


def test_day_summary_reorders_pages_by_negative_page(monkeypatch):
    """🔴 页序是新→旧、页内是正序 —— 必须 (-page, idx) 重排才得到时间正序。"""
    # page0 = 尾盘(最新), page1 = 早些时候
    page0 = [_rec(14, 30, 10.0, 1, 0), _rec(15, 0, 10.1, 2, 2)]
    page1 = [_rec(9, 25, 9.8, 5, 2), _rec(9, 30, 9.9, 1, 1)]
    _patch_rows(monkeypatch, {0: page0, 800: page1})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.1, "volume": 9,
                                "high": 10.1, "low": 9.8, "open": 9.8}])

    data = day_summary("600519", "2026-10-14")["data"]
    times = [t["time"] for t in data["trades"]]
    assert times == ["09:25", "09:30", "14:30", "15:00"]
    minutes = [t["minutes"] for t in data["trades"]]
    assert minutes == sorted(minutes)  # 零逆序对


def test_day_summary_volume_matches_daily(monkeypatch):
    """盘中量合计 == 日K成交量 → verified=True(与实测 600519/300750 0.0000% 同口径)。"""
    page0 = [
        _rec(14, 30, 10.0, 3, 0),
        _rec(15, 0, 10.1, 2, 2),      # 收盘竞价, 计盘中
        _rec(15, 10, 10.1, 5, 5),     # 盘后, 不计
    ]
    page1 = [_rec(9, 30, 9.9, 4, 1)]
    _patch_rows(monkeypatch, {0: page0, 800: page1})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.1, "volume": 9,
                                "high": 10.1, "low": 9.9, "open": 9.9}])

    data = day_summary("600519", "2026-10-14")["data"]
    summary, check = data["summary"], data["cross_check"]
    assert summary["volume_regular"] == 9          # 3 + 2 + 4
    assert summary["volume_after_hours"] == 5
    assert check["verified"] is True
    assert check["volume_diff_pct"] == 0.0
    assert check["close_diff_pct"] == 0.0


def test_day_summary_flags_volume_mismatch(monkeypatch):
    """量对不上就不能说 verified —— 差值随包下发, 不藏着。"""
    _patch_rows(monkeypatch, {0: [_rec(14, 30, 10.0, 3, 0), _rec(15, 0, 10.1, 2, 2)]})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.1, "volume": 100,
                                "high": 10.1, "low": 10.0, "open": 10.0}])
    check = day_summary("600519", "2026-10-14")["data"]["cross_check"]
    assert check["verified"] is False
    assert check["volume_diff_pct"] < -90


def test_day_summary_markers_dropped(monkeypatch):
    rows = [_rec(14, 30, 10.0, 0, 8), _rec(14, 31, 10.0, 2, 1)]
    _patch_rows(monkeypatch, {0: rows})
    _patch_daily(monkeypatch, [{"date": "2026-10-14", "close": 10.0, "volume": 2,
                                "high": 10.0, "low": 10.0, "open": 10.0}])
    data = day_summary("600519", "2026-10-14")["data"]
    assert data["count"] == 1
    assert data["summary"]["marker_rows_dropped"] == 1
    assert data["trades"][0]["vol"] == 2
