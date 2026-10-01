// 小说工作区 — API 客户端（21 个端点 + 换元仿写 13 个端点的一层薄封装）
//
// 请求约定全部复用 `@/lib/api` 的 `request`（同一套 fetch / toast / 错误处理），
// 不自建第二套。导出接口见 ARCHITECTURE.md §3.6。
//
// 三条纪律：
//   1. 自动保存类调用传 `quiet: true` —— 由 ChapterEditor 自行展示「未保存（原因）」
//      徽标，避免 800ms 防抖把 toast 刷屏。
//   2. 导出走 `window.open(exportUrl(...))`，不经过 `request`（后端返回的是
//      带 Content-Disposition 的文件流，不是 JSON）。
//   3. 唯一例外：仿写预检 `novelRewritePrecheck` 直接 fetch（422 响应体里带
//      hits + 引导示例，`request` 会把它们丢掉）。原因写在函数注释里。

import { request } from '@/lib/api'
import { toast } from '@/components/Toast'
import type {
  AdoptRequest,
  AdoptResponse,
  AiDraftRequest,
  AiStatus,
  Blueprint,
  BlueprintResponse,
  BookMeta,
  BookMetaPayload,
  BookState,
  BooksResponse,
  ChapterCard,
  ChapterDetailResponse,
  ChaptersResponse,
  LintResponse,
  NovelJob,
  NovelStatusResponse,
  OkResponse,
  OutlineResponse,
  OutlineVolume,
  PatchBookMetaRequest,
  PatchStateRequest,
  PrecheckHit,
  PrecheckResult,
  PutChapterResult,
  RebuildViewsResult,
  ReportResponse,
  ReportsResponse,
  RewriteAdoptRequest,
  RewriteAdoptResponse,
  RewriteCheckRequest,
  RewriteDisclaimerResponse,
  RewriteGenerateRequest,
  RewriteJob,
  RewriteSnapshotResponse,
  ViewName,
  ViewResponse,
} from './novelTypes'

const BASE = '/api/novel'

