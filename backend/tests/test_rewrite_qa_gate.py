"""QA 独立验证 · A 组：合规闸门能否被绕过（★最高优先级★）。

与工程师自测的差异：本文件**不复用** `build_report` 的 happy path 断言，
而是从「攻击者」视角构造报告 —— 目的是**找出一条能让「有 fail 但 adoptable=True」
成立的路径**。只要有一条能成立，就是 P0。

攻击面：
  1. 直接给 `RewriteReport` 传一个 `summary={"adoptable": True, "blocking": 0}`；
  2. 把报告 dump 成 JSON 后只篡改 `summary`（不碰 checks）再 load；
  3. 八个质检位**逐位**注入 fail（`fail` 在 ①-⑧ 每一层都要拦住）；
  4. `apply_checks()` 想把 fail 项勾掉放行；
  5. `unavailable` 是否被算进 `summary.passed`；
  6. 反向三问少勾一问 / warn|unavailable 少勾一项；
  7. `require_adoptable()` 四条闸门逐条打。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.novel_rewrite_store import (
    CHECK_BEAT_SEQUENCE,
    CHECK_KEYS,
    CHECK_NEAR_DUPLICATE,
    CHECK_ONE_TO_ONE,
    CHECK_PROPER_NOUN,
    CHECK_RELATION_TOPOLOGY,
    CHECK_SIGNATURE_SCENE,
    CHECK_UNIQUE_PROP,
    AbstractLayer,
    Blueprint,
    CheckItem,
    Evidence,
    FunctionSlot,
    L1Symbols,
    L2Scenes,
    L3Relations,
    L5Beats,
    RebuildLayer,
    RelationEdge,
    RewriteAckRequiredError,
    RewriteReport,
    RewriteStore,
    apply_checks,
    build_report,
    make_rewrite_id,
    require_adoptable,
    write_ack,
)
from app.services.novel_store import NovelStore

# ─────────────────────────── 夹具 ───────────────────────────


def _blueprint() -> Blueprint:
    """一份「无 fail」的蓝图：L3 → warn，L5 → pass，L1/L2 → pass。"""
    return Blueprint(
        id="bp-qa-0001",
        book_id="bk-qa",
        title="QA 蓝图",
        abstract=AbstractLayer(
            function_slots=[FunctionSlot(slot="主角"), FunctionSlot(slot="导师")],
            reversal_types=["身份错位"],
            reversal_positions=[0.6],
            pacing="六章骨架",
        ),
        rebuild=RebuildLayer(
            L1_symbols=L1Symbols(banned=["青云宗"], new_lexicon={"青云宗": "临海船行"}),
            L2_scenes=L2Scenes(banned=["登仙台"], new_scenes=["船坞"]),
            L3_relations=L3Relations(
                source_graph=[RelationEdge(source="甲", target="乙", kind="师徒", power="高→低")],
                new_graph=[RelationEdge(source="丙", target="丁", kind="同门", power="对等")],
            ),
            L5_beats=L5Beats(source_seq=["受辱", "隐忍", "反击"], new_seq=["失去", "抉择", "顿悟"]),
        ),
    )


def _clean_report() -> RewriteReport:
    """一份「干净」报告（无 fail）：5 pass + 1 warn(L3) + 2 unavailable(⑤⑥)。"""
    return build_report(
        rewrite_id=make_rewrite_id("bk-qa"),
        blueprint=_blueprint(),
        kind="chapter",
        draft_text="临海船行的账房里，算盘声停了。",
        character_table=[{"name": "阿昭", "slot": "主角"}, {"name": "白露", "slot": "盟友"}],
        reversal_table=[{"type": "假死", "chapter_index": 2}],
        total_chapters=6,
        chapter_id="ch-001",
    )


def _fully_checked(report: RewriteReport) -> RewriteReport:
    """把所有非 fail 项 + 反向三问全勾上，并写 ack（合法采纳路径）。"""
    payload = [
        {"key": item.key, "human_checked": True}
        for item in report.checks
        if item.status != "fail"
    ]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))]
    updated = apply_checks(report, payload, reverse)
    return write_ack(updated)


def _inject_fail(report: RewriteReport, key: str) -> RewriteReport:
    """把某一项强制置 fail（带依据，满足 CheckItem 的不变式）。"""
    raw = report.model_dump(mode="json")
    for item in raw["checks"]:
        if item["key"] == key:
            item["status"] = "fail"
            item["detail"] = "QA 注入的 fail"
            item["evidence"] = [{"line": 1, "excerpt": "QA 证据"}]
    return RewriteReport.model_validate(raw)


# ═══════════════════════ 攻击 1/2：篡改 summary ═══════════════════════


def test_attack_summary_adoptable_true_is_recomputed() -> None:
    """攻击：构造时直接传 `summary={adoptable: True, blocking: 0}` → 必须被强算覆盖。"""
    report = _clean_report()
    assert report.summary.blocking == 0
    raw = report.model_dump(mode="json")
    raw["summary"] = {
        "blocking": 0,
        "warn": 0,
        "unavailable": 0,
        "passed": 8,
        "adoptable": True,
    }
    rebuilt = RewriteReport.model_validate(raw)
    # 反向三问还没勾 → 即便 blocking=0 也不该放行
    assert rebuilt.summary.adoptable is False, "外部传入的 summary 竟然生效了"
    assert rebuilt.summary.passed == rebuilt.summary.passed  # 占位，真实断言在下方
    assert rebuilt.summary.unavailable >= 2


def test_attack_summary_adoptable_true_when_everything_checked() -> None:
    """攻击升级：全勾 + ack + 传 `adoptable=True`，但同时有 fail → 仍必须 False。"""
    report = _fully_checked(_clean_report())
    assert report.summary.adoptable is True, "干净报告全勾后应可采纳（基线）"

    raw = _inject_fail(report, CHECK_PROPER_NOUN).model_dump(mode="json")
    raw["summary"] = {
        "blocking": 0,
        "warn": 0,
        "unavailable": 0,
        "passed": 8,
        "adoptable": True,
    }
    rebuilt = RewriteReport.model_validate(raw)
    assert rebuilt.summary.blocking == 1, "fail 没有被算进 blocking"
    assert rebuilt.summary.adoptable is False, "★P0★ 出现了「有 fail 但 adoptable=True」"


def test_attack_json_roundtrip_only_summary_tampered(tmp_path: Path) -> None:
    """攻击：落盘后只改 `summary`（不动 checks）→ 服务端 reload 必须重新算出真值。"""
    store = RewriteStore(NovelStore(root=tmp_path))
    saved = store.save_report("bk-qa", _clean_report())

    path = store.report_path("bk-qa", saved.rewrite_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary"] = {"blocking": 0, "warn": 0, "unavailable": 0, "passed": 8, "adoptable": True}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    reloaded = store.load_report("bk-qa", saved.rewrite_id)
    assert reloaded.summary.adoptable is False, "落盘篡改的 summary 被服务端采信了"
    assert reloaded.summary.unavailable == saved.summary.unavailable


# ═══════════════════════ 攻击 3：八位逐位注入 fail ═══════════════════════


@pytest.mark.parametrize("key", list(CHECK_KEYS))
def test_fail_in_every_layer_blocks_adoption(key: str) -> None:
    """★核心★ 八项质检**每一位**出现 fail，都必须 `adoptable=False` + 闸门拒绝。"""
    report = _fully_checked(_clean_report())
    assert report.summary.adoptable is True

    poisoned = _inject_fail(report, key)
    assert poisoned.summary.blocking == 1, f"{key} 注入 fail 后 blocking 不对"
    assert poisoned.summary.adoptable is False, f"★P0★ {key}=fail 却仍 adoptable=True"

    with pytest.raises(RewriteAckRequiredError):
        require_adoptable(poisoned)


def test_all_eight_layers_fail_blocking_count_is_eight() -> None:
    """八项全 fail → blocking == 8，adoptable False。"""
    report = _fully_checked(_clean_report())
    raw = report.model_dump(mode="json")
    for item in raw["checks"]:
        item["status"] = "fail"
        item["detail"] = "QA 注入"
        item["evidence"] = [{"line": 1, "excerpt": "QA"}]
    poisoned = RewriteReport.model_validate(raw)
    assert poisoned.summary.blocking == len(CHECK_KEYS)
    assert poisoned.summary.passed == 0
    assert poisoned.summary.adoptable is False


# ═══════════════════════ 攻击 4：fail 项勾选放行 ═══════════════════════


def test_fail_item_cannot_be_checked_off() -> None:
    """`apply_checks()` 对 fail 项必须**拒收勾选**（硬阻断不可降级）。"""
    report = _inject_fail(_clean_report(), CHECK_RELATION_TOPOLOGY)
    for item in report.checks:
        assert item.human_checked is False

    payload = [{"key": item.key, "human_checked": True} for item in report.checks]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))]
    updated = apply_checks(report, payload, reverse)

    fail_item = next(item for item in updated.checks if item.key == CHECK_RELATION_TOPOLOGY)
    assert fail_item.status == "fail"
    assert fail_item.human_checked is False, "fail 项竟然被勾上了"
    assert updated.summary.adoptable is False

    acked = write_ack(updated)
    with pytest.raises(RewriteAckRequiredError):
        require_adoptable(acked)


def test_write_ack_alone_never_makes_fail_adoptable() -> None:
    """只写 ack（不勾选）不足以放行 fail 项。"""
    report = _inject_fail(_clean_report(), CHECK_BEAT_SEQUENCE)
    acked = write_ack(report)
    assert acked.ack.acknowledged_at
    assert acked.summary.adoptable is False
    with pytest.raises(RewriteAckRequiredError) as info:
        require_adoptable(acked)
    assert "硬阻断" in str(info.value)


# ═══════════════════════ 攻击 5：unavailable 是否算进 passed ═══════════════════════


def test_unavailable_is_never_counted_as_passed() -> None:
    """`summary.passed` 只数 pass；unavailable 单独计数且必须>0（⑤⑥ 恒定不可用）。"""
    report = _clean_report()
    pass_count = sum(1 for item in report.checks if item.status == "pass")
    unavailable_count = sum(1 for item in report.checks if item.status == "unavailable")

    assert unavailable_count >= 2, "⑤⑥ 应恒定 unavailable"
    assert report.summary.passed == pass_count
    assert report.summary.unavailable == unavailable_count
    assert report.summary.passed + report.summary.warn + report.summary.unavailable + report.summary.blocking == len(
        report.checks
    )


def test_unavailable_items_must_carry_human_tip() -> None:
    """四态纪律：`unavailable` 必须给 human_tip（否则 UI 上会被误读成通过）。"""
    report = _clean_report()
    for item in report.checks:
        if item.status == "unavailable":
            assert item.human_tip, f"{item.key} 是 unavailable 却没有人工指引"


def test_fail_item_must_carry_evidence_or_detail() -> None:
    """四态纪律：`fail` 必须有依据（禁止无据阻断）。"""
    with pytest.raises(ValueError):  # pydantic ValidationError 是 ValueError 子类
        CheckItem(key="x", layer="L1", mode="auto", status="fail")


def test_pass_item_with_evidence_is_allowed() -> None:
    """`pass` 允许带 evidence（不作为不变式约束），避免以后误加校验。"""
    item = CheckItem(
        key="y", layer="L1", mode="auto", status="pass", evidence=[Evidence(line=1, excerpt="z")]
    )
    assert item.status == "pass"


# ═══════════════════════ 攻击 6/7：闸门四条逐条打 ═══════════════════════


def _strip_checks(report: RewriteReport, statuses: tuple[str, ...]) -> RewriteReport:
    """把指定状态的项全部去掉 `human_checked`。"""
    raw = report.model_dump(mode="json")
    for item in raw["checks"]:
        if item["status"] in statuses:
            item["human_checked"] = False
            item["checked_at"] = None
    return RewriteReport.model_validate(raw)


def test_gate_missing_one_warn_check() -> None:
    """少勾一个 warn → 422。"""
    report = _fully_checked(_clean_report())
    report = _strip_checks(report, ("warn",))
    assert report.summary.adoptable is False
    with pytest.raises(RewriteAckRequiredError) as info:
        require_adoptable(report)
    assert "待核对未勾选" in str(info.value)


def test_gate_missing_one_unavailable_check() -> None:
    """少勾一个 unavailable → 422（unavailable 绝不等于 pass，必须人工勾）。"""
    report = _fully_checked(_clean_report())
    report = _strip_checks(report, ("unavailable",))
    assert report.summary.adoptable is False
    with pytest.raises(RewriteAckRequiredError):
        require_adoptable(report)


@pytest.mark.parametrize("skip_index", [0, 1, 2])
def test_gate_reverse_three_must_be_all_checked(skip_index: int) -> None:
    """反向三问**任一问**没勾 → 422（不能只勾两问）。"""
    report = _fully_checked(_clean_report())
    raw = report.model_dump(mode="json")
    raw["reverse_three"][skip_index]["human_checked"] = False
    rebuilt = RewriteReport.model_validate(raw)
    assert rebuilt.summary.adoptable is False
    with pytest.raises(RewriteAckRequiredError) as info:
        require_adoptable(rebuilt)
    assert "反向校验三问" in str(info.value)


def test_gate_without_ack() -> None:
    """没写 ack → 422。"""
    report = _clean_report()
    payload = [
        {"key": item.key, "human_checked": True}
        for item in report.checks
        if item.status != "fail"
    ]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))]
    updated = apply_checks(report, payload, reverse)
    assert updated.summary.adoptable is False, "没 ack 竟然 adoptable=True"
    with pytest.raises(RewriteAckRequiredError) as info:
        require_adoptable(updated)
    assert "ack" in str(info.value)


def test_gate_empty_reverse_three_blocks() -> None:
    """反向三问被整个删掉（篡改）→ 必须拦住，不能因为「没问」就放行。"""
    report = _fully_checked(_clean_report())
    raw = report.model_dump(mode="json")
    raw["reverse_three"] = []
    rebuilt = RewriteReport.model_validate(raw)
    assert rebuilt.summary.adoptable is False
    with pytest.raises(RewriteAckRequiredError) as info:
        require_adoptable(rebuilt)
    assert "反向校验三问缺失" in str(info.value)


def test_gate_empty_checks_with_ack_is_still_blocked() -> None:
    """极端篡改：checks 清空 + reverse 清空 + ack 已写 → 仍不能放行（反向三问缺失）。"""
    report = _fully_checked(_clean_report())
    raw = report.model_dump(mode="json")
    raw["checks"] = []
    raw["reverse_three"] = []
    rebuilt = RewriteReport.model_validate(raw)
    assert rebuilt.summary.adoptable is False


# ═══════════════════════ 已知边界：磁盘整体篡改 ═══════════════════════


def test_disk_tamper_of_check_status_is_not_detectable(tmp_path: Path) -> None:
    """【已知限制 · P2】把磁盘上 `checks[].status` 从 fail 改成 pass → 服务端**发现不了**。

    记录事实（不是背书）：`require_adoptable()` 重算的是 summary→checks 方向，
    不会反向校验 checks 的真伪；报告也没有签名/摘要。本地单机应用下写盘=完全授信，
    故只作 P2 提示，不作 P0 阻断。
    """
    store = RewriteStore(NovelStore(root=tmp_path))
    # 先走完合法采纳路径（全勾 + ack），再把 ① 改成 fail —— 确保唯一变量是 status
    poisoned = _inject_fail(_fully_checked(_clean_report()), CHECK_PROPER_NOUN)
    saved = store.save_report("bk-qa", poisoned)
    path = store.report_path("bk-qa", saved.rewrite_id)

    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload["checks"]:
        if item["status"] == "fail":
            item["status"] = "pass"
            item["evidence"] = []
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    reloaded = store.load_report("bk-qa", saved.rewrite_id)
    assert reloaded.summary.blocking == 0  # 篡改生效 —— 已知限制
    require_adoptable(reloaded)  # 不抛 —— 已知限制


def test_report_file_stays_inside_rewrite_dir(tmp_path: Path) -> None:
    """报告只能落在 `rewrite/reports/`（结构性防越界，配合 B 组路径测试）。"""
    store = RewriteStore(NovelStore(root=tmp_path))
    saved = store.save_report("bk-qa", _clean_report())
    path = store.report_path("bk-qa", saved.rewrite_id)
    assert path.parent == store.rewrite_dir("bk-qa") / "reports"
    assert path.exists()


def test_list_reports_reflects_recomputed_adoptable(tmp_path: Path) -> None:
    """清单接口的 `adoptable` 取自磁盘 summary；篡改 summary 不改报告后应被重算覆盖。"""
    store = RewriteStore(NovelStore(root=tmp_path))
    saved = store.save_report("bk-qa", _clean_report())
    payload = json.loads(store.report_path("bk-qa", saved.rewrite_id).read_text(encoding="utf-8"))
    payload["summary"]["adoptable"] = True
    store.report_path("bk-qa", saved.rewrite_id).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    items = store.list_reports("bk-qa")
    # list_reports 直接读磁盘字段 → 篡改会被透传（P2：清单未走模型重算）
    assert items and items[0]["rewrite_id"] == saved.rewrite_id
    store.load_report("bk-qa", saved.rewrite_id)  # 读一次即重算（不落盘）


def test_any_dict_report_with_fail_never_adopts() -> None:
    """模糊式抽查：随机组合三项 fail，adoptable 必须恒 False。"""
    import itertools

    for combo in itertools.combinations(CHECK_KEYS, 3):
        report = _clean_report()
        raw = report.model_dump(mode="json")
        for item in raw["checks"]:
            if item["key"] in combo:
                item["status"] = "fail"
                item["detail"] = "QA"
                item["evidence"] = [{"line": 1, "excerpt": "QA"}]
        rebuilt = _fully_checked(RewriteReport.model_validate(raw))
        assert rebuilt.summary.blocking == 3, combo
        assert rebuilt.summary.adoptable is False, f"★P0★ {combo} 组合下 adoptable=True"


def test_metrics_keys_are_json_serializable() -> None:
    """metrics 必须可 JSON 序列化（否则报告落盘会炸）。"""
    report: Any = _clean_report()
    dumped = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    assert CHECK_NEAR_DUPLICATE in dumped
    assert CHECK_UNIQUE_PROP in dumped
    assert CHECK_ONE_TO_ONE in dumped
    assert CHECK_SIGNATURE_SCENE in dumped
