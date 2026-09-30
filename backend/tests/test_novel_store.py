"""小说工作区 — 文件系统事实层单测（自包含：无 conftest，用 tmp_path 注入）。

覆盖 ARCHITECTURE.md T01 完成判据：
建书产物 / 原子写并发 100 次 / 手工改 md 可见 / 路径穿越拒绝 /
version 乐观锁 / 视图重建幂等 / txt 导出无标记残留 / 模型字段名快照。
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from app.services.novel_store import (
    CHAPTERS_DIR,
    BookMeta,
    BookState,
    ChapterFact,
    LintHit,
    NovelJob,
    NovelStore,
    NovelValidationError,
    OutlineChapter,
    RelationDelta,
    count_words,
    parse_job_id,
    slugify,
    validate_id,
    validate_rel_path,
)

# ─────────────────────────── 脚手架 ───────────────────────────

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


def _seed(tmp_path: Path) -> tuple[NovelStore, str]:
    """建一本带两章的书，返回 (store, book_id)。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("星海黎明")
    store.save_outline(book.id, book.version, _NODES)
    return store, book.id


def _write_text(path: Path, text: str) -> None:
    """手写文本（不做换行翻译，模拟用户用记事本编辑）。"""
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


# ─────────────────────────── 目录结构 ───────────────────────────


def test_create_book_produces_expected_layout(tmp_path: Path) -> None:
    """P0-1①：建书后磁盘出现 book.json + 正文/ + state.json + views/。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("星海黎明")

    book_dir = store.book_dir(book.id)
    assert (book_dir / "book.json").exists()
    assert (book_dir / "state.json").exists()
    assert (book_dir / CHAPTERS_DIR).is_dir()
    assert (book_dir / "views").is_dir()
    assert (book_dir / "views" / "context-card.md").exists()
    assert (book_dir / "views" / "timeline.md").exists()
    assert (book_dir / "views" / "characters.md").exists()
    assert book.version == 1
    assert book.id.startswith("book-")


def test_create_book_rejects_empty_title(tmp_path: Path) -> None:
    store = NovelStore(root=tmp_path)
    with pytest.raises(NovelValidationError) as exc:
        store.create_book("   ")
    assert exc.value.code == "invalid_title"


# ─────────────────────────── 原子写 ───────────────────────────


def test_concurrent_writes_keep_file_intact(tmp_path: Path) -> None:
    """P0-1②：并发写同一章节 100 次，文件不损坏、内容等于最后一次写入。"""
    store, book_id = _seed(tmp_path)
    errors: list[BaseException] = []
    expected: set[str] = set()

    def worker(index: int) -> None:
        text = f"内容-{index}-" + "字" * index
        expected.add(text)
        try:
            store.write_chapter(book_id, "ch-001", text)
        except Exception as exc:  # 收集线程内异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 101)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    content, _path = store.read_chapter(book_id, "ch-001")
    assert content in expected, "并发写结果必须是某一次完整写入，不能是撕裂内容"

    # 主线程再写一次 → 内容必须完全等于这次写入
    store.write_chapter(book_id, "ch-001", "最终内容")
    assert store.read_chapter(book_id, "ch-001")[0] == "最终内容"

    # 原子写不留 .tmp 残留
    leftovers = [p.name for p in store.chapters_dir(book_id).glob(".*")]
    assert leftovers == []


def test_write_chapter_preserves_crlf(tmp_path: Path) -> None:
    """P0-4③：正文不做换行翻译，字节级保留用户习惯（newline=""）。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "第一行\r\n第二行\r\n")
    content, path = store.read_chapter(book_id, "ch-001")
    assert content == "第一行\r\n第二行\r\n"
    assert path.read_bytes() == "第一行\r\n第二行\r\n".encode()


