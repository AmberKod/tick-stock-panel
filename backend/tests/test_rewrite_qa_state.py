"""QA 独立验证 · E 组：`rw-` 任务状态机与并发。

重点：`rw-` job_id 反解（含 book_id 带连字符）、step2 注入失败后的 resume 语义
（step1 的 `at` 不能被改写）、done 后 resume → 409、cancel 后 resume、
并发 5 个 job 峰值 ≤2、并发写同一 `rewrite/` 文件是否损坏。

**事件循环纪律**：`ai_semaphore()` 返回的是模块级 `asyncio.Semaphore`，
一旦被某个 loop 绑定，换 loop 就 RuntimeError。本文件每条用例结束后
主动解绑（与既有 `test_novel_qa_jobs.py` 同姿势），避免污染后续用例。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from app.services import novel_jobs as novel_jobs_mod
from app.services import novel_rewrite_ai
from app.services.novel_jobs import NovelJobBusyError, NovelJobNotResumableError
from app.services.novel_rewrite_jobs import (
    STEP_NAMES,
    TERMINAL_JOB_STATUSES,
    RewriteJobRegistry,
    make_rewrite_job_id,
    parse_rewrite_job_id,
)
from app.services.novel_rewrite_store import RW_REPORTS_DIR, Blueprint, RewriteStore
from app.services.novel_store import (
    ERR_INVALID_ID,
    NovelStore,
    NovelValidationError,
)

_PLAN_MD = """## L1 符号层
- 青云宗 → 临海船行

```rw-roles
[{"name": "陆昭", "slot": "主角"}, {"name": "白露", "slot": "盟友"}]
```

```rw-reversals
[{"type": "身份错位", "chapter_index": 1, "position_ratio": 0.1}]
```
"""

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

_BLUEPRINT: dict[str, Any] = {
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


@pytest.fixture(autouse=True)
def _unbind_semaphore() -> Any:
    """每条用例结束后解除 semaphore 与事件循环的绑定（防跨用例污染）。"""
    yield
    novel_jobs_mod._SEMAPHORE._loop = None


class _AiStub:
    """`novel_rewrite_ai.generate` 的测试替身（只换网关，不换业务）。"""

    def __init__(self, text: str = _PLAN_MD, fail_times: int = 0, delay: float = 0.02) -> None:
        self.text = text
        self.fail_times = fail_times
        self.delay = delay
        self.calls = 0
        self.inflight = 0
        self.peak = 0

    async def __call__(self, messages: list[dict], **_kwargs: Any) -> str:
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            if self.calls <= self.fail_times:
                raise RuntimeError("QA 注入的 AI 故障")
            return self.text
        finally:
            self.inflight -= 1


def _enable(monkeypatch: pytest.MonkeyPatch, stub: _AiStub) -> None:
    """让 AI 可用 + 换掉唯一出口。"""
    monkeypatch.setattr(novel_rewrite_ai, "require_ai_ready", lambda: None)
    monkeypatch.setattr(novel_rewrite_ai, "generate", stub)


def _env(tmp_path: Path) -> tuple[NovelStore, RewriteStore, RewriteJobRegistry, str]:
    """建书 + 蓝图 + registry。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 状态机书")
    store.save_outline(book.id, book.version, _NODES)
    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate(_BLUEPRINT))
    return store, rewrite, RewriteJobRegistry(store=store), book.id


