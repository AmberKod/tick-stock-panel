"""Hotspot 数据模型单元测试."""
from __future__ import annotations

from app.services.hotspot.models import (
    HotspotResults,
    HotspotStock,
    HotspotSummary,
    SourceError,
)


def test_summary_defaults():
    s = HotspotSummary(topic="半导体")
    assert s.topic == "半导体"
    assert s.heat_score == 50.0  # 默认 50.0
    assert s.stage == "初次异动"
    assert s.quality_status == "partial"
    assert s.aliases == [] and s.leaders == []
    assert s.sample_stock_count == 0


def test_results_extends_list():
    r = HotspotResults(
        [HotspotSummary(topic="T1"), HotspotSummary(topic="T2")],
        provider_used="stub",
        market="cn",
    )
    assert isinstance(r, list)
    assert len(r) == 2
    assert r[0].topic == "T1"
    assert r.provider_used == "stub"
    assert r.market == "cn"
    assert r.is_usable is True


def test_empty_results_not_usable():
    r = HotspotResults([], market="hk")
    assert r.is_usable is False
    assert r.market == "hk"


def test_source_error_serialization():
    e1 = SourceError(provider="akshare", method="discover", message="timeout")
    e2 = SourceError(provider="akshare", method="discover", message="timeout")
    e3 = SourceError(provider="akshare", method="discover", message="other")
    r = HotspotResults(
        [],
        source_errors=[e1, e2, e3, "plain string", "  "],
        market="cn",
    )
    # 重复 e1 被去重;末尾空白被丢;最终剩余 3 条独立记录
    assert len(r.source_errors) == 3
    assert "akshare.discover: timeout" in r.source_errors
    assert "akshare.discover: other" in r.source_errors
    assert "plain string" in r.source_errors


def test_quality_status_constants_match_strings():
    """QUALITY_* 模块常量与字符串值一一对应。"""
    from app.services.hotspot.models import (
        QUALITY_FAILED,
        QUALITY_MISSING,
        QUALITY_OK,
        QUALITY_PARTIAL,
        QUALITY_STALE,
    )
    assert QUALITY_OK == "available"
    assert QUALITY_PARTIAL == "partial"
    assert QUALITY_STALE == "stale"
    assert QUALITY_FAILED == "failed"
    assert QUALITY_MISSING == "missing_mapping"


def test_hotspot_stages_canonical():
    """5 段生命周期必须完整保留顺序。"""
    from app.services.hotspot.models import HOTSPOT_STAGES
    assert HOTSPOT_STAGES == ("初次异动", "确认扩散", "加速主升", "分歧放量", "降温退潮")
    # 类型别名可直接用 str 比较
    stage = "确认扩散"
    assert stage in HOTSPOT_STAGES


def test_hotspot_stock_defaults():
    s = HotspotStock(code="000001.SZ")
    assert s.code == "000001.SZ"
    assert s.role == ""
    assert s.hot_stock_score == 0.0
    assert s.is_limit_up is False
    # amount 默认 None → safe_float 不调用也不应崩
    assert s.amount is None


def test_results_expose_machine_readable_quality():
    partial = HotspotResults([HotspotSummary(topic="partial")])
    assert partial.quality_status == "partial"
    assert partial.to_dict()["quality_status"] == "partial"
    missing = HotspotResults([], market="hk", quality_status="missing_mapping")
    assert missing.to_dict()["quality_status"] == "missing_mapping"


def test_summary_observation_is_unknown_until_source_identifies_it():
    summary = HotspotSummary(topic="old snapshot")
    assert summary.snapshot_at == ""
    assert summary.snapshot_market == ""
