"""小说工作区 — AI 层单测（自包含：无 conftest，用 tmp_path 注入 store）。

覆盖 ARCHITECTURE.md T02 完成判据：
ai_unavailable 的 fail-closed、上下文真的被喂给模型（含 beat + open 伏笔）、
非法快照不落盘、lint 正负样例、门禁 missing_beat。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import novel_ai
from app.services.novel_store import ChapterFact, NovelStore, RelationDelta

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
    """建一本两章的书，并在 ch-001 埋一条 open 伏笔。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("星海黎明")
    store.save_outline(book.id, book.version, _NODES)
    store.ingest_facts(
        book.id,
        "ch-001",
        ChapterFact(
            chars=["陆昭"],
            state_changes=["陆昭捡到黑匣子"],
            planted=["黑匣子的真实来源"],
            source="manual",
        ),
    )
    return store, book.id


def _stub_ai(monkeypatch: pytest.MonkeyPatch, reply: str) -> dict:
    """把 generate_ai_text 换成返回 reply 的桩，返回捕获容器。"""
    captured: dict = {}

    async def fake(
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        captured["messages"] = messages
        return reply

    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(novel_ai, "generate_ai_text", fake)
    return captured


# ─────────────────────────── AI 可用性 ───────────────────────────


def test_ai_status_unavailable_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配 Key：available=False + code=ai_unavailable + 中文原因（不 mock 生成）。"""
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    monkeypatch.setattr(novel_ai, "current_ai_provider", lambda: "openai_compat")

    status = novel_ai.ai_status()
    assert status.available is False
    assert status.configured is False
    assert status.code == "ai_unavailable"
    assert status.reason and "AI 网关未配置" in status.reason


def test_ai_status_codex_cli_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider=codex_cli 且 CLI 不可用：文案点名 Codex CLI，不静默放行。"""
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    monkeypatch.setattr(novel_ai, "current_ai_provider", lambda: "codex_cli")
    monkeypatch.setattr(novel_ai, "codex_cli_available", lambda: False)

    status = novel_ai.ai_status()
    assert status.available is False
    assert status.code == "ai_unavailable"
    assert status.reason and "Codex CLI 未就绪" in status.reason


def test_ai_status_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    status = novel_ai.ai_status()
    assert status.available is True
    assert status.reason is None


# ─────────────────────────── 上下文 ───────────────────────────


def test_context_card_contains_beat_and_open_foreshadow(tmp_path: Path) -> None:
    """上下文卡必须含本章 beat 与 ≥1 条 open 伏笔（P0-8② 的数据前提）。"""
    store, book_id = _seed(tmp_path)
    card = novel_ai.build_context_card(store, book_id, "ch-002")

    assert "陆昭受伤并与白露关系转冷" in card, "本章节拍必须进入上下文"
    assert "黑匣子的真实来源" in card, "未收伏笔必须进入上下文"
    open_count = len([f for f in store.get_state(book_id).foreshadow if f.status == "open"])
    assert open_count >= 1


async def test_prompt_really_includes_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-8②：实际发给模型的 prompt 里包含 beat 与 open 伏笔。"""
    store, book_id = _seed(tmp_path)
    captured = _stub_ai(monkeypatch, "雨下了整夜，星港的霓虹在水面碎成一片。")

    book = store.get_book(book_id)
    state = store.get_state(book_id)
    _volume, chapter = store.get_chapter_node(book_id, "ch-002")
    card = novel_ai.build_context_card(store, book_id, "ch-002")
    messages = novel_ai.assemble_messages(book, state, chapter, "continue", None, card)
    await novel_ai.generate_draft(messages)

    joined = "\n".join(str(m.get("content") or "") for m in captured["messages"])
    assert "陆昭受伤并与白露关系转冷" in joined
    assert "黑匣子的真实来源" in joined
    assert len(captured["messages"]) == 2
    assert captured["messages"][0]["role"] == "system"


# ─────────────────────────── 写前门禁 ───────────────────────────


def test_check_write_gate_blocks_and_skips(tmp_path: Path) -> None:
    """P0-12①：无细纲 → 拒绝；skip=True 显式放行。"""
    store, book_id = _seed(tmp_path)
    _volume, chapter = store.get_chapter_node(book_id, "ch-002")
    novel_ai.check_write_gate(chapter)  # 有 beat，通过

    empty = chapter.model_copy(update={"beat": "", "summary": ""})
    with pytest.raises(novel_ai.WriteGateError) as exc:
        novel_ai.check_write_gate(empty)
    assert exc.value.code == "missing_beat"

    novel_ai.check_write_gate(empty, skip=True)  # 用户知情放行


# ─────────────────────────── 事实快照解析 ───────────────────────────


def test_parse_fact_snapshot_accepts_fenced_json() -> None:
    raw = (
        "```json\n"
        '{"chars": ["陆昭", "白露"], "state_changes": ["陆昭受伤"], '
        '"planted": ["黑匣子被夺走"], "resolved": ["f-001"], '
        '"relations": [{"from": "陆昭", "to": "白露", "delta": "信任动摇"}]}'
        "\n```"
    )
    fact = novel_ai.parse_fact_snapshot(raw)
    assert fact.chars == ["陆昭", "白露"]
    assert fact.state_changes == ["陆昭受伤"]
    assert fact.planted == ["黑匣子被夺走"]
    assert fact.resolved == ["f-001"]
    assert fact.relations[0].source == "陆昭"
    assert fact.relations[0].target == "白露"
    assert fact.source == "ai"
    assert fact.id == ""  # id 由 ingest_facts 回填


def test_parse_fact_snapshot_tolerates_missing_fields() -> None:
    fact = novel_ai.parse_fact_snapshot('{"chars": ["陆昭"]}')
    assert fact.chars == ["陆昭"]
    assert fact.state_changes == []
    assert fact.relations == []


def test_parse_fact_snapshot_rejects_bad_payload() -> None:
    for raw in ("", "不是 JSON", "[]", '{"chars": {"name": "陆昭"}}'):
        with pytest.raises(novel_ai.FactParseError):
            novel_ai.parse_fact_snapshot(raw)
    with pytest.raises(novel_ai.FactParseError):
        novel_ai.parse_fact_snapshot('{"relations": [{"from": 1, "to": "白露"}]}')


def test_illegal_snapshot_does_not_touch_state(tmp_path: Path) -> None:
    """P0-10②：快照非法 → 不写入 state.json（字节不变）。"""
    store, book_id = _seed(tmp_path)
    state_path = store.book_dir(book_id) / "state.json"
    before = state_path.read_bytes()

    with pytest.raises(novel_ai.FactParseError):
        novel_ai.parse_fact_snapshot("AI 返回了一段散文，不是 JSON")

    assert state_path.read_bytes() == before


async def test_generate_fact_snapshot_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "chars": ["陆昭"],
        "state_changes": ["陆昭从健康→轻伤住院"],
        "planted": ["星港走私案的幕后主使"],
        "resolved": [],
        "relations": [{"from": "陆昭", "to": "白露", "delta": "信任开始动摇"}],
    }
    _stub_ai(monkeypatch, json.dumps(payload, ensure_ascii=False))

    chapter = store_chapter_stub()
    fact = await novel_ai.generate_fact_snapshot(chapter, "正文内容", None)
    assert fact.chars == ["陆昭"]
    assert fact.relations[0].delta == "信任开始动摇"


def store_chapter_stub():
    """构造一个最小章节节点（不依赖磁盘）。"""
    from app.services.novel_store import OutlineChapter

    return OutlineChapter(id="ch-002", title="雨夜", beat="陆昭受伤")


# ─────────────────────────── 写后自检 ───────────────────────────


def _rules(hits: list) -> set[str]:
    return {hit.rule for hit in hits}


def test_lint_truncated_tail() -> None:
    assert "truncated_tail" in _rules(
        novel_ai.lint_text("雨下了整夜，星港的霓虹在水面碎成一片")
    )
    assert "truncated_tail" not in _rules(
        novel_ai.lint_text("雨下了整夜，星港的霓虹在水面碎成一片。")
    )


def test_lint_eng_leak() -> None:
    hits = novel_ai.lint_text("系统返回了 null 值。")
    leak = [hit for hit in hits if hit.rule == "eng_leak"]
    assert leak and leak[0].line == 1
    assert "null" in leak[0].message
    assert not _rules(novel_ai.lint_text("雨下了整夜，星港的霓虹在水面碎成一片。")) & {"eng_leak"}


def test_lint_ai_cliche() -> None:
    three = "他不由得回头。她不由得微笑。我不由得叹息。"
    assert "ai_cliche" in _rules(novel_ai.lint_text(three))
    two = "他不由得回头。她不由得微笑。"
    assert "ai_cliche" not in _rules(novel_ai.lint_text(two))


def test_lint_repeat_sentence() -> None:
    hits = novel_ai.lint_text("这是一句完整的句子。这是一句完整的句子。")
    repeat = [hit for hit in hits if hit.rule == "repeat_sentence"]
    assert repeat and repeat[0].excerpt


def test_lint_clean_text_has_no_hits() -> None:
    clean = (
        "雨下了整夜，星港的霓虹在水面碎成一片。\n\n"
        "陆昭把黑匣子塞进外套，转身走进了废弃船坞。\n\n"
        "身后传来一声闷响，像是金属落在积水上。"
    )
    assert novel_ai.lint_text(clean) == []


def test_lint_empty_text() -> None:
    assert novel_ai.lint_text("") == []


# ─────────────────────────── 调用出口 fail-closed ───────────────────────────


async def test_generate_draft_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: False)
    with pytest.raises(novel_ai.AiCallError) as exc:
        await novel_ai.generate_draft([{"role": "user", "content": "写一段"}])
    assert exc.value.code == "ai_unavailable"


async def test_generate_draft_wraps_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(
        messages: list[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        raise RuntimeError("网关返回 502")

    monkeypatch.setattr(novel_ai, "ai_configured", lambda provider=None: True)
    monkeypatch.setattr(novel_ai, "generate_ai_text", boom)
    with pytest.raises(novel_ai.AiCallError) as exc:
        await novel_ai.generate_draft([{"role": "user", "content": "写一段"}])
    assert exc.value.code == "ai_error"
    assert "502" in str(exc.value)


async def test_generate_draft_rejects_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """禁止空字符串冒充成功。"""
    _stub_ai(monkeypatch, "   ")
    with pytest.raises(novel_ai.AiCallError) as exc:
        await novel_ai.generate_draft([{"role": "user", "content": "写一段"}])
    assert exc.value.code == "ai_error"


def test_relation_delta_alias_roundtrip() -> None:
    """RelationDelta 既能吃 from/to，也能吃 source/target（populate_by_name）。"""
    from_alias = RelationDelta.model_validate({"from": "陆昭", "to": "白露", "delta": "x"})
    from_name = RelationDelta.model_validate({"source": "陆昭", "target": "白露", "delta": "x"})
    assert from_alias.source == from_name.source == "陆昭"
    assert from_alias.model_dump(by_alias=True) == {"from": "陆昭", "to": "白露", "delta": "x"}
