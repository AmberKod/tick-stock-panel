"""tdx_xdxr_provider 单测 —— 除权事件解析 + 自建复权因子。

全部走本地桩(不打网络)。重点锁三件事:
  1. 只有 category==1 参与复权计算, 股本变动类只展示;
  2. 因子是**乘性**( JD-process 交易所除权参考价), 不是腾讯那种"减固定金额";
  3. 拿不到"除权日前一交易日收盘"时跳过的事件必须**白名单式上报**, 不许静悄悄算错。

设计一根 5 天的小序列做手算基准:
    bars  = 2026-01-05..09, close = [100, 101, 102, 103, 104]
    event = 2026-01-07 每股分红 2.0  -> idx=2, prev_close=101
    r     = (101 - 2) / 101 = 0.9801980198...
    qfq   = [r, r, 1, 1, 1]             (最新一天归一到 1)
    hfq   = [1, 1, 1/r, 1/r, 1/r]       (最早一天归一到 1)
"""

from __future__ import annotations

import pytest

from app.data_providers import tdx_xdxr_provider as prov
from app.data_providers.tdx_xdxr_provider import (
    TdxXdxrUnavailableError,
    adj_ratios,
    xdxr_events,
)

_BARS = [("2026-01-05", 100.0), ("2026-01-06", 101.0), ("2026-01-07", 102.0),
         ("2026-01-08", 103.0), ("2026-01-09", 104.0)]


def _rec(year=2026, month=1, day=7, category=1, fenhong=2.0, songzhuangu=0.0,
         peigu=0.0, peigujia=0.0, name="除权除息", **extra):
    rec = {
        "year": year, "month": month, "day": day, "category": category, "name": name,
        "fenhong": fenhong, "songzhuangu": songzhuangu, "peigu": peigu, "peigujia": peigujia,
        "suogu": None, "xingquanjia": None, "fenshu": None,
        "panqian_liutong": extra.get("pre_float"),
        "panhou_liutong": extra.get("post_float"),
        "qian_zongguben": extra.get("pre_total"),
        "hou_zongguben": extra.get("post_total"),
    }
    return type("FakeRec", (), rec)()


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _patch_events(monkeypatch, recs):
    def fake_exec(cmd_factory):
        cmd_factory()          # 桩也要"构造"一次, 与真实调用一致
        if not isinstance(recs, list):
            raise recs
        return recs

    monkeypatch.setattr(prov, "_exec", fake_exec)


def _patch_bars(monkeypatch, bars=None, broken: bool = False):
    rows = bars or _BARS

    def fake_get(url, params=None, timeout=None):
        _ = (url, params, timeout)
        if broken:
            raise RuntimeError("网络炸了")
        sym = "sh000000"
        if params and "param" in params:
            head = params["param"].split(",")[0]
            sym = head if head.startswith(("sh", "sz")) else "sh" + head
        day = [[d, float(c), float(c), float(c), float(c), 1000] for d, c in rows]
        return _FakeResp({"data": {sym: {"day": day}}})

    monkeypatch.setattr(prov.httpx, "get", fake_get)


@pytest.fixture(autouse=True)
def _clean():
    prov.clear_cache()
    yield
    prov.clear_cache()


# ── 市场归类 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("600519", 1), ("688001", 1), ("601398", 1),
    ("000001", 0), ("002594", 0), ("300750", 0),
])
def test_market_for(code, expected):
    assert int(prov._market_for(code)) == expected


@pytest.mark.parametrize("code", ["", "12345", "83abcd", "AAPL.US", "00700"])
def test_market_for_rejects_non_a(code):
    """北交所/非 A 股代码一律 fail-closed —— 没实测过的东西不许猜。"""
    with pytest.raises(TdxXdxrUnavailableError, match="bad_code"):
        prov._market_for(code)


@pytest.mark.parametrize("code", ["920001", "831305", "430047"])
def test_market_for_rejects_bj(code):
    """北交所: easy_tdx 有 Market.BJ, 但 xdxr 在那边没实测过 —— 宁可不给, 不给错的。"""
    with pytest.raises(TdxXdxrUnavailableError, match="北交所"):
        prov._market_for(code)


# ── 事件解析 ────────────────────────────────────────────────────────────────

