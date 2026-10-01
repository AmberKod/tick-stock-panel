"""换元仿写 — 路由层契约单测（自包含：自建 FastAPI + `tmp_path` 注入）。

覆盖 `ARCHITECTURE-rewrite.md` §3.6（T03 完成判据）与 PRD §4 P0-9/P0-10/P0-11：
13 端点契约；`risk_ack` 缺失 → 422 `rewrite_ack_required`；L3 空 → 422
`rewrite_gate_blocked`；粘贴原文 → 422 `rewrite_source_rejected` 且响应**不含原文**；
无 ack → `adopt` 422；version 过期 → 409；AI 未配置 → 503 `ai_unavailable`
**且不建 job**；`rw-` job 轮询 / resume / 并发上限。

**自包含约定**：本仓 `tests/` 无 `conftest.py`，全部用例自建 app 并注入
`NovelStore(root=tmp_path)`（不 import `app.main`，避免拉起完整生命周期）。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel_rewrite as rw_api
from app.services import novel_rewrite_ai
from app.services.novel_rewrite_jobs import RewriteJobRegistry
from app.services.novel_rewrite_store import (
    DISCLAIMER_TEXT,
    DISCLAIMER_VERSION,
    PRECHECK_HONESTY_NOTE,
    REWRITE_DIR,
    RewriteStore,
    build_report,
    capture_authoritative_snapshot,
    make_rewrite_job_id,
    parse_rewrite_job_id,
)
from app.services.novel_store import NovelStore

_NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷",
        "order": 1,
        "children": [
            {
                "id": "ch-001",
                "type": "chapter",
                "title": "引子",
                "order": 1,
                "summary": "冷开场",
                "beat": "埋下伏笔",
            }
        ],
    }
]

_PLAN_MD = """## L1 符号层
- 青云宗 → 改为「临海船行」

## L3 关系层
- 陆昭 → 白露 [同门 / 对等]

## L5 桥段层
失去 → 抉择 → 顿悟

```rw-roles
[{"name": "陆昭", "slot": "主角"}, {"name": "白露", "slot": "盟友"}]
```

