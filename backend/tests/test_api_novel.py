"""小说工作区 — 路由层契约单测（自包含：自建 FastAPI + tmp_path 注入 store/registry）。

覆盖 ARCHITECTURE.md T03 完成判据：
路由契约、`/status` 语义（无 Key → 200 + available=false）、非法 id 422、
version 冲突 409、`/ai/draft` 无 Key → 503 + ai_unavailable、
第 2 步注入异常后 failed_step=draft_text 且 resume 不重跑第 1 步。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel as novel_api
from app.services import novel_ai
from app.services.novel_jobs import NovelJobRegistry
from app.services.novel_store import NovelStore

_NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷 破晓",
        "order": 1,
        "children": [
            {
                "id": "ch-001",
                "type": "chapter",
                "title": "引子",
                "order": 1,
                "summary": "冷开场：陆昭在废弃船坞发现黑匣子",
                "beat": "埋下黑匣子来源的伏笔",
            },
            {
                "id": "ch-002",
                "type": "chapter",
                "title": "雨夜",
                "order": 2,
                "summary": "雨夜追击中黑匣子被夺走",
                "beat": "陆昭受伤并与白露关系转冷",
            },
        ],
    }
]

_FACT_JSON = {
    "chars": ["陆昭", "白露"],
    "state_changes": ["陆昭从健康→轻伤住院"],
    "planted": ["星港走私案的幕后主使"],
    "resolved": [],
    "relations": [{"from": "陆昭", "to": "白露", "delta": "信任开始动摇"}],
}


def _build_app(tmp_path: Path) -> tuple[FastAPI, NovelStore, NovelJobRegistry]:
    """自建 app（不 import app.main，避免拉起完整生命周期）。"""
    store = NovelStore(root=tmp_path)
    registry = NovelJobRegistry(store=store)
    app = FastAPI()
    app.include_router(novel_api.router)
    app.dependency_overrides[novel_api.shared_store] = lambda: store
    app.dependency_overrides[novel_api.shared_registry] = lambda: registry
    return app, store, registry


def _seed(store: NovelStore) -> str:
    book = store.create_book("星海黎明")
    store.save_outline(book.id, book.version, _NODES)
    return book.id


def _wait_job(client: TestClient, job_id: str, timeout: float = 8.0) -> dict:
    """轮询到终态（前端也是这个姿势）。"""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/novel/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in ("done", "failed", "cancelled"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job 未进入终态: {payload}")


def _detail_code(response) -> str:
    return str(response.json()["detail"]["code"])


# ─────────────────────────── 路由契约 ───────────────────────────


def test_routes_registered(tmp_path: Path) -> None:
    app, _store, _registry = _build_app(tmp_path)
    paths = {route.path for route in app.routes}
    expected = {
        "/api/novel/status",
        "/api/novel/books",
        "/api/novel/books/{book_id}",
        "/api/novel/books/{book_id}/meta",
        "/api/novel/books/{book_id}/outline",
        "/api/novel/books/{book_id}/chapters",
        "/api/novel/books/{book_id}/chapters/{chapter_id}",
        "/api/novel/books/{book_id}/chapters/{chapter_id}/ai/draft",
        "/api/novel/books/{book_id}/chapters/{chapter_id}/adopt",
        "/api/novel/books/{book_id}/state",
        "/api/novel/books/{book_id}/views/{name}",
        "/api/novel/books/{book_id}/views/rebuild",
        "/api/novel/books/{book_id}/lint",
        "/api/novel/books/{book_id}/export",
        "/api/novel/jobs/{job_id}",
        "/api/novel/jobs/{job_id}/resume",
        "/api/novel/jobs/{job_id}/cancel",
    }
    assert expected <= paths


# ─────────────────────────── /status ───────────────────────────


def test_status_returns_200_even_when_ai_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状态查询本身不是失败：无 Key 也返回 200 + available=false。"""
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/novel/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["available"] is False
        assert payload["code"] == "ai_unavailable"
        assert payload["reason"]
        assert payload["data_dir_abs"] == str(store.novel_root())


# ─────────────────────────── 书架 / 大纲 / 章节 ───────────────────────────


