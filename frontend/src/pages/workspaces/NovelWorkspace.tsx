// 小说工作区 (/novel) — 三栏容器 + 状态编排
//
// 状态编排全部提升到这一层（不引全局状态库）：
//   currentBookId / currentChapterId / selection / activeJobId / 左栏折叠 / 移动端分段。
// 服务端数据一律走 React Query（QK.novel*），权威数据在磁盘，刷新即重建。
//
// 布局纪律（PRD §7.1）：容器 `min-h-0 flex-1 overflow-hidden`，**每栏内部独立滚动**，
// 不出现整页滚动条。<768px 顶部三段式分段控件，同一时刻只渲染一栏。
//
// 不改 WorkspaceShell.tsx 的 WORKSPACES 与 props 契约 —— 本页只是 /novel 路由子树。

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Plus } from 'lucide-react'

import { errMessage, novelApi } from '@/lib/novelApi'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { AiDraftPanel } from '@/components/novel/AiDraftPanel'
import { BookshelfOutlinePanel } from '@/components/novel/BookshelfOutlinePanel'
import { ChapterEditor } from '@/components/novel/ChapterEditor'
import { ChapterListPanel } from '@/components/novel/ChapterListPanel'
import type { ChapterCard } from '@/lib/novelTypes'

type MobileTab = 'books' | 'chapters' | 'editor'

const MOBILE_TABS: { key: MobileTab; label: string }[] = [
  { key: 'books', label: '书架' },
  { key: 'chapters', label: '章节' },
  { key: 'editor', label: '编辑' },
]

