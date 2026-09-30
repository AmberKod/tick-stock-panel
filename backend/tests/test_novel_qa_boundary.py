"""QA 独立补充测试（一）：数据层边界 —— 空值 / 超长 / 编码 / 换行 / 导出 / 幂等。

由 QA（严过关）编写。刻意避开工程师 `test_novel_store.py` 已覆盖的角度，专打：

- 书名空串 / 纯空白 / 边界长度（200 字 vs 201 字）
- 章节标题为空串时的 filename slug 退化
- 正文 CRLF 与「末尾无换行」的字节级保留（P0-4③）
- Unicode / emoji 书名与正文往返
- 20 万字超长正文往返
- `setting_summary` 清空为空串时不应顺手抹掉书名
- 未知字段拒绝（422 invalid_payload）
- 导出：单章 md 字节 == 磁盘原文、空书导出、txt 标记剥离、全书顺序按 `order` 排序
- 派生视图连续 5 次 rebuild 字节一致（幂等）+ 删除 views/ 后重建一致
- 重复采纳的行为必须明确（内容幂等 / 选区替换二次应显式报错）
- state.json 落盘键名必须是 from / to

自包含：无 conftest，用 `NovelStore(root=tmp_path)` 注入。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.novel_store import (
    VIEW_NAMES,
    ChapterFact,
    NovelStore,
    NovelValidationError,
    RelationDelta,
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


def _book(store: NovelStore) -> str:
    book = store.create_book("边界测试书")
    store.save_outline(book.id, book.version, NODES)
    return book.id


# ─────────────────────────── 书名边界 ───────────────────────────


@pytest.mark.parametrize("title", ["", "   ", "\t\n", "　"])
def test_create_book_rejects_blank_title(tmp_path: Path, title: str) -> None:
    """空串 / 纯空白书名必须被拒（否则磁盘上会出现无法区分的书）。"""
    store = NovelStore(root=tmp_path)
    with pytest.raises(NovelValidationError) as excinfo:
        store.create_book(title)
    assert excinfo.value.code == "invalid_title"


def test_create_book_title_length_boundary(tmp_path: Path) -> None:
    """200 字放行、201 字拒绝（边界值 ±1）。"""
    store = NovelStore(root=tmp_path)
    assert store.create_book("书" * 200).title == "书" * 200
    with pytest.raises(NovelValidationError) as excinfo:
        store.create_book("书" * 201)
    assert excinfo.value.code == "invalid_title"


def test_create_book_accepts_unicode_and_emoji_title(tmp_path: Path) -> None:
    """emoji + 中文书名落盘后必须能原样读回（UTF-8 全链路）。"""
    store = NovelStore(root=tmp_path)
    title = "📚 星海黎明 🌌 · 卷二"
    book = store.create_book(title)
    assert store.get_book(book.id).title == title
    assert store.list_books()[0]["title"] == title


# ─────────────────────────── 章节标题空串 ───────────────────────────


def test_empty_chapter_title_falls_back_to_default_slug(tmp_path: Path) -> None:
    """章节标题为空串时 slug 退化为 `ch`，文件名仍按序号递增且不冲突。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-001", "type": "chapter", "title": "引子", "order": 1},
                {"id": "ch-002", "type": "chapter", "title": "", "order": 2},
                {"id": "ch-003", "type": "chapter", "title": "🚀", "order": 3},
            ],
        }
    ]
    book = store.get_book(book_id)
    saved = store.save_outline(book_id, book.version, nodes)
    files = {c.id: c.file for v in saved.outline.nodes for c in v.children}
    assert files["ch-002"].endswith("ch-002-ch.md"), files
    assert files["ch-003"].endswith("ch-003-ch.md"), files
    # 三个文件都已真实落盘
    for rel in files.values():
        assert store.chapter_path(book_id, rel).exists()


