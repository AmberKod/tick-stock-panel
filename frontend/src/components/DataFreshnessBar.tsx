import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Database, Download, Loader2 } from 'lucide-react'
import { api, type MarketFreshness } from '@/lib/api'
import { useDataFreshness } from '@/lib/useSharedQueries'
import { Modal } from '@/components/Modal'
import { DatePicker } from '@/components/DatePicker'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

/**
 * 底部常驻数据新鲜度状态栏。
 *
 * 回答三个问题: 现在在拉什么 / 各市场数据到哪天 / 缺的那段要不要补。
 * 用 sticky bottom-0 而不是 fixed: 保留文档流位置, 长页面滚动时始终贴底且不遮挡内容。
 */

const STATUS_META: Record<string, { label: string; dot: string; text: string }> = {
  ok:         { label: '已就绪',   dot: 'bg-bull',    text: 'text-foreground/70' },
  stale:      { label: '落后',     dot: 'bg-warning', text: 'text-warning' },
  shallow:    { label: '历史偏薄', dot: 'bg-warning', text: 'text-warning' },
  behind_raw: { label: '待计算',   dot: 'bg-warning', text: 'text-warning' },
  partial:    { label: '同步中',   dot: 'bg-warning', text: 'text-warning' },
  empty:      { label: '无数据',   dot: 'bg-muted',   text: 'text-muted' },
  unknown:    { label: '未知',     dot: 'bg-muted',   text: 'text-muted' },
}

const MARKET_LABEL: Record<string, string> = { CN: 'A股', HK: '港股', US: '美股' }

function marketName(m: MarketFreshness) {
  return m.label || MARKET_LABEL[m.market] || m.market
}

function MarketChip({ m }: { m: MarketFreshness }) {
  const meta = STATUS_META[m.status] ?? STATUS_META.unknown
  const pct = m.coverage_ratio > 0 && m.coverage_ratio < 1
    ? ` ${(m.coverage_ratio * 100).toFixed(0)}%`
    : ''
  const stale = m.stale_days != null && m.stale_days > 0 ? ` 落后${m.stale_days}天` : ''
  return (
    <span
      className="flex items-center gap-1.5 whitespace-nowrap"
      title={`最新 ${m.latest_date ?? '—'} · 原始 ${m.raw_latest_date ?? '—'} · 覆盖 ${m.coverage_units ?? '—'}${m.coverage_unit_label}${pct}`}
    >
      <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
      <span className="text-foreground/80">{marketName(m)}</span>
      <span className="font-mono text-foreground/60">{m.latest_date ?? '—'}</span>
      <span className={meta.text}>{meta.label}{stale}</span>
    </span>
  )
}

