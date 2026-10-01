"""小说工作区 · 换元仿写 — 路由层（薄壳，13 端点）。

设计依据：`deliverables/novel-workspace/ARCHITECTURE-rewrite.md` §3.6 / §8.3。

职责边界：参数校验 → 调 service → 结构化错误码。**不写业务逻辑**。
统一错误响应沿用既有：`HTTPException(status_code=N, detail={"code":..., "message":...})`，
翻译口径**复用** `api/novel.py` 的 `http_error` / `as_http` / `guarded` 三个公开别名
（**不复制错误翻译表**）。

端点（前缀 `/api/novel/books/{book_id}/rewrite`）：
    R1  GET    /blueprint
    R2  PUT    /blueprint
    R3  POST   /precheck
    R4  POST   /plan            → 202
    R5  POST   /outline         → 202
    R6  POST   /chapter-draft   → 202
    R7  GET    /reports/{rewrite_id}
    R8  POST   /reports/{rewrite_id}/check
    R9  POST   /reports/{rewrite_id}/adopt
    R10 GET    /jobs/{job_id}
    R11 POST   /jobs/{job_id}/resume
    R12 POST   /jobs/{job_id}/cancel
    R13 GET    /reports

附加只读端点（P0-10 审计 / 免责单点）：
    GET  /snapshot    —— 权威数据三重校验值（零写入审计）
    GET  /disclaimer  —— 免责声明**物理单点**下发（前端不得另写一份）

许可证：MIT（与宿主项目一致）。本文件全部为自研实现。
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api import novel as novel_api
from app.services import novel_rewrite_ai, novel_rewrite_jobs
from app.services.novel_rewrite_jobs import (
    RewriteJobRegistry,
    shared_rewrite_job_registry,
)
from app.services.novel_rewrite_store import (
    PRECHECK_HONESTY_NOTE,
    PRECHECK_SAMPLE,
    Blueprint,
    Disclaimer,
    RewriteAckRequiredError,
    RewriteStore,
    apply_checks,
    capture_authoritative_snapshot,
    clean_precheck_response,
    is_gate_ready,
    precheck_blueprint,
    precheck_text,
    require_adoptable,
    write_ack,
)
from app.services.novel_store import (
    ERR_INVALID_PAYLOAD,
    NovelConflict,
    NovelNotFound,
    NovelStore,
    NovelValidationError,
    now_iso,
    validate_id,
)

router = APIRouter(prefix="/api/novel", tags=["novel-rewrite"])


# ─────────────────────────── 依赖注入 ───────────────────────────


def shared_rewrite_store() -> RewriteStore:
    """仿写域读写。

    测试用 `app.dependency_overrides[novel_rewrite.shared_rewrite_store] =
    lambda: RewriteStore(NovelStore(root=tmp_path))` 注入临时根目录
    （本仓 `tests/` 无 conftest.py，测试必须自包含）。
    """
    return RewriteStore(NovelStore())


def shared_store() -> NovelStore:
    """底层事实层（与 `api/novel.py` 同源，供需要直接读写权威数据的端点使用）。"""
    return NovelStore()


def shared_registry() -> RewriteJobRegistry:
    """仿写任务注册表单例（semaphore 与既有 job 共享）。"""
    return shared_rewrite_job_registry()


# ─────────────────────────── 错误翻译 ───────────────────────────


def _as_http(exc: Exception) -> HTTPException:
    """仿写域异常翻译：**复用既有表**，只补 AiCallError 一种（503）。

    `novel_rewrite_ai.AiCallError` 就是 `novel_ai.AiCallError`，所以其实
    `novel_api.as_http` 已经能翻译；这里显式补一层是为了让「仿写路由」
    在将来若换成独立异常类时不会静默退化成 500。
    """
    if isinstance(exc, novel_rewrite_ai.AiCallError):
        return novel_api.http_error(503, exc.code, str(exc))
    return novel_api.as_http(exc)


@contextlib.contextmanager
def _guarded() -> Iterator[None]:
    """统一出口：service 异常 → 结构化 HTTP 错误（复用既有翻译表）。"""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:  # 统一出口，避免裸 500
        raise _as_http(exc) from exc


# ─────────────────────────── 请求模型 ───────────────────────────


class PutBlueprintRequest(BaseModel):
    """保存结构蓝图。"""

    blueprint: dict[str, Any] = Field(default_factory=dict)


class PrecheckRequest(BaseModel):
    """输入侧预检：`fields` 与 `blueprint` 二选一。"""

    fields: dict[str, str] = Field(default_factory=dict)
    blueprint: dict[str, Any] | None = None


class GenerateRequest(BaseModel):
    """生成请求（plan / outline / chapter-draft 共用）。"""

    risk_ack: bool = False
    skip_gate: bool = False
    skip_reason: str = ""
    chapter_id: str | None = None
    version: int | None = None


class CheckRequest(BaseModel):
    """报告逐项勾选（幂等）。"""

    checks: list[dict[str, Any]] = Field(default_factory=list)
    reverse: list[dict[str, Any]] = Field(default_factory=list)


class AdoptRequest(BaseModel):
    """采纳请求（★P3：一次只处理一个产物，无批量、无一键全书★）。"""

    ack: bool = False
    target: str = "chapter"         # chapter | outline
    version: int | None = None      # target=outline 时的乐观锁
    fact: dict[str, Any] | None = None


# ─────────────────────────── R1/R2 · 蓝图 ───────────────────────────


@router.get("/books/{book_id}/rewrite/blueprint")
def get_blueprint(
    book_id: str,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """读结构蓝图；不存在 → 返回空蓝图（**不落盘**）+ 闸门就绪度。"""
    with _guarded():
        blueprint = store.load_blueprint(book_id)
        ready, missing = is_gate_ready(blueprint)
        return {
            "ok": True,
            "blueprint": blueprint.model_dump(mode="json", by_alias=True),
            "ready": ready,
            "missing_layers": missing,
        }


@router.put("/books/{book_id}/rewrite/blueprint")
def put_blueprint(
    book_id: str,
    body: PutBlueprintRequest,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """保存结构蓝图（**落盘前跑完整预检**，命中即 422，一个字节都不落）。"""
    with _guarded():
        if not body.blueprint:
            raise NovelValidationError("请求体需包含 blueprint", code=ERR_INVALID_PAYLOAD)
        try:
            incoming = Blueprint.model_validate(body.blueprint)
        except Exception as exc:
            raise NovelValidationError(
                f"蓝图结构非法: {exc}", code=ERR_INVALID_PAYLOAD
            ) from exc
        saved = store.save_blueprint(book_id, incoming)
        ready, missing = is_gate_ready(saved)
        return {
            "ok": True,
            "blueprint": saved.model_dump(mode="json", by_alias=True),
            "ready": ready,
            "missing_layers": missing,
        }


# ─────────────────────────── R3 · 预检 ───────────────────────────


@router.post("/books/{book_id}/rewrite/precheck")
def post_precheck(
    book_id: str,
    body: PrecheckRequest,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """输入侧原文预检（本地四规则，不调 AI）。

    命中 → 422 `rewrite_source_rejected` + `hits`（**绝不回显被拒原文全文**）。
    """
    with _guarded():
        hits: list[dict] = []
        if body.blueprint is not None:
            blueprint = Blueprint.model_validate(body.blueprint)
            hits = precheck_blueprint(blueprint)
        elif body.fields:
            for name, text in body.fields.items():
                hits.extend(precheck_text(str(name), str(text)))
        else:
            raise NovelValidationError(
                "请求体需包含 fields 或 blueprint", code=ERR_INVALID_PAYLOAD
            )

        if hits:
            # 422 + hits + 引导示例。**响应体绝不回显被拒原文全文**（P0-3④）。
            payload = clean_precheck_response(hits)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": payload["code"],
                    "message": "输入疑似原文，已被预检拒绝（未保存）。请改写为结构笔记后重试。",
                    "hits": payload["hits"],
                    "sample": payload["sample"],
                    "honesty_note": payload["honesty_note"],
                },
            )
        return {
            "ok": True,
            "passed": True,
            "hits": [],
            "sample": PRECHECK_SAMPLE,
            "honesty_note": PRECHECK_HONESTY_NOTE,
        }


# ─────────────────────────── R4/R5/R6 · 生成（202）───────────────────────────


def _start_job(
    book_id: str,
    kind: str,
    body: GenerateRequest,
    registry: RewriteJobRegistry,
) -> dict[str, Any]:
    """三个生成端点共用的启动逻辑（避免三处漂移）。"""
    job = registry.create_job(
        book_id,
        kind,
        chapter_id=body.chapter_id,
        risk_ack=bool(body.risk_ack),
        skip_gate=bool(body.skip_gate),
        skip_reason=body.skip_reason,
    )
    return job.model_dump(mode="json")


@router.post("/books/{book_id}/rewrite/plan", status_code=202)
async def post_plan(
    book_id: str,
    body: GenerateRequest,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """生成五层重建设定卡（202 + `RewriteJob`）。

    `risk_ack` 缺失 → 422 `rewrite_ack_required`；L3/L5 缺表 → 422
    `rewrite_gate_blocked`；AI 不可用 → 503 `ai_unavailable`（**不建 job、不 mock**）。
    """
    with _guarded():
        return _start_job(book_id, novel_rewrite_jobs.KIND_PLAN, body, registry)


@router.post("/books/{book_id}/rewrite/outline", status_code=202)
async def post_outline(
    book_id: str,
    body: GenerateRequest,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """生成六章级大纲补丁（**不写 `book.json`**，产物落 `rewrite/drafts/`）。"""
    with _guarded():
        return _start_job(book_id, novel_rewrite_jobs.KIND_OUTLINE, body, registry)


@router.post("/books/{book_id}/rewrite/chapter-draft", status_code=202)
async def post_chapter_draft(
    book_id: str,
    body: GenerateRequest,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """生成分章草稿（**不写 `正文/`**，产物落 `rewrite/drafts/`）。"""
    with _guarded():
        return _start_job(book_id, novel_rewrite_jobs.KIND_CHAPTER, body, registry)


# ─────────────────────────── R7/R8/R9 · 报告 ───────────────────────────


@router.get("/books/{book_id}/rewrite/reports/{rewrite_id}")
def get_report(
    book_id: str,
    rewrite_id: str,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """读质检报告（含 `summary` 强算结果与 ack 留痕）。"""
    with _guarded():
        report = store.load_report(book_id, rewrite_id)
        return {"ok": True, "report": report.model_dump(mode="json")}


@router.post("/books/{book_id}/rewrite/reports/{rewrite_id}/check")
def post_check(
    book_id: str,
    rewrite_id: str,
    body: CheckRequest,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """报告逐项勾选（**幂等**，落盘不丢）。

    `fail` 项**不接受勾选**（硬阻断不可降级为 warn）。
    """
    with _guarded():
        report = store.load_report(book_id, rewrite_id)
        updated = apply_checks(report, body.checks, body.reverse)
        saved = store.save_report(book_id, updated)
        return {"ok": True, "report": saved.model_dump(mode="json")}


@router.post("/books/{book_id}/rewrite/reports/{rewrite_id}/adopt")
def post_adopt(
    book_id: str,
    rewrite_id: str,
    body: AdoptRequest,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """采纳进正式书稿（★唯一入口★，一次只处理一个产物）。

    服务端二次校验（**不信前端**）：无 fail + 全部 warn/unavailable 已勾选 +
    反向三问已勾选 + ack 已写，缺任一 → 422 `rewrite_ack_required`。

    - `target=chapter` → 走既有 `write_chapter` + `set_chapter_status`
      （**不动 `version`** —— A2 裁定，version 是前端大纲乐观锁凭据）。
    - `target=outline` → 走既有 `save_outline(book_id, version, nodes)`，
      version 过期 → 409 `version_conflict`。
    """
    with _guarded():
        if not body.ack:
            raise RewriteAckRequiredError("请先完成采纳二次确认（ack=true）")

        base = store.base
        target = (body.target or "chapter").lower()
        if target not in ("chapter", "outline"):
            raise NovelValidationError(
                f"target 非法: {body.target!r}（只支持 chapter / outline）",
                code=ERR_INVALID_PAYLOAD,
            )
        # ★P2-9：version 校验**必须在 ack 落盘之前**。
        # 原来只在 `save_outline()` 里撞锁，此时 ack 已写、报告已落盘，
        # 审计上留下一次「已确认但没采纳」的留痕。这里把**两种** version 失败
        # （缺失 → 422 / 过期 → 409）都提到 write_ack 之前。
        if target == "outline":
            if body.version is None:
                raise NovelValidationError(
                    "target=outline 时必须携带 version（大纲乐观锁）", code=ERR_INVALID_PAYLOAD
                )
            current_version = base.get_book(book_id).version
            if int(body.version) != current_version:
                raise NovelConflict(
                    f"大纲 version 已过期：当前 {current_version}，请求 {body.version}"
                    "（请刷新大纲后重试）"
                )

        report = store.load_report(book_id, rewrite_id)
        # ① ack 落盘（勾选留痕：时间 + 免责版本 + 勾了哪些项）
        report = write_ack(report)
        # ② 服务端二次校验（不信前端）
        require_adoptable(report)
        store.save_report(book_id, report)

        result: dict[str, Any] = {"ok": True}

        if target == "chapter":
            chapter_id = report.chapter_id
            if not chapter_id:
                raise NovelValidationError(
                    "该报告未绑定章节，无法采纳进正文", code=ERR_INVALID_PAYLOAD
                )
            draft_text = store.read_rewrite_draft(book_id, report.draft_file)
            written = base.write_chapter(book_id, chapter_id, draft_text)
            base.set_chapter_status(book_id, chapter_id, "published")
            state = base.get_state(book_id)
            if body.fact is not None:
                fact = novel_api.validate_fact(body.fact)
                state = base.ingest_facts(book_id, chapter_id, fact)
            files = base.rebuild_views(book_id)
            # ★P2-4：重复采纳留痕（区分首采与重采；幂等覆盖不损坏数据，但要可审计）
            report.adopted_at = now_iso()
            report.adopted_target = "chapter"
            store.save_report(book_id, report)
            result.update(
                {
                    "chapter": written,
                    "state": state.model_dump(mode="json", by_alias=True),
                    "views_rebuilt": files,
                    "adopted_at": report.adopted_at,
                }
            )
            return result

        # target == outline
        raw = store.read_rewrite_draft(book_id, report.draft_file)
        patch = novel_rewrite_ai.parse_outline_patch(raw)
        book = base.save_outline(book_id, int(body.version), list(patch.get("nodes") or []))
        files = base.rebuild_views(book_id)
        report.adopted_at = now_iso()
        report.adopted_target = "outline"
        store.save_report(book_id, report)
        result.update(
            {
                "outline": book.outline.model_dump(mode="json"),
                "version": book.version,
                "views_rebuilt": files,
                "adopted_at": report.adopted_at,
            }
        )
        return result


# ─────────────────────────── R10/R11/R12 · 任务 ───────────────────────────


@router.get("/books/{book_id}/rewrite/jobs/{job_id}")
async def get_rewrite_job(
    book_id: str,
    job_id: str,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """轮询仿写任务（权威在磁盘，跨刷新/重启可读）。"""
    with _guarded():
        job = registry.get_job(job_id)
        if job.book_id != book_id:
            raise NovelNotFound(f"仿写任务不存在: {job_id}")
        return job.model_dump(mode="json")


@router.post("/books/{book_id}/rewrite/jobs/{job_id}/resume")
async def resume_rewrite_job(
    book_id: str,
    job_id: str,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """从失败步重试（已完成步骤不重跑，`at` 保留）。"""
    with _guarded():
        # ★P2-11★ 归属校验必须**先于副作用**（`registry.resume()` 之前）。
        # 旧写法是「先 resume 再判归属」：`resume()` 会先把 B 书任务的 steps
        # 翻转、`status="queued"`、`failed_step` 清空、**落盘**、再 `_spawn()`
        # 真起后台任务，然后才返回 404。于是调用方拿到「我不做这件事」，
        # 实际上 B 书任务已经跑起来了（占掉共享 semaphore 一格，并向 B 书
        # `rewrite/` 写产物）。404 的语义被自己破坏。
        # 正确顺序：先 `get_job` 判归属，再决定要不要动 registry。
        job = registry.get_job(job_id)
        if job.book_id != book_id:
            raise NovelNotFound(f"仿写任务不存在: {job_id}")
        job = registry.resume(job_id)
        return job.model_dump(mode="json")


@router.post("/books/{book_id}/rewrite/jobs/{job_id}/cancel")
async def cancel_rewrite_job(
    book_id: str,
    job_id: str,
    registry: RewriteJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """协作式取消（当前步骤收尾后停止，已完成产物保留）。"""
    with _guarded():
        # ★P2-11★ 同 `resume`：归属校验先于副作用（`registry.cancel()` 会置取消
        # 标记、落盘、并可能对孤儿任务直接写终态，这些都是不可逆副作用）。
        job = registry.get_job(job_id)
        if job.book_id != book_id:
            raise NovelNotFound(f"仿写任务不存在: {job_id}")
        job = registry.cancel(job_id)
        return job.model_dump(mode="json")


# ─────────────────────────── R13 · 报告清单 ───────────────────────────


@router.get("/books/{book_id}/rewrite/reports")
def list_reports(
    book_id: str,
    kind: str | None = Query(None, description="plan | outline | chapter"),
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """报告清单（最新在前）。"""
    with _guarded():
        if kind is not None and kind not in ("plan", "outline", "chapter"):
            raise NovelValidationError(
                f"kind 非法: {kind!r}（只支持 plan / outline / chapter）",
                code=ERR_INVALID_PAYLOAD,
            )
        items = store.list_reports(book_id, kind=kind)
        return {"ok": True, "reports": items, "count": len(items)}


# ─────────────────────────── 附加 · 零写入自检快照 ───────────────────────────


@router.get("/books/{book_id}/rewrite/snapshot")
def get_snapshot(
    book_id: str,
    store: RewriteStore = Depends(shared_rewrite_store),
) -> dict[str, Any]:
    """权威数据三重快照（P0-10 零写入审计入口，只读）。

    `{book_mtime_ns, book_version, state_mtime_ns, state_sha256, chapters:{...}}`
    """
    with _guarded():
        return {"ok": True, "snapshot": capture_authoritative_snapshot(store.base, book_id)}


# ─────────────────────────── 附加 · 免责声明单点来源 ───────────────────────────


@router.get("/books/{book_id}/rewrite/disclaimer")
def get_disclaimer(book_id: str) -> dict[str, Any]:
    """只读免责声明（★物理单点★）。

    主理人裁定：免责声明不允许存在第二份文案 —— 只要前端另备一份占位，
    将来改了后端 `DISCLAIMER_TEXT` 而占位没跟着改，用户在**生成前**
    （恰恰是告知最该生效的时机）看到的就是过期声明。

    所以这里直接把 `novel_rewrite_store.DISCLAIMER_TEXT` / `DISCLAIMER_VERSION`
    下发，前端**不得**在任何分支里写自己的免责措辞；网络失败时也只能显示
    「加载失败」这样的状态句，不能显示替代声明。

    `book_id` 只做路径占位（声明是全局常量），但仍要校验 id 形态，
    避免把任意字符串透进存储层。
    """
    validate_id(book_id, "book_id")
    return {
        "ok": True,
        "disclaimer": Disclaimer().model_dump(mode="json"),
        "honesty_note": PRECHECK_HONESTY_NOTE,
    }
