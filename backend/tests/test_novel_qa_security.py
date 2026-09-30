"""QA 独立补充测试（二）：路径安全与注入 payload。

由 QA（严过关）编写。覆盖点：

- `validate_id` / `validate_rel_path` 的穿越 payload 矩阵（`../`、绝对路径、盘符、
  大写、空、超长、换行注入）
- `book_id` / `chapter_id` / `draft_id` / `job_id` 四类 id 的拒绝
- `views/{name}` 非法名（含 `../book.json`）必须拒
- HTTP 层：非法 id → 422，绝不 200 返回别家数据
- 手工编辑 / 恶意构造 `book.json` 的 `file` 字段不得越出 `正文/`（权威文件保护）

自包含：无 conftest。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel as novel_api
from app.services.novel_store import (
    NovelStore,
    NovelValidationError,
    validate_id,
    validate_rel_path,
)

NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷",
        "order": 1,
        "children": [
            {"id": "ch-001", "type": "chapter", "title": "引子", "order": 1,
             "summary": "冷开场", "beat": "埋伏笔"},
        ],
    }
]


def _build_app(tmp_path: Path) -> tuple[FastAPI, NovelStore]:
    store = NovelStore(root=tmp_path)
    app = FastAPI()
    app.include_router(novel_api.router)
    app.dependency_overrides[novel_api.shared_store] = lambda: store
    return app, store


# ─────────────────────────── id 校验矩阵 ───────────────────────────

ILLEGAL_IDS = [
    "",
    "..",
    "../evil",
    "..\\evil",
    "a/b",
    "/abs",
    "//abs",
    "C:\\win",
    "C:/win",
    "Book",            # 大写
    "book_id",         # 下划线
    "book 001",        # 空格
    "book.001",        # 点
    "书-id",            # 非 ascii
    "x" * 65,          # 超长
    "job-../x",
]

LEGAL_IDS = ["a", "book-2026-001", "ch-001", "v1", "-", "x" * 64, "d-ch-001-20260101000000-ab12-continue"]


@pytest.mark.parametrize("value", ILLEGAL_IDS)
def test_validate_id_rejects_payloads(value: str) -> None:
    """所有穿越 / 非法 id 必须被 `validate_id` 拒绝（code=invalid_id）。"""
    with pytest.raises(NovelValidationError) as excinfo:
        validate_id(value, "book_id")
    assert excinfo.value.code == "invalid_id"


@pytest.mark.parametrize("value", LEGAL_IDS)
def test_validate_id_accepts_legal(value: str) -> None:
    assert validate_id(value, "book_id") == value


def test_validate_id_rejects_trailing_newline() -> None:
    """P2-1 修复后：尾部换行必须被拒（`$` 只挡行尾，`\\Z` 才挡字符串尾）。"""
    with pytest.raises(NovelValidationError) as excinfo:
        validate_id("book-1\n", "book_id")
    assert excinfo.value.code == "invalid_id"
    with pytest.raises(NovelValidationError):
        validate_id("book-1\nbook-2", "book_id")


# ─────────────────────────── 相对路径校验 ───────────────────────────


@pytest.mark.parametrize(
    "rel",
    ["../../evil.md", "/etc/passwd", "\\etc\\passwd", "C:/win/x.md", "", "   ", "../book.json"],
)
def test_validate_rel_path_rejects_escape(tmp_path: Path, rel: str) -> None:
    root = tmp_path / "book-1"
    root.mkdir()
    with pytest.raises(NovelValidationError) as excinfo:
        validate_rel_path(rel, root)
    assert excinfo.value.code == "path_escape"


def test_book_dir_and_chapter_path_reject_escape(tmp_path: Path) -> None:
    """store 的路径入口（book_dir / chapter_path）对穿越 payload 一律拒绝。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("安全书")
    for bad in ("../evil", "..", "a/b", "/abs"):
        with pytest.raises(NovelValidationError):
            store.book_dir(bad)
        with pytest.raises(NovelValidationError):
            store.chapter_path(book.id, f"../../{bad}.md")
        with pytest.raises(NovelValidationError):
            store.chapter_path(bad, "正文/ch-001-ch.md")


def test_draft_id_rejects_escape(tmp_path: Path) -> None:
    store = NovelStore(root=tmp_path)
    book = store.create_book("草稿安全")
    for bad in ("../state", "..", "d/../x", "/abs"):
        with pytest.raises(NovelValidationError):
            store.read_draft(book.id, bad)
        with pytest.raises(NovelValidationError):
            store.delete_draft(book.id, bad)


@pytest.mark.parametrize(
    "job_id",
    [
        "job-../evil-20260101000000-ab12",
        "job-..-20260101000000-ab12",
        "job-book-1-20260101-ab12",       # 时间戳位数不符
        "job-book-1-20260101000000-ZZZZ",  # 非 hex
        "../job-book-1-20260101000000-ab12",
        "book-1-20260101000000-ab12",      # 缺 job- 前缀
        "",
    ],
)
def test_job_id_rejects_malformed_and_escape(tmp_path: Path, job_id: str) -> None:
    store = NovelStore(root=tmp_path)
    store.create_book("任务安全")
    with pytest.raises(NovelValidationError):
        store.load_job(job_id)


