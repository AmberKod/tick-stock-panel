"""QA 独立补充测试（三）：任务状态机 / AI 快照 / 并发 / 错误码。

由 QA（严过关）编写。刻意打工程师可能没覆盖的角度：

- 无 AI：`/status` 必须 200 + available=false；`/ai/draft` 必须 503 + ai_unavailable（不 500、不 mock）
- 状态机：running → resume 409 job_busy；done → resume 409 job_not_resumable；
  **cancel → resume 必须重跑被取消的那一步**
- 断点恢复：注入第 3 步失败后 resume，第 1/2 步的 `at` 与 draft_id 不得变化，
  且不得产生第二份草稿文件
- 事实快照：AI 返回垃圾 / 空串 / 缺字段 / 带代码围栏 —— 均不得污染 `state.json`
- 并发：`Semaphore(2)` 是否真的限流；100 次并发写同一章节是否损坏
- 乐观锁：PUT /outline version 冲突 409；adopt +1 version 而 PUT /chapters 不变

自包含：无 conftest，自建 FastAPI + tmp_path 注入 store/registry。
"""
from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel as novel_api
from app.services import novel_ai
from app.services import novel_jobs as novel_jobs_mod
from app.services.novel_jobs import NovelJobRegistry
from app.services.novel_store import NovelStore

NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷",
        "order": 1,
        "children": [
            {"id": "ch-001", "type": "chapter", "title": "引子", "order": 1,
             "summary": "冷开场", "beat": "埋下黑匣子来源的伏笔"},
            {"id": "ch-002", "type": "chapter", "title": "雨夜", "order": 2,
             "summary": "雨夜追击中黑匣子被夺走", "beat": "陆昭受伤并与白露关系转冷"},
        ],
    }
]

_FACT = {
    "chars": ["陆昭", "白露"],
    "state_changes": ["陆昭从健康→轻伤住院"],
    "planted": ["星港走私案的幕后主使"],
    "resolved": [],
    "relations": [{"from": "陆昭", "to": "白露", "delta": "信任开始动摇"}],
}

_AI_DELAY = 0.35


def _build_app(tmp_path: Path) -> tuple[FastAPI, NovelStore, NovelJobRegistry]:
    store = NovelStore(root=tmp_path)
    registry = NovelJobRegistry(store=store)
    app = FastAPI()
    app.include_router(novel_api.router)
    app.dependency_overrides[novel_api.shared_store] = lambda: store
    app.dependency_overrides[novel_api.shared_registry] = lambda: registry
    return app, store, registry


def _seed(store: NovelStore) -> str:
    book = store.create_book("QA 任务书")
    store.save_outline(book.id, book.version, NODES)
    store.write_chapter(book.id, "ch-001", "AAA 待润色的句子 BBB")
    store.write_chapter(book.id, "ch-002", "雨下了整夜。")
    return book.id


def _state_bytes(store: NovelStore, book_id: str) -> bytes:
    return (store.book_dir(book_id) / "state.json").read_bytes()


