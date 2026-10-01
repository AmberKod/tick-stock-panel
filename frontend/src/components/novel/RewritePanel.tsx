// 小说工作区 · 换元仿写 — 主面板
//
// 流程按 PRD §2 排下来：**结构蓝图 → 生成 → 质检报告 → 采纳**。
// 蓝图表单字段多，已拆到 `BlueprintForm.tsx`；本文件只保留主线。
//
// 四条纪律（改动前先读）：
//   1. **P3 人在环**：生成期间不写 `book.json` / `state.json` / `正文/` ——
//      产物只落 `rewrite/drafts/`，唯一入书口是 `/adopt`（一次一个产物，无批量）。
//   2. **四态不混淆**：渲染全部走 `CHECK_STATUS_*` 穷举表；`unavailable` 是「待核」，
//      绝不当「通过」；`fail` 不给勾选框（硬阻断不可被勾选降级）。
//   3. **措辞纪律**：不出现承诺式表述；免责文案**只有后端单点**
//      —— 生成前取 `GET .../rewrite/disclaimer`，出报告后取 `report.disclaimer`
//      （两者同源，见 novelTypes.ts 的注释）。前端不备替代文案。
//   4. **采纳闸门**：`disabled` 由「blocking>0 / 待核项未勾 / 反向三问未勾完 /
//      未确认风险」共同决定；服务端还会二次校验，前端只是提前告知。

import { useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCheck, Loader2, Play, RefreshCw, Square } from 'lucide-react'

import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { useRewriteJob } from '@/lib/useNovelJob'
import { aiStatusShort, errMessage, novelApi, novelRewritePrecheck } from '@/lib/novelApi'
import { errorCode } from '@/lib/api'
import { UnavailableBar, type UnavailableCode } from '@/components/novel/UnavailableBar'
import { RelationGraphCompare } from '@/components/novel/RelationGraphCompare'
import { RewriteReportTable } from '@/components/novel/RewriteReportTable'
import { RiskNoticeCard } from '@/components/novel/RiskNoticeCard'
import { BlueprintForm, BTN_CLS } from '@/components/novel/BlueprintForm'
import {
  REWRITE_KIND_LABELS,
  REWRITE_STEP_LABELS,
  REWRITE_STEP_ORDER,
} from '@/lib/novelTypes'
import type {
  AiStatus,
  Blueprint,
  CheckStatus,
  PrecheckResult,
  ReportResponse,
  RewriteKind,
  StepStatus,
} from '@/lib/novelTypes'

/** 进行中的动作（同一时刻只允许一个写操作，避免连点竞态） */
type BusyAction = '' | 'precheck' | 'save' | 'plan' | 'outline' | 'chapter' | 'check' | 'adopt'

interface Notice {
  code: UnavailableCode
  message: string
}

const STEP_TEXT: Record<StepStatus, string> = {
  pending: '待跑',
  running: '进行中',
  done: '完成',
  failed: '失败',
  skipped: '跳过（本次未重跑）',
  cancelled: '已取消',
}

const STEP_TONE: Record<StepStatus, string> = {
  pending: 'text-muted',
  running: 'text-accent',
  done: 'text-secondary',
  failed: 'text-danger',
  skipped: 'text-muted',
  cancelled: 'text-warning',
}

/**
 * 后端错误码 → 提示条配色。
 *
 * 优先用 `request` 附加在 Error 上的 `code`（P2-8 之后后端结构化码能透到前端）；
 * 取不到 `code` 时（如网络层失败、非结构化错误）才退回文案特征反查。
 * ★只用于挑配色★ —— 展示文案始终用后端原文，绝不改写。
 */
