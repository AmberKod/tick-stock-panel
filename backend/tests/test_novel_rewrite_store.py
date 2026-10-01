"""换元仿写 — 数据层 / 算法内核 / 仿写域 IO 单测。

覆盖 `ARCHITECTURE-rewrite.md` §3（T01 完成判据）与 PRD §4 P0-1..P0-10。

**自包含约定**：本仓 `tests/` 无 `conftest.py`，全部用例用 `tmp_path` +
`NovelStore(root=tmp_path)` 注入临时根目录，不碰真实 `data/`。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import novel_rewrite_store as rw
from app.services.novel_rewrite_store import (
    Blueprint,
    CheckItem,
    FunctionSlot,
    RelationEdge,
    RewriteReport,
    RewriteStore,
)
from app.services.novel_store import (
    ERR_PATH_ESCAPE,
    NovelStore,
    NovelStoreError,
    NovelValidationError,
)

# ─────────────────────────── 夹具 ───────────────────────────


def _store(tmp_path: Path) -> NovelStore:
    """临时根目录上的 `NovelStore`（每次读盘，不缓存）。"""
    return NovelStore(root=tmp_path)


def _book(tmp_path: Path) -> tuple[NovelStore, str]:
    """建一本书，返回 `(store, book_id)`。"""
    store = _store(tmp_path)
    book = store.create_book("换元仿写测试书")
    return store, book.id


def _edge(source: str, target: str, kind: str = "师徒", power: str = "高→低") -> RelationEdge:
    """构造一条关系边。"""
    return RelationEdge(source=source, target=target, kind=kind, power=power)


def _long_prose(chars: int = 1400) -> str:
    """无结构化标记的长散文（>1200 字，用于 R-len）。"""
    unit = "他把那封信又读了一遍，窗外的雨一直没有停。"
    body = (unit * (chars // len(unit) + 1))[:chars]
    return body


# ═══════════════════════════ 1. 输入侧预检四规则 ═══════════════════════════


def test_precheck_r_len_hits_on_long_plain_prose() -> None:
    """2000 字无标记散文 → 命中 `R-len`。"""
    text = _long_prose(2000)
    hits = rw.precheck_text("source_ref.note", text)
    assert hits, "长散文必须命中预检"
    assert "R-len" in [h["rule"] for h in hits]


def test_precheck_r_exempt_releases_list_note() -> None:
    """同等内容改成 `- ` 列表 → **放行**（R-exempt 优先，豁免前三条）。"""
    text = "\n".join(f"- {line}" for line in _long_prose(2000).split("。") if line)
    assert rw.precheck_text("source_ref.note", text) == []


def test_precheck_r_exempt_needs_three_markers() -> None:
    """结构化标记 <3 行 → 不豁免。"""
    text = "- 只有一行列表\n" + _long_prose(1400)
    assert rw.precheck_text("f", text) != []


def test_precheck_r_quote_hits_three_long_quotes() -> None:
    """3 处 ≥30 字引号片段 → 命中 `R-quote`。"""
    quote = "这一段是原作里的对白内容需要足够长才能触发规则" * 2
    text = f"“{quote}”\n“{quote}”\n“{quote}”"
    hits = rw.precheck_text("f", text)
    assert "R-quote" in [h["rule"] for h in hits]


def test_precheck_r_quote_ignores_short_quotes() -> None:
    """引号片段 <30 字 × 3 处 → 不命中 `R-quote`。"""
    text = "“很短”\n“也很短”\n“同样很短”"
    assert "R-quote" not in [h["rule"] for h in rw.precheck_text("f", text)]


def test_precheck_r_para_hits_five_continuous_sentences() -> None:
    """连续 ≥5 段以句号结尾且无列表标记 → 命中 `R-para`。"""
    text = "\n".join(f"这是第{i}句散文内容。" for i in range(1, 6))
    hits = rw.precheck_text("f", text)
    assert "R-para" in [h["rule"] for h in hits]


def test_precheck_empty_and_short_pass() -> None:
    """空字段 / 短字段 → 直接放行。"""
    assert rw.precheck_text("f", "") == []
    assert rw.precheck_text("f", "    ") == []
    assert rw.precheck_text("f", "主角 / 导师 / 对手") == []


def test_precheck_never_echoes_full_text() -> None:
    """hits 只含 ≤40 字 excerpt，**绝不回显被拒原文全文**（P0-3④）。"""
    text = _long_prose(2000)
    hits = rw.precheck_text("source_ref.note", text)
    assert hits
    for hit in hits:
        assert len(hit["excerpt"]) <= rw.EXCERPT_CHARS + 1
        assert text not in hit["excerpt"]
    raw = json.dumps(hits, ensure_ascii=False)
    assert text[:200] not in raw


def test_clean_precheck_response_has_no_full_text() -> None:
    """预检响应体不含原文全文，且带引导示例与诚实声明。"""
    text = _long_prose(2000)
    payload = rw.clean_precheck_response(rw.precheck_text("f", text))
    assert payload["ok"] is False
    assert payload["code"] == "rewrite_source_rejected"
    assert payload["sample"]
    assert payload["honesty_note"]
    raw = json.dumps(payload, ensure_ascii=False)
    assert text[:200] not in raw


def test_precheck_blueprint_scans_free_text_fields() -> None:
    """`precheck_blueprint` 覆盖 `source_ref.note` 与 `L4.new_causal_chain` 等字段。"""
    bp = Blueprint()
    bp.source_ref.note = _long_prose(1500)
    hits = rw.precheck_blueprint(bp)
    assert any(h["field"] == "source_ref.note" for h in hits)

    bp2 = Blueprint()
    bp2.rebuild.L4_events.new_causal_chain = [_long_prose(1500)]
    hits2 = rw.precheck_blueprint(bp2)
    assert any(h["field"].startswith("rebuild.L4_events") for h in hits2)


# ═══════════════════════════ 2. L3 拓扑指纹 ═══════════════════════════


def test_fingerprint_identical_triplet_is_fail() -> None:
    """度数 + 类型 + 权力三元组全等 → fail。"""
    edges = [_edge("甲", "乙"), _edge("乙", "丙")]
    src = rw.relation_fingerprint(edges)
    new = rw.relation_fingerprint([_edge("甲", "乙"), _edge("乙", "丙")])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "fail"


def test_fingerprint_same_degrees_different_kind_and_flow_is_warn() -> None:
    """度数全等但类型与权力分布都不同 → warn（需人工并排比对）。"""
    src = rw.relation_fingerprint([_edge("甲", "乙", "师徒", "高→低")])
    new = rw.relation_fingerprint([_edge("甲", "乙", "敌对", "对等")])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "warn"


def test_fingerprint_same_degrees_same_flow_is_fail() -> None:
    """度数全等 + 权力流向高度重合 → fail（L3-B）。"""
    src = rw.relation_fingerprint([_edge("甲", "乙", "师徒", "高→低")])
    new = rw.relation_fingerprint([_edge("甲", "乙", "同门", "高→低")])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "fail"


def test_fingerprint_empty_graph_is_unavailable() -> None:
    """任一侧空图 → unavailable（**绝不 pass**）。"""
    src = rw.relation_fingerprint([])
    new = rw.relation_fingerprint([_edge("甲", "乙")])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "unavailable"
    assert status != "pass"


def test_fingerprint_pads_isolated_nodes_with_zero() -> None:
    """角色数不同时短度数列补 0 对齐（防「多一个孤立人」就判 pass）。"""
    src = rw.relation_fingerprint([_edge("甲", "乙"), _edge("乙", "丙")])
    new = rw.relation_fingerprint([_edge("甲", "乙")])
    status, metrics = rw.compare_fingerprint(src, new)
    assert metrics["degrees_src"] == [2, 1, 1]
    assert metrics["degrees_new"] == [1, 1, 0]
    assert status in ("warn", "fail")


def test_fingerprint_all_unknown_power_is_unavailable() -> None:
    """`power` 全不可解析 → unavailable（权力流向无法量化，绝不 pass）。"""
    src = rw.relation_fingerprint([_edge("甲", "乙", "师徒", "")])
    new = rw.relation_fingerprint([_edge("甲", "乙", "同门", "unknown")])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "unavailable"


def test_fingerprint_huge_scale_gap_is_pass() -> None:
    """规模差异极大 → pass（图都重构了）。"""
    src = rw.relation_fingerprint([_edge("甲", "乙")])
    new = rw.relation_fingerprint([_edge(f"n{i}", f"n{i + 1}") for i in range(6)])
    status, _m = rw.compare_fingerprint(src, new)
    assert status == "pass"


def test_fingerprint_uses_multiset_not_set() -> None:
    """边类型用**多重集** Jaccard：师徒×2+敌对×1 vs 师徒×1+敌对×1+同门×1 → 0.5。"""
    a = {"师徒": 2, "敌对": 1}
    b = {"师徒": 1, "敌对": 1, "同门": 1}
    assert rw.multiset_jaccard(a, b) == pytest.approx(0.5)


# ═══════════════════════════ 3. L5 桥段序列 LCS ═══════════════════════════


def test_lcs_exactly_two_thirds_is_fail() -> None:
    """`ratio` **恰好 2/3 → fail**（闭区间，命门层从严）。"""
    status, metrics = rw.check_beat_sequence(["受辱", "隐忍", "反击"], ["受辱", "隐忍", "清算"])
    assert metrics["ratio"] == pytest.approx(2 / 3, abs=1e-3)
    assert status == "fail"


def test_lcs_exactly_one_half_is_warn() -> None:
    """`ratio` **恰好 1/2 → warn**。"""
    src = ["受辱", "隐忍", "反击", "清算"]
    new = ["受辱", "隐忍", "失去", "获得"]
    status, metrics = rw.check_beat_sequence(src, new)
    assert metrics["ratio"] == pytest.approx(0.5)
    assert status == "warn"


def test_lcs_synonym_normalization() -> None:
    """同义归一：`打脸` ≡ `反击`。"""
    assert rw.normalize_beat("打脸") == "反击"
    status, _m = rw.check_beat_sequence(["受辱", "打脸"], ["受辱", "反击"])
    assert status == "fail"


def test_lcs_truncates_over_64() -> None:
    """超 64 长度截断并标记 `truncated`。"""
    src = [f"受辱{i}" for i in range(70)]
    new = [f"受辱{i}" for i in range(70)]
    _status, metrics = rw.check_beat_sequence(src, new)
    assert metrics["truncated"] is True
    assert metrics["max_len"] == rw.MAX_SEQ_LEN


def test_lcs_low_ratio_high_jaccard_warns_reorder() -> None:
    """LCS 低但多重集 Jaccard ≥0.8 → warn「疑似打乱顺序照搬」。"""
    src = ["受辱", "隐忍", "反击", "清算"]
    new = ["清算", "反击", "隐忍", "受辱"]
    status, metrics = rw.check_beat_sequence(src, new)
    assert metrics["ratio"] < 0.5
    assert metrics["jaccard"] >= 0.8
    assert status == "warn"
    assert "打乱顺序" in str(metrics["reason"])


def test_lcs_empty_sequence_is_unavailable() -> None:
    """任一侧空序列 → unavailable。"""
    status, _m = rw.check_beat_sequence([], ["受辱"])
    assert status == "unavailable"
    assert status != "pass"


def test_lcs_trace_returns_common_beats() -> None:
    """`lcs_trace` 回溯出撞车的桥段本身（可核对性）。"""
    assert rw.lcs_trace(["受辱", "隐忍", "反击"], ["受辱", "反击"]) == ["受辱", "反击"]
    assert rw.lcs_len(["a", "b", "c"], ["a", "c"]) == 2


# ═══════════════════════════ 4. ⑦ 一对一人物映射 ═══════════════════════════


def test_one_to_one_different_counts_is_pass() -> None:
    """角色数不同 → pass（PRD 明说不构成一对一）。"""
    slots = [FunctionSlot(slot="主角"), FunctionSlot(slot="导师")]
    roles = [{"name": "甲", "slot": "主角"}]
    status, metrics = rw.check_one_to_one(slots, roles)
    assert status == "pass"
    assert metrics["src_count"] == 2 and metrics["new_count"] == 1


def test_one_to_one_same_counts_same_slots_is_fail() -> None:
    """角色数相同 + 功能位多重集完全相等 → fail（典型换人名不换构）。"""
    slots = [FunctionSlot(slot="主角"), FunctionSlot(slot="导师")]
    roles = [{"name": "甲", "slot": "主角"}, {"name": "乙", "slot": "师父"}]
    status, _m = rw.check_one_to_one(slots, roles)
    assert status == "fail"


def test_one_to_one_missing_table_is_unavailable() -> None:
    """缺原作功能位表或新作角色表 → unavailable（绝不 pass）。"""
    status, _m = rw.check_one_to_one([], [{"name": "甲", "slot": "主角"}])
    assert status == "unavailable"
    status2, _m2 = rw.check_one_to_one([FunctionSlot(slot="主角")], [])
    assert status2 == "unavailable"


def test_one_to_one_slot_synonyms() -> None:
    """功能位同义词归一：`师父` ≡ `导师`、`反派` ≡ `对手`。"""
    assert rw.normalize_slot("师父") == "导师"
    assert rw.normalize_slot("反派") == "对手"


# ═══════════════════════════ 5. ⑧ 同构反转底牌 ═══════════════════════════


def test_reversal_same_type_same_position_is_fail() -> None:
    """类型同 + 位置同（容差 0.1）→ fail。"""
    status, _m = rw.check_isomorphic_reversal(
        ["身份错位"], [0.6], [{"type": "身份错位", "position_ratio": 0.62}], 6
    )
    assert status == "fail"


def test_reversal_same_type_missing_position_is_warn() -> None:
    """类型同 + 位置缺失 → warn（需人工核对出现章节）。"""
    status, _m = rw.check_isomorphic_reversal(
        ["身份错位"], [], [{"type": "身份错位"}], 6
    )
    assert status == "warn"


def test_reversal_same_type_different_position_is_pass() -> None:
    """类型同 + 位置不同 → pass（换了位置 = 合法重建）。"""
    status, _m = rw.check_isomorphic_reversal(
        ["身份错位"], [0.2], [{"type": "身份错位", "position_ratio": 0.8}], 6
    )
    assert status == "pass"


def test_reversal_different_type_is_pass() -> None:
    """类型不同 → pass。"""
    status, _m = rw.check_isomorphic_reversal(
        ["身份错位"], [0.6], [{"type": "信任崩塌", "position_ratio": 0.62}], 6
    )
    assert status == "pass"


def test_reversal_missing_sample_is_unavailable() -> None:
    """缺样本 → unavailable。"""
    status, _m = rw.check_isomorphic_reversal([], [], [], 6)
    assert status == "unavailable"


def test_reversal_chapter_index_normalized() -> None:
    """章序号（>1）归一化为 `(idx-1)/max(1, total-1)`：原作第 2 章 ↔ 新作第 2 章 → fail。"""
    status, metrics = rw.check_isomorphic_reversal(
        ["身份错位"], [2.0], [{"type": "身份错位", "chapter_index": 2}], 6
    )
    assert status == "fail"
    assert metrics["hits"][0]["position"] == pytest.approx(0.2)


def test_reversal_synonyms() -> None:
    """反转类型同义词归一：`真假身份` ≡ `身份错位`。"""
    assert rw.normalize_reversal("真假身份") == "身份错位"


# ═══════════════════════════ 6. 状态语义 ═══════════════════════════


def test_check_item_rejects_fail_without_evidence() -> None:
    """`fail` 必须有 evidence 或 detail（禁止无据阻断）。"""
    with pytest.raises(ValidationError):
        CheckItem(key="k", layer="L1", mode="auto", status="fail")


def test_check_item_rejects_unavailable_without_human_tip() -> None:
    """`unavailable` 必须有 human_tip（必须告诉用户怎么人工核）。"""
    with pytest.raises(ValidationError):
        CheckItem(key="k", layer="L1", mode="auto", status="unavailable")


def test_check_status_literal_rejects_fifth_value() -> None:
    """`CheckStatus` 是四值 Literal，第五值直接拒绝。"""
    with pytest.raises(ValidationError):
        CheckItem(key="k", layer="L1", mode="auto", status="ok")  # type: ignore[arg-type]


def test_report_summary_is_recomputed_and_blocks_fail(tmp_path: Path) -> None:
    """有 fail → `summary.adoptable is False`（强算，外部传什么都不算）。"""
    _store_base, book_id = _book(tmp_path)
    bp = Blueprint(book_id=book_id)
    bp.rebuild.L1_symbols.banned = ["青云宗"]
    report = rw.build_report(
        rewrite_id="rw-x-1",
        blueprint=bp,
        draft_text="他走进青云宗的山门。",
    )
    assert report.summary.blocking >= 1
    assert report.summary.adoptable is False


def test_report_summary_can_become_adoptable(tmp_path: Path) -> None:
    """全勾选 + 反向三问 + ack → `adoptable True`。"""
    _store_base, book_id = _book(tmp_path)
    bp = Blueprint(book_id=book_id)
    report = rw.build_report(rewrite_id="rw-x-2", blueprint=bp)
    # 空蓝图下所有可自动项都是 unavailable，绝不冒充 pass
    assert all(c.status == "unavailable" for c in report.checks)
    assert report.summary.adoptable is False

    checks = [{"key": c.key, "human_checked": True} for c in report.checks]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))]
    report = rw.apply_checks(report, checks, reverse)
    assert report.summary.adoptable is False, "未 ack 前应不可采纳"

    report = rw.write_ack(report)
    assert report.summary.adoptable is True
    assert report.ack.acknowledged_at
    assert report.ack.disclaimer_version == rw.DISCLAIMER_VERSION
    rw.require_adoptable(report)


def test_fail_item_cannot_be_checked_to_pass(tmp_path: Path) -> None:
    """`fail` 项不接受勾选（硬阻断不可降级为 warn）。"""
    _store_base, book_id = _book(tmp_path)
    bp = Blueprint(book_id=book_id)
    bp.rebuild.L1_symbols.banned = ["青云宗"]
    report = rw.build_report(
        rewrite_id="rw-x-3", blueprint=bp, draft_text="青云宗的山门"
    )
    fail_key = next(c.key for c in report.checks if c.status == "fail")
    report = rw.apply_checks(report, [{"key": fail_key, "human_checked": True}], [])
    item = next(c for c in report.checks if c.key == fail_key)
    assert item.human_checked is False
    with pytest.raises(rw.RewriteAckRequiredError):
        rw.require_adoptable(report)


def test_apply_checks_is_idempotent(tmp_path: Path) -> None:
    """`apply_checks` 幂等，可多次调。"""
    _store_base, book_id = _book(tmp_path)
    report = rw.build_report(rewrite_id="rw-x-4", blueprint=Blueprint(book_id=book_id))
    payload = [{"key": c.key, "human_checked": True} for c in report.checks]
    once = rw.apply_checks(report, payload, [])
    twice = rw.apply_checks(once, payload, [])
    assert [c.human_checked for c in once.checks] == [c.human_checked for c in twice.checks]


def test_report_has_eight_checks_and_three_questions(tmp_path: Path) -> None:
    """报告 ≥8 项对照 + 3 条反向校验三问。"""
    _store_base, book_id = _book(tmp_path)
    report = rw.build_report(rewrite_id="rw-x-5", blueprint=Blueprint(book_id=book_id))
    assert len(report.checks) == 8
    assert len(report.reverse_three) == 3
    assert {c.key for c in report.checks} == set(rw.CHECK_KEYS)
    for item in report.checks:
        assert item.status in rw.CHECK_STATUSES


def test_report_never_promises(tmp_path: Path) -> None:
    """报告全文不含「保证」类承诺词（PRD §2.6 工程侧禁止 12）。"""
    _store_base, book_id = _book(tmp_path)
    report = rw.build_report(rewrite_id="rw-x-6", blueprint=Blueprint(book_id=book_id))
    raw = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    for banned in ("保证过原创检测", "保证不侵权", "已通过查重", "保证原创"):
        assert banned not in raw
    assert "不是查重报告" in rw.DISCLAIMER_TEXT


def test_unavailable_is_never_pass(tmp_path: Path) -> None:
    """无样本项 `status == unavailable` **且 `!= pass`**。"""
    _store_base, book_id = _book(tmp_path)
    report = rw.build_report(rewrite_id="rw-x-7", blueprint=Blueprint(book_id=book_id))
    for item in report.checks:
        if item.status == "unavailable":
            assert item.human_tip, "unavailable 必须给出人工核对指引"
            assert item.status != "pass"
    assert report.summary.passed == 0


# ═══════════════════════════ 7. 闸门 ═══════════════════════════


def test_gate_ready_requires_l3_and_l5() -> None:
    """L3 或 L5 缺表 → `ready=False` + `missing_layers`。"""
    bp = Blueprint()
    ready, missing = rw.is_gate_ready(bp)
    assert ready is False
    assert set(missing) == {"L3", "L5"}

    bp.rebuild.L3_relations.source_graph = [_edge("甲", "乙")]
    bp.rebuild.L3_relations.new_graph = [_edge("甲", "丙")]
    _ready, missing = rw.is_gate_ready(bp)
    assert missing == ["L5"]

    bp.rebuild.L5_beats.source_seq = ["受辱"]
    bp.rebuild.L5_beats.new_seq = ["失去"]
    assert rw.is_gate_ready(bp)[0] is True


def test_require_gate_raises_blocked() -> None:
    """未 ready 且未 skip → `RewriteGateBlockedError`（422）。"""
    with pytest.raises(rw.RewriteGateBlockedError) as info:
        rw.require_gate(Blueprint())
    assert info.value.code == "rewrite_gate_blocked"
    assert info.value.missing == ["L3", "L5"]
    rw.require_gate(Blueprint(), skip=True)  # 显式跳过 → 放行


# ═══════════════════════════ 8. 仿写域 IO 与路径安全 ═══════════════════════════


def test_rewrite_path_rejects_escape(tmp_path: Path) -> None:
    """`../` / 绝对路径 / `book.json` → 抛 `path_escape`(422)。"""
    store, book_id = _book(tmp_path)
    for rel in ("../book.json", "book.json", "/etc/passwd", "C:/Windows/win.ini", "正文/ch-001.md"):
        with pytest.raises(NovelValidationError) as info:
            store.rewrite_path(book_id, rel)
        assert info.value.code == ERR_PATH_ESCAPE


def test_rewrite_path_allows_inside_rewrite(tmp_path: Path) -> None:
    """`rewrite/` 内的相对路径放行（rel 以书籍目录为根）。"""
    store, book_id = _book(tmp_path)
    path = store.rewrite_path(book_id, "rewrite/blueprint.json")
    assert path.parent == store.rewrite_dir(book_id)
    path2 = store.rewrite_path(book_id, "rewrite/drafts/rw-1.md")
    assert str(path2).replace("\\", "/").endswith("rewrite/drafts/rw-1.md")


def test_blueprint_roundtrip_and_no_persist_on_missing(tmp_path: Path) -> None:
    """蓝图落盘可回读；不存在时 `load_blueprint` 返回空蓝图**不落盘**。"""
    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)
    bp = Blueprint(book_id=book_id, title="结构蓝图")
    bp.abstract.function_slots = [FunctionSlot(slot="主角")]

    assert not store.blueprint_path(book_id).exists()
    empty = store.load_blueprint(book_id)
    assert empty.title == ""
    assert not store.blueprint_path(book_id).exists(), "读不存在的蓝图不得落盘"

    saved = store.save_blueprint(book_id, bp)
    assert saved.id.startswith("bp-")
    assert store.blueprint_path(book_id).exists()
    reloaded = store.load_blueprint(book_id)
    assert reloaded.title == "结构蓝图"
    assert reloaded.abstract.function_slots[0].slot == "主角"


def test_save_blueprint_rejects_source_text_without_writing(tmp_path: Path) -> None:
    """预检命中 → 422，**一个字节都不落盘**（P0-3③）。"""
    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)
    bp = Blueprint(book_id=book_id)
    bp.source_ref.note = _long_prose(2000)
    with pytest.raises(rw.RewriteSourceRejectedError) as info:
        store.save_blueprint(book_id, bp)
    assert info.value.code == "rewrite_source_rejected"
    assert info.value.hits
    assert not store.blueprint_path(book_id).exists()


def test_write_and_read_rewrite_draft(tmp_path: Path) -> None:
    """草稿 md 用 `newline=""`（字节级保留 \n），outline 用 JSON。"""
    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)
    rel = store.write_rewrite_draft(book_id, "rw-a-1", "chapter", "第一行\n第二行\n")
    assert rel == "drafts/rw-a-1.md"
    assert store.read_rewrite_draft(book_id, rel) == "第一行\n第二行\n"
    raw = store.rewrite_dir(book_id) / "drafts" / "rw-a-1.md"
    assert b"\r\n" not in raw.read_bytes()

    rel2 = store.write_rewrite_draft(book_id, "rw-a-2", "outline", '{"nodes": []}')
    assert rel2 == "drafts/rw-a-2.outline.json"


def test_report_roundtrip(tmp_path: Path) -> None:
    """报告落盘 → 回读（`summary` 回读时仍强算）。"""
    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)
    report = rw.build_report(rewrite_id="rw-b-1", blueprint=Blueprint(book_id=book_id))
    store.save_report(book_id, report)
    loaded = store.load_report(book_id, "rw-b-1")
    assert loaded.rewrite_id == "rw-b-1"
    assert loaded.summary.adoptable is False
    assert store.list_reports(book_id)[0]["rewrite_id"] == "rw-b-1"


def test_zero_write_to_authoritative_data(tmp_path: Path) -> None:
    """★P0-10★ 仿写全链路（蓝图/草稿/报告）执行后权威数据**零变更**。"""
    base, book_id = _book(tmp_path)
    store = RewriteStore(base)
    before = rw.capture_authoritative_snapshot(base, book_id)

    bp = Blueprint(book_id=book_id, title="蓝图")
    bp.rebuild.L3_relations.source_graph = [_edge("甲", "乙")]
    bp.rebuild.L3_relations.new_graph = [_edge("丙", "丁", "敌对", "对等")]
    bp.rebuild.L5_beats.source_seq = ["受辱", "隐忍", "反击"]
    bp.rebuild.L5_beats.new_seq = ["失去", "抉择", "顿悟"]
    store.save_blueprint(book_id, bp)

    store.write_rewrite_draft(book_id, "rw-c-1", "chapter", "新作正文草稿。\n")
    report = rw.build_report(
        rewrite_id="rw-c-1", blueprint=bp, draft_text="新作正文草稿。\n"
    )
    store.save_report(book_id, report)

    after = rw.capture_authoritative_snapshot(base, book_id)
    assert before == after, "仿写生成阶段对 book.json / state.json / 正文/ 必须零写入"


def test_atomic_write_leaves_no_tmp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """原子写失败时清理 `.tmp`，无残留污染主文件。"""
    import app.services.novel_store as ns

    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("模拟 os.replace 前异常")

    monkeypatch.setattr(ns, "_replace_with_retry", boom)
    with pytest.raises(NovelStoreError):
        store.write_rewrite_draft(book_id, "rw-d-1", "chapter", "内容")
    drafts = store.rewrite_dir(book_id) / "drafts"
    assert drafts.exists()
    assert list(drafts.glob(".*tmp")) == []


def test_rewrite_job_checkpoint_roundtrip(tmp_path: Path) -> None:
    """`rw-` job checkpoint 落 `checkpoints/` 且可回读。"""
    store = RewriteStore(_store(tmp_path))
    _base, book_id = _book(tmp_path)
    job = rw.RewriteJob(
        job_id=rw.make_rewrite_job_id(book_id),
        book_id=book_id,
        kind="plan",
        steps=[{"name": n} for n in ("precheck", "generate", "evaluate", "finalize")],
    )
    store.save_rewrite_job(job)
    assert store.rewrite_job_path(job.job_id).parent.name == "checkpoints"
    loaded = store.load_rewrite_job(job.job_id)
    assert loaded.job_id == job.job_id
    assert [s.name for s in loaded.steps] == ["precheck", "generate", "evaluate", "finalize"]


def test_rewrite_job_id_roundtrip() -> None:
    """`rw-` job_id 可无歧义反解出 book_id 与时间戳。"""
    job_id = rw.make_rewrite_job_id("book-20261005")
    book_id, ts = rw.parse_rewrite_job_id(job_id)
    assert book_id == "book-20261005"
    assert len(ts) == 14
    with pytest.raises(NovelValidationError):
        rw.parse_rewrite_job_id("job-book-1-20261005010101-abcd")
    with pytest.raises(NovelValidationError):
        rw.parse_rewrite_job_id("rw-BOOK-1-20261005010101-abcd")


def test_rewrite_id_matches_id_rules(tmp_path: Path) -> None:
    """`rewrite_id` 必须能被 `validate_id` 校验（天然防穿越）。"""
    _base, book_id = _book(tmp_path)
    rewrite_id = rw.make_rewrite_id(book_id)
    assert rewrite_id.startswith("rw-")
    store = RewriteStore(_store(tmp_path))
    store.report_path(book_id, rewrite_id)  # 不抛即通过


# ═══════════════════════════ 9. schema 快照（防前后端静默错位）═══════════════════════════


def test_schema_field_snapshot() -> None:
    """字段名快照：任一侧改字段名必须同步改另一侧（§8.4）。"""
    assert set(Blueprint.model_fields) == {
        "version", "id", "book_id", "title", "created_at", "updated_at",
        "source_ref", "abstract", "rebuild", "gate",
    }
    assert set(RewriteReport.model_fields) == {
        "rewrite_id", "blueprint_id", "book_id", "chapter_id", "kind", "draft_file",
        "generated_at", "disclaimer", "checks", "reverse_three", "summary", "ack",
        # ★P2-4 采纳留痕：区分首采 / 重采
        "adopted_at", "adopted_target",
    }
    assert set(CheckItem.model_fields) == {
        "key", "layer", "mode", "status", "detail", "evidence", "human_tip",
        "human_checked", "checked_at", "metrics",
    }
