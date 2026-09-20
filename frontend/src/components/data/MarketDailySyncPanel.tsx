import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Loader2, Play, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

function isoDate(date: Date) {
  return date.toISOString().slice(0, 10)
}

function initialDates() {
  const end = new Date()
  const start = new Date(end)
  start.setMonth(start.getMonth() - 6)
  return { start: isoDate(start), end: isoDate(end) }
}

const errorLabels: Record<string, string> = {
  timeout: '超时',
  rate_limited: '限流',
  capability_denied: '能力不足',
  provider_error: '数据源错误',
  empty_result: '空结果',
  missing_from_result: '结果缺失',
  circuit_open: '熔断跳过',
}

export function MarketDailySyncPanel() {
  const qc = useQueryClient()
  const dates = useMemo(initialDates, [])
  const [market, setMarket] = useState<'HK' | 'US'>('HK')
  const [startDate, setStartDate] = useState(dates.start)
  const [endDate, setEndDate] = useState(dates.end)
  const [mode, setMode] = useState<'full' | 'incremental'>('incremental')
  const [jobId, setJobId] = useState<string | null>(null)

  const job = useQuery({
    queryKey: QK.pipelineJob(jobId ?? ''),
    queryFn: () => api.pipelineJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => {
      const status = q.state.data?.status
      return status === 'succeeded' || status === 'failed' ? false : 1500
    },
  })

  const run = useMutation({
    mutationFn: () => api.marketDailyRun({ market, start_date: startDate, end_date: endDate, mode }),
    onSuccess: (data) => {
      setJobId(data.job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
    onError: (error: Error) => toast(`启动${market}日K同步失败: ${error.message}`, 'error'),
  })

  const retry = useMutation({
    mutationFn: () => api.marketDailyRetry(jobId!),
    onSuccess: (data) => {
      setJobId(data.job_id)
      qc.invalidateQueries({ queryKey: QK.pipelineJobs })
    },
    onError: (error: Error) => toast(`重试失败: ${error.message}`, 'error'),
  })

  const result = job.data?.result
  const failed = result?.failed_symbols ?? []
  const completed = result?.completed_symbols?.length ?? 0
  const total = result?.symbols_total ?? (completed + failed.length)
  const running = job.data?.status === 'running' || job.data?.status === 'pending'
  const errors = Object.entries(result?.provider_errors ?? {}).reduce<Record<string, number>>((acc, [, code]) => {
    acc[code] = (acc[code] ?? 0) + 1
    return acc
  }, {})

  return (
    <section className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-3 mb-3">
        <div>
          <h3 className="text-sm font-medium text-foreground">港美股日K同步</h3>
          <p className="text-[10px] text-muted mt-1">自动读取真实标的池，按标的保存进度，失败后可只重试失败项</p>
        </div>
        {job.data && (
          <span className={`inline-flex items-center gap-1 text-[10px] ${running ? 'text-accent' : failed.length ? 'text-warning' : 'text-bear'}`}>
            {running ? <Loader2 className="h-3 w-3 animate-spin" /> : failed.length ? <AlertTriangle className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
            {running ? `${job.data.progress}% · ${job.data.stage}` : failed.length ? '部分失败' : '已完成'}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
        <select value={market} onChange={(e) => setMarket(e.target.value as 'HK' | 'US')} disabled={running} className="rounded-input bg-base border border-border px-2 py-2 text-xs">
          <option value="HK">港股 HK</option>
          <option value="US">美股 US</option>
        </select>
        <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={running} className="rounded-input bg-base border border-border px-2 py-2 text-xs" />
        <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} disabled={running} className="rounded-input bg-base border border-border px-2 py-2 text-xs" />
        <select value={mode} onChange={(e) => setMode(e.target.value as 'full' | 'incremental')} disabled={running} className="rounded-input bg-base border border-border px-2 py-2 text-xs">
          <option value="incremental">增量同步</option>
          <option value="full">全量同步</option>
        </select>
        <button onClick={() => run.mutate()} disabled={running || run.isPending || !startDate || !endDate} className="inline-flex items-center justify-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs font-medium text-white disabled:opacity-40">
          {run.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
          启动同步
        </button>
      </div>

      {job.data && (
        <div className="mt-3 border-t border-border/60 pt-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-secondary">
            <span>任务 <b className="font-mono text-foreground">{job.data.id}</b></span>
            <span>完成 {completed}{total ? ` / ${total}` : ''}</span>
            <span>失败 {failed.length}</span>
            {result?.last_success_symbol && <span>最近成功 <b className="font-mono text-foreground">{result.last_success_symbol}</b></span>}
          </div>
          {Object.keys(errors).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {Object.entries(errors).map(([code, count]) => <span key={code} className="rounded bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger">{errorLabels[code] ?? code}: {count}</span>)}
            </div>
          )}
          {failed.length > 0 && (
            <div className="mt-3 flex items-start justify-between gap-3">
              <div className="min-w-0 text-[10px] text-muted font-mono break-all">失败标的: {failed.slice(0, 30).join(', ')}{failed.length > 30 ? ` ... 共 ${failed.length} 只` : ''}</div>
              <button onClick={() => retry.mutate()} disabled={running || retry.isPending} className="inline-flex shrink-0 items-center gap-1 rounded-btn bg-warning/10 px-2 py-1 text-[10px] text-warning hover:bg-warning/20 disabled:opacity-40">
                <RefreshCw className={`h-3 w-3 ${retry.isPending ? 'animate-spin' : ''}`} />
                仅重试失败项
              </button>
            </div>
          )}
          {job.data.error && <div className="mt-2 text-[10px] text-danger">{job.data.error}</div>}
        </div>
      )}
    </section>
  )
}
