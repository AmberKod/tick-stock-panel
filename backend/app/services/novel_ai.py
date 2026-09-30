"""小说工作区 — AI 层：上下文组装 / 自研提示词 / 事实快照解析 / 写后自检。

设计依据：`deliverables/novel-workspace/ARCHITECTURE.md` §3.4 / §7.5。

**许可证声明**：本文件内的三套提示词（续写 / 润色 / 事实快照）与全部规则
**均为自研**，未复制任何第三方提示词原文。AGPL/GPL 项目（QMAI /
AI_NovelGenerator / NovelForge / ReNovel-AI）只借鉴信息架构概念，不搬代码、
不抄提示词。文件 license 与宿主项目一致（MIT）。

**AI 通道纪律**：本模块是唯一调用 `app.services.ai_provider` 的地方；
`api/` 与 `novel_jobs.py` 禁止直接调 `ai_provider`。失败一律 fail-closed
（抛 `AiCallError`），绝不 mock 生成、绝不空字符串冒充成功。
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Any

from pydantic import BaseModel

from app.services.ai_provider import (
    Message,
    ai_configured,
    codex_cli_available,
    current_ai_model,
    current_ai_provider,
    generate_ai_text,
    is_codex_cli_provider,
)
from app.services.novel_store import (
    ERR_AI_ERROR,
    ERR_AI_UNAVAILABLE,
    ERR_MISSING_BEAT,
    BookMeta,
    ChapterFact,
    LintHit,
    NovelStore,
    OutlineChapter,
    RelationDelta,
)

# ─────────────────────────── 常量 ───────────────────────────

#: 事实快照调用的输出上限（结构化输出，短即可）。
FACT_MAX_TOKENS = 1200
#: 事实快照提示词里附带的正文长度上限（防止超窗，诚实不静默截断到看不见）。
FACT_TEXT_LIMIT = 6000

#: 续写/润色的采样温度（小说创作要多样性；事实抽取走低温，见 build_fact_prompt）。
DRAFT_TEMPERATURE = 0.8
FACT_TEMPERATURE = 0.2


class AiStatus(BaseModel):
    """AI 网关可用性的显式状态（fail-closed 的唯一出口）。"""

    configured: bool
    provider: str
    model: str
    available: bool
    reason: str | None = None
    code: str | None = None


class AiCallError(RuntimeError):
    """AI 调用失败（不可用时 code=ai_unavailable，调用失败时 code=ai_error）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FactParseError(ValueError):
    """事实快照解析失败 —— 任何失败都不落盘（P0-10②）。"""


class WriteGateError(ValueError):
    """写前门禁未通过。

    Attributes:
        code: 结构化错误码（`missing_beat`）。
    """

    def __init__(self, message: str = "写前门禁未通过", code: str = ERR_MISSING_BEAT) -> None:
        super().__init__(message)
        self.code = code


# ─────────────────────────── 可用性 ───────────────────────────


def ai_status(provider: str | None = None) -> AiStatus:
    """读取 AI 网关可用性：唯一出口，禁止 mock、禁止空列表冒充成功。

    Returns:
        `AiStatus`；不可用时 `available=False` 且带中文 `reason` 与 `code`。
    """
    resolved = provider or current_ai_provider()
    model = current_ai_model()
    if ai_configured(resolved):
        return AiStatus(configured=True, provider=resolved, model=model, available=True)

    if is_codex_cli_provider(resolved):
        codex_ready = codex_cli_available()
        reason = (
            "Codex CLI 未就绪（未检测到可用命令）— 续写/润色不可用。"
            if not codex_ready
            else "Codex CLI 已就绪但未通过登录校验 — 续写/润色不可用。"
        )
    else:
        reason = (
            "AI 网关未配置 — 续写/润色不可用。前往「设置 · AI」配置后自动解锁。"
            "写稿、大纲、导出不受影响。"
        )
    return AiStatus(
        configured=False,
        provider=resolved,
        model=model,
        available=False,
        reason=reason,
        code=ERR_AI_UNAVAILABLE,
    )