def _wait_job(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
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


def _wait_status(client: TestClient, job_id: str, status: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        response = client.get(f"/api/novel/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == status:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job 未进入 {status}: {payload}")


def _fake_factory(delay: float = 0.01, fact_raw: str | None = None):
    """构造一个可控的假 AI：续写返回正文，事实快照返回指定字符串。"""

    async def _fake(messages, *, temperature=0.0, max_tokens=None, timeout=180.0):
        await asyncio.sleep(delay)
        if "【输出格式】" in messages[-1]["content"]:
            return json.dumps(_FACT) if fact_raw is None else fact_raw
        return "这是模型续写的正文，承接前文。"

    return _fake


def _enable_ai(monkeypatch: pytest.MonkeyPatch, fake) -> None:
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(novel_ai, "generate_ai_text", fake)


# ─────────────────────────── 诚实不可用 ───────────────────────────


def test_status_returns_200_with_available_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 AI Key：`/status` 必须 200 + available=false + 中文 reason（状态查询不是失败）。"""
    app, store, _registry = _build_app(tmp_path)
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    with TestClient(app) as client:
        response = client.get("/api/novel/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["code"] == "ai_unavailable"
    assert payload["reason"]
    assert payload["data_dir_abs"] == str(store.novel_root())
    assert payload["books_dir_abs"] == str(store.books_root())


def test_ai_draft_returns_503_ai_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 AI Key：`/ai/draft` 必须 503 + ai_unavailable，绝不 500、绝不返回假草稿。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    with TestClient(app) as client:
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ai_unavailable"
    assert not list(store.drafts_dir(book_id).glob("*.md"))


def test_polish_without_selection_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory())
    with TestClient(app) as client:
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "polish"}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "selection_required"


def test_unknown_mode_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P2-2：非法 mode 的错误码应是 `invalid_payload`，不是 `invalid_id`。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory())
    with TestClient(app) as client:
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "rewrite"}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_payload"


def test_missing_beat_returns_422_and_skip_gate_allows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无细纲 → 422 missing_beat；skip_gate=true 是用户知情放行，应能建 job。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    nodes = [
        {"id": "v1", "type": "volume", "title": "第一卷", "order": 1,
         "children": [{"id": "ch-002", "type": "chapter", "title": "雨夜", "order": 2}]}
    ]
    book = store.get_book(book_id)
    store.save_outline(book_id, book.version, nodes)
    _enable_ai(monkeypatch, _fake_factory())
    with TestClient(app) as client:
        blocked = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        assert blocked.status_code == 422
        assert blocked.json()["detail"]["code"] == "missing_beat"
        allowed = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft",
            json={"mode": "continue", "skip_gate": True},
        )
        assert allowed.status_code == 202


# ─────────────────────────── 状态机 ───────────────────────────


def test_resume_while_running_returns_409_job_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory(delay=_AI_DELAY))
    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        _wait_status(client, job_id, "running")
        response = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "job_busy"
        assert _wait_job(client, job_id)["status"] == "done"


def test_resume_after_done_returns_409_not_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory())
    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        job_id = created.json()["job_id"]
        assert _wait_job(client, job_id)["status"] == "done"
        response = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "job_not_resumable"


def test_cancel_then_resume_must_rerun_the_cancelled_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消发生在第 2 步进行中 → 该步被标 `skipped`（未产出）。

    `resume` 必须把「被取消的 skipped」当作未完成重跑，否则 job 会伪报 done
    而 `artifacts.draft_text` 仍是 None，第 3 步只能对空文本抽事实。
    """
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory(delay=_AI_DELAY))
    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        job_id = created.json()["job_id"]

        # 等到第 2 步真正 running 再取消，确保取消落在 draft_text 上
        deadline = time.time() + 10
        payload: dict = {}
        step2: dict = {}
        while time.time() < deadline:
            payload = client.get(f"/api/novel/jobs/{job_id}").json()
            step2 = next(s for s in payload["steps"] if s["name"] == "draft_text")
            if step2["status"] == "running":
                break
            time.sleep(0.01)
        assert step2["status"] == "running", payload

        cancelled = client.post(f"/api/novel/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        assert _wait_status(client, job_id, "cancelled")["status"] == "cancelled"

        resumed = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        final = _wait_job(client, job_id)
        step2_final = next(s for s in final["steps"] if s["name"] == "draft_text")
        assert step2_final["status"] == "done", final
        assert final["artifacts"]["draft_text"], "被取消的步骤重跑后必须真的产出草稿"
        assert final["status"] == "done"


# ─────────────────────────── 断点恢复 ───────────────────────────


def test_resume_after_step3_failure_keeps_step1_step2_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第 3 步失败后 resume：第 1/2 步的 at 与 draft_id 不变，且不产生第二份草稿。

    （工程师注入的是第 2 步；这里注入第 3 步，换个注入点独立复核 P0-13。）
    """
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    calls = {"fact": 0}

    async def fake(messages, *, temperature=0.0, max_tokens=None, timeout=180.0):
        await asyncio.sleep(0.01)
        if "【输出格式】" in messages[-1]["content"]:
            calls["fact"] += 1
            return "这不是 JSON" if calls["fact"] == 1 else json.dumps(_FACT)
        return "续写正文。"

    _enable_ai(monkeypatch, fake)
    state_before = _state_bytes(store, book_id)

    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        job_id = created.json()["job_id"]
        failed = _wait_job(client, job_id)
        assert failed["status"] == "failed"
        assert failed["failed_step"] == "fact_snapshot"
        before = {s["name"]: s["at"] for s in failed["steps"]}
        draft_id = failed["artifacts"]["draft_id"]
        draft_count = len(list(store.drafts_dir(book_id).glob("*.md")))

        resumed = client.post(f"/api/novel/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        final = _wait_job(client, job_id)
        assert final["status"] == "done", final

    after = {s["name"]: s["at"] for s in final["steps"]}
    assert after["context"] == before["context"], "第 1 步不得重跑"
    assert after["draft_text"] == before["draft_text"], "第 2 步不得重跑"
    assert final["artifacts"]["draft_id"] == draft_id
    assert len(list(store.drafts_dir(book_id).glob("*.md"))) == draft_count
    assert _state_bytes(store, book_id) == state_before


# ─────────────────────────── 事实快照 ───────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "",                                   # 空串
        "   ",                                # 纯空白
        "抱歉，我无法完成这个请求。",          # 自然语言，无 JSON
        "[1, 2, 3]",                          # 合法 JSON 但不是对象
        "null",
        '{"chars": {"a": 1}}',                # 字段类型错
        '{"relations": [{"from": 1, "to": 2}]}',  # relations 类型错
    ],
)
def test_parse_fact_snapshot_rejects_bad_payloads(raw: str) -> None:
    with pytest.raises(novel_ai.FactParseError):
        novel_ai.parse_fact_snapshot(raw)


def test_parse_fact_snapshot_accepts_fence_and_missing_fields() -> None:
    """带 ```json 围栏可解析；缺字段走默认值（不会 raise）。"""
    fenced = '```json\n{"chars": ["陆昭"], "relations": [{"from": "甲", "to": "乙", "delta": "d"}]}\n```'
    fact = novel_ai.parse_fact_snapshot(fenced)
    assert fact.chars == ["陆昭"]
    assert fact.relations[0].source == "甲" and fact.relations[0].target == "乙"
    assert fact.state_changes == [] and fact.planted == []

    empty = novel_ai.parse_fact_snapshot("{}")
    assert empty.chars == [] and empty.relations == []


def test_bad_fact_snapshot_never_touches_state_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """事实快照解析失败：state.json 字节不变、正文不变、草稿保留可重试。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory(fact_raw="这不是 JSON"))
    state_before = _state_bytes(store, book_id)
    body_before = store.read_chapter(book_id, "ch-002")[0]

    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        job_id = created.json()["job_id"]
        final = _wait_job(client, job_id)
        assert final["status"] == "failed"
        assert final["failed_step"] == "fact_snapshot"
        assert final["steps"][0]["status"] == "done"
        assert final["artifacts"]["fact_json"] is None

    assert _state_bytes(store, book_id) == state_before
    assert store.read_chapter(book_id, "ch-002")[0] == body_before
    assert list(store.drafts_dir(book_id).glob("*.md")), "草稿应保留供重试"


def test_empty_ai_output_fails_draft_step_not_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 返回空内容 = 失败，绝不当作成功（不静默空结果）。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)

    async def blank(messages, *, temperature=0.0, max_tokens=None, timeout=180.0):
        return "   "

    _enable_ai(monkeypatch, blank)
    with TestClient(app) as client:
        created = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/ai/draft", json={"mode": "continue"}
        )
        assert created.status_code == 202
        final = _wait_job(client, created.json()["job_id"])
    assert final["status"] == "failed"
    assert final["failed_step"] == "draft_text"


def test_successful_job_never_writes_state_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-6④：完整跑完 4 步（含 polish 模式）后 state.json 字节不变。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory())
    state_before = _state_bytes(store, book_id)
    body_before = store.read_chapter(book_id, "ch-001")[0]

    with TestClient(app) as client:
        for mode, extra in (("continue", {}), ("polish", {"selection": "AAA 待润色的句子"})):
            created = client.post(
                f"/api/novel/books/{book_id}/chapters/ch-001/ai/draft",
                json={"mode": mode, **extra},
            )
            assert created.status_code == 202, (mode, created.text)
            assert _wait_job(client, created.json()["job_id"])["status"] == "done"

    assert _state_bytes(store, book_id) == state_before
    assert store.read_chapter(book_id, "ch-001")[0] == body_before


# ─────────────────────────── 乐观锁 ───────────────────────────


def test_outline_version_conflict_409(tmp_path: Path) -> None:
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    with TestClient(app) as client:
        response = client.put(
            f"/api/novel/books/{book_id}/outline", json={"version": 9999, "nodes": NODES}
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "version_conflict"


def test_adopt_bumps_version_but_put_chapter_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """自动保存不能让前端缓存的 version 失效（否则刷 409）；采纳改 status 才 +1。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    _enable_ai(monkeypatch, _fake_factory())
    base = store.get_book(book_id).version

    store.write_chapter(book_id, "ch-002", "改了一次正文")
    assert store.get_book(book_id).version == base

    draft_id = store.write_draft(book_id, "ch-002", "continue", "采纳后的正文")
    with TestClient(app) as client:
        response = client.post(
            f"/api/novel/books/{book_id}/chapters/ch-002/adopt", json={"draft_id": draft_id}
        )
    assert response.status_code == 200
    assert store.get_book(book_id).version == base + 1
    assert store.read_chapter(book_id, "ch-002")[0] == "采纳后的正文"


def test_patch_state_unknown_chapter_returns_404(tmp_path: Path) -> None:
    """P2-6：手工补录的 chapter_id 必须真实存在于大纲，否则 404（不留孤儿事实）。"""
    app, store, _registry = _build_app(tmp_path)
    book_id = _seed(store)
    fact = {"chars": ["甲"], "state_changes": [], "planted": [], "resolved": [], "relations": []}
    with TestClient(app) as client:
        bad = client.patch(
            f"/api/novel/books/{book_id}/state", json={"chapter_id": "ch-999", "fact": fact}
        )
        assert bad.status_code == 404
        assert bad.json()["detail"]["code"] == "not_found"
        good = client.patch(
            f"/api/novel/books/{book_id}/state", json={"chapter_id": "ch-001", "fact": fact}
        )
        assert good.status_code == 200
    assert "ch-999" not in [item.id for item in store.get_state(book_id).chapters]


# ─────────────────────────── 并发 ───────────────────────────


async def test_semaphore_caps_concurrent_jobs_at_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Semaphore(2) 必须真的限流：5 个 job 并发，同时在跑的 AI 调用峰值应为 2。"""
    store = NovelStore(root=tmp_path)
    book_id = _seed(store)
    registry = NovelJobRegistry(store=store)

    peak = 0
    inflight = 0

    async def fake(messages, *, temperature=0.0, max_tokens=None, timeout=180.0):
        nonlocal peak, inflight
        inflight += 1
        peak = max(peak, inflight)
        try:
            await asyncio.sleep(0.05)
            if "【输出格式】" in messages[-1]["content"]:
                return json.dumps(_FACT)
            return "并发草稿。"
        finally:
            inflight -= 1

    _enable_ai(monkeypatch, fake)
    try:
        jobs = [registry.create_job(book_id, "ch-001", "continue") for _ in range(5)]
        deadline = time.time() + 30
        while time.time() < deadline:
            statuses = [registry.get_job(job.job_id).status for job in jobs]
            if all(s in ("done", "failed", "cancelled") for s in statuses):
                break
            await asyncio.sleep(0.02)
        statuses = [registry.get_job(job.job_id).status for job in jobs]
        assert statuses == ["done"] * 5, statuses
        assert peak <= 2, f"并发峰值 {peak} 超过 MAX_CONCURRENT_JOBS=2"
        assert peak >= 2, f"并发峰值 {peak} 说明闸门把任务串行化了"
    finally:
        # pytest-asyncio 每条用例新建事件循环；解除信号量与本次循环的绑定，避免污染后续用例
        novel_jobs_mod._SEMAPHORE._loop = None


def test_hundred_concurrent_writes_keep_chapter_intact(tmp_path: Path) -> None:
    """P0-1②：100 次并发写同一章节后文件不损坏，内容等于某一次完整写入。"""
    store = NovelStore(root=tmp_path)
    book_id = _seed(store)
    payloads = [f"第 {i:03d} 次写入 —— {'字' * 60}" for i in range(100)]

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            store.write_chapter(book_id, "ch-001", payloads[index])
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(100)))

    assert not errors, errors[:3]
    body = store.read_chapter(book_id, "ch-001")[0]
    assert body in payloads, "正文被撕裂成多次写入的混合体"
    assert store.get_book(book_id).title == "QA 任务书"