# ─────────────────────────── 正文字节级保留 ───────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "第一行\r\n第二行\r\n第三行",          # CRLF，末尾无换行
        "a\nb\nc",                              # LF，末尾无换行
        "末尾有换行\n",
        "\n\n\n",                               # 只有换行
        "混合\r\n与\n换行\r",                    # 混用
    ],
)
def test_chapter_body_preserves_newlines_byte_for_byte(tmp_path: Path, body: str) -> None:
    """P0-4③：Markdown 原文不做任何转码/重排，字节级保留用户换行习惯。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    store.write_chapter(book_id, "ch-001", body)
    raw = store.chapter_path(book_id, "正文/ch-001-ch.md").read_bytes()
    assert raw.decode("utf-8") == body
    assert store.read_chapter(book_id, "ch-001")[0] == body


def test_chapter_body_preserves_unicode_and_emoji(tmp_path: Path) -> None:
    """emoji / 生僻字 / 组合字符往返一致。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    body = "陆昭👨‍🚀打开了黑匣子\u200b——「𠮷野」在雨夜里说：\"走。\"\n\n…"
    store.write_chapter(book_id, "ch-001", body)
    assert store.read_chapter(book_id, "ch-001")[0] == body


def test_very_long_chapter_body_round_trips(tmp_path: Path) -> None:
    """20 万字超长正文：写入 + 读回一致，且字数统计不爆。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    body = ("星海历三零二年，陆昭在废弃船坞里发现了那只黑匣子。" * 5000)[:200_000]
    store.write_chapter(book_id, "ch-001", body)
    assert store.read_chapter(book_id, "ch-001")[0] == body
    assert store.list_chapters(book_id)[0]["word_count"] == len(body)


# ─────────────────────────── 书籍元数据局部更新 ───────────────────────────


def test_setting_summary_can_be_cleared_without_touching_title(tmp_path: Path) -> None:
    """只清空设定摘要时，书名/作者等其它字段必须保持原值（不清空）。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    store.update_book_meta(book_id, {"author": "许清楚", "setting_summary": "星海历 302 年"})
    store.update_book_meta(book_id, {"setting_summary": ""})
    payload = store.get_book_meta(book_id)
    assert payload["setting_summary"] == ""
    assert payload["title"] == "边界测试书"
    assert payload["author"] == "许清楚"


def test_update_book_meta_rejects_unknown_field(tmp_path: Path) -> None:
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    with pytest.raises(NovelValidationError) as excinfo:
        store.update_book_meta(book_id, {"version": 99})
    assert excinfo.value.code == "invalid_payload"


def test_update_book_meta_rejects_oversized_setting_summary(tmp_path: Path) -> None:
    """防止误粘贴整本书把 book.json 撑爆。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    with pytest.raises(NovelValidationError) as excinfo:
        store.update_book_meta(book_id, {"setting_summary": "字" * 4001})
    assert excinfo.value.code == "invalid_payload"


# ─────────────────────────── 导出 ───────────────────────────


def test_export_chapter_md_equals_disk_bytes(tmp_path: Path) -> None:
    """P0-14①：单章 md 导出字节等于磁盘原文（含 CRLF）。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    body = "# 引子\r\n\r\n**加粗** 与 `代码`\r\n"
    store.write_chapter(book_id, "ch-001", body)
    _name, content = store.export_chapter(book_id, "ch-001", "md")
    assert content.encode("utf-8") == store.chapter_path(book_id, "正文/ch-001-ch.md").read_bytes()


