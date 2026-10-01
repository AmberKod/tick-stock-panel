"""小说工作区 — 文件系统事实层（Pydantic 模型 + 原子写 + 派生视图 + 导出）。

设计依据：`deliverables/novel-workspace/ARCHITECTURE.md` §3.1~§3.3 / §7.1~§7.3。

**前端 TS 类型镜像字段清单**（`frontend/src/lib/novelTypes.ts` 必须与之一一对应，
任一侧改字段名都要同步改另一侧，并更新 `tests/test_novel_store.py` 的 schema 快照用例）：

    BookMeta       : version, id, title, author, genre, pov, tense, setting_summary,
                     created_at, updated_at, outline
    BookMetaPayload: id, title, author, genre, pov, tense, setting_summary,
                     created_at, updated_at, chapter_count, word_count
                     —— GET/PATCH /books/{book_id}/meta 的响应体（不含大纲树）。
    OutlineTree    : nodes
    OutlineVolume  : id, type, title, order, children
    OutlineChapter : id, type, title, order, status, word_target, summary, beat,
                     file, word_count
    BookState      : book_id, updated_at, rolling, characters, foreshadow, chapters
    RollingState   : summary, updated_chapter
    CharacterState : status, location, last_seen_chapter, traits
    ForeshadowItem : id, text, planted_chapter, status, resolved_chapter
    ChapterFact    : id, title, chars, state_changes, planted, resolved, relations,
                     source, adopted_at
    RelationDelta  : 磁盘与 HTTP JSON 的键名是 from / to / delta
                     （Pydantic 字段名是 source / target，`from` 是 Python 保留字）
    NovelJob       : job_id, book_id, chapter_id, mode, selection, skip_gate,
                     created_at, updated_at, steps, artifacts, status, failed_step
    JobStep        : name, status, at, error
    JobArtifacts   : context_card_md, draft_id, draft_text, fact_json, ingest_plan
    LintHit        : rule, message, line, excerpt
    AiStatus       : configured, provider, model, available, reason, code

**磁盘布局**（`settings.data_dir / "novel"`）：

    books/<book_id>/book.json         权威：元数据 + 大纲树
    books/<book_id>/state.json        权威：追踪态（滚动摘要/角色/伏笔/章节事实）
    books/<book_id>/正文/*.md          权威：章节正文（字节级保留用户换行习惯）
    books/<book_id>/drafts/*.md       草稿：AI 产出，未采纳不污染任何权威数据
    books/<book_id>/checkpoints/*.json  Step 级断点
    books/<book_id>/views/*.md        派生只读：可删可重建，重建幂等

**实现说明（与 ARCHITECTURE.md 的 3 处细微差异，都是为了让契约更稳）**：

1. 草稿文件名由 `d-<ts>-<mode>.md` 调整为 `d-<chapter_id>-<ts>-<hex4>-<mode>.md`
   —— 否则 `list_drafts(book_id, chapter_id)` 无法按章节过滤（原命名里没有章节信息）。
   `draft_id` 仍匹配 `ID_RE`，可被 `validate_id` 校验（天然防穿越）。
2. `write_chapter` **不递增 `version`** —— version 是前端大纲乐观锁的凭据，
   800ms 防抖的自动保存若每次 +1，会让前端缓存的 version 立刻失效而刷 409。
   它只回写 `word_count` 与 `book.updated_at`。
3. `adopt_draft` **递增 `version`**（按 §4.2 时序图），因为它改的是大纲里的
   `status` 字段 —— 前端采纳后必须重新拉取大纲（invalidateQueries）再改大纲。

许可证：MIT（与宿主项目一致）。本文件全部为自研实现，未复制任何第三方代码。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.config import settings

# ─────────────────────────── 常量 ───────────────────────────

#: 合法 id：`book_id` / `chapter_id` / `volume_id` / `draft_id` 共用。
#: 必须用 `\Z` 而不是 `$` —— `$` 允许结尾换行（`re.match("^[a-z]+$", "abc\n")` 会命中），
#: 会让 `validate_id("book-1\n")` 通过并拼出带换行的目录路径。
ID_RE = re.compile(r"^[a-z0-9-]{1,64}\Z")

#: 章节正文目录名（中文）。A5：用常量收敛，将来若要切英文名只改这一处。
CHAPTERS_DIR = "正文"
DRAFTS_DIR = "drafts"
CHECKPOINTS_DIR = "checkpoints"
VIEWS_DIR = "views"
#: 换元仿写域目录名（与 CHAPTERS_DIR 同构，由 `rewrite_path()` 强制其内）。
REWRITE_DIR = "rewrite"

BOOKS_DIR = "books"
BOOK_FILE = "book.json"
STATE_FILE = "state.json"

VIEW_CONTEXT_CARD = "context-card"
VIEW_TIMELINE = "timeline"
VIEW_CHARACTERS = "characters"
VIEW_NAMES: tuple[str, str, str] = (VIEW_CONTEXT_CARD, VIEW_TIMELINE, VIEW_CHARACTERS)

CHAPTER_STATUS_DRAFT = "draft"
CHAPTER_STATUS_AI_DRAFT = "ai_draft"
CHAPTER_STATUS_PUBLISHED = "published"

FACT_SOURCE_AI = "ai"
FACT_SOURCE_MANUAL = "manual"

#: 上下文卡里"最近 N 章事实快照"的数量（PRD §3.2 机制 7）。
RECENT_CHAPTER_FACTS = 2
#: 上下文卡里附录的"本章正文尾部"字数。
CONTEXT_TAIL_CHARS = 800
#: slugify 退化值（标题为纯中文时）。
DEFAULT_SLUG = "ch"
#: 书籍元数据可更新字段（order 决定校验顺序，`title` 单独校验）。
BOOK_META_TEXT_FIELDS = ("author", "genre", "pov", "tense", "setting_summary")
#: 单个元数据文本字段的最大长度（设定摘要允许长文本，但仍要挡住误粘贴整本书）。
BOOK_META_TEXT_LIMIT = 4000

# ─────────────────────────── 错误码常量（§7.3） ───────────────────────────
# 以下常量被 api / jobs / ai 共同依赖（依赖方向单向：它们 import 本模块）。

ERR_INVALID_ID = "invalid_id"                  # 422
ERR_PATH_ESCAPE = "path_escape"                # 422
ERR_INVALID_TITLE = "invalid_title"            # 422
ERR_MISSING_BEAT = "missing_beat"              # 422（写前门禁，由 novel_ai 抛出）
ERR_FACT_PARSE_FAILED = "fact_parse_failed"    # 422
ERR_SELECTION_REQUIRED = "selection_required"  # 422
ERR_INVALID_PAYLOAD = "invalid_payload"        # 422
ERR_NOT_FOUND = "not_found"                    # 404
ERR_VERSION_CONFLICT = "version_conflict"      # 409
ERR_JOB_BUSY = "job_busy"                      # 409
ERR_JOB_NOT_RESUMABLE = "job_not_resumable"    # 409
ERR_AI_UNAVAILABLE = "ai_unavailable"          # 503
ERR_AI_ERROR = "ai_error"                      # 503
ERR_WRITE_FAILED = "write_failed"              # 500
ERR_INTERNAL = "internal_error"                # 500

# 换元仿写域（增量，见 `deliverables/novel-workspace/ARCHITECTURE-rewrite.md` §8.3）。
# 三个新异常都继承 NovelValidationError → 自动享受 api/novel.py 的
# `_http_error(422, exc.code, ...)` 翻译，错误翻译表一行不改。
ERR_REWRITE_SOURCE_REJECTED = "rewrite_source_rejected"   # 422
ERR_REWRITE_GATE_BLOCKED = "rewrite_gate_blocked"         # 422
ERR_REWRITE_ACK_REQUIRED = "rewrite_ack_required"         # 422


# ─────────────────────────── 异常 ───────────────────────────


class NovelStoreError(RuntimeError):
    """小说工作区的基础异常。"""


class NovelNotFound(NovelStoreError):  # noqa: N818 - 名称由 ARCHITECTURE.md §3.3 冻结
    """书籍 / 章节 / 草稿 / 视图不存在。"""


class NovelValidationError(NovelStoreError):
    """非法 id / 路径穿越 / 非法入参 —— HTTP 语义 422。

    Attributes:
        code: 结构化错误码，默认 `invalid_id`。
    """

    def __init__(self, message: str = "非法参数", code: str = ERR_INVALID_ID) -> None:
        super().__init__(message)
        self.code = code


class NovelConflict(NovelStoreError):  # noqa: N818 - 名称由 ARCHITECTURE.md §3.3 冻结
    """乐观锁冲突（`book.json` 的 version 不符）—— HTTP 语义 409。"""


# ─────────────────────────── 数据模型（§3.1） ───────────────────────────


class OutlineChapter(BaseModel):
    """大纲树的章节点（两级树的叶子）。"""

    id: str
    type: str = "chapter"
    title: str = ""
    order: int = 1
    status: str = CHAPTER_STATUS_DRAFT
    word_target: int = 0
    summary: str = ""
    beat: str = ""
    file: str = ""
    word_count: int = 0


class OutlineVolume(BaseModel):
    """大纲树的卷节点（两级树的中间层，不做递归）。"""

    id: str
    type: str = "volume"
    title: str = ""
    order: int = 1
    children: list[OutlineChapter] = Field(default_factory=list)


class OutlineTree(BaseModel):
    """大纲树。"""

    nodes: list[OutlineVolume] = Field(default_factory=list)


class BookMeta(BaseModel):
    """`book.json`：书籍元数据 + 大纲树（结构化权威之一）。"""

    version: int = 1
    id: str
    title: str
    author: str = ""
    genre: str = ""
    pov: str = ""
    tense: str = ""
    setting_summary: str = ""
    created_at: str = ""
    updated_at: str = ""
    outline: OutlineTree = Field(default_factory=OutlineTree)


class CharacterState(BaseModel):
    """角色当前状态（追踪态）。"""

    status: str = ""
    location: str = ""
    last_seen_chapter: str | None = None
    traits: list[str] = Field(default_factory=list)


class ForeshadowItem(BaseModel):
    """伏笔条目。"""

    id: str
    text: str
    planted_chapter: str | None = None
    status: str = "open"
    resolved_chapter: str | None = None


class RelationDelta(BaseModel):
    """关系变化。`from` 是 Python 保留字，故模型字段为 source/target，
    磁盘与 HTTP JSON 用别名 from/to（PRD §5.3 逐字一致）。
    """

    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(default="", alias="from")
    target: str = Field(default="", alias="to")
    delta: str = ""


class ChapterFact(BaseModel):
    """章节事实快照（每章一条，采纳时并入 `state.json`）。"""

    id: str = ""
    title: str = ""
    chars: list[str] = Field(default_factory=list)
    state_changes: list[str] = Field(default_factory=list)
    planted: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)
    relations: list[RelationDelta] = Field(default_factory=list)
    source: str = FACT_SOURCE_MANUAL
    adopted_at: str | None = None


class RollingState(BaseModel):
    """全局滚动摘要（人工可改）。"""

    summary: str = ""
    updated_chapter: str | None = None


class BookState(BaseModel):
    """`state.json`：追踪态。"""

    book_id: str = ""
    updated_at: str = ""
    rolling: RollingState = Field(default_factory=RollingState)
    characters: dict[str, CharacterState] = Field(default_factory=dict)
    foreshadow: list[ForeshadowItem] = Field(default_factory=list)
    chapters: list[ChapterFact] = Field(default_factory=list)


class JobStep(BaseModel):
    """AI 任务的一步（四步：context → draft_text → fact_snapshot → ingest）。"""

    name: str
    status: str = "pending"
    at: str | None = None
    error: str | None = None


class JobArtifacts(BaseModel):
    """任务产物。第 4 步只落摄取计划（`ingest_plan`），绝不写 `state.json`。"""

    context_card_md: str | None = None
    draft_id: str | None = None
    draft_text: str | None = None
    fact_json: dict[str, object] | None = None
    ingest_plan: dict[str, object] | None = None


class NovelJob(BaseModel):
    """`checkpoints/<job_id>.json`。"""

    job_id: str = ""
    book_id: str = ""
    chapter_id: str = ""
    mode: str = "continue"
    selection: str | None = None
    skip_gate: bool = False
    created_at: str = ""
    updated_at: str = ""
    steps: list[JobStep] = Field(default_factory=list)
    artifacts: JobArtifacts = Field(default_factory=JobArtifacts)
    status: str = "queued"
    failed_step: str | None = None


class LintHit(BaseModel):
    """写后自检命中项（只提醒，不改写）。"""

    rule: str
    message: str
    line: int | None = None
    excerpt: str = ""


# ─────────────────────────── 模块级纯函数 ───────────────────────────


def now_iso() -> str:
    """当前时间 ISO 8601（带本地偏移）。"""
    return datetime.now().astimezone().isoformat()


def validate_id(value: str, what: str = "id") -> str:
    """校验 id 必须匹配 `^[a-z0-9-]{1,64}\\Z`（结尾不允许换行，故用 `\\Z` 而非 `$`）。

    Args:
        value: 待校验的 id。
        what: 用于错误文案的参数名。

    Returns:
        原样返回的 value。

    Raises:
        NovelValidationError: 不匹配时抛出（code=`invalid_id`）。
    """
    text = str(value or "")
    if not ID_RE.match(text):
        raise NovelValidationError(
            f"{what} 非法: 只允许 [a-z0-9-] 且长度 1-64（收到 {text[:64]!r}）",
            code=ERR_INVALID_ID,
        )
    return text


def lexically_inside(root: Path, target: Path) -> bool:
    """纯**词法**的「target 必须在 root 之内」判断 —— 路径校验的唯一口径。

    为什么不用 `Path.resolve()`（QA P2-1）：`resolve()` 要做 FS 往返，在目录
    **正被并发创建**的瞬间会返回瞬态值 —— 16 线程并发首次创建
    `rewrite/drafts/` 时，约 1-2% 的**合法**路径被误判成 `path_escape`
    （失败后立刻重算又能通过）。FastAPI 同步端点走线程池，可与 job 写盘并发，
    用户就会看到莫名 422 或 job step2 failed。

    `os.path.abspath` + `os.path.commonpath` 是纯字符串运算（只依赖 cwd，
    不 stat、不 open），结果与并发完全无关 —— 校验要么恒真要么恒假。

    代价（**明确记录**）：不解析符号链接。若**书籍目录内部**存在指向外部的
    软链，词法判断会放行。本应用的书籍目录由 `create_book` 建为真实目录、
    `file` 字段由 `slugify` 生成，且「能写磁盘 = 完全授信」（同 P2-3 的威胁
    模型边界），故接受该代价；换来的是判定不受并发影响。
    """
    root_key = os.path.normcase(os.path.abspath(str(root)))
    target_key = os.path.normcase(os.path.abspath(str(target)))
    if root_key == target_key:
        return True
    try:
        return os.path.commonpath([root_key, target_key]) == root_key
    except ValueError:
        # Windows 不同盘符没有公共前缀 → 绝不可能包含
        return False


def validate_rel_path(rel: str, root: Path) -> Path:
    """校验相对路径并拼接，拒绝 `..` 与绝对路径（目录穿越防护）。

    Args:
        rel: 相对路径（来自 `book.json` 的 `file` 字段或用户输入）。
        root: 允许写入的根目录。

    Returns:
        拼接后的 `Path`（未 resolve，保持可读性）。

    Raises:
        NovelValidationError: 路径逃逸时抛出（code=`path_escape`）。
    """
    text = str(rel or "").strip()
    if not text:
        raise NovelValidationError("相对路径不能为空", code=ERR_PATH_ESCAPE)
    if text.startswith("/") or text.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", text):
        raise NovelValidationError(f"不接受绝对路径: {text!r}", code=ERR_PATH_ESCAPE)
    # ★盘符注入★：`drafts/C:/Windows/win.ini` 这种写法在**字符串层面**看起来只是
    # 「目录里的一个子路径」，但 `Path.resolve()` 会把它解析成另一个盘符的绝对路径。
    # 改成词法校验后不再做 FS 往返，就必须在**每一段**上挡掉盘符，否则等于开了一个
    # 跨盘逃逸口子（QA `test_store_path_helpers_respect_boundary` 守着这条）。
    for part in Path(text).parts:
        if re.match(r"^[A-Za-z]:", part):
            raise NovelValidationError(f"不接受绝对路径或盘符: {text!r}", code=ERR_PATH_ESCAPE)

    base = Path(root)
    candidate = base / text
    # 词法判断（不做 FS 往返）—— 见 `lexically_inside` 的并发根因说明。
    if not lexically_inside(base, candidate):
        raise NovelValidationError(f"路径逃逸出书籍目录: {text!r}", code=ERR_PATH_ESCAPE)
    return candidate


def slugify(title: str) -> str:
    """中英混合标题 → 小写 ascii slug；全中文时退化为 `ch`（靠序号区分）。"""
    ascii_text = (
        unicodedata.normalize("NFKD", str(title or "")).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:32]
    return slug or DEFAULT_SLUG


def count_words(text: str) -> int:
    """字数（不含空白）—— P0-4① 的口径。"""
    return len(re.sub(r"\s+", "", str(text or "")))


def strip_markdown(text: str) -> str:
    """txt 导出：剥离行内 Markdown 标记，不删正文内容。

    处理：`#` 标题符、区块引用 `>`、列表前缀 `-`/`*`/`+`/`1.`、`**` 加粗、
    反引号、以及纯分割线整行。
    """
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", line):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^>\s?", "", line)
        line = re.sub(r"^([-*+]|\d+[.)])\s+", "", line)
        line = line.replace("**", "").replace("`", "")
        lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_job_id(job_id: str) -> tuple[str, str]:
    """`job-<book_id>-<YYYYmmddHHMMSS>-<hex4>` → `(book_id, ts)`。

    Raises:
        NovelValidationError: 格式不符时抛出。
    """
    text = str(job_id or "").strip()
    parts = text.rsplit("-", 2)
    if len(parts) != 3 or parts[0] == text:
        raise NovelValidationError(f"job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    head, ts, tail = parts
    if not head.startswith("job-"):
        raise NovelValidationError(f"job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    if not re.fullmatch(r"\d{14}", ts) or not re.fullmatch(r"[0-9a-f]{4}", tail):
        raise NovelValidationError(f"job_id 格式非法: {text!r}", code=ERR_INVALID_ID)
    book_id = head[4:]
    validate_id(book_id, "book_id")
    return book_id, ts


def make_job_id(book_id: str, *, at: datetime | None = None) -> str:
    """生成 `job-<book_id>-<ts>-<hex4>`。"""
    moment = at or datetime.now()
    return f"job-{book_id}-{moment.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4]}"


# ─────────────────────────── 原子写（§7.2） ───────────────────────────

#: 进程内写串行化锁（规避并发 replace 的共享冲突，见 `_atomic_write_text` 说明）。
#: 本应用是单机单进程，写入量极小，锁竞争可忽略。
_WRITE_LOCK = threading.RLock()
#: `os.replace` 的重试次数与退避基数（Windows 杀软/索引服务偶发 WinError 5）。
_REPLACE_RETRIES = 6
_REPLACE_BACKOFF = 0.02


def _atomic_write_text(path: Path, text: str, *, newline: str = "\n") -> None:
    """`.tmp` + `fsync` + `os.replace` 原子替换写。

    Args:
        path: 目标文件。
        text: 文本内容。
        newline: 传给 `open()` 的换行策略。JSON 用 `"\\n"`（跨平台字节稳定）；
            章节 Markdown 用 `""`（不翻译换行，字节级保留用户习惯 —— P0-4③）。

    Raises:
        NovelStoreError: 任何 OSError 都被包装（失败时清理临时文件）。

    注：`os.replace` 在 POSIX 上对读者是原子的，但 **Windows 上两个线程同时对
    同一目标做 replace 会偶发 WinError 5（拒绝访问）** —— 这不是"文件损坏"，
    而是目标文件被并发换名时的共享冲突。因此本进程内用一把全局锁把
    "写 tmp → fsync → replace" 串行化；写入本身仍保持原子性（读者不会读到半截）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline=newline) as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_with_retry(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise NovelStoreError(f"写入失败: {path}（{exc}）") from exc


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """`os.replace` 带退避重试。

    Windows 上连续替换同一文件名时，杀毒软件/索引服务可能在 replace 之后短暂
    持有旧文件的句柄（MoveFileEx 返回 WinError 5 拒绝访问）。这不是"文件损坏"
    （读者永远看不到半截内容），但会让自动保存偶发失败，故退避重试。
    """
    last_error: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    if last_error is not None:
        raise last_error


def _atomic_write_json(path: Path, payload: object) -> None:
    """原子写 JSON（`indent=2`、`ensure_ascii=False`、LF 换行）。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性
        raise NovelStoreError(f"JSON 序列化失败: {path}（{exc}）") from exc
    _atomic_write_text(path, text, newline="\n")


# ── 公开别名（换元仿写域复用，绝不复制原子写实现）──
# 私有名 `_atomic_write_text` / `_atomic_write_json` 保留给既有的 12 处调用点
# （一行不动），新代码统一用无下划线的公开名。两份实现同一份代码，
# 将来改 WinError5 退避逻辑只改一处。
atomic_write_text = _atomic_write_text
atomic_write_json = _atomic_write_json


def _read_text(path: Path) -> str:
    """读文本文件，不做换行翻译（`newline=""`，字节级保留）。"""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _read_json(path: Path) -> object:
    """读 JSON 文件；不存在返回 None，损坏则抛 NovelStoreError。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NovelStoreError(f"读取失败（文件可能已损坏）: {path}（{exc}）") from exc


def _mtime_iso(path: Path) -> str:
    """文件修改时间的 ISO 字符串（派生视图的 generated_at）。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:  # pragma: no cover - 防御性
        return ""


# ─────────────────────────── 服务 ───────────────────────────


class NovelStore:
    """文件系统事实层：权威数据的唯一读写入口（每次读盘，不缓存）。"""

    def __init__(self, root: Path | None = None) -> None:
        """Args:
            root: 小说数据根目录。默认 `settings.data_dir / "novel"`；
                测试直接传 `tmp_path` 注入（无需 conftest）。
        """
        self.root = Path(root) if root is not None else Path(settings.data_dir) / "novel"

    # ── 路径 ──

    def novel_root(self) -> Path:
        """小说数据根目录（绝对路径）。"""
        return self.root

    def books_root(self) -> Path:
        """`books/` 目录。"""
        return self.root / BOOKS_DIR

    def book_dir(self, book_id: str) -> Path:
        """书籍目录（已校验 book_id）。"""
        return self.books_root() / validate_id(book_id, "book_id")

    def chapters_dir(self, book_id: str) -> Path:
        """`正文/` 目录。"""
        return self.book_dir(book_id) / CHAPTERS_DIR

    def drafts_dir(self, book_id: str) -> Path:
        """`drafts/` 目录。"""
        return self.book_dir(book_id) / DRAFTS_DIR

    def checkpoints_dir(self, book_id: str) -> Path:
        """`checkpoints/` 目录。"""
        return self.book_dir(book_id) / CHECKPOINTS_DIR

    def views_dir(self, book_id: str) -> Path:
        """`views/` 目录。"""
        return self.book_dir(book_id) / VIEWS_DIR

    def chapter_path(self, book_id: str, rel_file: str) -> Path:
        """把大纲里的 `file` 字段拼成绝对路径，并约束在 `正文/` 内。

        两道校验，缺一不可：
          1. `validate_rel_path` —— 不许逃出书籍目录（防 `../`、绝对路径、盘符）。
          2. **必须在 `正文/` 之内** —— 否则 `file` 可以指向 `state.json` /
             `book.json`，随后一次 `PUT /chapters/{id}`（自动保存的原子写）
             就会把权威 JSON 覆盖成章节正文，纯 HTTP 即可毁掉一本书。
             校验放在这里而不是各调用点，是为了让 read / write / export /
             save_outline 共用同一入口，不会有人抄漏一份。
        """
        path = validate_rel_path(rel_file, self.book_dir(book_id))
        # 与 `rewrite_path()` 共用同一个词法口径 —— 并发下不会误判（QA P2-1）。
        if not lexically_inside(self.chapters_dir(book_id), path):
            raise NovelValidationError(
                f"章节文件必须位于 {CHAPTERS_DIR}/ 内: {rel_file!r}", code=ERR_PATH_ESCAPE
            )
        return path

    def rewrite_dir(self, book_id: str) -> Path:
        """`rewrite/` 目录（换元仿写域的根，已校验 book_id）。"""
        return self.book_dir(book_id) / REWRITE_DIR

    def rewrite_path(self, book_id: str, rel_file: str) -> Path:
        """把仿写域的相对路径拼成绝对路径，并约束在 `rewrite/` 内。

        与 `chapter_path()` **完全同构**的两道校验，缺一不可：
          1. `validate_rel_path` —— 不许逃出书籍目录（防 `../`、绝对路径、盘符）。
          2. **必须在 `rewrite/` 之内** —— 否则仿写域的文件名可以指向
             `book.json` / `state.json` / `正文/*.md`，一次原子写就能毁掉一本书。
             这是「仿写生成阶段零写入权威数据」的**结构性保证**（不靠自觉）。

        该函数必须与 `chapter_path()` 放在同一个类里 —— 「路径校验唯一入口」
        这条纪律的价值就在于「不会有人抄漏一份」。把它丢到新文件等于开第二个入口。
        """
        path = validate_rel_path(rel_file, self.book_dir(book_id))
        # 词法判断：**不**用 `Path.resolve()`。QA P2-1：并发首次建 `rewrite/drafts/`
        # 时 `resolve()` 返回瞬态值，约 1-2% 合法路径被误判 `path_escape`。
        # 详见 `lexically_inside`。
        if not lexically_inside(self.rewrite_dir(book_id), path):
            raise NovelValidationError(
                f"仿写文件必须位于 {REWRITE_DIR}/ 内: {rel_file!r}", code=ERR_PATH_ESCAPE
            )
        return path

    # ── 内部读写 ──

    def _write_book(self, book: BookMeta) -> None:
        _atomic_write_json(self.book_dir(book.id) / BOOK_FILE, book.model_dump(mode="json"))

    def _load_book(self, book_id: str) -> BookMeta:
        path = self.book_dir(book_id) / BOOK_FILE
        payload = _read_json(path)
        if payload is None:
            raise NovelNotFound(f"书籍不存在: {book_id}")
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"book.json 格式非法: {path}")
        return BookMeta.model_validate(payload)

    def _state_path(self, book_id: str) -> Path:
        return self.book_dir(book_id) / STATE_FILE

    def _write_state(self, state: BookState) -> None:
        _atomic_write_json(self._state_path(state.book_id), state.model_dump(mode="json", by_alias=True))

    def _iter_chapter_nodes(self, book: BookMeta) -> list[tuple[OutlineVolume, OutlineChapter]]:
        """按 (卷 order, 卷 id, 章 order, 章 id) 排序遍历全部章节节点。"""
        pairs: list[tuple[OutlineVolume, OutlineChapter]] = []
        volumes = sorted(book.outline.nodes, key=lambda v: (v.order, v.id))
        for volume in volumes:
            chapters = sorted(volume.children, key=lambda c: (c.order, c.id))
            for chapter in chapters:
                pairs.append((volume, chapter))
        return pairs

    def _find_chapter(self, book: BookMeta, chapter_id: str) -> tuple[OutlineVolume | None, OutlineChapter]:
        """按 id 定位章节节点。"""
        for volume, chapter in self._iter_chapter_nodes(book):
            if chapter.id == chapter_id:
                return volume, chapter
        raise NovelNotFound(f"章节不存在: {chapter_id}")

    def get_chapter_node(self, book_id: str, chapter_id: str) -> tuple[OutlineVolume | None, OutlineChapter]:
        """按 id 定位章节节点（供 jobs/ai 层使用，避免它们自己遍历大纲）。"""
        book = self._load_book(book_id)
        return self._find_chapter(book, validate_id(chapter_id, "chapter_id"))

    def set_chapter_status(self, book_id: str, chapter_id: str, status: str) -> BookMeta:
        """更新章节状态（如草稿产出后标 `ai_draft`）；不降级 `published`，不变更 version。"""
        book = self._load_book(book_id)
        _volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
        if chapter.status == CHAPTER_STATUS_PUBLISHED and status != CHAPTER_STATUS_PUBLISHED:
            return book
        if chapter.status == status:
            return book
        chapter.status = status
        book.updated_at = now_iso()
        self._write_book(book)
        return book

    # ── job checkpoint（IO 收敛在 store，jobs 层不直接碰文件） ──

    def job_path(self, job_id: str) -> Path:
        """checkpoint 文件路径（由 job_id 反解 book_id）。"""
        book_id, _ts = parse_job_id(job_id)
        return self.checkpoints_dir(book_id) / f"{job_id}.json"

    def save_job(self, job: NovelJob) -> None:
        """原子写 checkpoint。"""
        validate_id(job.job_id, "job_id")
        path = self.job_path(job.job_id)
        if path.parent.resolve() != self.checkpoints_dir(job.book_id).resolve():
            raise NovelValidationError(f"job 路径非法: {job.job_id!r}", code=ERR_PATH_ESCAPE)
        _atomic_write_json(path, job.model_dump(mode="json"))

    def load_job(self, job_id: str) -> NovelJob:
        """读 checkpoint（权威在磁盘，进程重启后仍可读）。"""
        path = self.job_path(job_id)
        payload = _read_json(path)
        if payload is None:
            raise NovelNotFound(f"任务不存在: {job_id}")
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"checkpoint 格式非法: {path}")
        return NovelJob.model_validate(payload)

    def _chapter_body(self, book_id: str, chapter: OutlineChapter) -> str:
        """读章节正文（文件不存在视为空）。"""
        if not chapter.file:
            return ""
        path = self.chapter_path(book_id, chapter.file)
        if not path.exists():
            return ""
        return _read_text(path)

    # ── 书架 ──

    def _book_stats(self, book: BookMeta) -> tuple[int, int]:
        """（章节数, 总字数）—— 书架列表与 meta 共用同一口径，避免两处算法漂移。"""
        word_count = 0
        for _volume, chapter in self._iter_chapter_nodes(book):
            word_count += count_words(self._chapter_body(book.id, chapter))
        return sum(len(volume.children) for volume in book.outline.nodes), word_count

    def list_books(self) -> list[dict[str, object]]:
        """`[{id, title, chapter_count, word_count, updated_at}]`，按 updated_at 降序。"""
        books: list[dict[str, object]] = []
        root = self.books_root()
        if not root.exists():
            return books
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / BOOK_FILE
            if not meta_path.exists():
                continue
            payload = _read_json(meta_path)
            if not isinstance(payload, dict):
                continue
            try:
                book = BookMeta.model_validate(payload)
            except Exception:  # pragma: no cover - 单本损坏不应拖垮列表
                continue
            chapter_count, word_count = self._book_stats(book)
            books.append(
                {
                    "id": book.id,
                    "title": book.title,
                    "chapter_count": chapter_count,
                    "word_count": word_count,
                    "updated_at": book.updated_at,
                }
            )
        books.sort(key=lambda item: (str(item["updated_at"]), str(item["id"])), reverse=True)
        return books

    def create_book(self, title: str) -> BookMeta:
        """建目录 + 写 `book.json` + 空 `state.json` + 空 `views/`。"""
        name = str(title or "").strip()
        if not name:
            raise NovelValidationError("书名不能为空", code=ERR_INVALID_TITLE)
        if len(name) > 200:
            raise NovelValidationError("书名过长（最多 200 字）", code=ERR_INVALID_TITLE)

        book_id = self._new_book_id()
        book = BookMeta(
            id=book_id,
            title=name,
            created_at=now_iso(),
            updated_at=now_iso(),
            outline=OutlineTree(
                nodes=[
                    OutlineVolume(
                        id="v1",
                        type="volume",
                        title="第一卷",
                        order=1,
                        children=[
                            OutlineChapter(
                                id="ch-001",
                                type="chapter",
                                title="第一章",
                                order=1,
                                file=f"{CHAPTERS_DIR}/ch-001-{DEFAULT_SLUG}.md",
                            )
                        ],
                    )
                ]
            ),
        )
        book_dir = self.book_dir(book_id)
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / CHAPTERS_DIR).mkdir(parents=True, exist_ok=True)
        (book_dir / VIEWS_DIR).mkdir(parents=True, exist_ok=True)
        self._write_book(book)
        state = BookState(book_id=book_id, updated_at=book.updated_at)
        self._write_state(state)
        chapter_file = book.outline.nodes[0].children[0].file
        _atomic_write_text(self.chapter_path(book_id, chapter_file), "", newline="")
        self.rebuild_views(book_id)
        return book

    def _new_book_id(self) -> str:
        """生成不冲突的 book_id（`book-<ts>`，冲突时追加序号）。"""
        base = f"book-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        candidate = base
        suffix = 2
        while self.book_dir(candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def get_book(self, book_id: str) -> BookMeta:
        """读 `book.json`（不存在 → NovelNotFound）。"""
        return self._load_book(book_id)

    def rename_book(self, book_id: str, title: str) -> BookMeta:
        """重命名书籍（`update_book_meta({"title": ...})` 的便捷子集，校验同一套）。

        校验逻辑只写一份 —— 这里委托给 `update_book_meta`，避免两处 200 字/空值
        判断各自漂移。
        """
        self.update_book_meta(book_id, {"title": title})
        return self._load_book(book_id)

    def get_book_meta(self, book_id: str) -> dict[str, object]:
        """书籍元数据（**不含大纲树**）+ 章节数/总字数。

        单独开这个入口是因为大纲树可能很大（几十卷 × 上百章），而左栏「设定摘要」
        卡只需要几个文本字段 —— 不该为了读设定去拉整棵树。
        """
        book = self._load_book(book_id)
        chapter_count, word_count = self._book_stats(book)
        return {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "genre": book.genre,
            "pov": book.pov,
            "tense": book.tense,
            "setting_summary": book.setting_summary,
            "created_at": book.created_at,
            "updated_at": book.updated_at,
            "chapter_count": chapter_count,
            "word_count": word_count,
        }

    def update_book_meta(self, book_id: str, fields: dict[str, object]) -> dict[str, object]:
        """局部更新书籍元数据：**只动显式传入的字段，缺失即保持原值，不清空**。

        `title` 走与 `rename_book` 相同的非空/长度校验。整体走 `_write_book`
        的原子写，并返回与 `get_book_meta` 同构的载荷，前端可直接替换缓存。

        Args:
            book_id: 书籍 id。
            fields: 待更新字段字典（None 值视为"未传"，跳过）。

        Raises:
            NovelValidationError: 字段不在白名单内、title 非法、文本超长。
        """
        unknown = sorted(str(key) for key in fields if str(key) not in BOOK_META_TEXT_FIELDS and str(key) != "title")
        if unknown:
            raise NovelValidationError(
                f"不支持更新的字段: {', '.join(unknown)}", code=ERR_INVALID_PAYLOAD
            )
        book = self._load_book(book_id)

        if "title" in fields:
            name = str(fields["title"] or "").strip()
            if not name:
                raise NovelValidationError("书名不能为空", code=ERR_INVALID_TITLE)
            if len(name) > 200:
                raise NovelValidationError("书名过长（最多 200 字）", code=ERR_INVALID_TITLE)
            book.title = name

        for key in BOOK_META_TEXT_FIELDS:
            value = fields.get(key)
            if value is None:
                continue
            text = str(value)
            if len(text) > BOOK_META_TEXT_LIMIT:
                raise NovelValidationError(
                    f"{key} 过长（最多 {BOOK_META_TEXT_LIMIT} 字）", code=ERR_INVALID_PAYLOAD
                )
            setattr(book, key, text)

        book.updated_at = now_iso()
        self._write_book(book)
        return self.get_book_meta(book_id)

    def delete_book(self, book_id: str) -> None:
        """删除整本书目录（P0-2②）。"""
        book_dir = self.book_dir(book_id)
        if not book_dir.exists():
            raise NovelNotFound(f"书籍不存在: {book_id}")
        try:
            shutil.rmtree(book_dir)
        except OSError as exc:
            raise NovelStoreError(f"删除失败: {book_dir}（{exc}）") from exc

    # ── 大纲 ──

    def save_outline(self, book_id: str, version: int, nodes: list[dict[str, object]]) -> BookMeta:
        """全量覆盖大纲树 + 乐观锁；联动新建/删除章节 md。

        Args:
            book_id: 书籍 id。
            version: 客户端读到的版本号，与磁盘不符 → NovelConflict（409）。
            nodes: 完整大纲树（卷 → 章两级）。

        Raises:
            NovelConflict: version 不符。
            NovelValidationError: 节点结构非法或 id 非法。
        """
        book = self._load_book(book_id)
        if int(version) != book.version:
            raise NovelConflict(
                f"大纲版本冲突: 客户端 version={version}，磁盘 version={book.version}"
            )

        try:
            tree = OutlineTree.model_validate({"nodes": list(nodes or [])})
        except Exception as exc:
            raise NovelValidationError(f"大纲结构非法: {exc}", code=ERR_INVALID_PAYLOAD) from exc

        for volume in tree.nodes:
            validate_id(volume.id, "volume_id")
            for chapter in volume.children:
                validate_id(chapter.id, "chapter_id")

        old_files = {
            chapter.id: chapter.file
            for _v, chapter in self._iter_chapter_nodes(book)
            if chapter.file
        }
        new_files: dict[str, str] = {}

        # A3：文件名在章节创建时一次性生成，此后永不随标题/排序变化。
        next_index = self._next_chapter_index(book)
        for volume in tree.nodes:
            for chapter in volume.children:
                existing = old_files.get(chapter.id) or chapter.file
                if existing:
                    # 前端回传的 file 必须过完整路径校验：既防 ../ 逃出书籍目录，
                    # 也防指向 state.json / book.json 这类权威文件（P1-A）。
                    self.chapter_path(book_id, existing)
                    new_files[chapter.id] = existing
                    chapter.file = existing
                    continue
                rel = f"{CHAPTERS_DIR}/ch-{next_index:03d}-{slugify(chapter.title)}.md"
                while rel in set(new_files.values()) or self.chapter_path(book_id, rel).exists():
                    next_index += 1
                    rel = f"{CHAPTERS_DIR}/ch-{next_index:03d}-{slugify(chapter.title)}.md"
                chapter.file = rel
                new_files[chapter.id] = rel
                next_index += 1

        book.outline = tree
        book.version += 1
        book.updated_at = now_iso()

        # 先落 book.json，再联动文件（任一步失败都诚实上报，不回滚 book.json）
        self._write_book(book)

        for rel in new_files.values():
            path = self.chapter_path(book_id, rel)
            if not path.exists():
                _atomic_write_text(path, "", newline="")
        for chapter_id, rel in old_files.items():
            if chapter_id in new_files:
                continue
            path = self.chapter_path(book_id, rel)
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:  # pragma: no cover - 防御性
                    raise NovelStoreError(f"删除章节文件失败: {path}（{exc}）") from exc
        return book

    def _next_chapter_index(self, book: BookMeta) -> int:
        """下一个可用的章节序号（现有最大序号 + 1）。"""
        max_index = 0
        for _volume, chapter in self._iter_chapter_nodes(book):
            match = re.search(r"ch-(\d{3,})", chapter.file or "")
            if match:
                max_index = max(max_index, int(match.group(1)))
        return max_index + 1

    # ── 章节 ──

    def list_chapters(self, book_id: str, volume_id: str | None = None) -> list[dict[str, object]]:
        """章节卡片列表（按卷过滤）。

        Returns:
            `[{id, title, order, status, word_count, summary, beat, file, word_target,
               volume_id, volume_title, updated_at, draft_count, last_draft_at, abs_path}]`
        """
        book = self._load_book(book_id)
        if volume_id:
            validate_id(volume_id, "volume_id")
        result: list[dict[str, object]] = []
        for volume, chapter in self._iter_chapter_nodes(book):
            if volume_id and volume.id != volume_id:
                continue
            drafts = self.list_drafts(book_id, chapter.id)
            path = self.chapter_path(book_id, chapter.file) if chapter.file else None
            result.append(
                {
                    "id": chapter.id,
                    "title": chapter.title,
                    "order": chapter.order,
                    "status": chapter.status,
                    "word_count": count_words(self._chapter_body(book_id, chapter)),
                    "word_target": chapter.word_target,
                    "summary": chapter.summary,
                    "beat": chapter.beat,
                    "file": chapter.file,
                    "volume_id": volume.id,
                    "volume_title": volume.title,
                    "updated_at": _mtime_iso(path) if path and path.exists() else "",
                    "draft_count": len(drafts),
                    "last_draft_at": drafts[0]["created_at"] if drafts else None,
                    "abs_path": str(path) if path else "",
                }
            )
        return result

    def read_chapter(self, book_id: str, chapter_id: str) -> tuple[str, Path]:
        """读章节正文：每次读盘，不缓存。

        Returns:
            `(原文, 绝对路径)`。
        """
        book = self._load_book(book_id)
        _volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
        if not chapter.file:
            raise NovelNotFound(f"章节未绑定文件: {chapter_id}")
        path = self.chapter_path(book_id, chapter.file)
        if not path.exists():
            return "", path
        return _read_text(path), path

    def write_chapter(self, book_id: str, chapter_id: str, content: str) -> dict[str, object]:
        """`newline=""` 原子写正文（字节级保留换行习惯），回写字数与 updated_at。

        不变更 `version`（version 是前端大纲乐观锁的凭据，自动保存不应让它失效）。
        """
        book = self._load_book(book_id)
        _volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
        if not chapter.file:
            raise NovelNotFound(f"章节未绑定文件: {chapter_id}")
        path = self.chapter_path(book_id, chapter.file)
        text = str(content or "")
        _atomic_write_text(path, text, newline="")

        chapter.word_count = count_words(text)
        book.updated_at = now_iso()
        self._write_book(book)
        return {
            "ok": True,
            "chapter_id": chapter.id,
            "word_count": chapter.word_count,
            "updated_at": book.updated_at,
            "abs_path": str(path),
        }

    # ── 草稿 ──

    def write_draft(self, book_id: str, chapter_id: str, mode: str, text: str) -> str:
        """写 AI 草稿到 `drafts/`（永不覆盖正文）。

        Returns:
            draft_id（`d-<chapter_id>-<ts>-<hex4>-<mode>`，文件名主干）。
        """
        validate_id(chapter_id, "chapter_id")
        safe_mode = re.sub(r"[^a-z0-9]+", "", str(mode or "").lower()) or "draft"
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        draft_id = f"d-{chapter_id}-{stamp}-{uuid.uuid4().hex[:4]}-{safe_mode}"
        path = self.drafts_dir(book_id) / f"{draft_id}.md"
        _atomic_write_text(path, str(text or ""), newline="")
        return draft_id

    def read_draft(self, book_id: str, draft_id: str) -> str:
        """读草稿全文。"""
        path = self._draft_path(book_id, draft_id)
        if not path.exists():
            raise NovelNotFound(f"草稿不存在: {draft_id}")
        return _read_text(path)

    def _draft_path(self, book_id: str, draft_id: str) -> Path:
        validate_id(draft_id, "draft_id")
        path = self.drafts_dir(book_id) / f"{draft_id}.md"
        # 二次校验：draft_id 不含分隔符，拼接后必须仍在 drafts/ 内。
        if path.parent.resolve() != self.drafts_dir(book_id).resolve():
            raise NovelValidationError(f"草稿路径非法: {draft_id!r}", code=ERR_PATH_ESCAPE)
        return path

    def list_drafts(self, book_id: str, chapter_id: str) -> list[dict[str, object]]:
        """某章节的草稿列表（按文件名降序 = 最新在前）。"""
        validate_id(chapter_id, "chapter_id")
        directory = self.drafts_dir(book_id)
        if not directory.exists():
            return []
        prefix = f"d-{chapter_id}-"
        items: list[dict[str, object]] = []
        for path in sorted(directory.glob(f"{prefix}*.md"), reverse=True):
            text = _read_text(path) if path.exists() else ""
            items.append(
                {
                    "id": path.stem,
                    "chapter_id": chapter_id,
                    "mode": path.stem.rsplit("-", 1)[-1],
                    "word_count": count_words(text),
                    "created_at": _mtime_iso(path),
                    "abs_path": str(path),
                }
            )
        return items

    def delete_draft(self, book_id: str, draft_id: str) -> None:
        """丢弃草稿（草稿可丢弃 —— P0-6③）。"""
        path = self._draft_path(book_id, draft_id)
        if not path.exists():
            raise NovelNotFound(f"草稿不存在: {draft_id}")
        try:
            path.unlink()
        except OSError as exc:
            raise NovelStoreError(f"删除草稿失败: {path}（{exc}）") from exc

    # ── 追踪态 ──

    def get_state(self, book_id: str) -> BookState:
        """读 `state.json`（不存在时返回空追踪态，不落盘）。"""
        validate_id(book_id, "book_id")
        payload = _read_json(self._state_path(book_id))
        if payload is None:
            return BookState(book_id=book_id)
        if not isinstance(payload, dict):  # pragma: no cover - 防御性
            raise NovelStoreError(f"state.json 格式非法: {self._state_path(book_id)}")
        state = BookState.model_validate(payload)
        state.book_id = book_id
        return state

    def ingest_facts(self, book_id: str, chapter_id: str, fact: ChapterFact) -> BookState:
        """唯一写 `state.json` 的入口：并入 characters / foreshadow / chapters。

        不自动改写 `rolling.summary`（由 AI 摘要或用户编辑提供）。
        """
        validate_id(chapter_id, "chapter_id")
        state = self.get_state(book_id)
        fact.id = chapter_id
        fact.adopted_at = fact.adopted_at or now_iso()

        # 章节事实：同 id 覆盖，整体按 id 排序（幂等）
        state.chapters = [item for item in state.chapters if item.id != chapter_id]
        state.chapters.append(fact)
        state.chapters.sort(key=lambda item: item.id)

        # 角色：出场即登记（已存在则保留原状态，只刷新最后出场章节）
        for name in fact.chars:
            key = str(name).strip()
            if not key:
                continue
            entry = state.characters.get(key)
            if entry is None:
                state.characters[key] = CharacterState(last_seen_chapter=chapter_id)
            else:
                entry.last_seen_chapter = chapter_id
        state.characters = {key: state.characters[key] for key in sorted(state.characters)}

        # 伏笔：埋设新增 / 回收标记
        for text in fact.planted:
            body = str(text).strip()
            if not body:
                continue
            if any(item.text == body and item.status == "open" for item in state.foreshadow):
                continue
            state.foreshadow.append(
                ForeshadowItem(
                    id=self._next_foreshadow_id(state),
                    text=body,
                    planted_chapter=chapter_id,
                    status="open",
                )
            )
        for token in fact.resolved:
            marker = str(token).strip()
            if not marker:
                continue
            for item in state.foreshadow:
                if item.status == "resolved":
                    continue
                if item.id == marker or item.text == marker:
                    item.status = "resolved"
                    item.resolved_chapter = chapter_id

        state.rolling.updated_chapter = chapter_id
        state.updated_at = now_iso()
        self._write_state(state)
        return state

    @staticmethod
    def _next_foreshadow_id(state: BookState) -> str:
        """下一个伏笔 id（`f-001` 起，按现有最大序号递增）。"""
        max_index = 0
        for item in state.foreshadow:
            match = re.search(r"(\d+)$", item.id)
            if match:
                max_index = max(max_index, int(match.group(1)))
        return f"f-{max_index + 1:03d}"

    def update_rolling_summary(self, book_id: str, summary: str) -> BookState:
        """更新全局滚动摘要（`rolling.summary`）。"""
        state = self.get_state(book_id)
        state.rolling.summary = str(summary or "")
        state.updated_at = now_iso()
        self._write_state(state)
        return state

    # ── 采纳 ──

    def adopt_draft(self, book_id: str, chapter_id: str, draft_id: str, *,
                    selection: str | None = None) -> dict[str, object]:
        """草稿转正式：正文 := 草稿（润色模式可仅替换选中区间）。

        不碰 `state.json`（摄取只在 `ingest_facts` / 采纳端点显式触发）。
        """
        book = self._load_book(book_id)
        _volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
        draft_text = self.read_draft(book_id, draft_id)
        if not chapter.file:
            raise NovelNotFound(f"章节未绑定文件: {chapter_id}")
        path = self.chapter_path(book_id, chapter.file)
        original = _read_text(path) if path.exists() else ""

        final_text = draft_text
        replaced_selection = False
        if selection:
            if selection not in original:
                raise NovelValidationError(
                    "未能在正文中找到待替换的选中片段，请重新选择后重试",
                    code=ERR_SELECTION_REQUIRED,
                )
            final_text = original.replace(selection, draft_text, 1)
            replaced_selection = True

        _atomic_write_text(path, final_text, newline="")
        chapter.word_count = count_words(final_text)
        chapter.status = CHAPTER_STATUS_PUBLISHED
        book.version += 1
        book.updated_at = now_iso()
        self._write_book(book)
        return {
            "ok": True,
            "chapter": chapter.model_dump(mode="json"),
            "chapter_id": chapter.id,
            "word_count": chapter.word_count,
            "abs_path": str(path),
            "replaced_selection": replaced_selection,
        }

    # ── 派生视图（幂等） ──

    def build_context_card(self, book_id: str, chapter_id: str | None = None) -> str:
        """续写上下文卡正文（job step1 与 views 复用同一函数，保证一致）。

        全部遍历排序化，同一权威 JSON 输出字节一致。
        """
        book = self._load_book(book_id)
        state = self.get_state(book_id)
        lines: list[str] = []
        lines.append(f"# 续写上下文卡 · {book.title}")
        lines.append("")
        lines.append("> 派生视图：由 book.json / state.json 生成 · 请勿手工编辑。")
        lines.append("")

        lines.append("## 一、设定摘要")
        lines.append(book.setting_summary.strip() or "（未填写）")
        meta: list[str] = []
        if book.genre:
            meta.append(f"类型：{book.genre}")
        if book.pov:
            meta.append(f"视角：{book.pov}")
        if book.tense:
            meta.append(f"时态：{book.tense}")
        if meta:
            lines.append("")
            lines.append(" · ".join(meta))
        lines.append("")

        lines.append("## 二、全书滚动摘要")
        lines.append(state.rolling.summary.strip() or "（暂无）")
        if state.rolling.updated_chapter:
            lines.append("")
            lines.append(f"（更新至：{state.rolling.updated_chapter}）")
        lines.append("")

        chapter: OutlineChapter | None = None
        volume_title = ""
        if chapter_id:
            volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
            volume_title = volume.title if volume else ""
            lines.append("## 三、本章细纲与节拍")
            lines.append(f"- 章节：{chapter.id} {chapter.title}（{volume_title}）".rstrip())
            lines.append(f"- 一句话细纲：{chapter.summary.strip() or '（未填写）'}")
            lines.append(f"- 本章节拍：{chapter.beat.strip() or '（未填写）'}")
            if chapter.word_target:
                lines.append(f"- 字数目标：{chapter.word_target}")
            lines.append("")

        lines.append("## 四、最近 2 章事实快照")
        # 按大纲顺序取最近 N 章 —— 不能按 fact.id 字符串排序（id 位数不齐时
        # "ch-9" > "ch-10"，会取错章）。大纲里查不到的 id（章节已删）排到最后。
        outline_order = {
            chapter.id: (volume.order, chapter.order)
            for volume, chapter in self._iter_chapter_nodes(book)
        }
        facts = sorted(
            state.chapters,
            key=lambda item: outline_order.get(item.id, (10**6, 10**6)),
        )[-RECENT_CHAPTER_FACTS:]
        if facts:
            for fact in facts:
                lines.append(f"### {fact.id} {fact.title}".rstrip())
                lines.append(f"- 出场角色：{'、'.join(fact.chars) if fact.chars else '（无）'}")
                if fact.state_changes:
                    for change in fact.state_changes:
                        lines.append(f"- 状态变化：{change}")
                if fact.planted:
                    for planted in fact.planted:
                        lines.append(f"- 埋设伏笔：{planted}")
                if fact.resolved:
                    for resolved in fact.resolved:
                        lines.append(f"- 回收伏笔：{resolved}")
                for relation in fact.relations:
                    lines.append(f"- 关系变化：{relation.source} → {relation.target}：{relation.delta}")
                lines.append(f"- 来源：{fact.source}")
                lines.append("")
        else:
            lines.append("（暂无已采纳的章节事实）")
            lines.append("")

        lines.append("## 五、未收伏笔（open）")
        open_items = sorted(
            [item for item in state.foreshadow if item.status == "open"],
            key=lambda item: item.id,
        )
        if open_items:
            for item in open_items:
                where = item.planted_chapter or "（未知章节）"
                lines.append(f"- {item.id} {item.text}（埋于 {where}）")
        else:
            lines.append("（无）")
        lines.append("")

        lines.append("## 六、角色当前状态")
        if state.characters:
            lines.append("| 角色 | 状态 | 位置 | 最后出场 |")
            lines.append("| --- | --- | --- | --- |")
            for name in sorted(state.characters):
                entry = state.characters[name]
                lines.append(
                    f"| {name} | {entry.status or '—'} | {entry.location or '—'} "
                    f"| {entry.last_seen_chapter or '—'} |"
                )
        else:
            lines.append("（暂无角色记录）")
        lines.append("")

        if chapter is not None:
            lines.append("## 七、本章正文尾部（最近 800 字，用于接得上前文）")
            body = self._chapter_body(book_id, chapter)
            tail = body[-CONTEXT_TAIL_CHARS:] if body else ""
            lines.append(tail.strip() or "（本章暂无正文）")
            lines.append("")

        return "\n".join(lines)

    def _build_timeline_view(self, book: BookMeta, state: BookState) -> str:
        """`views/timeline.md`：按章节顺序列出状态变化与伏笔埋/收（A6：不新增 schema）。"""
        lines: list[str] = []
        lines.append(f"# 时间线 · {book.title}")
        lines.append("")
        lines.append("> 派生视图：按章节顺序从 state.json 派生（状态变化 / 伏笔埋收）· 请勿手工编辑。")
        lines.append("> 注：MVP 不记录故事内时间（年月日），只记录章节序列。")
        lines.append("")

        facts = sorted(state.chapters, key=lambda item: item.id)
        if not facts:
            lines.append("（暂无已采纳的章节事实 — 采纳章节后会自动出现）")
            lines.append("")
            return "\n".join(lines)

        for fact in facts:
            lines.append(f"## {fact.id} {fact.title}".rstrip())
            if fact.chars:
                lines.append(f"- 出场：{'、'.join(fact.chars)}")
            for change in fact.state_changes:
                lines.append(f"- 状态变化：{change}")
            for planted in fact.planted:
                lines.append(f"- 埋设伏笔：{planted}")
            for resolved in fact.resolved:
                lines.append(f"- 回收伏笔：{resolved}")
            for relation in fact.relations:
                lines.append(f"- 关系变化：{relation.source} → {relation.target}：{relation.delta}")
            lines.append(f"- 来源：{fact.source} · 采纳于 {fact.adopted_at or '—'}")
            lines.append("")

        open_items = sorted(
            [item for item in state.foreshadow if item.status == "open"],
            key=lambda item: item.id,
        )
        lines.append("## 仍未回收的伏笔")
        if open_items:
            for item in open_items:
                lines.append(f"- {item.id} {item.text}（埋于 {item.planted_chapter or '—'}）")
        else:
            lines.append("（无）")
        lines.append("")
        return "\n".join(lines)

    def _build_characters_view(self, book: BookMeta, state: BookState) -> str:
        """`views/characters.md`：角色表（按名字排序，幂等）。"""
        lines: list[str] = []
        lines.append(f"# 角色表 · {book.title}")
        lines.append("")
        lines.append("> 派生视图：由 state.json 派生 · 请勿手工编辑。")
        lines.append("")
        if not state.characters:
            lines.append("（暂无角色记录 — 采纳带出场角色的章节后会自动出现）")
            lines.append("")
            return "\n".join(lines)

        lines.append("| 角色 | 状态 | 位置 | 最后出场 | 特质 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name in sorted(state.characters):
            entry = state.characters[name]
            traits = "、".join(entry.traits) if entry.traits else "—"
            lines.append(
                f"| {name} | {entry.status or '—'} | {entry.location or '—'} "
                f"| {entry.last_seen_chapter or '—'} | {traits} |"
            )
        lines.append("")
        return "\n".join(lines)

    def rebuild_views(self, book_id: str) -> list[str]:
        """重建全部派生视图（排序化 → 同一权威 JSON 两次输出字节一致）。

        Returns:
            生成的文件名列表（已排序）。
        """
        book = self._load_book(book_id)
        state = self.get_state(book_id)
        directory = self.views_dir(book_id)
        directory.mkdir(parents=True, exist_ok=True)
        payloads: dict[str, str] = {
            f"{VIEW_CONTEXT_CARD}.md": self.build_context_card(book_id, None),
            f"{VIEW_TIMELINE}.md": self._build_timeline_view(book, state),
            f"{VIEW_CHARACTERS}.md": self._build_characters_view(book, state),
        }
        for name in sorted(payloads):
            _atomic_write_text(directory / name, payloads[name], newline="\n")
        return sorted(payloads)

    def read_view(self, book_id: str, name: str) -> str:
        """读派生视图；不存在时先从权威 JSON 重建一次（派生数据可随时再生）。"""
        if name not in VIEW_NAMES:
            raise NovelValidationError(f"视图名非法: {name!r}", code=ERR_INVALID_PAYLOAD)
        path = self.views_dir(book_id) / f"{name}.md"
        if not path.exists():
            self.rebuild_views(book_id)
        if not path.exists():  # pragma: no cover - 防御性
            raise NovelNotFound(f"视图不存在: {name}")
        return _read_text(path)

    # ── 导出 ──

    def export_chapter(self, book_id: str, chapter_id: str, fmt: str) -> tuple[str, str]:
        """导出单章。

        Returns:
            `(文件名, 内容)`。fmt=md 时内容等于磁盘 md 原文（P0-14①）。
        """
        book = self._load_book(book_id)
        _volume, chapter = self._find_chapter(book, validate_id(chapter_id, "chapter_id"))
        raw = self._chapter_body(book_id, chapter)
        extension = "txt" if fmt == "txt" else "md"
        content = strip_markdown(raw) if fmt == "txt" else raw
        return f"{self._export_stem(book.title, chapter.title, chapter.id)}.{extension}", content

    def export_book(self, book_id: str, fmt: str) -> tuple[str, str]:
        """按大纲顺序拼接整本导出（卷名作为 `#` 分隔）。

        Returns:
            `(文件名, 内容)`。
        """
        book = self._load_book(book_id)
        chunks: list[str] = []
        if fmt == "txt":
            chunks.append(f"{book.title}\n")
        for volume, chapter in self._iter_chapter_nodes(book):
            body = self._chapter_body(book_id, chapter)
            if fmt == "txt":
                chunks.append(f"{volume.title}\n\n{chapter.title}\n\n{strip_markdown(body)}")
            else:
                chunks.append(f"# {volume.title}\n\n## {chapter.title}\n\n{body}")
        # 零章书：不拼接尾随换行，导出内容为空字符串（此前会返回一个孤零零的 "\n"）
        content = "\n\n".join(chunks).rstrip("\n")
        if chunks:
            content += "\n"
        extension = "txt" if fmt == "txt" else "md"
        return f"{self._export_stem(book.title, '', book.id)}.{extension}", content

    @staticmethod
    def _export_stem(book_title: str, chapter_title: str, fallback: str) -> str:
        """导出文件名主干：优先用可读的 ascii slug，纯中文时退化成 id（保证 ASCII 安全）。"""
        for candidate in (chapter_title, book_title):
            slug = slugify(candidate)
            if slug != DEFAULT_SLUG:
                return slug
        return fallback