def test_books_crud(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/novel/books", json={"title": "星海黎明"})
        assert created.status_code == 200
        book_id = created.json()["id"]

        listed = client.get("/api/novel/books")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["books"]] == [book_id]

        renamed = client.patch(f"/api/novel/books/{book_id}", json={"title": "星海黎明 Ⅱ"})
        assert renamed.json()["title"] == "星海黎明 Ⅱ"

        deleted = client.delete(f"/api/novel/books/{book_id}")
        assert deleted.json() == {"ok": True}
        assert not store.book_dir(book_id).exists()


def test_book_meta_read_then_patch_keeps_untouched_fields(tmp_path: Path) -> None:
    """`GET .../meta` 读到的值 = 刚写入的值；只传 setting_summary 时 title 不变。"""
    app, _store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = client.post("/api/novel/books", json={"title": "星海黎明"}).json()["id"]

        meta = client.get(f"/api/novel/books/{book_id}/meta")
        assert meta.status_code == 200
        payload = meta.json()
        assert payload["title"] == "星海黎明"
        assert payload["setting_summary"] == ""
        assert "outline" not in payload  # meta 不含大纲树
        assert payload["chapter_count"] == 1

        patched = client.patch(
            f"/api/novel/books/{book_id}",
            json={"setting_summary": "星历 312 年，人类退守星环带。", "genre": "太空歌剧"},
        )
        assert patched.status_code == 200
        body = patched.json()
        # 未传的字段保持原值，不被清空
        assert body["title"] == "星海黎明"
        assert body["author"] == ""
        assert body["pov"] == ""
        assert body["tense"] == ""
        # 传了的字段确实写进去了
        assert body["setting_summary"] == "星历 312 年，人类退守星环带。"
        assert body["genre"] == "太空歌剧"

        # 回读 = 刚写入的值（不是缓存里的旧值）
        again = client.get(f"/api/novel/books/{book_id}/meta").json()
        assert again["setting_summary"] == body["setting_summary"]
        assert again["genre"] == body["genre"]
        assert again["title"] == "星海黎明"


def test_book_meta_patch_rejects_empty_body_and_unknown_field(tmp_path: Path) -> None:
    """空请求体 / 非白名单字段 → 422 invalid_payload；空书名 → 422 invalid_title。"""
    app, _store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = client.post("/api/novel/books", json={"title": "星海黎明"}).json()["id"]

        empty = client.patch(f"/api/novel/books/{book_id}", json={})
        assert empty.status_code == 422
        assert _detail_code(empty) == "invalid_payload"

        # 未声明字段被 Pydantic 忽略（不静默写盘）→ 落到空体分支，仍是 422
        unknown = client.patch(f"/api/novel/books/{book_id}", json={"outline": []})
        assert unknown.status_code == 422
        assert _detail_code(unknown) == "invalid_payload"

        blank_title = client.patch(f"/api/novel/books/{book_id}", json={"title": "   "})
        assert blank_title.status_code == 422
        assert _detail_code(blank_title) == "invalid_title"

        missing = client.get("/api/novel/books/book-9999/meta")
        assert missing.status_code == 404


def test_invalid_book_id_returns_422(tmp_path: Path) -> None:
    app, _store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/novel/books/BAD_ID/outline")
        assert response.status_code == 422
        assert _detail_code(response) == "invalid_id"

        missing = client.get("/api/novel/books/book-9999/outline")
        assert missing.status_code == 404
        assert _detail_code(missing) == "not_found"


