"""小说工作区 API（薄路由层）。

设计依据：`deliverables/novel-workspace/ARCHITECTURE.md` §3.6 / §7.3。

职责边界：参数校验 → 调 service → 结构化错误码。**不写业务逻辑**。
统一错误响应：`HTTPException(status_code=N, detail={"code": ..., "message": ...})`。

端点共 23 个（21 + 主理人 A4 裁定的 `/jobs/{id}/cancel` + 书籍元数据的
`GET /books/{id}/meta`；`PATCH /books/{id}` 就地扩展，不新增端点）。

许可证：MIT（与宿主项目一致）。本文件全部为自研实现。
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services import novel_ai
from app.services.novel_jobs import (
    NovelJobBusyError,
    NovelJobError,
    NovelJobNotResumableError,
    NovelJobRegistry,
    shared_novel_job_registry,
)
from app.services.novel_store import (
    ERR_FACT_PARSE_FAILED,
    ERR_INTERNAL,
    ERR_INVALID_PAYLOAD,
    ERR_NOT_FOUND,
    ERR_VERSION_CONFLICT,
    ERR_WRITE_FAILED,
    VIEW_NAMES,
    ChapterFact,
    NovelConflict,
    NovelNotFound,
    NovelStore,
    NovelStoreError,
    NovelValidationError,
    count_words,
)

router = APIRouter(prefix="/api/novel", tags=["novel"])


# ─────────────────────────── 依赖注入 ───────────────────────────


def shared_store() -> NovelStore:
    """默认 store（root=`settings.data_dir/novel`）。

    测试用 `app.dependency_overrides[novel_api.shared_store] = lambda: NovelStore(root=tmp_path)`
    注入临时根目录（本仓 `tests/` 无 conftest.py，测试必须自包含）。
    """
    return NovelStore()


def shared_registry() -> NovelJobRegistry:
    """任务注册表单例（持有 semaphore 与 task 表）。"""
    return shared_novel_job_registry()


# ─────────────────────────── 错误翻译 ───────────────────────────


def _http_error(status: int, code: str, message: str) -> HTTPException:
    """统一结构化错误：`detail={"code":..., "message":...}`。"""
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _as_http(exc: Exception) -> HTTPException:
    """把 service 层异常翻译成结构化 HTTP 错误（诚实上报，不吞异常）。"""
    if isinstance(exc, NovelNotFound):
        return _http_error(404, ERR_NOT_FOUND, str(exc))
    if isinstance(exc, novel_ai.WriteGateError):
        return _http_error(422, exc.code, str(exc))
    if isinstance(exc, novel_ai.FactParseError):
        return _http_error(422, ERR_FACT_PARSE_FAILED, str(exc))
    if isinstance(exc, novel_ai.AiCallError):
        return _http_error(503, exc.code, str(exc))
    if isinstance(exc, NovelValidationError):
        return _http_error(422, exc.code, str(exc))
    if isinstance(exc, NovelConflict):
        return _http_error(409, ERR_VERSION_CONFLICT, str(exc))
    if isinstance(exc, NovelJobBusyError):
        return _http_error(409, exc.code, str(exc))
    if isinstance(exc, NovelJobNotResumableError):
        return _http_error(409, exc.code, str(exc))
    if isinstance(exc, NovelJobError):
        return _http_error(409, exc.code, str(exc))
    if isinstance(exc, NovelStoreError):
        return _http_error(500, ERR_WRITE_FAILED, str(exc))
    return _http_error(500, ERR_INTERNAL, str(exc))


@contextlib.contextmanager
def _guarded() -> Iterator[None]:
    """把端点体内的 service 异常统一翻译成结构化 HTTP 错误。"""
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:  # 统一出口，避免裸 500
        raise _as_http(exc) from exc


# ─────────────────────────── 请求模型 ───────────────────────────


class CreateBookRequest(BaseModel):
    """建书请求。"""

    title: str = ""


class PatchBookRequest(BaseModel):
    """更新书籍元数据请求（局部更新：不传的字段保持原值，不清空）。"""

    title: str | None = None
    author: str | None = None
    genre: str | None = None
    pov: str | None = None
    tense: str | None = None
    setting_summary: str | None = None


class PutOutlineRequest(BaseModel):
    """全量保存大纲（带乐观锁）。"""

    version: int = 0
    nodes: list[dict[str, Any]] = Field(default_factory=list)


class PutChapterRequest(BaseModel):
    """写正文（自动保存）。"""

    content: str = ""


class PatchStateRequest(BaseModel):
    """更新追踪态：改滚动摘要 或 手工补录章节事实。"""

    rolling_summary: str | None = None
    chapter_id: str | None = None
    fact: dict[str, Any] | None = None


class AiDraftRequest(BaseModel):
    """启动 AI 任务。"""

    mode: str = "continue"
    selection: str | None = None
    skip_gate: bool = False


class AdoptRequest(BaseModel):
    """采纳草稿。"""

    draft_id: str
    fact: dict[str, Any] | None = None
    selection: str | None = None


class LintRequest(BaseModel):
    """写后自检。"""

    text: str | None = None
    chapter_id: str | None = None


# ─────────────────────────── 1. 状态 ───────────────────────────


@router.get("/status")
def get_status(store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """AI 网关可用性（fail-closed 入口）。

    无 Key 时同样返回 **200**（状态查询本身不是失败），`available=false`
    且带中文 `reason` 与 `code=ai_unavailable`。
    """
    with _guarded():
        status = novel_ai.ai_status()
        payload = status.model_dump(mode="json")
        payload["data_dir_abs"] = str(store.novel_root())
        payload["books_dir_abs"] = str(store.books_root())
        return payload


# ─────────────────────────── 2-5. 书架 ───────────────────────────


@router.get("/books")
def list_books(store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """书架列表（含数据目录绝对路径，兑现 synergy「本地优先」承诺）。"""
    with _guarded():
        return {
            "books": store.list_books(),
            "data_dir_abs": str(store.novel_root()),
        }


@router.post("/books")
def create_book(body: CreateBookRequest, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """新建一本书（建目录 + book.json + 空 state.json + 空 views/）。"""
    with _guarded():
        return store.create_book(body.title).model_dump(mode="json")


@router.patch("/books/{book_id}")
def patch_book(
    book_id: str,
    body: PatchBookRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """局部更新书籍元数据（title / author / genre / pov / tense / setting_summary）。

    语义：**只传要改的字段**，未出现的字段保持原值、不会被清空（所以
    「只改设定摘要」不会顺手把书名抹掉）。返回与 `GET .../meta` 同构的载荷。
    """
    with _guarded():
        fields = body.model_dump(exclude_none=True)
        if not fields:
            raise NovelValidationError(
                "请求体需至少包含一个待更新字段", code=ERR_INVALID_PAYLOAD
            )
        return store.update_book_meta(book_id, fields)


@router.get("/books/{book_id}/meta")
def get_book_meta(book_id: str, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """读书籍元数据（不含大纲树）+ 章节数/总字数。

    左栏「设定摘要」卡只需要几个文本字段，不该为了读设定拉整棵大纲树。
    """
    with _guarded():
        return store.get_book_meta(book_id)


@router.delete("/books/{book_id}")
def delete_book(book_id: str, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """删除书籍（连带删除整个目录）。"""
    with _guarded():
        store.delete_book(book_id)
        return {"ok": True}


# ─────────────────────────── 6-7. 大纲 ───────────────────────────


@router.get("/books/{book_id}/outline")
def get_outline(book_id: str, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """读大纲树（含 version，供前端乐观锁）。"""
    with _guarded():
        book = store.get_book(book_id)
        return {
            "version": book.version,
            "nodes": [node.model_dump(mode="json") for node in book.outline.nodes],
            "book_title": book.title,
        }


@router.put("/books/{book_id}/outline")
def put_outline(
    book_id: str,
    body: PutOutlineRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """全量保存大纲树（version 不符 → 409 `version_conflict`）。"""
    with _guarded():
        book = store.save_outline(book_id, body.version, body.nodes)
        return book.model_dump(mode="json")


# ─────────────────────────── 8-10. 章节 ───────────────────────────


@router.get("/books/{book_id}/chapters")
def list_chapters(
    book_id: str,
    volume_id: str | None = Query(None, description="按卷过滤，不传为全部"),
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """章节卡片列表（字数/状态/草稿数）。"""
    with _guarded():
        return {"chapters": store.list_chapters(book_id, volume_id)}


@router.get("/books/{book_id}/chapters/{chapter_id}")
def get_chapter(
    book_id: str,
    chapter_id: str,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """读章节正文（Markdown 原文 + 字数 + 绝对路径 + 最新草稿）。"""
    with _guarded():
        content, path = store.read_chapter(book_id, chapter_id)
        node = store.get_chapter_node(book_id, chapter_id)[1]
        drafts = store.list_drafts(book_id, chapter_id)
        draft: dict[str, Any] | None = None
        if drafts:
            draft = {
                "id": drafts[0]["id"],
                "mode": drafts[0]["mode"],
                "created_at": drafts[0]["created_at"],
                "text": store.read_draft(book_id, str(drafts[0]["id"])),
            }
        return {
            "chapter": node.model_dump(mode="json"),
            "content": content,
            "word_count": count_words(content),
            "abs_path": str(path),
            "draft": draft,
        }


@router.put("/books/{book_id}/chapters/{chapter_id}")
def put_chapter(
    book_id: str,
    chapter_id: str,
    body: PutChapterRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """写正文（自动保存；`newline=""` 原子写，字节级保留换行习惯）。"""
    with _guarded():
        return store.write_chapter(book_id, chapter_id, body.content)


# ─────────────────────────── 11-12. 追踪态 ───────────────────────────


@router.get("/books/{book_id}/state")
def get_state(book_id: str, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """读追踪态（`from`/`to` 键名与 PRD §5.3 一致）。"""
    with _guarded():
        return store.get_state(book_id).model_dump(mode="json", by_alias=True)


@router.patch("/books/{book_id}/state")
def patch_state(
    book_id: str,
    body: PatchStateRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """更新追踪态：改滚动摘要，或手工补录章节事实（不强依赖 AI）。"""
    with _guarded():
        if body.chapter_id or body.fact is not None:
            if not body.chapter_id or body.fact is None:
                raise NovelValidationError(
                    "手工补录需要同时提供 chapter_id 与 fact", code=ERR_INVALID_PAYLOAD
                )
            # 章节必须真实存在于大纲：否则会写入一条永远显示不出来的孤儿事实
            # （上下文卡按大纲顺序取最近 N 章，查不到的 id 排到最后）。
            store.get_chapter_node(book_id, body.chapter_id)
            fact = _validate_fact(body.fact)
            state = store.ingest_facts(book_id, body.chapter_id, fact)
        elif body.rolling_summary is not None:
            state = store.update_rolling_summary(book_id, body.rolling_summary)
        else:
            raise NovelValidationError(
                "请求体需包含 rolling_summary，或 chapter_id + fact", code=ERR_INVALID_PAYLOAD
            )
        store.rebuild_views(book_id)
        return state.model_dump(mode="json", by_alias=True)


# ─────────────────────────── 13-14. 派生视图 ───────────────────────────


@router.post("/books/{book_id}/views/rebuild")
def rebuild_views(book_id: str, store: NovelStore = Depends(shared_store)) -> dict[str, Any]:
    """由权威 JSON 重建全部派生视图（幂等）。"""
    with _guarded():
        return {"ok": True, "files": store.rebuild_views(book_id)}


@router.get("/books/{book_id}/views/{name}")
def read_view(
    book_id: str,
    name: str,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """读派生只读视图（context-card / timeline / characters）。"""
    with _guarded():
        if name not in VIEW_NAMES:
            raise NovelValidationError(
                f"视图名非法，只支持 {' / '.join(VIEW_NAMES)}", code=ERR_INVALID_PAYLOAD
            )
        content = store.read_view(book_id, name)
        return {
            "name": name,
            "content": content,
            "generated_at": _view_generated_at(store, book_id, name),
        }


# ─────────────────────────── 15. AI 任务 ───────────────────────────


@router.post("/books/{book_id}/chapters/{chapter_id}/ai/draft", status_code=202)
async def create_ai_draft(
    book_id: str,
    chapter_id: str,
    body: AiDraftRequest,
    store: NovelStore = Depends(shared_store),
    registry: NovelJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """启动 AI 续写/润色任务（异步 job + 轮询）。

    返回 202 + `NovelJob`。AI 不可用时返回 **503 + `code=ai_unavailable`**；
    写前门禁未通过返回 **422 + `code=missing_beat`**（可用 `skip_gate` 显式放行）。
    """
    with _guarded():
        job = registry.create_job(
            book_id,
            chapter_id,
            body.mode,
            selection=body.selection,
            skip_gate=body.skip_gate,
        )
        return job.model_dump(mode="json")


# ─────────────────────────── 16. 采纳 ───────────────────────────


@router.post("/books/{book_id}/chapters/{chapter_id}/adopt")
def adopt_draft(
    book_id: str,
    chapter_id: str,
    body: AdoptRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """草稿转正式 + 摄取事实快照 + 重建派生视图。

    `fact` 非法时返回 422 `fact_parse_failed`，此时**正文未被改动**（先校验后写入，
    避免"正文已改、追踪态没写"的半成品状态）。
    """
    with _guarded():
        fact = _validate_fact(body.fact) if body.fact is not None else None
        result = store.adopt_draft(book_id, chapter_id, body.draft_id, selection=body.selection)
        if fact is not None:
            state = store.ingest_facts(book_id, chapter_id, fact)
        else:
            state = store.get_state(book_id)
        files = store.rebuild_views(book_id)
        return {
            "ok": True,
            "chapter": result["chapter"],
            "word_count": result["word_count"],
            "abs_path": result["abs_path"],
            "state": state.model_dump(mode="json", by_alias=True),
            "views_rebuilt": files,
        }


# ─────────────────────────── 17. 写后自检 ───────────────────────────


@router.post("/books/{book_id}/lint")
def lint(
    book_id: str,
    body: LintRequest,
    store: NovelStore = Depends(shared_store),
) -> dict[str, Any]:
    """写后自检（纯本地正则规则，只提醒不改写）。"""
    with _guarded():
        if body.chapter_id:
            text, _path = store.read_chapter(book_id, body.chapter_id)
            source = body.chapter_id
        elif body.text is not None:
            text = body.text
            source = "inline"
        else:
            raise NovelValidationError(
                "请求体需包含 text 或 chapter_id", code=ERR_INVALID_PAYLOAD
            )
        hits = novel_ai.lint_text(text)
        return {
            "source": source,
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "count": len(hits),
        }


# ─────────────────────────── 18. 导出 ───────────────────────────


@router.get("/books/{book_id}/export")
def export(
    book_id: str,
    format: str = Query("md", description="md | txt"),
    scope: str = Query("chapter", description="chapter | book"),
    chapter_id: str | None = Query(None, description="scope=chapter 时必填"),
    store: NovelStore = Depends(shared_store),
) -> Response:
    """导出单章或整本（`.md` / 剥离标记的 `.txt`）。"""
    with _guarded():
        fmt = (format or "md").lower()
        if fmt not in ("md", "txt"):
            raise NovelValidationError(f"format 非法: {format!r}", code=ERR_INVALID_PAYLOAD)
        if (scope or "chapter") not in ("chapter", "book"):
            raise NovelValidationError(f"scope 非法: {scope!r}", code=ERR_INVALID_PAYLOAD)
        if (scope or "chapter") == "chapter":
            if not chapter_id:
                raise NovelValidationError(
                    "scope=chapter 时必须提供 chapter_id", code=ERR_INVALID_PAYLOAD
                )
            filename, content = store.export_chapter(book_id, chapter_id, fmt)
        else:
            filename, content = store.export_book(book_id, fmt)
        media_type = "text/plain; charset=utf-8" if fmt == "txt" else "text/markdown; charset=utf-8"
        return Response(
            content=content.encode("utf-8"),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )


# ─────────────────────────── 19-21. 任务查询 / 续跑 / 取消 ───────────────────────────


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    registry: NovelJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """轮询任务进度（权威在磁盘，跨刷新/重启可读）。"""
    with _guarded():
        return registry.get_job(job_id).model_dump(mode="json")


@router.post("/jobs/{job_id}/resume")
async def resume_job(
    job_id: str,
    registry: NovelJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """从失败步重试（已完成步骤不重跑）。running → 409 `job_busy`。"""
    with _guarded():
        return registry.resume(job_id).model_dump(mode="json")


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    registry: NovelJobRegistry = Depends(shared_registry),
) -> dict[str, Any]:
    """协作式取消：置取消标记，当前步骤收尾后停止（不假装立即中断）。"""
    with _guarded():
        return registry.cancel(job_id).model_dump(mode="json")


# ─────────────────────────── 内部工具 ───────────────────────────


def _validate_fact(payload: dict[str, Any]) -> ChapterFact:
    """校验事实快照；失败 → `FactParseError`（→ 422 `fact_parse_failed`，绝不落盘）。"""
    try:
        return ChapterFact.model_validate(payload)
    except Exception as exc:
        raise novel_ai.FactParseError(f"事实快照校验失败: {exc}") from exc


def _view_generated_at(store: NovelStore, book_id: str, name: str) -> str:
    """派生视图的生成时间（文件 mtime）。"""
    path = store.views_dir(book_id) / f"{name}.md"
    if not path.exists():
        return ""
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()


__all__ = ["router", "shared_registry", "shared_store"]
