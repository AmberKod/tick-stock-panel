"""Hotspot 数据源(StubHotspotSource)单元测试."""
from __future__ import annotations

import pytest

from app.services.hotspot import source as source_module
from app.services.hotspot.models import (
    QUALITY_PARTIAL,
    HotspotDetail,
    HotspotResults,
    HotspotSummary,
)
from app.services.hotspot.source import (
    HotspotSource,
    StubHotspotSource,
    select_source,
)


def test_stub_source_supports_only_cn():
    s = StubHotspotSource()
    assert s.supports("cn") is True
    assert s.supports("hk") is False
    assert s.supports("us") is False
    assert s.name == "stub"


def test_stub_discover_cn_returns_usable_results():
    s = StubHotspotSource()
    results = s.discover(market="cn", top=20)
    assert results.is_usable
    assert results.market == "cn"
    assert len(results) > 0
    for summary in results:
        assert summary.heat_score >= 0 and summary.heat_score <= 100
        assert summary.topic
        # 三维度趋势从无观测值 → 未判定(None); 绝不允许兜成"初次异动"
        assert summary.stage is None
        assert summary.trend_score is None
        assert summary.persistence_score is None
        assert summary.cooling_score is None


def test_stub_discover_top_limit():
    s = StubHotspotSource()
    results = s.discover(market="cn", top=2)
    assert len(results) == 2


def test_stub_discover_zero_top_returns_all():
    s = StubHotspotSource()
    results = s.discover(market="cn", top=0)
    assert [item.topic for item in results] == ["人工智能", "新能源车", "半导体", "白酒", "光伏"]
    limited = s.discover(market="cn", top=2)
    assert [item.topic for item in limited] == [item.topic for item in results[:2]]


def test_stub_discover_hk_returns_empty_with_error():
    s = StubHotspotSource()
    results = s.discover(market="hk", top=10)
    assert len(results) == 0
    assert results.market == "hk"
    assert any("hk" in err for err in results.source_errors)


def test_stub_fetch_detail_existing_topic():
    s = StubHotspotSource()
    detail = s.fetch_detail("人工智能", market="cn")
    assert detail is not None
    assert isinstance(detail, HotspotDetail)
    assert detail.summary.topic == "人工智能"
    assert detail.stock_count > 0
    # 龙头股应有 role
    for s_ in detail.summary.leader_stocks:
        assert s_.role == "核心龙头"


def test_stub_fetch_detail_unknown_topic_returns_none():
    s = StubHotspotSource()
    detail = s.fetch_detail("不存在的题材", market="cn")
    assert detail is None


def test_stub_fetch_detail_hk_returns_none():
    s = StubHotspotSource()
    detail = s.fetch_detail("人工智能", market="hk")
    assert detail is None


def test_select_source_default_cn_returns_local_concept():
    """A 股默认走本地概念源 (2026-09-18 起替换 akshare), 且复用单例。

    akshare 东财源依赖 push2.eastmoney.com, 代理环境不可达 (28.7s 超时后空
    列表), 热点页长期空白; 参考项目本来就本地算, 与港美行业源同口径。
    """
    from app.services.hotspot.cn_concept_source import CnConceptHotspotSource

    src = select_source("cn")
    assert isinstance(src, CnConceptHotspotSource)
    assert select_source("cn") is src


def test_select_source_other_markets_fallback(monkeypatch):
    class _Override(HotspotSource):
        name = "custom"
        def supports(self, market): return True
        def discover(self, market="cn", top=10):
            return HotspotResults(
                [HotspotSummary(topic="XXX", heat_score=70.0)],
                provider_used="custom",
                market=market,
            )
    override = _Override()
    src = select_source("hk", override=override)
    assert src is override


def test_stub_source_inherits_abstract_contract():
    s = StubHotspotSource()
    # base class methods surface in subclass
    assert isinstance(s, HotspotSource)


class _ProbeList(HotspotSource):
    """列表式可遍历的 source 用于完整性检查."""

    def discover(self, market="cn", top=10):
        return HotspotResults([], market=market)


# 额外测试:如果覆盖 supports(),应能在 select_source 之前被识别。
def test_custom_source_supports_overrides():
    src = _ProbeList()
    src.supports = lambda m: m == "xx"  # type: ignore[assignment]
    assert src.supports("xx") is True
    assert src.supports("yy") is False


@pytest.mark.parametrize("method", ["discover", "fetch_detail"])
def test_missing_constituents_returns_partial_without_exception(monkeypatch, method):
    monkeypatch.setitem(source_module._STUB_CONSTITUENTS_A, "人工智能", [])
    source = StubHotspotSource()
    if method == "discover":
        results = source.discover(market="cn", top=20)
        summary = next(item for item in results if item.topic == "人工智能")
        assert len(results) == 5
    else:
        detail = source.fetch_detail("人工智能", market="cn")
        assert detail is not None
        assert detail.stocks == []
        assert detail.stock_count == 0
        summary = detail.summary
    assert summary.quality_status == QUALITY_PARTIAL
    assert summary.missing_fields == ["stocks", "leader_stocks"]
    assert summary.leader_stocks == []
    assert summary.leaders == []


@pytest.mark.parametrize("flag,expected", [(False, False), (True, True), (None, False), ("false", False), ("true", True)])
def test_source_respects_provided_limit_up_flag(flag, expected):
    row = {"code": "300001.SZ", "change_pct": 0.10, "amount": 1e8, "is_limit_up": flag}
    stock = StubHotspotSource._coerce_stocks([row], source="stub.constituents")[0]
    assert stock.is_limit_up is expected
    assert stock.hot_stock_score == pytest.approx(79.0 if expected else 71.0)


def test_source_missing_limit_up_flag_is_not_inferred():
    row = {"code": "300001.SZ", "change_pct": 0.10, "amount": 1e8}
    stock = StubHotspotSource._coerce_stocks([row], source="stub.constituents")[0]
    assert stock.is_limit_up is False
    assert stock.hot_stock_score == pytest.approx(71.0)


def test_stub_decimal_scoring_drives_constituent_roles():
    detail = StubHotspotSource().fetch_detail("新能源车", market="cn")
    assert detail is not None
    assert [stock.code for stock in detail.stocks] == ["300750.SZ", "002594.SZ", "300014.SZ"]
    stocks = {stock.code: stock for stock in detail.stocks}
    assert stocks["300750.SZ"].hot_stock_score == pytest.approx(88.9949)
    assert stocks["002594.SZ"].hot_stock_score == pytest.approx(82.4773)
    assert stocks["002594.SZ"].role == "核心龙头"
    assert stocks["300014.SZ"].hot_stock_score == pytest.approx(78.3719)
    assert stocks["300014.SZ"].role == "助攻"