function json(method: string, body?: unknown) {
  return {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

export const novelApi = {
  // 1. AI 网关可用性（永远 200；无 Key 时 available=false + reason + code）
  status: () => request<NovelStatusResponse>(`${BASE}/status`),

  // 2~7. 书架 + 书籍元数据
  listBooks: () => request<BooksResponse>(`${BASE}/books`),
  createBook: (title: string) => request<BookMeta>(`${BASE}/books`, json('POST', { title })),
  /** 读元数据（不含大纲树）。patch 与之同构，前端可直接拿返回值替换缓存。 */
  getBookMeta: (bookId: string) =>
    request<BookMetaPayload>(`${BASE}/books/${encodeURIComponent(bookId)}/meta`),
  /** 局部更新：只传要改的字段，未出现的字段后端保持原值、不清空。 */
  updateBookMeta: (bookId: string, patch: PatchBookMetaRequest) =>
    request<BookMetaPayload>(
      `${BASE}/books/${encodeURIComponent(bookId)}`,
      json('PATCH', patch),
    ),
  deleteBook: (bookId: string) =>
    request<OkResponse>(`${BASE}/books/${encodeURIComponent(bookId)}`, json('DELETE')),

  // 6~7. 大纲（PUT 必带 version 乐观锁，冲突 409 version_conflict）
  getOutline: (bookId: string) =>
    request<OutlineResponse>(`${BASE}/books/${encodeURIComponent(bookId)}/outline`),
  putOutline: (bookId: string, version: number, nodes: OutlineVolume[]) =>
    request<BookMeta>(
      `${BASE}/books/${encodeURIComponent(bookId)}/outline`,
      json('PUT', { version, nodes }),
    ),

  // 8~10. 章节
  listChapters: (bookId: string, volumeId?: string) =>
    request<ChaptersResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/chapters${
        volumeId ? `?volume_id=${encodeURIComponent(volumeId)}` : ''
      }`,
    ),
  getChapter: (bookId: string, chapterId: string) =>
    request<ChapterDetailResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/chapters/${encodeURIComponent(chapterId)}`,
    ),
  /** 自动保存入口：quiet=true，失败由编辑器自行展示「未保存」徽标 */
  putChapter: (bookId: string, chapterId: string, content: string) =>
    request<PutChapterResult>(
      `${BASE}/books/${encodeURIComponent(bookId)}/chapters/${encodeURIComponent(chapterId)}`,
      { ...json('PUT', { content }), quiet: true },
    ),

  // 11~12. 追踪态
  getState: (bookId: string) =>
    request<BookState>(`${BASE}/books/${encodeURIComponent(bookId)}/state`),
  patchState: (bookId: string, body: PatchStateRequest) =>
    request<BookState>(`${BASE}/books/${encodeURIComponent(bookId)}/state`, json('PATCH', body)),

  // 13~14. 派生只读视图
  getView: (bookId: string, name: ViewName) =>
    request<ViewResponse>(`${BASE}/books/${encodeURIComponent(bookId)}/views/${name}`),
  rebuildViews: (bookId: string) =>
    request<RebuildViewsResult>(
      `${BASE}/books/${encodeURIComponent(bookId)}/views/rebuild`,
      json('POST'),
    ),

  // 15. 启动 AI 任务（202；无 Key → 503 ai_unavailable；无细纲 → 422 missing_beat）
  aiDraft: (bookId: string, chapterId: string, body: AiDraftRequest) =>
    request<NovelJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/chapters/${encodeURIComponent(
        chapterId,
      )}/ai/draft`,
      json('POST', body),
    ),

  // 16. 采纳草稿（草稿 → 正文 + 摄取事实 + 重建视图）
  adopt: (bookId: string, chapterId: string, body: AdoptRequest) =>
    request<AdoptResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/chapters/${encodeURIComponent(
        chapterId,
      )}/adopt`,
      json('POST', body),
    ),

  // 17. 写后自检（纯本地规则，只提醒不改写）
  lint: (bookId: string, body: { text?: string; chapter_id?: string }) =>
    request<LintResponse>(`${BASE}/books/${encodeURIComponent(bookId)}/lint`, json('POST', body)),

  // 19~21. 任务
  getJob: (jobId: string) => request<NovelJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}`),
  resumeJob: (jobId: string) =>
    request<NovelJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/resume`, json('POST')),
  cancelJob: (jobId: string) =>
    request<NovelJob>(`${BASE}/jobs/${encodeURIComponent(jobId)}/cancel`, json('POST')),

  // ===== 换元仿写（13 端点，前缀 /books/{id}/rewrite）=====

  // R1/R2 · 结构蓝图
  getRewriteBlueprint: (bookId: string) =>
    request<BlueprintResponse>(`${BASE}/books/${encodeURIComponent(bookId)}/rewrite/blueprint`),
  /** 保存前后端会跑完整预检，命中即 422（一个字节都不落盘）。 */
  putRewriteBlueprint: (bookId: string, blueprint: Blueprint) =>
    request<BlueprintResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/blueprint`,
      json('PUT', { blueprint }),
    ),

  // R4/R5/R6 · 生成（202 + RewriteJob）
  rewritePlan: (bookId: string, body: RewriteGenerateRequest = {}) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/plan`,
      json('POST', body),
    ),
  rewriteOutline: (bookId: string, body: RewriteGenerateRequest = {}) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/outline`,
      json('POST', body),
    ),
  rewriteChapterDraft: (bookId: string, body: RewriteGenerateRequest = {}) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/chapter-draft`,
      json('POST', body),
    ),

  // R7/R8/R9 · 报告
  getRewriteReport: (bookId: string, rewriteId: string) =>
    request<ReportResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/reports/${encodeURIComponent(
        rewriteId,
      )}`,
    ),
  postRewriteCheck: (bookId: string, rewriteId: string, body: RewriteCheckRequest) =>
    request<ReportResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/reports/${encodeURIComponent(
        rewriteId,
      )}/check`,
      json('POST', body),
    ),
  /** ★唯一入书口★：一次只处理一个产物，必须 ack=true。 */
  postRewriteAdopt: (bookId: string, rewriteId: string, body: RewriteAdoptRequest) =>
    request<RewriteAdoptResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/reports/${encodeURIComponent(
        rewriteId,
      )}/adopt`,
      json('POST', { ack: true, target: 'chapter', ...body }),
    ),

  // R10/R11/R12 · 任务
  getRewriteJob: (bookId: string, jobId: string) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/jobs/${encodeURIComponent(jobId)}`,
    ),
  resumeRewriteJob: (bookId: string, jobId: string) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/jobs/${encodeURIComponent(
        jobId,
      )}/resume`,
      json('POST'),
    ),
  cancelRewriteJob: (bookId: string, jobId: string) =>
    request<RewriteJob>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/jobs/${encodeURIComponent(
        jobId,
      )}/cancel`,
      json('POST'),
    ),

  // R13 · 报告清单
  listRewriteReports: (bookId: string, kind?: 'plan' | 'outline' | 'chapter') =>
    request<ReportsResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/reports${
        kind ? `?kind=${encodeURIComponent(kind)}` : ''
      }`,
    ),

  // 附加 · 零写入自检快照（只读，P0-10 审计入口）
  getRewriteSnapshot: (bookId: string) =>
    request<RewriteSnapshotResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/snapshot`,
    ),
  /** 附加 · 免责声明单点下发（★前端不得另写一份★） */
  getRewriteDisclaimer: (bookId: string) =>
    request<RewriteDisclaimerResponse>(
      `${BASE}/books/${encodeURIComponent(bookId)}/rewrite/disclaimer`,
    ),
}

