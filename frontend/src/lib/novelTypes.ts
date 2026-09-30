// 小说工作区 — TS 类型镜像
//
// 与 backend/app/services/novel_store.py 的 Pydantic 模型手工同步。
// 任一侧改字段名，必须同步改另一侧，并更新 test_novel_store.py 的 schema 快照用例。
//
// 两条必须记住的对齐约定（踩了就是静默错位）：
//   1. RelationDelta 的 **HTTP/磁盘 JSON 键名是 from / to**（Pydantic 字段名是
//      source/target，因为 `from` 是 Python 保留字）。本文件按 HTTP 侧写。
//   2. 枚举值收窄成联合类型，拼错在编译期就报错（参照 api.ts 的 PostureVerdict）。
//
// 时间格式：ISO 8601 带本地偏移，前端只做展示格式化，不做时区换算。

// ===== 枚举 =====

/** 章节状态：○ 仅大纲 / ◐ AI草稿待采纳 / ● 正式 */
export type ChapterStatus = 'draft' | 'ai_draft' | 'published'

/** 事实快照来源 */
export type FactSource = 'ai' | 'manual'

/** 伏笔状态 */
export type ForeshadowStatus = 'open' | 'resolved'

/** AI 任务模式 */
export type JobMode = 'continue' | 'polish'

/** 任务状态。终态集合 = {done, failed, cancelled}（见 NOVEL_JOB_TERMINAL） */
export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'

/**
 * 单步状态。
 * - `skipped`：resume 后**已有产物**的已完成步骤，表示「本次未重跑」。
 * - `cancelled`：被取消的那一步，**没有产物**，resume 时必须重跑。
 *   两者都表示"这一步这次没走完"，但语义相反，UI 不能混为一谈。
 */
export type StepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped' | 'cancelled'

/** 四步名称与顺序（固定） */
export type StepName = 'context' | 'draft_text' | 'fact_snapshot' | 'ingest'

/** 派生只读视图名 */
export type ViewName = 'context-card' | 'timeline' | 'characters'

// ===== 大纲（卷 → 章 两级，不做递归）=====

export interface OutlineChapter {
  id: string
  type: 'chapter'
  title: string
  order: number
  status: ChapterStatus
  word_target: number
  summary: string
  beat: string
  file: string
  word_count: number
}

export interface OutlineVolume {
  id: string
  type: 'volume'
  title: string
  order: number
  children: OutlineChapter[]
}

export interface OutlineTree {
  nodes: OutlineVolume[]
}

// ===== book.json =====

export interface BookMeta {
  version: number
  id: string
  title: string
  author: string
  genre: string
  pov: string
  tense: string
  setting_summary: string
  created_at: string
  updated_at: string
  outline: OutlineTree
}

// ===== state.json =====

export interface CharacterState {
  status: string
  location: string
  last_seen_chapter: string | null
  traits: string[]
}

export interface ForeshadowItem {
  id: string
  text: string
  planted_chapter: string | null
  status: ForeshadowStatus
  resolved_chapter: string | null
}

/**
 * 关系变化 —— HTTP/磁盘键名是 **from / to**。
 * Pydantic 侧字段名为 source/target（Python 保留字），序列化时带 alias。
 */
export interface RelationDelta {
  from: string
  to: string
  delta: string
}

export interface ChapterFact {
  id: string
  title: string
  chars: string[]
  state_changes: string[]
  planted: string[]
  resolved: string[]
  relations: RelationDelta[]
  source: FactSource
  adopted_at: string | null
}

export interface RollingState {
  summary: string
  updated_chapter: string | null
}

export interface BookState {
  book_id: string
  updated_at: string
  rolling: RollingState
  characters: Record<string, CharacterState>
  foreshadow: ForeshadowItem[]
  chapters: ChapterFact[]
}

// ===== job / checkpoint =====

export interface JobStep {
  name: StepName
  status: StepStatus
  at: string | null
  error: string | null
}

/** 第 4 步 ingest 只落摄取计划，**不写 state.json**；真正写入只在 /adopt。 */
export interface IngestPlan {
  chapter_id: string
  chapter_title: string
  new_characters: string[]
  state_changes: string[]
  planted: string[]
  resolved: string[]
  relations: RelationDelta[]
  rolling_summary_patch: string
  summary_patch_source: 'local_rule'
  adopt_required: boolean
  note: string
}

export interface JobArtifacts {
  context_card_md: string | null
  draft_id: string | null
  draft_text: string | null
  /** 事实快照（ChapterFact 的 JSON 形态；relations 键名为 from/to） */
  fact_json: ChapterFact | null
  ingest_plan: IngestPlan | null
}

