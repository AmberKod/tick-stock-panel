"""Hotspot 持久化(parquet round-trip / path 安全)测试."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Barrier

import polars as pl
import pytest

from app.services.hotspot.models import HotspotStock, HotspotSummary
from app.services.hotspot.storage import (
    HotspotStorage,
    constituents_dir,
    delete_constituents,
    history_dir,
    hotspot_root,
    job_state_path,
    legacy_topics_path,
    read_constituents,
    read_topics,
    safe_topic_filename,
    topics_path,
    write_constituents,
    write_topics,
)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


def _summary(topic: str, **kw) -> HotspotSummary:
    return HotspotSummary(topic=topic, name=topic, heat_score=80.0, **kw)


def _stock(code: str, **kw) -> HotspotStock:
    return HotspotStock(code=code, name=code, hot_stock_score=75.0, **kw)


def test_topic_paths_construction(data_dir):
    assert hotspot_root(data_dir).name == "hotspot"
    assert topics_path(data_dir).name == "topics.parquet"
    assert constituents_dir(data_dir).name == "constituents"
    assert history_dir(data_dir).name == "history"


def test_topics_path_is_sharded_per_market(data_dir):
    """每个市场一份 topics 分片, 互不覆盖。"""
    assert topics_path(data_dir, "cn") == hotspot_root(data_dir) / "cn" / "topics.parquet"
    assert topics_path(data_dir, "hk") == hotspot_root(data_dir) / "hk" / "topics.parquet"
    assert topics_path(data_dir, "us") == hotspot_root(data_dir) / "us" / "topics.parquet"
    # 未知市场不能落进 A 股分片冒充 A 股数据
    assert topics_path(data_dir, "jp").parent.name == "jp"
    assert legacy_topics_path(data_dir) == hotspot_root(data_dir) / "topics.parquet"


def test_market_topics_snapshots_do_not_overwrite_each_other(data_dir):
    """回归: 写 A 股 → 写港股 → 读 A 股, 必须仍能读到 A 股数据。

    分片前所有市场共写一个 topics.parquet(整文件覆盖), 查完港股再查 A 股
    会把 A 股快照清空。
    """
    write_topics(data_dir, [_summary("A股题材", snapshot_market="cn")], market="cn")
    write_topics(data_dir, [_summary("港股题材", snapshot_market="hk")], market="hk")

    assert [item.topic for item in read_topics(data_dir, market="cn")] == ["A股题材"]
    assert [item.topic for item in read_topics(data_dir, market="hk")] == ["港股题材"]
    assert read_topics(data_dir, market="us") == []


def test_legacy_unsharded_topics_are_split_by_snapshot_market(data_dir):
    """旧的不分片 topics.parquet 首次访问时按 snapshot_market 拆到各市场。"""
    legacy = legacy_topics_path(data_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "topic": ["A股题材", "港股题材", "无市场标记的旧行"],
        "snapshot_market": ["cn", "hk", ""],
    }).write_parquet(legacy)

    assert [item.topic for item in read_topics(data_dir, market="cn")] == ["A股题材", "无市场标记的旧行"]
    assert [item.topic for item in read_topics(data_dir, market="hk")] == ["港股题材"]
    # 拆分成功后旧文件不再保留, 且不会重跑第二次
    assert not legacy.exists()
    assert read_topics(data_dir, market="us") == []


def test_safe_topic_filename_handles_special_chars():
    assert safe_topic_filename("人工智能") == "人工智能"
    name = safe_topic_filename("chip?eta/<script>")
    assert "?" not in name and "/" not in name and "<" not in name
    assert safe_topic_filename("  ") == "unnamed"
    assert safe_topic_filename("x" * 200, )[:96]


def test_write_read_topics_roundtrip(data_dir):
    summaries = [
        _summary("人工智能", source="concept", change_pct=0.05),
        _summary("半导体", source="industry", change_pct=0.03),
    ]
    out = write_topics(data_dir, summaries)
    assert out.exists()
    back = read_topics(data_dir)
    assert len(back) == 2
    assert back[0].topic == "人工智能"
    assert back[1].source == "industry"
    assert isinstance(back[0].heat_score, float)


def test_write_topics_empty_removes_file(data_dir):
    write_topics(data_dir, [_summary("A")])
    assert topics_path(data_dir).exists()
    write_topics(data_dir, [])
    assert not topics_path(data_dir).exists()


def test_write_read_constituents_roundtrip(data_dir):
    stocks = [
        _stock("300474.SZ", change_pct=0.10, role="核心龙头"),
        _stock("603019.SH", change_pct=0.07, role="助攻"),
    ]
    p = write_constituents(data_dir, "人工智能", stocks)
    assert p is not None and p.exists()
    back = read_constituents(data_dir, "人工智能")
    assert len(back) == 2
    assert back[0].code == "300474.SZ"
    assert back[1].role == "助攻"


def test_write_constituents_empty_removes(data_dir):
    write_constituents(data_dir, "人工智能", [_stock("X")])
    write_constituents(data_dir, "人工智能", [])
    assert read_constituents(data_dir, "人工智能") == []


def test_constituents_of_same_topic_do_not_overwrite_across_markets(data_dir):
    """同名 topic 跨市场互不覆盖 —— 港美产出行业名、A 股是概念名, 撞名概率不低。

    改前 constituents 只按 topic 名分文件, 写港股"半导体"会把 A 股"半导体"整份冲掉。
    """
    write_constituents(data_dir, "半导体", [_stock("300474.SZ", role="A股龙头")], market="cn")
    write_constituents(data_dir, "半导体", [_stock("00700.HK", role="港股龙头")], market="hk")

    cn = read_constituents(data_dir, "半导体", market="cn")
    hk = read_constituents(data_dir, "半导体", market="hk")
    assert [s.code for s in cn] == ["300474.SZ"]
    assert [s.code for s in hk] == ["00700.HK"]


def test_constituents_three_markets_roundtrip(data_dir):
    for market, code in (("cn", "300474.SZ"), ("hk", "00700.HK"), ("us", "NVDA")):
        write_constituents(data_dir, "半导体", [_stock(code)], market=market)
    assert [s.code for s in read_constituents(data_dir, "半导体", market="cn")] == ["300474.SZ"]
    assert [s.code for s in read_constituents(data_dir, "半导体", market="hk")] == ["00700.HK"]
    assert [s.code for s in read_constituents(data_dir, "半导体", market="us")] == ["NVDA"]


def test_delete_constituents_only_removes_target_market(data_dir):
    write_constituents(data_dir, "半导体", [_stock("300474.SZ")], market="cn")
    write_constituents(data_dir, "半导体", [_stock("00700.HK")], market="hk")

    delete_constituents(data_dir, "半导体", market="cn")

    assert read_constituents(data_dir, "半导体", market="cn") == []
    assert [s.code for s in read_constituents(data_dir, "半导体", market="hk")] == ["00700.HK"]


def test_constituents_are_sharded_under_market_dir(data_dir):
    path = write_constituents(data_dir, "半导体", [_stock("NVDA")], market="us")
    assert path is not None
    # 必须落在 <market>/constituents/ 下, 而不是旧的扁平 constituents/
    assert path.parent.name == "constituents"
    assert path.parent.parent.name == "us"
    assert not (data_dir / "hotspot" / "constituents").exists()


def test_write_constituents_empty_removes_only_target_market(data_dir):
    write_constituents(data_dir, "半导体", [_stock("300474.SZ")], market="cn")
    write_constituents(data_dir, "半导体", [_stock("00700.HK")], market="hk")

    write_constituents(data_dir, "半导体", [], market="cn")

    assert read_constituents(data_dir, "半导体", market="cn") == []
    assert [s.code for s in read_constituents(data_dir, "半导体", market="hk")] == ["00700.HK"]


def test_storage_class_delegates(data_dir):
    storage = HotspotStorage(data_dir)
    storage.write_topics([_summary("A"), _summary("B")])
    storage.write_constituents("A", [_stock("X")])
    storage.append_history([_summary("A", stage="加速主升")], market="cn")

    assert len(storage.read_topics()) == 2
    assert storage.read_constituents("A")[0].code == "X"
    history = list(storage.load_history(filename="topics.jsonl"))
    # 仅 A 出现在 history(append_history 写在 topics.jsonl)
    topics_in_history = [row["topic"] for row in history]
    assert "A" in topics_in_history
    assert "B" not in topics_in_history


def test_storage_constituents_history(data_dir):
    storage = HotspotStorage(data_dir)
    storage.append_constituents_history(
        "人工智能",
        [_stock("300474.SZ"), _stock("603019.SH")],
        market="cn",
    )
    history = list(storage.load_history(filename="constituents.jsonl"))
    codes = [r["code"] for r in history]
    assert codes == ["300474.SZ", "603019.SH"]
    assert all(r["topic"] == "人工智能" for r in history)
    assert all(r["market"] == "cn" for r in history)


def test_append_history_writes_market_and_filter_selects_it(data_dir):
    """history 是三市场共用的一个 jsonl: 同名 topic 只能靠 market 区分。"""
    storage = HotspotStorage(data_dir)
    storage.append_history([_summary("半导体")], market="cn")
    storage.append_history([_summary("半导体")], market="hk")

    all_rows = list(storage.load_history(filename="topics.jsonl"))
    assert len(all_rows) == 2
    assert [r["market"] for r in all_rows] == ["cn", "hk"]

    hk_rows = list(storage.load_history(filename="topics.jsonl", market="hk"))
    assert [r["topic"] for r in hk_rows] == ["半导体"]
    assert all(r["market"] == "hk" for r in hk_rows)


def test_legacy_rows_without_market_never_match_market_filter(data_dir):
    """存量老行没有 market 字段 → market 未知 → 过滤时一律不匹配(绝不倒推 cn)。"""
    storage = HotspotStorage(data_dir)
    target = history_dir(data_dir) / "topics.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        # 老行: source 看着像 A 股, 但落盘时没写 market —— 那只是旁证, 不是事实
        handle.write(
            '{"generated_at": "2026-09-01T00:00:00+00:00", "topic": "老行",'
            ' "source": "cn_local_concept"}\n',
        )
    storage.append_history([_summary("新行")], market="cn")

    # 不过滤: 老行照出(不丢数据)
    assert [r["topic"] for r in storage.load_history(filename="topics.jsonl")] == ["老行", "新行"]
    # 按 cn 过滤: 只匹配显式写了 market 的新行
    assert [r["topic"] for r in storage.load_history(filename="topics.jsonl", market="cn")] == ["新行"]
    # 按 hk 过滤: 一条都没有
    assert list(storage.load_history(filename="topics.jsonl", market="hk")) == []


def test_constituents_history_carries_market(data_dir):
    """港美成分股与 A 股同名 topic 靠 market 区分, 不能被混成一条序列。"""
    storage = HotspotStorage(data_dir)
    storage.append_constituents_history("半导体", [_stock("300474.SZ")], market="cn")
    storage.append_constituents_history("半导体", [_stock("NVDA.US")], market="us")

    cn_rows = list(storage.load_history(filename="constituents.jsonl", market="cn"))
    us_rows = list(storage.load_history(filename="constituents.jsonl", market="us"))
    assert [r["code"] for r in cn_rows] == ["300474.SZ"]
    assert [r["code"] for r in us_rows] == ["NVDA.US"]


def test_job_state_round_trip(data_dir):
    storage = HotspotStorage(data_dir)
    storage.write_job_state({
        "last_run": "2026-09-13T08:00:00+00:00",
        "last_status": "success",
        "rows": 5,
        "provider_used": "stub",
        "markets": {"cn": 5},
    })
    state = storage.read_job_state()
    assert state["last_status"] == "success"
    assert state["rows"] == 5


def test_load_history_skips_malformed_lines(tmp_path, data_dir):
    storage = HotspotStorage(data_dir)
    storage.append_history([_summary("valid")], market="cn")
    # 故意追加坏行 + 额外一条良行
    target = history_dir(data_dir) / "topics.jsonl"
    with target.open("a", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write("\n")
        f.write('{"topic": "extra", "stage": "初次异动"}\n')
    rows = list(storage.load_history(filename="topics.jsonl"))
    topics = [r["topic"] for r in rows]
    # "valid" 与 "extra" 都被读到;坏行/空白被忽略
    assert "valid" in topics
    assert "extra" in topics


def test_corrupt_topics_parquet_returns_empty(tmp_path, data_dir):
    storage = HotspotStorage(data_dir)
    topics_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    topics_path(data_dir).write_bytes(b"not a parquet")
    assert storage.read_topics() == []


@pytest.mark.parametrize("heat_score", [0.0, 50.0, 99.75])
def test_topic_heat_score_survives_exact_roundtrip(data_dir, heat_score):
    write_topics(data_dir, [HotspotSummary(topic="score", heat_score=heat_score)])
    assert read_topics(data_dir)[0].heat_score == heat_score


def test_legacy_heat_defaults_only_for_missing_or_invalid_values(data_dir):
    target = topics_path(data_dir)
    target.parent.mkdir(parents=True)
    pl.DataFrame({
        "topic": ["zero", "missing", "invalid"],
        "heat_score": [0.0, None, float("nan")],
    }).write_parquet(target)
    assert [item.heat_score for item in read_topics(data_dir)] == [0.0, 50.0, 50.0]


def test_topics_preserve_leaders_and_observation_with_snapshot(data_dir):
    stock = HotspotStock(
        code="000001.SZ", change_pct=0.0, turnover_rate=0.05,
        hot_stock_score=0.0, role="后排", source_confidence=0.0,
    )
    original = HotspotSummary(
        topic="leaders", leaders=[stock.code], leader_stocks=[stock],
        snapshot_at="2026-09-11T08:00:00+00:00", snapshot_market="cn",
    )
    write_topics(data_dir, [original])
    restored = read_topics(data_dir)[0]
    assert [asdict(item) for item in restored.leader_stocks] == [asdict(stock)]
    assert restored.snapshot_at == original.snapshot_at
    assert restored.snapshot_market == "cn"


def test_legacy_topics_mark_unpersisted_leaders_missing(data_dir):
    target = topics_path(data_dir)
    target.parent.mkdir(parents=True)
    pl.DataFrame({
        "topic": ["legacy"], "leaders": [["leader"]], "quality_status": ["available"],
    }).write_parquet(target)
    item = read_topics(data_dir)[0]
    assert item.leader_stocks == []
    assert "leader_stocks" in item.missing_fields
    assert item.quality_status == "partial"
    assert item.snapshot_at == ""


def test_write_topics_does_not_invent_observation_time(data_dir):
    write_topics(data_dir, [HotspotSummary(topic="unknown age")])
    item = read_topics(data_dir)[0]
    assert item.snapshot_at == ""
    assert item.snapshot_market == ""


def test_job_attempts_preserve_legacy_market_success(data_dir):
    target = job_state_path(data_dir)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({
        "last_run": "2026-09-11T08:00:00+00:00", "last_status": "success",
        "rows": 5, "markets": {"cn": 5},
    }), encoding="utf-8")
    storage = HotspotStorage(data_dir)
    storage.write_job_state({
        "last_run": "2026-09-13T08:00:00+00:00", "last_status": "skipped",
        "rows": 0, "markets": {"hk": 0},
    })
    state = storage.read_job_state()
    assert state["last_status"] == "skipped"
    assert state["last_success_at"] == {"cn": "2026-09-11T08:00:00+00:00"}
    assert state["markets"] == {"cn": 5, "hk": 0}
    storage.write_job_state({
        "last_run": "2026-09-13T09:00:00+00:00", "last_status": "error",
        "rows": 0, "markets": {"cn": 0},
    })
    assert storage.read_job_state()["last_success_at"] == state["last_success_at"]


def test_failed_topic_publication_keeps_previous_complete_snapshot(data_dir, monkeypatch):
    write_topics(data_dir, [HotspotSummary(topic="previous", heat_score=0.0)])
    previous = topics_path(data_dir).read_bytes()

    def interrupted_write(frame, file, *args, **kwargs):
        Path(file).write_bytes(b"interrupted parquet")
        raise OSError("injected disk failure")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", interrupted_write)
    with pytest.raises(OSError, match="disk failure"):
        write_topics(data_dir, [HotspotSummary(topic="replacement")])
    assert topics_path(data_dir).read_bytes() == previous
    assert read_topics(data_dir)[0].heat_score == 0.0
    assert list(topics_path(data_dir).parent.glob("*.tmp")) == []


def test_concurrent_topics_publish_rows_and_observation_as_one_snapshot(data_dir, monkeypatch):
    barrier = Barrier(2)
    original_write = pl.DataFrame.write_parquet

    def coordinated_write(frame, file, *args, **kwargs):
        result = original_write(frame, file, *args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(pl.DataFrame, "write_parquet", coordinated_write)

    def publish(number):
        write_topics(data_dir, [HotspotSummary(
            topic=f"writer-{number}-{row}", heat_score=float(number),
            snapshot_at=f"2026-09-{10 + number}T08:00:00+00:00", snapshot_market="cn",
        ) for row in range(3)])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(publish, [1, 2]))
    restored = read_topics(data_dir)
    assert len(restored) == 3
    writer = int(restored[0].heat_score)
    assert [item.topic for item in restored] == [f"writer-{writer}-{row}" for row in range(3)]
    assert all(item.snapshot_at == f"2026-09-{10 + writer}T08:00:00+00:00" for item in restored)
