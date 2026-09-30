// 小说工作区 — 右栏：章节编辑器
//
// P0-5 要点：
//   ① 800ms 防抖自动保存（保存中 / 已保存 / 未保存（含原因）三态徽标）
//   ② 后端 500 或断网时**明示「未保存」且保留编辑器内容不清空**
//   ③ 字数实时更新
//   ④ 预览 tab 用 MarkdownLite，不支持的语法原样显示（诚实）
// 另：捕获 textarea 选区，供「AI 润色选中」使用。

import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Check, CloudOff, Loader2, Save } from 'lucide-react'

import { cn } from '@/lib/cn'
import { errMessage, novelApi } from '@/lib/novelApi'
import { QK } from '@/lib/queryKeys'
import { CHAPTER_STATUS_DOT, CHAPTER_STATUS_LABELS } from '@/lib/novelTypes'
import type { ChapterCard } from '@/lib/novelTypes'
import { MarkdownLite } from './MarkdownLite'
import { UnavailableBar } from './UnavailableBar'

export interface ChapterEditorProps {
  bookId: string | null
  chapterId: string | null
  /** 章节卡片（状态点/节拍/细纲/标题） */
  chapter: ChapterCard | null
  /** 选区变化回调，供 AI 润色使用 */
  onSelectionChange: (text: string) => void
  /** 保存成功后通知父级（刷新章节列表的字数与时间） */
  onSaved?: () => void
  /** 底部 AI 面板（由父级传入，保持「编辑器不认识 AI」的边界） */
  aiPanel?: React.ReactNode
}

type SaveState = 'idle' | 'pending' | 'saving' | 'saved' | 'error'

const AUTOSAVE_DELAY_MS = 800

function countWords(text: string): number {
  return text.replace(/\s/g, '').length
}

