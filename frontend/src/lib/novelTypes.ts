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

// ===== 换元仿写（rewrite）=====
//
// 与 backend/app/services/novel_rewrite_store.py 的 Pydantic 模型手工同步。
// 三条必须记住的对齐约定（踩了就是静默错位）：
//   1. 关系边的 **HTTP/磁盘 JSON 键名是 from / to**（Pydantic 字段名是
//      source/target，因为 `from` 是 Python 保留字）。本文件按 HTTP 侧写。
//   2. 质检四态 `CheckStatus` 是**封闭联合**，禁止增加第五态、禁止把
//      `unavailable` 渲染成 `pass`（「待核」≠「通过」）。
//   3. 免责文案以后端下发的 `report.disclaimer.text` 为**唯一来源**，
//      前端不另写一份（P0-6：不许出现承诺式措辞）。

/** 质检四态（★禁止增加第五态，禁止混淆★） */
export type CheckStatus = 'pass' | 'warn' | 'fail' | 'unavailable'

export const CHECK_STATUSES: readonly CheckStatus[] = [
  'pass',
  'warn',
  'fail',
  'unavailable',
] as const

/** 八项质检的 key（与后端 CHECK_KEYS 同序） */
export type CheckKey =
  | 'proper_noun'
  | 'signature_scene'
  | 'relation_topology'
  | 'beat_sequence'
  | 'near_duplicate'
  | 'unique_prop'
  | 'one_to_one_character'
  | 'isomorphic_reversal'

export const CHECK_KEYS: readonly CheckKey[] = [
  'proper_noun',
  'signature_scene',
  'relation_topology',
  'beat_sequence',
  'near_duplicate',
  'unique_prop',
  'one_to_one_character',
  'isomorphic_reversal',
] as const

export const CHECK_LABELS: Record<CheckKey, string> = {
  proper_noun: '① 原作专名黑名单',
  signature_scene: '② 标志场景黑名单',
  relation_topology: '③ 关系拓扑指纹（命门）',
  beat_sequence: '④ 桥段功能序列（命门）',
  near_duplicate: '⑤ 原句 / 近复制句',
  unique_prop: '⑥ 独特道具 / 具体对白',
  one_to_one_character: '⑦ 一对一人物映射',
  isomorphic_reversal: '⑧ 同构反转底牌',
}

/** 四态标签（`unavailable` 是「待核」，不是「通过」） */
export const CHECK_STATUS_LABELS: Record<CheckStatus, string> = {
  pass: '通过',
  warn: '需人工核对',
  fail: '硬阻断',
  unavailable: '待核',
}

/** 四态状态点字符 */
export const CHECK_STATUS_DOT: Record<CheckStatus, string> = {
  pass: '●',
  warn: '◐',
  fail: '✕',
  unavailable: '?',
}

/**
 * 四态文字配色（穷举表，新增状态会在编译期报错）。
 * `pass` 用中性前景色 —— 域色 #22c55e 只用于状态点（见 CHECK_PASS_COLOR）。
 */
export const CHECK_STATUS_TONE: Record<CheckStatus, string> = {
  pass: 'text-secondary',
  warn: 'text-warning',
  fail: 'text-danger',
  unavailable: 'text-muted',
}

/** 域色 #22c55e 的唯一合法用途之一：pass 状态点。 */
export const CHECK_PASS_COLOR = '#22c55e'

/**
 * **免责声明不存在第二来源** —— 前端任何分支都不写免责措辞。
 *
 * 声明只能来自两处、且两处同源：
 *   1. `GET .../rewrite/disclaimer`（生成前，报告还没有的时候）；
 *   2. `report.disclaimer`（出了报告之后）。
 * 若端点取不到，UI 只能显示「加载失败」这样的**状态句**（见 RiskNoticeCard 的
 * `DISCLAIMER_LOAD_FAILED`），绝不能显示替代声明 —— 否则将来改了后端文案，
 * 用户在生成前（告知最该生效的时机）看到的就是过期声明。
 */

/** 产物类型（plan 设定卡 / outline 章级大纲 / chapter 分章草稿） */
export type RewriteKind = 'plan' | 'outline' | 'chapter'