def test_manual_edit_is_visible(tmp_path: Path) -> None:
    """P0-1③：手工编辑 md 后重新读取能读到新内容。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "旧内容")
    path = store.chapter_path(book_id, "正文/ch-001-ch.md")
    _write_text(path, "外部编辑器写入的新内容\n")

    content, _abs = store.read_chapter(book_id, "ch-001")
    assert content == "外部编辑器写入的新内容\n"
    assert store.list_chapters(book_id)[0]["word_count"] == count_words("外部编辑器写入的新内容\n")


# ─────────────────────────── 路径安全 ───────────────────────────


def test_path_traversal_rejected(tmp_path: Path) -> None:
    """P0-2③：`../`、绝对路径、非法 id 全部拒绝。"""
    store, book_id = _seed(tmp_path)

    with pytest.raises(NovelValidationError) as exc:
        store.chapter_path(book_id, "../evil.md")
    assert exc.value.code == "path_escape"

    with pytest.raises(NovelValidationError):
        store.chapter_path(book_id, "/etc/passwd")

    with pytest.raises(NovelValidationError):
        store.chapter_path(book_id, "正文/../../evil.md")

    for bad in ("../x", "Book", "book_x", "", "a/b", "中文"):
        with pytest.raises(NovelValidationError):
            validate_id(bad)

    assert validate_id("book-2026-001") == "book-2026-001"
    with pytest.raises(NovelValidationError):
        validate_rel_path("", tmp_path)


# ─────────────────────────── 大纲 ───────────────────────────


def test_outline_version_conflict(tmp_path: Path) -> None:
    """乐观锁：version 不符 → NovelConflict（HTTP 409）。"""
    from app.services.novel_store import NovelConflict

    store, book_id = _seed(tmp_path)
    book = store.get_book(book_id)
    with pytest.raises(NovelConflict):
        store.save_outline(book_id, book.version - 1, _NODES)
    # 正确 version 可写，且 version 自增
    updated = store.save_outline(book_id, book.version, _NODES)
    assert updated.version == book.version + 1


def test_save_outline_creates_and_deletes_chapter_files(tmp_path: Path) -> None:
    """新增章节建空 md；删除章节连带删 md（仅删本书 正文/ 内的文件）。"""
    store, book_id = _seed(tmp_path)
    book = store.get_book(book_id)
    files = {c.id: c.file for v in book.outline.nodes for c in v.children}
    assert files["ch-001"] and files["ch-002"]
    assert store.chapter_path(book_id, files["ch-002"]).exists()

    # 新增 ch-003，删除 ch-001
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷 破晓",
            "order": 1,
            "children": [
                {
                    "id": "ch-002",
                    "type": "chapter",
                    "title": "雨夜",
                    "order": 1,
                    "file": files["ch-002"],
                },
                {"id": "ch-003", "type": "chapter", "title": "密会", "order": 2},
            ],
        }
    ]
    updated = store.save_outline(book_id, book.version, nodes)
    new_files = {c.id: c.file for v in updated.outline.nodes for c in v.children}
    assert new_files["ch-002"] == files["ch-002"], "已有章节的文件名永不随排序变化（A3）"
    assert new_files["ch-003"].startswith(f"{CHAPTERS_DIR}/ch-")
    assert store.chapter_path(book_id, new_files["ch-003"]).exists()
    assert not store.chapter_path(book_id, files["ch-001"]).exists()


# ─────────────────────────── 追踪态 ───────────────────────────


def test_relations_disk_keys_are_from_to(tmp_path: Path) -> None:
    """A2：磁盘 JSON 键名必须是 from/to（与 PRD §5.3 逐字一致）。"""
    store, book_id = _seed(tmp_path)
    fact = ChapterFact(
        chars=["陆昭", "白露"],
        state_changes=["陆昭从健康→轻伤住院"],
        planted=["黑匣子的真实来源"],
        resolved=[],
        relations=[RelationDelta(source="陆昭", target="白露", delta="信任开始动摇")],
        source="manual",
    )
    store.ingest_facts(book_id, "ch-001", fact)

    raw = json.loads((store.book_dir(book_id) / "state.json").read_text(encoding="utf-8"))
    relation = raw["chapters"][0]["relations"][0]
    assert relation["from"] == "陆昭"
    assert relation["to"] == "白露"
    assert "source" not in relation and "target" not in relation

    state = store.get_state(book_id)
    assert state.chapters[0].relations[0].source == "陆昭"
    assert state.foreshadow[0].id == "f-001"
    assert state.rolling.updated_chapter == "ch-001"
    assert set(state.characters) == {"陆昭", "白露"}


def test_ingest_resolves_foreshadow(tmp_path: Path) -> None:
    """回收伏笔：按 id 或原文标记 resolved。"""
    store, book_id = _seed(tmp_path)
    store.ingest_facts(
        book_id,
        "ch-001",
        ChapterFact(chars=["陆昭"], planted=["黑匣子的真实来源"]),
    )
    state = store.ingest_facts(book_id, "ch-002", ChapterFact(resolved=["f-001"]))
    resolved = [item for item in state.foreshadow if item.id == "f-001"]
    assert resolved[0].status == "resolved"
    assert resolved[0].resolved_chapter == "ch-002"


# ─────────────────────────── 派生视图 ───────────────────────────


def test_rebuild_views_is_idempotent(tmp_path: Path) -> None:
    """P0-11②：同一权威 JSON 连续两次 rebuild 输出字节一致。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "雨下了整夜。")
    store.ingest_facts(
        book_id,
        "ch-001",
        ChapterFact(chars=["陆昭"], state_changes=["陆昭捡到黑匣子"], planted=["黑匣子来源"]),
    )

    files = store.rebuild_views(book_id)
    assert files == ["characters.md", "context-card.md", "timeline.md"]
    first = {name: (store.views_dir(book_id) / name).read_bytes() for name in files}

    store.rebuild_views(book_id)
    second = {name: (store.views_dir(book_id) / name).read_bytes() for name in files}
    assert first == second

    # 删掉 views/ 后重建，字节仍一致（可删可重建）
    shutil.rmtree(store.views_dir(book_id))
    store.rebuild_views(book_id)
    third = {name: (store.views_dir(book_id) / name).read_bytes() for name in files}
    assert third == first


