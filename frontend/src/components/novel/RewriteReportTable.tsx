// 小说工作区 · 换元仿写 — 八项质检对照表（四态渲染的**唯一**实现处）
//
// 四态纪律（★禁止混淆★）：
//   pass        → 中性色点 + 「通过」，**不渲染勾选框**（无需人工确认）
//   warn        → 琥珀色 + 「需人工核对」，**可勾选**
//   unavailable → 灰色 + 「待核」，**可勾选**，绝不能显示成绿色/「通过」
//   fail        → 红色 + 「硬阻断」，**不渲染勾选框**（硬阻断不可被勾选降级为 warn）
//
// 三张 `Record<CheckStatus, string>` 穷举表在 `lib/novelTypes.ts`（新增第五态会编译报错）。
// `metrics` 只做「可核对性」展示（把算法中间量摊开），不承诺任何结论。

import { CHECK_LABELS, CHECK_PASS_COLOR, CHECK_STATUS_DOT, CHECK_STATUS_LABELS, CHECK_STATUS_TONE } from '@/lib/novelTypes'
import type { CheckItem, CheckKey, CheckStatus, RewriteReport } from '@/lib/novelTypes'
import { cn } from '@/lib/cn'

export interface RewriteReportTableProps {
  report: RewriteReport
  /** 勾选变更（fail / pass 项不会被调用 —— 它们没有勾选框） */
  onToggleCheck: (key: string, checked: boolean) => void
  /** 勾选写入中的标记（禁用勾选框，避免连点产生竞态） */
  pending?: boolean
  className?: string
}

/** 把算法中间量摊成一行短文本 —— 让用户能核对「为什么这么判」。 */
function metricSummary(item: CheckItem): string {
  const metrics = item.metrics ?? {}
  const num = (value: unknown): string => (typeof value === 'number' ? String(value) : '')
  const join = (parts: string[]): string => parts.filter(Boolean).join(' · ')

  switch (item.key as CheckKey) {
    case 'relation_topology':
      return join([
        num(metrics.sim) ? `拓扑相似度 ${num(metrics.sim)}` : '',
        num(metrics.deg_sim) ? `度数相似 ${num(metrics.deg_sim)}` : '',
        num(metrics.kind_jaccard) ? `类型重合 ${num(metrics.kind_jaccard)}` : '',
      ])
    case 'beat_sequence':
      return join([
        num(metrics.lcs_len) && num(metrics.max_len)
          ? `最长公共子序列 ${num(metrics.lcs_len)}/${num(metrics.max_len)}`
          : '',
        num(metrics.ratio) ? `重合比 ${num(metrics.ratio)}` : '',
        num(metrics.jaccard) ? `集合重合 ${num(metrics.jaccard)}` : '',
      ])
    case 'one_to_one_character':
      return join([
        num(metrics.src_count) && num(metrics.new_count)
          ? `角色数 ${num(metrics.src_count)} / ${num(metrics.new_count)}`
          : '',
        num(metrics.jaccard) ? `功能位重合 ${num(metrics.jaccard)}` : '',
      ])
    case 'isomorphic_reversal': {
      const hits = Array.isArray(metrics.hits) ? metrics.hits.length : 0
      return hits ? `同型反转 ${hits} 处` : ''
    }
    case 'proper_noun':
    case 'signature_scene':
      return `黑名单 ${num(metrics.banned_count) || '0'} 条 · 命中 ${num(metrics.hit_count) || '0'} 处`
    default:
      return ''
  }
}

/** 状态点颜色：pass 用域色 #22c55e（状态点是该色的合法用途），其余走文本色。 */
function dotStyle(status: CheckStatus): React.CSSProperties | undefined {
  return status === 'pass' ? { color: CHECK_PASS_COLOR } : undefined
}