export const REWRITE_KIND_LABELS: Record<RewriteKind, string> = {
  plan: '五层重建设定卡',
  outline: '六章级大纲',
  chapter: '分章草稿',
}

// ===== 结构蓝图 =====

/** 来源标注（只存笔记/描述，**绝不存原文**） */
export interface SourceRef {
  label: string
  work_type: string
  note: string
}

/** 抽象层功能位（思想层面，不受著作权保护） */
export interface FunctionSlot {
  slot: string
  trait: string
}

export interface AbstractLayer {
  function_slots: FunctionSlot[]
  emotion_beats: string[]
  info_gap: string[]
  reversal_types: string[]
  reversal_positions: number[]
  motifs: string[]
  pacing: string
}

/** 关系图有向边（HTTP 键名 from / to） */
export interface RewriteRelationEdge {
  from: string
  to: string
  /** 关系类型：师徒 / 同门 / 敌对 / 管理 / 血缘 … */
  kind: string
  /** 权力流向："高→低" / "低→高" / "对等" */
  power: string
}

export interface L1Symbols {
  banned: string[]
  new_lexicon: Record<string, string>
}

export interface L2Scenes {
  banned: string[]
  new_scenes: string[]
}

export interface L3Relations {
  source_graph: RewriteRelationEdge[]
  /** A1 裁定：降级为只读展示，判据只用两张图 */
  source_fingerprint: Record<string, unknown> | null
  new_graph: RewriteRelationEdge[]
}

export interface L4Events {
  new_causal_chain: string[]
}

export interface L5Beats {
  source_seq: string[]
  new_seq: string[]
}

export interface RebuildLayer {
  L1_symbols: L1Symbols
  L2_scenes: L2Scenes
  L3_relations: L3Relations
  L4_events: L4Events
  L5_beats: L5Beats
}

export interface GateInfo {
  required_layers: string[]
  skipped_at: string | null
  skip_reason: string
}

export interface Blueprint {
  version: number
  id: string
  book_id: string
  title: string
  created_at: string
  updated_at: string
  source_ref: SourceRef
  abstract: AbstractLayer
  rebuild: RebuildLayer
  gate: GateInfo
}

/** 空蓝图（新建书时的初始形态；不填充任何示例内容 —— 空就是空）。 */
export function emptyBlueprint(bookId = ''): Blueprint {
  return {
    version: 1,
    id: '',
    book_id: bookId,
    title: '',
    created_at: '',
    updated_at: '',
    source_ref: { label: '', work_type: '', note: '' },
    abstract: {
      function_slots: [],
      emotion_beats: [],
      info_gap: [],
      reversal_types: [],
      reversal_positions: [],
      motifs: [],
      pacing: '',
    },
    rebuild: {
      L1_symbols: { banned: [], new_lexicon: {} },
      L2_scenes: { banned: [], new_scenes: [] },
      L3_relations: { source_graph: [], source_fingerprint: null, new_graph: [] },
      L4_events: { new_causal_chain: [] },
      L5_beats: { source_seq: [], new_seq: [] },
    },
    gate: { required_layers: ['L3', 'L5'], skipped_at: null, skip_reason: '' },
  }
}

/** 关系拓扑指纹（L3 判据的展示态） */
export interface RelationFingerprint {
  node_count: number
  edge_count: number
  nodes: string[]
  degrees: number[]
  kinds: Record<string, number>
  flow: Record<string, number>
  unknown_power: number
}

// ===== 质检报告 =====

export interface Evidence {
  line: number | null
  excerpt: string
}

export interface CheckItem {
  key: string
  layer: string
  mode: string
  status: CheckStatus
  detail: string
  evidence: Evidence[]
  human_tip: string
  human_checked: boolean
  checked_at: string | null
  /** 算法中间量：展示「为什么这么判」（可核对性） */
  metrics: Record<string, unknown>
}

export interface ReverseQuestion {
  q: string
  expect: string
  human_checked: boolean
  human_answer: string | null
}

export interface ReportSummary {
  /** fail 数（硬阻断） */
  blocking: number
  warn: number
  unavailable: number
  passed: number
  adoptable: boolean
}

