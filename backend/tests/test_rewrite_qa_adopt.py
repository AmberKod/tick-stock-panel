"""QA 独立验证 · A 组（API 侧）：`POST /adopt` 采纳闸门能否被绕过。

与模型层的差别：这里**真的发 HTTP**，验证路由层没有偷偷放行 ——
特别是「带 ack 但报告里有 fail」「反向三问少勾一问」「version 过期」
这三条，以及「被拒的采纳不能污染正文 / 大纲 / 报告」。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel_rewrite as rw_api
from app.services.novel_rewrite_store import (
    Blueprint,
    RewriteStore,
    apply_checks,
    build_report,
    make_rewrite_id,
    write_ack,
)
from app.services.novel_store import NovelStore

_NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷",
        "order": 1,
        "children": [
            {"id": "ch-001", "type": "chapter", "title": "引子", "order": 1,
             "summary": "冷开场", "beat": "埋伏笔"}
        ],
    }
]

_OUTLINE_PATCH = {
    "nodes": [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-001", "type": "chapter", "title": "重写后的第一章", "order": 1,
                 "status": "draft", "word_target": 3000, "summary": "新细纲", "beat": "新节拍"}
            ],
        }
    ]
}


class Env:
    """测试环境。"""

    def __init__(self, client: TestClient, store: NovelStore, rewrite: RewriteStore) -> None:
        self.client = client
        self.store = store
        self.rewrite = rewrite


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    """最小 app + 临时根目录。"""
    store = NovelStore(root=tmp_path)
    rewrite = RewriteStore(store)
    app = FastAPI()
    app.include_router(rw_api.router)
    app.dependency_overrides[rw_api.shared_rewrite_store] = lambda: rewrite
    app.dependency_overrides[rw_api.shared_store] = lambda: store
    app.dependency_overrides[rw_api.shared_registry] = lambda: None
    with TestClient(app) as client:
        yield Env(client, store, rewrite)


def _seed(env: Env) -> str:
    """建书 + 大纲。"""
    book = env.store.create_book("QA 采纳书")
    env.store.save_outline(book.id, book.version, _NODES)
    return book.id


def _blueprint_payload() -> dict[str, Any]:
    """过闸门的蓝图（L3/L5 齐）。"""
    return {
        "title": "QA 蓝图",
        "abstract": {
            "function_slots": [{"slot": "主角"}, {"slot": "导师"}],
            "reversal_types": ["身份错位"],
            "reversal_positions": [0.6],
            "pacing": "六章骨架",
        },
        "rebuild": {
            "L1_symbols": {"banned": ["青云宗"], "new_lexicon": {}},
            "L2_scenes": {"banned": ["登仙台"], "new_scenes": []},
            "L3_relations": {
                "source_graph": [{"from": "甲", "to": "乙", "kind": "师徒", "power": "高→低"}],
                "new_graph": [{"from": "丙", "to": "丁", "kind": "同门", "power": "对等"}],
            },
            "L5_beats": {"source_seq": ["受辱", "隐忍", "反击"], "new_seq": ["失去", "抉择", "顿悟"]},
        },
    }


def _make_report(
    env: Env, book_id: str, *, kind: str = "chapter", body: str = "临海船行的账房里，算盘声停了。"
) -> Any:
    """造一份报告 + 对应草稿，返回 `RewriteReport`。"""
    rewrite_id = make_rewrite_id(book_id)
    if kind == "outline":
        draft_rel = env.rewrite.write_rewrite_draft(
            book_id, rewrite_id, "outline", json.dumps(_OUTLINE_PATCH, ensure_ascii=False)
        )
    else:
        draft_rel = env.rewrite.write_rewrite_draft(book_id, rewrite_id, "chapter", body)
    blueprint = Blueprint.model_validate(_blueprint_payload())
    report = build_report(
        rewrite_id=rewrite_id,
        blueprint=blueprint,
        kind=kind,
        draft_text=body if kind != "outline" else "",
        outline_patch=_OUTLINE_PATCH if kind == "outline" else None,
        character_table=[{"name": "阿昭", "slot": "主角"}, {"name": "白露", "slot": "盟友"}],
        reversal_table=[{"type": "假死", "chapter_index": 2}],
        total_chapters=6,
        chapter_id="ch-001" if kind == "chapter" else None,
        draft_file=draft_rel,
    )
    return env.rewrite.save_report(book_id, report)


def _check_all(env: Env, book_id: str, report: Any) -> None:
    """把所有非 fail 项 + 反向三问勾满。"""
    payload = {
        "checks": [
            {"key": item.key, "human_checked": True}
            for item in report.checks
            if item.status != "fail"
        ],
        "reverse": [
            {"index": index, "human_checked": True}
            for index in range(len(report.reverse_three))
        ],
    }
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/check", json=payload
    )
    assert response.status_code == 200, response.text


def _poison(env: Env, book_id: str, report: Any, key: str) -> Any:
    """把报告里某一项改成 fail（走磁盘，模拟「生成后又发现问题」）。"""
    raw = json.loads(
        env.rewrite.report_path(book_id, report.rewrite_id).read_text(encoding="utf-8")
    )
    for item in raw["checks"]:
        if item["key"] == key:
            item["status"] = "fail"
            item["detail"] = "QA 注入"
            item["evidence"] = [{"line": 1, "excerpt": "QA"}]
    env.rewrite.report_path(book_id, report.rewrite_id).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    return env.rewrite.load_report(book_id, report.rewrite_id)


# ═══════════════════════ 无 ack ═══════════════════════


def test_adopt_without_ack_is_422(env: Env) -> None:
    """不带 `ack` → 422 `rewrite_ack_required`，且正文不动。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    before = env.store.read_chapter(book_id, "ch-001")[0]

    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"target": "chapter"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "rewrite_ack_required"
    assert env.store.read_chapter(book_id, "ch-001")[0] == before