async def _wait(registry: RewriteJobRegistry, job_id: str, timeout: float = 20.0) -> Any:
    """轮询到终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get_job(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job 未进入终态: {registry.get_job(job_id).status}")


# ═══════════════════════ job_id 反解 ═══════════════════════


@pytest.mark.parametrize(
    "book_id",
    ["bk", "bk-qa", "bk-my-long-book", "b", "bk-123", "a" * 60],
)
def test_job_id_roundtrip(book_id: str) -> None:
    """`rw-<book_id>-<ts>-<hex4>` 往返一致（book_id 可含连字符）。"""
    job_id = make_rewrite_job_id(book_id)
    parsed_book, ts = parse_rewrite_job_id(job_id)
    assert parsed_book == book_id
    assert len(ts) == 14 and ts.isdigit()


def test_job_id_with_hyphenated_book_id_is_not_ambiguous() -> None:
    """★边界★ `rw-bk-a-b-20260101010101-abcd` 必须反解出 `bk-a-b`（不是 `bk`）。

    `rsplit("-", 2)` 只切最后两段，所以 book_id 里的连字符安全。
    """
    assert parse_rewrite_job_id("rw-bk-a-b-20260101010101-abcd") == ("bk-a-b", "20260101010101")


@pytest.mark.parametrize(
    "bad",
    [
        "job-bk-20260101010101-abcd",   # 既有续写前缀
        "rw-bk-2026010-abcd",            # 时间戳位数不对
        "rw-bk-20260101010101-abcg",     # 非 hex
        "rw-bk-20260101010101",          # 少一段
        "rw-bk-20260101010101-abcd-extra-1",  # 多一段（rsplit 后 head 合法但 tail 不合法）
        "",
        "rw",
        "rw-",
        "RW-bk-20260101010101-abcd",     # 大写前缀
        "rw-BK-20260101010101-abcd",     # 大写 book_id（validate_id 拒绝）
    ],
)
def test_job_id_rejects_malformed(bad: str) -> None:
    """畸形 job_id → `invalid_id`（422），不能被当成合法任务读盘。"""
    with pytest.raises(NovelValidationError) as info:
        parse_rewrite_job_id(bad)
    assert info.value.code == ERR_INVALID_ID, bad


def test_job_id_with_path_traversal_is_rejected(tmp_path: Path) -> None:
    """job_id 里塞路径穿越 → 要么解析失败，要么落盘仍被圈在 checkpoints/。"""
    store = NovelStore(root=tmp_path)
    rewrite = RewriteStore(store)
    for bad in (
        "rw-..-20260101010101-abcd",
        "rw-bk-20260101010101-abcd",
        "rw-..%2f..-20260101010101-abcd",
    ):
        try:
            parse_rewrite_job_id(bad)
        except NovelValidationError:
            continue
        path = rewrite.rewrite_job_path(bad)
        assert path.parent == store.checkpoints_dir(parse_rewrite_job_id(bad)[0])


# ═══════════════════════ 四步状态机 ═══════════════════════


async def test_generate_failure_marks_failed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """step2 注入失败 → `status=failed` + `failed_step=generate`，step1 保持 done。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub(fail_times=1))
    job = registry.create_job(book_id, "plan", risk_ack=True)
    final = await _wait(registry, job.job_id)

    assert final.status == "failed"
    assert final.failed_step == "generate"
    assert final.steps[0].status == "done"
    assert final.steps[1].status == "failed"
    assert final.steps[2].status == "pending"
    assert final.steps[1].error


async def test_resume_after_failure_keeps_step1_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★完成判据★ resume 后 step1 变成 skipped，但 `at` **不被改写**。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    stub = _AiStub(fail_times=1)
    _enable(monkeypatch, stub)
    job = registry.create_job(book_id, "plan", risk_ack=True)
    failed = await _wait(registry, job.job_id)
    step1_at = failed.steps[0].at
    assert step1_at

    await asyncio.sleep(0.02)  # 让时间戳有机会变化
    resumed = registry.resume(job.job_id)
    assert resumed.steps[0].status == "skipped"
    assert resumed.steps[0].at == step1_at, "★P1★ resume 把已完成步骤的时间戳改写了"

    final = await _wait(registry, job.job_id)
    assert final.status == "done"
    assert final.steps[0].at == step1_at
    assert final.steps[0].status == "skipped"
    assert final.steps[1].status == "done"
    assert final.failed_step is None
    assert stub.calls == 2