export function RewriteReportTable({
  report,
  onToggleCheck,
  pending = false,
  className,
}: RewriteReportTableProps) {
  const checkable = (status: CheckStatus): boolean => status === 'warn' || status === 'unavailable'

  return (
    <section className={cn('rounded-card border border-border bg-surface', className)}>
      {/* 汇总条：四项计数 + 是否可采纳（可采纳由后端强算，前端只展示） */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-2.5 py-2 text-[11px]">
        <span className="text-foreground">质检对照表（{report.checks.length} 项）</span>
        <span className="text-muted">通过 {report.summary.passed}</span>
        <span className="text-warning">待核对 {report.summary.warn}</span>
        <span className="text-muted">待核 {report.summary.unavailable}</span>
        <span className={report.summary.blocking > 0 ? 'text-danger' : 'text-muted'}>
          硬阻断 {report.summary.blocking}
        </span>
      </div>

      <ul className="divide-y divide-border">
        {report.checks.map((item) => {
          const key = item.key as CheckKey
          const label = CHECK_LABELS[key] ?? item.key
          const summary = metricSummary(item)
          return (
            <li key={item.key} className="px-2.5 py-2">
              <div className="flex items-start gap-2">
                {/* 状态点 */}
                <span
                  className={cn('mt-0.5 w-3 shrink-0 text-center text-xs', CHECK_STATUS_TONE[item.status])}
                  style={dotStyle(item.status)}
                  aria-hidden="true"
                >
                  {CHECK_STATUS_DOT[item.status]}
                </span>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="text-xs text-foreground">{label}</span>
                    <span className="rounded-btn border border-border px-1 text-[10px] text-muted">
                      {item.layer}
                    </span>
                    <span className="rounded-btn border border-border px-1 text-[10px] text-muted">
                      {item.mode === 'auto' ? '自动' : item.mode === 'semi_auto' ? '半自动' : item.mode}
                    </span>
                    <span className={cn('text-[11px]', CHECK_STATUS_TONE[item.status])}>
                      {CHECK_STATUS_LABELS[item.status]}
                    </span>
                  </div>

                  {item.detail ? (
                    <p className="mt-0.5 text-[11px] leading-relaxed text-secondary">{item.detail}</p>
                  ) : null}

                  {summary ? (
                    <p className="mt-0.5 font-mono text-[10px] text-muted">{summary}</p>
                  ) : null}

                  {item.evidence.length > 0 ? (
                    <ul className="mt-1 space-y-0.5">
                      {item.evidence.map((evidence, index) => (
                        <li key={index} className="text-[11px] text-secondary">
                          <span className="mr-1 text-muted">
                            {evidence.line === null ? '·' : `L${evidence.line}`}
                          </span>
                          {evidence.excerpt}
                        </li>
                      ))}
                    </ul>
                  ) : null}

                  {/* 人工指引：unavailable 必有（后端强校验），warn/fail 也有 */}
                  {item.human_tip ? (
                    <p className="mt-1 rounded-btn border border-border bg-elevated px-1.5 py-1 text-[11px] leading-relaxed text-secondary">
                      {item.human_tip}
                    </p>
                  ) : null}

                  {item.checked_at ? (
                    <p className="mt-0.5 text-[10px] text-muted">
                      已核对于 {item.checked_at}
                    </p>
                  ) : null}
                </div>

                {/* 勾选区：只有 warn / unavailable 有勾选框 */}
                <div className="w-24 shrink-0 text-right">
                  {checkable(item.status) ? (
                    <label className="inline-flex cursor-pointer items-center gap-1 text-[11px] text-secondary">
                      <input
                        type="checkbox"
                        checked={item.human_checked}
                        disabled={pending}
                        onChange={(e) => onToggleCheck(item.key, e.target.checked)}
                        className="h-3 w-3 accent-warning disabled:opacity-50"
                      />
                      已核对
                    </label>
                  ) : item.status === 'fail' ? (
                    <span className="text-[11px] text-danger">硬阻断 · 需改后重跑</span>
                  ) : (
                    <span className="text-[11px] text-muted">—</span>
                  )}
                </div>
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