/**
 * R3 · 输入侧预检 —— **唯一不走 `request` 的调用**（已在文件头纪律里登记）。
 *
 * 原因：预检拒绝是 422，而 422 的响应体里带着 `hits`（命中规则 + ≤40 字片段）
 * 与 `sample`（结构笔记引导示例）。`request` 只把 `detail.message` 抽成
 * Error.message 后丢弃其余字段，用户就看不到「为什么被拒 / 该改成什么样」——
 * 这正是本功能最需要如实展示的信息。
 *
 * 因此这里直接取原始 JSON：422 + `rewrite_source_rejected` 视为**正常返回**
 * （ok=false，不弹 toast，由面板内联展示）；其它非 2xx 才 toast + throw。
 */
export async function novelRewritePrecheck(
  bookId: string,
  body: { fields?: Record<string, string>; blueprint?: unknown },
): Promise<PrecheckResult> {
  const res = await fetch(`${BASE}/books/${encodeURIComponent(bookId)}/rewrite/precheck`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  let payload: Record<string, unknown> = {}
  try {
    payload = (await res.json()) as Record<string, unknown>
  } catch {
    payload = {}
  }

  const detail = (payload.detail ?? {}) as Record<string, unknown>
  const hits: PrecheckHit[] = Array.isArray(detail.hits)
    ? (detail.hits as PrecheckHit[])
    : Array.isArray(payload.hits)
      ? (payload.hits as PrecheckHit[])
      : []

  if (res.status === 422 && detail.code === 'rewrite_source_rejected') {
    return {
      ok: false,
      passed: false,
      hits,
      sample: String(detail.sample ?? ''),
      honesty_note: String(detail.honesty_note ?? ''),
      message: String(detail.message ?? '输入疑似原文，已被预检拒绝（未保存）。'),
    }
  }
  if (!res.ok) {
    const message =
      String(detail.message ?? payload.message ?? '') || `${res.status} ${res.statusText}`
    toast(message, 'error')
    throw new Error(message)
  }
  return {
    ok: true,
    passed: true,
    hits: [],
    sample: String(payload.sample ?? ''),
    honesty_note: String(payload.honesty_note ?? ''),
    message: '',
  }
}

/**
 * 18. 导出 —— 返回可直接 `window.open()` 的 URL。
 * 后端返回带 Content-Disposition 的文件流，走 request 会被 JSON 解析打断。
 */
export function novelExportUrl(
  bookId: string,
  opts: { format: 'md' | 'txt'; scope: 'chapter' | 'book'; chapterId?: string },
): string {
  const params = new URLSearchParams({ format: opts.format, scope: opts.scope })
  if (opts.scope === 'chapter' && opts.chapterId) params.set('chapter_id', opts.chapterId)
  return `${BASE}/books/${encodeURIComponent(bookId)}/export?${params.toString()}`
}

/** 从 `request` 抛出的 Error 里取中文文案（后端 detail.message 已由 api.ts 提取）。 */
export function errMessage(err: unknown, fallback = '请求失败'): string {
  if (err instanceof Error && err.message) return err.message
  if (typeof err === 'string' && err) return err
  return fallback
}

/** 章节卡片的中栏展示用序号：直接用大纲 order（磁盘文件名不随排序变化）。 */
export function chapterSeq(card: Pick<ChapterCard, 'order'>): string {
  return String(card.order).padStart(2, '0')
}

/** AI 状态的简短展示串，供按钮 title / 徽标复用。 */
export function aiStatusShort(status: AiStatus | null): string {
  if (!status) return 'AI 状态未知'
  if (status.available) return `AI 就绪 · ${status.provider} / ${status.model || '未指定模型'}`
  return status.reason || 'AI 不可用'
}
