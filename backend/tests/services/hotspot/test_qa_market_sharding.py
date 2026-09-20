"""QA 独立验证: 热点快照按 market 分片(cn/hk/us)互不覆盖 + 迁移边界。

与 test_storage.py / test_service.py 分开, 由 QA 以 fresh eyes 编写,
重点覆盖 A/B 两类场景里实现方未覆盖的方向与边界。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services.hotspot.models import HotspotResults, HotspotSummary
from app.services.hotspot.service import discover_hotspots, refresh_hotspots
from app.services.hotspot.source import StubHotspotSource
from app.services.hotspot.storage import (
    HotspotStorage,
    hotspot_root,
    legacy_topics_path,
    read_topics,
    topics_path,
    write_topics,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _summary(topic: str, snapshot_market: str = "cn", **kw) -> HotspotSummary:
    return HotspotSummary(topic=topic, name=topic, snapshot_market=snapshot_market, **kw)


def _topics_of(data_dir: Path, market: str) -> list[str]:
    return [item.topic for item in read_topics(data_dir, market=market)]


# ---------------------------------------------------------------------------
# A. 证明分片真的隔离(正反两个方向 + 三市场轮转)
# ---------------------------------------------------------------------------


def test_forward_cn_then_hk_keeps_cn(data_dir):
    """原始 bug 场景: 写 A 股 → 写港股 → 读 A 股必须还在。"""
    write_topics(data_dir, [_summary("cn-1", snapshot_market="cn")], market="cn")
    write_topics(data_dir, [_summary("hk-1", snapshot_market="hk")], market="hk")
    assert _topics_of(data_dir, "cn") == ["cn-1"]


def test_reverse_hk_then_cn_keeps_hk(data_dir):
    """反向: 写港股 → 写 A 股 → 读港股必须还在。"""
    write_topics(data_dir, [_summary("hk-1", snapshot_market="hk")], market="hk")
    write_topics(data_dir, [_summary("cn-1", snapshot_market="cn")], market="cn")
    assert _topics_of(data_dir, "hk") == ["hk-1"]
    assert _topics_of(data_dir, "cn") == ["cn-1"]


@pytest.mark.parametrize(
    "order",
    [
        ("cn", "hk", "us"),
        ("us", "hk", "cn"),
        ("hk", "cn", "us"),
        ("us", "cn", "hk"),
        ("cn", "us", "hk"),
        ("hk", "us", "cn"),
    ],
)
def test_three_markets_rotation_in_any_order_keeps_all(data_dir, order):
    """三个市场任意轮转顺序, 每个市场的快照都必须完整保留。"""
    expected = {}
    for market in order:
        topics = [f"{market}-t{i}" for i in range(3)]
        expected[market] = topics
        write_topics(
            data_dir,
            [_summary(topic, snapshot_market=market) for topic in topics],
            market=market,
        )

    for market in ("cn", "hk", "us"):
        assert _topics_of(data_dir, market) == expected[market]


def test_repeated_write_to_same_market_replaces_only_that_market(data_dir):
    """同一市场重复写仍是整文件覆盖, 但别的市场不受牵连。"""
    write_topics(data_dir, [_summary("cn-old", snapshot_market="cn")], market="cn")
    write_topics(data_dir, [_summary("hk-1", snapshot_market="hk")], market="hk")
    # 第二次写 A 股, 覆盖自己的旧快照
    write_topics(data_dir, [_summary("cn-new", snapshot_market="cn")], market="cn")
    assert _topics_of(data_dir, "cn") == ["cn-new"]
    assert _topics_of(data_dir, "hk") == ["hk-1"]


# ---------------------------------------------------------------------------
# A4. service 回退路径 —— 三个市场各自回退自己的分片
# ---------------------------------------------------------------------------


class ThreeMarketSource(StubHotspotSource):
    """同时支持 cn/hk/us, 每市场返回该市场独有 topic。"""

    name = "three_market_stub"

    def supports(self, market: str) -> bool:
        return market in {"cn", "hk", "us"}

    def discover(self, *, market="cn", top=20):
        items = [_summary(f"{market}-live-{i}", quality_status="available") for i in range(2)]
        return HotspotResults(items, provider_used=self.name, market=market)


class FailingThreeMarketSource(ThreeMarketSource):
    """拉新恒失败, 用于触发回退分支。"""

    name = "failing_three_market"

    def discover(self, *, market="cn", top=20):
        return HotspotResults([], provider_used=self.name, source_errors=["upstream down"], market=market)


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_fallback_reads_own_market_snapshot(data_dir, market):
    """拉新失败 → 每个市场都只回退自己那一份, 且不能串到别的市场。"""
    storage = HotspotStorage(data_dir)
    for other in ("cn", "hk", "us"):
        write_topics(
            data_dir,
            [_summary(f"{other}-cached-{i}", snapshot_market=other) for i in range(3)],
            market=other,
        )

    fallback = discover_hotspots(data_dir, market=market, source=FailingThreeMarketSource(), storage=storage)
    assert fallback.is_usable, f"{market} 回退结果不可用"
    assert fallback.fallback_used is True
    assert fallback.market == market
    assert [item.topic for item in fallback] == [f"{market}-cached-{i}" for i in range(3)]


def test_fallback_does_not_leak_across_markets_after_refresh(data_dir):
    """走真实 refresh 落盘后, 港美回退都不能拿到 A 股的行。"""
    storage = HotspotStorage(data_dir)
    source = ThreeMarketSource()
    for market in ("cn", "hk", "us"):
        result = refresh_hotspots(data_dir, market=market, source=source, storage=storage)
        assert result["rows"] == 2

    for market in ("hk", "us"):
        fallback = discover_hotspots(data_dir, market=market, source=FailingThreeMarketSource(), storage=storage)
        assert sorted(item.topic for item in fallback) == [f"{market}-live-0", f"{market}-live-1"]


# ---------------------------------------------------------------------------
# B. 迁移边界
# ---------------------------------------------------------------------------


def _write_legacy(data_dir: Path, rows: list[dict]) -> Path:
    legacy = legacy_topics_path(data_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(legacy)
    return legacy


def test_legacy_mixed_markets_split_correctly_and_file_removed(data_dir):
    """混合 cn/hk/us/空 的旧文件: 每行落到自己的分片, 空值归 cn, 旧文件删除。"""
    legacy = _write_legacy(data_dir, [
        {"topic": "A-1", "snapshot_market": "cn"},
        {"topic": "H-1", "snapshot_market": "hk"},
        {"topic": "U-1", "snapshot_market": "us"},
        {"topic": "A-2", "snapshot_market": "cn"},
        {"topic": "OLD-NO-MARKET", "snapshot_market": ""},
    ])

    assert _topics_of(data_dir, "cn") == ["A-1", "A-2", "OLD-NO-MARKET"]
    assert _topics_of(data_dir, "hk") == ["H-1"]
    assert _topics_of(data_dir, "us") == ["U-1"]
    assert not legacy.exists(), "迁移成功后旧文件应删除"


def test_legacy_missing_snapshot_market_column_goes_to_cn(data_dir):
    """更旧的快照根本没有 snapshot_market 列 → 全部归 cn(与迁移前读取语义一致)。"""
    _write_legacy(data_dir, [{"topic": "ancient-1"}])
    assert _topics_of(data_dir, "cn") == ["ancient-1"]
    assert _topics_of(data_dir, "hk") == []


def test_legacy_uppercase_market_is_normalized(data_dir):
    """HK / Cn 这类大小写混入的行要归一化到正确分片。"""
    _write_legacy(data_dir, [
        {"topic": "cap-hk", "snapshot_market": "HK"},
        {"topic": "cap-cn", "snapshot_market": "Cn"},
    ])
    assert _topics_of(data_dir, "hk") == ["cap-hk"]
    assert _topics_of(data_dir, "cn") == ["cap-cn"]


@pytest.mark.xfail(
    reason=(
        "已知偏差(QA-2026-09-20): _snapshot_market_of() 把 cn/hk/us 之外的 "
        "snapshot_market(如 jp)一律归到 cn 分片, 与 storage.py:52 "
        "_SUPPORTED_TOPIC_MARKETS 上方注释『其余值单独归目录, 绝不落到 cn 桶』相矛盾。"
        "修复 _snapshot_market_of 后本用例应转为通过。"
    ),
)
def test_legacy_unknown_market_row_should_not_pollute_cn_shard(data_dir):
    """理想行为: 未知市场(jp)的旧行进自己的分片, 不落到 cn 桶。"""
    _write_legacy(data_dir, [
        {"topic": "jp-row", "snapshot_market": "jp"},
        {"topic": "cn-row", "snapshot_market": "cn"},
    ])

    assert _topics_of(data_dir, "cn") == ["cn-row"]
    assert _topics_of(data_dir, "jp") == ["jp-row"]


def test_legacy_unknown_market_row_never_leaks_into_other_markets(data_dir):
    """安全底线(当前行为): 未知市场的旧行即便落在 cn 文件里, 也不能泄漏给任何市场。

    这是本次改动真正要守的不变量 —— 跨市场不能串数据。
    注意: 当前实现会把 jp 行写进 cn 分片的 parquet 文件(service 层再按
    snapshot_market 过滤掉), 所以这些行对自身市场也是不可达的(被搁死)。
    若后续把 _snapshot_market_of 改成按原市场归档, 本用例仍然应当通过。
    """
    _write_legacy(data_dir, [
        {"topic": "jp-row", "snapshot_market": "jp"},
        {"topic": "cn-row", "snapshot_market": "cn"},
        {"topic": "hk-row", "snapshot_market": "hk"},
    ])

    storage = HotspotStorage(data_dir)
    for market, expected in (("cn", ["cn-row"]), ("hk", ["hk-row"])):
        result = discover_hotspots(data_dir, market=market, source=FailingThreeMarketSource(), storage=storage)
        assert sorted(item.topic for item in result) == expected, f"{market} 回退读到了别的市场的行"


def test_writing_unknown_market_uses_own_directory(data_dir):
    """写入 market='jp' 落自己的目录, 不污染 cn。"""
    write_topics(data_dir, [_summary("jp-1", snapshot_market="jp")], market="jp")
    write_topics(data_dir, [_summary("cn-1", snapshot_market="cn")], market="cn")

    assert topics_path(data_dir, "jp").parent.name == "jp"
    assert _topics_of(data_dir, "cn") == ["cn-1"]
    assert _topics_of(data_dir, "jp") == ["jp-1"]


def test_corrupt_legacy_file_is_preserved(data_dir):
    """旧文件损坏: 不能静默删掉用户数据, 要原样保留等下次重试。"""
    legacy = legacy_topics_path(data_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"this is definitely not a parquet file")

    assert read_topics(data_dir, market="cn") == []
    assert legacy.exists(), "损坏的旧文件必须保留, 不能静默删除"

    # 修好后能正常迁移
    legacy.unlink()
    _write_legacy(data_dir, [{"topic": "recovered", "snapshot_market": "cn"}])
    assert _topics_of(data_dir, "cn") == ["recovered"]


def test_write_empty_deletes_only_current_market_shard(data_dir):
    """write_topics([]) 只删当前市场分片, 其他市场不受影响。"""
    for market in ("cn", "hk", "us"):
        write_topics(data_dir, [_summary(f"{market}-1", snapshot_market=market)], market=market)

    write_topics(data_dir, [], market="hk")

    assert not topics_path(data_dir, "hk").exists()
    assert _topics_of(data_dir, "cn") == ["cn-1"]
    assert _topics_of(data_dir, "us") == ["us-1"]


def test_write_empty_triggers_migration_without_losing_other_markets(data_dir):
    """write_topics([]) 会顺带完成旧文件迁移, 但只清空目标市场, 其他市场必须完整。

    说明(QA 复核结论): 实现方在 write/read 前统一先跑一次迁移, 所以任何一次
    write_topics(含空写)都会把旧的不分片文件拆走并删除。这不是"静默丢数据" ——
    迁移是先于清空完成的, 校验点在于: 非目标市场的行必须落到各自分片且不丢。
    """
    legacy = _write_legacy(data_dir, [
        {"topic": "old-cn", "snapshot_market": "cn"},
        {"topic": "old-hk", "snapshot_market": "hk"},
        {"topic": "old-us", "snapshot_market": "us"},
    ])
    write_topics(data_dir, [], market="hk")

    # 迁移已完成 → 旧整片文件不再保留
    assert not legacy.exists()
    # 只有目标市场被清空
    assert _topics_of(data_dir, "hk") == []
    # 其他市场的历史行必须完好保留
    assert _topics_of(data_dir, "cn") == ["old-cn"]
    assert _topics_of(data_dir, "us") == ["old-us"]


def test_write_empty_clears_current_market_even_if_just_migrated(data_dir):
    """空写的语义就是"清空该市场", 刚迁移进来的同名数据同样被清空。"""
    _write_legacy(data_dir, [{"topic": "old-cn", "snapshot_market": "cn"}])
    write_topics(data_dir, [], market="cn")
    assert _topics_of(data_dir, "cn") == []


def test_existing_shard_wins_during_migration(data_dir):
    """迁移时目标分片已存在: 同名 topic 以分片内现有行为准。"""
    write_topics(data_dir, [_summary("dup", heat_score=99.0, snapshot_market="cn")], market="cn")
    _write_legacy(data_dir, [{"topic": "dup", "snapshot_market": "cn", "heat_score": 1.0}])

    rows = read_topics(data_dir, market="cn")
    assert [r.topic for r in rows] == ["dup"]
    assert rows[0].heat_score == 99.0


def test_migration_runs_once_and_is_idempotent(data_dir):
    """反复读写不应重复追加迁移行。"""
    _write_legacy(data_dir, [{"topic": "once", "snapshot_market": "cn"}])
    for _ in range(3):
        read_topics(data_dir, market="cn")
        read_topics(data_dir, market="hk")
    assert _topics_of(data_dir, "cn") == ["once"]
    assert not legacy_topics_path(data_dir).exists()


def test_shards_live_under_hotspot_root(data_dir):
    """目录布局: hotspot/<market>/topics.parquet。"""
    write_topics(data_dir, [_summary("u", snapshot_market="us")], market="us")
    assert topics_path(data_dir, "us") == hotspot_root(data_dir) / "us" / "topics.parquet"
    assert topics_path(data_dir, "us").exists()
