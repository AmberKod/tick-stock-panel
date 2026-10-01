"""小说工作区 — AI 任务状态机（四步 checkpoint + asyncio 编排 + 协作式取消）。

设计依据：`deliverables/novel-workspace/ARCHITECTURE.md` §1.3 / §1.4 / §3.5。

要点：
- **状态权威在磁盘**：每步完成/失败立即原子写 `checkpoints/<job_id>.json`；
  内存只持有 `dict[job_id, asyncio.Task]`（用于取消）与 `asyncio.Semaphore`。
  进程重启 / 页面刷新后 `GET /jobs/{id}` 一律从磁盘读。
- **纯 asyncio**：`ai_provider.generate_ai_text` 在两条 provider 分支上都不阻塞
  事件循环（OpenAI 分支是原生协程；Codex 分支内部已 `asyncio.to_thread`），
  因此不需要 `run_in_executor`，且能保留协作式取消语义。
- **并发上限 2**：AI 网关是共享外部配额，`ai_provider` 无内置限流。
- **第 4 步 ingest 只算计划**：绝不写 `state.json`（A1 裁定，保护 P0-6④）。
- **取消是协作式的**：置取消标记 + `task.cancel()`，当前步骤收尾后停止，
  不假装立即中断；已完成的步骤产物保留，可 `resume`。

许可证：MIT（与宿主项目一致）。本文件全部为自研实现。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services import novel_ai
from app.services.novel_store import (
    CHAPTER_STATUS_AI_DRAFT,
    ERR_INVALID_PAYLOAD,
    ERR_JOB_BUSY,
    ERR_JOB_NOT_RESUMABLE,
    ERR_SELECTION_REQUIRED,
    NovelJob,
    NovelStore,
    NovelValidationError,
    make_job_id,
    now_iso,
    parse_job_id,
)

logger = logging.getLogger(__name__)

#: 四步名称与顺序（PRD §5.4）。
STEP_NAMES: tuple[str, str, str, str] = ("context", "draft_text", "fact_snapshot", "ingest")
#: 终态集合（前端轮询到此即停）。
TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})
#: 并发上限：AI 网关是共享外部配额。
MAX_CONCURRENT_JOBS = 2

#: 模块级并发闸门（进程内唯一）。
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


def ai_semaphore() -> asyncio.Semaphore:
    """共享并发闸门（换元仿写的 `RewriteJobRegistry` 复用同一把）。

    AI 网关是**共享外部配额**。若仿写另起一把 `Semaphore(2)`，
    「1 个续写 + 1 个仿写」实际并发就是 4，会把用户 Key 打爆。
    共享后全局上限恒为 2。
    """
    return _SEMAPHORE


class NovelJobError(RuntimeError):
    """任务层异常基类。

    Attributes:
        code: 结构化错误码。
    """

    def __init__(self, message: str = "任务操作失败", code: str = ERR_JOB_BUSY) -> None:
        super().__init__(message)
        self.code = code


class NovelJobBusyError(NovelJobError):
    """同一 job 正在运行，不允许重复 resume —— HTTP 409。"""

    def __init__(self, message: str = "任务正在运行，请稍后再试") -> None:
        super().__init__(message, code=ERR_JOB_BUSY)


class NovelJobNotResumableError(NovelJobError):
    """任务已是终态且不可续跑（如 done）—— HTTP 409。"""

    def __init__(self, message: str = "任务已完成，无需重试") -> None:
        super().__init__(message, code=ERR_JOB_NOT_RESUMABLE)


class NovelNoEventLoopError(NovelJobError):
    """创建任务时没有事件循环（路由层误用 `def` 端点）。"""

    def __init__(self, message: str = "缺少事件循环") -> None:
        super().__init__(message, code="no_event_loop")


class NovelJobRegistry:
    """AI 任务注册表：create / start / get / resume / cancel。"""

    def __init__(self, store: NovelStore | None = None) -> None:
        """Args:
            store: 文件系统事实层；默认 `NovelStore()`（root=settings.data_dir/novel）。
        """
        self._store = store or NovelStore()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight: set[str] = set()
        self._cancel_requests: set[str] = set()

    @property
    def store(self) -> NovelStore:
        """当前使用的 store（测试注入临时根目录）。"""
        return self._store

    # ── 对外 API ──

    def create_job(
        self,
        book_id: str,
        chapter_id: str,
        mode: str,
        selection: str | None = None,
        skip_gate: bool = False,
    ) -> NovelJob:
        """校验 + 写前门禁 + 落初始 checkpoint + 起后台任务。

        Args:
            book_id: 书籍 id。
            chapter_id: 章节 id。
            mode: `continue` 或 `polish`。
            selection: 润色模式的选中原文。
            skip_gate: 用户显式「跳过本次门禁」。

        Returns:
            新建的 `NovelJob`（`status=queued`）。

        Raises:
            NovelNotFound: 书或章节不存在。
            novel_ai.WriteGateError: 写前门禁未通过（422 `missing_beat`）。
            NovelValidationError: mode 非法 / 润色无选区（422）。
            NovelNoEventLoopError: 当前不在事件循环中（任务无法创建）。
        """
        if mode not in ("continue", "polish"):
            # mode 不是 id，用 invalid_id 会让前端按「id 非法」去提示，语义错位
            raise NovelValidationError(
                f"mode 非法: {mode!r}（只支持 continue / polish）", code=ERR_INVALID_PAYLOAD
            )
        if mode == "polish" and not (selection or "").strip():
            raise NovelValidationError("请先选中要润色的文本", code=ERR_SELECTION_REQUIRED)

        # fail-closed：明知会失败就不要建 job（P0-8① → 503 ai_unavailable，不抛 500）
        status = novel_ai.ai_status()
        if not status.available:
            raise novel_ai.AiCallError(novel_ai.ERR_AI_UNAVAILABLE, status.reason or "AI 网关不可用")

        self._store.get_book(book_id)  # 不存在会抛 NovelNotFound
        _volume, chapter = self._store.get_chapter_node(book_id, chapter_id)
        novel_ai.check_write_gate(chapter, skip=bool(skip_gate))

        job = NovelJob(
            job_id=make_job_id(book_id),
            book_id=book_id,
            chapter_id=chapter_id,
            mode=mode,
            selection=selection,
            skip_gate=bool(skip_gate),
            created_at=now_iso(),
            updated_at=now_iso(),
            steps=[{"name": name} for name in STEP_NAMES],
            status="queued",
        )
        self._store.save_job(job)
        self._spawn(job)
        return job

    def get_job(self, job_id: str) -> NovelJob:
        """从磁盘读任务（权威在磁盘，跨刷新/重启都可读）。"""
        parse_job_id(job_id)
        return self._store.load_job(job_id)

    def resume(self, job_id: str) -> NovelJob:
        """续跑：只跑「没产物」的步骤。

        步骤状态语义（三态都不能混）：
          - `done` → `skipped`：已有产物，本次不重跑，保留 `at`。
          - `cancelled` → `pending`：被取消的那一步**没有产物**，必须重跑。
          - `failed` / `running` → `pending`：重跑。

        Raises:
            NovelJobBusyError: 任务正在运行（409）。
            NovelJobNotResumableError: 任务已完成（409）。
        """
        job = self.get_job(job_id)
        if job.job_id in self._inflight or job.status == "running":
            raise NovelJobBusyError(f"任务 {job_id} 正在运行，请稍后再试")
        if job.status == "done":
            raise NovelJobNotResumableError(f"任务 {job_id} 已完成，无需重试")

        for step in job.steps:
            if step.status == "done":
                # 已完成步骤本次不重跑 —— 保留 `at`（P0-13 验收：step1 时间戳不被改写）
                step.status = "skipped"
            elif step.status in ("failed", "running", "cancelled"):
                # `cancelled` 必须重置为 pending：被取消的那一步**没有产物**，
                # 若继续当 skipped 跳过，job 会伪报 done 而 draft_text 仍是 None。
                step.status = "pending"
        job.status = "queued"
        job.failed_step = None
        job.updated_at = now_iso()
        self._store.save_job(job)
        self._spawn(job)
        return job

    def cancel(self, job_id: str) -> NovelJob:
        """协作式取消：置取消标记 + `task.cancel()`。

        不假装立即中断：若在途的 LLM 请求正在等待，实际生效点是下一个 await
        边界（当前步骤收尾、写 checkpoint 之前）。已完成步骤的产物保留，可 resume。
        """
        job = self.get_job(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        self._cancel_requests.add(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return job

    # ── 内部 ──

    def _spawn(self, job: NovelJob) -> None:
        """创建后台任务（调用方必须处于事件循环中 —— 路由层用 `async def`）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise NovelNoEventLoopError(
                "创建 AI 任务需要事件循环：路由层必须用 async def 端点"
            ) from exc
        task = loop.create_task(self._run(job))
        self._tasks[job.job_id] = task
        self._inflight.add(job.job_id)

    async def _run(self, job: NovelJob) -> None:
        """四步状态机：逐步执行，每步前后原子写 checkpoint。"""
        try:
            async with _SEMAPHORE:
                for index, name in enumerate(STEP_NAMES):
                    step = job.steps[index]
                    if step.status in ("done", "skipped"):
                        continue
                    if job.job_id in self._cancel_requests:
                        job.status = "cancelled"
                        job.updated_at = now_iso()
                        self._store.save_job(job)
                        return

                    step.status = "running"
                    step.at = now_iso()
                    step.error = None
                    job.status = "running"
                    job.updated_at = now_iso()
                    self._store.save_job(job)

                    try:
                        await self._run_step(job, name)
                    except asyncio.CancelledError:
                        # 用 `cancelled` 而不是 `skipped`：这一步**没有产物**，
                        # resume 时必须重跑（skipped 的语义是「本次不重跑」）。
                        step.status = "cancelled"
                        step.error = "已取消（等待当前步骤收尾后停止）"
                        job.status = "cancelled"
                        job.updated_at = now_iso()
                        self._store.save_job(job)
                        raise
                    except Exception as exc:  # 任何失败都要落盘为 step 失败
                        step.status = "failed"
                        step.at = now_iso()
                        step.error = str(exc)[:500]
                        job.failed_step = name
                        job.status = "failed"
                        job.updated_at = now_iso()
                        self._store.save_job(job)
                        logger.warning("novel job %s step %s failed: %s", job.job_id, name, exc)
                        return

                    step.status = "done"
                    step.at = now_iso()
                    job.updated_at = now_iso()
                    self._store.save_job(job)

                if job.job_id in self._cancel_requests:
                    job.status = "cancelled"
                else:
                    job.status = "done"
                    job.failed_step = None
                job.updated_at = now_iso()
                self._store.save_job(job)
        except asyncio.CancelledError:
            if job.status not in TERMINAL_JOB_STATUSES:
                job.status = "cancelled"
                job.updated_at = now_iso()
                self._store.save_job(job)
            raise
        finally:
            self._tasks.pop(job.job_id, None)
            self._inflight.discard(job.job_id)
            self._cancel_requests.discard(job.job_id)

    async def _run_step(self, job: NovelJob, name: str) -> None:
        """执行单步。

        Raises:
            novel_ai.AiCallError: AI 调用失败。
            novel_ai.FactParseError: 事实快照解析失败。
            NovelJobError: 未知步骤。
        """
        store = self._store
        if name == "context":
            job.artifacts.context_card_md = novel_ai.build_context_card(
                store, job.book_id, job.chapter_id
            )
            return

        if name == "draft_text":
            book = store.get_book(job.book_id)
            state = store.get_state(job.book_id)
            _volume, chapter = store.get_chapter_node(job.book_id, job.chapter_id)
            card = job.artifacts.context_card_md or novel_ai.build_context_card(
                store, job.book_id, job.chapter_id
            )
            messages = novel_ai.assemble_messages(
                book, state, chapter, job.mode, job.selection, card
            )
            text = await novel_ai.generate_draft(messages, max_tokens=None)
            job.artifacts.draft_id = store.write_draft(
                job.book_id, job.chapter_id, job.mode, text
            )
            job.artifacts.draft_text = text
            store.set_chapter_status(job.book_id, job.chapter_id, CHAPTER_STATUS_AI_DRAFT)
            return

        if name == "fact_snapshot":
            book = store.get_book(job.book_id)
            _volume, chapter = store.get_chapter_node(job.book_id, job.chapter_id)
            fact = await novel_ai.generate_fact_snapshot(
                chapter, job.artifacts.draft_text or "", book
            )
            job.artifacts.fact_json = fact.model_dump(mode="json", by_alias=True)
            return

        if name == "ingest":
            # A1：只计算摄取计划，绝不写 state.json。
            job.artifacts.ingest_plan = self._build_ingest_plan(job)
            return

        raise NovelJobError(f"未知步骤: {name}", code=ERR_JOB_BUSY)

    def _build_ingest_plan(self, job: NovelJob) -> dict[str, Any]:
        """由事实快照计算摄取计划（纯本地规则，不调用 AI）。"""
        fact = job.artifacts.fact_json or {}
        state = self._store.get_state(job.book_id)
        _volume, chapter = self._store.get_chapter_node(job.book_id, job.chapter_id)

        chars = [str(item) for item in (fact.get("chars") or [])]
        state_changes = [str(item) for item in (fact.get("state_changes") or [])]
        planted = [str(item) for item in (fact.get("planted") or [])]
        resolved = [str(item) for item in (fact.get("resolved") or [])]
        relations = [item for item in (fact.get("relations") or []) if isinstance(item, dict)]

        new_characters = [name for name in chars if name and name not in state.characters]
        summary_parts = state_changes[:3] or planted[:2] or chars[:2]
        patch = f"{chapter.id} {chapter.title or ''}".strip()
        if summary_parts:
            patch = f"{patch}：{'；'.join(summary_parts)}"

        return {
            "chapter_id": job.chapter_id,
            "chapter_title": chapter.title,
            "new_characters": new_characters,
            "state_changes": state_changes,
            "planted": planted,
            "resolved": resolved,
            "relations": relations,
            "rolling_summary_patch": patch,
            "summary_patch_source": "local_rule",
            "adopt_required": True,
            "note": "本步骤只计算摄取计划，未写入 state.json；点「采纳」后才会真正写入追踪态。",
        }


def novel_store_make_job_id(book_id: str) -> str:
    """生成 job_id（转发到 novel_store，避免本模块重复实现）。"""
    from app.services.novel_store import make_job_id

    return make_job_id(book_id)


_REGISTRY: NovelJobRegistry | None = None


def shared_novel_job_registry() -> NovelJobRegistry:
    """模块级单例（进程内唯一，持有 semaphore 与 task 表）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = NovelJobRegistry()
    return _REGISTRY