export function NovelWorkspace() {
  const qc = useQueryClient()
  const [bookId, setBookId] = useState<string | null>(null)
  const [chapterId, setChapterId] = useState<string | null>(null)
  const [selection, setSelection] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [isMobile, setIsMobile] = useState(false)
  const [mobileTab, setMobileTab] = useState<MobileTab>('books')

  // 移动端判定：订阅 matchMedia，不引第三方依赖
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const query = window.matchMedia('(max-width: 767px)')
    const apply = () => setIsMobile(query.matches)
    apply()
    query.addEventListener('change', apply)
    return () => query.removeEventListener('change', apply)
  }, [])

  const statusQuery = useQuery({ queryKey: QK.novelStatus, queryFn: () => novelApi.status() })
  const booksQuery = useQuery({ queryKey: QK.novelBooks, queryFn: () => novelApi.listBooks() })
  // 大纲树只在左栏用（QK.novelOutline 由 BookshelfOutlinePanel 自己持有），
  // 父级不再预取 —— 少一份带乐观锁 version 的缓存副本，就少一处 409 来源。
  const stateQuery = useQuery({
    queryKey: QK.novelState(bookId ?? ''),
    queryFn: () => novelApi.getState(bookId as string),
    enabled: !!bookId,
  })
  const chaptersQuery = useQuery({
    queryKey: QK.novelChapters(bookId ?? ''),
    queryFn: () => novelApi.listChapters(bookId as string),
    enabled: !!bookId,
  })

  const books = booksQuery.data?.books ?? []
  const dataDirAbs = statusQuery.data?.data_dir_abs ?? booksQuery.data?.data_dir_abs ?? ''

  // 首次进入自动选中第一本书（空态另有展示，不偷偷造数据）
  useEffect(() => {
    if (!bookId && books.length > 0) setBookId(books[0].id)
  }, [bookId, books])

  // 切书 → 清空章节/选区/任务；切章 → 清空选区与任务
  useEffect(() => {
    setChapterId(null)
    setSelection('')
    setJobId(null)
  }, [bookId])

  useEffect(() => {
    setSelection('')
    setJobId(null)
  }, [chapterId])

  const chapters: ChapterCard[] = chaptersQuery.data?.chapters ?? []
  const chapter = useMemo(
    () => chapters.find((item) => item.id === chapterId) ?? null,
    [chapters, chapterId],
  )

  const stats = useMemo(() => {
    const state = stateQuery.data
    if (!state) return { characters: 0, foreshadow: 0, open: 0 }
    return {
      characters: Object.keys(state.characters ?? {}).length,
      foreshadow: state.foreshadow?.length ?? 0,
      open: (state.foreshadow ?? []).filter((item) => item.status === 'open').length,
    }
  }, [stateQuery.data])

  // 设定摘要由左栏自己拉 `GET /books/{id}/meta`（book.json 的 genre/pov/tense/
  // setting_summary），父级不再中转 —— meta 与大纲树生命周期不同，混在一起会让
  // 大纲的乐观锁 version 与设定互相污染缓存。
  const refreshAfterSave = () => {
    if (!bookId) return
    qc.invalidateQueries({ queryKey: QK.novelChapters(bookId) })
  }

  const emptyState = books.length === 0 && !booksQuery.isLoading

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-base">
      {/* 移动端分段控件 */}
      {isMobile && (
        <div className="flex shrink-0 gap-1 border-b border-border bg-surface px-2 py-1.5">
          {MOBILE_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setMobileTab(tab.key)}
              className={cn(
                'flex-1 rounded-btn px-2 py-1 text-xs transition-colors',
                mobileTab === tab.key
                  ? 'bg-elevated text-foreground'
                  : 'text-muted hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* ① 左栏：书架 + 大纲 */}
        {(!isMobile || mobileTab === 'books') && (
          <BookshelfOutlinePanel
            bookId={bookId}
            currentChapterId={chapterId}
            dataDirAbs={dataDirAbs}
            stats={stats}
            collapsed={!isMobile && leftCollapsed}
            onToggleCollapse={() => setLeftCollapsed((v) => !v)}
            onSelectBook={(id) => {
              setBookId(id || null)
              if (isMobile) setMobileTab('chapters')
            }}
            onSelectChapter={(id) => {
              setChapterId(id)
              if (isMobile) setMobileTab('editor')
            }}
            onStructureChanged={() => {
              if (!bookId) return
              qc.invalidateQueries({ queryKey: QK.novelChapters(bookId) })
              qc.invalidateQueries({ queryKey: QK.novelState(bookId) })
              qc.invalidateQueries({ queryKey: QK.novelOutline(bookId) })
            }}
          />
        )}

        {/* ② 中栏：章节列表 + 导出 + 视图抽屉 */}
        {(!isMobile || mobileTab === 'chapters') && (
          <ChapterListPanel
            bookId={bookId}
            currentChapterId={chapterId}
            onSelectChapter={(id) => {
              setChapterId(id)
              if (isMobile) setMobileTab('editor')
            }}
          />
        )}

        {/* ③ 右栏：编辑器 + 底部 AI 面板 */}
        {(!isMobile || mobileTab === 'editor') && (
          <div className="flex min-w-0 flex-1 flex-col">
            {emptyState ? (
              <EmptyBookshelf dataDirAbs={dataDirAbs} />
            ) : (
              <ChapterEditor
                bookId={bookId}
                chapterId={chapterId}
                chapter={chapter}
                onSelectionChange={setSelection}
                onSaved={refreshAfterSave}
                aiPanel={
                  <AiDraftPanel
                    bookId={bookId}
                    chapterId={chapterId}
                    chapter={chapter}
                    aiStatus={statusQuery.data ?? null}
                    selection={selection}
                    jobId={jobId}
                    onJobCreated={setJobId}
                    onAdopted={() => {
                      if (!bookId) return
                      qc.invalidateQueries({ queryKey: QK.novelChapters(bookId) })
                      qc.invalidateQueries({ queryKey: QK.novelState(bookId) })
                      qc.invalidateQueries({ queryKey: QK.novelOutline(bookId) })
                      qc.invalidateQueries({ queryKey: QK.novelBooks })
                    }}
                  />
                }
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/** 空态（PRD §7.3）：如实展示数据将落在哪个绝对路径 —— 兑现「本地优先」承诺。 */
function EmptyBookshelf({ dataDirAbs }: { dataDirAbs: string }) {
  const qc = useQueryClient()
  const [title, setTitle] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  return (
    <div className="grid min-h-0 flex-1 place-items-center bg-surface px-6">
      <div className="w-full max-w-md rounded-card border border-border bg-base p-5 text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-[#22c55e]/10">
          <BookOpen className="h-6 w-6" style={{ color: '#22c55e' }} aria-hidden="true" />
        </div>
        <h2 className="mt-3 text-base font-semibold text-foreground">还没有书</h2>
        <p className="mt-1 text-xs leading-relaxed text-secondary">
          新建后会在下面这个目录里生成 <code className="font-mono">book.json</code>、
          <code className="font-mono">正文/</code>、<code className="font-mono">state.json</code>、
          <code className="font-mono">views/</code> —— 全是明文，任何编辑器都能打开。
        </p>
        <div className="mt-2 break-all rounded-btn border border-dashed border-border px-2 py-1.5 text-[11px] text-muted">
          {dataDirAbs || '（数据目录未知）'}
        </div>
        <form
          className="mt-3 flex gap-1.5"
          onSubmit={async (e) => {
            e.preventDefault()
            if (!title.trim() || pending) return
            setPending(true)
            setError('')
            try {
              await novelApi.createBook(title.trim())
              setTitle('')
              qc.invalidateQueries({ queryKey: QK.novelBooks })
            } catch (err) {
              setError(errMessage(err, '新建失败'))
            } finally {
              setPending(false)
            }
          }}
        >
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="第一本书的名字"
            className="min-w-0 flex-1 rounded-input border border-border bg-surface px-2 py-1.5 text-xs outline-none placeholder:text-muted focus:border-accent/60"
          />
          <button
            type="submit"
            disabled={pending || !title.trim()}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-3 py-1.5 text-xs text-foreground transition-colors hover:border-accent/40 disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {pending ? '新建中…' : '新建第一本书'}
          </button>
        </form>
        {error && <div className="mt-2 text-[11px] text-danger">新建失败：{error}</div>}
      </div>
    </div>
  )
}