def test_export_chapter_txt_strips_inline_markers(tmp_path: Path) -> None:
    """P0-14③：txt 导出中不得残留 `#` / `**` / 反引号 / 纯分割线。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    body = "# 引子\n\n**加粗** 与 `代码` 混排\n\n> 引用一句\n\n- 列表项\n\n---\n\n收尾。\n"
    store.write_chapter(book_id, "ch-001", body)
    _name, content = store.export_chapter(book_id, "ch-001", "txt")
    assert "#" not in content
    assert "**" not in content
    assert "`" not in content
    assert "---" not in content
    assert "加粗" in content and "代码" in content and "列表项" in content
    assert content.splitlines()[0] == "引子"


def test_export_book_follows_outline_order_not_insertion_order(tmp_path: Path) -> None:
    """P0-14②：全书顺序由大纲 `order` 决定，而非 nodes 数组的插入顺序。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("顺序之书")
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-c", "type": "chapter", "title": "第三章", "order": 3},
                {"id": "ch-a", "type": "chapter", "title": "第一章", "order": 1},
                {"id": "ch-b", "type": "chapter", "title": "第二章", "order": 2},
            ],
        },
        {
            "id": "v2",
            "type": "volume",
            "title": "第二卷",
            "order": 2,
            "children": [
                {"id": "ch-d", "type": "chapter", "title": "第四章", "order": 1},
            ],
        },
    ]
    saved = store.save_outline(book.id, book.version, nodes)
    files = {c.id: c.file for v in saved.outline.nodes for c in v.children}
    for cid in files:
        store.write_chapter(book.id, cid, f"正文-{cid}")
    _name, content = store.export_book(book.id, "md")
    order = [
        line[len("## "):]
        for line in content.splitlines()
        if line.startswith("## ")
    ]
    assert order == ["第一章", "第二章", "第三章", "第四章"], order


def test_context_card_recent_facts_follow_outline_order(tmp_path: Path) -> None:
    """P2-4：上下文卡「最近 2 章」必须按大纲 `(卷序, 章序)` 取，不能按 id 字符串排。

    构造 id 位数不齐的章节（`ch-9` / `ch-10` / `ch-11`）：字符串排序会得出
    `"ch-10" < "ch-11" < "ch-9"`，于是"最近 2 章"错取成 ch-11 + ch-9。
    标记放在 relations.delta 里 —— 滚动摘要不会摘录 relations，不会串味。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("顺序书")
    nodes = [
        {
            "id": "v1",
            "type": "volume",
            "title": "第一卷",
            "order": 1,
            "children": [
                {"id": "ch-9", "type": "chapter", "title": "第九章", "order": 1},
                {"id": "ch-10", "type": "chapter", "title": "第十章", "order": 2},
                {"id": "ch-11", "type": "chapter", "title": "第十一章", "order": 3},
            ],
        }
    ]
    saved = store.save_outline(book.id, book.version, nodes)
    assert [c.id for v in saved.outline.nodes for c in v.children] == ["ch-9", "ch-10", "ch-11"]
    for cid in ("ch-9", "ch-10", "ch-11"):
        store.ingest_facts(
            book.id,
            cid,
            ChapterFact(relations=[RelationDelta(source="甲", target="乙", delta=f"标记-{cid}")]),
        )
    card = store.build_context_card(book.id, "ch-11")
    assert "标记-ch-10" in card
    assert "标记-ch-11" in card
    assert "标记-ch-9" not in card, "最近 2 章取错了（按 id 字符串排序而非大纲顺序）"


def test_export_empty_book_does_not_crash(tmp_path: Path) -> None:
    """零章书导出：不抛异常，md 无正文章节、txt 至少保留书名。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("空书")
    store.save_outline(book.id, book.version, [])
    _name, md = store.export_book(book.id, "md")
    assert md == ""          # P2-5：零章书 md 导出是空串，不再是一个孤立的换行
    _name2, txt = store.export_book(book.id, "txt")
    assert txt.strip() == "空书"


# ─────────────────────────── 派生视图幂等 ───────────────────────────


def _seed_state(store: NovelStore, book_id: str) -> None:
    store.ingest_facts(
        book_id,
        "ch-001",
        ChapterFact(
            title="引子",
            chars=["陆昭", "白露"],
            state_changes=["陆昭捡到黑匣子"],
            planted=["黑匣子的真实来源"],
            relations=[RelationDelta(source="陆昭", target="白露", delta="初识")],
            source="manual",
        ),
    )