function classifyRewriteError(err: unknown, message: string): UnavailableCode {
  const code = errorCode(err)
  if (code === 'rewrite_source_rejected') return 'rewrite_source_rejected'
  if (code === 'rewrite_gate_blocked') return 'rewrite_gate_blocked'
  if (code === 'rewrite_ack_required') return 'rewrite_ack_required'
  if (code === 'ai_unavailable') return 'ai_unavailable'
  if (code === 'ai_error') return 'ai_error'

  // 兜底：按后端文案特征反查（code 缺失时；后端改文案会让这里失准，
  // 但只影响配色，不影响文案本身）。
  if (message.includes('疑似原文') || message.includes('预检')) return 'rewrite_source_rejected'
  if (message.includes('命门层') || message.includes('未填表')) return 'rewrite_gate_blocked'
  if (
    message.includes('勾选') ||
    message.includes('二次确认') ||
    message.includes('硬阻断') ||
    message.includes('反向校验')
  ) {
    return 'rewrite_ack_required'
  }
  if (message.includes('AI') && message.includes('可用')) return 'ai_unavailable'
  return 'generic'
}

function kindLabel(kind: string): string {
  return REWRITE_KIND_LABELS[kind as RewriteKind] ?? kind
}

/** 提交前清洗：去掉空行 / 空边（textarea 换行会带出空串）。 */
function trimBlueprint(bp: Blueprint): Blueprint {
  const lines = (list: string[]): string[] => list.map((x) => x.trim()).filter(Boolean)
  const pairs = (map: Record<string, string>): Record<string, string> =>
    Object.fromEntries(Object.entries(map).filter(([key, value]) => key.trim() && value.trim()))
  const edges = (list: Blueprint['rebuild']['L3_relations']['source_graph']) =>
    list
      .map((edge) => ({
        from: edge.from.trim(),
        to: edge.to.trim(),
        kind: edge.kind.trim(),
        power: edge.power.trim(),
      }))
      .filter((edge) => edge.from || edge.to)

  return {
    ...bp,
    source_ref: {
      label: bp.source_ref.label.trim(),
      work_type: bp.source_ref.work_type.trim(),
      note: bp.source_ref.note.trim(),
    },
    abstract: {
      ...bp.abstract,
      function_slots: bp.abstract.function_slots
        .map((slot) => ({ slot: slot.slot.trim(), trait: slot.trait.trim() }))
        .filter((slot) => slot.slot || slot.trait),
      emotion_beats: lines(bp.abstract.emotion_beats),
      info_gap: lines(bp.abstract.info_gap),
      reversal_types: lines(bp.abstract.reversal_types),
      reversal_positions: bp.abstract.reversal_positions.filter((value) =>
        Number.isFinite(value),
      ),
      motifs: lines(bp.abstract.motifs),
      pacing: bp.abstract.pacing.trim(),
    },
    rebuild: {
      ...bp.rebuild,
      L1_symbols: {
        banned: lines(bp.rebuild.L1_symbols.banned),
        new_lexicon: pairs(bp.rebuild.L1_symbols.new_lexicon),
      },
      L2_scenes: {
        banned: lines(bp.rebuild.L2_scenes.banned),
        new_scenes: lines(bp.rebuild.L2_scenes.new_scenes),
      },
      L3_relations: {
        source_graph: edges(bp.rebuild.L3_relations.source_graph),
        source_fingerprint: bp.rebuild.L3_relations.source_fingerprint,
        new_graph: edges(bp.rebuild.L3_relations.new_graph),
      },
      L4_events: { new_causal_chain: lines(bp.rebuild.L4_events.new_causal_chain) },
      L5_beats: {
        source_seq: lines(bp.rebuild.L5_beats.source_seq),
        new_seq: lines(bp.rebuild.L5_beats.new_seq),
      },
    },
  }
}

export interface RewritePanelProps {
  bookId: string | null
  chapterId: string | null
  aiStatus: AiStatus | null
  /** 仿写任务 id（由父级持有，切栏 / 切移动端 tab 不丢） */
  rewriteJobId: string | null
  /** 当前查看的报告 id */
  rewriteId: string | null
  onJobCreated: (jobId: string | null) => void
  onReportSelected: (rewriteId: string | null) => void
  /** 采纳成功后由父级统一失效章节 / 追踪态 / 大纲 / 书架 */
  onAdopted: () => void
  className?: string
}