export interface NovelJob {
  job_id: string
  book_id: string
  chapter_id: string
  mode: JobMode
  selection: string | null
  skip_gate: boolean
  created_at: string
  updated_at: string
  steps: JobStep[]
  artifacts: JobArtifacts
  status: JobStatus
  failed_step: StepName | null
}

// ===== 写后自检 =====

export interface LintHit {
  rule: string
  message: string
  line: number | null
  excerpt: string
}

// ===== AI 可用性 =====

export interface AiStatus {
  configured: boolean
  provider: string
  model: string
  available: boolean
  reason: string | null
  code: string | null
}

export interface NovelStatusResponse extends AiStatus {
  /** 小说数据根目录绝对路径（空态如实展示，兑现「数据在你自己的 data/ 里」） */
  data_dir_abs: string
  books_dir_abs: string
}

// ===== 各端点的响应包装 =====

export interface BookListItem {
  id: string
  title: string
  chapter_count: number
  word_count: number
  updated_at: string
}

export interface BooksResponse {
  books: BookListItem[]
  data_dir_abs: string
}

/**
 * `GET /books/{id}/meta` 的响应体 —— 书籍元数据**不含大纲树**。
 * 左栏「设定摘要」卡只需要这几个文本字段，不该为了读设定拉整棵树。
 */
export interface BookMetaPayload {
  id: string
  title: string
  author: string
  genre: string
  pov: string
  tense: string
  setting_summary: string
  created_at: string
  updated_at: string
  chapter_count: number
  word_count: number
}

/** `PATCH /books/{id}` 请求体：只传要改的字段，未传的保持原值、不清空。 */
export interface PatchBookMetaRequest {
  title?: string
  author?: string
  genre?: string
  pov?: string
  tense?: string
  setting_summary?: string
}

export interface OutlineResponse {
  version: number
  nodes: OutlineVolume[]
  book_title: string
}

export interface ChapterCard {
  id: string
  title: string
  order: number
  status: ChapterStatus
  word_count: number
  word_target: number
  summary: string
  beat: string
  file: string
  volume_id: string
  volume_title: string
  updated_at: string
  draft_count: number
  last_draft_at: string | null
  abs_path: string
}

export interface ChaptersResponse {
  chapters: ChapterCard[]
}

export interface DraftPayload {
  id: string
  mode: JobMode
  created_at: string
  text: string
}

export interface ChapterDetailResponse {
  chapter: OutlineChapter
  content: string
  word_count: number
  abs_path: string
  draft: DraftPayload | null
}

export interface PutChapterResult {
  ok: boolean
  chapter_id: string
  word_count: number
  updated_at: string
  abs_path: string
}

export interface ViewResponse {
  name: string
  content: string
  generated_at: string
}

export interface RebuildViewsResult {
  ok: boolean
  files: string[]
}

export interface LintResponse {
  source: string
  hits: LintHit[]
  count: number
}

export interface AdoptResponse {
  ok: boolean
  chapter: OutlineChapter
  word_count: number
  abs_path: string
  state: BookState
  views_rebuilt: string[]
}

export interface OkResponse {
  ok: boolean
}

// ===== 请求体 =====

export interface PatchStateRequest {
  rolling_summary?: string
  chapter_id?: string
  fact?: Partial<ChapterFact>
}

export interface AiDraftRequest {
  mode: JobMode
  selection?: string | null
  skip_gate?: boolean
}

export interface AdoptRequest {
  draft_id: string
  fact?: ChapterFact | null
  selection?: string | null
}

// ===== 展示用常量 =====

export const CHAPTER_STATUS_LABELS: Record<ChapterStatus, string> = {
  draft: '仅大纲',
  ai_draft: 'AI 草稿待采纳',
  published: '正式',
}

/** 状态点图例：○ 仅大纲 / ◐ AI草稿待采纳 / ● 正式 */
export const CHAPTER_STATUS_DOT: Record<ChapterStatus, string> = {
  draft: '○',
  ai_draft: '◐',
  published: '●',
}

export const STEP_LABELS: Record<StepName, string> = {
  context: '组装上下文',
  draft_text: '生成正文',
  fact_snapshot: '事实快照',
  ingest: '摄取计划',
}

export const STEP_ORDER: StepName[] = ['context', 'draft_text', 'fact_snapshot', 'ingest']

export const VIEW_LABELS: Record<ViewName, string> = {
  'context-card': '续写上下文卡',
  timeline: '时间线',
  characters: '角色表',
}

export const LINT_RULE_LABELS: Record<string, string> = {
  truncated_tail: '结尾疑似截断',
  eng_leak: '工程词泄漏',
  ai_cliche: 'AI 高频味词',
  repeat_sentence: '连续重复句',
}