# ─────────────────────────── 写前门禁 ───────────────────────────


def check_write_gate(chapter: OutlineChapter, *, skip: bool = False) -> None:
    """写前门禁：本章 `beat` 与 `summary` 均为空 → 拒绝续写（P0-12）。

    Args:
        chapter: 章节节点。
        skip: 用户显式「跳过本次门禁」时为 True（知情放行，不偷偷放款）。

    Raises:
        WriteGateError: 门禁未通过且未显式跳过。
    """
    if skip:
        return
    if not (chapter.beat or "").strip() and not (chapter.summary or "").strip():
        raise WriteGateError(
            f"本章（{chapter.title or chapter.id}）细纲与节拍均为空，AI 续写已禁用",
            code=ERR_MISSING_BEAT,
        )


# ─────────────────────────── 上下文卡 ───────────────────────────


def build_context_card(store: NovelStore, book_id: str, chapter_id: str) -> str:
    """组装续写上下文卡（纯本地，不发任何网络请求）。

    与 `NovelStore.build_context_card` 同源 —— 视图与 AI 看到的是同一份上下文，
    避免"UI 显示的上下文"与"实际喂给模型的上下文"不一致。
    """
    return store.build_context_card(book_id, chapter_id)


# ─────────────────────────── 自研提示词 ───────────────────────────

_CONTINUE_SYSTEM = (
    "你是一名中文长篇小说的合写助手。你的唯一任务是续写正文，不产出评分、不产出建议、"
    "不复述用户给你的上下文。\n"
    "硬性约束：\n"
    "1. 只使用上下文卡里已经存在的设定、角色与伏笔，不要新造地名、组织或未交代的身世。\n"
    "2. 严格承接「本章节拍」推进剧情，节拍里没写到的转折不要提前发生。\n"
    "3. 视角与人称跟随上下文卡的设定；不写章节标题以外的元信息（不要输出「以下是续写」之类）。\n"
    "4. 直接输出正文，可以分自然段；不要写注释、不要在结尾写总结。\n"
    "5. 如果上下文不足，就写你确实能承接的那一段，不要靠凭空补设定来凑字数。"
)

_POLISH_SYSTEM = (
    "你是一名中文小说的文字编辑。你的唯一任务是润色用户给出的片段，保留作者的语感与事实。\n"
    "硬性约束：\n"
    "1. 只做语言层面的改进：节奏、冗余、重复用词、句子衔接、标点。\n"
    "2. 不改变情节与事实：不增删事件、不改名、不新增设定。\n"
    "3. 只输出润色后的片段本身，不要解释改了什么，不要加引号包裹全文。\n"
    "4. 保持原文的段落数量与叙述视角。"
)

_FACT_SYSTEM = (
    "你是一名结构化信息抽取助手。你的唯一任务是从给定章节正文里抽取事实，"
    "输出一个 JSON 对象（可被 json.loads 直接解析）。\n"
    "硬性约束：\n"
    "1. 只输出 JSON 对象本身，不要输出解释、不要输出 Markdown 代码围栏以外的任何文字。\n"
    "2. 只抽取正文里明确出现过的信息，不推断、不补全、不编造。\n"
    "3. 字段全部使用中文自然语言短句；没有对应内容的字段给空数组。"
)

_FACT_SCHEMA = (
    '{"chars": ["出场角色名"], '
    '"state_changes": ["角色名从A变为B"], '
    '"planted": ["本章新埋下的伏笔描述"], '
    '"resolved": ["被回收的伏笔 id 或伏笔原文"], '
    '"relations": [{"from": "角色A", "to": "角色B", "delta": "关系变化描述"}]}'
)