def test_outline_version_conflict_returns_409(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.put(
            f"/api/novel/books/{book_id}/outline",
            json={"version": 99, "nodes": _NODES},
        )
        assert response.status_code == 409
        assert _detail_code(response) == "version_conflict"


def test_chapter_read_write_roundtrip(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        put = client.put(
            f"/api/novel/books/{book_id}/chapters/ch-001", json={"content": "雨下了整夜。"}
        )
        assert put.status_code == 200
        assert put.json()["word_count"] == 6

        got = client.get(f"/api/novel/books/{book_id}/chapters/ch-001")
        assert got.status_code == 200
        payload = got.json()
        assert payload["content"] == "雨下了整夜。"
        assert payload["abs_path"].endswith(".md")
        assert payload["draft"] is None


# ─────────────────────────── 派生视图 / lint / 导出 ───────────────────────────


def test_views_rebuild_and_read(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        rebuilt = client.post(f"/api/novel/books/{book_id}/views/rebuild")
        assert rebuilt.status_code == 200
        assert rebuilt.json()["files"] == ["characters.md", "context-card.md", "timeline.md"]

        view = client.get(f"/api/novel/books/{book_id}/views/context-card")
        assert view.status_code == 200
        assert "续写上下文卡" in view.json()["content"]

        bad = client.get(f"/api/novel/books/{book_id}/views/not-a-view")
        assert bad.status_code == 422


def test_lint_endpoint(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.post(
            f"/api/novel/books/{book_id}/lint", json={"text": "结尾没有标点"}
        )
        assert response.status_code == 200
        assert response.json()["count"] >= 1

        bad = client.post(f"/api/novel/books/{book_id}/lint", json={})
        assert bad.status_code == 422


def test_export_endpoints(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        client.put(
            f"/api/novel/books/{book_id}/chapters/ch-001",
            json={"content": "# 引子\n\n**粗体**内容\n"},
        )
        md = client.get(
            f"/api/novel/books/{book_id}/export"
            "?format=md&scope=chapter&chapter_id=ch-001"
        )
        assert md.status_code == 200
        assert md.text == "# 引子\n\n**粗体**内容\n"

        txt = client.get(
            f"/api/novel/books/{book_id}/export"
            "?format=txt&scope=chapter&chapter_id=ch-001"
        )
        assert txt.status_code == 200
        assert "**" not in txt.text and "#" not in txt.text

        book_md = client.get(f"/api/novel/books/{book_id}/export?format=md&scope=book")
        assert book_md.status_code == 200
        assert "第一卷 破晓" in book_md.text

        bad = client.get(f"/api/novel/books/{book_id}/export?format=docx&scope=book")
        assert bad.status_code == 422


# ─────────────────────────── AI 端点 fail-closed ───────────────────────────


def test_ai_draft_returns_503_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-8①：无 AI 时 /ai/draft 返回 503 + code=ai_unavailable，不抛 500。"""
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "continue"},
        )
        assert response.status_code == 503
        assert _detail_code(response) == "ai_unavailable"


def test_ai_draft_gate_returns_422_missing_beat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-12①：后端二次校验 —— 无细纲拒绝续写（skip_gate 可显式放行）。"""
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        book = store.get_book(book_id)
        nodes = book.outline.model_dump(mode="json")["nodes"]
        nodes[0]["children"][1]["beat"] = ""
        nodes[0]["children"][1]["summary"] = ""
        client.put(
            f"/api/novel/books/{book_id}/outline",
            json={"version": book.version, "nodes": nodes},
        )

        blocked = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "continue"},
        )
        assert blocked.status_code == 422
        assert _detail_code(blocked) == "missing_beat"


def test_ai_draft_polish_requires_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "polish"},
        )
        assert response.status_code == 422
        assert _detail_code(response) == "selection_required"


# ─────────────────────────── Step 级 checkpoint 与断点恢复 ───────────────────────────


def test_step_failure_and_resume_does_not_rerun_step1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-13：第 2 步注入异常 → failed_step=draft_text；resume 不重跑第 1 步。"""
    calls = {"n": 0}

    async def fake_generate(
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("注入失败：网关 502")
        if calls["n"] == 2:
            return "雨下了整夜，星港的霓虹在水面碎成一片。"
        return json.dumps(_FACT_JSON, ensure_ascii=False)

    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(novel_ai, "generate_ai_text", fake_generate)

    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        state_path = store.book_dir(book_id) / "state.json"
        before_state = state_path.read_bytes()

        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "continue"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        failed = _wait_job(client, job_id)
        assert failed["status"] == "failed"
        assert failed["failed_step"] == "draft_text"
        assert failed["steps"][0]["status"] == "done"
        assert failed["artifacts"]["context_card_md"]
        first_at = failed["steps"][0]["at"]
        assert first_at

        # A1：草稿阶段绝不写 state.json
        assert state_path.read_bytes() == before_state

        resumed = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        done = _wait_job(client, job_id)

        assert done["status"] == "done"
        assert done["steps"][0]["status"] == "skipped", "已完成步骤本次不重跑"
        assert done["steps"][0]["at"] == first_at, "step1 的时间戳不得被改写"
        assert done["steps"][1]["status"] == "done"
        assert done["steps"][2]["status"] == "done"
        assert done["steps"][3]["status"] == "done"
        assert done["artifacts"]["draft_text"]
        assert done["artifacts"]["fact_json"]["chars"] == ["陆昭", "白露"]
        assert done["artifacts"]["ingest_plan"]["adopt_required"] is True

        # 仍未写 state.json（A1：只有 /adopt 才写）
        assert state_path.read_bytes() == before_state

        # 已完成的 job 再 resume → 409
        again = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert again.status_code == 409
        assert _detail_code(again) == "job_not_resumable"


def test_adopt_writes_state_and_rebuilds_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-10①：采纳后 state.json 新增章节事实，派生视图重建。"""
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        draft_id = store.write_draft(book_id, "ch-002", "continue", "草稿正文内容")

        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/adopt",
            json={"draft_id": draft_id, "fact": _FACT_JSON},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["chapter"]["status"] == "published"
        assert [item["id"] for item in payload["state"]["chapters"]] == ["ch-002"]
        assert payload["state"]["chapters"][0]["relations"][0]["from"] == "陆昭"
        assert payload["views_rebuilt"] == ["characters.md", "context-card.md", "timeline.md"]
        assert store.read_chapter(book_id, "ch-002")[0] == "草稿正文内容"


def test_adopt_rejects_illegal_fact(tmp_path: Path) -> None:
    """fact 非法 → 422 fact_parse_failed，且正文不变（先校验后写入）。"""
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        store.write_chapter(book_id, "ch-002", "原始正文")
        draft_id = store.write_draft(book_id, "ch-002", "continue", "草稿正文")

        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/adopt",
            json={"draft_id": draft_id, "fact": {"chars": "陆昭"}},
        )
        assert response.status_code == 422
        assert _detail_code(response) == "fact_parse_failed"
        assert store.read_chapter(book_id, "ch-002")[0] == "原始正文"


