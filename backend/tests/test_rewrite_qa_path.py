"""QA 独立验证 · B 组：仿写域路径安全（越界写 = 一次原子写毁掉一本书）。

出手角度：把 `rewrite_path()` 当成**攻击面**，逐个 payload 打，并要求**每一条
非法的都必须抛 `path_escape`**；同时用「白盒 + 黑盒」双重确认仿写域没有任何一处
绕过 `rewrite_path()` 直接拼路径。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.novel_rewrite_store import (
    REWRITE_PREFIX,
    RewriteStore,
    in_rewrite,
)
from app.services.novel_store import (
    ERR_PATH_ESCAPE,
    NovelStore,
    NovelValidationError,
)

# 攻击 payload：全部**必须**被拒
ESCAPE_PAYLOADS: tuple[str, ...] = (
    "../book.json",
    "..\\book.json",
    "../state.json",
    "../../book.json",
    "..\\..\\book.json",
    "rewrite/../../book.json",
    "rewrite/..\\..\\state.json",
    "/abs/path.md",
    "\\abs\\path.md",
    "C:\\Windows\\win.ini",
    "C:/Windows/win.ini",
    "D:/evil.md",
    "book.json",
    "state.json",
    "正文/ch-001.md",
    "checkpoints/job-x.json",
    "views/outline.md",
    "../books/bk-other/book.json",
    "..",
    "../",
    "./../evil.md",
    "a/../../../evil.md",
    "rewrite/../../正文/ch-001.md",
    "rewrite/../book.json",
    "\u4e2d\u6587/../book.json",  # 中文目录后穿越
    "drafts/../../../book.json",
    "reports/../../state.json",
    "..%2fbook.json",  # 编码不算逃逸，但也不能落在 rewrite/ 外
)

# 合法 payload（`rewrite_path()` 的入参相对 **book_dir**，必须带 `rewrite/` 前缀）
OK_PAYLOADS: tuple[str, ...] = (
    "rewrite/blueprint.json",
    "rewrite/drafts/rw-x.md",
    "rewrite/reports/rw-x.json",
    "rewrite/drafts/nested/deep.md",
    "rewrite/中文名.md",
    "rewrite/drafts/rw-名字-20260101010101-abcd.md",
    "rewrite/" + "a" * 120 + ".md",  # 超长文件名
    "rewrite/drafts/emoji-\U0001f600.md",  # emoji
    "rewrite/drafts/全角标点·。！？.md",
    "rewrite/drafts/  .md",  # 文件名含空格
    "rewrite/drafts/尾换行\n.md".strip(),  # 尾换行
)


@pytest.fixture()
def store(tmp_path: Path) -> NovelStore:
    """临时根目录的 `NovelStore`。"""
    return NovelStore(root=tmp_path)


# ═══════════════════════ 黑盒：rewrite_path 逐 payload ═══════════════════════


@pytest.mark.parametrize("payload", ESCAPE_PAYLOADS)
def test_rewrite_path_rejects_escape(store: NovelStore, payload: str) -> None:
    """★硬断言★ 任一逃逸 payload → `path_escape`（422），绝不返回可用 Path。"""
    with pytest.raises(NovelValidationError) as info:
        store.rewrite_path("bk-qa", payload)
    assert info.value.code == ERR_PATH_ESCAPE, f"{payload!r} 没有被判为 path_escape"


@pytest.mark.parametrize("payload", OK_PAYLOADS)
def test_rewrite_path_accepts_legal_and_confines(store: NovelStore, payload: str) -> None:
    """合法 payload 必须放行，且解析后**真的**位于 `rewrite/` 内。"""
    path = store.rewrite_path("bk-qa", payload)
    root = store.rewrite_dir("bk-qa").resolve()
    resolved = path.resolve()
    assert root == resolved or root in resolved.parents, f"{payload!r} 落在了 rewrite/ 外"


def test_rewrite_path_is_isomorphic_to_chapter_path(store: NovelStore) -> None:
    """`rewrite_path()` 与 `chapter_path()` 同构：**真逃逸** payload 的处置必须一致。

    （`正文/ch-001.md` 对 `chapter_path` 是合法的、对 `rewrite_path` 是非法的，
    不能拿来比 —— 两者只是「圈定的目录」不同，拦截强度必须相同。）
    """
    for payload in ("../book.json", "book.json", "state.json", "C:/x", "../.."):
        with pytest.raises(NovelValidationError):
            store.rewrite_path("bk-qa", payload)
        with pytest.raises(NovelValidationError):
            store.chapter_path("bk-qa", payload)


def test_rewrite_path_error_codes_match_chapter_path(store: NovelStore) -> None:
    """两者抛出的错误码必须同为 `path_escape`（口径漂移检测）。"""
    codes = set()
    for payload in ("../book.json", "book.json"):
        try:
            store.rewrite_path("bk-qa", payload)
        except NovelValidationError as exc:
            codes.add(exc.code)
        try:
            store.chapter_path("bk-qa", payload)
        except NovelValidationError as exc:
            codes.add(exc.code)
    assert codes == {ERR_PATH_ESCAPE}, codes


def test_sibling_prefix_directory_is_not_confused(store: NovelStore) -> None:
    """`rewrite-extra/` 不能因为「前缀相同」被当成 `rewrite/` 内的路径。

    `Path.parents` 是按**路径分段**比较的，`rewrite-extra/x` 的父是 `books/<id>`，
    不在 `rewrite/` 内 —— 这条用来防「字符串 startswith」式的假校验。
    """
    with pytest.raises(NovelValidationError):
        store.rewrite_path("bk-qa", "../rewrite-extra/x.md")


# ═══════════════════════ in_rewrite 归一 ═══════════════════════


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("drafts/a.md", "rewrite/drafts/a.md"),
        ("/drafts/a.md", "rewrite/drafts/a.md"),
        ("rewrite/drafts/a.md", "rewrite/drafts/a.md"),
        ("", "rewrite/"),
        ("  drafts/a.md  ", "rewrite/drafts/a.md"),
    ],
)
def test_in_rewrite_normalizes(rel: str, expected: str) -> None:
    """`in_rewrite()` 必须稳定补 `rewrite/` 前缀（不重复、不漏）。"""
    assert in_rewrite(rel) == expected


def test_in_rewrite_does_not_sanitize_dotdot_but_path_layer_does(store: NovelStore) -> None:
    """`in_rewrite()` 只补前缀；真正的拦截在 `rewrite_path()`（分层防漏）。

    这条是**回归护栏**：如果哪天有人把 `in_rewrite` 改成「顺便清掉 ..」，
    反而会掩盖「上层没有走校验」的 bug —— 所以这里显式确认分工不变。
    """
    assert in_rewrite("../../evil.md") == "rewrite/../../evil.md"
    with pytest.raises(NovelValidationError):
        store.rewrite_path("bk-qa", in_rewrite("../../evil.md"))
    assert REWRITE_PREFIX == "rewrite/"


# ═══════════════════════ RewriteStore 各写入口 ═══════════════════════


@pytest.mark.parametrize(
    "factory",
    [
        lambda rs: rs.draft_path("bk-qa", "../../book.json"),
        lambda rs: rs.draft_path("bk-qa", "C:/Windows/win.ini"),
        lambda rs: rs.draft_path("bk-qa", "正文/ch-001.md"),
        lambda rs: rs.report_path("bk-qa", "../state.json"),
        lambda rs: rs.report_path("bk-qa", "/abs.json"),
        lambda rs: rs.blueprint_path("bk-qa"),  # 合法，作为对照
    ],
)
def test_store_path_helpers_respect_boundary(tmp_path: Path, factory) -> None:
    """`RewriteStore` 的每个路径出口都要过校验（blueprint 那条是合法对照）。"""
    rs = RewriteStore(NovelStore(root=tmp_path))
    try:
        path = factory(rs)
    except NovelValidationError as exc:
        assert exc.code == ERR_PATH_ESCAPE
        return
    assert rs.rewrite_dir("bk-qa").resolve() in path.resolve().parents


def test_read_rewrite_draft_rejects_escape(tmp_path: Path) -> None:
    """`read_rewrite_draft()` 的入参来自报告里的 `draft_file`（可被篡改）→ 必须校验。"""
    rs = RewriteStore(NovelStore(root=tmp_path))
    (rs.rewrite_dir("bk-qa") / "drafts").mkdir(parents=True, exist_ok=True)
    rs.write_rewrite_draft("bk-qa", "rw-bk-qa-20260101010101-abcd", "chapter", "正文")
    from app.services.novel_store import NovelNotFound

    for payload in ("../../../book.json", "/abs/x.md", "C:/Windows/win.ini", "..\\..\\state.json"):
        # 要么判成 path_escape，要么被圈进 rewrite/ 后 404 —— 绝不能返回内容
        with pytest.raises((NovelValidationError, NovelNotFound)) as info:
            rs.read_rewrite_draft("bk-qa", payload)
        if isinstance(info.value, NovelValidationError):
            assert info.value.code == ERR_PATH_ESCAPE, payload


def test_read_rewrite_draft_never_leaks_authoritative_files(tmp_path: Path) -> None:
    """★硬断言★ 哪怕传 `book.json` / `state.json`，也**绝不会**读到权威 JSON 的内容。

    `in_rewrite()` 会把它们圈进 `rewrite/` 内（不存在 → 404），
    而不是读到真正的外层 `book.json`。
    """
    rs = RewriteStore(NovelStore(root=tmp_path))
    book = rs.base.create_book("QA 路径书")
    secret = rs.base.book_dir(book.id).joinpath("book.json").read_text(encoding="utf-8")
    for payload in ("book.json", "state.json", "正文/ch-001.md"):
        try:
            body = rs.read_rewrite_draft(book.id, payload)
        except Exception:
            continue  # 抛错（404 / 422）都算安全
        assert body != secret, f"{payload} 竟然读到了真正的 book.json"
        assert '"version"' not in body or "QA 路径书" not in body


def test_tampered_draft_file_in_report_cannot_read_book_json(tmp_path: Path) -> None:
    """篡改报告里的 `draft_file` 指向 `book.json` → 采纳时必须炸，不能静默读到权威 JSON。"""
    from app.services.novel_rewrite_store import build_report
    from app.services.novel_store import NovelNotFound

    rs = RewriteStore(NovelStore(root=tmp_path))
    book = rs.base.create_book("QA 书")
    report = build_report(
        rewrite_id="rw-bk-qa-20260101010101-abcd",
        blueprint=rs.load_blueprint(book.id),
        kind="chapter",
        draft_file="../../book.json",
        chapter_id=None,
    )
    rs.save_report(book.id, report)
    with pytest.raises(NovelValidationError) as info:
        rs.read_rewrite_draft(book.id, report.draft_file)
    assert info.value.code == ERR_PATH_ESCAPE
    # 对照：合法的相对路径能读回来
    rs.write_rewrite_draft(book.id, report.rewrite_id, "chapter", "正文内容")
    assert rs.read_rewrite_draft(book.id, f"drafts/{report.rewrite_id}.md") == "正文内容"
    # 不存在 → 404 语义
    with pytest.raises(NovelNotFound):
        rs.read_rewrite_draft(book.id, "drafts/nope.md")


def test_rewrite_dir_layout(tmp_path: Path) -> None:
    """`rewrite/` 必须是 `books/<id>/rewrite/`。"""
    rs = RewriteStore(NovelStore(root=tmp_path))
    assert rs.rewrite_dir("bk-qa") == rs.base.book_dir("bk-qa") / "rewrite"


# ═══════════════════════ 白盒：不得绕过 rewrite_path ═══════════════════════


def test_no_raw_write_bypasses_rewrite_path() -> None:
    """★白盒★ 仿写三个新模块里，写操作必须只有 `atomic_write_*`（经 rewrite_path）。

    允许出现 `open(`（读）、`read_text`（读）；**不允许**出现 `write_text(` /
    `open(..., "w")` / `os.replace(` / `shutil.move(` 这类直写。
    """
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    targets = [
        root / "novel_rewrite_store.py",
        root / "novel_rewrite_ai.py",
        root / "novel_rewrite_jobs.py",
    ]
    forbidden = re.compile(r"\.write_text\(|open\([^)]*['\"][wa]|os\.replace\(|shutil\.move\(|unlink\(|rmtree\(")
    for target in targets:
        text = target.read_text(encoding="utf-8")
        offenders = [
            f"{index}:{line.strip()}"
            for index, line in enumerate(text.splitlines(), start=1)
            if forbidden.search(line) and not line.strip().startswith("#")
        ]
        assert not offenders, f"{target.name} 出现了绕过 rewrite_path 的直写: {offenders}"


def test_every_write_call_site_uses_rewrite_path_or_atomic() -> None:
    """仿写域的全部写入调用点：`atomic_write_json` / `atomic_write_text`。"""
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    text = (root / "novel_rewrite_store.py").read_text(encoding="utf-8")
    writes = re.findall(r"(atomic_write_(?:json|text))\(", text)
    assert writes, "一个写入点都没找到（正则写错了？）"
    assert set(writes) <= {"atomic_write_json", "atomic_write_text"}
    # 每个写入点都要能追溯到 rewrite_path / checkpoints_dir
    assert "rewrite_path(" in text
    assert "rewrite_job_path(" in text


def test_rewrite_job_path_confines_to_checkpoints(tmp_path: Path) -> None:
    """`rw-` job 的 checkpoint 必须落在 `checkpoints/` 内（不能借 job_id 越界）。"""
    rs = RewriteStore(NovelStore(root=tmp_path))
    job_id = "rw-bk-qa-20260101010101-abcd"
    path = rs.rewrite_job_path(job_id)
    assert path.parent == rs.base.checkpoints_dir("bk-qa")
    assert path.name == f"{job_id}.json"

    for bad in ("rw-bk-qa-20260101010101-abcd/../../evil", "rw-..-20260101010101-abcd"):
        with pytest.raises(NovelValidationError):
            rs.rewrite_job_path(bad)


def test_rewrite_job_path_rejects_non_rewrite_prefix(tmp_path: Path) -> None:
    """`job-` 前缀（既有续写 job）不能混进仿写通道。"""
    rs = RewriteStore(NovelStore(root=tmp_path))
    with pytest.raises(NovelValidationError):
        rs.rewrite_job_path("job-bk-qa-20260101010101-abcd")