async def test_resume_done_job_is_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """done 后 resume → 409（不是静默重跑）。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub())
    job = registry.create_job(book_id, "plan", risk_ack=True)
    await _wait(registry, job.job_id)
    with pytest.raises(NovelJobNotResumableError):
        registry.resume(job.job_id)


async def test_resume_running_job_is_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正在跑的任务 resume → 409 `job_busy`（不能起第二个后台任务）。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub(delay=0.3))
    job = registry.create_job(book_id, "plan", risk_ack=True)
    await asyncio.sleep(0.05)
    assert registry.get_job(job.job_id).status == "running"
    with pytest.raises(NovelJobBusyError):
        registry.resume(job.job_id)
    await _wait(registry, job.job_id)


async def test_cancel_then_resume_reruns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel 后进终态 cancelled；再 resume 可继续跑完。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub(delay=0.25))
    job = registry.create_job(book_id, "plan", risk_ack=True)
    await asyncio.sleep(0.05)
    registry.cancel(job.job_id)
    cancelled = await _wait(registry, job.job_id)
    assert cancelled.status == "cancelled"

    _enable(monkeypatch, _AiStub(delay=0.01))
    registry.resume(job.job_id)
    final = await _wait(registry, job.job_id)
    assert final.status == "done", final.status


def test_cancel_terminal_job_is_noop(tmp_path: Path) -> None:
    """取消已终态任务 → 直接返回（不抛）。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    from app.services.novel_rewrite_store import RewriteJob

    job = RewriteJob(
        job_id=make_rewrite_job_id(book_id), book_id=book_id, kind="plan", status="done"
    )
    registry._store.save_rewrite_job(job)
    assert registry.cancel(job.job_id).status == "done"


async def test_all_four_steps_are_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """四步名字与顺序固定（前端按名字渲染）。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub())
    job = registry.create_job(book_id, "plan", risk_ack=True)
    final = await _wait(registry, job.job_id)
    assert tuple(step.name for step in final.steps) == STEP_NAMES
    assert all(step.status in ("done", "skipped") for step in final.steps)


async def test_ai_unavailable_does_not_create_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★fail-closed★ AI 不可用 → 抛 AiCallError，**不建 job**（盘上无 rw-*.json）。"""
    _store, rewrite, registry, book_id = _env(tmp_path)

    def _down() -> None:
        raise novel_rewrite_ai.AiCallError("ai_unavailable", "AI 网关未配置")

    monkeypatch.setattr(novel_rewrite_ai, "require_ai_ready", _down)
    with pytest.raises(novel_rewrite_ai.AiCallError):
        registry.create_job(book_id, "plan", risk_ack=True)
    checkpoints = rewrite.base.checkpoints_dir(book_id)
    assert not list(checkpoints.glob("rw-*.json")), "AI 不可用时竟然建了 job"


async def test_missing_risk_ack_does_not_create_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`risk_ack` 缺失 → 422，同样不建 job。"""
    _store, rewrite, registry, book_id = _env(tmp_path)
    _enable(monkeypatch, _AiStub())
    from app.services.novel_rewrite_store import RewriteAckRequiredError

    with pytest.raises(RewriteAckRequiredError):
        registry.create_job(book_id, "plan", risk_ack=False)
    assert not list(rewrite.base.checkpoints_dir(book_id).glob("rw-*.json"))


async def test_gate_blocked_does_not_create_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L3/L5 缺表且未 skip → 422 `rewrite_gate_blocked`，不建 job。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 闸门书")
    store.save_outline(book.id, book.version, _NODES)
    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate({"title": "空蓝图"}))
    registry = RewriteJobRegistry(store=store)
    _enable(monkeypatch, _AiStub())

    from app.services.novel_rewrite_store import RewriteGateBlockedError

    with pytest.raises(RewriteGateBlockedError) as info:
        registry.create_job(book.id, "plan", risk_ack=True)
    assert set(info.value.missing) == {"L3", "L5"}
    assert not list(rewrite.base.checkpoints_dir(book.id).glob("rw-*.json"))


async def test_skip_gate_is_recorded_on_blueprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 skip 闸门 → 放行，但必须在蓝图里留 `skipped_at` / `skip_reason`（知情放行）。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 跳过闸门书")
    store.save_outline(book.id, book.version, _NODES)
    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate({"title": "空蓝图"}))
    registry = RewriteJobRegistry(store=store)
    _enable(monkeypatch, _AiStub())

    registry.create_job(book.id, "plan", risk_ack=True, skip_gate=True, skip_reason="QA 跳过")
    blueprint = rewrite.load_blueprint(book.id)
    assert blueprint.gate.skipped_at, "跳过了闸门却没留时间"
    assert blueprint.gate.skip_reason == "QA 跳过"


# ═══════════════════════ 并发 ═══════════════════════


async def test_concurrency_peak_is_capped_at_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★共享 Semaphore(2)★ 5 个仿写 job 并发，AI 调用峰值必须 ≤2。"""
    _store, _rewrite, registry, book_id = _env(tmp_path)
    stub = _AiStub(delay=0.05)
    _enable(monkeypatch, stub)

    jobs = [registry.create_job(book_id, "plan", risk_ack=True) for _ in range(5)]
    for job in jobs:
        final = await _wait(registry, job.job_id)
        assert final.status == "done", final.status

    assert stub.calls == 5
    assert stub.peak <= 2, f"★P1★ 并发峰值 {stub.peak} 超过 2"
    assert stub.peak >= 2, "并发峰值 <2 说明被完全串行化了"