```rw-reversals
[{"type": "身份错位", "chapter_index": 1, "position_ratio": 0.1}]
```
"""

_OUTLINE_JSON = json.dumps(
    {
        "nodes": [
            {
                "id": "v1",
                "type": "volume",
                "title": "第一卷",
                "order": 1,
                "children": [
                    {
                        "id": "ch-001",
                        "type": "chapter",
                        "title": "重写后的第一章",
                        "order": 1,
                        "status": "draft",
                        "word_target": 3000,
                        "summary": "新细纲",
                        "beat": "新节拍",
                        "info_gain": "船行的来历",
                        "foreshadow_planted": ["黑匣子"],
                        "foreshadow_resolved": [],
                        "stimulus": "当众被质疑",
                    }
                ],
            }
        ]
    },
    ensure_ascii=False,
)


# ─────────────────────────── 夹具 ───────────────────────────


class Env:
    """测试环境（client 用 `with` 进入，拿到**常驻事件循环**的 portal）。

    必须用 `with TestClient(app)`：Starlette 的 TestClient 若不用上下文管理器，
    会为**每个请求**新建一个 portal（新事件循环），导致 `asyncio.create_task`
    起的后台任务在请求返回时立刻被取消（job 永远停在 `cancelled`）。
    """

    def __init__(self, client: TestClient, store: NovelStore,
                 rewrite: RewriteStore, registry: RewriteJobRegistry) -> None:
        self.client = client
        self.store = store
        self.rewrite = rewrite
        self.registry = registry


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    """自建 app + 注入临时根目录的 store / registry（常驻 portal）。

    **收尾必须解绑共享 semaphore**：`RewriteJobRegistry` 跑任务会
    `async with ai_semaphore()`，而 `ai_semaphore()` 返回的是 `novel_jobs`
    里的**模块级** `asyncio.Semaphore` —— 它一旦被本用例的 portal 事件循环
    绑定，后续任何在**别的事件循环**里跑的用例（例如
    `test_novel_qa_jobs.py::test_semaphore_caps_concurrent_jobs_at_two`）
    都会撞上 `RuntimeError: ... is bound to a different event loop`。
    所以这里在用例结束后把 `_loop` 置回 None（与既有用例同样的姿势）。
    """
    from app.services import novel_jobs as novel_jobs_mod

    store = NovelStore(root=tmp_path)
    rewrite = RewriteStore(store)
    registry = RewriteJobRegistry(store=store)
    app = FastAPI()
    app.include_router(rw_api.router)
    app.dependency_overrides[rw_api.shared_rewrite_store] = lambda: rewrite
    app.dependency_overrides[rw_api.shared_store] = lambda: store
    app.dependency_overrides[rw_api.shared_registry] = lambda: registry
    try:
        with contextlib.ExitStack() as stack:
            client = stack.enter_context(TestClient(app))
            yield Env(client, store, rewrite, registry)
    finally:
        novel_jobs_mod._SEMAPHORE._loop = None


def _seed(store: NovelStore) -> str:
    """建书 + 大纲，返回 book_id。"""
    book = store.create_book("换元仿写路由测试")
    store.save_outline(book.id, book.version, _NODES)
    return book.id


def _blueprint_payload() -> dict[str, Any]:
    """一份**过闸门**的蓝图（L3 两图 + L5 两序列齐）。"""
    return {
        "title": "结构蓝图",
        "source_ref": {
            "label": "某修真小说",
            "work_type": "修真",
            "note": "- 功能位：主角 / 导师 / 对手\n- 情绪节拍：压抑 → 误解 → 爆发达\n- 母题：身份错位",
        },
        "abstract": {
            "function_slots": [{"slot": "主角"}, {"slot": "导师"}],
            "emotion_beats": ["压抑", "误解", "爆发达"],
            "reversal_types": ["身份错位"],
            "reversal_positions": [0.6],
            "pacing": "六章骨架",
        },
        "rebuild": {
            "L1_symbols": {"banned": ["青云宗"], "new_lexicon": {"青云宗": "临海船行"}},
            "L2_scenes": {"banned": ["登仙台"], "new_scenes": ["船坞"]},
            "L3_relations": {
                "source_graph": [
                    {"from": "甲", "to": "乙", "kind": "师徒", "power": "高→低"}
                ],
                "new_graph": [
                    {"from": "丙", "to": "丁", "kind": "同门", "power": "对等"}
                ],
            },
            "L4_events": {"new_causal_chain": ["船行缺钱 → 接下私活"]},
            "L5_beats": {
                "source_seq": ["受辱", "隐忍", "反击"],
                "new_seq": ["失去", "抉择", "顿悟"],
            },
        },
    }


def _wait_job(client: TestClient, book_id: str, job_id: str, timeout: float = 10.0) -> dict:
    """轮询到终态（前端同一姿势）。"""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/novel/books/{book_id}/rewrite/jobs/{job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in ("done", "failed", "cancelled"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job 未进入终态: {payload}")


class _AiStub:
    """`generate_ai_text` 的测试替身（**只替换网关，不替换业务**）。"""

    def __init__(self, kind: str = "plan", fail_times: int = 0) -> None:
        self.kind = kind
        self.fail_times = fail_times
        self.calls = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def __call__(self, messages: list[dict], **_kwargs: Any) -> str:
        self.calls += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            await asyncio.sleep(0.05)
            if self.calls <= self.fail_times:
                raise RuntimeError("模拟 AI 网关故障")
            if self.kind == "outline":
                return f"```json\n{_OUTLINE_JSON}\n```"
            if self.kind == "chapter":
                return "这是仿写出来的正文草稿。船行的灯还亮着。"
            return _PLAN_MD
        finally:
            self.concurrent -= 1


def _enable_ai(monkeypatch: pytest.MonkeyPatch, stub: _AiStub) -> None:
    """让 AI 可用 + 替换唯一网关（保持「唯一出口」纪律不变）。"""
    monkeypatch.setattr(novel_rewrite_ai, "ai_ready", lambda: (True, "", ""))
    monkeypatch.setattr(novel_rewrite_ai, "generate_ai_text", stub)


def _disable_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 不可用（fail-closed）。"""
    monkeypatch.setattr(
        novel_rewrite_ai,
        "ai_ready",
        lambda: (False, "ai_unavailable", "AI 网关未配置 — 仿写不可用。"),
    )


