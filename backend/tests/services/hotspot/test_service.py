"""HotspotService 业务编排测试(包含历史快照回退)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.hotspot.models import HotspotResults, HotspotStock, HotspotSummary
from app.services.hotspot.service import (
    HotspotService,
    discover_hotspots,
    get_hotspot_detail,
    refresh_hotspots,
)
from app.services.hotspot.source import StubHotspotSource
from app.services.hotspot.storage import HotspotStorage, topics_path, write_topics


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _summary(topic: str, **kw) -> HotspotSummary:
    return HotspotSummary(topic=topic, name=topic, **kw)


def _stock(code: str, **kw) -> HotspotStock:
    return HotspotStock(code=code, name=code, hot_stock_score=75.0, **kw)


def test_discover_cn_writes_snapshot_and_returns_results(data_dir):
    storage = HotspotStorage(data_dir)
    results = discover_hotspots(
        data_dir, market="cn", top=20, source=StubHotspotSource(), storage=storage
    )
    assert results.is_usable
    # 走 persist 流程后应生成 topics.parquet
    from app.services.hotspot.storage import topics_path
    assert topics_path(data_dir).exists()
    state = storage.read_job_state()
    assert state["last_status"] == "success"
    assert state["provider_used"] == "stub"


def test_discover_falls_back_to_snapshot_when_source_fails(data_dir):
    """live 失败 → 仍能返回上次 snapshot, 但 quality 标 stale."""
    storage = HotspotStorage(data_dir)

    # 先固化一个 snapshot
    write_topics(data_dir, [_summary("历史题材", heat_score=70.0)])

    class _BrokenSource(StubHotspotSource):
        def discover(self, market="cn", top=10):
            # 返回空但不抛错 → 触发 fallback
            from app.services.hotspot.models import HotspotResults
            return HotspotResults([], provider_used=self.name, source_errors=["empty"], market=market)

    results = discover_hotspots(
        data_dir,
        market="cn",
        source=_BrokenSource(),
        storage=storage,
    )
    assert results.is_usable
    assert results.fallback_used is True
    assert results.stale is True
    assert results[0].topic == "历史题材"


def test_discover_empty_when_no_source_no_cache(data_dir):
    results = discover_hotspots(
        data_dir,
        market="cn",
        source=StubHotspotSource(),  # 走 stub 通常有 fixture
        storage=HotspotStorage(data_dir),
    )
    # stub 默认有 5 个 fixture,所以这里能用 —— 切到 no-source 仅作为回归
    assert results.market == "cn"


def test_discover_hk_returns_missing_mapping_fail_closed(data_dir):
    # 显式注入不支持 hk 的源: 默认源已是真实港美行业源, 测试必须隔离真实数据
    results = discover_hotspots(
        data_dir, market="hk", source=StubHotspotSource(), storage=HotspotStorage(data_dir),
    )
    assert not results.is_usable
    assert any("hk" in err or "missing" in err.lower() for err in results.source_errors)


def test_discover_us_same_missing_mapping(data_dir):
    results = discover_hotspots(
        data_dir, market="us", source=StubHotspotSource(), storage=HotspotStorage(data_dir),
    )
    assert not results.is_usable
    assert any("us" in err or "missing" in err.lower() for err in results.source_errors)


def test_detail_for_cn_existing_topic_writes_constituents(data_dir):
    detail = get_hotspot_detail(
        data_dir, "人工智能", market="cn",
        source=StubHotspotSource(), storage=HotspotStorage(data_dir),
    )
    assert detail is not None
    assert detail.summary.topic == "人工智能"
    assert detail.stock_count > 0
    # 写盘:成分股按市场分片, A 股落到 cn/constituents/ 下
    p = data_dir / "hotspot" / "cn" / "constituents" / "人工智能.parquet"
    assert p.exists()


def test_detail_for_unknown_topic_returns_none(data_dir):
    detail = get_hotspot_detail(
        data_dir, "不存在的题材", market="cn",
        source=StubHotspotSource(), storage=HotspotStorage(data_dir),
    )
    assert detail is None


def test_detail_for_unsupported_source_returns_missing_mapping_detail(data_dir):
    """source.supports(market) 为 False → missing_mapping detail。

    港美已接入本地行业聚合源, "无数据源"改由 source.supports 判定,
    不再写死 market != cn。
    """
    detail = get_hotspot_detail(
        data_dir, "人工智能", market="hk",
        source=StubHotspotSource(), storage=HotspotStorage(data_dir),
    )
    assert detail is not None
    from app.services.hotspot.models import QUALITY_MISSING
    assert detail.summary.quality_status == QUALITY_MISSING
    assert detail.summary.missing_fields  # 至少 1 个


def test_refresh_writes_job_state_on_success(data_dir):
    storage = HotspotStorage(data_dir)
    result = refresh_hotspots(data_dir, market="cn", source=StubHotspotSource(), storage=storage)
    assert result["status"] == "ok"
    assert result["rows"] > 0
    state = storage.read_job_state()
    assert state["last_status"] == "success"


def test_refresh_writes_skipped_for_unsupported_market(data_dir):
    """注入不支持该市场的 stub 源 → skipped (港美默认源已可用, 故需显式注入)。"""
    storage = HotspotStorage(data_dir)
    result = refresh_hotspots(
        data_dir, market="us", source=StubHotspotSource(), storage=storage
    )
    assert result["status"] == "skipped"
    state = storage.read_job_state()
    assert state["last_status"] == "skipped"


def test_refresh_records_error_when_source_throws(data_dir):
    class _Throws(StubHotspotSource):
        def discover(self, market="cn", top=10):
            raise RuntimeError("kaboom")

    result = refresh_hotspots(
        data_dir,
        market="cn",
        source=_Throws(),
        storage=HotspotStorage(data_dir),
    )
    assert result["status"] == "error"
    assert "kaboom" in result.get("provider", "") or result["status"] == "error"
    state = HotspotStorage(data_dir).read_job_state()
    assert state["last_status"] == "error"


def test_hotspot_service_class_initializes_storage(data_dir):
    svc = HotspotService(data_dir)
    assert svc.storage.data_dir == data_dir
    state = svc.job_state()
    assert isinstance(state, dict)


class EmptySource(StubHotspotSource):
    name = "empty_source"

    def discover(self, *, market="cn", top=20):
        return HotspotResults([], provider_used=self.name, source_errors=["upstream empty"], market=market)


class ThrowingSource(StubHotspotSource):
    name = "throwing_source"

    def discover(self, *, market="cn", top=20):
        raise TimeoutError("injected source timeout")


class StaticSource(StubHotspotSource):
    def __init__(self, result):
        self.result = result
        self.requested_limits = []

    def discover(self, *, market="cn", top=20):
        self.requested_limits.append(top)
        return self.result


def test_source_exception_returns_cached_score_and_quality(data_dir):
    storage = HotspotStorage(data_dir)
    storage.write_topics([_summary("zero", heat_score=0.0, quality_status="available", provider_used="original")])
    result = discover_hotspots(data_dir, source=ThrowingSource(), storage=storage)
    assert [item.heat_score for item in result] == [0.0]
    assert result.provider_used == "original"
    assert result.stale and result.fallback_used
    assert result.quality_status == "stale"
    assert "TimeoutError" in result.source_errors[0]
    assert result[0].stale and result[0].fallback_used
    assert result[0].quality_status == "stale"
    assert result[0].source_errors == result.source_errors


def test_source_exception_without_cache_is_explicitly_failed(data_dir):
    result = discover_hotspots(data_dir, source=ThrowingSource())
    assert not result.is_usable
    assert result.quality_status == "failed"
    assert not result.fallback_used
    assert result.stale_age_hours is None
    assert "timeout" in result.source_errors[0]


def test_degraded_source_retains_quality_and_age_across_fallback(data_dir):
    original = HotspotResults(
        [_summary("source cache", quality_status="available")], provider_used="source",
        stale=True, fallback_used=True, stale_age_hours=48.0, source_errors=["old upstream cache"],
    )
    storage = HotspotStorage(data_dir)
    result = discover_hotspots(data_dir, source=StaticSource(original), storage=storage)
    assert result.stale and result.fallback_used
    assert result.stale_age_hours == 48.0
    assert result.quality_status == "stale"
    assert result[0].stale and result[0].fallback_used
    assert result[0].stale_age_hours == 48.0
    state = storage.read_job_state()
    assert state["last_status"] == "degraded"
    assert not state.get("last_success_at", {}).get("cn")
    cached = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert cached.stale_age_hours >= 48.0
    assert "old upstream cache" in cached.source_errors
    assert original[0].stale is False
    assert original[0].topic_date == ""


def test_item_degradation_is_reflected_in_envelope_and_job_state(data_dir):
    original = HotspotResults([_summary(
        "old item", quality_status="stale", stale=True, fallback_used=True, stale_age_hours=24.0,
    )])
    result = discover_hotspots(data_dir, source=StaticSource(original))
    assert result.stale and result.fallback_used
    assert result.quality_status == "stale"
    assert result.stale_age_hours == 24.0
    assert HotspotStorage(data_dir).read_job_state()["last_status"] == "degraded"


def test_unknown_age_degraded_snapshot_does_not_borrow_previous_success_time(data_dir):
    storage = HotspotStorage(data_dir)
    discover_hotspots(data_dir, source=StubHotspotSource(), storage=storage)
    success_time = storage.read_job_state()["last_success_at"]["cn"]
    degraded = HotspotResults(
        [_summary("unknown age", quality_status="available")], stale=True, fallback_used=True,
    )
    result = discover_hotspots(data_dir, source=StaticSource(degraded), storage=storage)
    assert result.stale_age_hours is None
    assert result[0].snapshot_at == ""
    cached = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert cached.stale_age_hours is None
    assert cached[0].topic == "unknown age"
    assert storage.read_job_state()["last_success_at"]["cn"] == success_time


@pytest.mark.parametrize("attempt", ["error", "empty", "hk", "us"])
def test_attempts_do_not_change_snapshot_observation(data_dir, attempt):
    storage = HotspotStorage(data_dir)
    old_time = datetime.now(UTC) - timedelta(hours=48)
    storage.write_topics([_summary(
        "two days old", snapshot_at=old_time.isoformat(), snapshot_market="cn",
        topic_date=old_time.date().isoformat(), quality_status="available",
    )])
    storage.write_job_state({
        "last_run": old_time.isoformat(), "last_status": "success", "rows": 1, "markets": {"cn": 1},
    })
    before_bytes = topics_path(data_dir).read_bytes()
    before = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    if attempt in {"hk", "us"}:
        refresh_hotspots(data_dir, market=attempt, source=StubHotspotSource(), storage=storage)
    else:
        refresh_hotspots(data_dir, source=ThrowingSource() if attempt == "error" else EmptySource(), storage=storage)
    after = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert before.stale_age_hours >= 47.9
    assert after.stale_age_hours >= before.stale_age_hours
    assert after[0].snapshot_at == old_time.isoformat()
    assert storage.read_job_state()["last_success_at"]["cn"] == old_time.isoformat()
    assert topics_path(data_dir).read_bytes() == before_bytes


def test_legacy_success_time_is_used_but_unknown_time_remains_unknown(data_dir):
    storage = HotspotStorage(data_dir)
    storage.write_topics([_summary("legacy")])
    unknown = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert unknown.stale_age_hours is None
    old_time = datetime.now(UTC) - timedelta(hours=48)
    storage.write_job_state({"last_run": old_time.isoformat(), "last_status": "success", "rows": 1})
    refresh_hotspots(data_dir, market="hk", source=StubHotspotSource(), storage=storage)
    cached = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert cached.stale_age_hours >= 47.9


def test_cn_fallback_does_not_read_another_market_snapshot(data_dir):
    storage = HotspotStorage(data_dir)
    storage.write_topics([_summary("foreign", snapshot_market="hk")])
    result = discover_hotspots(data_dir, source=EmptySource(), storage=storage)
    assert not result.is_usable
    assert result.quality_status == "failed"


class MultiMarketSource(StubHotspotSource):
    """测试源: 同时支持 cn / hk, 每个市场返回该市场独有的 topic。"""

    name = "multi_market_stub"

    def supports(self, market: str) -> bool:
        return market in {"cn", "hk"}

    def discover(self, *, market="cn", top=20):
        items = [_summary(f"{market}-题材-{i}", quality_status="available") for i in range(3)]
        return HotspotResults(items, provider_used=self.name, market=market)


def test_hk_snapshot_does_not_wipe_cn_snapshot(data_dir):
    """回归: 先拉 A 股再拉港股, A 股快照必须还在。

    分片前 service 只把当前市场的结果整文件覆盖写 topics.parquet,
    拉完港股后 A 股快照被清空, A 股回退读不到任何东西。
    """
    storage = HotspotStorage(data_dir)
    source = MultiMarketSource()

    cn = refresh_hotspots(data_dir, market="cn", source=source, storage=storage)
    assert cn["rows"] == 3
    hk = refresh_hotspots(data_dir, market="hk", source=source, storage=storage)
    assert hk["rows"] == 3

    assert [item.topic for item in storage.read_topics(market="cn")] == ["cn-题材-0", "cn-题材-1", "cn-题材-2"]
    assert [item.topic for item in storage.read_topics(market="hk")] == ["hk-题材-0", "hk-题材-1", "hk-题材-2"]
    # live 失败回退时, A 股读到的只能是 A 股自己的快照
    fallback = discover_hotspots(data_dir, market="cn", source=EmptySource(), storage=storage)
    assert [item.topic for item in fallback] == ["cn-题材-0", "cn-题材-1", "cn-题材-2"]


class EmptyMultiMarketSource(MultiMarketSource):
    """支持 cn/hk 但每次拉新都返回空 —— 用于触发"回退持久化快照"分支。"""

    name = "empty_multi_market_source"

    def discover(self, *, market="cn", top=20):
        return HotspotResults([], provider_used=self.name, source_errors=["upstream empty"], market=market)


def test_hk_fallback_reads_hk_snapshot_not_cn(data_dir):
    """回归: 港股拉新失败时必须回退港股分片。

    read_topics 的 market 默认 cn, service 漏传时会恒读 A 股分片:
    再按 {"", "hk"} 过滤 → 恒空 → 港美的快照回退路径完全失效。
    """
    storage = HotspotStorage(data_dir)
    source = MultiMarketSource()
    refresh_hotspots(data_dir, market="hk", source=source, storage=storage)

    fallback = discover_hotspots(data_dir, market="hk", source=EmptyMultiMarketSource(), storage=storage)
    assert [item.topic for item in fallback] == ["hk-题材-0", "hk-题材-1", "hk-题材-2"]
    assert fallback.is_usable
    assert fallback.stale is True
    assert fallback.market == "hk"


def test_full_snapshot_is_independent_of_request_limit_and_refresh(data_dir):
    class ManyTopicsSource(StubHotspotSource):
        def __init__(self):
            self.requested_limits = []

        def discover(self, *, market="cn", top=20):
            self.requested_limits.append(top)
            items = [_summary(f"topic-{index}", quality_status="available") for index in range(25)]
            return HotspotResults(items[:top] if top else items, provider_used=self.name, market=market)

    source = ManyTopicsSource()
    storage = HotspotStorage(data_dir)
    small = discover_hotspots(data_dir, source=source, top=2, storage=storage)
    assert len(small) == 2
    assert len(storage.read_topics()) == 25
    fallback = discover_hotspots(data_dir, source=EmptySource(), top=20, storage=storage)
    assert len(fallback) == 20
    refresh_result = refresh_hotspots(data_dir, source=source, storage=storage)
    assert refresh_result["rows"] == 25
    assert source.requested_limits == [0, 0]
    assert len(storage.read_topics()) == 25


def test_refresh_parameter_remains_source_first_placeholder(data_dir):
    source = StaticSource(HotspotResults([_summary("live", quality_status="available")]))
    discover_hotspots(data_dir, source=source, refresh=False)
    discover_hotspots(data_dir, source=source, refresh=True)
    assert source.requested_limits == [0, 0]


def test_refresh_degraded_data_does_not_report_success(data_dir):
    source = StaticSource(HotspotResults(
        [_summary("cached", quality_status="available")], stale=True, fallback_used=True, stale_age_hours=12.0,
    ))
    result = refresh_hotspots(data_dir, source=source)
    assert result["status"] == "degraded"
    assert result["rows"] == 1
    assert HotspotStorage(data_dir).read_job_state()["last_status"] == "degraded"