def test_adopt_ack_false_is_422(env: Env) -> None:
    """`ack=false` 等同于没勾。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": False, "target": "chapter"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_ack_required"


# ═══════════════════════ 有 fail ═══════════════════════


@pytest.mark.parametrize(
    "key",
    [
        "proper_noun",
        "signature_scene",
        "relation_topology",
        "beat_sequence",
        "one_to_one_character",
        "isomorphic_reversal",
    ],
)
def test_adopt_blocked_when_any_layer_fails(env: Env, key: str) -> None:
    """★核心★ 任一质检位 fail → 422，且**正文一个字都不改**。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    poisoned = _poison(env, book_id, report, key)
    assert poisoned.summary.blocking == 1

    before = env.store.read_chapter(book_id, "ch-001")[0]
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 422, response.text
    assert "硬阻断" in response.json()["detail"]["message"]
    assert env.store.read_chapter(book_id, "ch-001")[0] == before, "★P0★ 有 fail 却写了正文"


def test_rejected_adopt_does_not_persist_ack(env: Env) -> None:
    """被拒的采纳不能把 ack 落盘（否则下次「看起来已确认」）。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    _poison(env, book_id, report, "beat_sequence")

    env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    reloaded = env.rewrite.load_report(book_id, report.rewrite_id)
    assert reloaded.ack.acknowledged_at is None, "被拒的采纳竟然把 ack 写进了报告"


# ═══════════════════════ 勾选不全 ═══════════════════════


def test_adopt_blocked_when_reverse_three_incomplete(env: Env) -> None:
    """反向三问少勾一问 → 422。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    payload = {
        "checks": [
            {"key": item.key, "human_checked": True}
            for item in report.checks
            if item.status != "fail"
        ],
        "reverse": [{"index": 0, "human_checked": True}, {"index": 1, "human_checked": True}],
    }
    assert env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/check", json=payload
    ).status_code == 200

    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 422
    assert "反向校验三问" in response.json()["detail"]["message"]