def test_xdxr_events_sorts_and_counts(monkeypatch):
    _patch_events(monkeypatch, [
        _rec(day=9, category=1, fenhong=1.0),
        _rec(day=5, category=5, pre_total=100.0, post_total=98.0),
        _rec(day=7, category=2, name="送配股上市"),
    ])
    data = xdxr_events("600519")["data"]
    assert [e["date"] for e in data["events"]] == [
        "2026-01-05", "2026-01-07", "2026-01-09"]
    assert data["count"] == 3
    assert data["category_counts"] == {5: 1, 2: 1, 1: 1}
    assert data["cash_events"] == 1
    # 股本变动类的单位是万股, 必须随事件下发, 别让用户猜
    assert data["events"][0]["pre_total_shares_wan"] == 100.0
    assert "万股" in data["unit_note"]


def test_xdxr_events_empty_is_fail_closed(monkeypatch):
    _patch_events(monkeypatch, [])
    with pytest.raises(TdxXdxrUnavailableError, match="empty_events"):
        xdxr_events("600519")


def test_xdxr_events_upstream_error_is_fail_closed(monkeypatch):
    _patch_events(monkeypatch, RuntimeError("tcp reset"))
    with pytest.raises(TdxXdxrUnavailableError, match="upstream_error"):
        xdxr_events("600519")


def test_xdxr_events_per_share_not_per_10(monkeypatch):
    """协议原值是"每 10 股", vendor 已除 10 —— 守的是量级, 别哪天上游改口径把 25.2 当 2.52 下发。"""
    _patch_events(monkeypatch, [_rec(category=1, fenhong=2.52, songzhuangu=0.8)])
    ev = xdxr_events("300750")["data"]["events"][0]
    assert ev["cash_per_share"] == pytest.approx(2.52)
    assert ev["bonus_share_ratio"] == pytest.approx(0.8)


# ── 复权因子 ────────────────────────────────────────────────────────────────

def test_adj_ratios_qfq_is_multiplicative(monkeypatch):
    """核心: qfq 必须是**乘性标量**, 不能像腾讯那样"减一个固定金额"。"""
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.0)])
    _patch_bars(monkeypatch)
    payload = adj_ratios("600519", mode="qfq", limit=640)
    rows = payload["data"]["ratios"]
    assert payload["source"] == prov.SOURCE
    assert payload["data"]["has_adjust"] is True
    assert [r["date"] for r in rows] == [d for d, _ in _BARS]
    expected_r = 99.0 / 101.0          # prev_close=101, 分红 2
    assert rows[0]["ratio"] == pytest.approx(expected_r, rel=1e-6)
    assert rows[1]["ratio"] == pytest.approx(expected_r, rel=1e-6)
    assert rows[2]["ratio"] == pytest.approx(1.0)
    # 段内必须是常数 —— 腾讯那条路在这里会漂(它减的是固定金额), 这正是差异来源
    assert rows[0]["ratio"] == rows[1]["ratio"]


def test_adj_ratios_hfq_anchors_on_first_day(monkeypatch):
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.0)])
    _patch_bars(monkeypatch)
    data = adj_ratios("600519", mode="hfq", limit=640)["data"]
    ratios = [r["ratio"] for r in data["ratios"]]
    assert ratios[0] == pytest.approx(1.0)
    assert ratios[-1] == pytest.approx(101.0 / 99.0, rel=1e-6)
    assert ratios[2] == ratios[3] == ratios[-1]


def test_adj_ratios_accumulates_multiple_events(monkeypatch):
    _patch_events(monkeypatch, [
        _rec(day=6, category=1, fenhong=1.0),
        _rec(day=8, category=1, fenhong=2.0),
    ])
    _patch_bars(monkeypatch)
    data = adj_ratios("600519", mode="qfq", limit=640)["data"]
    rows = {r["date"]: r["ratio"] for r in data["ratios"]}
    r0106 = (100.0 - 1.0) / 100.0      # idx=1, prev=100 -> 只影响 01-05
    r0108 = (102.0 - 2.0) / 102.0      # idx=3, prev=102 -> 影响 01-05..07
    assert rows["2026-01-05"] == pytest.approx(r0106 * r0108, rel=1e-6)
    # 🔴 除权日**当天**不受自己这次除权影响: 当天起就是除权后价格了,
    #    再乘一次等于把当天又砍一刀(这是最容易写错的一行)。
    assert rows["2026-01-06"] == pytest.approx(r0108, rel=1e-6)
    assert rows["2026-01-07"] == pytest.approx(r0108, rel=1e-6)
    assert rows["2026-01-08"] == pytest.approx(1.0)
    assert len(data["events_used"]) == 2