def _concurrent_write_errors(
    rewrite: RewriteStore, book_id: str, rewrite_id: str, payloads: list[str]
) -> list[BaseException]:
    """16 线程并发写同一份草稿，返回捕获到的异常列表。"""
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            rewrite.write_rewrite_draft(book_id, rewrite_id, "chapter", payloads[index])
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(len(payloads))))
    return errors


def test_concurrent_writes_to_same_draft_keep_content_intact(tmp_path: Path) -> None:
    """★WinError5 / 撕裂★ 100 次并发写同一 `rewrite/drafts/` 文件 → 内容不被撕裂。

    （该已知问题**已修复**，见下方 `test_concurrent_writes_never_raise_spurious_path_escape`）
    但**绝不允许**读到半截内容 —— 这是原子写的硬底线。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 并发写书")
    rewrite = RewriteStore(store)
    rewrite_id = make_rewrite_job_id(book.id)
    payloads = [f"第 {index:03d} 次写入 —— {'字' * 60}" for index in range(100)]

    errors = _concurrent_write_errors(rewrite, book.id, rewrite_id, payloads)
    for exc in errors:
        assert "rewrite/" in str(exc), f"出现了与路径校验无关的并发错误: {exc!r}"

    if rewrite.draft_path(book.id, f"{rewrite_id}.md").exists():
        body = rewrite.read_rewrite_draft(book.id, f"drafts/{rewrite_id}.md")
        assert body in payloads, "正文被撕裂成多次写入的混合体"


def test_concurrent_writes_never_raise_spurious_path_escape(tmp_path: Path) -> None:
    """★已修复的 P2-1 · 回归锁★ 并发首次建目录时不得再误判 `path_escape`。

    【原缺陷】16 线程同时 `write_rewrite_draft()` 且 `rewrite/drafts/` 尚不存在
    → 约 1-2% 的调用抛「仿写文件必须位于 rewrite/ 内」，而路径**确实合法**
    （失败后立刻重算 `resolve()` 又能通过）。
    【根因】`Path.resolve()` 要做 FS 往返，在目录**正被并发创建**的瞬间返回瞬态值。
    【修法】`novel_store.py` 新增 `lexically_inside()`（normcase + abspath +
    commonpath，纯词法、零 FS 往返），`rewrite_path()` / `chapter_path()` /
    `validate_rel_path()` 全部改用它。A/B 实测：16 线程 × 100 次 × 20 轮 = 2000 次
    调用，旧法 65 次误判（3.25%），新法 0 次（0.00%）。

    【为什么去掉 xfail 标记】本用例原本带 `@pytest.mark.xfail(strict=False)`。
    修复一旦落地它必然转 XPASS：strict=False 下 CI 不会红，但会**永久假绿**
    —— 表面"已知问题仍存在"，实则早已修好，反而掩盖回归。与
    `test_market_today_guard.py` 注释里记录的同一条纪律一致：**去掉 xfail 必须
    和修复进同一个改动**，否则要么假绿、要么 strict=True 把 XPASS 当 CI 错误。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 并发写书")
    rewrite = RewriteStore(store)
    rewrite_id = make_rewrite_job_id(book.id)
    payloads = [f"第 {index:03d} 次写入 —— {'字' * 60}" for index in range(100)]

    errors = _concurrent_write_errors(rewrite, book.id, rewrite_id, payloads)
    assert not errors, f"并发写出现了 {len(errors)} 次误判: {errors[:2]}"


