"""小说工作区 · 换元仿写工作台 — 数据层 + 算法内核。

设计依据：`deliverables/novel-workspace/ARCHITECTURE-rewrite.md` §1.2 / §3.1 /
§3.4 / §5 / §8；需求依据：`PRD-rewrite.md` §2 / §5。

**职责边界**：本模块只做三件事 —— 领域模型、纯函数算法、仿写域 IO。
它 **不 import** `novel_ai` / `novel_jobs` / `api`（可脱离 HTTP 与事件循环单测），
**只 import** `novel_store` 的公开件（路径校验 / 原子写 / 异常 / 常量）。
所有写路径一律经 `NovelStore.rewrite_path()`，该文件强制「必须位于 `rewrite/` 内」。

**法理基线（写进代码，作为所有闸门的依据）**：
著作权保护**表达**不保护**思想**。功能位（导师/对手/盟友/背叛者）、情绪节拍、
母题属于思想层面，不受保护；受保护的是具体文字、**人物关系拓扑**、
**桥段序列的特定组合**、独特道具与具体对白。因此合规与否不取决于「改没改人名」，
而取决于 **L3（关系拓扑）与 L5（桥段序列）是否被真正重建** —— 这正是这两层
被定为命门、且是唯一由算法自动判定的两层的原因。

**四态纪律（★本次核心★）**：
`pass` / `warn` / `fail` / `unavailable` 严格区分，禁止混淆：
  - `pass`        规则未命中；
  - `warn`        需人工确认，勾选后方可采纳；
  - `fail`        硬阻断，不可降级为 warn，不可采纳；
  - `unavailable` 无样本 / 无法自动比对，**必须由人工核对，绝不等于 pass**。
任何「没样本就报 pass」的实现视为缺陷。`RewriteReport._recompute_summary()`
让 `summary` 永远是 `checks` 的函数 —— 不可能出现「有 fail 但 adoptable=True」。

**只提示，不承诺**：报告与 UI 文案禁止出现任何「保证类」承诺型表述
（例如声称已通过查重、声称不侵权、声称原创性无虞）。
`DISCLAIMER_TEXT` 单点定义，pytest 关键字断言覆盖。

许可证：MIT（与宿主项目一致）。本文件全部为自研实现，未复制任何第三方代码。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.novel_store import (
    ERR_REWRITE_ACK_REQUIRED,
    ERR_REWRITE_GATE_BLOCKED,
    ERR_REWRITE_SOURCE_REJECTED,
    REWRITE_DIR,
    JobStep,
    NovelNotFound,
    NovelStore,
    NovelStoreError,
    NovelValidationError,
    atomic_write_json,
    atomic_write_text,
    count_words,
    now_iso,
    validate_id,
)

#: 传给 `NovelStore.rewrite_path()` 的路径前缀（该方法以 `book_dir` 为根）。
REWRITE_PREFIX = f"{REWRITE_DIR}/"


def in_rewrite(rel: str) -> str:
    """把「相对 `rewrite/` 的路径」归一成 `rewrite_path()` 需要的书籍相对路径。

    对外契约（`RewriteReport.draft_file` / `write_rewrite_draft` 的返回值）统一
    **相对 `rewrite/`**；内部调 `rewrite_path()` 前一律经本函数补前缀，
    避免两处各拼一份导致「校验口径漂移」。
    """
    text = str(rel or "").strip().lstrip("/")
    if text.startswith(REWRITE_PREFIX):
        return text
    return REWRITE_PREFIX + text

# ─────────────────────────── 常量与阈值 ───────────────────────────

#: 仿写域子目录（相对 `rewrite/`）。
BLUEPRINT_FILE = "blueprint.json"
RW_DRAFTS_DIR = "drafts"
RW_REPORTS_DIR = "reports"

#: 蓝图 id / 产物 id 前缀。
BLUEPRINT_ID_PREFIX = "bp"
REWRITE_ID_PREFIX = "rw"
JOB_ID_PREFIX = "rw"

#: 免责声明（单点定义，前端不得另写一份）。
DISCLAIMER_VERSION = "rw-disclaimer-v1"
DISCLAIMER_TEXT = (
    "本报告是规则命中清单，不是查重报告，也不是法律意见。"
    "它只提示风险，不对原创性或侵权风险作出任何承诺。"
    "最终是否可用，由你本人逐条核对后决定。"
)
#: 预检诚实声明（UI 固定小字，后端同时下发）。
PRECHECK_HONESTY_NOTE = "预检只做形态判断，不构成法律判断；是否合规由你本人核对。"

# ── 预检阈值（PRD §2.2.1）──
REWRITE_INPUT_LIMIT = 1200      # R-len：单字段字数上限
QUOTE_MIN_CHARS = 30            # R-quote：引号片段最短字数
QUOTE_MIN_HITS = 3              # R-quote：命中处数
PARA_MIN_RUN = 5                # R-para：连续散文段数
EXEMPT_MIN_MARKERS = 3          # R-exempt：结构化标记行数
EXCERPT_CHARS = 40              # hits 里回显的上下文长度（绝不回显全文）
EVIDENCE_EXCERPT_CHARS = 60     # Evidence.excerpt 上限

# ── 算法阈值 ──
MAX_SEQ_LEN = 64                # L5 序列长度上限（超出截断并 warn）
LCS_FAIL_RATIO = 2 / 3          # ≥ → fail（闭区间，命门层从严）
LCS_WARN_RATIO = 1 / 2          # ≥ → warn
LCS_JACCARD_WARN = 0.8          # LCS 低但多重集重合高 → 疑似「打乱顺序照搬」
L3_FAIL_SIM = 0.75              # 指纹综合相似度 ≥ → fail
L3_WARN_SIM = 0.5               # 指纹综合相似度 ≥ → warn
L3_KIND_JACCARD_FAIL = 0.8      # 度数全等 且 类型/权力 Jaccard ≥ → fail
OTO_FAIL_JACCARD = 0.8          # ⑦ 角色数相同且功能位多重集 Jaccard ≥ → fail
OTO_WARN_JACCARD = 0.6          # ⑦ → warn
REVERSAL_POS_TOL = 0.1          # ⑧ 反转位置容差（归一化到 0-1 进度）
NGRAM_SIZE = 12                 # ⑤ 原句比对（P1-2 启用，P0 恒 unavailable）
#: 浮点比较容差（避免 2/3 的二进制表示让「恰好等于」被判成 warn）。
EPS = 1e-9

#: 质检四态（★禁止增加第五态，禁止混淆★）。
CheckStatus = Literal["pass", "warn", "fail", "unavailable"]
CHECK_STATUSES: tuple[str, str, str, str] = ("pass", "warn", "fail", "unavailable")

#: 八项质检的 key 枚举（PRD §2.4 → 可执行定稿）。
CHECK_PROPER_NOUN = "proper_noun"
CHECK_SIGNATURE_SCENE = "signature_scene"
CHECK_RELATION_TOPOLOGY = "relation_topology"
CHECK_BEAT_SEQUENCE = "beat_sequence"
CHECK_NEAR_DUPLICATE = "near_duplicate"
CHECK_UNIQUE_PROP = "unique_prop"
CHECK_ONE_TO_ONE = "one_to_one_character"
CHECK_ISOMORPHIC_REVERSAL = "isomorphic_reversal"
CHECK_KEYS: tuple[str, ...] = (
    CHECK_PROPER_NOUN,
    CHECK_SIGNATURE_SCENE,
    CHECK_RELATION_TOPOLOGY,
    CHECK_BEAT_SEQUENCE,
    CHECK_NEAR_DUPLICATE,
    CHECK_UNIQUE_PROP,
    CHECK_ONE_TO_ONE,
    CHECK_ISOMORPHIC_REVERSAL,
)

#: 反向校验三问（PRD §2.4，人工项，必须全勾选）。
REVERSE_THREE: tuple[tuple[str, str], ...] = (
    ("遮住专有名词，还能认出是原作吗？", "不能"),
    ("关系图与原作并排，是同一张图吗？", "不是"),
    ("名场面顺序连成一行，是同一条路径吗？", "不是"),
)


# ─────────────────────────── 异常 ───────────────────────────

class RewriteError(RuntimeError):
    """仿写域基础异常。"""


class RewriteSourceRejectedError(NovelValidationError):
    """输入侧预检命中（疑似原文）—— 422 `rewrite_source_rejected`。

    Attributes:
        hits: 命中明细 `[{field, rule, excerpt, hint}]`，供路由层放进 detail。
            `excerpt` ≤ 40 字，**绝不回显被拒原文全文**（P0-3④）。
    """

    def __init__(self, message: str, hits: list[dict] | None = None) -> None:
        super().__init__(message, code=ERR_REWRITE_SOURCE_REJECTED)
        self.hits: list[dict] = list(hits or [])


class RewriteGateBlockedError(NovelValidationError):
    """五层硬闸门未过（L3/L5 缺表且未显式 skip）—— 422 `rewrite_gate_blocked`。

    Attributes:
        missing: 缺失的层名列表（如 `["L3"]`）。
    """

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message, code=ERR_REWRITE_GATE_BLOCKED)
        self.missing: list[str] = list(missing or [])


class RewriteAckRequiredError(NovelValidationError):
    """采纳闸门未过（未勾选 / 未 ack）—— 422 `rewrite_ack_required`。"""

    def __init__(self, message: str = "请先完成全部勾选与二次确认") -> None:
        super().__init__(message, code=ERR_REWRITE_ACK_REQUIRED)


# ─────────────────────────── 蓝图模型 ───────────────────────────

class SourceRef(BaseModel):
    """来源标注（只存笔记/描述，**绝不存原文**）。"""

    label: str = ""
    work_type: str = ""
    note: str = ""


class FunctionSlot(BaseModel):
    """抽象层功能位（思想层面，不受著作权保护）。"""

    slot: str = ""
    trait: str = ""


class AbstractLayer(BaseModel):
    """抽象层：只继承情绪公式，不继承任何表达。"""

    function_slots: list[FunctionSlot] = Field(default_factory=list)
    emotion_beats: list[str] = Field(default_factory=list)
    info_gap: list[str] = Field(default_factory=list)
    reversal_types: list[str] = Field(default_factory=list)
    #: ★可空；空 → ⑧ 的位置维度降级为 warn/unavailable。
    reversal_positions: list[float] = Field(default_factory=list)
    motifs: list[str] = Field(default_factory=list)
    pacing: str = ""


class RelationEdge(BaseModel):
    """关系图有向边。`from`/`to` 是 Python 保留字 → 沿用既有 `RelationDelta` 的别名做法。

    磁盘与 HTTP JSON 的键名是 `from` / `to`；Pydantic 字段名是 `source` / `target`。
    """

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(default="", alias="from")
    target: str = Field(default="", alias="to")
    #: 关系类型：师徒 / 同门 / 敌对 / 管理 / 血缘 …
    kind: str = ""
    #: 权力流向："高→低" / "低→高" / "对等"。
    power: str = ""


class L1Symbols(BaseModel):
    """L1 符号层。"""

    #: 原作专名黑名单（判据）。
    banned: list[str] = Field(default_factory=list)
    new_lexicon: dict[str, str] = Field(default_factory=dict)


class L2Scenes(BaseModel):
    """L2 场景层。"""

    banned: list[str] = Field(default_factory=list)
    new_scenes: list[str] = Field(default_factory=list)


class L3Relations(BaseModel):
    """L3 关系层（★命门★）。

    `source_fingerprint` 兼容 PRD 草案：主理人 A1 裁定**降级为只读展示**，
    判据只用 `source_graph` + `new_graph`（结构化边列表），指纹由算法算出。
    """

    source_graph: list[RelationEdge] = Field(default_factory=list)
    source_fingerprint: dict[str, object] | None = None
    new_graph: list[RelationEdge] = Field(default_factory=list)


class L4Events(BaseModel):
    """L4 事件层。"""

    new_causal_chain: list[str] = Field(default_factory=list)


class L5Beats(BaseModel):
    """L5 桥段序列层（★命门★）。"""

    source_seq: list[str] = Field(default_factory=list)
    new_seq: list[str] = Field(default_factory=list)


class RebuildLayer(BaseModel):
    """五层重建表。"""

    L1_symbols: L1Symbols = Field(default_factory=L1Symbols)
    L2_scenes: L2Scenes = Field(default_factory=L2Scenes)
    L3_relations: L3Relations = Field(default_factory=L3Relations)
    L4_events: L4Events = Field(default_factory=L4Events)
    L5_beats: L5Beats = Field(default_factory=L5Beats)


class GateInfo(BaseModel):
    """五层硬闸门状态。"""

    required_layers: list[str] = Field(default_factory=lambda: ["L3", "L5"])
    skipped_at: str | None = None
    skip_reason: str = ""


class Blueprint(BaseModel):
    """`rewrite/blueprint.json`：结构蓝图（仿写的唯一权威输入）。"""

    version: int = 1
    id: str = ""
    book_id: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    source_ref: SourceRef = Field(default_factory=SourceRef)
    abstract: AbstractLayer = Field(default_factory=AbstractLayer)
    rebuild: RebuildLayer = Field(default_factory=RebuildLayer)
    gate: GateInfo = Field(default_factory=GateInfo)


# ─────────────────────────── 报告模型 ───────────────────────────

class RelationFingerprint(BaseModel):
    """关系拓扑指纹（L3 判据）。"""

    node_count: int = 0
    edge_count: int = 0
    #: 排序后的节点名。
    nodes: list[str] = Field(default_factory=list)
    #: 无向度数列，降序（孤立节点的 0 是有效信息，保留）。
    degrees: list[int] = Field(default_factory=list)
    #: 关系类型多重集。
    kinds: dict[str, int] = Field(default_factory=dict)
    #: 权力流向多重集 `{"-1": n, "0": n, "1": n, "unknown": n}`。
    flow: dict[str, int] = Field(default_factory=dict)
    #: power 无法解析的边数。
    unknown_power: int = 0


class Evidence(BaseModel):
    """命中证据（行号 + ≤60 字 excerpt）。"""

    line: int | None = None
    excerpt: str = ""


class CheckItem(BaseModel):
    """质检对照项（八项之一）。"""

    key: str
    layer: str
    mode: str
    status: CheckStatus
    detail: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    human_tip: str = ""
    human_checked: bool = False
    checked_at: str | None = None
    #: 算法中间量，供 UI 展示「可核对性」（为什么这么判）。
    metrics: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _semantics(self) -> CheckItem:
        """状态语义硬保证：禁止无据阻断，禁止无人工指引的不可用。"""
        if self.status == "fail" and not (self.evidence or self.detail):
            raise ValueError("fail 必须给出证据或说明，禁止无据阻断")
        if self.status == "unavailable" and not self.human_tip:
            raise ValueError("unavailable 必须给出 human_tip（如何人工核对）")
        if self.status == "unavailable" and self.human_checked is True and not self.checked_at:
            self.checked_at = now_iso()
        return self


class ReverseQuestion(BaseModel):
    """反向校验三问之一（纯人工项）。"""

    q: str
    expect: str
    human_checked: bool = False
    human_answer: str | None = None


class ReportSummary(BaseModel):
    """报告汇总。**永远是 `checks` 的函数**，由 `RewriteReport` 强算。"""

    blocking: int = 0           # fail 数
    warn: int = 0
    unavailable: int = 0
    passed: int = 0
    adoptable: bool = False


class ReportAck(BaseModel):
    """采纳二次确认留痕。"""

    required: bool = True
    acknowledged_at: str | None = None
    disclaimer_version: str | None = None
    #: ★勾选结果随 ack 落盘（PRD §2.5 T4）。
    checked_keys: list[str] = Field(default_factory=list)


class Disclaimer(BaseModel):
    """报告抬头（不可关闭、不可折叠掉）。"""

    version: str = DISCLAIMER_VERSION
    text: str = DISCLAIMER_TEXT


class RewriteReport(BaseModel):
    """`rewrite/reports/<rewrite_id>.json`：原创质检报告 + ack。"""

    rewrite_id: str
    blueprint_id: str = ""
    book_id: str = ""
    chapter_id: str | None = None
    kind: str = "chapter"       # plan | outline | chapter
    draft_file: str = ""        # 相对 rewrite/ 的路径
    generated_at: str = ""
    disclaimer: Disclaimer = Field(default_factory=Disclaimer)
    checks: list[CheckItem] = Field(default_factory=list)
    reverse_three: list[ReverseQuestion] = Field(default_factory=list)
    summary: ReportSummary = Field(default_factory=ReportSummary)
    ack: ReportAck = Field(default_factory=ReportAck)
    #: ★P2-4 采纳留痕：区分「首次采纳」与「重复采纳」（幂等覆盖不损坏数据，
    #: 但审计上必须能分开；`ack.acknowledged_at` 只记录「确认」，不记录「落书」）。
    adopted_at: str | None = None
    adopted_target: str | None = None      # chapter | outline

    @model_validator(mode="after")
    def _recompute_summary(self) -> RewriteReport:
        """`summary` 永远是 `checks` 的函数，外部传什么都不算。

        硬保证：不可能出现「有 fail 但 `adoptable=True`」，
        也不可能把 `unavailable` 数进 `passed`。
        """
        counts = {"pass": 0, "warn": 0, "fail": 0, "unavailable": 0}
        for item in self.checks:
            counts[item.status] = counts.get(item.status, 0) + 1

        blocking = counts["fail"]
        pending = [
            item for item in self.checks if item.status in ("warn", "unavailable")
        ]
        all_checked = all(item.human_checked for item in pending)
        reverse_ok = bool(self.reverse_three) and all(
            question.human_checked for question in self.reverse_three
        )
        acked = bool(self.ack.acknowledged_at)

        self.summary = ReportSummary(
            blocking=blocking,
            warn=counts["warn"],
            unavailable=counts["unavailable"],
            passed=counts["pass"],
            adoptable=blocking == 0 and all_checked and reverse_ok and acked,
        )
        return self


# ─────────────────────────── 仿写 job 模型 ───────────────────────────

class RewriteArtifacts(BaseModel):
    """仿写 job 产物（`checkpoints/rw-*.json` 内，不再另落 `rewrite/runs/`）。"""

    precheck_hits: list[dict] = Field(default_factory=list)
    draft_rel: str | None = None
    draft_text: str | None = None
    outline_patch: dict | None = None
    #: ⑦ 的输入（来自 ```rw-roles 结构化块）。
    character_table: list[dict] = Field(default_factory=list)
    #: ⑧ 的输入（来自 ```rw-reversals 结构化块）。
    reversal_table: list[dict] = Field(default_factory=list)
    #: 写后自检命中清单（复用 `novel_ai.lint_text()`，只提示不改写）。
    lint_hits: list[dict] = Field(default_factory=list)


class RewriteJob(BaseModel):
    """`checkpoints/rw-*.json`：仿写任务（4 步状态机）。"""

    job_id: str = ""
    book_id: str = ""
    chapter_id: str | None = None
    kind: str = "plan"          # plan | outline | chapter
    blueprint_id: str = ""
    risk_ack: bool = False
    skip_gate: bool = False
    created_at: str = ""
    updated_at: str = ""
    steps: list[JobStep] = Field(default_factory=list)
    artifacts: RewriteArtifacts = Field(default_factory=RewriteArtifacts)
    rewrite_id: str | None = None
    status: str = "queued"      # queued|running|done|failed|cancelled
    failed_step: str | None = None


# ─────────────────────────── 预检（本地零依赖，不调 AI）───────────────────────────

_STRUCT_MARKER_RE = re.compile(
    r"^\s*(?:[-*+]\s|\||#{1,6}\s|[A-Za-z一-龥]{1,12}\s*[:：])", re.MULTILINE
)
_QUOTE_RE = re.compile(r"[“\"「『]([^”\"」』]{1,400}?)[”\"」』]")
_SENT_END = "。！？!?…」』”）\"'"

#: 结构笔记示例（自研文案，用于引导用户改填；绝不回显被拒原文）。
PRECHECK_SAMPLE = (
    "- 功能位：主角 / 导师（表面提携实则压制）/ 对手 / 盟友 / 背叛者\n"
    "- 情绪节拍：压抑 → 误解 → 孤立 → 爆发达 → 余痛\n"
    "- 反转类型：身份错位（第 4 章，进度 0.6）\n"
    "节奏：六章骨架，刺激点密度 每章 1 个"
)


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """取前 `limit` 字作为上下文片段 —— **绝不返回完整字段内容**（P0-3④）。"""
    body = re.sub(r"\s+", "", str(text or ""))
    if len(body) <= limit:
        return body
    return body[:limit] + "…"


def _hit(field: str, rule: str, text: str, hint: str) -> dict:
    """构造一条预检命中（excerpt ≤40 字，不回显全文）。"""
    return {"field": field, "rule": rule, "excerpt": _excerpt(text), "hint": hint}


def precheck_text(field: str, text: str) -> list[dict]:
    """输入侧预检四规则（PRD §2.2.1，本地零依赖，不调 AI）。

    规则优先级：**R-exempt 最高**（≥3 行结构化标记 → 整体放行，视为笔记）；
    否则依次 R-len / R-quote / R-para。

    Args:
        field: 字段名（用于命中定位，如 `source_ref.note`）。
        text: 待检文本。

    Returns:
        命中列表 `[{field, rule, excerpt, hint}]`；通过返回 `[]`。
    """
    body = str(text or "").strip()
    if not body:
        return []

    # ── R-exempt 结构化豁免：优先级最高，命中即整体放行 ──
    if len(_STRUCT_MARKER_RE.findall(body)) >= EXEMPT_MIN_MARKERS:
        return []

    hits: list[dict] = []
    words = count_words(body)

    # ── R-len 长度 ──
    if words > REWRITE_INPUT_LIMIT:
        hits.append(
            _hit(field, "R-len", body, "单字段超过 1200 字且无结构化标记，疑似原文全文")
        )

    # ── R-quote 对白密度：≥30 字引号片段 ≥3 处 ──
    long_quotes = [m for m in _QUOTE_RE.findall(body) if count_words(m) >= QUOTE_MIN_CHARS]
    if len(long_quotes) >= QUOTE_MIN_HITS:
        hits.append(
            _hit(
                field,
                "R-quote",
                body,
                f"出现 {len(long_quotes)} 处 ≥30 字的引号片段，疑似对白摘录",
            )
        )

    # ── R-para 段落连续性：连续 ≥5 段以句号/引号结尾 ──
    run = 0
    best = 0
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            run = 0
            continue
        run = run + 1 if stripped[-1] in _SENT_END else 0
        best = max(best, run)
    if best >= PARA_MIN_RUN:
        hits.append(
            _hit(
                field,
                "R-para",
                body,
                f"连续 {best} 段以句号/引号结尾且无列表标记，疑似散文原文",
            )
        )
    return hits


def precheck_blueprint(bp: Blueprint) -> list[dict]:
    """遍历蓝图的全部自由文本字段跑预检（PRD §2.2.1「所有自由文本字段」）。

    字段清单（ARCHITECTURE-rewrite.md §11-⑨ 列举）：
    `source_ref.note` / `source_ref.label` / `abstract.pacing` /
    `abstract.emotion_beats` 逐项 / `abstract.info_gap` 逐项 / `abstract.motifs` 逐项 /
    `abstract.function_slots[*].trait` / `rebuild.L4_events.new_causal_chain` 逐项 /
    `rebuild.L1_symbols.new_lexicon` 的 value。
    """
    hits: list[dict] = []
    hits.extend(precheck_text("source_ref.note", bp.source_ref.note))
    hits.extend(precheck_text("source_ref.label", bp.source_ref.label))
    hits.extend(precheck_text("abstract.pacing", bp.abstract.pacing))

    for index, item in enumerate(bp.abstract.emotion_beats):
        hits.extend(precheck_text(f"abstract.emotion_beats[{index}]", item))
    for index, item in enumerate(bp.abstract.info_gap):
        hits.extend(precheck_text(f"abstract.info_gap[{index}]", item))
    for index, item in enumerate(bp.abstract.motifs):
        hits.extend(precheck_text(f"abstract.motifs[{index}]", item))
    for index, slot in enumerate(bp.abstract.function_slots):
        hits.extend(precheck_text(f"abstract.function_slots[{index}].trait", slot.trait))
    for index, item in enumerate(bp.rebuild.L4_events.new_causal_chain):
        hits.extend(precheck_text(f"rebuild.L4_events.new_causal_chain[{index}]", item))
    for key, value in bp.rebuild.L1_symbols.new_lexicon.items():
        hits.extend(precheck_text(f"rebuild.L1_symbols.new_lexicon[{key}]", value))
    return hits


def clean_precheck_response(hits: list[dict]) -> dict:
    """构造预检拒绝的响应体：**绝不回显被拒原文全文**（P0-3④）。

    Returns:
        `{ok, passed, hits, sample, honesty_note}` —— `hits` 只含
        `field/rule/excerpt(≤40字)/hint`。
    """
    return {
        "ok": False,
        "passed": False,
        "code": ERR_REWRITE_SOURCE_REJECTED,
        "hits": [
            {
                "field": str(item.get("field", "")),
                "rule": str(item.get("rule", "")),
                "excerpt": str(item.get("excerpt", ""))[:EXCERPT_CHARS],
                "hint": str(item.get("hint", "")),
            }
            for item in hits
        ],
        "sample": PRECHECK_SAMPLE,
        "honesty_note": PRECHECK_HONESTY_NOTE,
    }


# ─────────────────────────── 质检算法（纯函数，全部可脱离 IO 单测）──────────────────────────

def banned_term_hits(text: str, banned: list[str]) -> list[Evidence]:
    """黑名单子串匹配（去空白后比对，避免换行把命中拆开）。

    Args:
        text: 产物文本。
        banned: 黑名单词（原作专名 / 标志场景 / 独特道具）。

    Returns:
        `Evidence` 列表（行号 + ≤60 字 excerpt）。空黑名单 → 空列表
        （调用方据此置 `unavailable`，绝不置 pass）。
    """
    terms = [str(item).strip() for item in (banned or []) if str(item).strip()]
    if not terms:
        return []

    hits: list[Evidence] = []
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        compact = re.sub(r"\s+", "", line)
        if not compact:
            continue
        for term in terms:
            compact_term = re.sub(r"\s+", "", term)
            if compact_term and compact_term in compact:
                hits.append(
                    Evidence(line=index, excerpt=_excerpt(line, EVIDENCE_EXCERPT_CHARS))
                )
                break
    return hits


def _norm_kind(kind: str) -> str:
    """关系类型归一：去空白 + 小写（中英混填时也能对齐）。"""
    return re.sub(r"\s+", "", str(kind or "")).lower()


def _power_sign(power: str) -> str:
    """权力流向 → `-1` / `+1` / `0` / `unknown`。

    `'高→低'`（施压/支配）→ `-1`；`'低→高'`（被压制/仰视）→ `+1`；
    `'对等'/'平级'` → `0`；无法解析 → `unknown`。
    """
    text = re.sub(r"\s", "", str(power or ""))
    if "高" in text and "低" in text:
        return "-1" if text.index("高") < text.index("低") else "+1"
    if any(key in text for key in ("对等", "平级", "平行", "均衡")):
        return "0"
    return "unknown"


def relation_fingerprint(edges: list[RelationEdge]) -> RelationFingerprint:
    """关系拓扑指纹：无向度数列（降序）+ 关系类型多重集 + 权力流向多重集。

    度数按**无向**计（社会关系是双向连接）；孤立节点保留在序列中
    （降序末尾的 0 是有效信息，防「多一个孤立人」就判 pass 的漏洞）。
    """
    items = list(edges or [])
    nodes = sorted({e.source for e in items} | {e.target for e in items})
    degree = {name: 0 for name in nodes}
    for edge in items:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1

    kinds = Counter(_norm_kind(edge.kind) for edge in items)
    flow = Counter(_power_sign(edge.power) for edge in items)
    return RelationFingerprint(
        node_count=len(nodes),
        edge_count=len(items),
        nodes=nodes,
        degrees=sorted(degree.values(), reverse=True),
        kinds=dict(kinds),
        flow=dict(flow),
        unknown_power=sum(1 for edge in items if _power_sign(edge.power) == "unknown"),
    )


def multiset_jaccard(a: dict[str, int], b: dict[str, int]) -> float:
    """多重集 Jaccard = Σmin / Σmax（保序保重数）。空并集 → 0.0。"""
    keys = set(a or {}) | set(b or {})
    inter = sum(min((a or {}).get(k, 0), (b or {}).get(k, 0)) for k in keys)
    union = sum(max((a or {}).get(k, 0), (b or {}).get(k, 0)) for k in keys)
    return inter / union if union else 0.0


def compare_fingerprint(
    src: RelationFingerprint, new: RelationFingerprint
) -> tuple[CheckStatus, dict]:
    """L3 关系拓扑比对（★命门★）。

    判定顺序（ARCHITECTURE-rewrite.md §5.2 定死）：
      1. 任一侧空图 → `unavailable`（无样本，绝不 pass）；
      2. 规模差异极大 → `pass`（图都重构了）；
      3. `power` 全不可解析 → `unavailable`（权力流向无法量化）；
      4. 三元组全等 → `fail`；
      5. 度数全等 + 类型/权力 Jaccard ≥0.8 → `fail`；
      6. 度数全等但分布不同 → `warn`；
      7. 综合相似度 sim ≥0.75 fail / ≥0.5 warn / 否则 pass。
    """
    if src.edge_count == 0 or new.edge_count == 0:
        return "unavailable", {
            "reason": "原作关系图或新作关系图为空，无法自动比对",
            "src_edges": src.edge_count,
            "new_edges": new.edge_count,
        }

    biggest = max(src.edge_count, new.edge_count)
    if abs(src.edge_count - new.edge_count) > max(2, 0.5 * biggest):
        return "pass", {
            "reason": "关系图规模显著不同",
            "src_edges": src.edge_count,
            "new_edges": new.edge_count,
        }

    if src.unknown_power == src.edge_count or new.unknown_power == new.edge_count:
        return "unavailable", {
            "reason": "权力流向全部无法解析，无法量化比对 —— 请人工并排比对两张关系图",
            "src_unknown": src.unknown_power,
            "new_unknown": new.unknown_power,
        }

    # 度数列补 0 对齐（角色数不同 / 孤立节点）
    length = max(len(src.degrees), len(new.degrees))
    a = list(src.degrees) + [0] * (length - len(src.degrees))
    b = list(new.degrees) + [0] * (length - len(new.degrees))
    same_degrees = a == b

    kind_j = multiset_jaccard(src.kinds, new.kinds)
    flow_j = multiset_jaccard(src.flow, new.flow)
    common: dict[str, object] = {
        "degrees_src": a,
        "degrees_new": b,
        "kind_jaccard": round(kind_j, 3),
        "flow_jaccard": round(flow_j, 3),
        "src_edges": src.edge_count,
        "new_edges": new.edge_count,
    }

    # L3-A：三元组全等 → fail
    if same_degrees and src.kinds == new.kinds and src.flow == new.flow:
        return "fail", {**common, "reason": "关系拓扑指纹与原作完全相同"}

    # L3-B：度数全等 + 类型/权力高度重合 → fail
    if same_degrees and (kind_j >= L3_KIND_JACCARD_FAIL or flow_j >= L3_KIND_JACCARD_FAIL):
        return "fail", {
            **common,
            "reason": "度数序列相同且关系类型/权力流向高度重合",
        }

    # L3-C：度数同但类型分布不同 → warn
    if same_degrees:
        return "warn", {
            **common,
            "reason": "度数序列相同但类型分布不同，请人工并排比对两张关系图",
        }

    total = sum(a) + sum(b)
    deg_sim = 1.0 - sum(abs(x - y) for x, y in zip(a, b, strict=False)) / max(1, total)
    sim = 0.5 * deg_sim + 0.3 * kind_j + 0.2 * flow_j
    metrics = {**common, "deg_sim": round(deg_sim, 3), "sim": round(sim, 3)}
    if sim >= L3_FAIL_SIM - EPS:
        return "fail", {**metrics, "reason": "关系拓扑综合相似度过高"}
    if sim >= L3_WARN_SIM - EPS:
        return "warn", {**metrics, "reason": "关系拓扑存在中等相似度，建议人工并排比对"}
    return "pass", metrics


#: 自研 24 个桥段标签（L5 序列元素）。
BEAT_TAGS: tuple[str, ...] = (
    "受辱", "隐忍", "反击", "反转", "清算", "误解", "孤立", "爆发达", "余痛", "失去",
    "获得", "背叛", "结盟", "试炼", "揭露", "逃亡", "追击", "抉择", "牺牲", "重逢",
    "伪装", "布局", "收网", "顿悟",
)

#: 自研同义词归一表（约 30 条）。用户自由输入的词归一化后保留原样，
#: 只与完全相同的词匹配（不做模糊匹配），未识别的放进 `metrics.unrecognized`。
BEAT_SYNONYMS: dict[str, str] = {
    "打脸": "反击",
    "翻盘": "反击",
    "打压下": "受辱",
    "受压": "受辱",
    "压抑": "受辱",
    "翻身": "爆发达",
    "高潮": "爆发达",
    "逆袭": "爆发达",
    "爽点": "爆发达",
    "真相大白": "揭露",
    "掉马": "揭露",
    "开挂": "获得",
    "奇遇": "获得",
    "误会": "误解",
    "错怪": "误解",
    "排挤": "孤立",
    "众叛亲离": "孤立",
    "忍辱": "隐忍",
    "蛰伏": "隐忍",
    "复仇": "清算",
    "算账": "清算",
    "出卖": "背叛",
    "反水": "背叛",
    "组队": "结盟",
    "联手": "结盟",
    "考验": "试炼",
    "历练": "试炼",
    "逃脱": "逃亡",
    "追赶": "追击",
    "选择": "抉择",
    "取舍": "抉择",
    "重聚": "重逢",
    "潜伏": "伪装",
    "谋划": "布局",
    "设局": "布局",
    "揭穿": "收网",
    "醒悟": "顿悟",
    "坠落": "失去",
}

#: 自研功能位同义词归一表（⑦）。
SLOT_SYNONYMS: dict[str, str] = {
    "师父": "导师",
    "师长": "导师",
    "师傅": "导师",
    "反派": "对手",
    "敌人": "对手",
    "宿敌": "对手",
    "伙伴": "盟友",
    "同伴": "盟友",
    "内鬼": "背叛者",
    "叛徒": "背叛者",
    "主角": "主角",
    "女主": "爱情对象",
    "男主": "爱情对象",
    "恋人": "爱情对象",
}

#: 自研反转类型同义词归一表（⑧）。
REVERSAL_SYNONYMS: dict[str, str] = {
    "真假身份": "身份错位",
    "身份反转": "身份错位",
    "信息差": "信息差误会",
    "误会": "信息差误会",
    "底牌错位": "底牌错位",
    "隐藏底牌": "底牌错位",
    "背叛反转": "信任崩塌",
    "反水": "信任崩塌",
    "假死": "生死反转",
    "复活": "生死反转",
}


def normalize_beat(tag: str) -> str:
    """桥段标签归一：去括号注释 + 去空白 + 小写 + 同义词表。"""
    text = re.sub(r"[（(].*?[)）]", "", str(tag or ""))
    text = re.sub(r"\s+", "", text).lower()
    return BEAT_SYNONYMS.get(text, text)


def normalize_slot(slot: str) -> str:
    """功能位归一：去空白 + 同义词表。"""
    text = re.sub(r"\s+", "", str(slot or ""))
    return SLOT_SYNONYMS.get(text, text)


def normalize_reversal(kind: str) -> str:
    """反转类型归一：去空白 + 同义词表。"""
    text = re.sub(r"\s+", "", str(kind or ""))
    return REVERSAL_SYNONYMS.get(text, text)


def lcs_len(a: list[str], b: list[str]) -> int:
    """最长公共子序列长度（标准 DP，滚动数组，长度上限 `MAX_SEQ_LEN`）。"""
    left = list(a)[:MAX_SEQ_LEN]
    right = list(b)[:MAX_SEQ_LEN]
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for x in left:
        cur = [0] * (len(right) + 1)
        for j, y in enumerate(right, start=1):
            if x == y:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = prev[j] if prev[j] >= cur[j - 1] else cur[j - 1]
        prev = cur
    return prev[len(right)]


def lcs_trace(a: list[str], b: list[str]) -> list[str]:
    """回溯公共子序列本身 —— 报告里展示「到底是哪几个桥段撞了」（可核对性关键）。"""
    left = list(a)[:MAX_SEQ_LEN]
    right = list(b)[:MAX_SEQ_LEN]
    if not left or not right:
        return []
    rows = len(left) + 1
    cols = len(right) + 1
    table = [[0] * cols for _ in range(rows)]
    for i, x in enumerate(left, start=1):
        for j, y in enumerate(right, start=1):
            if x == y:
                table[i][j] = table[i - 1][j - 1] + 1
            else:
                table[i][j] = max(table[i - 1][j], table[i][j - 1])
    result: list[str] = []
    i, j = len(left), len(right)
    while i > 0 and j > 0:
        if left[i - 1] == right[j - 1]:
            result.append(left[i - 1])
            i -= 1
            j -= 1
        elif table[i - 1][j] >= table[i][j - 1]:
            i -= 1
        else:
            j -= 1
    result.reverse()
    return result


def check_beat_sequence(
    source_seq: list[str], new_seq: list[str]
) -> tuple[CheckStatus, dict]:
    """L5 桥段序列 LCS 比对（★命门★）。

    阈值边界（闭区间，`ratio >= T - EPS`，命门层从严）：
      - `ratio ≥ 2/3`（含恰好等于）→ **fail**；
      - `ratio ≥ 1/2` → **warn**；
      - `ratio < 1/2` 但多重集 Jaccard ≥0.8 → **warn**（疑似打乱顺序照搬）；
      - 否则 → pass；任一侧空序列 → **unavailable**。
    """
    a = [normalize_beat(x) for x in (source_seq or []) if str(x).strip()]
    b = [normalize_beat(x) for x in (new_seq or []) if str(x).strip()]
    if not a or not b:
        return "unavailable", {
            "reason": "原作桥段序列或新作桥段序列为空，无法自动比对"
        }

    truncated = len(a) > MAX_SEQ_LEN or len(b) > MAX_SEQ_LEN
    trimmed_a = a[:MAX_SEQ_LEN]
    trimmed_b = b[:MAX_SEQ_LEN]
    common_len = lcs_len(trimmed_a, trimmed_b)
    longest = max(len(trimmed_a), len(trimmed_b))
    ratio = common_len / longest if longest else 0.0
    jaccard = multiset_jaccard(dict(Counter(a)), dict(Counter(b)))
    common = lcs_trace(trimmed_a, trimmed_b)
    unrecognized = sorted(
        {tag for tag in a + b if tag not in BEAT_TAGS and tag not in BEAT_SYNONYMS}
    )

    metrics: dict[str, object] = {
        "lcs_len": common_len,
        "max_len": longest,
        "ratio": round(ratio, 3),
        "jaccard": round(jaccard, 3),
        "common": common,
        "truncated": truncated,
        "src_norm": a,
        "new_norm": b,
        "unrecognized": unrecognized,
    }
    if ratio >= LCS_FAIL_RATIO - EPS:
        return "fail", {**metrics, "reason": "桥段序列与原作重合度过高（LCS ≥ 2/3）"}
    if ratio >= LCS_WARN_RATIO - EPS:
        return "warn", {**metrics, "reason": "桥段序列与原作存在中等重合（LCS ≥ 1/2）"}
    if jaccard >= LCS_JACCARD_WARN - EPS:
        return "warn", {
            **metrics,
            "reason": "顺序不同但桥段集合高度重合，疑似打乱顺序照搬",
        }
    return "pass", metrics


def check_one_to_one(
    source_slots: list[FunctionSlot], new_roles: list[dict]
) -> tuple[CheckStatus, dict]:
    """⑦ 一对一人物映射检测（典型「换人名不换构」）。

    角色数不同 → **pass**（PRD 明说：不构成一对一）；
    角色数相同 + 功能位多重集完全相等 → **fail**；
    Jaccard ≥0.8 → fail / ≥0.6 → warn；缺任一表 → **unavailable**。
    """
    slots = list(source_slots or [])
    roles = [item for item in (new_roles or []) if isinstance(item, dict)]
    if not slots or not roles:
        return "unavailable", {
            "reason": "缺原作功能位表或新作角色表，需人工核对"
        }

    src_count = len(slots)
    new_count = len(roles)
    src_ms = Counter(normalize_slot(s.slot) for s in slots)
    new_ms = Counter(normalize_slot(str(item.get("slot", ""))) for item in roles)

    if src_count != new_count:
        return "pass", {
            "src_count": src_count,
            "new_count": new_count,
            "reason": "角色数不同，不构成一对一映射",
        }

    if src_ms == new_ms:
        return "fail", {
            "src_slots": dict(src_ms),
            "new_slots": dict(new_ms),
            "reason": "角色数相同且功能位一一对应 —— 典型「换人名不换关系结构」",
        }

    jaccard = multiset_jaccard(dict(src_ms), dict(new_ms))
    metrics = {
        "src_count": src_count,
        "new_count": new_count,
        "src_slots": dict(src_ms),
        "new_slots": dict(new_ms),
        "jaccard": round(jaccard, 3),
    }
    if jaccard >= OTO_FAIL_JACCARD - EPS:
        return "fail", {**metrics, "reason": "功能位多重集高度重合"}
    if jaccard >= OTO_WARN_JACCARD - EPS:
        return "warn", {**metrics, "reason": "功能位存在中等重合，请人工核对是否换名不换构"}
    return "pass", metrics


def _to_ratio(position: float, total_chapters: int) -> float:
    """位置归一到 0-1 进度：章序号（>1）→ `(idx-1)/max(1, total-1)`。"""
    try:
        value = float(position)
    except (TypeError, ValueError):
        return 0.0
    if value > 1.0:
        denominator = max(1, int(total_chapters) - 1)
        return (value - 1.0) / denominator
    return value


def check_isomorphic_reversal(
    src_types: list[str],
    src_positions: list[float],
    new_reversals: list[dict],
    total_chapters: int = 0,
) -> tuple[CheckStatus, dict]:
    """⑧ 同构反转底牌检测。

    四态裁定：类型同+位置同 → **fail**；类型同+位置缺失 → **warn**；
    类型同+位置不同 → **pass**（换了位置 = 合法重建）；类型不同 → pass；
    缺样本 → **unavailable**。
    """
    types = [str(item) for item in (src_types or []) if str(item).strip()]
    reversals = [item for item in (new_reversals or []) if isinstance(item, dict)]
    if not types or not reversals:
        return "unavailable", {
            "reason": "缺原作反转类型或新作反转登记表，需人工核对"
        }

    src_kinds = {normalize_reversal(item) for item in types}
    src_pos = [_to_ratio(p, total_chapters) for p in (src_positions or [])]

    hits: list[dict] = []
    for item in reversals:
        if normalize_reversal(str(item.get("type", ""))) not in src_kinds:
            continue  # 类型不同 → 合法重建，不计
        ratio = item.get("position_ratio")
        if ratio is None and item.get("chapter_index") is not None:
            ratio = _to_ratio(item["chapter_index"], total_chapters)
        if src_pos and ratio is not None:
            try:
                value = float(ratio)
            except (TypeError, ValueError):
                value = None
            same = (
                any(abs(value - p) <= REVERSAL_POS_TOL for p in src_pos)
                if value is not None
                else None
            )
            hits.append({"type": item.get("type"), "same_position": same, "position": value})
        else:
            # 位置信息缺失（任一侧）→ 无法判定，标 None
            hits.append({"type": item.get("type"), "same_position": None, "position": ratio})

    if not hits:
        return "pass", {"reason": "未出现与原作同类型的反转"}
    if any(hit["same_position"] is True for hit in hits):
        return "fail", {"hits": hits, "reason": "反转类型与出现位置均与原作一致"}
    if any(hit["same_position"] is None for hit in hits):
        return "warn", {
            "hits": hits,
            "reason": "反转类型与原作相同，但位置信息不足 —— 请人工核对出现章节",
        }
    return "pass", {"hits": hits, "reason": "反转类型相同但出现位置不同（路径已重建）"}


# ─────────────────────────── 报告组装 ───────────────────────────

def _make_item(
    key: str,
    layer: str,
    mode: str,
    status: CheckStatus,
    *,
    detail: str = "",
    evidence: list[Evidence] | None = None,
    human_tip: str = "",
    metrics: dict[str, object] | None = None,
) -> CheckItem:
    """构造 `CheckItem`（`unavailable` 必须有 human_tip，`fail` 必须有依据）。"""
    return CheckItem(
        key=key,
        layer=layer,
        mode=mode,
        status=status,
        detail=detail,
        evidence=list(evidence or []),
        human_tip=human_tip,
        metrics=dict(metrics or {}),
    )


def _unavailable_tip(what: str) -> str:
    """无样本项的统一人工指引（绝不显示为 pass）。"""
    return (
        f"未提供{what}的比对样本，本项无法自动比对 —— 请人工核对，"
        "此项不会显示为通过。"
    )


def build_report(
    *,
    rewrite_id: str,
    blueprint: Blueprint,
    kind: str = "chapter",
    draft_text: str = "",
    outline_patch: dict | None = None,
    character_table: list[dict] | None = None,
    reversal_table: list[dict] | None = None,
    total_chapters: int = 0,
    chapter_id: str | None = None,
    draft_file: str = "",
    lint_hits: list[dict] | None = None,
) -> RewriteReport:
    """组装 8 项质检 + 反向三问（`summary` 由 `RewriteReport` validator 强算）。

    Args:
        rewrite_id: `rw-<book_id>-<ts>-<hex4>`。
        blueprint: 结构蓝图（判据来源）。
        kind: `plan` / `outline` / `chapter`。
        draft_text: 产物文本（L1/L2 命中判据）。
        outline_patch: 大纲补丁（大纲产物）。
        character_table: ⑦ 的输入（AI 结构化块；空 → unavailable）。
        reversal_table: ⑧ 的输入（AI 结构化块；空 → unavailable）。
        total_chapters: 总章数（⑧ 位置归一化用）。
        chapter_id: 关联章节（chapter 产物）。
        draft_file: 相对 `rewrite/` 的草稿路径。
        lint_hits: 写后自检命中（`novel_ai.lint_text()` 结果）。

    Returns:
        `RewriteReport`（`summary` 已强算，不可能「有 fail 但 adoptable=True」）。
    """
    body = str(draft_text or "")
    if kind == "outline" and outline_patch:
        body = body or json.dumps(outline_patch, ensure_ascii=False)

    rebuild = blueprint.rebuild
    abstract = blueprint.abstract
    checks: list[CheckItem] = []

    # ① 专名黑名单命中（L1，auto）
    banned_symbols = rebuild.L1_symbols.banned
    if banned_symbols:
        hits = banned_term_hits(body, banned_symbols)
        if hits:
            checks.append(
                _make_item(
                    CHECK_PROPER_NOUN,
                    "L1",
                    "auto",
                    "fail",
                    detail=f"产物中出现 {len(hits)} 处原作专名黑名单命中",
                    evidence=hits,
                    human_tip="把命中处的专名整体替换为你自造的名称，再重新自检。",
                    metrics={"banned_count": len(banned_symbols), "hit_count": len(hits)},
                )
            )
        else:
            checks.append(
                _make_item(
                    CHECK_PROPER_NOUN,
                    "L1",
                    "auto",
                    "pass",
                    detail="未命中原作专名黑名单",
                    metrics={"banned_count": len(banned_symbols), "hit_count": 0},
                )
            )
    else:
        checks.append(
            _make_item(
                CHECK_PROPER_NOUN,
                "L1",
                "auto",
                "unavailable",
                detail="未声明原作专名黑名单",
                human_tip=_unavailable_tip("原作专名黑名单"),
            )
        )

    # ② 标志场景黑名单命中（L2，auto）
    banned_scenes = rebuild.L2_scenes.banned
    if banned_scenes:
        hits = banned_term_hits(body, banned_scenes)
        if hits:
            checks.append(
                _make_item(
                    CHECK_SIGNATURE_SCENE,
                    "L2",
                    "auto",
                    "fail",
                    detail=f"产物中出现 {len(hits)} 处标志场景黑名单命中",
                    evidence=hits,
                    human_tip="把命中场景换成完全不同的场景（换地点 / 换事由 / 换参与者）。",
                    metrics={"banned_count": len(banned_scenes), "hit_count": len(hits)},
                )
            )
        else:
            checks.append(
                _make_item(
                    CHECK_SIGNATURE_SCENE,
                    "L2",
                    "auto",
                    "pass",
                    detail="未命中标志场景黑名单",
                    metrics={"banned_count": len(banned_scenes), "hit_count": 0},
                )
            )
    else:
        checks.append(
            _make_item(
                CHECK_SIGNATURE_SCENE,
                "L2",
                "auto",
                "unavailable",
                detail="未声明标志场景黑名单",
                human_tip=_unavailable_tip("标志场景黑名单"),
            )
        )

    # ③ 关系拓扑指纹（L3，auto，★命门★）
    src_fp = relation_fingerprint(rebuild.L3_relations.source_graph)
    new_fp = relation_fingerprint(rebuild.L3_relations.new_graph)
    status, metrics = compare_fingerprint(src_fp, new_fp)
    checks.append(
        _make_item(
            CHECK_RELATION_TOPOLOGY,
            "L3",
            "auto",
            status,
            detail=str(metrics.get("reason", "")),
            evidence=(
                [Evidence(line=None, excerpt=f"原作度数序列 {src_fp.degrees}")]
                if status == "fail"
                else []
            ),
            human_tip=(
                _unavailable_tip("原作关系图或新作关系图")
                if status == "unavailable"
                else "并排比对两张关系图：节点数、度数序列、每条边的类型与权力流向是否都变了。"
            ),
            metrics={
                **metrics,
                "src": src_fp.model_dump(mode="json"),
                "new": new_fp.model_dump(mode="json"),
            },
        )
    )

    # ④ 桥段功能序列（L5，auto，★命门★）
    beat_status, beat_metrics = check_beat_sequence(
        rebuild.L5_beats.source_seq, rebuild.L5_beats.new_seq
    )
    checks.append(
        _make_item(
            CHECK_BEAT_SEQUENCE,
            "L5",
            "auto",
            beat_status,
            detail=str(beat_metrics.get("reason", "")),
            evidence=(
                [Evidence(line=None, excerpt="撞车桥段：" + " → ".join(
                    str(x) for x in beat_metrics.get("common", [])
                ))]
                if beat_status == "fail" and beat_metrics.get("common")
                else []
            ),
            human_tip=(
                _unavailable_tip("原作桥段序列或新作桥段序列")
                if beat_status == "unavailable"
                else "把两行桥段序列各读一遍，问自己是不是同一条路径；"
                "若是，换掉中间环节重排顺序。"
            ),
            metrics=beat_metrics,
        )
    )

    # ⑤ 原句 / 近复制句（P0 恒定 unavailable —— A3 裁定，无合法原句样本入口）
    checks.append(
        _make_item(
            CHECK_NEAR_DUPLICATE,
            "L1",
            "semi_auto",
            "unavailable",
            detail=f"P0 未提供原作摘录样本，{NGRAM_SIZE}-gram 比对无从执行",
            human_tip=_unavailable_tip("原作原句摘录"),
            metrics={"ngram": NGRAM_SIZE, "reason": "no_legal_sample_entry_in_p0"},
        )
    )

    # ⑥ 独特道具 / 具体对白（semi_auto，P0 无独立样本入口 → unavailable）
    checks.append(
        _make_item(
            CHECK_UNIQUE_PROP,
            "L1/L2",
            "semi_auto",
            "unavailable",
            detail="未提供原作独特道具 / 具体对白样本",
            human_tip=_unavailable_tip("原作独特道具与具体对白"),
        )
    )

    # ⑦ 一对一人物映射（L3，auto，输入来自 AI 结构化块）
    oto_status, oto_metrics = check_one_to_one(
        abstract.function_slots, list(character_table or [])
    )
    checks.append(
        _make_item(
            CHECK_ONE_TO_ONE,
            "L3",
            "auto",
            oto_status,
            detail=str(oto_metrics.get("reason", "")),
            evidence=(
                [Evidence(line=None, excerpt=f"新作功能位 {oto_metrics.get('new_slots', {})}"[:EVIDENCE_EXCERPT_CHARS])]
                if oto_status == "fail"
                else []
            ),
            human_tip=(
                "请手动列出新作角色及其功能位后重跑自检（可在面板「手工补录新角色表」里填）。"
                if oto_status == "unavailable"
                else (
                    "逐个角色问：这个人的功能位是否与原作某人一一对应？是的话合并或改写。"
                    if oto_status in ("warn", "fail")
                    else ""
                )
            ),
            metrics=oto_metrics,
        )
    )

    # ⑧ 同构反转底牌（L5，auto，输入来自 AI 结构化块）
    rev_status, rev_metrics = check_isomorphic_reversal(
        abstract.reversal_types,
        abstract.reversal_positions,
        list(reversal_table or []),
        total_chapters,
    )
    checks.append(
        _make_item(
            CHECK_ISOMORPHIC_REVERSAL,
            "L5",
            "auto",
            rev_status,
            detail=str(rev_metrics.get("reason", "")),
            evidence=(
                [Evidence(line=None, excerpt=f"同位置反转 {rev_metrics.get('hits', [])}"[:EVIDENCE_EXCERPT_CHARS])]
                if rev_status == "fail"
                else []
            ),
            human_tip=(
                "请手动登记新作反转的「类型 + 出现章节」后重跑自检。"
                if rev_status == "unavailable"
                else (
                    "把新作反转的出现章节往前或往后挪，或换一种反转机制。"
                    if rev_status in ("warn", "fail")
                    else ""
                )
            ),
            metrics=rev_metrics,
        )
    )

    # 写后自检命中清单（只提示，不阻断；挂在 ④ 的 metrics 上供 UI 展示）
    if lint_hits:
        for item in checks:
            if item.key == CHECK_BEAT_SEQUENCE:
                item.metrics["lint_hits"] = list(lint_hits)
                break

    return RewriteReport(
        rewrite_id=rewrite_id,
        blueprint_id=blueprint.id,
        book_id=blueprint.book_id,
        chapter_id=chapter_id,
        kind=kind,
        draft_file=draft_file,
        generated_at=now_iso(),
        checks=checks,
        reverse_three=[
            ReverseQuestion(q=question, expect=expect) for question, expect in REVERSE_THREE
        ],
    )


def apply_checks(
    report: RewriteReport, checks: list[dict], reverse: list[dict]
) -> RewriteReport:
    """落勾选（幂等，可多次调）：warn/unavailable 项 + 反向三问。

    `fail` 项**不接受勾选**（勾了也不放行 —— 硬阻断不可降级）。
    每次落勾选都会刷新 `checked_at`，`summary` 由 validator 重算。
    """
    by_key = {str(item.get("key", "")): item for item in (checks or []) if isinstance(item, dict)}
    for item in report.checks:
        payload = by_key.get(item.key)
        if payload is None:
            continue
        if item.status == "fail":
            continue  # 硬阻断项不可勾选放行
        checked = bool(payload.get("human_checked", False))
        item.human_checked = checked
        item.checked_at = now_iso() if checked else None

    for index, question in enumerate(report.reverse_three):
        payload = next(
            (
                item
                for item in (reverse or [])
                if isinstance(item, dict) and int(item.get("index", -1)) == index
            ),
            None,
        )
        if payload is None:
            continue
        checked = bool(payload.get("human_checked", False))
        question.human_checked = checked
        question.human_answer = (
            str(payload.get("human_answer")) if payload.get("human_answer") is not None else None
        )

    # 触发 validator 重算 summary（model_validate 会跑 _recompute_summary）
    return RewriteReport.model_validate(report.model_dump(mode="json"))


def write_ack(report: RewriteReport) -> RewriteReport:
    """写 ack：`acknowledged_at` + `disclaimer_version` + `checked_keys`（留痕）。"""
    report.ack.acknowledged_at = now_iso()
    report.ack.disclaimer_version = DISCLAIMER_VERSION
    report.ack.checked_keys = [
        item.key for item in report.checks if item.human_checked
    ]
    return RewriteReport.model_validate(report.model_dump(mode="json"))


def require_adoptable(report: RewriteReport) -> None:
    """采纳闸门的服务端二次校验（**不信前端**）。

    四条全满足才放行：
      ① `summary.blocking == 0`；
      ② 全部 warn/unavailable 项已勾选；
      ③ 反向三问全勾选；
      ④ ack 已写（`acknowledged_at` 非空）。

    Raises:
        RewriteAckRequiredError: 任一不满足（422 `rewrite_ack_required`）。
    """
    if report.summary.blocking > 0:
        raise RewriteAckRequiredError(
            f"存在 {report.summary.blocking} 项硬阻断（fail），不可采纳。"
            "请修改蓝图后重新生成，或在草稿里手动替换后重新自检。"
        )
    pending = [item for item in report.checks if item.status in ("warn", "unavailable")]
    missing = [item.key for item in pending if not item.human_checked]
    if missing:
        raise RewriteAckRequiredError(
            f"仍有 {len(missing)} 项待核对未勾选：{', '.join(missing)}"
        )
    if not report.reverse_three:
        raise RewriteAckRequiredError("反向校验三问缺失，无法确认已完成人工核对")
    unanswered = [
        index for index, question in enumerate(report.reverse_three) if not question.human_checked
    ]
    if unanswered:
        raise RewriteAckRequiredError(
            f"反向校验三问未全部勾选（缺第 {', '.join(str(i + 1) for i in unanswered)} 问）"
        )
    if not report.ack.acknowledged_at:
        raise RewriteAckRequiredError("尚未完成采纳二次确认（ack 缺失）")


# ─────────────────────────── 闸门 ───────────────────────────

def is_gate_ready(bp: Blueprint) -> tuple[bool, list[str]]:
    """五层硬闸门就绪判定（**计算值，不落盘**，避免盘/算不一致）。

    L3：`source_graph` 与 `new_graph` 均非空；L5：`source_seq` 与 `new_seq` 均非空。
    """
    missing: list[str] = []
    relations = bp.rebuild.L3_relations
    if not relations.source_graph or not relations.new_graph:
        missing.append("L3")
    beats = bp.rebuild.L5_beats
    if not beats.source_seq or not beats.new_seq:
        missing.append("L5")
    return (not missing), missing


def require_gate(bp: Blueprint, *, skip: bool = False) -> None:
    """闸门未过且未显式 skip → 422 `rewrite_gate_blocked`。

    `skip=True` 时放行，但**调用方必须**写 `gate.skipped_at` / `skip_reason`
    （知情放行，不偷偷放款）。
    """
    ready, missing = is_gate_ready(bp)
    if ready or skip:
        return
    raise RewriteGateBlockedError(
        "L3 关系拓扑与 L5 桥段序列是命门层，未填表不可发起生成"
        "（可显式跳过，但产物会标记未过闸门）：" + "、".join(missing),
        missing=missing,
    )


# ─────────────────────────── id 生成 ───────────────────────────

def _stamp(moment: datetime | None = None) -> str:
    """`YYYYmmddHHMMSS`。"""
    return (moment or datetime.now()).strftime("%Y%m%d%H%M%S")


def make_blueprint_id(*, at: datetime | None = None) -> str:
    """`bp-<ts>-<hex4>`。"""
    return f"{BLUEPRINT_ID_PREFIX}-{_stamp(at)}-{uuid.uuid4().hex[:4]}"


def make_rewrite_id(book_id: str, *, at: datetime | None = None) -> str:
    """`rw-<book_id>-<ts>-<hex4>`（与 job_id 同构）。"""
    return f"{REWRITE_ID_PREFIX}-{validate_id(book_id, 'book_id')}-{_stamp(at)}-{uuid.uuid4().hex[:4]}"


def make_rewrite_job_id(book_id: str, *, at: datetime | None = None) -> str:
    """`rw-<book_id>-<ts>-<hex4>`。"""
    return make_rewrite_id(book_id, at=at)


def parse_rewrite_job_id(job_id: str) -> tuple[str, str]:
    """`rw-<book_id>-<ts>-<hex4>` → `(book_id, ts)`。

    `rw-` 前缀与既有 `parse_job_id()`（强制 `job-`）**不兼容**，故自建。

    Raises:
        NovelValidationError: 格式不符（code=`invalid_id`）。
    """
    from app.services.novel_store import ERR_INVALID_ID

    text = str(job_id or "").strip()
    parts = text.rsplit("-", 2)
    if len(parts) != 3:
        raise NovelValidationError(f"仿写 job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    head, ts, tail = parts
    if not head.startswith(f"{JOB_ID_PREFIX}-"):
        raise NovelValidationError(f"仿写 job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    if not re.fullmatch(r"\d{14}", ts) or not re.fullmatch(r"[0-9a-f]{4}", tail):
        raise NovelValidationError(f"仿写 job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    book_id = head[len(JOB_ID_PREFIX) + 1 :]
    validate_id(book_id, "book_id")
    return book_id, ts


# ─────────────────────────── 仿写域 IO ───────────────────────────

def _sha256(path: Path) -> str:
    """文件内容 sha256（不存在 → 空串）。"""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime_ns(path: Path) -> int:
    """文件 mtime_ns（不存在 → 0）。"""
    if not path.exists():
        return 0
    return path.stat().st_mtime_ns


def capture_authoritative_snapshot(store: NovelStore, book_id: str) -> dict:
    """权威数据三重快照（P0-10 零写入断言）。

    Returns:
        `{book_mtime_ns, book_version, state_mtime_ns, state_sha256,
          chapters: {rel: {mtime_ns, sha256}}}` —— mtime + version + 内容哈希三重。
    """
    from app.services.novel_store import BOOK_FILE, STATE_FILE

    book_dir = store.book_dir(book_id)
    book_path = book_dir / BOOK_FILE
    state_path = book_dir / STATE_FILE

    version = 0
    if book_path.exists():
        try:
            payload = json.loads(book_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                version = int(payload.get("version", 0))
        except (OSError, UnicodeError, json.JSONDecodeError):  # pragma: no cover - 防御性
            version = -1

    chapters: dict[str, dict[str, object]] = {}
    if book_path.exists():
        try:
            payload = json.loads(book_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                nodes = (payload.get("outline") or {}).get("nodes") or []
                for volume in nodes:
                    for chapter in (volume or {}).get("children") or []:
                        rel = str(chapter.get("file") or "")
                        if not rel:
                            continue
                        path = store.chapter_path(book_id, rel)
                        chapters[rel] = {
                            "mtime_ns": _mtime_ns(path),
                            "sha256": _sha256(path),
                        }
        except (OSError, UnicodeError, json.JSONDecodeError):  # pragma: no cover - 防御性
            chapters = {}
    chapters = dict(sorted(chapters.items()))

    return {
        "book_mtime_ns": _mtime_ns(book_path),
        "book_version": version,
        "state_mtime_ns": _mtime_ns(state_path),
        "state_sha256": _sha256(state_path),
        "chapters": chapters,
    }


class RewriteStore:
    """仿写域读写（**全部写经 `NovelStore.rewrite_path()`**，结构性防越界）。"""

    def __init__(self, store: NovelStore | None = None) -> None:
        """Args:
            store: 文件系统事实层；默认 `NovelStore()`（root=settings.data_dir/novel）。
        """
        self._store = store or NovelStore()

    @property
    def base(self) -> NovelStore:
        """底层 `NovelStore`（借它的路径校验与原子写）。"""
        return self._store

    # ── 路径 ──

    def rewrite_dir(self, book_id: str) -> Path:
        """`books/<id>/rewrite/`。"""
        return self._store.rewrite_dir(book_id)

    def blueprint_path(self, book_id: str) -> Path:
        """`rewrite/blueprint.json`。"""
        return self._store.rewrite_path(book_id, f"{REWRITE_DIR}/{BLUEPRINT_FILE}")

    def draft_path(self, book_id: str, name: str) -> Path:
        """`rewrite/drafts/<name>`（name 由本模块生成，仍走 `rewrite_path` 校验）。"""
        return self._store.rewrite_path(book_id, in_rewrite(f"{RW_DRAFTS_DIR}/{name}"))

    def report_path(self, book_id: str, rewrite_id: str) -> Path:
        """`rewrite/reports/<rewrite_id>.json`。"""
        return self._store.rewrite_path(
            book_id, in_rewrite(f"{RW_REPORTS_DIR}/{rewrite_id}.json")
        )

    def rewrite_job_path(self, job_id: str) -> Path:
        """`checkpoints/rw-<...>.json`（复用既有 checkpoints 目录 + `validate_id`）。"""
        book_id, _ts = parse_rewrite_job_id(job_id)
        validate_id(job_id, "job_id")
        path = self._store.checkpoints_dir(book_id) / f"{job_id}.json"
        if path.parent.resolve() != self._store.checkpoints_dir(book_id).resolve():
            from app.services.novel_store import ERR_PATH_ESCAPE

            raise NovelValidationError(f"仿写 job 路径非法: {job_id!r}", code=ERR_PATH_ESCAPE)
        return path

    # ── 蓝图 ──

    def load_blueprint(self, book_id: str) -> Blueprint:
        """读蓝图；不存在 → 返回空 `Blueprint`（**不落盘**，与 `get_state` 同风格）。"""
        validate_id(book_id, "book_id")
        path = self.blueprint_path(book_id)
        if not path.exists():
            return Blueprint(book_id=book_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NovelStoreError(f"读取失败（文件可能已损坏）: {path}（{exc}）") from exc
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"blueprint.json 格式非法: {path}")
        blueprint = Blueprint.model_validate(payload)
        blueprint.book_id = book_id
        return blueprint

    def save_blueprint(self, book_id: str, bp: Blueprint) -> Blueprint:
        """**先跑预检**，命中即 `RewriteSourceRejectedError`（**一个字节都不落盘**）。"""
        validate_id(book_id, "book_id")
        hits = precheck_blueprint(bp)
        if hits:
            raise RewriteSourceRejectedError(
                "输入疑似原文，已被预检拒绝（未保存）。请改写为结构笔记后重试。",
                hits=hits,
            )

        blueprint = bp.model_copy(deep=True)
        blueprint.book_id = book_id
        if not blueprint.id:
            blueprint.id = make_blueprint_id()
        if not blueprint.created_at:
            blueprint.created_at = now_iso()
        blueprint.updated_at = now_iso()

        _ready, _missing = is_gate_ready(blueprint)
        atomic_write_json(
            self.blueprint_path(book_id), blueprint.model_dump(mode="json", by_alias=True)
        )
        return blueprint

    # ── 草稿 ──

    def write_rewrite_draft(
        self, book_id: str, rewrite_id: str, kind: str, text: str
    ) -> str:
        """写仿写产物到 `rewrite/drafts/`。

        md 用 `newline=""`（字节级保留），`.outline.json` 用 `atomic_write_json`。

        Returns:
            相对 `rewrite/` 的路径（如 `drafts/rw-xxx.md`）。
        """
        validate_id(book_id, "book_id")
        if kind == "outline":
            name = f"{rewrite_id}.outline.json"
            path = self.draft_path(book_id, name)
            try:
                payload = json.loads(str(text or ""))
            except json.JSONDecodeError as exc:
                raise RewriteError(f"大纲补丁不是合法 JSON，未落盘: {exc}") from exc
            atomic_write_json(path, payload)
        else:
            name = f"{rewrite_id}.md"
            path = self.draft_path(book_id, name)
            atomic_write_text(path, str(text or ""), newline="")
        return f"{RW_DRAFTS_DIR}/{name}"

    def read_rewrite_draft(self, book_id: str, rel: str) -> str:
        """读仿写产物全文（`rel` 相对 `rewrite/`，经 `rewrite_path` 校验）。"""
        path = self._store.rewrite_path(book_id, in_rewrite(rel))
        if not path.exists():
            raise NovelNotFound(f"仿写产物不存在: {rel}")
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()

    # ── 报告 ──

    def save_report(self, book_id: str, report: RewriteReport) -> RewriteReport:
        """原子写 `rewrite/reports/<rewrite_id>.json`。"""
        validate_id(book_id, "book_id")
        validate_id(report.rewrite_id, "rewrite_id")
        atomic_write_json(
            self.report_path(book_id, report.rewrite_id),
            report.model_dump(mode="json"),
        )
        return report

    def load_report(self, book_id: str, rewrite_id: str) -> RewriteReport:
        """读报告；不存在 → `NovelNotFound`（404）。"""
        path = self.report_path(book_id, rewrite_id)
        if not path.exists():
            raise NovelNotFound(f"质检报告不存在: {rewrite_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NovelStoreError(f"读取失败（文件可能已损坏）: {path}（{exc}）") from exc
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"报告格式非法: {path}")
        return RewriteReport.model_validate(payload)

    def list_reports(self, book_id: str, kind: str | None = None) -> list[dict]:
        """报告清单 `[{rewrite_id, kind, generated_at, blocking, adoptable}]`，最新在前。"""
        validate_id(book_id, "book_id")
        directory = self.rewrite_dir(book_id) / RW_REPORTS_DIR
        if not directory.exists():
            return []
        items: list[dict] = []
        for path in sorted(directory.glob("rw-*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):  # pragma: no cover
                continue
            if not isinstance(payload, dict):  # pragma: no cover - 防御性
                continue
            if kind and str(payload.get("kind", "")) != kind:
                continue
            summary = payload.get("summary") or {}
            items.append(
                {
                    "rewrite_id": str(payload.get("rewrite_id", path.stem)),
                    "kind": str(payload.get("kind", "")),
                    "generated_at": str(payload.get("generated_at", "")),
                    "blocking": int(summary.get("blocking", 0) or 0),
                    "adoptable": bool(summary.get("adoptable", False)),
                }
            )
        return items

    # ── job checkpoint ──

    def save_rewrite_job(self, job: RewriteJob) -> None:
        """原子写 `checkpoints/rw-<...>.json`（权威在磁盘）。"""
        path = self.rewrite_job_path(job.job_id)
        atomic_write_json(path, job.model_dump(mode="json"))

    def load_rewrite_job(self, job_id: str) -> RewriteJob:
        """读仿写 job；不存在 → `NovelNotFound`（404）。"""
        path = self.rewrite_job_path(job_id)
        if not path.exists():
            raise NovelNotFound(f"仿写任务不存在: {job_id}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NovelStoreError(f"读取失败（文件可能已损坏）: {path}（{exc}）") from exc
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"仿写 checkpoint 格式非法: {path}")
        return RewriteJob.model_validate(payload)


def rewrite_status_tone(status: str) -> str:
    """状态语义 → 语义色 token 名（供前端/日志复用，避免两处漂移）。"""
    return {
        "pass": "ok",
        "warn": "warn",
        "fail": "blocking",
        "unavailable": "pending",
    }.get(str(status), "pending")


def as_dict(model: BaseModel) -> dict[str, Any]:
    """`model_dump(mode="json")` 的类型收窄包装（路由层统一出口）。"""
    return dict(model.model_dump(mode="json"))
