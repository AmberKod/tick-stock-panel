"""QA 独立验证 · D 组：五层质检算法边界（L3 指纹 / L5 LCS / ⑦ 功能位 / ⑧ 反转）。

出手角度：**卡在阈值那一位**（恰好 2/3、恰好 1/2、恰好 0.1 容差），
外加「多重集 vs 集合」「度数列补 0」「空图 / 单节点 / 除零」这类
容易被写错成「看起来差不多就 pass」的地方。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import pytest

from app.services.novel_rewrite_store import (
    MAX_SEQ_LEN,
    REVERSAL_POS_TOL,
    FunctionSlot,
    RelationEdge,
    check_beat_sequence,
    check_isomorphic_reversal,
    check_one_to_one,
    compare_fingerprint,
    multiset_jaccard,
    normalize_beat,
    normalize_reversal,
    normalize_slot,
    relation_fingerprint,
)


def _edge(source: str, target: str, kind: str = "师徒", power: str = "高→低") -> RelationEdge:
    """构造一条关系边。"""
    return RelationEdge(source=source, target=target, kind=kind, power=power)


def _edges_3(a: str, b: str, c: str) -> list[RelationEdge]:
    """链式两条边 a→b→c（度数 [2,1,1]）。"""
    return [_edge(a, b), _edge(b, c)]


# ═══════════════════════ L3 · 关系拓扑指纹 ═══════════════════════


def test_l3_empty_graph_is_never_pass() -> None:
    """任一侧空图 → **unavailable**（绝不能因为「没样本」就 pass）。"""
    empty = relation_fingerprint([])
    full = relation_fingerprint([_edge("甲", "乙")])
    for status, _metrics in (
        compare_fingerprint(empty, full),
        compare_fingerprint(full, empty),
        compare_fingerprint(empty, empty),
    ):
        assert status == "unavailable", status


def test_l3_scale_gap_is_pass() -> None:
    """规模差异极大 → pass（图都重构了）。"""
    small = relation_fingerprint([_edge("甲", "乙")])
    big = relation_fingerprint([_edge(f"a{index}", f"b{index}") for index in range(6)])
    status, metrics = compare_fingerprint(small, big)
    assert status == "pass", metrics
    assert metrics["reason"] == "关系图规模显著不同"


def test_l3_all_power_unknown_is_unavailable() -> None:
    """`power` 全不可解析 → unavailable（权力流向无从量化）。"""
    src = relation_fingerprint([_edge("甲", "乙", power="不明")])
    new = relation_fingerprint([_edge("丙", "丁", power="不明")])
    status, metrics = compare_fingerprint(src, new)
    assert status == "unavailable", metrics
    assert "无法解析" in metrics["reason"]


def test_l3_all_unknown_on_one_side_degrades_only_that_side() -> None:
    """只有**一侧**的 power 全不可解析才降级；另一侧部分 unknown 时仍要正常比对。"""
    src = relation_fingerprint([_edge("甲", "乙", power="高→低"), _edge("乙", "丙", power="对等")])
    new = relation_fingerprint([_edge("a", "b", power="高→低"), _edge("b", "c", power="不明")])
    assert new.unknown_power == 1 < new.edge_count
    status, _metrics = compare_fingerprint(src, new)
    assert status in ("fail", "warn", "pass"), status

    # 对照：只有 1 条边且不可解析 → 该侧「全 unknown」 → unavailable
    single = relation_fingerprint([_edge("a", "b", power="不明")])
    assert compare_fingerprint(src, single)[0] == "unavailable"


def test_l3_identical_fingerprint_fails() -> None:
    """三元组（度数列 / 类型 / 权力）全等 → fail（换人名不换拓扑）。"""
    src = relation_fingerprint([_edge("甲", "乙"), _edge("乙", "丙", kind="同门", power="对等")])
    new = relation_fingerprint([_edge("a", "b"), _edge("b", "c", kind="同门", power="对等")])
    status, metrics = compare_fingerprint(src, new)
    assert status == "fail", metrics
    assert metrics["reason"] == "关系拓扑指纹与原作完全相同"


def test_l3_degrees_padded_with_zeros_and_not_confused() -> None:
    """度数列补 0 对齐：4 节点图 vs 2 节点图 → 补 [1,1,0,0]，**不会**被当成相同。"""
    src = relation_fingerprint([_edge("甲", "乙"), _edge("丙", "丁")])
    new = relation_fingerprint([_edge("a", "b")])
    status, metrics = compare_fingerprint(src, new)
    assert len(metrics["degrees_src"]) == len(metrics["degrees_new"]) == 4
    assert metrics["degrees_new"][-2:] == [0, 0]
    assert status != "fail", "补 0 后竟然判成了完全相同"


def test_l3_multiset_vs_set_semantics() -> None:
    """★多重集 vs 集合★：A-B 有 2 条同类型边 ≠ 1 条 —— 不能被判成「类型集合相同」。"""
    assert multiset_jaccard({"师徒": 2}, {"师徒": 1}) == pytest.approx(0.5)
    assert multiset_jaccard({"师徒": 1}, {"师徒": 1}) == pytest.approx(1.0)
    assert multiset_jaccard({}, {}) == 0.0

    two_edges = relation_fingerprint([_edge("甲", "乙"), _edge("甲", "乙")])
    one_edge = relation_fingerprint([_edge("a", "b")])
    status, metrics = compare_fingerprint(two_edges, one_edge)
    assert status != "fail", metrics  # 度数 [2,2] vs [1,1] 不同 → 不该判完全相同
    assert metrics["degrees_src"] == [2, 2]
    assert metrics["degrees_new"] == [1, 1]  # 同为 2 节点 → 不需要补 0
    assert metrics["kind_jaccard"] == pytest.approx(0.5)  # 多重集 1/2，不是集合的 1.0


def test_l3_kind_case_and_space_normalized() -> None:
    """类型归一：大小写 / 空白差异不影响判定（不能靠改空格绕过）。"""
    src = relation_fingerprint([_edge("甲", "乙", kind="师徒")])
    new = relation_fingerprint([_edge("a", "b", kind=" 师 徒 ")])
    status, _metrics = compare_fingerprint(src, new)
    assert status == "fail", "类型大小写/空白差异竟然绕过了指纹比对"


def test_l3_single_node_self_loop() -> None:
    """单节点自环图：两边同为单节点自环 → fail（不是 pass）。"""
    src = relation_fingerprint([_edge("甲", "甲")])
    new = relation_fingerprint([_edge("乙", "乙")])
    status, _metrics = compare_fingerprint(src, new)
    assert status == "fail", status
    assert src.degrees == [2] and src.node_count == 1


def test_l3_power_sign_variants() -> None:
    """权力流向解析：高→低 / 低→高 / 对等 / 无法解析。"""
    fp = relation_fingerprint(
        [
            _edge("甲", "乙", power="高→低"),
            _edge("丙", "丁", power="低→高"),
            _edge("戊", "己", power="对等"),
            _edge("庚", "辛", power=""),
        ]
    )
    assert fp.flow == {"-1": 1, "+1": 1, "0": 1, "unknown": 1}
    assert fp.unknown_power == 1


def test_l3_warn_when_same_degrees_but_different_kinds() -> None:
    """度数全等、类型与权力分布都不同 → warn（不是 fail、更不是 pass）。"""
    src = relation_fingerprint(
        [
            _edge("甲", "乙", kind="师徒", power="高→低"),
            _edge("丙", "丁", kind="师徒", power="高→低"),
        ]
    )
    new = relation_fingerprint(
        [
            _edge("a", "b", kind="血缘", power="对等"),
            _edge("c", "d", kind="敌对", power="低→高"),
        ]
    )
    status, metrics = compare_fingerprint(src, new)
    assert status == "warn", metrics
    assert metrics["kind_jaccard"] == 0.0
    assert metrics["flow_jaccard"] == 0.0


def test_l3_fail_when_degrees_and_power_flow_identical() -> None:
    """度数全等 + **权力流向**完全重合（哪怕类型换了）→ fail（L3-B 的 OR 分支）。"""
    src = relation_fingerprint(
        [
            _edge("甲", "乙", kind="师徒", power="高→低"),
            _edge("丙", "丁", kind="师徒", power="高→低"),
        ]
    )
    new = relation_fingerprint(
        [
            _edge("a", "b", kind="血缘", power="高→低"),
            _edge("c", "d", kind="敌对", power="高→低"),
        ]
    )
    status, metrics = compare_fingerprint(src, new)
    assert status == "fail", metrics
    assert metrics["flow_jaccard"] == 1.0 and metrics["kind_jaccard"] == 0.0


def test_l3_fail_when_same_degrees_and_high_kind_overlap() -> None:
    """度数全等 + 类型 Jaccard ≥0.8 → fail。"""
    src = relation_fingerprint([*_edges_3("甲", "乙", "丙"), _edge("戊", "己", kind="同门")])
    new = relation_fingerprint([*_edges_3("a", "b", "c"), _edge("x", "y", kind="同门")])
    status, metrics = compare_fingerprint(src, new)
    assert status == "fail", metrics
    assert metrics["kind_jaccard"] >= 0.8


# ═══════════════════════ L5 · 桥段序列 LCS ═══════════════════════


def test_l5_both_sides_empty_is_unavailable() -> None:
    """任一侧空 → unavailable（绝不是 pass）。"""
    assert check_beat_sequence([], [])[0] == "unavailable"
    assert check_beat_sequence(["受辱"], [])[0] == "unavailable"
    assert check_beat_sequence([], ["受辱"])[0] == "unavailable"
    assert check_beat_sequence(["", "  "], ["受辱"])[0] == "unavailable"


def test_l5_ratio_exactly_two_thirds_fails() -> None:
    """★浮点边界★ `lcs=4 / max=6` 与 `lcs=2 / max=3` 都恰好 = 2/3 → **fail**。"""
    status_a, metrics_a = check_beat_sequence(
        ["A", "B", "C", "D", "E", "F"], ["A", "B", "C", "D", "x", "y"]
    )
    assert metrics_a["lcs_len"] == 4 and metrics_a["max_len"] == 6
    assert status_a == "fail", f"恰好 2/3 没被判 fail: {metrics_a}"

    status_b, metrics_b = check_beat_sequence(["A", "B", "C"], ["A", "B", "x"])
    assert metrics_b["lcs_len"] == 2 and metrics_b["max_len"] == 3
    assert status_b == "fail", f"恰好 2/3 没被判 fail: {metrics_b}"


def test_l5_ratio_exactly_one_half_warns() -> None:
    """★浮点边界★ `lcs=1 / max=2` 与 `lcs=3 / max=6` 恰好 = 1/2 → **warn**（不是 fail）。"""
    status_a, metrics_a = check_beat_sequence(["A", "B"], ["A", "x"])
    assert metrics_a["ratio"] == pytest.approx(0.5)
    assert status_a == "warn", metrics_a

    status_b, metrics_b = check_beat_sequence(
        ["A", "B", "C", "D", "E", "F"], ["A", "B", "C", "x", "y", "z"]
    )
    assert metrics_b["ratio"] == pytest.approx(0.5)
    assert status_b == "warn", metrics_b


def test_l5_just_below_two_thirds_warns() -> None:
    """差一点点到 2/3（3/5=0.6）→ warn 而非 fail。"""
    status, metrics = check_beat_sequence(["A", "B", "C", "D", "E"], ["A", "B", "C", "x", "y"])
    assert metrics["ratio"] == pytest.approx(0.6)
    assert status == "warn", metrics


def test_l5_shuffled_same_set_warns_by_jaccard() -> None:
    """★打乱顺序照搬★ LCS 低但多重集 Jaccard 高 → warn（不能只靠 LCS 放过）。"""
    status, metrics = check_beat_sequence(["受辱", "隐忍", "反击"], ["反击", "隐忍", "受辱"])
    assert status == "warn", metrics
    assert metrics["jaccard"] == pytest.approx(1.0)
    assert "打乱顺序" in metrics["reason"]


def test_l5_truncation_flag_and_ratio_bounded() -> None:
    """超 `MAX_SEQ_LEN` 会截断：`truncated=True` 且 ratio 仍在 [0,1]（不被扭曲到 >1）。"""
    long_a = [f"a{index}" for index in range(100)]
    long_b = [f"a{index}" for index in range(80)] + [f"b{index}" for index in range(20)]
    status, metrics = check_beat_sequence(long_a, long_b)
    assert metrics["truncated"] is True
    assert 0.0 <= metrics["ratio"] <= 1.0
    assert status in ("fail", "warn", "pass")
    # 截断后参与比对的长度不超过上限
    assert metrics["max_len"] <= MAX_SEQ_LEN


def test_l5_truncation_does_not_hide_a_full_copy() -> None:
    """截断不能成为「长序列照搬」的逃生门：100 字完全相同仍要 fail。"""
    same = [f"x{index}" for index in range(100)]
    status, metrics = check_beat_sequence(same, list(same))
    assert status == "fail", metrics
    assert metrics["truncated"] is True


def test_l5_synonym_normalization() -> None:
    """同义词归一：打脸→反击、压抑→受辱、逆袭→爆发达。"""
    assert normalize_beat("打脸") == "反击"
    assert normalize_beat("压抑") == "受辱"
    assert normalize_beat("逆袭") == "爆发达"
    assert normalize_beat(" 打脸 ") == "反击"
    assert normalize_beat("受辱（当众）") == "受辱"  # 括号注释被剥


def test_l5_unrecognized_tags_are_reported() -> None:
    """未识别词不做模糊匹配，但要在 `unrecognized` 里暴露（可核对性）。"""
    _status, metrics = check_beat_sequence(["莫名桥段", "受辱"], ["另一个怪词", "隐忍"])
    assert "莫名桥段" in metrics["unrecognized"]
    assert "另一个怪词" in metrics["unrecognized"]
    assert "受辱" not in metrics["unrecognized"]


def test_l5_identical_sequence_fails_and_traces_common() -> None:
    """完全相同 → fail，且 `common` 给出撞车桥段（UI 可核对）。"""
    status, metrics = check_beat_sequence(["受辱", "隐忍", "反击"], ["受辱", "隐忍", "反击"])
    assert status == "fail"
    assert metrics["common"] == ["受辱", "隐忍", "反击"]


def test_l5_disjoint_sequences_pass() -> None:
    """完全不同 → pass。"""
    status, metrics = check_beat_sequence(["受辱", "隐忍", "反击"], ["失去", "抉择", "顿悟"])
    assert status == "pass", metrics
    assert metrics["lcs_len"] == 0


# ═══════════════════════ ⑦ 一对一人物映射 ═══════════════════════


def test_oto_missing_table_is_unavailable() -> None:
    """缺任一张表 → unavailable（绝不能 pass）。"""
    assert check_one_to_one([], [{"slot": "主角"}])[0] == "unavailable"
    assert check_one_to_one([FunctionSlot(slot="主角")], [])[0] == "unavailable"
    assert check_one_to_one([], [])[0] == "unavailable"
    # 表里有但 slot 为空也算「没内容」吗？—— 记录事实：空 slot 仍计入角色数
    status, _metrics = check_one_to_one([FunctionSlot(slot="")], [{"slot": ""}])
    assert status in ("fail", "warn", "pass")


def test_oto_different_character_count_passes() -> None:
    """角色数不同 → pass（PRD：不构成一对一）。"""
    status, metrics = check_one_to_one(
        [FunctionSlot(slot="主角"), FunctionSlot(slot="导师")], [{"slot": "主角"}]
    )
    assert status == "pass", metrics
    assert metrics["src_count"] == 2 and metrics["new_count"] == 1


def test_oto_same_slots_different_order_still_fails() -> None:
    """★多重集语义★ 功能位**顺序不同**但集合相同 → 仍 fail（不能被顺序骗过）。"""
    status, metrics = check_one_to_one(
        [FunctionSlot(slot="主角"), FunctionSlot(slot="导师")],
        [{"slot": "导师"}, {"slot": "主角"}],
    )
    assert status == "fail", metrics
    assert metrics["reason"].startswith("角色数相同且功能位一一对应")


def test_oto_slot_alias_normalization() -> None:
    """★别名★ 师父 / 师傅 / 师长 → 导师；反派 → 对手；伙伴 → 盟友；内鬼 → 背叛者。"""
    assert normalize_slot("师父") == "导师"
    assert normalize_slot("师傅") == "导师"
    assert normalize_slot("师长") == "导师"
    assert normalize_slot("反派") == "对手"
    assert normalize_slot("伙伴") == "盟友"
    assert normalize_slot("内鬼") == "背叛者"
    status, _metrics = check_one_to_one([FunctionSlot(slot="导师")], [{"slot": "师父"}])
    assert status == "fail", "别名归一没生效 → 换词就能绕过 ⑦"


def test_oto_jaccard_boundaries() -> None:
    """Jaccard ≥0.8 → fail；≥0.6 → warn；<0.6 → pass。"""
    # 10 个功能位只换掉 1 个 → min 9 / max 11 = 0.818 ≥ 0.8 → fail
    src = [FunctionSlot(slot=f"位{index}") for index in range(10)]
    new = [{"slot": f"位{index}"} for index in range(9)] + [{"slot": "全新位"}]
    status, metrics = check_one_to_one(src, new)
    assert metrics["jaccard"] == pytest.approx(9 / 11, abs=1e-3)
    assert status == "fail", metrics

    # 5 个功能位换掉 1 个 → 4/6 = 0.667 → warn
    src5 = [FunctionSlot(slot=f"位{index}") for index in range(5)]
    new5 = [{"slot": f"位{index}"} for index in range(4)] + [{"slot": "全新位"}]
    status5, metrics5 = check_one_to_one(src5, new5)
    assert metrics5["jaccard"] == pytest.approx(4 / 6, abs=1e-3)
    assert status5 == "warn", metrics5

    # 5 个功能位换掉 3 个 → 2/8 = 0.25 → pass
    src5b = [FunctionSlot(slot=f"位{index}") for index in range(5)]
    new5b = [{"slot": f"位{index}"} for index in range(2)] + [
        {"slot": f"新{index}"} for index in range(3)
    ]
    status5b, metrics5b = check_one_to_one(src5b, new5b)
    assert metrics5b["jaccard"] == pytest.approx(2 / 8, abs=1e-3)
    assert status5b == "pass", metrics5b


def test_oto_ignores_non_dict_rows() -> None:
    """角色表里混入非 dict 行（AI 乱吐）→ 过滤掉，不炸。"""
    status, _metrics = check_one_to_one(
        [FunctionSlot(slot="主角")], [{"slot": "主角"}, "垃圾行", 123]
    )
    assert status == "fail"  # 过滤后仍是一对一


# ═══════════════════════ ⑧ 同构反转底牌 ═══════════════════════


def test_rev_missing_samples_is_unavailable() -> None:
    """缺原作反转类型或新作登记表 → unavailable。"""
    assert check_isomorphic_reversal([], [0.5], [{"type": "身份错位"}])[0] == "unavailable"
    assert check_isomorphic_reversal(["身份错位"], [0.5], [])[0] == "unavailable"


def test_rev_type_less_row_is_not_unavailable_at_algorithm_level() -> None:
    """【已知限制 · P2】算法层：登记表里全是 `type` 为空的行 → 判 pass（不是 unavailable）。

    保护在解析层：`parse_reversal_table()` 会把 `type` 为空的行**丢掉**，
    于是整表为空 → ⑧ unavailable。这里记录算法层的口径，并确认解析层兜住了。
    """
    from app.services.novel_rewrite_ai import parse_reversal_table

    assert check_isomorphic_reversal(["身份错位"], [0.5], [{"type": ""}])[0] == "pass"
    assert parse_reversal_table('```rw-reversals\n[{"type": ""}]\n```') == []
    # 解析层兜住后，走 build_report 就是 unavailable
    assert check_isomorphic_reversal(["身份错位"], [0.5], parse_reversal_table('```rw-reversals\n[{"type": ""}]\n```'))[0] == "unavailable"


def test_rev_different_type_passes() -> None:
    """反转类型不同 → pass（换了机制）。"""
    status, metrics = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "信任崩塌", "position_ratio": 0.5}]
    )
    assert status == "pass", metrics
    assert "未出现与原作同类型的反转" in metrics["reason"]


def test_rev_same_type_same_position_fails() -> None:
    """类型同 + 位置同（差值 0）→ fail。"""
    status, _metrics = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "身份错位", "position_ratio": 0.5}]
    )
    assert status == "fail"


def test_rev_tolerance_boundary_exactly_0_1() -> None:
    """★容差边界★ 位置差**恰好 0.1** → 判为「同位置」→ fail（闭区间）。"""
    assert REVERSAL_POS_TOL == 0.1
    status, metrics = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "身份错位", "position_ratio": 0.6}]
    )
    assert status == "fail", f"恰好 0.1 的容差边界没被判同位置: {metrics}"

    # 差 0.11 → 不同位置 → pass
    status2, metrics2 = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "身份错位", "position_ratio": 0.61}]
    )
    assert status2 == "pass", metrics2


def test_rev_position_missing_is_warn_not_pass() -> None:
    """类型同但位置信息缺失 → **warn**（不是 pass、更不是 fail）。"""
    status, metrics = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "身份错位"}]
    )
    assert status == "warn", metrics
    assert metrics["hits"][0]["same_position"] is None

    # 原作侧没给位置 → 同样 warn
    status2, _m2 = check_isomorphic_reversal(
        ["身份错位"], [], [{"type": "身份错位", "position_ratio": 0.5}]
    )
    assert status2 == "warn"


def test_rev_chapter_index_normalization() -> None:
    """`chapter_index` 会被归一化成进度：第 4 章 / 共 6 章 → (4-1)/5 = 0.6。"""
    status, metrics = check_isomorphic_reversal(
        ["身份错位"], [0.6], [{"type": "身份错位", "chapter_index": 4}], total_chapters=6
    )
    assert status == "fail", metrics
    assert metrics["hits"][0]["position"] == pytest.approx(0.6)


def test_rev_total_one_does_not_divide_by_zero() -> None:
    """★除零★ `total_chapters=1` / `=0` 时不得抛 ZeroDivisionError。"""
    for total in (0, 1):
        status, metrics = check_isomorphic_reversal(
            ["身份错位"], [0.5], [{"type": "身份错位", "chapter_index": 5}], total_chapters=total
        )
        assert status in ("pass", "warn", "fail"), metrics
    # ratio ≤1 的取值直接当进度用
    _s, m = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "身份错位", "position_ratio": 0.5}], total_chapters=0
    )
    assert m["hits"][0]["position"] == pytest.approx(0.5)


def test_rev_type_alias_normalization() -> None:
    """反转类型别名：真假身份→身份错位、反水→信任崩塌、假死/复活→生死反转。"""
    assert normalize_reversal("真假身份") == "身份错位"
    assert normalize_reversal("反水") == "信任崩塌"
    assert normalize_reversal("假死") == "生死反转"
    status, _metrics = check_isomorphic_reversal(
        ["身份错位"], [0.5], [{"type": "真假身份", "position_ratio": 0.5}]
    )
    assert status == "fail", "别名归一没生效 → 换个说法就能绕过 ⑧"


def test_rev_multiple_hits_one_same_position_fails() -> None:
    """多条反转里只要**有一条**类型+位置都撞 → fail。"""
    status, metrics = check_isomorphic_reversal(
        ["身份错位", "信任崩塌"],
        [0.2, 0.8],
        [
            {"type": "身份错位", "position_ratio": 0.9},  # 位置不同
            {"type": "信任崩塌", "position_ratio": 0.8},  # 撞
        ],
    )
    assert status == "fail", metrics


def test_rev_reversal_table_with_garbage_rows() -> None:
    """登记表里混入非 dict / 缺字段 → 过滤，不炸。"""
    status, _metrics = check_isomorphic_reversal(
        ["身份错位"],
        [0.5],
        [{"type": "信任崩塌", "position_ratio": 0.5}, "垃圾", {"no_type": 1}],
    )
    assert status == "pass"