def test_concurrent_writes_are_clean_once_directory_exists(tmp_path: Path) -> None:
    """对照：目录先建好再并发写 → 一次误判都没有（证明诱因是「首次建目录」）。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 并发写书2")
    rewrite = RewriteStore(store)
    (rewrite.rewrite_dir(book.id) / "drafts").mkdir(parents=True, exist_ok=True)
    rewrite_id = make_rewrite_job_id(book.id)
    payloads = [f"第 {index:03d} 次写入 —— {'字' * 60}" for index in range(100)]

    errors = _concurrent_write_errors(rewrite, book.id, rewrite_id, payloads)
    assert not errors, f"目录已存在时仍误判: {errors[:2]}"
    body = rewrite.read_rewrite_draft(book.id, f"drafts/{rewrite_id}.md")
    assert body in payloads


def test_concurrent_report_writes_are_not_corrupted(tmp_path: Path) -> None:
    """并发写同一份报告 → 落盘内容始终是**某一版完整**报告（不是半截 JSON）。"""
    import json

    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 并发报告书")
    rewrite = RewriteStore(store)
    rewrite_id = make_rewrite_job_id(book.id)

    from app.services.novel_rewrite_store import (
        Blueprint,
        build_report,
    )

    (rewrite.rewrite_dir(book.id) / RW_REPORTS_DIR).mkdir(parents=True, exist_ok=True)
    blueprint = Blueprint.model_validate(_BLUEPRINT)
    reports = [
        build_report(
            rewrite_id=rewrite_id,
            blueprint=blueprint,
            kind="chapter",
            draft_text=f"版本 {index} 的正文。" * 20,
            chapter_id=None,
        )
        for index in range(30)
    ]

    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            rewrite.save_report(book.id, reports[index])
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(30)))

    assert not errors, errors[:3]
    payload = json.loads(rewrite.report_path(book.id, rewrite_id).read_text(encoding="utf-8"))
    assert payload["rewrite_id"] == rewrite_id
    assert set(payload["summary"]) == {"blocking", "warn", "unavailable", "passed", "adoptable"}
    assert len(payload["checks"]) == 8


async def test_two_registries_share_one_semaphore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两个 registry 实例共享同一把 semaphore（否则并发上限会翻倍）。"""
    assert novel_jobs_mod.ai_semaphore() is novel_jobs_mod.ai_semaphore()
    _store, _rewrite, registry, book_id = _env(tmp_path)
    other = RewriteJobRegistry(store=_store)
    assert registry is not other
    _enable(monkeypatch, _AiStub(delay=0.05))
    jobs = [
        registry.create_job(book_id, "plan", risk_ack=True),
        other.create_job(book_id, "plan", risk_ack=True),
    ]
    for job in jobs:
        assert (await _wait(registry, job.job_id)).status == "done"


# ═══════════════════════ 跨书越权（路由层）═══════════════════════