export function ChapterEditor({
  bookId,
  chapterId,
  chapter,
  onSelectionChange,
  onSaved,
  aiPanel,
}: ChapterEditorProps) {
  const [tab, setTab] = useState<'source' | 'preview'>('source')
  const [text, setText] = useState('')
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState('')
  const [savedAt, setSavedAt] = useState('')

  const timerRef = useRef<number | null>(null)
  const baseRef = useRef('')
  const textRef = useRef('')
  const loadedKeyRef = useRef('')
  // 当前编辑器内容属于哪一章。切章时的 flush 必须写回**旧**章节：那时 props
  // 已经是新章节了，直接读 props 会把上一章没落盘的正文写进新章（真实串章事故）。
  const ownerRef = useRef<{ bookId: string; chapterId: string } | null>(null)

  const loadKey = `${bookId ?? ''}/${chapterId ?? ''}`
  const detailQuery = useQuery({
    queryKey: QK.novelChapter(bookId ?? '', chapterId ?? ''),
    queryFn: () => novelApi.getChapter(bookId as string, chapterId as string),
    enabled: !!bookId && !!chapterId,
  })

  // 只在切章（或首次拿到数据）时把磁盘内容灌进编辑器 —— 避免自动保存回写后
  // detail refetch 把用户正在敲的内容覆盖掉。
  useEffect(() => {
    const detail = detailQuery.data
    if (!detail) return
    if (loadedKeyRef.current === loadKey) return
    loadedKeyRef.current = loadKey
    textRef.current = detail.content
    baseRef.current = detail.content
    ownerRef.current = { bookId: bookId ?? '', chapterId: chapterId ?? '' }
    setText(detail.content)
    setSaveState('saved')
    setSaveError('')
    setSavedAt('')
  }, [detailQuery.data, loadKey, bookId, chapterId])

  // 目标章节由调用方显式传入，不读 props —— 切章 flush 时 props 已翻篇。
  const runSave = useCallback(
    async (targetBookId: string, targetChapterId: string, next: string) => {
      if (!targetBookId || !targetChapterId) return
      setSaveState('saving')
      try {
        const res = await novelApi.putChapter(targetBookId, targetChapterId, next)
        baseRef.current = next
        setSaveState('saved')
        setSaveError('')
        setSavedAt(res.updated_at)
        onSaved?.()
      } catch (err) {
        // 失败不清空编辑器：内容仍在 text 里，只把状态标成「未保存（原因）」
        setSaveState('error')
        setSaveError(errMessage(err, '保存失败'))
      }
    },
    [onSaved],
  )

  const flushRef = useRef<() => Promise<void>>(async () => {})
  flushRef.current = async () => {
    const owner = ownerRef.current
    if (!owner) return
    if (textRef.current === baseRef.current) return
    await runSave(owner.bookId, owner.chapterId, textRef.current)
  }

  // 切章/卸载时把未落盘的改动补写一次，避免静默丢失
  useEffect(() => {
    return () => {
      if (timerRef.current) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
      void flushRef.current()
    }
  }, [loadKey])

  const scheduleSave = (next: string) => {
    textRef.current = next
    setText(next)
    if (timerRef.current) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (next === baseRef.current) {
      setSaveState('saved')
      return
    }
    setSaveState('pending')
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      void runSave(bookId ?? '', chapterId ?? '', textRef.current)
    }, AUTOSAVE_DELAY_MS)
  }

  const captureSelection = (el: HTMLTextAreaElement) => {
    const value = el.value.slice(el.selectionStart, el.selectionEnd)
    onSelectionChange(value.trim())
  }

  if (!bookId || !chapterId) {
    return (
      <div className="flex min-h-0 flex-1 flex-col bg-surface">
        <div className="grid flex-1 place-items-center px-6 text-center">
          <div className="text-xs leading-relaxed text-muted">
            选中一章开始写作。
            <br />
            正文是纯 Markdown 明文，落在这个书的「正文/」目录里，任何编辑器都能打开。
          </div>
        </div>
      </div>
    )
  }

  const gateMissing = !chapter?.beat?.trim() && !chapter?.summary?.trim()
  const wordCount = countWords(text)

  return (
    <div className="flex min-h-0 flex-1 flex-col bg-surface">
      {/* 头部：章序号 + 标题 + 状态点 + 保存徽标 */}
      <div className="shrink-0 border-b border-border px-4 py-2">
        <div className="flex items-center gap-2">
          <span
            className="shrink-0 text-[12px] leading-none"
            style={{ color: '#22c55e' }}
            aria-hidden="true"
          >
            {CHAPTER_STATUS_DOT[chapter?.status ?? 'draft']}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-foreground">
            {String(chapter?.order ?? 1).padStart(2, '0')} {chapter?.title ?? '未命名章节'}
            <span className="ml-2 text-[11px] font-normal text-muted">
              {CHAPTER_STATUS_LABELS[chapter?.status ?? 'draft']}
            </span>
          </span>
          <SaveBadge
            state={saveState}
            error={saveError}
            at={savedAt}
            onRetry={() => void runSave(bookId ?? '', chapterId ?? '', textRef.current)}
          />
        </div>

        {/* 节拍 / 细纲 只读展示 —— 让「按大纲写作」在视觉上成立 */}
        <div className="mt-1 space-y-0.5 text-[11px] leading-relaxed">
          <div className="truncate text-secondary">
            本节节拍：{chapter?.beat?.trim() || '（未填写）'}
          </div>
          <div className="truncate text-muted">
            大纲细纲：{chapter?.summary?.trim() || '（未填写）'}
          </div>
        </div>

        {gateMissing && (
          <div className="mt-1.5">
            <UnavailableBar
              code="generic"
              message="本章细纲为空，AI 续写已禁用（填写细纲后启用）"
            />
          </div>
        )}
        {saveState === 'error' && (
          <div className="mt-1.5">
            <UnavailableBar code="write_failed" message={`保存失败：${saveError}。你的改动仍在编辑器中，未落盘。`} />
          </div>
        )}
      </div>

      {/* 源码 / 预览 */}
      <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-1.5">
        <button
          type="button"
          onClick={() => setTab('source')}
          className={cn(
            'rounded-btn px-2 py-0.5 text-[11px] transition-colors',
            tab === 'source' ? 'bg-elevated text-foreground' : 'text-muted hover:text-foreground',
          )}
        >
          源码
        </button>
        <button
          type="button"
          onClick={() => setTab('preview')}
          className={cn(
            'rounded-btn px-2 py-0.5 text-[11px] transition-colors',
            tab === 'preview' ? 'bg-elevated text-foreground' : 'text-muted hover:text-foreground',
          )}
        >
          预览
        </button>
        <span className="ml-auto font-mono text-[11px] text-muted">
          {wordCount.toLocaleString('zh-CN')} 字
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {detailQuery.isLoading && !detailQuery.data ? (
          <div className="grid h-full place-items-center text-xs text-muted">加载中…</div>
        ) : tab === 'source' ? (
          <textarea
            value={text}
            onChange={(e) => scheduleSave(e.target.value)}
            onMouseUp={(e) => captureSelection(e.currentTarget)}
            onKeyUp={(e) => captureSelection(e.currentTarget)}
            onSelect={(e) => captureSelection(e.currentTarget)}
            spellCheck={false}
            placeholder="在这里写正文（Markdown）。输入后 800ms 自动落盘。"
            className="h-full w-full resize-none bg-transparent px-4 py-3 font-mono text-[13px] leading-7 text-foreground outline-none placeholder:text-muted"
          />
        ) : (
          <div className="px-4 py-3">
            <MarkdownLite source={text} className="text-[13px]" />
          </div>
        )}
      </div>

      {/* 底部 AI 面板（可折叠，默认展开） */}
      {aiPanel ? <div className="max-h-[46%] shrink-0 overflow-y-auto border-t border-border">{aiPanel}</div> : null}
    </div>
  )
}

function SaveBadge({
  state,
  error,
  at,
  onRetry,
}: {
  state: SaveState
  error: string
  at: string
  onRetry: () => void
}) {
  if (state === 'saving') {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] text-muted">
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        保存中
      </span>
    )
  }
  if (state === 'error') {
    return (
      <button
        type="button"
        onClick={onRetry}
        title={error}
        className="inline-flex shrink-0 items-center gap-1 rounded-full border border-danger/50 bg-danger/10 px-2 py-0.5 text-[10px] text-danger"
      >
        <CloudOff className="h-3 w-3" aria-hidden="true" />
        未保存（点此重试）
      </button>
    )
  }
  if (state === 'pending') {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] text-muted">
        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
        未保存
      </span>
    )
  }
  if (state === 'saved') {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] text-muted">
        <Check className="h-3 w-3" aria-hidden="true" />
        已保存{at ? ` · ${at.slice(11, 16)}` : ''}
      </span>
    )
  }
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] text-muted">
      <Save className="h-3 w-3" aria-hidden="true" />
      待编辑
    </span>
  )
}