def test_adj_ratios_with_bonus_shares(monkeypatch):
    """送转要进分母: 宁德 2023-04-26 每 10 股转增 8 股 -> song=0.8。"""
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.52, songzhuangu=0.8)])
    _patch_bars(monkeypatch)
    data = adj_ratios("300750", mode="qfq", limit=640)["data"]
    expected = (101.0 - 2.52) / (1.8 * 101.0)
    assert data["ratios"][0]["ratio"] == pytest.approx(expected, rel=1e-6)


def test_adj_ratios_with_rights_issue(monkeypatch):
    """配股: 分子加回配股价×比例, 分母加比例 —— 口算不了就别改这行。"""
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=0.5,
                                     peigu=0.3, peigujia=8.0)])
    _patch_bars(monkeypatch)
    data = adj_ratios("600036", mode="qfq", limit=640)["data"]
    expected = (101.0 - 0.5 + 8.0 * 0.3) / (1.3 * 101.0)
    assert data["ratios"][0]["ratio"] == pytest.approx(expected, rel=1e-6)


def test_adj_ratios_skips_event_without_prev_close(monkeypatch):
    """除权日落在日K首日 -> 没有前收盘 -> **跳过并上报**, 不许用当日收盘顶替。"""
    _patch_events(monkeypatch, [_rec(day=5, category=1, fenhong=1.0)])
    _patch_bars(monkeypatch)
    data = adj_ratios("600519", mode="qfq", limit=640)["data"]
    assert data["skipped_events_without_prev_close"] == ["2026-01-05"]
    assert data["events_used"] == []
    assert data["has_adjust"] is False


def test_adj_ratios_ignores_event_outside_bars(monkeypatch):
    _patch_events(monkeypatch, [_rec(year=2019, month=5, day=10, category=1, fenhong=9.0)])
    _patch_bars(monkeypatch)
    data = adj_ratios("600519", mode="qfq", limit=640)["data"]
    assert data["events_used"] == []
    assert data["has_adjust"] is False


def test_adj_ratios_bad_mode():
    with pytest.raises(TdxXdxrUnavailableError, match="bad_mode"):
        adj_ratios("600519", mode="none")


def test_adj_ratios_bad_event_is_fail_closed(monkeypatch):
    """分红超过前收盘价 -> 分子 <=0, 这种数据算出来的因子是负价格, 必须拒绝。"""
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=999.0)])
    _patch_bars(monkeypatch)
    with pytest.raises(TdxXdxrUnavailableError, match="bad_event"):
        adj_ratios("600519", mode="qfq", limit=640)


def test_adj_ratios_bars_failure_is_fail_closed(monkeypatch):
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=1.0)])
    _patch_bars(monkeypatch, broken=True)
    with pytest.raises(TdxXdxrUnavailableError, match="bars_error"):
        adj_ratios("600519", mode="qfq", limit=640)


def test_adj_ratios_exposes_formula_and_deviation(monkeypatch):
    """偏差边界必须随包下发, 不能只写在仓库文档里 —— 用的人不一定读过那篇 md。"""
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.0)])
    _patch_bars(monkeypatch)
    data = adj_ratios("600519", mode="qfq", limit=640)["data"]
    assert "前收盘" in data["formula"]
    assert "0.467%" in data["deviation_note"]
    assert "回购" in data["deviation_note"]


def test_adj_ratios_cache_is_mode_aware(monkeypatch):
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.0)])
    _patch_bars(monkeypatch)
    first = adj_ratios("600519", mode="qfq", limit=640)
    second = adj_ratios("600519", mode="qfq", limit=640)
    assert first == second          # 命中缓存: 结构也一致(不是裸 body)
    assert "data" in second and "source" in second
    hfq = adj_ratios("600519", mode="hfq", limit=640)
    assert hfq["data"]["mode"] == "hfq"


def test_adj_ratios_limit_is_clamped(monkeypatch):
    _patch_events(monkeypatch, [_rec(day=7, category=1, fenhong=2.0)])
    _patch_bars(monkeypatch)
    # 越界不报错, 夹到区间里 —— 端点层另有 400 形状检查
    assert len(adj_ratios("600519", limit=5)["data"]["ratios"]) == 5
    prov.clear_cache()
    assert len(adj_ratios("600519", limit=9999)["data"]["ratios"]) == 5