def build_continue_prompt(
    book: BookMeta,
    chapter: OutlineChapter,
    context_card: str,
    *,
    target_words: int = 0,
) -> list[Message]:
    """续写提示词（自研）。

    Args:
        book: 书籍元数据。
        chapter: 本章节点（提供细纲/节拍/字数目标）。
        context_card: 上下文卡 Markdown。
        target_words: 目标字数（0 表示不限）。
    """
    goal = f"本次续写目标约 {target_words} 字。" if target_words else "本次续写长度由你按节拍需要自行把握。"
    genre_note = f"（类型：{book.genre}）" if book.genre else ""
    pov_note = f"（视角：{book.pov}）" if book.pov else ""
    user = (
        f"【书籍】《{book.title}》{genre_note}{pov_note}\n\n"
        f"【续写上下文卡】\n{context_card}\n\n"
        f"【本章任务】\n"
        f"- 章节：{chapter.title or chapter.id}\n"
        f"- 一句话细纲：{chapter.summary or '（未填写）'}\n"
        f"- 本章节拍：{chapter.beat or '（未填写）'}\n"
        f"- {goal}\n\n"
        f"请从上下文卡末尾的正文之后接着写，直接输出正文。"
    )
    return [
        {"role": "system", "content": _CONTINUE_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_polish_prompt(
    book: BookMeta,
    chapter: OutlineChapter,
    selection: str,
    context_card: str,
) -> list[Message]:
    """润色提示词（自研）。

    Args:
        book: 书籍元数据。
        chapter: 本章节点。
        selection: 用户选中的原文片段。
        context_card: 上下文卡 Markdown（用于对齐语感与设定）。
    """
    user = (
        f"【书籍】《{book.title}》· 章节：{chapter.title or chapter.id}\n"
        f"【本章节拍】{chapter.beat or '（未填写）'}\n\n"
        f"【上下文卡（用于对齐设定，不要复述）】\n{context_card}\n\n"
        f"【待润色的原文片段】\n{selection}\n\n"
        f"请输出润色后的片段，只输出片段本身。"
    )
    return [
        {"role": "system", "content": _POLISH_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_fact_prompt(book: BookMeta, chapter: OutlineChapter, draft_text: str) -> list[Message]:
    """事实快照提示词（自研）：要求模型只输出一个 JSON 对象。"""
    body = (draft_text or "").strip()
    truncated = len(body) > FACT_TEXT_LIMIT
    if truncated:
        body = body[:FACT_TEXT_LIMIT]
    user = (
        f"【书籍】《{book.title}》· 章节：{chapter.id} {chapter.title}\n\n"
        f"【章节正文（{'已截断到前 ' + str(FACT_TEXT_LIMIT) + ' 字' if truncated else '全文'}）】\n"
        f"{body}\n\n"
        f"【输出格式】严格输出如下结构的 JSON 对象，不要加代码围栏以外的任何文字：\n"
        f"{_FACT_SCHEMA}\n"
    )
    return [
        {"role": "system", "content": _FACT_SYSTEM},
        {"role": "user", "content": user},
    ]


def assemble_messages(
    book: BookMeta,
    state: Any,
    chapter: OutlineChapter,
    mode: str,
    selection: str | None,
    context_card: str,
) -> list[Message]:
    """按 mode 分发到续写/润色提示词。

    Args:
        book: 书籍元数据。
        state: 追踪态（用于把未收伏笔数量写进提示词，强化"记得收伏笔"）。
        chapter: 本章节点。
        mode: `continue` 或 `polish`。
        selection: 润色模式的选中原文。
        context_card: 上下文卡。

    Returns:
        `list[Message]`（`[{'role': 'system', ...}, {'role': 'user', ...}]`）。
    """
    if mode == "polish":
        return build_polish_prompt(book, chapter, selection or "", context_card)

    open_count = 0
    try:
        open_count = len([f for f in getattr(state, "foreshadow", []) if f.status == "open"])
    except AttributeError:  # pragma: no cover - 防御性
        open_count = 0
    messages = build_continue_prompt(book, chapter, context_card, target_words=chapter.word_target)
    if open_count:
        reminder = f"\n补充要求：本书还有 {open_count} 条未回收的伏笔，若本章节拍允许可顺势推进其一。"
        messages[-1]["content"] += reminder
    return messages


# ─────────────────────────── 事实快照解析 ───────────────────────────

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_code_fence(raw: str) -> str:
    """剥掉 ```json ... ``` 围栏；无围栏时原样返回。"""
    text = str(raw or "").strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    # 模型偶尔在 JSON 前后加说明：截取第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _as_str_list(value: Any, field: str) -> list[str]:
    """把任意值强制为 `list[str]`；类型不符则 FactParseError（绝不静默丢字段）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, list):
        raise FactParseError(f"字段 {field} 应是数组，实际是 {type(value).__name__}")
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, (int, float)):
            text = str(item)
        else:
            raise FactParseError(f"字段 {field} 含非字符串元素: {type(item).__name__}")
        if text:
            result.append(text)
    return result


def parse_fact_snapshot(raw: str) -> ChapterFact:
    """解析并校验事实快照；任何失败都 raise，绝不落盘半个快照（P0-10②）。

    Args:
        raw: 模型返回的原始字符串（容许带 ```json 围栏或前后说明文字）。

    Returns:
        `ChapterFact`（`id`/`title`/`adopted_at` 由 `ingest_facts` 回填）。

    Raises:
        FactParseError: 无法解析成对象，或字段类型错误。
    """
    text = _strip_code_fence(raw)
    if not text:
        raise FactParseError("AI 未返回任何内容，无法解析事实快照")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactParseError(f"AI 返回的事实快照不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactParseError(f"事实快照应是 JSON 对象，实际是 {type(payload).__name__}")

    relations: list[RelationDelta] = []
    raw_relations = payload.get("relations", [])
    if raw_relations is None:
        raw_relations = []
    if not isinstance(raw_relations, list):
        raise FactParseError("字段 relations 应是数组")
    for item in raw_relations:
        if not isinstance(item, dict):
            raise FactParseError(f"relations 含非对象元素: {type(item).__name__}")
        source = item.get("from", item.get("source", ""))
        target = item.get("to", item.get("target", ""))
        if not isinstance(source, str) or not isinstance(target, str):
            raise FactParseError("relations 元素的 from/to 必须是字符串")
        relations.append(
            RelationDelta(
                source=source.strip(),
                target=target.strip(),
                delta=str(item.get("delta") or "").strip(),
            )
        )

    source = str(payload.get("source") or "").strip()
    return ChapterFact(
        id="",
        title=str(payload.get("title") or "").strip(),
        chars=_as_str_list(payload.get("chars"), "chars"),
        state_changes=_as_str_list(payload.get("state_changes"), "state_changes"),
        planted=_as_str_list(payload.get("planted"), "planted"),
        resolved=_as_str_list(payload.get("resolved"), "resolved"),
        relations=relations,
        source=source if source in ("ai", "manual") else "ai",
        adopted_at=None,
    )


# ─────────────────────────── 写后自检（纯本地规则） ───────────────────────────

_SENTENCE_ENDINGS = "。！？!?…」』”）\"'"
_ENG_LEAK_RE = re.compile(
    r"(?<![A-Za-z0-9])(json|api|null|undefined|nan|traceback|prompt|token)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]")
_CLICHE_WORDS: tuple[str, ...] = (
    "不由得",
    "不禁",
    "仿佛",
    "似乎",
    "某种程度上",
    "值得一提",
    "总而言之",
    "与此同时",
    "深深地",
    "悄然",
    "缓缓地",
    "意味深长",
)
_CLICHE_THRESHOLD = 3


def lint_text(text: str) -> list[LintHit]:
    """写后自检：纯本地正则规则，只提醒不改写（P0-12）。

    四条规则：
        - `truncated_tail`：结尾疑似截断（末句没有句末标点）。
        - `eng_leak`：工程词泄漏（json / api / null / undefined 等）。
        - `ai_cliche`：AI 高频味词出现 ≥ 3 次。
        - `repeat_sentence`：连续重复句。
    """
    body = str(text or "")
    hits: list[LintHit] = []
    if not body.strip():
        return hits

    lines = body.splitlines()

    # ① 结尾疑似截断
    last_index = 0
    last_line = ""
    for index, line in enumerate(lines, start=1):
        if line.strip():
            last_index = index
            last_line = line.strip()
    if last_line and last_line[-1] not in _SENTENCE_ENDINGS:
        hits.append(
            LintHit(
                rule="truncated_tail",
                message="结尾疑似截断：最后一句没有句末标点",
                line=last_index,
                excerpt=last_line[-40:],
            )
        )

    # ② 工程词泄漏
    for index, line in enumerate(lines, start=1):
        match = _ENG_LEAK_RE.search(line)
        if match:
            hits.append(
                LintHit(
                    rule="eng_leak",
                    message=f"出现工程词 \"{match.group(0)}\"",
                    line=index,
                    excerpt=line.strip()[:60],
                )
            )

    # ③ AI 高频味词
    for word in _CLICHE_WORDS:
        count = body.count(word)
        if count >= _CLICHE_THRESHOLD:
            line_no = next(
                (i for i, line in enumerate(lines, start=1) if word in line),
                None,
            )
            hits.append(
                LintHit(
                    rule="ai_cliche",
                    message=f"\"{word}\" 出现 {count} 次（≥{_CLICHE_THRESHOLD} 次）",
                    line=line_no,
                    excerpt=word,
                )
            )

    # ④ 连续重复句
    sentences = [seg.strip() for seg in _SENTENCE_SPLIT_RE.split(body)]
    sentences = [seg for seg in sentences if len(seg) >= 6]
    for previous, current in itertools.pairwise(sentences):
        if previous == current:
            line_no = next(
                (i for i, line in enumerate(lines, start=1) if current in line),
                None,
            )
            hits.append(
                LintHit(
                    rule="repeat_sentence",
                    message="连续重复句",
                    line=line_no,
                    excerpt=current[:40],
                )
            )
    return hits


# ─────────────────────────── 调用出口 ───────────────────────────


async def generate_draft(
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    temperature: float = DRAFT_TEMPERATURE,
) -> str:
    """唯一调用 `generate_ai_text` 的地方。

    Args:
        messages: 提示词消息列表。
        max_tokens: 输出上限；`None` 交给服务端默认（推理型模型思考 token 会挤占正文）。
        temperature: 采样温度。

    Returns:
        模型返回的文本（非空）。

    Raises:
        AiCallError: 网关未配置（`ai_unavailable`）或调用失败（`ai_error`）。
    """
    status = ai_status()
    if not status.available:
        raise AiCallError(ERR_AI_UNAVAILABLE, status.reason or "AI 网关不可用")
    try:
        text = await generate_ai_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=180.0,
        )
    except (RuntimeError, ValueError) as exc:
        raise AiCallError(ERR_AI_ERROR, str(exc)) from exc
    if not (text or "").strip():
        raise AiCallError(ERR_AI_ERROR, "AI 返回空内容，未生成任何草稿")
    return text


async def generate_fact_snapshot(
    chapter: OutlineChapter,
    draft_text: str,
    book: BookMeta | None = None,
) -> ChapterFact:
    """生成并解析章节事实快照（第二次 AI 调用）。

    Raises:
        AiCallError: 调用失败。
        FactParseError: 解析/校验失败（调用方必须据此不落盘）。
    """
    messages = build_fact_prompt(book or BookMeta(id="", title=""), chapter, draft_text)
    raw = await generate_draft(messages, max_tokens=FACT_MAX_TOKENS, temperature=FACT_TEMPERATURE)
    return parse_fact_snapshot(raw)