export interface ReportAck {
  required: boolean
  acknowledged_at: string | null
  disclaimer_version: string | null
  checked_keys: string[]
}

export interface Disclaimer {
  version: string
  text: string
}

export interface RewriteReport {
  rewrite_id: string
  blueprint_id: string
  book_id: string
  chapter_id: string | null
  kind: RewriteKind
  /** 相对 rewrite/ 的草稿路径 */
  draft_file: string
  generated_at: string
  disclaimer: Disclaimer
  checks: CheckItem[]
  reverse_three: ReverseQuestion[]
  summary: ReportSummary
  ack: ReportAck
}

// ===== 仿写 job =====

export type RewriteStepName = 'precheck' | 'generate' | 'evaluate' | 'finalize'

export interface RewriteJobStep {
  name: RewriteStepName
  status: StepStatus
  at: string | null
  error: string | null
}

export const REWRITE_STEP_ORDER: RewriteStepName[] = [
  'precheck',
  'generate',
  'evaluate',
  'finalize',
]

export const REWRITE_STEP_LABELS: Record<RewriteStepName, string> = {
  precheck: '输入预检',
  generate: 'AI 生成',
  evaluate: '本地质检',
  finalize: '出报告',
}

export interface RewriteArtifacts {
  precheck_hits: Record<string, unknown>[]
  draft_rel: string | null
  draft_text: string | null
  outline_patch: Record<string, unknown> | null
  character_table: Record<string, unknown>[]
  reversal_table: Record<string, unknown>[]
  lint_hits: Record<string, unknown>[]
}

export interface RewriteJob {
  job_id: string
  book_id: string
  chapter_id: string | null
  kind: RewriteKind
  blueprint_id: string
  risk_ack: boolean
  skip_gate: boolean
  created_at: string
  updated_at: string
  steps: RewriteJobStep[]
  artifacts: RewriteArtifacts
  rewrite_id: string | null
  status: JobStatus
  failed_step: string | null
}

// ===== 请求 / 响应包装 =====

export interface BlueprintResponse {
  ok: boolean
  blueprint: Blueprint
  ready: boolean
  missing_layers: string[]
}

/** 预检命中项（后端绝不会回显被拒原文全文，只给 ≤40 字片段） */
export interface PrecheckHit {
  field: string
  rule: string
  excerpt: string
  hint: string
}

/** 预检结果：422 也是**正常返回**（要把 hits + 引导示例展示给用户）。 */
export interface PrecheckResult {
  ok: boolean
  passed: boolean
  hits: PrecheckHit[]
  sample: string
  honesty_note: string
  message: string
}

export interface ReportResponse {
  ok: boolean
  report: RewriteReport
}

export interface ReportListItem {
  rewrite_id: string
  kind: string
  generated_at: string
  blocking: number
  adoptable: boolean
}

export interface ReportsResponse {
  ok: boolean
  reports: ReportListItem[]
  count: number
}

export interface RewriteAdoptResponse {
  ok: boolean
  chapter?: OutlineChapter
  state?: BookState
  views_rebuilt?: string[]
  outline?: OutlineTree
  version?: number
}

export interface RewriteSnapshotResponse {
  ok: boolean
  snapshot: Record<string, unknown>
}

/** `GET .../rewrite/disclaimer` —— 免责声明的**单点下发**（生成前用）。 */
export interface RewriteDisclaimerResponse {
  ok: boolean
  disclaimer: Disclaimer
  /** 预检诚实声明（UI 固定小字，后端同时下发） */
  honesty_note: string
}

export interface RewriteGenerateRequest {
  risk_ack?: boolean
  skip_gate?: boolean
  skip_reason?: string
  chapter_id?: string | null
  version?: number | null
}

export interface RewriteCheckPayload {
  key: string
  human_checked: boolean
}

export interface RewriteReversePayload {
  index: number
  human_checked: boolean
  human_answer?: string | null
}

export interface RewriteCheckRequest {
  checks: RewriteCheckPayload[]
  reverse: RewriteReversePayload[]
}

export interface RewriteAdoptRequest {
  ack?: boolean
  target?: 'chapter' | 'outline'
  version?: number | null
  fact?: Record<string, unknown> | null
}