function BackfillDialog({ m, onClose }: { m: MarketFreshness; onClose: () => void }) {
  const gap = m.gap
  const [start, setStart] = useState(gap?.from ?? m.today)
  const [end, setEnd] = useState(gap?.to ?? m.today)
  const [mode, setMode] = useState<'incremental' | 'full'>(
    (gap?.missing_days ?? 0) > 365 ? 'full' : 'incremental',
  )
  const [busy, setBusy] = useState(false)
  const isCn = m.market === 'CN'

  async function submit() {
    setBusy(true)
    try {
      if (isCn) {
        // A 股没有 market-daily 端点, 日K补拉 + enriched 重算走盘后管道
        await api.pipelineRun()
        toast('已触发 A 股盘后管道，可在任务中心查看进度', 'success')
      } else {
        await api.marketDailyRun({
          market: m.market as 'HK' | 'US',
          start_date: start,
          end_date: end,
          mode,
        })
        toast(`已提交 ${marketName(m)} ${start} ~ ${end} 同步任务`, 'success')
      }
      onClose()
    } catch (e: any) {
      toast(typeof e?.message === 'string' ? e.message : '提交失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal onClose={onClose} ariaLabel={`补拉${marketName(m)}数据`} panelClassName="w-[420px] rounded-lg border border-border bg-background p-4 shadow-xl">
      <div className="mb-3 flex items-center gap-2">
        <Download className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-medium">补拉 {marketName(m)} 数据</h2>
      </div>

      <div className="mb-3 rounded border border-border/60 bg-elevated/50 px-2.5 py-2 text-[11px] leading-relaxed text-foreground/70">
        当前最新 <span className="font-mono">{m.latest_date ?? '—'}</span>
        {m.stale_days != null && m.stale_days > 0 && <> · 落后 {m.stale_days} 天（容差 {m.tolerance_days} 天）</>}
        {m.history_insufficient && <> · 历史仅 {m.history_days} 天，建议补一整年</>}
        {m.status === 'behind_raw' && <> · 原始数据已到 {m.raw_latest_date}，只是还没算成 enriched</>}
      </div>

      {isCn ? (
        <p className="mb-3 text-[11px] text-muted">
          A 股日K与指标由盘后管道统一处理，将触发完整管道（含 enriched 重算）。
        </p>
      ) : (
        <>
          <div className="mb-2 flex items-end gap-2">
            <div className="flex-1">
              <div className="mb-1 text-[11px] text-muted">起始日</div>
              <DatePicker value={start} onChange={setStart} max={end} />
            </div>
            <div className="flex-1">
              <div className="mb-1 text-[11px] text-muted">结束日</div>
              <DatePicker value={end} onChange={setEnd} min={start} max={m.today} />
            </div>
          </div>
          <div className="mb-2 flex gap-1.5">
            {[
              { key: 'suggest', label: '建议区间' },
              { key: 'y1', label: '近一年' },
              { key: 'y3', label: '近三年' },
            ].map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => {
                  const to = m.today
                  const from =
                    p.key === 'suggest' ? (gap?.from ?? to)
                      : p.key === 'y1' ? shiftYear(to, 1)
                        : shiftYear(to, 3)
                  setStart(from)
                  setEnd(to)
                }}
                className="rounded border border-border px-2 py-0.5 text-[11px] text-foreground/70 hover:bg-elevated"
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="mb-3 flex items-center gap-3 text-[11px]">
            <span className="text-muted">模式</span>
            {(['incremental', 'full'] as const).map((v) => (
              <label key={v} className="flex items-center gap-1 text-foreground/80">
                <input
                  type="radio"
                  checked={mode === v}
                  onChange={() => setMode(v)}
                  className="accent-accent"
                />
                {v === 'incremental' ? '增量' : '全量重拉'}
              </label>
            ))}
          </div>
        </>
      )}

      <div className="flex justify-end gap-2">
        <button type="button" onClick={onClose} className="rounded px-2.5 py-1 text-xs text-muted hover:bg-elevated">
          取消
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy}
          className="flex items-center gap-1 rounded bg-accent px-2.5 py-1 text-xs text-white disabled:opacity-60"
        >
          {busy && <Loader2 className="h-3 w-3 animate-spin" />}
          开始拉取
        </button>
      </div>
    </Modal>
  )
}

function shiftYear(iso: string, years: number): string {
  const d = new Date(`${iso}T00:00:00`)
  d.setFullYear(d.getFullYear() - years)
  return d.toISOString().slice(0, 10)
}

export function DataFreshnessBar() {
  // 有任务在跑时 3s 轮询, 空闲时 30s
  const [polling, setPolling] = useState<number>(30_000)
  const { data } = useDataFreshness({ refetchInterval: polling })
  const job = data?.active_job ?? null
  const [target, setTarget] = useState<MarketFreshness | null>(null)

  useEffect(() => {
    setPolling(job ? 3_000 : 30_000)
  }, [job?.id])

  const markets = data?.markets ?? []
  const needBackfill = markets.filter((m) => m.status !== 'ok' && m.gap)

  if (!data) return null

  return (
    <>
      <div
        role="status"
        aria-live="polite"
        className="sticky bottom-0 z-40 flex items-center gap-3 border-t border-border bg-background/95 px-3 py-1 text-[11px] backdrop-blur-sm"
      >
        {job ? (
          <span className="flex min-w-0 items-center gap-1.5 text-accent">
            <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
            <span className="shrink-0">
              正在同步 {job.market ? MARKET_LABEL[job.market] ?? job.market : ''}
            </span>
            <span className="truncate text-foreground/70">{job.message ?? job.stage ?? ''}</span>
            {job.progress != null && (
              <span className="shrink-0 font-mono text-foreground/60">{job.progress}%</span>
            )}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-muted">
            {needBackfill.length > 0
              ? <AlertTriangle className="h-3 w-3 shrink-0 text-warning" />
              : <CheckCircle2 className="h-3 w-3 shrink-0 text-bull" />}
            数据
          </span>
        )}

        <span className="flex items-center gap-3 overflow-x-auto">
          {markets.map((m) => (
            <MarketChip key={m.market} m={m} />
          ))}
        </span>

        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {needBackfill.map((m) => (
            <button
              key={m.market}
              type="button"
              onClick={() => setTarget(m)}
              className="flex items-center gap-1 rounded border border-warning/40 px-1.5 py-0.5 text-warning hover:bg-warning/10"
              title={`${marketName(m)} 缺 ${m.gap?.from} ~ ${m.gap?.to}`}
            >
              <Database className="h-3 w-3" />
              补 {marketName(m)}
            </button>
          ))}
        </span>
      </div>

      {target && <BackfillDialog m={target} onClose={() => setTarget(null)} />}
    </>
  )
}