def test_rebuild_views_is_byte_identical_across_five_runs(tmp_path: Path) -> None:
    """P0-11②：同一权威 JSON 连续 5 次 rebuild，输出字节必须完全一致。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    _seed_state(store, book_id)
    store.rebuild_views(book_id)

    def snapshot() -> dict[str, bytes]:
        return {
            name: (store.views_dir(book_id) / f"{name}.md").read_bytes() for name in VIEW_NAMES
        }

    baseline = snapshot()
    assert all(baseline.values()), "视图不应为空"
    for _ in range(4):
        store.rebuild_views(book_id)
        assert snapshot() == baseline


def test_rebuild_views_after_deleting_views_dir(tmp_path: Path) -> None:
    """P0-11①：删除 views/ 后可完整重建，且与删除前字节一致。"""
    import shutil

    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    _seed_state(store, book_id)
    store.rebuild_views(book_id)
    before = {n: (store.views_dir(book_id) / f"{n}.md").read_text(encoding="utf-8") for n in VIEW_NAMES}
    shutil.rmtree(store.views_dir(book_id))
    assert not store.views_dir(book_id).exists()
    store.rebuild_views(book_id)
    after = {n: (store.views_dir(book_id) / f"{n}.md").read_text(encoding="utf-8") for n in VIEW_NAMES}
    assert after == before
    # read_view 在文件缺失时会自行重建（派生数据可随时再生）
    assert store.read_view(book_id, "characters") == before["characters"]


def test_state_json_persists_from_to_keys(tmp_path: Path) -> None:
    """磁盘与 HTTP 的 relations 键名必须是 from / to（PRD §5.3 逐字一致）。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    _seed_state(store, book_id)
    payload = json.loads((store.book_dir(book_id) / "state.json").read_text(encoding="utf-8"))
    relation = payload["chapters"][0]["relations"][0]
    assert set(relation) == {"from", "to", "delta"}, relation
    assert relation["from"] == "陆昭" and relation["to"] == "白露"


# ─────────────────────────── 重复采纳 ───────────────────────────


def test_repeated_adopt_is_content_idempotent(tmp_path: Path) -> None:
    """同一 draft_id 连续采纳两次：正文内容幂等，version 每次 +1（行为明确）。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    store.write_chapter(book_id, "ch-001", "原始正文")
    draft_id = store.write_draft(book_id, "ch-001", "continue", "AI 草稿全文")

    first = store.adopt_draft(book_id, "ch-001", draft_id)
    v1 = store.get_book(book_id).version
    assert store.read_chapter(book_id, "ch-001")[0] == "AI 草稿全文"
    assert first["chapter"]["status"] == "published"

    second = store.adopt_draft(book_id, "ch-001", draft_id)
    v2 = store.get_book(book_id).version
    assert store.read_chapter(book_id, "ch-001")[0] == "AI 草稿全文"
    assert second["chapter"]["status"] == "published"
    assert v2 == v1 + 1


def test_second_adopt_with_same_selection_reports_explicit_error(tmp_path: Path) -> None:
    """选区间已被替换后再次采纳同一选区：必须显式报错，不能静默覆盖。"""
    store = NovelStore(root=tmp_path)
    book_id = _book(store)
    store.write_chapter(book_id, "ch-001", "AAA 中间 BBB")
    draft_id = store.write_draft(book_id, "ch-001", "polish", "替换后的片段")
    first = store.adopt_draft(book_id, "ch-001", draft_id, selection="AAA")
    assert first["replaced_selection"] is True
    assert store.read_chapter(book_id, "ch-001")[0] == "替换后的片段 中间 BBB"

    with pytest.raises(NovelValidationError) as excinfo:
        store.adopt_draft(book_id, "ch-001", draft_id, selection="AAA")
    assert excinfo.value.code == "selection_required"
    assert store.read_chapter(book_id, "ch-001")[0] == "替换后的片段 中间 BBB"