def test_read_view_rejects_traversal_name(tmp_path: Path) -> None:
    """`views/{name}` 传 `../book.json` 必须被拒，不能读到权威文件。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("视图安全")
    for bad in ("../book.json", "..", "book.json", "context-card/../../book.json"):
        with pytest.raises(NovelValidationError) as excinfo:
            store.read_view(book.id, bad)
        assert excinfo.value.code == "invalid_payload"


# ─────────────────────────── HTTP 层 ───────────────────────────


@pytest.mark.parametrize("book_id", ["Book", "book_id", "x" * 65, "book.001", "..", "%2e%2e"])
def test_api_rejects_illegal_book_id(tmp_path: Path, book_id: str) -> None:
    """非法 book_id 必须 422（或路由层 404），绝不能 200 返回数据。"""
    app, _store = _build_app(tmp_path)
    with TestClient(app) as client:
        for path in (
            f"/api/novel/books/{book_id}/outline",
            f"/api/novel/books/{book_id}/chapters",
            f"/api/novel/books/{book_id}/state",
            f"/api/novel/books/{book_id}/meta",
        ):
            response = client.get(path)
            assert response.status_code in (404, 422), (path, response.status_code)
            if response.status_code == 422:
                assert response.json()["detail"]["code"] == "invalid_id"


@pytest.mark.parametrize("chapter_id", ["Ch-001", "ch_001", "ch/001", "%2e%2e", "x" * 65])
def test_api_rejects_illegal_chapter_id(tmp_path: Path, chapter_id: str) -> None:
    """注：字面 `..` 会被 HTTP 客户端在发请求前归一化（服务端根本收不到），
    因此这里用 `%2e%2e` 让「点号」真正抵达后端。"""
    app, store = _build_app(tmp_path)
    book = store.create_book("HTTP 安全书")
    with TestClient(app) as client:
        response = client.get(f"/api/novel/books/{book.id}/chapters/{chapter_id}")
        assert response.status_code in (404, 422), response.status_code


@pytest.mark.parametrize("name", ["..%2fbook.json", "book.json", "Context-Card", ""])
def test_api_rejects_illegal_view_name(tmp_path: Path, name: str) -> None:
    app, store = _build_app(tmp_path)
    book = store.create_book("视图 HTTP")
    with TestClient(app) as client:
        response = client.get(f"/api/novel/books/{book.id}/views/{name}")
        assert response.status_code in (404, 422), response.status_code
        if response.status_code == 422:
            assert response.json()["detail"]["code"] == "invalid_payload"


def test_api_cannot_read_other_books_data_via_traversal(tmp_path: Path) -> None:
    """穿越读取别家书籍内容：必须被拒，响应体里不得出现另一本书的正文。"""
    app, store = _build_app(tmp_path)
    victim = store.create_book("受害书")
    store.save_outline(victim.id, victim.version, NODES)
    store.write_chapter(victim.id, "ch-001", "绝密正文：黑匣子的真实来源")
    attacker = store.create_book("攻击者之书")

    with TestClient(app) as client:
        for path in (
            f"/api/novel/books/{attacker.id}/chapters/../{victim.id}",
            f"/api/novel/books/{attacker.id}/views/..%2f..%2f{victim.id}%2fbook.json",
        ):
            response = client.get(path)
            assert response.status_code in (404, 422), (path, response.status_code)
            assert "绝密正文" not in response.text


# ─────────────────────────── 权威文件保护 ───────────────────────────


def test_outline_file_field_must_stay_inside_chapters_dir(tmp_path: Path) -> None:
    """`file` 字段必须被约束在 `正文/` 内。

    若允许 `file` 指向 `state.json` / `book.json`，则后续 `PUT /chapters/{id}`
    的原子写会把权威 JSON 覆盖成章节正文 —— 纯 HTTP 即可造成数据损毁。
    期望：`save_outline` 拒绝越出 `正文/` 的 file；实际：当前会被接受。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("权威文件保护")
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-001", "type": "chapter", "title": "第一章", "order": 1,
                 "file": "正文/ch-001-ch.md"},
                {"id": "ch-002", "type": "chapter", "title": "第二章", "order": 2,
                 "file": "state.json"},
            ],
        }
    ]
    with pytest.raises(NovelValidationError):
        store.save_outline(book.id, book.version, nodes)


def test_outline_file_field_must_reject_book_json(tmp_path: Path) -> None:
    """同上，`file` 指向 `book.json` 也必须被拒（否则写正文会毁掉大纲）。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("权威文件保护2")
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-003", "type": "chapter", "title": "第三章", "order": 1,
                 "file": "book.json"},
            ],
        }
    ]
    with pytest.raises(NovelValidationError):
        store.save_outline(book.id, book.version, nodes)