def test_adopt_blocked_when_warn_unavailable_unchecked(env: Env) -> None:
    """warn / unavailable 项没勾满 → 422（unavailable 必须人工勾，不能自动放行）。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    payload = {
        "checks": [
            {"key": item.key, "human_checked": True}
            for item in report.checks
            if item.status == "pass"
        ],
        "reverse": [
            {"index": index, "human_checked": True}
            for index in range(len(report.reverse_three))
        ],
    }
    env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/check", json=payload
    )
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 422
    assert "待核对未勾选" in response.json()["detail"]["message"]


def test_fail_item_cannot_be_checked_via_api(env: Env) -> None:
    """走 API 勾 fail 项 → 勾不上（`human_checked` 保持 False）。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _poison(env, book_id, report, "proper_noun")
    payload = {
        "checks": [{"key": item.key, "human_checked": True} for item in report.checks],
        "reverse": [
            {"index": index, "human_checked": True}
            for index in range(len(report.reverse_three))
        ],
    }
    updated = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/check", json=payload
    ).json()["report"]
    fail_item = next(item for item in updated["checks"] if item["key"] == "proper_noun")
    assert fail_item["status"] == "fail"
    assert fail_item["human_checked"] is False


# ═══════════════════════ 成功路径 ═══════════════════════


def test_adopt_chapter_happy_path(env: Env) -> None:
    """全勾 + ack → 200，正文被写入且章节置 published。"""
    book_id = _seed(env)
    report = _make_report(env, book_id, body="新正文：船行的灯还亮着。")
    _check_all(env, book_id, report)

    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 200, response.text
    assert env.store.read_chapter(book_id, "ch-001")[0] == "新正文：船行的灯还亮着。"
    node = env.store.get_chapter_node(book_id, "ch-001")[1]
    assert node.status == "published"

    saved = env.rewrite.load_report(book_id, report.rewrite_id)
    assert saved.ack.acknowledged_at
    assert saved.ack.disclaimer_version
    assert saved.ack.checked_keys, "ack 里没留下勾选项（无法留痕）"


def test_repeated_adopt_is_idempotent_and_flagged(env: Env) -> None:
    """★P2-4 已修★：同一 rewrite_id 重复 adopt —— 幂等不损坏，且**有留痕**。

    修之前：两次都 200，报告里没有任何「已采纳」标记，审计上分不清首采与重采。
    修之后：`adopted_at` / `adopted_target` 落盘，重采会刷新 `adopted_at`
    （幂等覆盖仍然不损坏数据，正文内容一致）。
    """
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    url = f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt"
    first = env.client.post(url, json={"ack": True, "target": "chapter"})
    second = env.client.post(url, json={"ack": True, "target": "chapter"})
    assert first.status_code == 200 and second.status_code == 200

    saved = env.rewrite.load_report(book_id, report.rewrite_id)
    assert saved.adopted_at, "首次采纳没有留痕"
    assert saved.adopted_target == "chapter"
    # 重采刷新时间戳（>=，同秒内可能相等）
    assert str(saved.adopted_at) >= str(first.json().get("adopted_at") or "")


def test_adopted_at_is_absent_before_adopt(env: Env) -> None:
    """★P2-4 配套★：没采纳过的报告 `adopted_at` 必须为 None ——
    否则「已落书」与「只确认过」会被混为一谈。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    saved = env.rewrite.load_report(book_id, report.rewrite_id)
    assert saved.adopted_at is None
    assert saved.adopted_target is None


def test_adopt_missing_report_is_404(env: Env) -> None:
    """报告不存在 → 404（不是 422、更不是 200）。"""
    book_id = _seed(env)
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/rw-{book_id}-20260101010101-abcd/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 404


def test_adopt_invalid_target_is_422(env: Env) -> None:
    """`target` 非法 → 422 `invalid_payload`。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "whole_book"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_payload"


# ═══════════════════════ 大纲采纳 · version 乐观锁 ═══════════════════════


def test_adopt_outline_requires_version(env: Env) -> None:
    """`target=outline` 不带 version → 422（不能静默覆盖大纲）。"""
    book_id = _seed(env)
    report = _make_report(env, book_id, kind="outline")
    _check_all(env, book_id, report)
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "outline"},
    )
    assert response.status_code == 422
    assert "version" in response.json()["detail"]["message"]


