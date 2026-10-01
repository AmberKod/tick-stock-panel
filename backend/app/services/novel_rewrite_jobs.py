"""小说工作区 · 换元仿写 — 任务编排（`RewriteJobRegistry` 四步状态机）。

设计依据：`deliverables/novel-workspace/ARCHITECTURE-rewrite.md` §1.2-决策 2 /
§4.1 / §8.5。

四步与既有 `NovelJobRegistry` **语义完全不同**（既有是 context → draft_text →
fact_snapshot → ingest 围绕「一章正文」；仿写是 precheck → generate → evaluate →
finalize，其中 step1/step3 是**本地算法**），故独立建 registry，不复用
`_run_step` 的 if/elif（否则两个状态机互相污染）。
但 **semaphore 必须共享**（`novel_jobs.ai_semaphore()`）—— AI 网关是共享外部配额。

| Step | 做什么 | 触碰范围 |
|---|---|---|
| 1 `precheck` | 本地四规则预检 + L3/L5 闸门判据（缺表且未 skip → 本步直接 failed） | 只读 |
| 2 `generate` | 一次 AI 调用（plan / outline / chapter）→ 原子写 `rewrite/drafts/` | **只写 `rewrite/`** |
| 3 `evaluate` | **本地零依赖**：5 项自动算法 + 复用 `novel_ai.lint_text()` | 只读 |
| 4 `finalize` | 组装报告（`summary` 强制重算）→ 原子写 `rewrite/reports/` | **只写 `rewrite/`** |

**零写入保证**：本模块对 `book.json` / `state.json` / `正文/` **零写入**
（唯一的写入方法是 `RewriteStore`，而它全部经
`NovelStore.rewrite_path()`，该方法强制「必须位于 `rewrite/` 内」）。

许可证：MIT（与宿主项目一致）。本文件全部为自研实现。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.services import novel_ai, novel_rewrite_ai
from app.services.novel_jobs import (
    ERR_INVALID_PAYLOAD,
    ERR_JOB_BUSY,
    NovelJobBusyError,
    NovelJobError,
    NovelJobNotResumableError,
    NovelNoEventLoopError,
    ai_semaphore,
)
from app.services.novel_rewrite_store import (
    RewriteJob,
    RewriteSourceRejectedError,
    RewriteStore,
    build_report,
    make_rewrite_id,
    make_rewrite_job_id,
    parse_rewrite_job_id,
    precheck_blueprint,
    require_gate,
)
from app.services.novel_store import (
    NovelNotFound,
    NovelStore,
    NovelValidationError,
    now_iso,
    validate_id,
)

logger = logging.getLogger(__name__)

#: 仿写四步（与既有四步语义不同，独立定义）。
STEP_NAMES: tuple[str, str, str, str] = ("precheck", "generate", "evaluate", "finalize")
#: 终态集合（前端轮询到此即停，与既有 `TERMINAL_JOB_STATUSES` 一致）。
TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})
#: 产物类型。
KIND_PLAN = "plan"
KIND_OUTLINE = "outline"
KIND_CHAPTER = "chapter"
REWRITE_KINDS: tuple[str, str, str] = (KIND_PLAN, KIND_OUTLINE, KIND_CHAPTER)


class RewriteJobRegistry:
    """仿写任务注册表：create / get / resume / cancel（与 `NovelJobRegistry` 同构）。"""

    def __init__(self, store: NovelStore | None = None) -> None:
        """Args:
            store: 文件系统事实层；默认 `NovelStore()`（root=settings.data_dir/novel）。
        """
        self._base = store or NovelStore()
        self._store = RewriteStore(self._base)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._inflight: set[str] = set()
        self._cancel_requests: set[str] = set()

    @property
    def store(self) -> NovelStore:
        """底层 `NovelStore`（测试注入临时根目录用）。"""
        return self._base

    @property
    def rewrite_store(self) -> RewriteStore:
        """仿写域读写。"""
        return self._store

    # ── 对外 API ──

    def create_job(
        self,
        book_id: str,
        kind: str,
        *,
        chapter_id: str | None = None,
        risk_ack: bool = False,
        skip_gate: bool = False,
        skip_reason: str = "",
    ) -> RewriteJob:
        """校验 → 落初始 checkpoint → 起后台任务。

        Args:
            book_id: 书籍 id。
            kind: `plan` / `outline` / `chapter`。
            chapter_id: `kind=chapter` 时必填。
            risk_ack: 风险告知勾选（**服务端二次校验**，缺失 → 422）。
            skip_gate: 显式跳过五层硬闸门。
            skip_reason: 跳过理由（落 `blueprint.gate.skip_reason`）。

        Returns:
            新建的 `RewriteJob`（`status=queued`）。

        Raises:
            RewriteAckRequiredError: `risk_ack` 缺失（422）。
            novel_rewrite_ai.AiCallError: AI 不可用（503，**不建 job、不 mock**）。
            RewriteGateBlockedError: 闸门未过（422）。
            NovelNotFound: 书或章节不存在（404）。
        """
        validate_id(book_id, "book_id")
        if kind not in REWRITE_KINDS:
            raise NovelValidationError(
                f"kind 非法: {kind!r}（只支持 {' / '.join(REWRITE_KINDS)}）",
                code=ERR_INVALID_PAYLOAD,
            )
        if kind == KIND_CHAPTER and not chapter_id:
            raise NovelValidationError(
                "kind=chapter 时必须提供 chapter_id", code=ERR_INVALID_PAYLOAD
            )

        # T2：风险告知勾选 —— 服务端二次校验，不信前端（PRD §2.5）。
        if not risk_ack:
            from app.services.novel_rewrite_store import RewriteAckRequiredError

            raise RewriteAckRequiredError(
                "请先勾选风险告知（risk_ack=true）：本功能只对原创性作规则提示、"
                "不作任何承诺，需你自行核对质检报告并对比原作"
            )

        # fail-closed：明知会失败就不要建 job（与 novel_jobs.create_job 同姿势）
        novel_rewrite_ai.require_ai_ready()

        self._base.get_book(book_id)  # 不存在会抛 NovelNotFound
        if chapter_id:
            self._base.get_chapter_node(book_id, chapter_id)

        blueprint = self._store.load_blueprint(book_id)
        require_gate(blueprint, skip=bool(skip_gate))
        if skip_gate:
            blueprint.gate.skipped_at = now_iso()
            blueprint.gate.skip_reason = str(skip_reason or "用户显式跳过闸门")
            self._store.save_blueprint(book_id, blueprint)

        job = RewriteJob(
            job_id=make_rewrite_job_id(book_id),
            book_id=book_id,
            chapter_id=chapter_id,
            kind=kind,
            blueprint_id=blueprint.id,
            risk_ack=True,
            skip_gate=bool(skip_gate),
            created_at=now_iso(),
            updated_at=now_iso(),
            steps=[{"name": name} for name in STEP_NAMES],
            status="queued",
        )
        self._store.save_rewrite_job(job)
        self._spawn(job)
        return job

    def get_job(self, job_id: str) -> RewriteJob:
        """从磁盘读任务（权威在磁盘，跨刷新/重启都可读）。"""
        parse_rewrite_job_id(job_id)
        return self._store.load_rewrite_job(job_id)

    def resume(self, job_id: str) -> RewriteJob:
        """续跑：只跑「没产物」的步骤（`done` → `skipped` 且**保留 `at`**）。

        Raises:
            NovelJobBusyError: 任务正在运行（409）。
            NovelJobNotResumableError: 任务已完成（409）。
        """
        job = self.get_job(job_id)
        if job.job_id in self._inflight or job.status == "running":
            raise NovelJobBusyError(f"任务 {job_id} 正在运行，请稍后再试")
        if job.status == "done":
            raise NovelJobNotResumableError(f"任务 {job_id} 已完成，无需重试")

        # ★P2-12（第二道防线）★ `resume` 是用户**明确的重跑意图**，必须清掉上一次
        # `cancel()` 留在内存里的取消标记。取消标记原本是「`cancel()` 写、`_run()`
        # 的 `finally` 清」—— 两者不在同一处；孤儿任务（磁盘非终态、内存无活 task）
        # 场景下 `_run()` 从没跑过，`finally` 永不执行 ⇒ 标记残留到进程结束，
        # 于是这次**合法的**续跑会在 `_run` 第一帧被秒杀成 `cancelled`
        # （用户看到「点了续跑，瞬间变已取消，重试无效」）。
        # 第一道防线在 `cancel()`（孤儿直接落终态并同步清标记），这里再清一次，
        # 使「重跑入口」本身也不依赖任何别处的清理。
        self._cancel_requests.discard(job.job_id)

        for step in job.steps:
            if step.status == "done":
                # 已完成步骤本次不重跑 —— 保留 `at`（T02 完成判据：step1 时间戳不被改写）
                step.status = "skipped"
            elif step.status in ("failed", "running", "cancelled"):
                step.status = "pending"
        job.status = "queued"
        job.failed_step = None
        job.updated_at = now_iso()
        self._store.save_rewrite_job(job)
        self._spawn(job)
        return job

    def cancel(self, job_id: str) -> RewriteJob:
        """协作式取消：置取消标记 + `task.cancel()`（不假装立即中断）。

        ★P2-12★ **孤儿任务**（磁盘非终态、但内存里**没有活 task** —— 等价进程
        重启后从 checkpoint 载入，或 `spawn` 前进程就结束了）不再只落内存标记：

        旧写法对孤儿也会 `self._cancel_requests.add(job_id)`，但 `self._tasks.get()`
        取不到 task 就无从 `task.cancel()`；而该标记唯一的清理点是 `_run()` 的
        `finally` —— `_run` 此刻根本没在跑，`finally` **永不执行**，标记就此残留
        到进程结束。此后该任务**合法的** `resume()` 会在 `_run` 第一帧命中这个
        残留标记，一步没跑就被置 `cancelled`：用户「点了续跑，瞬间变已取消，
        重试无效」。根因是「标记写内存、清理却在 `_run`」，两者不在同一处。

        所以这里**不依赖 `_run`**：无活 task 时直接把终态落到磁盘，并把内存
        标记 / 任务表一并清干净（与 `_run` 的 `finally` 同姿势）。
        """
        job = self.get_job(job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            return job

        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            # 有活 task：交回 `_run()` 协作式收尾（它的 finally 会清标记、退 inflight）。
            self._cancel_requests.add(job_id)
            task.cancel()
            return job

        # 无活 task ⇒ `_run()` 不在跑 ⇒ 它的 finally 永不执行 ⇒ 只能自己收尾。
        job.status = "cancelled"
        job.updated_at = now_iso()
        for step in job.steps:
            # 只有「还没跑完」的步骤被标 cancelled；`done` / `skipped` 的产物保留
            # （与协作式取消的语义一致：已完成产物不回滚，且可 resume 续跑）。
            if step.status in ("pending", "running"):
                step.status = "cancelled"
        self._store.save_rewrite_job(job)
        self._cancel_requests.discard(job_id)
        self._tasks.pop(job_id, None)
        self._inflight.discard(job_id)
        return job

    # ── 内部 ──

    def _spawn(self, job: RewriteJob) -> None:
        """创建后台任务（调用方必须处于事件循环中 —— 路由层用 `async def`）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise NovelNoEventLoopError(
                "创建仿写任务需要事件循环：路由层必须用 async def 端点"
            ) from exc
        task = loop.create_task(self._run(job))
        self._tasks[job.job_id] = task
        self._inflight.add(job.job_id)

    async def _run(self, job: RewriteJob) -> None:
        """四步状态机：逐步执行，每步前后原子写 checkpoint。"""
        try:
            async with ai_semaphore():
                for index, name in enumerate(STEP_NAMES):
                    step = job.steps[index]
                    if step.status in ("done", "skipped"):
                        continue
                    if job.job_id in self._cancel_requests:
                        job.status = "cancelled"
                        job.updated_at = now_iso()
                        self._store.save_rewrite_job(job)
                        return

                    step.status = "running"
                    step.at = now_iso()
                    step.error = None
                    job.status = "running"
                    job.updated_at = now_iso()
                    self._store.save_rewrite_job(job)

                    try:
                        await self._run_step(job, name)
                    except asyncio.CancelledError:
                        step.status = "cancelled"
                        step.error = "已取消（等待当前步骤收尾后停止）"
                        job.status = "cancelled"
                        job.updated_at = now_iso()
                        self._store.save_rewrite_job(job)
                        raise
                    except Exception as exc:  # 任何失败都要落盘为 step 失败
                        step.status = "failed"
                        step.at = now_iso()
                        step.error = str(exc)[:500]
                        job.failed_step = name
                        job.status = "failed"
                        job.updated_at = now_iso()
                        self._store.save_rewrite_job(job)
                        logger.warning(
                            "rewrite job %s step %s failed: %s", job.job_id, name, exc
                        )
                        return

                    step.status = "done"
                    step.at = now_iso()
                    job.updated_at = now_iso()
                    self._store.save_rewrite_job(job)

                if job.job_id in self._cancel_requests:
                    job.status = "cancelled"
                else:
                    job.status = "done"
                    job.failed_step = None
                job.updated_at = now_iso()
                self._store.save_rewrite_job(job)
        except asyncio.CancelledError:
            if job.status not in TERMINAL_JOB_STATUSES:
                job.status = "cancelled"
                job.updated_at = now_iso()
                self._store.save_rewrite_job(job)
            raise
        finally:
            self._tasks.pop(job.job_id, None)
            self._inflight.discard(job.job_id)
            self._cancel_requests.discard(job.job_id)

    async def _run_step(self, job: RewriteJob, name: str) -> None:
        """执行单步。

        Raises:
            novel_rewrite_ai.AiCallError: AI 调用失败（→ step2 failed）。
            其他异常：落盘为对应 step 失败，已完成的步骤产物保留（可 resume）。
        """
        if name == "precheck":
            self._step_precheck(job)
            return
        if name == "generate":
            await self._step_generate(job)
            return
        if name == "evaluate":
            self._step_evaluate(job)
            return
        if name == "finalize":
            self._step_finalize(job)
            return
        raise NovelJobError(f"未知步骤: {name}", code=ERR_JOB_BUSY)

    # ── Step 1 · precheck（本地）──

    def _step_precheck(self, job: RewriteJob) -> None:
        """本地四规则预检 + 闸门判据（缺表且未 skip → **本步直接失败**，不进下一步）。"""
        blueprint = self._store.load_blueprint(job.book_id)
        hits = precheck_blueprint(blueprint)
        if hits:
            raise RewriteSourceRejectedError(
                "蓝图输入疑似原文，生成已终止（未调用 AI）。请改写为结构笔记后重试。",
                hits=hits,
            )
        job.artifacts.precheck_hits = hits
        require_gate(blueprint, skip=bool(job.skip_gate))

    # ── Step 2 · generate（唯一 AI 调用）──

    async def _step_generate(self, job: RewriteJob) -> None:
        """一次 AI 调用 → 原子写 `rewrite/drafts/`（**只写仿写域**）。"""
        blueprint = self._store.load_blueprint(job.book_id)
        rewrite_id = job.rewrite_id or make_rewrite_id(job.book_id)
        job.rewrite_id = rewrite_id

        if job.kind == KIND_PLAN:
            messages = novel_rewrite_ai.build_plan_prompt(blueprint)
            text = await novel_rewrite_ai.generate(
                messages,
                max_tokens=novel_rewrite_ai.PLAN_MAX_TOKENS,
                temperature=novel_rewrite_ai.PLAN_TEMPERATURE,
            )
            rel = self._store.write_rewrite_draft(
                job.book_id, rewrite_id, KIND_PLAN, text
            )
            job.artifacts.draft_rel = rel
            job.artifacts.draft_text = text
            job.artifacts.character_table = novel_rewrite_ai.parse_character_table(text)
            job.artifacts.reversal_table = novel_rewrite_ai.parse_reversal_table(text)
            return

        if job.kind == KIND_OUTLINE:
            plan_md = self._plan_markdown(job)
            messages = novel_rewrite_ai.build_outline_prompt(blueprint, plan_md)
            raw = await novel_rewrite_ai.generate(
                messages,
                max_tokens=novel_rewrite_ai.OUTLINE_MAX_TOKENS,
                temperature=novel_rewrite_ai.OUTLINE_TEMPERATURE,
            )
            patch = novel_rewrite_ai.parse_outline_patch(raw)
            rel = self._store.write_rewrite_draft(
                job.book_id,
                rewrite_id,
                KIND_OUTLINE,
                json.dumps(patch, ensure_ascii=False, indent=2),
            )
            job.artifacts.draft_rel = rel
            job.artifacts.outline_patch = patch
            job.artifacts.draft_text = raw
            return

        # kind == chapter
        plan_md = self._plan_markdown(job)
        _volume, chapter = self._base.get_chapter_node(
            job.book_id, job.chapter_id or ""
        )
        tail = self._chapter_tail(job)
        messages = novel_rewrite_ai.build_chapter_prompt(
            blueprint, plan_md, chapter, tail
        )
        text = await novel_rewrite_ai.generate(
            messages,
            max_tokens=novel_rewrite_ai.CHAPTER_MAX_TOKENS,
            temperature=novel_rewrite_ai.CHAPTER_TEMPERATURE,
        )
        rel = self._store.write_rewrite_draft(
            job.book_id, rewrite_id, KIND_CHAPTER, text
        )
        job.artifacts.draft_rel = rel
        job.artifacts.draft_text = text
        return

    def _plan_markdown(self, job: RewriteJob) -> str:
        """取本 job 的设定卡（plan 产物）。

        `kind=plan` 时就是本次产物本身；`outline` / `chapter` 时取
        `rewrite/drafts/` 下**最近一份** `.md`（plan 产物），没有则返回空串
        （诚实：不伪造设定卡，提示词里会写明「未提供」）。
        """
        if job.kind == KIND_PLAN:
            return job.artifacts.draft_text or ""
        directory = self._store.rewrite_dir(job.book_id) / "drafts"
        if not directory.exists():
            return ""
        candidates = sorted(
            (p for p in directory.glob("rw-*.md") if not p.name.endswith(".outline.json")),
            reverse=True,
        )
        if not candidates:
            return ""
        try:
            return self._store.read_rewrite_draft(
                job.book_id, f"drafts/{candidates[0].name}"
            )
        except NovelNotFound:  # pragma: no cover - 防御性
            return ""

    def _chapter_tail(self, job: RewriteJob) -> str:
        """上一章正文尾部（只读，绝不写 `正文/`）。"""
        if not job.chapter_id:
            return ""
        try:
            book = self._base.get_book(job.book_id)
        except NovelNotFound:  # pragma: no cover - 防御性
            return ""
        pairs = [
            (volume, chapter)
            for volume in sorted(book.outline.nodes, key=lambda v: (v.order, v.id))
            for chapter in sorted(volume.children, key=lambda c: (c.order, c.id))
        ]
        previous: str | None = None
        for _volume, chapter in pairs:
            if chapter.id == job.chapter_id:
                break
            previous = chapter.file or previous
        if not previous:
            return ""
        try:
            path = self._base.chapter_path(job.book_id, previous)
        except NovelValidationError:  # pragma: no cover - 防御性
            return ""
        if not path.exists():
            return ""
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()[-800:]

    # ── Step 3 · evaluate（本地零依赖算法）──

    def _step_evaluate(self, job: RewriteJob) -> None:
        """本地 5 项算法 + 复用 `novel_ai.lint_text()`（**不调 AI**）。"""
        text = job.artifacts.draft_text or ""
        if job.artifacts.draft_rel and job.kind != KIND_OUTLINE and not text:
            text = self._store.read_rewrite_draft(job.book_id, job.artifacts.draft_rel)
            job.artifacts.draft_text = text
        hits = novel_ai.lint_text(text)
        job.artifacts.lint_hits = [
            hit.model_dump(mode="json") for hit in hits
        ]

    # ── Step 4 · finalize ──

    def _step_finalize(self, job: RewriteJob) -> None:
        """组装报告（`summary` 强制重算）→ 原子写 `rewrite/reports/`。"""
        blueprint = self._store.load_blueprint(job.book_id)
        rewrite_id = job.rewrite_id or make_rewrite_id(job.book_id)
        job.rewrite_id = rewrite_id

        book = self._base.get_book(job.book_id)
        total_chapters = sum(len(volume.children) for volume in book.outline.nodes)

        text = job.artifacts.draft_text or ""
        if job.artifacts.draft_rel and job.kind != KIND_OUTLINE and not text:
            text = self._store.read_rewrite_draft(job.book_id, job.artifacts.draft_rel)

        report = build_report(
            rewrite_id=rewrite_id,
            blueprint=blueprint,
            kind=job.kind,
            draft_text=text,
            outline_patch=job.artifacts.outline_patch,
            character_table=job.artifacts.character_table,
            reversal_table=job.artifacts.reversal_table,
            total_chapters=total_chapters,
            chapter_id=job.chapter_id,
            draft_file=job.artifacts.draft_rel or "",
            lint_hits=list(job.artifacts.lint_hits),
        )
        self._store.save_report(job.book_id, report)


_REGISTRY: RewriteJobRegistry | None = None


def shared_rewrite_job_registry() -> RewriteJobRegistry:
    """模块级单例（进程内唯一，持有 task 表）。semaphore 与既有 job 共享。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = RewriteJobRegistry()
    return _REGISTRY


def as_dict(job: RewriteJob) -> dict[str, Any]:
    """`RewriteJob` → JSON 字典（路由层统一出口）。"""
    return dict(job.model_dump(mode="json"))