# ═══════════════════════════ R1/R2 · 蓝图 ═══════════════════════════


def test_r1_get_blueprint_empty(env: Env) -> None:
    """GET blueprint：不存在 → 空蓝图（不落盘）+ `ready=false` + missing。"""
    client, store, rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.get(f"/api/novel/books/{book_id}/rewrite/blueprint")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["ready"] is False
    assert set(payload["missing_layers"]) == {"L3", "L5"}
    assert not rewrite.blueprint_path(book_id).exists()


def test_r2_put_blueprint_ready(env: Env) -> None:
    """PUT blueprint：L3/L5 齐 → `ready=true`。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["missing_layers"] == []
    assert payload["blueprint"]["id"].startswith("bp-")


def test_r2_put_blueprint_rejected_and_not_persisted(env: Env) -> None:
    """PUT blueprint 命中预检 → 422 `rewrite_source_rejected` 且**不落盘**。"""
    client, store, rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    payload = _blueprint_payload()
    payload["source_ref"]["note"] = "他把那封信又读了一遍，窗外的雨一直没有停。" * 60
    response = client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": payload}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_source_rejected"
    assert not rewrite.blueprint_path(book_id).exists()


# ═══════════════════════════ R3 · 预检 ═══════════════════════════


def test_r3_precheck_pass(env: Env) -> None:
    """结构笔记 → 放行。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/precheck",
        json={"fields": {"note": "- 功能位：主角 / 导师\n- 情绪节拍：压抑 → 爆发达\n- 母题：身份错位"}},
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_r3_precheck_rejected_without_echo(env: Env) -> None:
    """粘贴原文 → 422，**响应体不含原文正文**（P0-3④）。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    prose = "他把那封信又读了一遍，窗外的雨一直没有停。" * 60
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/precheck", json={"fields": {"note": prose}}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "rewrite_source_rejected"
    assert detail["hits"]
    assert detail["sample"]
    assert prose[:100] not in json.dumps(detail, ensure_ascii=False)


def test_r3_precheck_requires_payload(env: Env) -> None:
    """既无 fields 也无 blueprint → 422 `invalid_payload`。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(f"/api/novel/books/{book_id}/rewrite/precheck", json={})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_payload"


# ═══════════════════════════ R4/R5/R6 · 生成 ═══════════════════════════