def test_adopt_outline_stale_version_is_409(env: Env) -> None:
    """★乐观锁★ version 过期 → 409 `version_conflict`，大纲**不得**被覆盖。"""
    book_id = _seed(env)
    report = _make_report(env, book_id, kind="outline")
    _check_all(env, book_id, report)

    before = env.store.get_book(book_id).outline.model_dump(mode="json")
    stale = env.store.get_book(book_id).version - 1
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "outline", "version": stale},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "version_conflict"
    assert env.store.get_book(book_id).outline.model_dump(mode="json") == before


def test_adopt_outline_current_version_succeeds(env: Env) -> None:
    """version 正确 → 200，大纲被替换。"""
    book_id = _seed(env)
    report = _make_report(env, book_id, kind="outline")
    _check_all(env, book_id, report)
    current = env.store.get_book(book_id).version
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "outline", "version": current},
    )
    assert response.status_code == 200, response.text
    titles = [c.title for v in env.store.get_book(book_id).outline.nodes for c in v.children]
    assert "重写后的第一章" in titles


def test_adopt_outline_stale_version_writes_no_ack(env: Env) -> None:
    """★P2-9 已修★：version 冲突必须在 ack **之前**被拦下。

    修之前顺序是 write_ack → require_adoptable → save_report → 写大纲，
    于是 409 时 ack 已写、报告已落盘，审计上留下一次「已确认但没采纳」的留痕。
    修之后：`target=outline` 且 `version` 缺失 / 过期 → 先撞锁，ack 不落盘。
    """
    book_id = _seed(env)
    report = _make_report(env, book_id, kind="outline")
    _check_all(env, book_id, report)
    stale = env.store.get_book(book_id).version - 1
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "outline", "version": stale},
    )
    assert response.status_code == 409, response.text
    reloaded = env.rewrite.load_report(book_id, report.rewrite_id)
    assert reloaded.ack.acknowledged_at is None, "409 时 ack 不应落盘"
    assert reloaded.adopted_at is None


def test_adopt_outline_without_version_is_422_and_writes_no_ack(env: Env) -> None:
    """★P2-9 配套★：`target=outline` 缺 version → 422，且 ack 同样不落盘。"""
    book_id = _seed(env)
    report = _make_report(env, book_id, kind="outline")
    _check_all(env, book_id, report)
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "outline"},
    )
    assert response.status_code == 422, response.text
    reloaded = env.rewrite.load_report(book_id, report.rewrite_id)
    assert reloaded.ack.acknowledged_at is None
    assert "version" in response.json()["detail"]["message"]


def test_adopt_chapter_without_chapter_id_is_422(env: Env) -> None:
    """章节型报告没绑 chapter_id → 422（不能写进「随便哪一章」）。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    raw = json.loads(
        env.rewrite.report_path(book_id, report.rewrite_id).read_text(encoding="utf-8")
    )
    raw["chapter_id"] = None
    env.rewrite.report_path(book_id, report.rewrite_id).write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    _check_all(env, book_id, env.rewrite.load_report(book_id, report.rewrite_id))
    response = env.client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{report.rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 422
    assert "未绑定章节" in response.json()["detail"]["message"]


def test_write_ack_records_checked_keys(env: Env) -> None:
    """`write_ack()` 的留痕：时间 + 免责版本 + 勾了哪些项。"""
    book_id = _seed(env)
    report = _make_report(env, book_id)
    _check_all(env, book_id, report)
    acked = write_ack(apply_checks(
        env.rewrite.load_report(book_id, report.rewrite_id),
        [{"key": item.key, "human_checked": True} for item in report.checks if item.status != "fail"],
        [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))],
    ))
    assert acked.ack.acknowledged_at and acked.ack.disclaimer_version
    assert set(acked.ack.checked_keys) == {
        item.key for item in report.checks if item.status != "fail"
    }


def test_blueprint_with_gate_layers_is_required_for_generation(env: Env) -> None:
    """L3/L5 缺表 → 闸门未过（`is_gate_ready` 返回 missing）。"""
    book_id = _seed(env)
    payload = _blueprint_payload()
    payload["rebuild"]["L3_relations"]["new_graph"] = []
    payload["rebuild"]["L5_beats"]["new_seq"] = []
    response = env.client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": payload}
    )
    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert set(response.json()["missing_layers"]) == {"L3", "L5"}
