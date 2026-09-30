// 小说工作区 — AI 面板
//
// 四个区块（PRD §7.3）：① 上下文卡 ② 续写 / 润色 ③ Step 进度与重试 ④ 写后自检 + 采纳。
//
// 诚实口径（对应主理人裁定的后端契约）：
//   - 第 4 步 ingest **只算摄取计划**，不写 state.json；真正写入只在「采纳」。
//   - resume 后已完成步骤变 skipped，UI 如实写「本次未重跑」。
//   - cancel 是协作式的：只置标记，返回的 job 可能仍是 running，
//     文案必须写「取消请求已提交，正在等待当前步骤收尾」，不许假装立即中断。
//   - 写后自检只提醒，不改写正文。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  ClipboardCopy,
  Loader2,
  RefreshCw,
  Sparkles,
  StopCircle,
  Wand2,
  XCircle,
} from 'lucide-react'

import { cn } from '@/lib/cn'
import { errMessage, novelApi } from '@/lib/novelApi'
import { QK } from '@/lib/queryKeys'
import { useNovelJob } from '@/lib/useNovelJob'
import {
  LINT_RULE_LABELS,
  STEP_LABELS,
  STEP_ORDER,
  VIEW_LABELS,
} from '@/lib/novelTypes'
import type {
  ChapterCard,
  ChapterFact,
  JobStep,
  NovelStatusResponse,
  StepName,
  StepStatus,
} from '@/lib/novelTypes'
import { UnavailableBar } from './UnavailableBar'

export interface AiDraftPanelProps {
  bookId: string | null
  chapterId: string | null
  chapter: ChapterCard | null
  aiStatus: NovelStatusResponse | null
  /** 编辑器里选中的原文（润色用） */
  selection: string
  /** 当前正在跟踪的 job */
  jobId: string | null
  onJobCreated: (jobId: string) => void
  onAdopted: () => void
}

const STEP_MARK: Record<StepStatus, string> = {
  pending: '·',
  running: '◌',
  done: '✓',
  failed: '✕',
  skipped: '⊘',
  cancelled: '⊗',
}

const STEP_TONE: Record<StepStatus, string> = {
  pending: 'text-muted',
  running: 'text-foreground',
  done: 'text-[#22c55e]',
  failed: 'text-danger',
  skipped: 'text-muted',
  cancelled: 'text-warning',
}

/** 步骤状态后缀 —— 诚实说明"这次为什么没走完"，不把取消包装成完成。 */
function stepNote(status: StepStatus): string {
  if (status === 'skipped') return '（本次未重跑）'
  if (status === 'cancelled') return '（已取消，续跑会重做）'
  return ''
}