export function RewritePanel({
  bookId,
  chapterId,
  aiStatus,
  rewriteJobId,
  rewriteId,
  onJobCreated,
  onReportSelected,
  onAdopted,
  className,
}: RewritePanelProps) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Blueprint | null>(null)
  const [riskAck, setRiskAck] = useState(false)
  const [busy, setBusy] = useState<BusyAction>('')
  const [notice, setNotice] = useState<Notice | null>(null)
  const [precheck, setPrecheck] = useState<PrecheckResult | null>(null)
  const [snapshotOpen, setSnapshotOpen] = useState(false)

  const blueprintQuery = useQuery({
    queryKey: QK.novelRewriteBlueprint(bookId ?? ''),
    queryFn: () => novelApi.getRewriteBlueprint(bookId as string),
    enabled: !!bookId,
  })
  const reportsQuery = useQuery({
    queryKey: QK.novelRewriteReports(bookId ?? ''),
    queryFn: () => novelApi.listRewriteReports(bookId as string),
    enabled: !!bookId,
  })
  const reportQuery = useQuery({
    queryKey: QK.novelRewriteReport(bookId ?? '', rewriteId ?? ''),
    queryFn: () => novelApi.getRewriteReport(bookId as string, rewriteId as string),
    enabled: !!bookId && !!rewriteId,
  })
  // 大纲只在「要采纳大纲产物」时才拉 —— 父级刻意不预取带乐观锁 version 的大纲。
  const outlineQuery = useQuery({
    queryKey: QK.novelOutline(bookId ?? ''),
    queryFn: () => novelApi.getOutline(bookId as string),
    enabled: !!bookId && reportQuery.data?.report.kind === 'outline',
  })
  // 免责声明：★物理单点★，前端不备任何替代文案。常量 → 取一次就够。
  const disclaimerQuery = useQuery({
    queryKey: QK.novelRewriteDisclaimer(bookId ?? ''),
    queryFn: () => novelApi.getRewriteDisclaimer(bookId as string),
    enabled: !!bookId,
    staleTime: Number.POSITIVE_INFINITY,
    retry: 1,
  })
  // 零写入自检快照：只在用户主动展开时才读（平时不打磁盘）。
  const snapshotQuery = useQuery({
    queryKey: QK.novelRewriteSnapshot(bookId ?? ''),
    queryFn: () => novelApi.getRewriteSnapshot(bookId as string),
    enabled: !!bookId && snapshotOpen,
    staleTime: 0,
  })

  const { job, polling } = useRewriteJob(bookId, rewriteJobId)
  const report = reportQuery.data?.report ?? null
  const serverBlueprint = blueprintQuery.data?.blueprint ?? null

  // 服务端蓝图 → 本地编辑副本。只在「蓝图身份」变化时覆盖，避免覆盖正在输入的内容。
  const serverSig = serverBlueprint ? `${serverBlueprint.id}|${serverBlueprint.updated_at}` : ''
  useEffect(() => {
    if (serverBlueprint) setDraft(serverBlueprint)
  }, [serverSig])

  // 切书重置：风险确认不能跟着上一本书带过来。
  useEffect(() => {
    setRiskAck(false)
    setPrecheck(null)
    setNotice(null)
  }, [bookId])

  // 任务完成 → 直接选中它产出的报告（rewrite_id 已在 job 里，不必再拉列表）。
  useEffect(() => {
    if (!job || job.status !== 'done' || !job.rewrite_id) return
    onReportSelected(job.rewrite_id)
    if (bookId) qc.invalidateQueries({ queryKey: QK.novelRewriteReports(bookId) })
  }, [job?.status, job?.rewrite_id])

  const gateReady = useMemo(() => {
    if (!draft) return false
    const relations = draft.rebuild.L3_relations
    const beats = draft.rebuild.L5_beats
    return (
      relations.source_graph.length > 0 &&
      relations.new_graph.length > 0 &&
      beats.source_seq.length > 0 &&
      beats.new_seq.length > 0
    )
  }, [draft])

  // ── 采纳闸门（提前告知；服务端 `require_adoptable` 还会二次校验）──
  const pendingChecked = report
    ? report.checks
        .filter((item) => item.status === 'warn' || item.status === 'unavailable')
        .every((item) => item.human_checked)
    : false
  const reverseOk = report
    ? report.reverse_three.length > 0 && report.reverse_three.every((q) => q.human_checked)
    : false
  const canAdopt =
    !!report && report.summary.blocking === 0 && pendingChecked && reverseOk && riskAck

  const adoptBlockers: string[] = []
  if (report) {
    if (report.summary.blocking > 0) {
      adoptBlockers.push(`还有 ${report.summary.blocking} 项硬阻断未清零`)
    }
    if (!pendingChecked) adoptBlockers.push('仍有「待核对 / 待核」项未勾选')
    if (!reverseOk) adoptBlockers.push('反向三问未全部勾选')
    if (!riskAck) adoptBlockers.push('未勾选风险确认')
  }

  const aiReady = !!aiStatus?.available
  const canGenerate = gateReady && riskAck && aiReady && busy === '' && !polling

  async function runPrecheck() {
    if (!bookId || !draft) return
    setBusy('precheck')
    setNotice(null)
    try {
      setPrecheck(await novelRewritePrecheck(bookId, { blueprint: trimBlueprint(draft) }))
    } catch (err) {
      setNotice({ code: 'generic', message: errMessage(err, '预检失败') })
    } finally {
      setBusy('')
    }
  }

  async function saveBlueprint() {
    if (!bookId || !draft) return
    setBusy('save')
    setNotice(null)
    setPrecheck(null)
    try {
      const res = await novelApi.putRewriteBlueprint(bookId, trimBlueprint(draft))
      qc.setQueryData(QK.novelRewriteBlueprint(bookId), res)
      setDraft(res.blueprint)
    } catch (err) {
      const message = errMessage(err, '蓝图保存失败')
      setNotice({ code: classifyRewriteError(err, message), message })
    } finally {
      setBusy('')
    }
  }

  async function start(kind: RewriteKind) {
    if (!bookId) return
    setBusy(kind)
    setNotice(null)
    try {
      const call =
        kind === 'plan'
          ? novelApi.rewritePlan
          : kind === 'outline'
            ? novelApi.rewriteOutline
            : novelApi.rewriteChapterDraft
      const created = await call(bookId, {
        risk_ack: riskAck,
        chapter_id: kind === 'chapter' ? chapterId : null,
      })
      onJobCreated(created.job_id)
      qc.setQueryData(QK.novelRewriteJob(bookId, created.job_id), created)
    } catch (err) {
      const message = errMessage(err, '生成启动失败')
      setNotice({ code: classifyRewriteError(err, message), message })
    } finally {
      setBusy('')
    }
  }

  async function writeCheck(
    checks: ReportResponse['report']['checks'],
    reverse: ReportResponse['report']['reverse_three'],
  ) {
    if (!bookId || !report) return
    const payload = {
      checks: checks.map((item) => ({ key: item.key, human_checked: item.human_checked })),
      reverse: reverse.map((question, index) => ({
        index,
        human_checked: question.human_checked,
      })),
    }
    // 乐观更新：勾选立刻反映到 UI；summary 由服务端强算后回写覆盖。
    qc.setQueryData(QK.novelRewriteReport(bookId, report.rewrite_id), {
      ok: true,
      report: { ...report, checks, reverse_three: reverse },
    } satisfies ReportResponse)
    setBusy('check')
    try {
      const res = await novelApi.postRewriteCheck(bookId, report.rewrite_id, payload)
      qc.setQueryData(QK.novelRewriteReport(bookId, report.rewrite_id), res)
    } catch (err) {
      setNotice({ code: 'generic', message: errMessage(err, '勾选写入失败') })
      qc.invalidateQueries({ queryKey: QK.novelRewriteReport(bookId, report.rewrite_id) })
    } finally {
      setBusy('')
    }
  }

  const toggleCheck = (key: string, checked: boolean) => {
    if (!report) return
    writeCheck(
      report.checks.map((item) => (item.key === key ? { ...item, human_checked: checked } : item)),
      report.reverse_three,
    )
  }

  const toggleReverse = (index: number, checked: boolean) => {
    if (!report) return
    writeCheck(
      report.checks,
      report.reverse_three.map((question, i) =>
        i === index ? { ...question, human_checked: checked } : question,
      ),
    )
  }

  async function adopt() {
    if (!bookId || !report || !canAdopt) return
    setBusy('adopt')
    setNotice(null)
    try {
      await novelApi.postRewriteAdopt(bookId, report.rewrite_id, {
        ack: true,
        target: report.kind === 'outline' ? 'outline' : 'chapter',
        version: report.kind === 'outline' ? (outlineQuery.data?.version ?? null) : null,
      })
      onAdopted()
      qc.invalidateQueries({ queryKey: QK.novelRewriteReports(bookId) })
      qc.invalidateQueries({ queryKey: QK.novelRewriteReport(bookId, report.rewrite_id) })
    } catch (err) {
      const message = errMessage(err, '采纳失败')
      setNotice({ code: classifyRewriteError(err, message), message })
    } finally {
      setBusy('')
    }
  }

  async function resumeJob() {
    if (!bookId || !rewriteJobId) return
    try {
      const updated = await novelApi.resumeRewriteJob(bookId, rewriteJobId)
      qc.setQueryData(QK.novelRewriteJob(bookId, rewriteJobId), updated)
    } catch (err) {
      setNotice({ code: 'generic', message: errMessage(err, '重试失败') })
    }
  }

  async function cancelJob() {
    if (!bookId || !rewriteJobId) return
    try {
      const updated = await novelApi.cancelRewriteJob(bookId, rewriteJobId)
      qc.setQueryData(QK.novelRewriteJob(bookId, rewriteJobId), updated)
    } catch (err) {
      setNotice({ code: 'generic', message: errMessage(err, '取消失败') })
    }
  }

  const topologyStatus: CheckStatus | undefined = report?.checks.find(
    (item) => item.key === 'relation_topology',
  )?.status

  if (!bookId) {
    return (
      <div className={cn('grid min-h-0 flex-1 place-items-center bg-surface p-6', className)}>
        <p className="text-xs text-muted">先选一本书，再开始仿写。</p>
      </div>
    )
  }

  return (
    <div className={cn('flex min-h-0 flex-1 flex-col bg-surface', className)}>
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-2">
        {/* 抬头 */}
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <h3 className="text-xs font-semibold text-foreground">换元仿写</h3>
          <span className="text-[11px] text-muted">{aiStatusShort(aiStatus)}</span>
          <span className={gateReady ? 'text-[11px] text-muted' : 'text-[11px] text-warning'}>
            {gateReady
              ? '闸门就绪（L3 / L5 已填）'
              : '闸门未就绪：L3 关系图与 L5 桥段序列都要填两侧'}
          </span>
        </div>

        {notice ? <UnavailableBar code={notice.code} message={notice.message} /> : null}

        {/* 风险告知 + 反向三问（**没有报告时也要在** —— 生成前就要先确认） */}
        <RiskNoticeCard
          disclaimer={disclaimerQuery.data?.disclaimer ?? report?.disclaimer ?? null}
          reverse={report?.reverse_three ?? []}
          riskAck={riskAck}
          onRiskAckChange={setRiskAck}
          onToggleReverse={toggleReverse}
          onRetryDisclaimer={() => {
            if (bookId) qc.invalidateQueries({ queryKey: QK.novelRewriteDisclaimer(bookId) })
          }}
        />

        {/* ── ① 结构蓝图 ── */}
        {draft ? (
          <BlueprintForm
            blueprint={draft}
            onChange={setDraft}
            precheck={precheck}
            busy={busy !== ''}
            prechecking={busy === 'precheck'}
            saving={busy === 'save'}
            onPrecheck={runPrecheck}
            onSave={saveBlueprint}
            defaultOpen={!report}
          />
        ) : (
          <div className="rounded-card border border-border bg-base px-2.5 py-2 text-[11px] text-muted">
            蓝图加载中…
          </div>
        )}

        {/* ── ② 生成 ── */}
        <section className="rounded-card border border-border bg-base">
          <div className="border-b border-border px-2.5 py-2 text-xs text-foreground">
            ② 生成（产物只落 <code className="font-mono">rewrite/drafts/</code>，不写正文）
          </div>
          <div className="flex flex-wrap gap-1.5 px-2.5 py-2">
            {(['plan', 'outline', 'chapter'] as RewriteKind[]).map((kind) => (
              <button
                key={kind}
                type="button"
                className={BTN_CLS}
                disabled={!canGenerate || (kind === 'chapter' && !chapterId)}
                title={
                  !aiReady
                    ? 'AI 网关不可用'
                    : !riskAck
                      ? '先勾选风险确认'
                      : !gateReady
                        ? 'L3 / L5 未填齐'
                        : kind === 'chapter' && !chapterId
                          ? '先在中栏选一个章节'
                          : ''
                }
                onClick={() => start(kind)}
              >
                {busy === kind ? (
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                ) : (
                  <Play className="h-3 w-3" aria-hidden="true" />
                )}
                {kindLabel(kind)}
              </button>
            ))}
          </div>

          {job ? (
            <div className="border-t border-border px-2.5 py-2">
              <div className="flex flex-wrap items-center gap-2 text-[11px]">
                <span className="text-foreground">
                  任务 {job.job_id} · {kindLabel(job.kind)} · {job.status}
                </span>
                {polling ? <span className="text-muted">轮询中…</span> : null}
                {job.failed_step ? (
                  <span className="text-danger">失败于 {job.failed_step}</span>
                ) : null}
                {(job.status === 'failed' || job.status === 'cancelled') && (
                  <button type="button" className={BTN_CLS} onClick={resumeJob}>
                    <RefreshCw className="h-3 w-3" aria-hidden="true" />
                    从失败步重试
                  </button>
                )}
                {polling ? (
                  <button type="button" className={BTN_CLS} onClick={cancelJob}>
                    <Square className="h-3 w-3" aria-hidden="true" />
                    取消
                  </button>
                ) : null}
              </div>

              <ol className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
                {REWRITE_STEP_ORDER.map((name) => {
                  const step = job.steps.find((item) => item.name === name)
                  const status: StepStatus = step?.status ?? 'pending'
                  return (
                    <li key={name} className="text-[11px]">
                      <span className="text-muted">{REWRITE_STEP_LABELS[name]}：</span>
                      <span className={STEP_TONE[status]}>{STEP_TEXT[status]}</span>
                      {step?.error ? (
                        <span className="ml-1 text-danger">（{step.error}）</span>
                      ) : null}
                    </li>
                  )
                })}
              </ol>

              {job.artifacts.draft_text ? (
                <details className="mt-1.5">
                  <summary className="cursor-pointer text-[11px] text-secondary">
                    草稿预览（尚未入书）
                  </summary>
                  <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-btn border border-border bg-surface px-2 py-1.5 text-[11px] leading-relaxed text-secondary">
                    {job.artifacts.draft_text}
                  </pre>
                </details>
              ) : null}
            </div>
          ) : null}
        </section>

        {/* ── ③ 报告 ── */}
        <section className="space-y-2">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <h4 className="text-xs font-semibold text-foreground">③ 质检报告</h4>
            <div className="flex flex-wrap gap-1">
              {(reportsQuery.data?.reports ?? []).map((item) => (
                <button
                  key={item.rewrite_id}
                  type="button"
                  onClick={() => onReportSelected(item.rewrite_id)}
                  className={cn(
                    'rounded-btn border px-1.5 py-0.5 text-[10px] transition-colors',
                    item.rewrite_id === rewriteId
                      ? 'border-accent/60 bg-elevated text-foreground'
                      : 'border-border text-muted hover:text-foreground',
                  )}
                >
                  {kindLabel(item.kind)} · {item.generated_at.slice(5, 16)} · 阻断 {item.blocking}
                </button>
              ))}
            </div>
          </div>

          {!report ? (
            <p className="rounded-card border border-border bg-surface px-2.5 py-2 text-[11px] text-muted">
              还没有报告 —— 发起一次生成，任务完成后报告会出现在这里。
            </p>
          ) : (
            <>
              <RewriteReportTable
                report={report}
                onToggleCheck={toggleCheck}
                pending={busy === 'check'}
              />
              <RelationGraphCompare
                sourceGraph={draft?.rebuild.L3_relations.source_graph ?? []}
                newGraph={draft?.rebuild.L3_relations.new_graph ?? []}
                status={topologyStatus}
              />

              <div className="rounded-card border border-border bg-surface px-2.5 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className={BTN_CLS}
                    disabled={!canAdopt || busy === 'adopt'}
                    onClick={adopt}
                  >
                    {busy === 'adopt' ? (
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                    ) : (
                      <FileCheck className="h-3 w-3" aria-hidden="true" />
                    )}
                    采纳进正式书稿（一次一个产物）
                  </button>
                  <span className="text-[11px] text-muted">
                    服务端判定：{report.summary.adoptable ? '可采纳' : '尚不可采纳'}
                  </span>
                </div>
                {adoptBlockers.length > 0 ? (
                  <ul className="mt-1 space-y-0.5">
                    {adoptBlockers.map((blocker) => (
                      <li key={blocker} className="text-[11px] text-warning">
                        · {blocker}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {report.ack.acknowledged_at ? (
                  <p className="mt-1 font-mono text-[10px] text-muted">
                    留痕 {report.ack.acknowledged_at} · 声明版本{' '}
                    {report.ack.disclaimer_version ?? '—'}
                  </p>
                ) : null}
              </div>

              {/* 零写入自检入口 —— 刻意做成一行小字，不是主按钮 */}
              <div className="rounded-card border border-border bg-surface px-2.5 py-2">
                <button
                  type="button"
                  onClick={() => setSnapshotOpen((v) => !v)}
                  aria-expanded={snapshotOpen}
                  className="text-[11px] text-muted underline decoration-dotted underline-offset-2 transition-colors hover:text-foreground"
                >
                  自检：生成过程是否污染了正式书稿？
                </button>
                {snapshotOpen ? (
                  <div className="mt-1.5 space-y-1.5">
                    <p className="text-[10px] leading-relaxed text-muted">
                      下面是本地权威文件的三重校验值（
                      <code className="font-mono">book.json</code> 的 mtime_ns 与 version、
                      <code className="font-mono">state.json</code> 的 mtime_ns 与 sha256、
                      各章节正文的 mtime_ns 与 sha256）。生成前后应当
                      <span className="text-secondary">完全一致</span> ——
                      若不一致，说明生成阶段动过正式书稿，请停止使用并反馈。
                    </p>
                    {snapshotQuery.isLoading ? (
                      <p className="text-[11px] text-muted">读取中…</p>
                    ) : snapshotQuery.isError ? (
                      <p className="text-[11px] text-danger">
                        读取失败：{errMessage(snapshotQuery.error, '未知原因')}
                      </p>
                    ) : (
                      <pre className="max-h-56 overflow-auto whitespace-pre-wrap rounded-btn border border-border bg-base px-2 py-1.5 font-mono text-[10px] leading-relaxed text-secondary">
                        {JSON.stringify(snapshotQuery.data?.snapshot ?? {}, null, 2)}
                      </pre>
                    )}
                    <button
                      type="button"
                      className={BTN_CLS}
                      onClick={() => void snapshotQuery.refetch()}
                    >
                      <RefreshCw className="h-3 w-3" aria-hidden="true" />
                      重新读取
                    </button>
                  </div>
                ) : null}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
