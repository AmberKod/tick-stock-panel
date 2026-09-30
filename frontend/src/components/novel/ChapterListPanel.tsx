// 小说工作区 — 中栏：章节卡片列表 + 导出区 + 派生视图只读抽屉
//
// 状态点图例：○ 仅大纲 / ◐ AI草稿待采纳 / ● 正式（PRD §7.2）。
// 有 AI 草稿的章节额外显示次级条「AI草稿待采纳 · N字 · X分钟前」。
// 导出走 window.open（后端返回带 Content-Disposition 的文件流，不是 JSON）。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, FileText, RefreshCw, Sparkles, X } from 'lucide-react'

import { cn } from '@/lib/cn'
import { novelApi, novelExportUrl } from '@/lib/novelApi'
import { QK } from '@/lib/queryKeys'
import { CHAPTER_STATUS_DOT, CHAPTER_STATUS_LABELS, VIEW_LABELS } from '@/lib/novelTypes'
import type { ChapterCard, ViewName } from '@/lib/novelTypes'
import { MarkdownLite } from './MarkdownLite'

export interface ChapterListPanelProps {
  bookId: string | null
  currentChapterId: string | null
  onSelectChapter: (chapterId: string) => void
}

function fmtAgo(iso: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const mins = Math.floor((Date.now() - date.getTime()) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

function fmtTime(iso: string): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function ChapterListPanel({
  bookId,
  currentChapterId,
  onSelectChapter,
}: ChapterListPanelProps) {
  const qc = useQueryClient()
  const [volumeFilter, setVolumeFilter] = useState<string>('')
  const [viewOpen, setViewOpen] = useState<ViewName | null>(null)
  const [viewRaw, setViewRaw] = useState(false)

  const chaptersQuery = useQuery({
    queryKey: QK.novelChapters(bookId ?? ''),
    // 与工作区共用同一个 key/请求（全量拉取），卷过滤在前端做 —— 避免两个
    // 不同 queryFn 抢同一个缓存条目。
    queryFn: () => novelApi.listChapters(bookId as string),
    enabled: !!bookId,
  })

  const viewQuery = useQuery({
    queryKey: QK.novelView(bookId ?? '', viewOpen ?? ''),
    queryFn: () => novelApi.getView(bookId as string, viewOpen as ViewName),
    enabled: !!bookId && !!viewOpen,
  })

  const rebuild = useMutation({
    mutationFn: () => novelApi.rebuildViews(bookId as string),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.novelView(bookId ?? '', viewOpen ?? '') })
    },
  })

  const allChapters: ChapterCard[] = chaptersQuery.data?.chapters ?? []
  const chapters = volumeFilter
    ? allChapters.filter((item) => item.volume_id === volumeFilter)
    : allChapters
  const volumes = Array.from(
    chapters.reduce((map, item) => {
      map.set(item.volume_id, item.volume_title)
      return map
    }, new Map<string, string>()),
  )

  const openExport = (format: 'md' | 'txt', scope: 'chapter' | 'book') => {
    if (!bookId) return
    const url = novelExportUrl(bookId, {
      format,
      scope,
      chapterId: scope === 'chapter' ? (currentChapterId ?? undefined) : undefined,
    })
    window.open(url, '_blank', 'noopener')
  }

  return (
    <div className="flex h-full w-72 shrink-0 flex-col border-r border-border bg-base">
      {/* 卷过滤 */}
      <div className="flex items-center gap-1 border-b border-border px-3 py-2">
        <span className="text-xs font-semibold text-secondary">章节</span>
        <select
          value={volumeFilter}
          onChange={(e) => setVolumeFilter(e.target.value)}
          className="ml-auto max-w-[8.5rem] rounded-input border border-border bg-surface px-1.5 py-0.5 text-[11px] text-secondary outline-none focus:border-accent/60"
          aria-label="按卷过滤"
        >
          <option value="">全部</option>
          {volumes.map(([id, title]) => (
            <option key={id} value={id}>
              {title}
            </option>
          ))}
        </select>
      </div>

      {/* 章节卡列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {!bookId ? (
          <div className="rounded-card border border-dashed border-border px-3 py-4 text-[11px] leading-relaxed text-muted">
            还没有选书 — 先在左栏新建或选一本书
          </div>
        ) : chapters.length === 0 ? (
          <div className="rounded-card border border-dashed border-border px-3 py-4 text-[11px] leading-relaxed text-muted">
            这本书还没有章节 — 在左栏大纲里点「+章」
          </div>
        ) : (
          chapters.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onSelectChapter(item.id)}
              className={cn(
                'mb-1.5 block w-full rounded-card border px-2.5 py-2 text-left transition-colors',
                item.id === currentChapterId
                  ? 'border-accent/40 bg-elevated'
                  : 'border-border/70 bg-surface/60 hover:border-accent/30 hover:bg-elevated/60',
              )}
            >
              <div className="flex items-center gap-1.5">
                <span
                  className="shrink-0 text-[11px] leading-none"
                  style={{ color: '#22c55e' }}
                  aria-hidden="true"
                >
                  {CHAPTER_STATUS_DOT[item.status]}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                  {String(item.order).padStart(2, '0')} {item.title}
                </span>
                <span className="shrink-0 text-[10px] text-muted">
                  {item.word_count.toLocaleString('zh-CN')}字
                </span>
              </div>
              <div className="mt-0.5 flex items-center gap-2 pl-4 text-[10px] text-muted">
                <span>{CHAPTER_STATUS_LABELS[item.status]}</span>
                <span>{fmtTime(item.updated_at)}</span>
                {item.word_target > 0 && <span>目标 {item.word_target}字</span>}
              </div>
              {item.draft_count > 0 && (
                <div
                  className="mt-1 flex items-center gap-1 rounded-btn bg-elevated px-1.5 py-0.5 text-[10px] text-secondary"
                  title="AI 草稿未采纳 — 不影响追踪态"
                >
                  <Sparkles className="h-2.5 w-2.5" style={{ color: '#22c55e' }} aria-hidden="true" />
                  AI草稿待采纳 · {item.draft_count} 份
                  {item.last_draft_at ? ` · ${fmtAgo(item.last_draft_at)}` : ''}
                </div>
              )}
            </button>
          ))
        )}
      </div>

      {/* 导出区 */}
      <div className="shrink-0 border-t border-border px-3 py-2">
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted/80">
          导出
        </div>
        <div className="flex flex-wrap gap-1">
          <button
            type="button"
            disabled={!bookId || !currentChapterId}
            onClick={() => openExport('md', 'chapter')}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            <FileText className="h-3 w-3" aria-hidden="true" />
            单章 .md
          </button>
          <button
            type="button"
            disabled={!bookId || !currentChapterId}
            onClick={() => openExport('txt', 'chapter')}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            <FileText className="h-3 w-3" aria-hidden="true" />
            单章 .txt
          </button>
          <button
            type="button"
            disabled={!bookId}
            onClick={() => openExport('md', 'book')}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            <Download className="h-3 w-3" aria-hidden="true" />
            全书 .md
          </button>
          <button
            type="button"
            disabled={!bookId}
            onClick={() => openExport('txt', 'book')}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            <Download className="h-3 w-3" aria-hidden="true" />
            全书 .txt
          </button>
        </div>
        <button
          type="button"
          disabled={!bookId}
          onClick={() => {
            setViewRaw(false)
            setViewOpen('context-card')
          }}
          className="mt-2 w-full rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
        >
          查看续写上下文卡
        </button>
      </div>

      {/* 派生视图只读抽屉 */}
      {viewOpen && (
        <div className="fixed inset-0 z-40 flex justify-end">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setViewOpen(null)}
            aria-hidden="true"
          />
          <div className="relative flex h-full w-[92vw] max-w-xl flex-col border-l border-border bg-surface shadow-xl">
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <span className="text-xs font-semibold text-foreground">
                {VIEW_LABELS[viewOpen]}
              </span>
              <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-muted">
                派生视图 · 请勿手工编辑
              </span>
              <div className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => setViewRaw((v) => !v)}
                  className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground"
                >
                  {viewRaw ? '看渲染' : '看源文件'}
                </button>
                <button
                  type="button"
                  disabled={rebuild.isPending}
                  onClick={() => rebuild.mutate()}
                  className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
                  title="由权威 JSON 重新生成（幂等）"
                >
                  <RefreshCw className={cn('h-3 w-3', rebuild.isPending && 'animate-spin')} />
                  重新生成
                </button>
                <button
                  type="button"
                  onClick={() => setViewOpen(null)}
                  className="rounded-btn p-1 text-muted hover:text-foreground"
                  aria-label="关闭"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              {viewQuery.isLoading ? (
                <div className="text-xs text-muted">加载中…</div>
              ) : viewQuery.error ? (
                <div className="text-xs text-danger">
                  视图读取失败：{viewQuery.error.message}
                </div>
              ) : viewRaw ? (
                <pre className="whitespace-pre-wrap font-mono text-[11px] leading-6 text-secondary">
                  {viewQuery.data?.content ?? ''}
                </pre>
              ) : (
                <MarkdownLite
                  source={viewQuery.data?.content ?? ''}
                  className="text-sm leading-7"
                />
              )}
            </div>
            {viewQuery.data?.generated_at && (
              <div className="shrink-0 border-t border-border px-3 py-1.5 text-[10px] text-muted">
                生成于 {viewQuery.data.generated_at}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