export function AiDraftPanel({
  bookId,
  chapterId,
  chapter,
  aiStatus,
  selection,
  jobId,
  onJobCreated,
  onAdopted,
}: AiDraftPanelProps) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(true)
  const [showContext, setShowContext] = useState(false)
  const [showManual, setShowManual] = useState(false)
  const [error, setError] = useState('')
  const [manual, setManual] = useState({ chars: '', state_changes: '', planted: '', resolved: '' })

  const available = !!aiStatus?.available
  const gateMissing = !chapter?.beat?.trim() && !chapter?.summary?.trim()
  const { job, polling } = useNovelJob(jobId)

  const contextQuery = useQuery({
    queryKey: QK.novelView(bookId ?? '', 'context-card'),
    queryFn: () => novelApi.getView(bookId as string, 'context-card'),
    enabled: !!bookId && showContext,
  })

  const startJob = useMutation({
    mutationFn: (body: { mode: 'continue' | 'polish'; selection?: string; skip_gate?: boolean }) =>
      novelApi.aiDraft(bookId as string, chapterId as string, body),
    onSuccess: (job) => {
      setError('')
      onJobCreated(job.job_id)
      qc.setQueryData(QK.novelJob(job.job_id), job)
    },
    onError: (err: unknown) => setError(errMessage(err, '启动 AI 任务失败')),
  })

  const resumeJob = useMutation({
    mutationFn: (id: string) => novelApi.resumeJob(id),
    onSuccess: (next) => {
      setError('')
      // 终态 → queued：写回缓存让轮询自己恢复（refetchInterval 会重新生效）
      qc.setQueryData(QK.novelJob(next.job_id), next)
      onJobCreated(next.job_id)
    },
    onError: (err: unknown) => setError(errMessage(err, '重试失败')),
  })

  const cancelJob = useMutation({
    mutationFn: (id: string) => novelApi.cancelJob(id),
    onSuccess: (next) => qc.setQueryData(QK.novelJob(next.job_id), next),
    onError: (err: unknown) => setError(errMessage(err, '取消失败')),
  })

  const lint = useMutation({
    mutationFn: (text: string) => novelApi.lint(bookId as string, { text }),
    onError: (err: unknown) => setError(errMessage(err, '自检失败')),
  })

  const adopt = useMutation({
    mutationFn: (body: { draft_id: string; fact?: ChapterFact | null; selection?: string | null }) =>
      novelApi.adopt(bookId as string, chapterId as string, body),
    onSuccess: () => {
      setError('')
      qc.invalidateQueries({ queryKey: QK.novelOutline(bookId ?? '') })
      qc.invalidateQueries({ queryKey: QK.novelChapters(bookId ?? '') })
      qc.invalidateQueries({ queryKey: QK.novelState(bookId ?? '') })
      qc.invalidateQueries({ queryKey: QK.novelChapter(bookId ?? '', chapterId ?? '') })
      qc.invalidateQueries({ queryKey: QK.novelView(bookId ?? '', 'context-card') })
      onAdopted()
    },
    onError: (err: unknown) => setError(errMessage(err, '采纳失败')),
  })

  const manualPatch = useMutation({
    mutationFn: () =>
      novelApi.patchState(bookId as string, {
        chapter_id: chapterId as string,
        fact: {
          source: 'manual',
          chars: splitLines(manual.chars),
          state_changes: splitLines(manual.state_changes),
          planted: splitLines(manual.planted),
          resolved: splitLines(manual.resolved),
        },
      }),
    onSuccess: () => {
      setError('')
      setShowManual(false)
      setManual({ chars: '', state_changes: '', planted: '', resolved: '' })
      qc.invalidateQueries({ queryKey: QK.novelState(bookId ?? '') })
      qc.invalidateQueries({ queryKey: QK.novelView(bookId ?? '', 'context-card') })
    },
    onError: (err: unknown) => setError(errMessage(err, '手工补录失败')),
  })

  const busy = startJob.isPending || (polling && job?.status !== 'failed')
  const draftText = job?.artifacts.draft_text ?? ''
  const draftId = job?.artifacts.draft_id ?? ''
  const fact = job?.artifacts.fact_json ?? null
  const plan = job?.artifacts.ingest_plan ?? null
  const steps: JobStep[] = job?.steps ?? []
  const stepMap = new Map<StepName, JobStep>(steps.map((s) => [s.name, s]))

  return (
    <div className="bg-surface">
      {/* 面板头（可折叠） */}
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-1 text-[11px] font-semibold text-secondary hover:text-foreground"
          aria-expanded={open}
        >
          {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          <Sparkles className="h-3 w-3" style={{ color: '#22c55e' }} aria-hidden="true" />
          AI 面板
        </button>
        {job?.status && (
          <span className="rounded-full border border-border px-1.5 py-0.5 text-[10px] text-muted">
            {job.status === 'running'
              ? '生成中'
              : job.status === 'queued'
                ? '排队中'
                : job.status === 'done'
                  ? '已完成'
                  : job.status === 'failed'
                    ? '已失败'
                    : '已取消'}
          </span>
        )}
        {!chapterId && <span className="text-[10px] text-muted">先选一章</span>}
      </div>

      {!open ? null : (
        <div className="space-y-2 px-3 py-2">
          {/* AI 不可用 —— PRD §7.4 场景 1/2 */}
          {!available && (
            <UnavailableBar
              code="ai_unavailable"
              message={aiStatus?.reason ?? undefined}
            />
          )}

          {/* ① 上下文卡 */}
          <div>
            <button
              type="button"
              onClick={() => setShowContext((v) => !v)}
              className="flex items-center gap-1 text-[11px] text-secondary hover:text-foreground"
            >
              {showContext ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              ① 续写上下文卡（本次将喂给 AI 的上下文）
            </button>
            {showContext && (
              <div className="mt-1 rounded-btn border border-border bg-base p-2">
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-[10px] text-muted">
                    {VIEW_LABELS['context-card']} · 派生视图，请勿手工编辑
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      void navigator.clipboard?.writeText(contextQuery.data?.content ?? '')
                    }}
                    className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground"
                  >
                    <ClipboardCopy className="h-3 w-3" aria-hidden="true" />
                    复制
                  </button>
                </div>
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-5 text-secondary">
                  {contextQuery.isLoading ? '加载中…' : (contextQuery.data?.content ?? '（暂无）')}
                </pre>
              </div>
            )}
          </div>

          {/* ② 续写 / 润色 */}
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              disabled={!available || !chapterId || busy || gateMissing}
              onClick={() => startJob.mutate({ mode: 'continue' })}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-foreground transition-colors hover:border-accent/40 disabled:opacity-40"
              title={gateMissing ? '本章细纲为空，续写已禁用' : '按大纲上下文续写本章'}
            >
              <Sparkles className="h-3 w-3" style={{ color: '#22c55e' }} aria-hidden="true" />
              续写本章
            </button>
            <button
              type="button"
              disabled={!available || !chapterId || busy || !selection}
              onClick={() => startJob.mutate({ mode: 'polish', selection })}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-foreground transition-colors hover:border-accent/40 disabled:opacity-40"
              title={selection ? `润色选中的 ${selection.length} 字` : '请先在编辑器里选中要润色的文本'}
            >
              <Wand2 className="h-3 w-3" aria-hidden="true" />
              润色选中
            </button>
            {!selection && (
              <span className="text-[10px] text-muted">未选中文本 · 润色不可用</span>
            )}
            {gateMissing && available && (
              <button
                type="button"
                disabled={busy || !chapterId}
                onClick={() => startJob.mutate({ mode: 'continue', skip_gate: true })}
                className="rounded-btn border border-dashed border-border px-2 py-1 text-[10px] text-muted hover:text-foreground disabled:opacity-40"
                title="知情放行：本章无细纲，AI 只能凭上下文自由发挥"
              >
                仍要续写（跳过本次门禁）
              </button>
            )}
          </div>

          {error && <UnavailableBar code="ai_error" message={error} />}

          {/* ③ Step 进度 */}
          {job && (
            <div className="rounded-btn border border-border bg-base p-2">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                {STEP_ORDER.map((name, index) => {
                  const step = stepMap.get(name)
                  const status: StepStatus = step?.status ?? 'pending'
                  const isFailed = status === 'failed'
                  return (
                    <span key={name} className="flex items-center gap-1 text-[10px]">
                      <span className={cn('font-mono', STEP_TONE[status])}>
                        {status === 'running' ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          STEP_MARK[status]
                        )}
                      </span>
                      <span className={isFailed ? 'text-danger' : 'text-secondary'}>
                        {index + 1} {STEP_LABELS[name]}
                        {stepNote(status)}
                      </span>
                    </span>
                  )
                })}
              </div>

              {job.status === 'failed' && job.failed_step && (
                <div className="mt-1.5">
                  <UnavailableBar
                    code={
                      job.failed_step === 'fact_snapshot' ? 'fact_parse_failed' : 'ai_error'
                    }
                    message={stepMap.get(job.failed_step)?.error ?? `失败在「${STEP_LABELS[job.failed_step]}」`}
                    action={
                      <button
                        type="button"
                        onClick={() => resumeJob.mutate(job.job_id)}
                        disabled={resumeJob.isPending}
                        className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
                      >
                        <RefreshCw className="h-3 w-3" aria-hidden="true" />
                        重试该步骤
                      </button>
                    }
                  />
                </div>
              )}

              {job.status === 'cancelled' && (
                <div className="mt-1.5">
                  <UnavailableBar
                    code="generic"
                    message="任务已取消。已完成的步骤不会重跑；被取消的那一步没有产物，「继续」时会重新执行。"
                    action={
                      <button
                        type="button"
                        onClick={() => resumeJob.mutate(job.job_id)}
                        disabled={resumeJob.isPending}
                        className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
                      >
                        <RefreshCw className="h-3 w-3" aria-hidden="true" />
                        继续
                      </button>
                    }
                  />
                </div>
              )}

              {polling && (
                <div className="mt-1.5 flex items-center gap-1.5 text-[10px] text-muted">
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                  生成中（每 1.5s 轮询，刷新页面也不会丢进度）
                  <button
                    type="button"
                    onClick={() => cancelJob.mutate(job.job_id)}
                    disabled={cancelJob.isPending}
                    className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
                    title="协作式取消"
                  >
                    <StopCircle className="h-3 w-3" aria-hidden="true" />
                    取消
                  </button>
                </div>
              )}
              {cancelJob.isSuccess && polling && (
                <div className="mt-1 text-[10px] text-muted">
                  取消请求已提交，正在等待当前步骤收尾（不会假装立即中断）。
                </div>
              )}
            </div>
          )}

          {/* 原文 ↔ 草稿 并排对照 + 采纳 */}
          {job?.status === 'done' && draftText && (
            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[11px] text-secondary">
                  ④ {job.mode === 'polish' ? '润色对照（仅替换选中区间）' : '续写对照'}
                </span>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={() => lint.mutate(draftText)}
                    disabled={lint.isPending}
                    className="rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground disabled:opacity-50"
                  >
                    写后自检
                  </button>
                  <button
                    type="button"
                    onClick={() => adopt.mutate({ draft_id: draftId, fact, selection: job.mode === 'polish' ? selection : null })}
                    disabled={adopt.isPending}
                    className="rounded-btn border border-border bg-elevated px-2 py-0.5 text-[10px] text-foreground hover:border-accent/40 disabled:opacity-50"
                    title="采纳后才会写入追踪态并重建派生视图"
                  >
                    {adopt.isPending ? '采纳中…' : '采纳'}
                  </button>
                </div>
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                <div className="rounded-btn border border-border bg-base p-2">
                  <div className="mb-1 text-[10px] text-muted">原文</div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-5 text-secondary">
                    {job.mode === 'polish' ? selection || '（未捕获到选区）' : '（续写模式：草稿将追加/替换正文）'}
                  </pre>
                </div>
                <div className="rounded-btn border border-border bg-base p-2">
                  <div className="mb-1 text-[10px] text-muted">AI 草稿</div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-5 text-foreground">
                    {draftText}
                  </pre>
                </div>
              </div>
              <div className="mt-1 text-[10px] leading-relaxed text-muted">
                草稿存于 drafts/，未采纳时不影响正文、也不写追踪态；不采纳可直接用文件管理器删掉对应草稿文件。
              </div>
            </div>
          )}

          {/* ④ 摄取计划（第 4 步产物，未落盘） */}
          {plan && (
            <div className="rounded-btn border border-dashed border-border bg-base p-2">
              <div className="mb-1 text-[10px] font-semibold text-secondary">
                摄取计划（第 4 步产物 · 尚未写入 state.json）
              </div>
              <ul className="space-y-0.5 text-[10px] leading-relaxed text-secondary">
                {plan.new_characters.length > 0 && (
                  <li>新角色：{plan.new_characters.join('、')}</li>
                )}
                {plan.state_changes.map((item, i) => (
                  <li key={`sc-${i}`}>状态变化：{item}</li>
                ))}
                {plan.planted.map((item, i) => (
                  <li key={`pl-${i}`}>埋设伏笔：{item}</li>
                ))}
                {plan.resolved.map((item, i) => (
                  <li key={`rs-${i}`}>回收伏笔：{item}</li>
                ))}
                {plan.relations.map((item, i) => (
                  <li key={`rl-${i}`}>
                    关系变化：{item.from} → {item.to}：{item.delta}
                  </li>
                ))}
                <li className="text-muted">
                  建议补丁（本地规则生成，非 AI 结论）：{plan.rolling_summary_patch}
                </li>
                <li className="text-muted">{plan.note}</li>
              </ul>
            </div>
          )}

          {/* 写后自检结果 */}
          {lint.data && (
            <div>
              <UnavailableBar
                code="lint_hits"
                message={`自检提醒 ${lint.data.count} 项 — 仅提醒，未改写你的文字。`}
              />
              {lint.data.hits.length > 0 && (
                <ul className="mt-1 space-y-1">
                  {lint.data.hits.map((hit, i) => (
                    <li
                      key={`${hit.rule}-${i}`}
                      className="flex items-start gap-2 rounded-btn border border-border bg-base px-2 py-1 text-[10px] leading-relaxed"
                    >
                      <span className="shrink-0 rounded-full border border-border px-1.5 py-0.5 text-muted">
                        {LINT_RULE_LABELS[hit.rule] ?? hit.rule}
                      </span>
                      <span className="min-w-0 flex-1 text-secondary">
                        {hit.message}
                        {hit.line ? `（第 ${hit.line} 行）` : ''}
                        {hit.excerpt ? ` · 命中：${hit.excerpt}` : ''}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* 手工补录（P0-10③：不强依赖 AI） */}
          <div>
            <button
              type="button"
              onClick={() => setShowManual((v) => !v)}
              className="flex items-center gap-1 text-[11px] text-secondary hover:text-foreground"
            >
              {showManual ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              手工补录本章事实（不强依赖 AI）
            </button>
            {showManual && (
              <div className="mt-1 space-y-1 rounded-btn border border-border bg-base p-2">
                <input
                  value={manual.chars}
                  onChange={(e) => setManual((m) => ({ ...m, chars: e.target.value }))}
                  placeholder="出场角色（逗号分隔）"
                  className="w-full rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                />
                <textarea
                  value={manual.state_changes}
                  onChange={(e) => setManual((m) => ({ ...m, state_changes: e.target.value }))}
                  placeholder="状态变化（每行一条，如「陆昭从健康→轻伤住院」）"
                  rows={2}
                  className="w-full resize-none rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                />
                <textarea
                  value={manual.planted}
                  onChange={(e) => setManual((m) => ({ ...m, planted: e.target.value }))}
                  placeholder="埋设伏笔（每行一条）"
                  rows={2}
                  className="w-full resize-none rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                />
                <textarea
                  value={manual.resolved}
                  onChange={(e) => setManual((m) => ({ ...m, resolved: e.target.value }))}
                  placeholder="回收伏笔（填伏笔 id 或原文）"
                  rows={2}
                  className="w-full resize-none rounded-input border border-border bg-surface px-1.5 py-1 text-[11px] outline-none focus:border-accent/60"
                />
                <div className="flex justify-end gap-1">
                  <button
                    type="button"
                    onClick={() => setShowManual(false)}
                    className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:text-foreground"
                  >
                    <XCircle className="h-3 w-3" aria-hidden="true" />
                    取消
                  </button>
                  <button
                    type="button"
                    onClick={() => manualPatch.mutate()}
                    disabled={manualPatch.isPending}
                    className="rounded-btn border border-border bg-elevated px-2 py-0.5 text-[10px] text-foreground hover:border-accent/40 disabled:opacity-50"
                  >
                    {manualPatch.isPending ? '写入中…' : '写入追踪态'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function splitLines(value: string): string[] {
  return value
    .split('\n')
    .flatMap((line) => line.split(/[,，]/))
    .map((item) => item.trim())
    .filter(Boolean)
}