def test_context_card_contains_beat_and_open_foreshadow(tmp_path: Path) -> None:
    """上下文卡必须含本章节拍与未收伏笔（P0-8②的数据前提）。"""
    store, book_id = _seed(tmp_path)
    store.ingest_facts(
        book_id, "ch-001", ChapterFact(chars=["陆昭"], planted=["黑匣子的真实来源"])
    )
    card = store.build_context_card(book_id, "ch-002")
    assert "陆昭受伤并与白露关系转冷" in card
    assert "黑匣子的真实来源" in card
    assert "雨夜追击中黑匣子被夺走" in card


# ─────────────────────────── 草稿 / 采纳 ───────────────────────────


def test_adopt_draft_writes_body_and_publishes(tmp_path: Path) -> None:
    """P0-6②：采纳后正文 = 草稿全文，status = published。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "原始正文")
    draft_id = store.write_draft(book_id, "ch-001", "continue", "AI 生成的草稿正文")

    result = store.adopt_draft(book_id, "ch-001", draft_id)
    assert store.read_chapter(book_id, "ch-001")[0] == "AI 生成的草稿正文"
    assert result["chapter"]["status"] == "published"

    # 润色模式：仅替换选中区间
    store.write_chapter(book_id, "ch-002", "原文AAA原文")
    polish_id = store.write_draft(book_id, "ch-002", "polish", "BBB")
    store.adopt_draft(book_id, "ch-002", polish_id, selection="AAA")
    assert store.read_chapter(book_id, "ch-002")[0] == "原文BBB原文"

    with pytest.raises(NovelValidationError) as exc:
        store.adopt_draft(book_id, "ch-002", polish_id, selection="不存在的片段")
    assert exc.value.code == "selection_required"


def test_draft_does_not_touch_state(tmp_path: Path) -> None:
    """P0-6④：写草稿阶段 state.json 字节不变。"""
    store, book_id = _seed(tmp_path)
    state_path = store.book_dir(book_id) / "state.json"
    before = state_path.read_bytes()
    store.write_draft(book_id, "ch-001", "continue", "草稿内容")
    assert state_path.read_bytes() == before


# ─────────────────────────── 导出 ───────────────────────────


def test_export_chapter_md_equals_disk_bytes(tmp_path: Path) -> None:
    """P0-14①：单章 md 导出字节等于磁盘原文。"""
    store, book_id = _seed(tmp_path)
    body = "# 引子\n\n雨下了整夜，**霓虹**在水面碎成一片。\n\n- 列表项\n"
    store.write_chapter(book_id, "ch-001", body)
    _name, content = store.export_chapter(book_id, "ch-001", "md")
    assert content == body


def test_export_txt_strips_marks(tmp_path: Path) -> None:
    """P0-14③：txt 导出不含 # / ** / ` 残留。"""
    store, book_id = _seed(tmp_path)
    body = "# 引子\n\n**粗体** 与 `代码`\n\n> 引用行\n\n- 列表项\n\n---\n"
    store.write_chapter(book_id, "ch-001", body)
    _name, content = store.export_chapter(book_id, "ch-001", "txt")
    assert "#" not in content
    assert "**" not in content
    assert "`" not in content
    assert "粗体" in content and "代码" in content and "列表项" in content