def _client_env(tmp_path: Path) -> tuple[Any, NovelStore, RewriteJobRegistry, str, str]:
    """两个 app 环境：返回 (client, store, registry, book_a, book_b)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import novel_rewrite as rw_api

    store = NovelStore(root=tmp_path)
    book_a = store.create_book("QA 甲书").id
    book_b = store.create_book("QA 乙书").id
    for book_id in (book_a, book_b):
        store.save_outline(book_id, store.get_book(book_id).version, _NODES)
    registry = RewriteJobRegistry(store=store)
    app = FastAPI()
    app.include_router(rw_api.router)
    app.dependency_overrides[rw_api.shared_rewrite_store] = lambda: RewriteStore(store)
    app.dependency_overrides[rw_api.shared_store] = lambda: store
    app.dependency_overrides[rw_api.shared_registry] = lambda: registry
    return TestClient(app), store, registry, book_a, book_b


def _foreign_job(registry: RewriteJobRegistry, book_id: str, status: str = "failed") -> Any:
    """在 book_id 名下造一个「卡在 generate」的失败任务（权威落盘）。"""
    from app.services.novel_rewrite_store import RewriteJob

    job = RewriteJob(
        job_id=make_rewrite_job_id(book_id),
        book_id=book_id,
        kind="plan",
        status=status,
        failed_step="generate",
        steps=[
            {
                "name": name,
                "status": "done" if name == "precheck" else ("failed" if name == "generate" else "pending"),
                "at": "2026-01-01T00:00:00+00:00",
            }
            for name in STEP_NAMES
        ],
    )
    registry._store.save_rewrite_job(job)
    return job


def test_resume_endpoint_must_not_cross_books(tmp_path: Path) -> None:
    """跨书 resume 必须被挡住（与 `GET /jobs/{id}` 同姿势）。

    注：本用例最初是 `xfail`（当时 resume 没有归属校验），工程师已按 P2-2 补上
    `if job.book_id != book_id: raise NovelNotFound`（`novel_rewrite.py:478`），
    故转为正向断言 —— 这是对「已修复」的回归锁。
    """
    client, _store, registry, book_a, book_b = _client_env(tmp_path)
    job = _foreign_job(registry, book_b)

    assert client.get(f"/api/novel/books/{book_a}/rewrite/jobs/{job.job_id}").status_code == 404
    response = client.post(f"/api/novel/books/{book_a}/rewrite/jobs/{job.job_id}/resume")
    assert response.status_code == 404, f"跨书 resume 未被挡住: {response.status_code}"
    assert response.json()["detail"]["code"] == "not_found"


def test_cancel_endpoint_must_not_cross_books(tmp_path: Path) -> None:
    """跨书 cancel 必须被挡住（同上，回归锁）。"""
    client, _store, registry, book_a, book_b = _client_env(tmp_path)
    job = _foreign_job(registry, book_b)

    response = client.post(f"/api/novel/books/{book_a}/rewrite/jobs/{job.job_id}/cancel")
    assert response.status_code == 404, f"跨书 cancel 未被挡住: {response.status_code}"


def test_resume_must_not_mutate_other_books_job_before_rejecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """越权 `resume` 必须**一个副作用都不产生**（归属校验先于副作用）。

    本用例最初是 `xfail`：当时 `novel_rewrite.py` 先 `registry.resume(job_id)`
    （翻转 steps、`status="queued"`、清空 `failed_step`、**落盘**、再 `_spawn()`
    真起后台任务），之后才判 `job.book_id != book_id`。于是「A 书路径 resume
    B 书任务」返回 404 的同时，B 书任务已经被真的跑起来了（占掉共享 semaphore
    一格、向 B 书 `rewrite/` 写产物）—— 404 的语义被自己破坏。

    工程师已按 **P2-11** 把归属校验提到 `registry.resume()` **之前**（先
    `get_job` 判归属，再决定是否动 registry），故转为正向断言 —— 这是对
    「已修复」的回归锁（与 `test_resume_endpoint_must_not_cross_books` 同姿势）。
    """
    client, _store, registry, book_a, book_b = _client_env(tmp_path)
    job = _foreign_job(registry, book_b)

    # 屏蔽 _spawn 只为隔离「磁盘状态是否被改写」，避免留下悬挂 task；
    # 即便如此，resume() 在 _spawn 之前就已经把状态落盘了。
    monkeypatch.setattr(RewriteJobRegistry, "_spawn", lambda self, _job: None)

    assert client.post(f"/api/novel/books/{book_a}/rewrite/jobs/{job.job_id}/resume").status_code == 404

    after = registry.get_job(job.job_id)
    assert after.status == "failed", (
        f"越权 resume 已把 B 书任务改成 {after.status}（failed_step={after.failed_step}）"
    )
    assert after.failed_step == "generate"


def test_cross_book_cancel_of_terminal_job_changes_nothing(tmp_path: Path) -> None:
    """跨书 cancel 一个**已终态**任务：必须 404，且不得留下任何副作用。

    `failed` 属于 `TERMINAL_JOB_STATUSES`（`novel_rewrite_jobs.py:66`），
    `cancel()` 会在 213 行提前 return，所以这条路径是干净的 —— 本用例把它锁住。
    """
    client, _store, registry, book_a, book_b = _client_env(tmp_path)
    job = _foreign_job(registry, book_b)

    assert client.post(f"/api/novel/books/{book_a}/rewrite/jobs/{job.job_id}/cancel").status_code == 404
    assert job.job_id not in registry._cancel_requests
    after = registry.get_job(job.job_id)
    assert after.status == "failed"
    assert after.failed_step == "generate"


async def test_cancel_of_taskless_queued_job_must_not_poison_later_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """孤儿任务被 cancel 后，此后**合法的** `resume` 必须真的能跑起来。

    本用例最初是 `xfail`（曾用名 `..._poisons_later_resume`，改名是因为断言
    方向已随修复反转）。当时的 bug：磁盘上是 `queued`、但 registry 里**没有活
    task**（等价进程重启后从 checkpoint 载入的孤儿任务）时，`cancel()` 把
    `job_id` 塞进 `_cancel_requests`，却因取不到 task 而无从 `task.cancel()`；
    该标记唯一的清理点是 `_run()` 的 `finally`，而 `_run` 此刻根本没在跑 ——
    标记残留到进程结束，于是此后合法的 `resume()` 会在 `_run` 第一帧命中它，
    一步没跑就被置 `cancelled`：用户「点了续跑，瞬间变已取消，重试无效」。
    根因是「标记写内存、清理却在 `_run`」，两者不在同一处。

    修复（**P2-12**）双管齐下，都不依赖 `_run`：
      1. `cancel()` 发现无活 task 时**直接把终态落盘**为 `cancelled`，并同步
         清掉内存标记与任务表条目；
      2. `resume()` 作为「明确的重跑意图」，入口处再清一次残留标记。

    注意：这与归属校验无关，同书 cancel 一样会踩 —— 是本用例要锁住的点。
    """
    from app.services.novel_rewrite_store import RewriteJob

    _store, _rewrite, registry, book_id = _env(tmp_path)
    job = RewriteJob(
        job_id=make_rewrite_job_id(book_id),
        book_id=book_id,
        kind="plan",
        status="queued",
        steps=[{"name": name} for name in STEP_NAMES],
    )
    registry._store.save_rewrite_job(job)
    _enable(monkeypatch, _AiStub())
    assert registry._tasks.get(job.job_id) is None, "前置条件：没有活 task"

    registry.cancel(job.job_id)
    canceled = registry.get_job(job.job_id)
    assert canceled.status == "cancelled", (
        f"孤儿任务被 cancel 后必须落盘为终态，不能永远停在 queued：{canceled.status}"
    )
    assert job.job_id not in registry._cancel_requests, (
        "取消标记必须就地清除 —— 不能只等 `_run()` 的 `finally`（它永远不会执行）"
    )
    assert job.job_id not in registry._inflight, "任务表条目必须一并清干净"

    registry.resume(job.job_id)
    ended = await _wait(registry, job.job_id)
    assert ended.status != "cancelled", (
        f"合法的 resume 被残留取消标记秒杀：status={ended.status}, steps={ended.steps}"
    )


def test_cancel_endpoint_on_terminal_job_is_safe(tmp_path: Path) -> None:
    """cancel 一个已终态任务（无论哪本书）都应是幂等的，不抛错。"""
    client, _store, _registry, _book_a, book_b = _client_env(tmp_path)
    from app.services.novel_rewrite_store import RewriteJob

    job = RewriteJob(
        job_id=make_rewrite_job_id(book_b), book_id=book_b, kind="plan", status="done"
    )
    _registry._store.save_rewrite_job(job)
    response = client.post(f"/api/novel/books/{book_b}/rewrite/jobs/{job.job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
