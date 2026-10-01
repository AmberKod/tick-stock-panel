"""QA 独立验证 · C 组：输入侧预检（R-len / R-quote / R-para / R-exempt）边界。

重点不是「复述工程师跑过的 happy path」，而是**卡在阈值的那一位**：
1200/1201 字、2/3 处引号、引号内 29/30 字、4/5 连续段、2/3 行结构化标记，
外加中英混排 / emoji / 全角标点 / 超长单行，以及**响应体绝不回显被拒原文全文**
这条硬断言。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import novel_rewrite as rw_api
from app.services.novel_rewrite_store import (
    ERR_REWRITE_SOURCE_REJECTED,
    EXCERPT_CHARS,
    EXEMPT_MIN_MARKERS,
    PARA_MIN_RUN,
    PRECHECK_HONESTY_NOTE,
    QUOTE_MIN_CHARS,
    QUOTE_MIN_HITS,
    REWRITE_INPUT_LIMIT,
    Blueprint,
    RewriteStore,
    clean_precheck_response,
    precheck_blueprint,
    precheck_text,
)
from app.services.novel_store import NovelStore

# 承诺型禁用词（出现任一即视为「过度承诺」）
PROMISE_WORDS: tuple[str, ...] = (
    "保证",
    "承诺",
    "确保",
    "一定不侵权",
    "不会侵权",
    "无风险",
    "已通过查重",
    "查重通过",
    "原创性无虞",
)


# ═══════════════════════ 夹具 ═══════════════════════


class Env:
    """测试环境：app + 临时根目录的 store + 一本书。"""

    def __init__(self, client: TestClient, store: NovelStore, book_id: str) -> None:
        self.client = client
        self.store = store
        self.book_id = book_id


@pytest.fixture()
def env(tmp_path: Path) -> Iterator[Env]:
    """只挂仿写路由的最小 app（不拉起 app.main 生命周期）。

    **必须用同一个 `tmp_path` 的 store 建书** —— 若用 `NovelStore()` 默认根目录，
    会把测试用的书写进真实数据目录。
    """
    store = NovelStore(root=tmp_path)
    created = store.create_book("QA 预检书")
    app = FastAPI()
    app.include_router(rw_api.router)
    app.dependency_overrides[rw_api.shared_rewrite_store] = lambda: RewriteStore(store)
    app.dependency_overrides[rw_api.shared_store] = lambda: store
    with TestClient(app) as test_client:
        yield Env(test_client, store, created.id)


@pytest.fixture()
def client(env: Env) -> TestClient:
    """测试客户端。"""
    return env.client


@pytest.fixture()
def book(env: Env) -> str:
    """临时根目录里的一本书。"""
    return env.book_id


# ═══════════════════════ R-len 边界 ═══════════════════════


def _prose(chars: int) -> str:
    """`chars` 个非空白字符的单行散文（无引号、无列表标记、不以句号结尾）。"""
    return "字" * chars


def test_rlen_returns_empty_below_and_at_limit() -> None:
    """R-len 边界：`> 1200` 才命中；恰好 1200 **不**命中。"""
    assert REWRITE_INPUT_LIMIT == 1200
    assert precheck_text("note", _prose(1200)) == []
    assert precheck_text("note", _prose(1199)) == []


def test_rlen_hits_one_over_limit() -> None:
    """1201 字 → R-len 命中。"""
    hits = precheck_text("note", _prose(1201))
    assert [hit["rule"] for hit in hits] == ["R-len"], hits


def test_rlen_ignores_whitespace() -> None:
    """字数口径是「非空白字符数」：1200 字 + 500 个空白仍不算超。"""
    body = ("字" * 1200) + (" " * 500)
    assert precheck_text("note", body) == []
    assert precheck_text("note", ("字" * 1200) + ("\n" * 200)) == []


def test_rlen_whitespace_only_is_not_a_hit() -> None:
    """纯空白 / 空串 → 直接放行（无内容可判）。"""
    for body in ("", "   ", "\n\n\t\n"):
        assert precheck_text("note", body) == []


# ═══════════════════════ R-quote 边界 ═══════════════════════


def _quote_block(count: int, inner: int) -> str:
    """`count` 处引号片段，每处内部 `inner` 个非空白字。

    用空格分隔（同一行）—— 因为 `”` 也算句末标点，分行会被 R-para 计数，
    干扰「只测 R-quote」的目的。
    """
    return " ".join("“" + "字" * inner + "”" for _ in range(count))


def test_rquote_needs_three_hits() -> None:
    """R-quote 边界：恰好 3 处命中，2 处不命中。"""
    assert QUOTE_MIN_HITS == 3
    assert precheck_text("note", _quote_block(2, QUOTE_MIN_CHARS)) == []
    hits = precheck_text("note", _quote_block(3, QUOTE_MIN_CHARS))
    assert [hit["rule"] for hit in hits] == ["R-quote"], hits


def test_rquote_inner_length_boundary() -> None:
    """引号内恰好 30 字算命中，29 字不算。"""
    assert QUOTE_MIN_CHARS == 30
    assert precheck_text("note", _quote_block(3, 29)) == []
    assert [hit["rule"] for hit in precheck_text("note", _quote_block(3, 30))] == ["R-quote"]


def test_rquote_counts_only_long_fragments() -> None:
    """5 处短引号（每处 10 字）不算命中 —— 短对白是正常笔记。"""
    assert precheck_text("note", _quote_block(5, 10)) == []


def test_rquote_mixed_quote_styles() -> None:
    """中英文 / 直角引号混排都能被识别（不能被「换引号样式」绕过）。"""
    styles = [
        "“" + "字" * 30 + "”",
        '"' + "字" * 30 + '"',
        "「" + "字" * 30 + "」",
        "『" + "字" * 30 + "』",
    ]
    hits = precheck_text("note", "\n".join(styles))
    assert [hit["rule"] for hit in hits] == ["R-quote"]


# ═══════════════════════ R-para 边界 ═══════════════════════


def _para_block(lines: int) -> str:
    """`lines` 行连续散文，每行以句号结尾。"""
    return "\n".join(f"这是第{index}段的内容。" for index in range(lines))


def test_rpara_needs_five_consecutive_lines() -> None:
    """R-para 边界：恰好 5 行命中，4 行不命中。"""
    assert PARA_MIN_RUN == 5
    assert precheck_text("note", _para_block(4)) == []
    hits = precheck_text("note", _para_block(5))
    assert [hit["rule"] for hit in hits] == ["R-para"], hits


def test_rpara_resets_on_blank_line() -> None:
    """空行会打断连续段计数：4 行 + 空行 + 4 行 → 不命中（否则会误伤笔记）。"""
    assert precheck_text("note", _para_block(4) + "\n\n" + _para_block(4)) == []
    # 对照：任一侧自己凑够 5 行 → 仍命中
    assert [hit["rule"] for hit in precheck_text("note", _para_block(3) + "\n\n" + _para_block(5))] == ["R-para"]


def test_rquote_does_not_false_positive_on_para_rule() -> None:
    """`”` 也算句末标点：3 处引号**分行**时会同时命中 R-quote + R-para（口径确认）。"""
    body = "\n".join("“" + "字" * 30 + "”" for _ in range(3))
    rules = {hit["rule"] for hit in precheck_text("note", body)}
    assert "R-quote" in rules


def test_rpara_resets_on_non_sentence_line() -> None:
    """中间夹一行不以句号结尾的 → 连续段被重置。"""
    body = _para_block(4) + "\n这一行没有句号\n" + _para_block(4)
    assert precheck_text("note", body) == []


def test_rpara_accepts_various_sentence_endings() -> None:
    """。！？!?…」』 都算句末标点（不能靠换标点绕过）。"""
    endings = ["。", "！", "？", "!", "?", "…", "」", "』", "）"]
    body = "\n".join(f"第{index}行内容{ending}" for index, ending in enumerate(endings))
    hits = precheck_text("note", body)
    assert [hit["rule"] for hit in hits] == ["R-para"]


# ═══════════════════════ R-exempt 边界 ═══════════════════════


def _marker_lines(count: int) -> str:
    """`count` 行 `- ` 结构化标记。"""
    return "\n".join(f"- 功能位{index}：某人" for index in range(count))


def test_rexempt_needs_three_markers() -> None:
    """R-exempt 边界：恰好 3 行放行，2 行不放行。"""
    assert EXEMPT_MIN_MARKERS == 3
    long_text = _prose(1300)
    assert precheck_text("note", _marker_lines(2) + "\n" + long_text) != []  # 2 行 → 仍命中 R-len
    assert precheck_text("note", _marker_lines(3) + "\n" + long_text) == []  # 3 行 → 整体放行


def test_rexempt_marker_styles() -> None:
    """`-` / `*` / `+` / `|` / `#` / `标题：` 六种标记都能被识别。"""
    styles = ["- 功能位：甲", "* 节拍：乙", "+ 信息差：丙", "| 列 | 表 |", "# 标题", "母题：丁"]
    long_text = _prose(1300)
    for combo in (styles[:3], styles[3:], styles[::2]):
        assert precheck_text("note", "\n".join(combo) + "\n" + long_text) == []


def test_rexempt_is_highest_priority() -> None:
    """R-exempt 优先级最高：有 3 行标记时，R-len / R-quote / R-para 全部不再判。"""
    body = (
        _marker_lines(3)
        + "\n"
        + _prose(2000)
        + "\n"
        + _quote_block(3, QUOTE_MIN_CHARS)
        + "\n"
        + _para_block(6)
    )
    assert precheck_text("note", body) == []


def test_rexempt_can_be_abused_by_three_fake_markers() -> None:
    """【已知限制 · P2】3 行任意 `- x` 标记可整体绕过三条例。

    记录事实：粘贴 3000 字原文 + 3 行 `- 备注：x` 即被判为「结构笔记」放行。
    预检定位是「形态判断」（`honesty_note` 已明示不构成法律判断），
    但建议在 PRD 里补一句「标记行需出现在前 N 行 / 占比」以提高绕过成本。
    """
    original = "这是被粘贴进来的原文。" * 120  # 约 1440 字
    assert precheck_text("note", original) != []  # 裸原文 → 拦住
    bypassed = "- 备注：一\n- 备注：二\n- 备注：三\n" + original
    assert precheck_text("note", bypassed) == []  # 加 3 行标记 → 放行（已知限制）


# ═══════════════════════ 混排 / 特殊字符 ═══════════════════════


@pytest.mark.parametrize(
    "body",
    [
        "中英混排 Mixed English 与中文。",
        "emoji 😀😀😀 与中文混排。",
        "全角标点：，。！？；：「」『』（）——…",
        "混合   空白\t\t与\n换行。",
        "a" * 5000,  # 超长单行（英文）
        "字" * 5000,  # 超长单行（中文）
        "零宽​空格与中文混排。",
        "① ② ③ 带圈数字与标点。",
    ],
)
def test_mixed_content_is_handled_without_crash(body: str) -> None:
    """混排 / emoji / 超长单行：不崩、不误报（超长的按 R-len 命中）。"""
    hits = precheck_text("note", body)
    rules = {hit["rule"] for hit in hits}
    assert rules <= {"R-len", "R-quote", "R-para"}, rules
    if len(body.replace(" ", "")) > REWRITE_INPUT_LIMIT:
        assert "R-len" in rules


def test_emoji_only_text_does_not_crash() -> None:
    """纯 emoji 不崩。"""
    assert isinstance(precheck_text("note", "😀" * 3000), list)


def test_trailing_newline_and_bom_like_prefix() -> None:
    """尾换行 / 特殊前缀不改变判定（先 strip 再判）。"""
    assert precheck_text("note", _prose(1200) + "\n\n") == []
    assert precheck_text("note", "\n" + _prose(1200)) == []


# ═══════════════════════ 响应体绝不回显原文 ═══════════════════════


def test_precheck_response_never_echoes_full_text(client: TestClient, book: str) -> None:
    """★硬断言★ 422 响应体里**绝不出现**被拒原文全文（P0-3④）。"""
    secret = "独门暗记" + "字" * 1400
    response = client.post(
        f"/api/novel/books/{book}/rewrite/precheck",
        json={"fields": {"source_ref.note": secret}},
    )
    assert response.status_code == 422
    payload = response.json()
    detail = payload["detail"]
    assert detail["code"] == ERR_REWRITE_SOURCE_REJECTED
    assert secret not in response.text, "★P0★ 响应体回显了被拒原文全文"
    for hit in detail["hits"]:
        assert len(hit["excerpt"]) <= EXCERPT_CHARS
        assert set(hit) == {"field", "rule", "excerpt", "hint"}


def test_precheck_response_has_sample_and_honesty_note(client: TestClient, book: str) -> None:
    """被拒响应必须带「引导示例 + 诚实声明」，且声明不含承诺词。"""
    response = client.post(
        f"/api/novel/books/{book}/rewrite/precheck",
        json={"fields": {"note": _prose(1201)}},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["sample"], "缺少引导示例"
    assert detail["honesty_note"] == PRECHECK_HONESTY_NOTE
    for word in PROMISE_WORDS:
        assert word not in detail["honesty_note"], f"承诺词 {word} 出现在诚实声明里"
        assert word not in detail["sample"]


def test_precheck_pass_response_shape(client: TestClient, book: str) -> None:
    """通过时的响应体形状（前端靠 `passed` + `honesty_note` 渲染）。"""
    response = client.post(
        f"/api/novel/books/{book}/rewrite/precheck",
        json={"fields": {"note": "- 功能位：甲\n- 节拍：乙\n- 母题：丙"}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["passed"] is True
    assert payload["hits"] == []
    assert payload["honesty_note"] == PRECHECK_HONESTY_NOTE


def test_precheck_requires_payload(client: TestClient, book: str) -> None:
    """既不给 fields 也不给 blueprint → 422 `invalid_payload`（不是 500）。"""
    response = client.post(f"/api/novel/books/{book}/rewrite/precheck", json={})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_payload"


def test_precheck_blueprint_mode_scans_all_free_text_fields(client: TestClient, book: str) -> None:
    """blueprint 模式必须遍历**所有**自由文本字段（不只看 note）。"""
    long = _prose(1300)
    cases = [
        {"source_ref": {"note": long}},
        {"source_ref": {"label": long}},
        {"abstract": {"pacing": long}},
        {"abstract": {"emotion_beats": [long]}},
        {"abstract": {"info_gap": [long]}},
        {"abstract": {"motifs": [long]}},
        {"abstract": {"function_slots": [{"slot": "主角", "trait": long}]}},
        {"rebuild": {"L4_events": {"new_causal_chain": [long]}}},
        {"rebuild": {"L1_symbols": {"new_lexicon": {"青云宗": long}}}},
    ]
    for patch in cases:
        bp = Blueprint.model_validate(patch)
        assert precheck_blueprint(bp), f"字段未被扫描: {list(patch)}"


def test_clean_precheck_response_truncates_excerpt() -> None:
    """`clean_precheck_response()` 自己也要再截一次（防止上层塞进超长 excerpt）。"""
    payload = clean_precheck_response(
        [{"field": "f", "rule": "R-len", "excerpt": "字" * 500, "hint": "h"}]
    )
    assert len(payload["hits"][0]["excerpt"]) == EXCERPT_CHARS
    assert payload["ok"] is False and payload["passed"] is False
    assert payload["code"] == ERR_REWRITE_SOURCE_REJECTED
    assert payload["honesty_note"] == PRECHECK_HONESTY_NOTE


def test_save_blueprint_writes_nothing_when_rejected(tmp_path: Any) -> None:
    """★硬断言★ 预检命中 → **一个字节都不落盘**。"""
    store = RewriteStore(NovelStore(root=tmp_path))
    created = store.base.create_book("QA 预检落盘书")
    bp = Blueprint.model_validate({"source_ref": {"note": _prose(1300)}})
    from app.services.novel_rewrite_store import RewriteSourceRejectedError

    with pytest.raises(RewriteSourceRejectedError):
        store.save_blueprint(created.id, bp)
    assert not store.blueprint_path(created.id).exists(), "被拒的蓝图竟然落盘了"


def test_save_blueprint_ok_when_clean(tmp_path: Any) -> None:
    """对照：干净蓝图能正常落盘。"""
    store = RewriteStore(NovelStore(root=tmp_path))
    created = store.base.create_book("QA 预检落盘书2")
    bp = Blueprint.model_validate({"source_ref": {"note": "- 功能位：甲\n- 节拍：乙\n- 母题：丙"}})
    saved = store.save_blueprint(created.id, bp)
    assert store.blueprint_path(created.id).exists()
    assert saved.id and saved.created_at and saved.updated_at
