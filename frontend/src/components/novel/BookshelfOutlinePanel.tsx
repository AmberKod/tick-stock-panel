// 小说工作区 — 左栏：书架 + 卷/章两级大纲树 + 设定摘要卡
//
// 大纲的增删改排序**全部在前端组装后一次性 PUT /outline（带 version 乐观锁）**：
// 后端只做全量覆盖，不做增量接口。冲突（别人改过）会返回 409 version_conflict，
// 这里诚实提示并重新拉取，不静默覆盖。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowDown,
  ArrowUp,
  BookOpen,
  ChevronDown,
  ChevronRight,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Trash2,
  X,
} from 'lucide-react'

import { cn } from '@/lib/cn'
import { errMessage, novelApi } from '@/lib/novelApi'
import { QK } from '@/lib/queryKeys'
import { CHAPTER_STATUS_DOT, CHAPTER_STATUS_LABELS } from '@/lib/novelTypes'
import type { BookListItem, OutlineChapter, OutlineVolume } from '@/lib/novelTypes'
import { UnavailableBar } from './UnavailableBar'

export interface BookshelfOutlinePanelProps {
  bookId: string | null
  currentChapterId: string | null
  dataDirAbs: string
  /** 来自 BookState 的只读统计（父级已取，不重复请求） */
  stats: { characters: number; foreshadow: number; open: number }
  collapsed: boolean
  onToggleCollapse: () => void
  onSelectBook: (bookId: string) => void
  onSelectChapter: (chapterId: string) => void
  /** 书籍/大纲结构发生变化（父级据此失效章节列表与追踪态） */
  onStructureChanged: () => void
}