def test_manual_fact_patch(tmp_path: Path) -> None:
    """P0-10③：手工补录章节事实（不强依赖 AI）。"""
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.patch(
            f"/api/novel/books/{book_id}/state",
            json={
                "chapter_id": "ch-001",
                "fact": {
                    "chars": ["陆昭"],
                    "state_changes": ["陆昭捡到黑匣子"],
                    "planted": ["黑匣子的真实来源"],
                    "relations": [{"from": "陆昭", "to": "白露", "delta": "初次相遇"}],
                    "source": "manual",
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["chapters"][0]["source"] == "manual"

        summary = client.patch(
            f"/api/novel/books/{book_id}/state",
            json={"rolling_summary": "前情提要：陆昭捡到黑匣子后被卷入走私案。"},
        )
        assert summary.json()["rolling"]["summary"].startswith("前情提要")


# ─────────────────────────── 取消（A4） ───────────────────────────


def test_cancel_job_is_cooperative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A4：取消请求已提交 → 当前步骤收尾后停止，状态落盘为 cancelled。"""
    started = asyncio.Event()

    async def slow_generate(
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        started.set()
        await asyncio.sleep(30)
        return "永远不会返回的内容"

    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(novel_ai, "generate_ai_text", slow_generate)

    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "continue"},
        )
        job_id = created.json()["job_id"]
        assert _wait_job_status(client, job_id, {"running", "queued"}) is True

        cancelled = client.post(f"/api/novel/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200

        final = _wait_job(client, job_id, timeout=10.0)
        assert final["status"] == "cancelled"
        # 已完成步骤的产物保留，可 resume
        assert final["artifacts"]["context_card_md"]


def _wait_job_status(client: TestClient, job_id: str, statuses: set[str],
                     timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/novel/jobs/{job_id}").json()
        if payload["status"] in statuses:
            return True
        time.sleep(0.02)
    return False


def test_patch_state_rejects_unknown_chapter(tmp_path: Path) -> None:
    """手工补录的 chapter_id 必须真实存在于大纲（否则会写进一条永远显示不出来的孤儿事实）。"""
    app, store, _registry = _build_app(tmp_path)
    with TestClient(app) as client:
        book_id = _seed(store)
        response = client.patch(
            f"/api/novel/books/{book_id}/state",
            json={"chapter_id": "ch-999", "fact": {"chars": ["幽灵角色"]}},
        )
        assert response.status_code == 404
        assert _detail_code(response) == "not_found"
        assert "幽灵角色" not in store.get_state(book_id).model_dump_json()
