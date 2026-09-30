// 小说工作区 — API 客户端（21 个端点的一层薄封装）
//
// 请求约定全部复用 `@/lib/api` 的 `request`（同一套 fetch / toast / 错误处理），
// 不自建第二套。导出接口见 ARCHITECTURE.md §3.6。
//
// 两条纪律：
//   1. 自动保存类调用传 `quiet: true` —— 由 ChapterEditor 自行展示「未保存（原因）」
//      徽标，避免 800ms 防抖把 toast 刷屏。
//   2. 导出走 `window.open(exportUrl(...))`，不经过 `request`（后端返回的是
//      带 Content-Disposition 的文件流，不是 JSON）。

import { request } from '@/lib/api'
import type {
  AdoptRequest,
  AdoptResponse,
  AiDraftRequest,
  AiStatus,
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
  PutChapterResult,
  RebuildViewsResult,
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
