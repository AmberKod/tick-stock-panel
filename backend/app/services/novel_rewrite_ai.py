"""小说工作区 · 换元仿写 — AI 层：自研提示词 + 结构化块解析 + 唯一 AI 出口。

设计依据：`deliverables/novel-workspace/ARCHITECTURE-rewrite.md` §3.5 / §8.5；
需求依据：`PRD-rewrite.md` §2.3 / §2.6 / §2.7。

**★许可证与合规红线★**

- 本文件内的三套提示词（设定卡 / 大纲补丁 / 分章草稿）与全部规则
  **均为自研，未复制第三方提示词**。
- 参考提示词包（`参考项目/xiao_shuo/提示词/`）是网上流传的非开源资料：
  **只吸收机制**（五层拓扑重建 / 三张必填表 / 反向校验三问），
  **不复制任何原文**进仓库。
- AGPL/GPL 参考项目（QMAI / AI_NovelGenerator / NovelForge / ReNovel-AI）
  **只借鉴概念与信息架构，不搬代码、不抄提示词**。
- license 与宿主项目一致（MIT）。

**AI 通道纪律**：本模块是仿写域**唯一**调用 `app.services.ai_provider` 的地方；
`api/novel_rewrite.py` 与 `novel_rewrite_jobs.py` 禁止直接调 `ai_provider`。
失败一律 fail-closed（抛 `AiCallError`），**绝不 mock 生成、绝不空列表冒充成功**。

**结构化块的诚实原则**：```rw-roles / ```rw-reversals 解析失败即返回 `[]`，
调用方据此把质检第 ⑦/⑧ 项置 `unavailable` —— **禁止用空列表冒充 pass**。

**对架构的两处偏离（均已评估，见代码内注释）**：
① import 了 `novel_ai` 的 `ai_status()` 与 `AiCallError`（单点出口，避免
「AI 不可用」文案与 503 语义两处漂移）；
② 采样温度改为**分层**（结构化产出 0.5 / 创意写作 0.8），偏离 §3.5 的
0.8/0.5/0.85 —— 主理人裁定，理由见下方「采样温度」常量块。
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.services import novel_ai
from app.services.ai_provider import Message, generate_ai_text
from app.services.novel_rewrite_store import Blueprint
from app.services.novel_store import ERR_AI_ERROR, ERR_AI_UNAVAILABLE, OutlineTree

#: 复用既有「AI 调用失败」异常 —— 自动享受 api/novel.py 的 503 翻译。
AiCallError = novel_ai.AiCallError

# ─────────────────────────── 常量 ───────────────────────────

PLAN_MAX_TOKENS = 3000
OUTLINE_MAX_TOKENS = 3000
CHAPTER_MAX_TOKENS = 4000

#: 采样温度 —— **分层**（主理人裁定，偏离 ARCHITECTURE-rewrite.md §3.5 的 0.8/0.5/0.85）。
#:
#: 为什么两步温度不同：
#:   · `plan` / `outline` 是**结构化产出** —— 设定卡必须吐出可被 `json.loads`
#:     直接解析的 ```rw-roles / ```rw-reversals 两块，大纲必须吐出可被
#:     `OutlineTree` 校验的 JSON。温度高时模型更容易自由发挥，**偏离五层重建
#:     约束**（例如自作聪明复用原作的权力结构）—— 这是「语义跑偏」，容错解析器
#:     只救得了「格式跑偏」，救不了语义。而且解析一旦失败，质检第 ⑦⑧ 两项就
#:     恒定 `unavailable`，等于把人工核对负担推给用户。因此压到 **0.5**。
#:   · `chapter` 是**创意写作**（正文），压温会让文风变平、句式趋同，
#:     反而伤产物质量，因此保持 **0.8**。
#:
#: 两条措施并行，不是二选一：压温降低「语义跑偏」概率，
#: 完整 JSON few-shot + 容错解析器兜住「格式跑偏」。
STRUCTURED_TEMPERATURE = 0.5       # 结构化产出（plan / outline）
CREATIVE_TEMPERATURE = 0.8         # 创意写作（chapter 正文）
PLAN_TEMPERATURE = STRUCTURED_TEMPERATURE
OUTLINE_TEMPERATURE = STRUCTURED_TEMPERATURE
CHAPTER_TEMPERATURE = CREATIVE_TEMPERATURE

#: 单次 AI 调用超时（秒）。
AI_TIMEOUT = 180.0

#: 结构化块围栏标签（自研，避免与既有 ```json 混淆）。
ROLES_FENCE = "rw-roles"
REVERSALS_FENCE = "rw-reversals"

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)


# ─────────────────────────── 自研提示词 ───────────────────────────

#: 九条禁止项（PRD §2.6）改写为负面指令，三套提示词共用。
_NEGATIVE_CONSTRAINTS = (
    "【禁止项 · 九条，违反任一即视为本次输出不合格】\n"
    "1. 禁止一对一人物映射：不允许「原作 A 对应新作 a、原作 B 对应新作 b」式的换名不换构。\n"
    "2. 禁止同构反转底牌：不允许反转的机制与出现位置照搬原作。\n"
    "3. 禁止复制原句、原作专名、独特道具名与具体对白。\n"
    "4. 禁止同义改写（洗稿）：换词不换结构同样算违规。\n"
    "5. 禁止融梗：不允许保留原作具体的桥段组合。\n"
    "6. 禁止改人名地名后照搬情节。\n"
    "7. 禁止打乱顺序后照搬：顺序变了但集合没变，仍然违规。\n"
    "8. 禁止一对一场景映射：原作的每个标志场景不允许在新作里有对应物。\n"
    "9. 禁止保留原作独有设定的具体参数（世界观规则、力量体系数值等）。\n"
)

#: 设定卡（plan）系统提示词 —— 自研。
_PLAN_SYSTEM = (
    "你是一名中文类型小说的「结构重建」助手。你的任务是把用户给出的**抽象结构笔记**，\n"
    "实例化成一份**全新**的五层设定卡：L1 符号层、L2 场景层、L3 关系层、L4 事件层、L5 桥段层。\n\n"
    "法理基线（必须遵守）：著作权保护表达、不保护思想。功能位、情绪节拍、母题属于思想层面，\n"
    "可以继承；具体文字、人物关系拓扑、桥段序列的特定组合、独特道具、具体对白属于表达层面，\n"
    "**必须全部重建**。\n\n"
    "L3 的硬要求：**不是换称呼，是整张关系图重新连线，并且必须换权力流向**。\n"
    "把「师兄」改成「表舅」不算重建（亲缘拓扑没变）；\n"
    "把「掌门」改成「物业经理」才算（权力来源、可支配资源、施压方式全变了）。\n\n"
    "L5 的硬要求：**同一情绪目标必须走完全不同的路径**。\n"
    "原作是「当众受辱 → 隐忍 → 反击」，新作不能也是这个顺序。\n\n"
    + _NEGATIVE_CONSTRAINTS
    + "\n【输出格式】\n"
    "先输出一份 Markdown 设定卡，依次包含五个二级标题：\n"
    "## L1 符号层 / ## L2 场景层 / ## L3 关系层 / ## L4 事件层 / ## L5 桥段层。\n"
    "L3 部分必须逐条列出新作的每一条关系边（谁 → 谁 / 关系类型 / 权力流向）。\n"
    "L5 部分必须给出新作的桥段序列（用「→」连接）。\n"
    "然后在文末输出两个机器可解析的 JSON 块，格式**严格**如下（键名不得改动）：\n\n"
    "```rw-roles\n"
    '[{"name": "新角色名", "slot": "功能位"}, {"name": "新角色名2", "slot": "功能位2"}]\n'
    "```\n\n"
    "```rw-reversals\n"
    '[{"type": "反转类型", "chapter_index": 1, "position_ratio": 0.0}]\n'
    "```\n\n"
    "说明：`slot` 只能取功能位（如 主角 / 导师 / 对手 / 盟友 / 背叛者 / 爱情对象）；\n"
    "`chapter_index` 从 1 开始；`position_ratio` 是 0-1 的进度值。\n"
    "两个 JSON 块必须存在且可被 json.loads 直接解析 —— 它们会被程序用于自动质检。"
)

#: 大纲补丁（outline）系统提示词 —— 自研。
_OUTLINE_SYSTEM = (
    "你是一名中文类型小说的「节奏架构」助手。你的任务是基于给定的五层设定卡，\n"
    "输出一份**六章级**的大纲补丁 JSON。\n\n"
    "硬要求：\n"
    "1. 只输出一个 JSON 对象，**不要输出代码围栏以外的任何文字**。\n"
    "2. JSON 的结构必须能被如下模型直接解析：\n"
    '   {"nodes": [{"id": "v1", "type": "volume", "title": "第一卷", "order": 1,\n'
    '               "children": [{"id": "ch-001", "type": "chapter", "title": "第一章",\n'
    '                             "order": 1, "status": "draft", "word_target": 3000,\n'
    '                             "summary": "一句话细纲", "beat": "本章节拍"}]}]}\n'
    "3. `id` 只允许小写字母、数字与连字符；`type` 只能是 volume / chapter。\n"
    "4. 每个章节点额外提供四个自研字段（程序会读取）：\n"
    "   `info_gain`（本章新信息增量的自然语言描述）、\n"
    "   `foreshadow_planted`（本章埋下的伏笔，字符串数组）、\n"
    "   `foreshadow_resolved`（本章回收的伏笔，字符串数组）、\n"
    "   `stimulus`（本章刺激点，字符串）。\n"
    "5. 六章的刺激点必须**分布不均**（有起有伏），不要每章一个等距爽点。\n"
    "6. 这份补丁**不会**被直接写进正式大纲，需用户核对后显式采纳。\n\n"
    + _NEGATIVE_CONSTRAINTS
)

#: 分章草稿（chapter）系统提示词 —— 自研。
_CHAPTER_SYSTEM = (
    "你是一名中文类型小说的合写助手。你的任务是按给定的设定卡与本章节拍，\n"
    "写出这一章的**正文草稿**。\n\n"
    "硬要求：\n"
    "1. 只输出正文，不要输出章节标题以外的元信息，不要写注释、不要写总结。\n"
    "2. 严格承接本章的 `summary` 与 `beat` 推进，节拍里没写到的转折不要提前发生。\n"
    "3. 只使用设定卡里已经建立的新符号、新场景、新关系，**不得**出现原作专名。\n"
    "4. 视角与人称跟随设定；段落自然分段，结尾落在句末标点上（不要半句截断）。\n"
    "5. 若上下文不足，就写你确实能承接的那一段，不要靠凭空补设定凑字数。\n\n"
    + _NEGATIVE_CONSTRAINTS
)


def _chapter_field(chapter: Any, name: str, default: Any = "") -> Any:
    """从章节节点取字段：既支持 `OutlineChapter` 模型，也支持同构 dict。

    （不用 `getattr(...) or chapter.get(...)` 那种写法 —— `0 or ...` 会去
    求右侧值，dict 分支会在模型对象上炸 AttributeError。）
    """
    if chapter is None:
        return default
    if isinstance(chapter, dict):
        value = chapter.get(name, default)
    else:
        value = getattr(chapter, name, default)
    return default if value is None else value


def _fmt_list(items: list[Any], empty: str = "（未填写）") -> str:
    """把列表渲染成提示词里的短行；空列表 → 占位文案。"""
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    return "、".join(values) if values else empty


def _fmt_slots(slots: list[Any]) -> str:
    """功能位 → `功能位（特征）`。"""
    values: list[str] = []
    for slot in slots or []:
        name = str(getattr(slot, "slot", "") or "").strip()
        trait = str(getattr(slot, "trait", "") or "").strip()
        values.append(f"{name}（{trait}）" if trait else name)
    return "、".join(values) if values else "（未填写）"


def _fmt_edges(edges: list[Any], empty: str = "（未填写）") -> str:
    """关系边 → `A → B [类型 / 权力]`。"""
    values: list[str] = []
    for edge in edges or []:
        source = str(getattr(edge, "source", "") or "").strip()
        target = str(getattr(edge, "target", "") or "").strip()
        kind = str(getattr(edge, "kind", "") or "").strip() or "未标注"
        power = str(getattr(edge, "power", "") or "").strip() or "未标注"
        values.append(f"{source} → {target} [{kind} / {power}]")
    return "\n".join(f"  - {item}" for item in values) if values else f"  {empty}"


# ─────────────────────────── 提示词构造 ───────────────────────────

def build_plan_prompt(bp: Blueprint) -> list[Message]:
    """设定卡提示词（自研）：五层重建 + 两段机器可解析块。

    Args:
        bp: 结构蓝图。

    Returns:
        `[{'role': 'system', ...}, {'role': 'user', ...}]`。
    """
    abstract = bp.abstract
    rebuild = bp.rebuild
    user = (
        f"【参考来源标注】{bp.source_ref.label or '（未填写）'}"
        f"（题材域：{bp.source_ref.work_type or '（未填写）'}）\n"
        f"【结构化拆书笔记】\n{bp.source_ref.note.strip() or '（未填写）'}\n\n"
        "【抽象层 · 可继承的思想层面信息】\n"
        f"- 功能位：{_fmt_slots(abstract.function_slots)}\n"
        f"- 情绪节拍：{_fmt_list(abstract.emotion_beats)}\n"
        f"- 信息差：{_fmt_list(abstract.info_gap)}\n"
        f"- 反转类型：{_fmt_list(abstract.reversal_types)}\n"
        f"- 母题：{_fmt_list(abstract.motifs)}\n"
        f"- 节奏：{abstract.pacing.strip() or '（未填写）'}\n\n"
        "【L1 符号层】原作专名黑名单（**禁止出现在产物中**）：\n"
        f"  {_fmt_list(rebuild.L1_symbols.banned)}\n"
        "  已有的新符号对照（原作 → 新作）："
        f"{json.dumps(rebuild.L1_symbols.new_lexicon, ensure_ascii=False) if rebuild.L1_symbols.new_lexicon else '（无）'}\n\n"
        "【L2 场景层】原作标志场景黑名单（**禁止出现在产物中**）：\n"
        f"  {_fmt_list(rebuild.L2_scenes.banned)}\n"
        f"  已拟定的新场景：{_fmt_list(rebuild.L2_scenes.new_scenes)}\n\n"
        "【L3 关系层 · 原作关系图（仅供理解结构，**不要照搬**）】\n"
        f"{_fmt_edges(rebuild.L3_relations.source_graph)}\n"
        "  → 请给出**整张重新连线且权力流向已改变**的新作关系图。\n\n"
        f"【L4 事件层】已拟定的新因果链：{_fmt_list(rebuild.L4_events.new_causal_chain)}\n\n"
        "【L5 桥段层】原作桥段序列（**必须走不同路径**）：\n"
        f"  {'→'.join(str(x) for x in rebuild.L5_beats.source_seq) or '（未填写）'}\n"
        "  已拟定的新桥段序列：\n"
        f"  {'→'.join(str(x) for x in rebuild.L5_beats.new_seq) or '（未填写，请你重新设计）'}\n\n"
        "请输出五层重建设定卡，并在文末给出 ```rw-roles 与 ```rw-reversals 两个 JSON 块。"
    )
    return [
        {"role": "system", "content": _PLAN_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_outline_prompt(bp: Blueprint, plan_md: str) -> list[Message]:
    """六章级大纲补丁提示词（自研）：只输出 JSON，不写 `book.json`。

    Args:
        bp: 结构蓝图。
        plan_md: 设定卡 Markdown（上一步产物）。
    """
    user = (
        f"【书籍标题】{bp.title or '（未命名）'}\n"
        f"【节奏要求】{bp.abstract.pacing.strip() or '（未填写）'}\n\n"
        f"【五层设定卡】\n{(plan_md or '').strip() or '（未生成设定卡，请按抽象层信息自行推演）'}\n\n"
        "请输出六章级大纲补丁 JSON（可被程序直接解析）。"
    )
    return [
        {"role": "system", "content": _OUTLINE_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_chapter_prompt(
    bp: Blueprint,
    plan_md: str,
    chapter: Any,
    context_tail: str,
) -> list[Message]:
    """分章草稿提示词（自研）。

    Args:
        bp: 结构蓝图。
        plan_md: 设定卡 Markdown。
        chapter: 章节节点（`OutlineChapter` 或同构 dict）。
        context_tail: 上一章正文尾部（用于衔接语感）。
    """
    title = _chapter_field(chapter, "title", "")
    summary = _chapter_field(chapter, "summary", "") or "（未填写）"
    beat = _chapter_field(chapter, "beat", "") or "（未填写）"
    word_target = int(_chapter_field(chapter, "word_target", 0) or 0)
    goal = f"本章目标约 {word_target} 字。" if word_target else "本章长度由你按节拍需要自行把握。"

    banned = list(bp.rebuild.L1_symbols.banned) + list(bp.rebuild.L2_scenes.banned)
    user = (
        f"【书籍】《{bp.title or '（未命名）'}》· 本章：{title or '（未命名章节）'}\n"
        f"- 一句话细纲：{summary}\n"
        f"- 本章节拍：{beat}\n"
        f"- {goal}\n\n"
        f"【禁止出现的原作专名与标志场景】{_fmt_list(banned, empty='（未声明黑名单）')}\n\n"
        f"【五层设定卡】\n{(plan_md or '').strip() or '（未提供）'}\n\n"
        f"【上一章正文尾部（用于衔接语感，不要复述）】\n{(context_tail or '').strip() or '（无）'}\n\n"
        "请从本章开头写起，直接输出正文。"
    )
    return [
        {"role": "system", "content": _CHAPTER_SYSTEM},
        {"role": "user", "content": user},
    ]


# ─────────────────────────── 结构化块解析 ───────────────────────────

def _strip_fence(raw: str) -> str:
    """剥掉 ```json ... ``` 围栏；无围栏时原样返回。"""
    text = str(raw or "").strip()
    match = _FENCE_RE.match(text)
    if match:
        return match.group(1).strip()
    return text


def _extract_block(md: str, tag: str) -> str:
    """抽取 ```` ```<tag> ... ``` ```` 围栏内的内容；找不到返回空串。"""
    text = str(md or "")
    pattern = re.compile(r"```\s*" + re.escape(tag) + r"\s*\n(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    # 容错：模型有时写成 ```json 且内容是目标结构，或围栏标签带空格/大写。
    # ★P2-7 两道闸，缺一不可★（否则会「跨标签串味」：
    #   `rw-roles` 块里写了 "type" 会被反转表认领，反之含 "slot" 的块会被角色表认领，
    #   后果是 ⑧ 由 unavailable 变成 warn —— 没放行成 pass，但报告含义失真）。
    #   ① 围栏标签**明确属于另一个域** → 直接跳过（不管内容里有什么字段）；
    #   ② 内容里同时出现两个域的判别字段 → 不认领（无法判定归属）。
    loose = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
    for info, candidate in loose.findall(text):
        body = candidate.strip()
        if not body.startswith("["):
            continue
        label = info.strip().lower()
        if tag == ROLES_FENCE and REVERSALS_FENCE in label:
            continue
        if tag == REVERSALS_FENCE and ROLES_FENCE in label:
            continue
        has_slot = '"slot"' in body
        has_type = '"type"' in body
        if tag == ROLES_FENCE and has_slot and not has_type:
            return body
        if tag == REVERSALS_FENCE and has_type and not has_slot:
            return body
    return ""


def _load_json_list(raw: str) -> list[dict]:
    """把块内容解析为 `list[dict]`；任何失败返回 `[]`（**绝不抛、绝不伪造**）。"""
    text = _strip_fence(raw).strip()
    if not text:
        return []
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def parse_character_table(md: str) -> list[dict]:
    """抽取 ```rw-roles 围栏内 JSON → `[{name, slot}]`。

    解析失败返回 `[]` —— 调用方据此把质检第 ⑦ 项置 `unavailable`，
    **禁止用空列表冒充 pass**。
    """
    raw = _extract_block(md, ROLES_FENCE)
    rows = _load_json_list(raw)
    result: list[dict] = []
    for item in rows:
        name = str(item.get("name", "") or "").strip()
        slot = str(item.get("slot", "") or "").strip()
        # ★P2-6：与 `parse_reversal_table()` 同口径 —— **丢掉 slot 为空的行**。
        # 保留「无名功能位」会把空槽位当成一个角色，让 ⑦ 因「角色数不同」误判 pass。
        if not slot:
            continue
        result.append({"name": name, "slot": slot})
    return result


def parse_reversal_table(md: str) -> list[dict]:
    """抽取 ```rw-reversals 围栏内 JSON → `[{type, chapter_index, position_ratio}]`。

    解析失败返回 `[]` → 质检第 ⑧ 项置 `unavailable`。
    """
    raw = _extract_block(md, REVERSALS_FENCE)
    rows = _load_json_list(raw)
    result: list[dict] = []
    for item in rows:
        kind = str(item.get("type", "") or "").strip()
        if not kind:
            continue
        index = item.get("chapter_index")
        ratio = item.get("position_ratio")
        row: dict[str, Any] = {"type": kind}
        if isinstance(index, (int, float)):
            row["chapter_index"] = index
        if isinstance(ratio, (int, float)):
            row["position_ratio"] = ratio
        result.append(row)
    return result


def parse_outline_patch(raw: str) -> dict:
    """解析大纲补丁：剥围栏 → `json.loads` → `OutlineTree` 校验。

    Returns:
        可被 `NovelStore.save_outline(book_id, version, nodes)` 直接消费的
        `{"nodes": [...]}`。

    Raises:
        ValueError: 解析或校验失败（**不落盘半份补丁**）。
    """
    text = _strip_fence(str(raw or ""))
    if not text:
        raise ValueError("AI 未返回任何内容，无法解析大纲补丁")
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI 返回的大纲补丁不是合法 JSON: {exc}") from exc
    if isinstance(payload, list):
        payload = {"nodes": payload}
    if not isinstance(payload, dict):
        raise ValueError(f"大纲补丁应是 JSON 对象，实际是 {type(payload).__name__}")
    try:
        tree = OutlineTree.model_validate({"nodes": list(payload.get("nodes") or [])})
    except Exception as exc:
        raise ValueError(f"大纲补丁结构非法: {exc}") from exc
    if not tree.nodes:
        raise ValueError("大纲补丁不含任何卷节点")
    return tree.model_dump(mode="json")


# ─────────────────────────── 唯一 AI 出口 ───────────────────────────

def ai_ready() -> tuple[bool, str, str]:
    """仿写域的 AI 可用性（复用 `novel_ai.ai_status()` 单点出口）。

    Returns:
        `(available, code, reason)`；不可用时 `available=False` 且带中文 reason。
    """
    status = novel_ai.ai_status()
    if status.available:
        return True, "", ""
    return False, (status.code or ERR_AI_UNAVAILABLE), (status.reason or "AI 网关不可用")


def require_ai_ready() -> None:
    """fail-closed：AI 不可用直接抛 `AiCallError`（503 `ai_unavailable`），不建 job。"""
    ok, code, reason = ai_ready()
    if not ok:
        raise AiCallError(code, reason)


async def generate(
    messages: list[Message],
    *,
    max_tokens: int | None = None,
    temperature: float = STRUCTURED_TEMPERATURE,
) -> str:
    """仿写域唯一调用 `generate_ai_text` 的地方（异常纪律同 `novel_ai.generate_draft`）。

    Args:
        messages: 提示词消息列表。
        max_tokens: 输出上限；`None` 交给服务端默认。
        temperature: 采样温度。

    Returns:
        模型返回的文本（非空）。

    Raises:
        AiCallError: 网关未配置（`ai_unavailable`）或调用失败（`ai_error`）。
            空结果视为失败 —— **不落盘空草稿**。
    """
    ok, code, reason = ai_ready()
    if not ok:
        raise AiCallError(code, reason)
    try:
        text = await generate_ai_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=AI_TIMEOUT,
        )
    except (RuntimeError, ValueError) as exc:
        raise AiCallError(ERR_AI_ERROR, str(exc)) from exc
    if not (text or "").strip():
        raise AiCallError(ERR_AI_ERROR, "AI 返回空内容，未生成任何仿写产物")
    return text