def test_r4_plan_requires_risk_ack(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """`risk_ack` 缺失 → 422 `rewrite_ack_required`。"""
    _enable_ai(monkeypatch, _AiStub())
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": False})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_ack_required"


def test_r4_plan_gate_blocked(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3/L5 空 → 422 `rewrite_gate_blocked`。"""
    _enable_ai(monkeypatch, _AiStub())
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_gate_blocked"


def test_r4_plan_ai_unavailable_creates_no_job(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI 不可用 → 503 `ai_unavailable`，**不建 job、不 mock**。"""
    _disable_ai(monkeypatch)
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_unavailable"
    checkpoints = store.checkpoints_dir(book_id)
    assert (not checkpoints.exists()) or list(checkpoints.glob("rw-*.json")) == []


def test_r4_plan_full_run_is_zero_write(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★P0-10★ 完整跑通 plan → 报告落 `rewrite/`，权威数据**零变更**。"""
    stub = _AiStub("plan")
    _enable_ai(monkeypatch, stub)
    client, store, rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    before = capture_authoritative_snapshot(store, book_id)

    put = client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    assert put.status_code == 200

    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True})
    assert response.status_code == 202
    job = response.json()
    assert job["job_id"].startswith("rw-")
    assert parse_rewrite_job_id(job["job_id"])[0] == book_id

    final = _wait_job(client, book_id, job["job_id"])
    assert final["status"] == "done", final
    assert final["rewrite_id"]
    assert rewrite.report_path(book_id, final["rewrite_id"]).exists()

    after = capture_authoritative_snapshot(store, book_id)
    assert before == after, "仿写生成阶段必须零写入 book.json / state.json / 正文/"

    # 设定卡的结构化块被解析出来 → ⑦/⑧ 不是 unavailable
    report = client.get(
        f"/api/novel/books/{book_id}/rewrite/reports/{final['rewrite_id']}"
    ).json()["report"]
    by_key = {c["key"]: c["status"] for c in report["checks"]}
    assert by_key["one_to_one_character"] != "unavailable"
    assert by_key["isomorphic_reversal"] != "unavailable"
    assert len(report["reverse_three"]) == 3


def test_r6_chapter_draft_run(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """chapter-draft：产物落 `rewrite/drafts/`，`正文/` 不变。"""
    _enable_ai(monkeypatch, _AiStub("chapter"))
    client, store, rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    _body, path = store.read_chapter(book_id, "ch-001")
    original = path.read_bytes()

    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/chapter-draft",
        json={"risk_ack": True, "chapter_id": "ch-001"},
    )
    assert response.status_code == 202
    job = _wait_job(client, book_id, response.json()["job_id"])
    assert job["status"] == "done"
    assert path.read_bytes() == original, "生成阶段绝不写 正文/"
    assert (rewrite.rewrite_dir(book_id) / "drafts").exists()


def test_r6_chapter_draft_requires_chapter_id(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kind=chapter` 缺 chapter_id → 422 `invalid_payload`。"""
    _enable_ai(monkeypatch, _AiStub())
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/chapter-draft", json={"risk_ack": True}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_payload"


# ═══════════════════════════ R7/R8/R9 · 报告与采纳 ═══════════════════════════


def _ready_report(client: TestClient, book_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """跑一个 chapter-draft 任务，返回 rewrite_id。"""
    _enable_ai(monkeypatch, _AiStub("chapter"))
    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/chapter-draft",
        json={"risk_ack": True, "chapter_id": "ch-001"},
    )
    job = _wait_job(client, book_id, response.json()["job_id"])
    assert job["status"] == "done"
    return str(job["rewrite_id"])


def test_r7_get_report(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET report：8 项 + 反向三问 + disclaimer。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    rewrite_id = _ready_report(client, book_id, monkeypatch)
    payload = client.get(f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}").json()
    assert payload["ok"] is True
    report = payload["report"]
    assert len(report["checks"]) == 8
    assert report["disclaimer"]["version"] == "rw-disclaimer-v1"
    assert report["summary"]["adoptable"] is False


def test_r8_check_then_adopt_requires_ack(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 ack → `POST adopt` 返回 422 `rewrite_ack_required`。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    rewrite_id = _ready_report(client, book_id, monkeypatch)
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/adopt",
        json={"ack": False},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_ack_required"


def test_r8_adopt_requires_all_checked(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ack 已给但未逐项勾选 → 422 `rewrite_ack_required`（服务端二次校验）。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    rewrite_id = _ready_report(client, book_id, monkeypatch)
    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/adopt",
        json={"ack": True},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "rewrite_ack_required"


def test_r9_adopt_chapter_writes_body_without_version_bump(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """采纳进正文：走 `write_chapter` + `set_chapter_status`，**不动 version**（A2）。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    rewrite_id = _ready_report(client, book_id, monkeypatch)
    book_before = store.get_book(book_id)

    report = client.get(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}"
    ).json()["report"]
    checks = [{"key": c["key"], "human_checked": True} for c in report["checks"]]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report["reverse_three"]))]
    assert client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/check",
        json={"checks": checks, "reverse": reverse},
    ).status_code == 200

    response = client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/adopt",
        json={"ack": True, "target": "chapter"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["chapter"]["chapter_id"] == "ch-001"

    book_after = store.get_book(book_id)
    assert book_after.version == book_before.version, "章节采纳不得让大纲乐观锁失效"
    body, _path = store.read_chapter(book_id, "ch-001")
    assert body.startswith("这是仿写出来的正文草稿")
    node = store.get_chapter_node(book_id, "ch-001")[1]
    assert node.status == "published"

    # ack 已落盘（留痕：时间 + 免责版本 + 勾了哪些项）
    saved = client.get(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}"
    ).json()["report"]
    assert saved["ack"]["acknowledged_at"]
    assert saved["ack"]["disclaimer_version"] == "rw-disclaimer-v1"
    assert saved["ack"]["checked_keys"]


def test_r9_adopt_outline_version_conflict(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大纲采纳带 version 乐观锁：过期 → 409 `version_conflict`。"""
    _enable_ai(monkeypatch, _AiStub("outline"))
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    response = client.post(f"/api/novel/books/{book_id}/rewrite/outline", json={"risk_ack": True})
    job = _wait_job(client, book_id, response.json()["job_id"])
    assert job["status"] == "done"
    rewrite_id = str(job["rewrite_id"])

    report = client.get(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}"
    ).json()["report"]
    checks = [{"key": c["key"], "human_checked": True} for c in report["checks"]]
    reverse = [{"index": i, "human_checked": True} for i in range(len(report["reverse_three"]))]
    client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/check",
        json={"checks": checks, "reverse": reverse},
    )

    bad = client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/adopt",
        json={"ack": True, "target": "outline", "version": 999},
    )
    assert bad.status_code == 409
    assert bad.json()["detail"]["code"] == "version_conflict"

    good = client.post(
        f"/api/novel/books/{book_id}/rewrite/reports/{rewrite_id}/adopt",
        json={"ack": True, "target": "outline", "version": store.get_book(book_id).version},
    )
    assert good.status_code == 200, good.text
    assert good.json()["version"] == store.get_book(book_id).version


def test_r13_list_reports(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """报告清单：最新在前，含 blocking / adoptable。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    rewrite_id = _ready_report(client, book_id, monkeypatch)
    payload = client.get(f"/api/novel/books/{book_id}/rewrite/reports").json()
    assert payload["count"] == 1
    assert payload["reports"][0]["rewrite_id"] == rewrite_id
    assert "blocking" in payload["reports"][0]
    assert "adoptable" in payload["reports"][0]


# ═══════════════════════════ R10/R11/R12 · 任务 ═══════════════════════════


def test_r10_get_job_invalid_id(env: Env) -> None:
    """非法 job_id → 422 `invalid_id`。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    assert client.get(
        f"/api/novel/books/{book_id}/rewrite/jobs/not-a-rw-job"
    ).status_code == 422
    assert client.get(
        f"/api/novel/books/{book_id}/rewrite/jobs/rw-{book_id}-20261005010101-abcd"
    ).status_code == 404


def test_r11_resume_after_step2_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """step2 注入失败 → `failed_step=generate` 且 step1 产物在；resume 后 step1 `at` 未改写。"""
    stub = _AiStub("plan", fail_times=1)
    _enable_ai(monkeypatch, stub)
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )

    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True})
    job_id = response.json()["job_id"]
    failed = _wait_job(client, book_id, job_id)
    assert failed["status"] == "failed"
    assert failed["failed_step"] == "generate"
    step1_at = failed["steps"][0]["at"]
    assert failed["steps"][0]["status"] == "done"

    resumed = client.post(
        f"/api/novel/books/{book_id}/rewrite/jobs/{job_id}/resume"
    ).json()
    final = _wait_job(client, book_id, resumed["job_id"])
    assert final["status"] == "done", final
    assert final["steps"][0]["at"] == step1_at, "resume 不得改写已完成步骤的时间戳"
    assert final["steps"][0]["status"] == "skipped"


def test_r12_cancel_job(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """协作式取消：置标记后任务进入终态。"""
    _enable_ai(monkeypatch, _AiStub("plan"))
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    response = client.post(f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True})
    job_id = response.json()["job_id"]
    cancelled = client.post(
        f"/api/novel/books/{book_id}/rewrite/jobs/{job_id}/cancel"
    ).json()
    assert cancelled["job_id"] == job_id
    final = _wait_job(client, book_id, job_id)
    assert final["status"] in ("done", "cancelled")


def test_shared_semaphore_caps_concurrency_at_two(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★共享 `Semaphore(2)`★ 并发 3 个仿写 job 时实测峰值 ≤2。"""
    stub = _AiStub("plan")
    _enable_ai(monkeypatch, stub)
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    client.put(
        f"/api/novel/books/{book_id}/rewrite/blueprint", json={"blueprint": _blueprint_payload()}
    )
    job_ids = []
    for _ in range(3):
        response = client.post(
            f"/api/novel/books/{book_id}/rewrite/plan", json={"risk_ack": True}
        )
        assert response.status_code == 202
        job_ids.append(response.json()["job_id"])
    for job_id in job_ids:
        final = _wait_job(client, book_id, job_id)
        assert final["status"] == "done", final
    assert stub.calls == 3
    assert stub.max_concurrent <= 2, f"实际并发峰值 {stub.max_concurrent} 超过 2"


# ═══════════════════════════ 附加 · 零写入快照端点 ═══════════════════════════


def test_snapshot_endpoint_is_read_only(env: Env) -> None:
    """快照端点返回三重指标（只读审计入口）。"""
    client, store, _rewrite, _registry = env.client, env.store, env.rewrite, env.registry
    book_id = _seed(store)
    payload = client.get(f"/api/novel/books/{book_id}/rewrite/snapshot").json()
    assert payload["ok"] is True
    snapshot = payload["snapshot"]
    assert snapshot["book_version"] == store.get_book(book_id).version
    assert "state_sha256" in snapshot
    assert "chapters" in snapshot


def test_disclaimer_endpoint_is_single_source(env: Env) -> None:
    """★免责声明物理单点★：端点返回的 text 必须与 `DISCLAIMER_TEXT` 恒等。

    主理人裁定后的硬约束：前端不得在任何分支写自己的免责措辞，
    因此这条断言是「单点来源」这条合规要求的**可执行判据** ——
    一旦有人另起一份文案又没同步，这条会先红。
    """
    book_id = _seed(env.store)
    payload = env.client.get(f"/api/novel/books/{book_id}/rewrite/disclaimer").json()
    assert payload["ok"] is True
    assert payload["disclaimer"]["text"] == DISCLAIMER_TEXT
    assert payload["disclaimer"]["version"] == DISCLAIMER_VERSION
    assert payload["honesty_note"] == PRECHECK_HONESTY_NOTE


def test_disclaimer_endpoint_matches_report_disclaimer(env: Env) -> None:
    """同一份声明：端点下发值 == 落在报告里的 `disclaimer`（同一来源两份出口）。"""
    book_id = _seed(env.store)
    endpoint = env.client.get(f"/api/novel/books/{book_id}/rewrite/disclaimer").json()
    report = env.rewrite.save_report(
        book_id,
        build_report(
            rewrite_id=make_rewrite_job_id(book_id),
            blueprint=env.rewrite.load_blueprint(book_id),
            kind="chapter",
            draft_text="临海船行的账房里，算盘声停了。",
        ),
    )
    assert report.disclaimer.text == endpoint["disclaimer"]["text"]
    assert report.disclaimer.version == endpoint["disclaimer"]["version"]


def test_rewrite_domain_dir_layout(env: Env) -> None:
    """`rewrite/` 落在 `books/<id>/rewrite/`（且**没有** `runs/` —— A6 裁定）。"""
    store, rewrite = env.store, env.rewrite
    book_id = _seed(store)
    rewrite.write_rewrite_draft(book_id, make_rewrite_job_id(book_id), "chapter", "x")
    assert rewrite.rewrite_dir(book_id) == store.book_dir(book_id) / REWRITE_DIR
    assert not (rewrite.rewrite_dir(book_id) / "runs").exists()