function newId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`
}

function clone(nodes: OutlineVolume[]): OutlineVolume[] {
  return JSON.parse(JSON.stringify(nodes)) as OutlineVolume[]
}

/** 重排 order，保证与数组顺序一致（后端按 order 排序渲染） */
function renumber(nodes: OutlineVolume[]): OutlineVolume[] {
  nodes.forEach((volume, vi) => {
    volume.order = vi + 1
    volume.children.forEach((chapter, ci) => {
      chapter.order = ci + 1
    })
  })
  return nodes
}

export function BookshelfOutlinePanel({
  bookId,
  currentChapterId,
  dataDirAbs,
  stats,
  collapsed,
  onToggleCollapse,
  onSelectBook,
  onSelectChapter,
  onStructureChanged,
}: BookshelfOutlinePanelProps) {
  const qc = useQueryClient()
  const [creatingBook, setCreatingBook] = useState(false)
  const [newBookTitle, setNewBookTitle] = useState('')
  const [renamingBook, setRenamingBook] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [editingChapter, setEditingChapter] = useState<string | null>(null)
  const [outlineError, setOutlineError] = useState('')
  const [editingSetting, setEditingSetting] = useState(false)
  const [settingDraft, setSettingDraft] = useState({
    genre: '',
    pov: '',
    tense: '',
    setting_summary: '',
  })
  const [metaError, setMetaError] = useState('')

  const booksQuery = useQuery({
    queryKey: QK.novelBooks,
    queryFn: () => novelApi.listBooks(),
  })
  const outlineQuery = useQuery({
    queryKey: QK.novelOutline(bookId ?? ''),
    queryFn: () => novelApi.getOutline(bookId as string),
    enabled: !!bookId,
  })
  // 设定摘要来自 book.json，走独立的 meta 端点（不含大纲树，避免为读设定拉整棵树）
  const metaQuery = useQuery({
    queryKey: QK.novelBookMeta(bookId ?? ''),
    queryFn: () => novelApi.getBookMeta(bookId as string),
    enabled: !!bookId,
  })

  const books: BookListItem[] = booksQuery.data?.books ?? []
  const outline = outlineQuery.data
  const nodes = outline?.nodes ?? []

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: QK.novelBooks })
    if (bookId) {
      qc.invalidateQueries({ queryKey: QK.novelOutline(bookId) })
      qc.invalidateQueries({ queryKey: QK.novelChapters(bookId) })
      qc.invalidateQueries({ queryKey: QK.novelState(bookId) })
    }
    onStructureChanged()
  }

  const saveOutline = useMutation({
    mutationFn: (next: OutlineVolume[]) =>
      novelApi.putOutline(bookId as string, outline?.version ?? 0, renumber(next)),
    onSuccess: () => {
      setOutlineError('')
      invalidateAll()
    },
    onError: (err: unknown) => {
      // 409 version_conflict：后端已给出中文原因，这里不静默覆盖
      setOutlineError(errMessage(err, '大纲保存失败'))
      qc.invalidateQueries({ queryKey: QK.novelOutline(bookId ?? '') })
    },
  })

  const updateMeta = useMutation({
    // 只传设定四个字段 —— title/author 不在本卡片里改，后端对未传字段保持原值
    mutationFn: () =>
      novelApi.updateBookMeta(bookId as string, {
        genre: settingDraft.genre,
        pov: settingDraft.pov,
        tense: settingDraft.tense,
        setting_summary: settingDraft.setting_summary,
      }),
    onSuccess: (meta) => {
      setMetaError('')
      setEditingSetting(false)
      // 后端回的是完整 meta，直接写回缓存；书架列表的 updated_at 也要刷新
      qc.setQueryData(QK.novelBookMeta(bookId as string), meta)
      qc.invalidateQueries({ queryKey: QK.novelBooks })
    },
    onError: (err: unknown) => setMetaError(errMessage(err, '设定保存失败')),
  })

  const createBook = useMutation({
    mutationFn: (title: string) => novelApi.createBook(title),
    onSuccess: (book) => {
      setCreatingBook(false)
      setNewBookTitle('')
      invalidateAll()
      onSelectBook(book.id)
    },
  })

  const renameBook = useMutation({
    // 改名与设定摘要走同一个端点：只传 title，其余字段后端保持原值
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      novelApi.updateBookMeta(id, { title }),
    onSuccess: () => {
      setRenamingBook(null)
      invalidateAll()
    },
  })

  const deleteBook = useMutation({
    mutationFn: (id: string) => novelApi.deleteBook(id),
    onSuccess: () => {
      invalidateAll()
      onSelectBook('')
    },
  })

  const addVolume = () => {
    const next = clone(nodes)
    next.push({ id: newId('v'), type: 'volume', title: `第${next.length + 1}卷`, order: next.length + 1, children: [] })
    saveOutline.mutate(next)
  }

  const addChapter = (volumeIndex: number) => {
    const next = clone(nodes)
    const volume = next[volumeIndex]
    if (!volume) return
    volume.children.push({
      id: newId('ch'),
      type: 'chapter',
      title: `第${volume.children.length + 1}章`,
      order: volume.children.length + 1,
      // file 留空 → 后端在创建时一次性定名（A3：此后永不随标题/排序变化）
      file: '',
      status: 'draft',
      word_target: 0,
      summary: '',
      beat: '',
      word_count: 0,
    })
    setExpanded((prev) => ({ ...prev, [volume.id]: true }))
    saveOutline.mutate(next)
  }

  const moveVolume = (index: number, delta: number) => {
    const target = index + delta
    if (target < 0 || target >= nodes.length) return
    const next = clone(nodes)
    const [item] = next.splice(index, 1)
    next.splice(target, 0, item)
    saveOutline.mutate(next)
  }

  const moveChapter = (volumeIndex: number, chapterIndex: number, delta: number) => {
    const next = clone(nodes)
    const volume = next[volumeIndex]
    if (!volume) return
    const target = chapterIndex + delta
    if (target < 0 || target >= volume.children.length) return
    const [item] = volume.children.splice(chapterIndex, 1)
    volume.children.splice(target, 0, item)
    saveOutline.mutate(next)
  }

  const removeVolume = (index: number) => {
    const volume = nodes[index]
    if (!volume) return
    // P0-3②：删除卷时子章节一并删除 —— 二次确认并如实说明数量
    const ok = window.confirm(
      `删除「${volume.title}」将同时删除其下 ${volume.children.length} 个章节及对应 Markdown 文件，且不可恢复。确定删除？`,
    )
    if (!ok) return
    const next = clone(nodes)
    next.splice(index, 1)
    saveOutline.mutate(next)
  }

  const removeChapter = (volumeIndex: number, chapterIndex: number) => {
    const chapter = nodes[volumeIndex]?.children[chapterIndex]
    if (!chapter) return
    const ok = window.confirm(`删除章节「${chapter.title}」及其 Markdown 文件？此操作不可恢复。`)
    if (!ok) return
    const next = clone(nodes)
    next[volumeIndex].children.splice(chapterIndex, 1)
    saveOutline.mutate(next)
  }

  const patchChapter = (chapterId: string, patch: Partial<OutlineChapter>) => {
    const next = clone(nodes)
    for (const volume of next) {
      const index = volume.children.findIndex((c) => c.id === chapterId)
      if (index >= 0) {
        volume.children[index] = { ...volume.children[index], ...patch }
        break
      }
    }
    saveOutline.mutate(next)
  }

  if (collapsed) {
    return (
      <div className="flex h-full w-12 shrink-0 flex-col items-center gap-2 border-r border-border bg-surface py-2">
        <button
          type="button"
          onClick={onToggleCollapse}
          className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
          title="展开书架与大纲"
          aria-label="展开书架与大纲"
        >
          <PanelLeftOpen className="h-4 w-4" />
        </button>
        <button
          type="button"
          onClick={() => setCreatingBook(true)}
          className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
          title="新建书籍"
          aria-label="新建书籍"
        >
          <BookOpen className="h-4 w-4" />
        </button>
        {books.slice(0, 8).map((book) => (
          <button
            key={book.id}
            type="button"
            onClick={() => onSelectBook(book.id)}
            title={book.title}
            className={cn(
              'flex h-8 w-8 items-center justify-center rounded-btn text-[11px] font-semibold transition-colors',
              book.id === bookId
                ? 'bg-elevated text-foreground'
                : 'text-muted hover:bg-elevated hover:text-foreground',
            )}
          >
            {book.title.slice(0, 1)}
          </button>
        ))}
      </div>
    )
  }

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r border-border bg-surface">
      {/* ① 书架 */}
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-xs font-semibold text-secondary">书籍</span>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            onClick={() => setCreatingBook((v) => !v)}
            className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
            title="新建书籍"
            aria-label="新建书籍"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={onToggleCollapse}
            className="rounded-btn p-1 text-muted transition-colors hover:bg-elevated hover:text-foreground"
            title="折叠书架"
            aria-label="折叠书架"
          >
            <PanelLeftClose className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {creatingBook && (
        <div className="px-3 pb-2">
          <form
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              if (newBookTitle.trim()) createBook.mutate(newBookTitle.trim())
            }}
          >
            <input
              autoFocus
              value={newBookTitle}
              onChange={(e) => setNewBookTitle(e.target.value)}
              placeholder="书名"
              className="min-w-0 flex-1 rounded-input border border-border bg-base px-2 py-1 text-xs text-foreground outline-none placeholder:text-muted focus:border-accent/60"
            />
            <button
              type="submit"
              disabled={createBook.isPending}
              className="rounded-btn border border-border bg-elevated px-2 text-xs text-secondary transition-colors hover:text-foreground disabled:opacity-50"
            >
              新建
            </button>
          </form>
        </div>
      )}

      <div className="max-h-40 shrink-0 overflow-y-auto px-2 pb-2">
        {books.length === 0 ? (
          <div className="rounded-btn border border-dashed border-border px-2 py-2 text-[11px] leading-relaxed text-muted">
            还没有书 — 点上方「＋」新建第一本
          </div>
        ) : (
          books.map((book) => (
            <div
              key={book.id}
              className={cn(
                'group flex items-center gap-1 rounded-btn px-2 py-1.5 text-xs transition-colors',
                book.id === bookId
                  ? 'bg-elevated text-foreground'
                  : 'text-secondary hover:bg-elevated/60 hover:text-foreground',
              )}
            >
              {book.id === bookId && (
                <span className="h-4 w-0.5 shrink-0 rounded bg-[#22c55e]" aria-hidden="true" />
              )}
              {renamingBook === book.id ? (
                <form
                  className="flex min-w-0 flex-1 gap-1"
                  onSubmit={(e) => {
                    e.preventDefault()
                    if (renameValue.trim()) {
                      renameBook.mutate({ id: book.id, title: renameValue.trim() })
                    }
                  }}
                >
                  <input
                    autoFocus
                    value={renameValue}
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setRenamingBook(null)
                    }}
                    className="min-w-0 flex-1 rounded-input border border-border bg-base px-1.5 py-0.5 text-[11px] outline-none focus:border-accent/60"
                  />
                </form>
              ) : (
                <button
                  type="button"
                  className="min-w-0 flex-1 truncate text-left"
                  onClick={() => onSelectBook(book.id)}
                  title={book.title}
                >
                  {book.title}
                  <span className="ml-1 text-[10px] text-muted">
                    {book.chapter_count}章/{book.word_count.toLocaleString('zh-CN')}字
                  </span>
                </button>
              )}
              <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                <button
                  type="button"
                  onClick={() => {
                    setRenamingBook(book.id)
                    setRenameValue(book.title)
                  }}
                  className="rounded p-0.5 text-muted hover:text-foreground"
                  title="重命名"
                  aria-label="重命名书籍"
                >
                  <Pencil className="h-3 w-3" />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (window.confirm(`删除《${book.title}》及其全部章节文件？此操作不可恢复。`)) {
                      deleteBook.mutate(book.id)
                    }
                  }}
                  className="rounded p-0.5 text-muted hover:text-danger"
                  title="删除书籍"
                  aria-label="删除书籍"
                >
                  <Trash2 className="h-3 w-3" />
                </button>
              </span>
            </div>
          ))
        )}
      </div>

      {/* ② 大纲树 */}
      <div className="flex items-center justify-between border-t border-border px-3 py-2">
        <span className="text-xs font-semibold text-secondary">大纲</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={!bookId || saveOutline.isPending}
            onClick={addVolume}
            className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            +卷
          </button>
          <button
            type="button"
            disabled={!bookId || nodes.length === 0 || saveOutline.isPending}
            onClick={() => addChapter(0)}
            className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
          >
            +章
          </button>
        </div>
      </div>

      {outlineError && (
        <div className="px-2 pb-1">
          <UnavailableBar code="write_failed" message={outlineError} />
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {!bookId ? (
          <div className="rounded-btn border border-dashed border-border px-2 py-3 text-[11px] leading-relaxed text-muted">
            先选一本书，再管理大纲
          </div>
        ) : nodes.length === 0 ? (
          <div className="rounded-btn border border-dashed border-border px-2 py-3 text-[11px] leading-relaxed text-muted">
            还没有卷 — 点「+卷」开始搭结构
          </div>
        ) : (
          nodes.map((volume, vi) => {
            const open = expanded[volume.id] ?? true
            return (
              <div key={volume.id} className="mb-0.5">
                <div className="group flex items-center gap-1 rounded-btn px-1 py-1 hover:bg-elevated/60">
                  <button
                    type="button"
                    onClick={() => setExpanded((prev) => ({ ...prev, [volume.id]: !open }))}
                    className="shrink-0 text-muted"
                    aria-label={open ? '收起卷' : '展开卷'}
                  >
                    {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                  </button>
                  <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-foreground">
                    {volume.title}
                    <span className="ml-1 text-[10px] text-muted">{volume.children.length}章</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                    <button
                      type="button"
                      onClick={() => addChapter(vi)}
                      className="rounded p-0.5 text-muted hover:text-foreground"
                      title="在本卷新增章"
                      aria-label="在本卷新增章"
                    >
                      <Plus className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => moveVolume(vi, -1)}
                      disabled={vi === 0}
                      className="rounded p-0.5 text-muted hover:text-foreground disabled:opacity-30"
                      title="上移"
                      aria-label="上移卷"
                    >
                      <ArrowUp className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => moveVolume(vi, 1)}
                      disabled={vi === nodes.length - 1}
                      className="rounded p-0.5 text-muted hover:text-foreground disabled:opacity-30"
                      title="下移"
                      aria-label="下移卷"
                    >
                      <ArrowDown className="h-3 w-3" />
                    </button>
                    <button
                      type="button"
                      onClick={() => removeVolume(vi)}
                      className="rounded p-0.5 text-muted hover:text-danger"
                      title="删除卷（含子章节）"
                      aria-label="删除卷"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </span>
                </div>

                {open &&
                  volume.children.map((chapter, ci) => {
                    const editing = editingChapter === chapter.id
                    return (
                      <div key={chapter.id}>
                        <div
                          className={cn(
                            'group flex items-center gap-1 rounded-btn py-1 pl-5 pr-1',
                            chapter.id === currentChapterId
                              ? 'bg-elevated'
                              : 'hover:bg-elevated/60',
                          )}
                        >
                          <span
                            className="shrink-0 text-[11px] leading-none"
                            style={{ color: '#22c55e' }}
                            title={CHAPTER_STATUS_LABELS[chapter.status]}
                            aria-label={CHAPTER_STATUS_LABELS[chapter.status]}
                          >
                            {CHAPTER_STATUS_DOT[chapter.status]}
                          </span>
                          <button
                            type="button"
                            onClick={() => onSelectChapter(chapter.id)}
                            className="min-w-0 flex-1 truncate text-left text-[11px] text-secondary"
                            title={chapter.title}
                          >
                            {String(chapter.order).padStart(2, '0')} {chapter.title}
                          </button>
                          <span className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                            <button
                              type="button"
                              onClick={() => setEditingChapter(editing ? null : chapter.id)}
                              className="rounded p-0.5 text-muted hover:text-foreground"
                              title="编辑细纲/节拍"
                              aria-label="编辑细纲"
                            >
                              <Pencil className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => moveChapter(vi, ci, -1)}
                              disabled={ci === 0}
                              className="rounded p-0.5 text-muted hover:text-foreground disabled:opacity-30"
                              title="上移"
                              aria-label="上移章"
                            >
                              <ArrowUp className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => moveChapter(vi, ci, 1)}
                              disabled={ci === volume.children.length - 1}
                              className="rounded p-0.5 text-muted hover:text-foreground disabled:opacity-30"
                              title="下移"
                              aria-label="下移章"
                            >
                              <ArrowDown className="h-3 w-3" />
                            </button>
                            <button
                              type="button"
                              onClick={() => removeChapter(vi, ci)}
                              className="rounded p-0.5 text-muted hover:text-danger"
                              title="删除章节"
                              aria-label="删除章节"
                            >
                              <Trash2 className="h-3 w-3" />
                            </button>
                          </span>
                        </div>
                        {editing && (
                          <div className="mb-1 ml-5 space-y-1 rounded-btn border border-border bg-base p-2">
                            <input
                              value={chapter.title}
                              onChange={(e) => patchChapter(chapter.id, { title: e.target.value })}
                              placeholder="章节标题"
                              className="w-full rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                            />
                            <input
                              value={chapter.summary}
                              onChange={(e) => patchChapter(chapter.id, { summary: e.target.value })}
                              placeholder="一句话细纲"
                              className="w-full rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                            />
                            <textarea
                              value={chapter.beat}
                              onChange={(e) => patchChapter(chapter.id, { beat: e.target.value })}
                              placeholder="本章节拍（写前门禁判据：为空则 AI 续写禁用）"
                              rows={2}
                              className="w-full resize-none rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                            />
                            <div className="flex justify-end">
                              <button
                                type="button"
                                onClick={() => setEditingChapter(null)}
                                className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground"
                              >
                                <X className="h-3 w-3" />
                                收起
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
              </div>
            )
          })
        )}
      </div>

      {/* ③ 设定摘要（book.json · GET/PATCH meta） */}
      <div className="shrink-0 border-t border-border p-3">
        <div className="mb-1 flex items-center gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-muted/80">
            设定摘要
          </span>
          {bookId && (
            <button
              type="button"
              disabled={!metaQuery.data}
              onClick={() => {
                if (editingSetting) {
                  setEditingSetting(false)
                  setMetaError('')
                  return
                }
                const meta = metaQuery.data
                setSettingDraft({
                  genre: meta?.genre ?? '',
                  pov: meta?.pov ?? '',
                  tense: meta?.tense ?? '',
                  setting_summary: meta?.setting_summary ?? '',
                })
                setEditingSetting(true)
              }}
              className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:text-foreground disabled:opacity-40"
              title="编辑题材 / 视角 / 时态 / 设定摘要"
            >
              <Pencil className="h-3 w-3" aria-hidden="true" />
              {editingSetting ? '取消' : '编辑'}
            </button>
          )}
        </div>

        {metaError && (
          <div className="mb-1">
            <UnavailableBar code="write_failed" message={`设定保存失败：${metaError}。你的输入仍在编辑框中。`} />
          </div>
        )}

        {editingSetting ? (
          <div className="space-y-1">
            <textarea
              autoFocus
              value={settingDraft.setting_summary}
              onChange={(e) =>
                setSettingDraft((d) => ({ ...d, setting_summary: e.target.value }))
              }
              placeholder="设定摘要：世界规则、力量体系、时间背景…（可留空）"
              rows={4}
              className="w-full resize-none rounded-input border border-border bg-base px-1.5 py-1 text-[11px] leading-relaxed outline-none placeholder:text-muted focus:border-accent/60"
            />
            <div className="flex gap-1">
              <input
                value={settingDraft.genre}
                onChange={(e) => setSettingDraft((d) => ({ ...d, genre: e.target.value }))}
                placeholder="题材"
                className="min-w-0 flex-1 rounded-input border border-border bg-base px-1.5 py-1 text-[11px] outline-none placeholder:text-muted focus:border-accent/60"
              />
              <input
                value={settingDraft.pov}
                onChange={(e) => setSettingDraft((d) => ({ ...d, pov: e.target.value }))}
                placeholder="视角"
                className="min-w-0 flex-1 rounded-input border border-border bg-base px-1.5 py-1 text-[11px] outline-none placeholder:text-muted focus:border-accent/60"
              />
              <input
                value={settingDraft.tense}
                onChange={(e) => setSettingDraft((d) => ({ ...d, tense: e.target.value }))}
                placeholder="时态"
                className="min-w-0 flex-1 rounded-input border border-border bg-base px-1.5 py-1 text-[11px] outline-none placeholder:text-muted focus:border-accent/60"
              />
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={() => updateMeta.mutate()}
                disabled={updateMeta.isPending}
                className="rounded-btn border border-border bg-elevated px-2 py-0.5 text-[10px] text-foreground transition-colors hover:border-accent/40 disabled:opacity-50"
                title="写回 data/novel/<书>/book.json"
              >
                {updateMeta.isPending ? '保存中…' : '保存设定'}
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-1 text-[11px] leading-relaxed text-secondary">
            <div className="line-clamp-4">
              {metaQuery.data?.setting_summary?.trim() || (
                <span className="text-muted">尚未填写设定摘要 — 点右上角「编辑」写一句</span>
              )}
            </div>
            {(metaQuery.data?.genre || metaQuery.data?.pov || metaQuery.data?.tense) && (
              <div className="flex flex-wrap gap-1">
                {[
                  metaQuery.data?.genre && `题材 ${metaQuery.data.genre}`,
                  metaQuery.data?.pov && `视角 ${metaQuery.data.pov}`,
                  metaQuery.data?.tense && `时态 ${metaQuery.data.tense}`,
                ]
                  .filter(Boolean)
                  .map((tag) => (
                    <span
                      key={tag as string}
                      className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-muted"
                    >
                      {tag}
                    </span>
                  ))}
              </div>
            )}
            <div className="text-[10px] text-muted">
              {stats.characters} 角色 · {stats.foreshadow} 伏笔（{stats.open} 未收）
            </div>
            <div className="break-all text-[10px] text-muted/80" title={dataDirAbs}>
              数据目录：{dataDirAbs || '—'}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