def test_export_book_follows_outline_order(tmp_path: Path) -> None:
    """P0-14②：全书导出顺序与大纲树一致，卷名作为 # 分隔。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "第一章正文")
    store.write_chapter(book_id, "ch-002", "第二章正文")
    _name, content = store.export_book(book_id, "md")
    assert content.index("第一卷 破晓") < content.index("第一章正文")
    assert content.index("第一章正文") < content.index("第二章正文")


# ─────────────────────────── 纯函数 ───────────────────────────


def test_pure_functions() -> None:
    assert count_words("你好 世界\n\n") == 4
    assert slugify("Rainy Night") == "rainy-night"
    assert slugify("雨夜") == "ch"
    assert parse_job_id("job-book-2026-001-20261002221000-ab12") == (
        "book-2026-001",
        "20261002221000",
    )
    with pytest.raises(NovelValidationError):
        parse_job_id("not-a-job")
    with pytest.raises(NovelValidationError):
        parse_job_id("job-book-1-2026-ab12")


# ─────────────────────────── schema 字段名快照 ───────────────────────────


def test_model_field_snapshot() -> None:
    """§7.4：字段名快照 —— 防止手滑改名导致前后端静默错位。"""
    assert set(BookMeta.model_fields) == {
        "version", "id", "title", "author", "genre", "pov", "tense",
        "setting_summary", "created_at", "updated_at", "outline",
    }
    assert set(BookState.model_fields) == {
        "book_id", "updated_at", "rolling", "characters", "foreshadow", "chapters",
    }
    assert set(NovelJob.model_fields) == {
        "job_id", "book_id", "chapter_id", "mode", "selection", "skip_gate",
        "created_at", "updated_at", "steps", "artifacts", "status", "failed_step",
    }
    assert set(OutlineChapter.model_fields) == {
        "id", "type", "title", "order", "status", "word_target",
        "summary", "beat", "file", "word_count",
    }
    assert set(ChapterFact.model_fields) == {
        "id", "title", "chars", "state_changes", "planted", "resolved",
        "relations", "source", "adopted_at",
    }
    assert set(RelationDelta.model_fields) == {"source", "target", "delta"}
    assert set(LintHit.model_fields) == {"rule", "message", "line", "excerpt"}


# ─────────────────────────── 书籍元数据（meta） ───────────────────────────


def test_get_book_meta_excludes_outline_and_counts_words(tmp_path: Path) -> None:
    """meta 只是"设定摘要卡"的入口：不含大纲树，但带章节数/总字数。"""
    store, book_id = _seed(tmp_path)
    store.write_chapter(book_id, "ch-001", "雨下了整夜。")

    meta = store.get_book_meta(book_id)
    assert set(meta) == {
        "id", "title", "author", "genre", "pov", "tense", "setting_summary",
        "created_at", "updated_at", "chapter_count", "word_count",
    }
    assert meta["title"] == "星海黎明"
    assert meta["chapter_count"] == 2
    assert meta["word_count"] == count_words("雨下了整夜。")
    assert "outline" not in meta


def test_update_book_meta_partial_keeps_other_fields(tmp_path: Path) -> None:
    """只传 setting_summary 时，title / author / pov / tense 一律保持原值、不清空。"""
    store, book_id = _seed(tmp_path)
    before = store.get_book_meta(book_id)

    after = store.update_book_meta(
        book_id, {"setting_summary": "星历 312 年，人类退守星环带。", "genre": "太空歌剧"}
    )
    assert after["setting_summary"] == "星历 312 年，人类退守星环带。"
    assert after["genre"] == "太空歌剧"
    assert after["title"] == before["title"] == "星海黎明"
    assert after["author"] == before["author"] == ""
    assert after["pov"] == before["pov"] == ""
    assert after["tense"] == before["tense"] == ""

    # 确实落盘（不是只改了内存对象）
    assert store.get_book_meta(book_id)["setting_summary"] == "星历 312 年，人类退守星环带。"

    # 空字符串是合法值 —— 允许清掉设定摘要，且不影响其它字段
    cleared = store.update_book_meta(book_id, {"setting_summary": ""})
    assert cleared["setting_summary"] == ""
    assert cleared["genre"] == "太空歌剧"


def test_update_book_meta_rejects_unknown_field_and_invalid_values(tmp_path: Path) -> None:
    """非白名单字段 / 空书名 / 超长文本 → NovelValidationError，且**不改动磁盘**。"""
    store, book_id = _seed(tmp_path)
    with pytest.raises(NovelValidationError):
        store.update_book_meta(book_id, {"outline": []})
    with pytest.raises(NovelValidationError):
        store.update_book_meta(book_id, {"title": "   "})
    with pytest.raises(NovelValidationError):
        store.update_book_meta(book_id, {"setting_summary": "字" * 4001})

    meta = store.get_book_meta(book_id)
    assert meta["title"] == "星海黎明"
    assert meta["setting_summary"] == ""


# ─────────────────────────── 章节文件必须在正文/内（P1-A） ───────────────────────────


def test_chapter_path_rejects_files_outside_chapters_dir(tmp_path: Path) -> None:
    """`chapter_path` 是全部章节读写的唯一入口，必须把 file 锁死在 `正文/` 内。

    否则 `file` 可以指向 state.json / book.json，一次自动保存的原子写就会把
    权威 JSON 覆盖成章节正文。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("正文约束")
    for bad in ("state.json", "book.json", "views/context-card.md", "drafts/x.md"):
        with pytest.raises(NovelValidationError) as excinfo:
            store.chapter_path(book.id, bad)
        assert excinfo.value.code == "path_escape", bad
    # 合法路径不受影响
    assert store.chapter_path(book.id, f"{CHAPTERS_DIR}/ch-001-ch.md").name == "ch-001-ch.md"


# ─────────────────────────── 上下文卡按大纲顺序取最近 N 章（P2-4） ───────────────────────────


def test_context_card_recent_facts_follow_outline_order(tmp_path: Path) -> None:
    """最近 2 章按大纲 order 取，不按 fact.id 字符串排序。

    id 位数不齐时字符串排序会取错：`sorted(["ch-8","ch-9","ch-10"])` →
    ch-10, ch-8, ch-9，最近两章会变成 ch-8/ch-9（漏掉真正最新的 ch-10）。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("顺序上下文")
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-8", "type": "chapter", "title": "第八章", "order": 8},
                {"id": "ch-9", "type": "chapter", "title": "第九章", "order": 9},
                {"id": "ch-10", "type": "chapter", "title": "第十章", "order": 10},
            ],
        }
    ]
    store.save_outline(book.id, book.version, nodes)
    for cid in ("ch-8", "ch-9", "ch-10"):
        store.ingest_facts(book.id, cid, ChapterFact(title=cid, chars=["甲"]))

    card = store.build_context_card(book.id, "ch-10")
    section = card.split("## 四、最近 2 章事实快照")[1].split("## 五")[0]
    assert "ch-10" in section, section
    assert "ch-8" not in section, section
