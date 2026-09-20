import { useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Database, Download, Loader2, RefreshCw } from 'lucide-react'
import {
  api, marketDataSourceLabel, marketFinancialFieldLabel, marketPriceBasisLabel,
  type InternationalMarket, type MarketDataCoverage, type MarketDataSource,
  type MarketDataStatusResponse, type MarketDataSyncItem, type MarketDataSyncResult,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { FINANCIAL_QK } from '@/lib/useFinancials'

type SyncOperation = MarketDataSyncResult['operation']

const OPERATION_LABELS: Record<SyncOperation, string> = {
  daily_download: '下载日 K',
  enriched_recompute: '重算策略指标',
  lot_size_sync: '同步每手股数',
  financial_sync: '同步历史财务',
}

const STATUS_LABELS = {
  completed: '已完成',
  completed_with_errors: '部分完成',
  empty: '没有可处理的数据',
  unsupported: '当前数据源不支持',
  failed: '执行失败',
  unchanged: '覆盖已确认，无需更新',
}

const ITEM_STATUS_LABELS: Record<string, string> = {
  ok: '完成',
  updated: '已更新',
  unchanged: '无需更新',
  partial: '部分完成',
  failed: '失败',
  skipped: '跳过',
  missing: '缺少资料',
  conflict: '资料冲突',
  future_snapshot: '资料尚未生效',
}

function count(value: number | undefined) {
  return value == null ? '未返回' : value.toLocaleString('zh-CN')
}

function dateRange(start: string | null | undefined, end: string | null | undefined) {
  if (!start && !end) return '暂无记录'
  return start === end ? start : (start ?? '起始日未记录') + ' 至 ' + (end ?? '截止日未记录')
}

function sourceNames(sources: string[]) {
  return [...new Set(sources.map(marketDataSourceLabel))].join('、') || '来源未记录'
}

function SourceList({ title, sources }: {
  title: string
  sources: (MarketDataSource & { role?: 'primary' | 'fallback' })[] | undefined
}) {
  return (
    <div className="min-w-0">
      <h4 className="text-xs font-medium text-secondary">{title}</h4>
      {sources?.length ? (
        <ul className="mt-2 space-y-2 text-xs">
          {sources.map((source) => (
            <li key={source.id} className="min-w-0 break-words">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {source.role && <span className="rounded bg-elevated px-1.5 py-0.5 text-[11px] text-muted">{source.role === 'primary' ? '主源' : '备用'}</span>}
                <span className="text-foreground">{source.label || marketDataSourceLabel(source.id)}</span>
                <span className={source.available ? 'text-secondary' : 'text-warning'}>{source.available ? '具备接入能力' : '当前不可用'}</span>
              </div>
              {source.reason && <p className="mt-1 text-[11px] leading-relaxed text-muted">{source.reason}</p>}
            </li>
          ))}
        </ul>
      ) : <p className="mt-2 text-[11px] text-muted">暂未返回来源状态。</p>}
    </div>
  )
}

function HKDataDetails({ coverage }: { coverage: MarketDataStatusResponse }) {
  const financials = coverage.financials
  const audit = coverage.price_audit
  const fields = Object.entries(financials?.fields ?? {})

  return (
    <div className="mt-3 space-y-3">
      <div className="grid gap-3 lg:grid-cols-2">
        <div className="min-w-0 rounded border border-border/60 p-3">
          <h3 className="text-sm font-medium text-foreground">日线与复权来源</h3>
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <SourceList title="日线价格" sources={coverage.daily_sources} />
            <SourceList title="复权资料" sources={coverage.adjustment_sources} />
            {coverage.verification_sources && <div className="sm:col-span-2"><SourceList title="争议行情核验" sources={coverage.verification_sources} /></div>}
          </div>
          <p className="mt-3 text-[11px] leading-relaxed text-muted">接入能力不代表本次更新成功。实际使用的来源、日期覆盖和未完成阶段见同步结果；日线备用成功后仍需有覆盖对应日期的复权资料。</p>
        </div>
        <div className="min-w-0 rounded border border-border/60 p-3">
          <h3 className="text-sm font-medium text-foreground">价格口径核查</h3>
          {audit ? (
            <>
              <p className={cn('mt-2 text-xs', audit.status === 'verified' ? 'text-secondary' : 'text-warning')}>
                {audit.status === 'verified' ? '已核实记录范围内的价格口径' : audit.status === 'partial' ? '部分价格口径已核实' : '价格口径尚未核实'}
              </p>
              <p className="mt-2 text-xs leading-relaxed text-secondary">
                有核实记录 {count(audit.verified_symbols)} 只 · 待核实 {count(audit.unknown_symbols)} 只 · 口径混合 {count(audit.mixed_basis_symbols)} 只
              </p>
              <p className="mt-2 break-words text-[11px] text-muted">复权资料：{sourceNames(audit.adjustment_sources)}</p>
              <p className="mt-1 break-words text-[11px] text-muted">最近核查：{audit.last_checked_at ?? '暂无记录'}</p>
              <p className="mt-1 text-[11px] leading-relaxed text-muted">统计仅代表已记录的核查范围，不代表每只股票的全部历史均已验证。回测结果会列出本次使用的核实区间。</p>
              {audit.warnings.length > 0 && <ul className="mt-2 space-y-1 break-words text-xs leading-relaxed text-warning">{audit.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
            </>
          ) : <p className="mt-2 text-xs text-muted">暂未返回价格核查记录。</p>}
        </div>
      </div>
      <div className="min-w-0 rounded border border-border/60 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium text-foreground">历史财务覆盖</h3>
          {financials && <span className={cn('text-xs', financials.status === 'available' ? 'text-secondary' : 'text-warning')}>{financials.status === 'available' ? '已有历史数据' : financials.status === 'partial' ? '部分可用' : '暂无可用历史'}</span>}
        </div>
        {financials ? (
          <>
            <p className="mt-2 text-xs text-secondary">已有 {count(financials.symbols)} 只标的 · {count(financials.rows)} 个报告版本</p>
            <div className="mt-2 grid gap-x-6 gap-y-1 text-[11px] text-muted sm:grid-cols-2">
              <p className="break-words">报告期：{dateRange(financials.first_period_end, financials.last_period_end)}</p>
              <p className="break-words">公告日期：{dateRange(financials.first_announce_date, financials.last_announce_date)}</p>
              <p className="break-words sm:col-span-2">实际来源：{sourceNames(financials.sources)}</p>
            </div>
            {financials.reason && <p className="mt-2 break-words text-xs leading-relaxed text-warning">{financials.reason}</p>}
            {fields.length > 0 ? (
              <details className="mt-3 text-xs">
                <summary className="cursor-pointer text-secondary">查看财务字段覆盖与缺口（{fields.filter(([, field]) => field.available_symbols > 0).length} 项已有数据）</summary>
                <ul className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {fields.map(([name, field]) => (
                    <li key={name} className="min-w-0 rounded bg-base/50 p-2.5">
                      <p className="font-medium text-foreground">{marketFinancialFieldLabel(name)}</p>
                      <p className="mt-1 text-secondary">可用 {count(field.available_symbols)} 只 · 缺少 {count(field.missing_symbols)} 只</p>
                      <p className="mt-1 break-words text-[11px] text-muted">公告：{dateRange(field.first_announce_date, field.last_announce_date)}</p>
                    </li>
                  ))}
                </ul>
              </details>
            ) : <p className="mt-2 text-xs text-muted">暂无可用字段记录。</p>}
            <p className="mt-2 text-[11px] leading-relaxed text-muted">历史筛选只使用公告日之后已公开的报告版本。字段、币种或每股口径不足时保留缺失，不以最新财务填补过去。</p>
          </>
        ) : <p className="mt-2 text-xs text-muted">暂未返回历史财务覆盖。</p>}
      </div>
    </div>
  )
}

function SyncItems({ items }: { items: MarketDataSyncItem[] }) {
  const [visibleCount, setVisibleCount] = useState(30)
  return (
    <details className="mt-2">
      <summary className="cursor-pointer">查看标的处理详情（{items.length}）</summary>
      <ul className="mt-2 max-h-80 space-y-2 overflow-y-auto break-words">
        {items.slice(0, visibleCount).map((item, index) => (
          <li key={item.symbol + '-' + index} className="rounded border border-border/60 p-2 leading-relaxed">
            <p className="font-medium"><span className="font-mono">{item.symbol}</span> · {item.applicability === 'verified_not_applicable' ? '已核实不适用' : ITEM_STATUS_LABELS[item.status] ?? '状态未说明'}{item.fallback_used && ' · 使用备用来源'}</p>
            {item.reason && <p className="mt-1">{item.reason}</p>}
            {item.source && <p className="mt-1">实际来源：{marketDataSourceLabel(item.source)}</p>}
            {!!item.attempted_sources?.length && <p>尝试来源：{sourceNames(item.attempted_sources)}</p>}
            {(item.requested_start !== undefined || item.requested_end !== undefined) && <p>请求日期：{dateRange(item.requested_start, item.requested_end)}</p>}
            {(item.actual_start !== undefined || item.actual_end !== undefined) && <p>实际覆盖：{dateRange(item.actual_start, item.actual_end)}</p>}
            {item.currency && <p>柜台币种：{item.currency}{item.volume_unit === 'share' ? ' · 成交量按股记录' : ''}</p>}
            {item.price_adjustment && <p>价格：{marketPriceBasisLabel(item.price_adjustment)}</p>}
            {item.adjustment_source && <p>复权资料：{marketDataSourceLabel(item.adjustment_source)}</p>}
            {item.verification_source && <p>争议日核验：{marketDataSourceLabel(item.verification_source)} · {item.verification_cached ? '含已保存的历史资料' : '本次独立来源核验'}</p>}
            {!!item.source_conflicts?.length && (
              <details className="mt-1">
                <summary className="cursor-pointer">查看争议日采用来源（{item.source_conflicts.length} 日）</summary>
                <ul className="mt-1 space-y-1 pl-2">
                  {item.source_conflicts.map((conflict, conflictIndex) => (
                    <li key={conflict.date + '-' + conflictIndex}>
                      <p>{conflict.date}：采用{marketDataSourceLabel(conflict.selected_source)}</p>
                      <p className="text-muted">{conflict.verification_cached ? '使用历史核验资料，原观测时间' : '核验资料观察时间'}：{conflict.observed_at ?? '未记录'}</p>
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {(item.raw_updated !== undefined || item.enriched_updated !== undefined) && (
              <p>{item.raw_updated !== undefined && (item.raw_updated ? '日 K 已更新' : '日 K 未变更')}{item.raw_updated !== undefined && item.enriched_updated !== undefined && ' · '}{item.enriched_updated !== undefined && (item.enriched_updated ? '策略指标已更新' : '策略指标未变更')}</p>
            )}
            {item.source_as_of && <p>来源资料日期：{item.source_as_of}</p>}
            {item.observed_at && <p>资料观察时间：{item.observed_at}</p>}
            {!!item.fields_available?.length && <p>可用字段：{item.fields_available.map(marketFinancialFieldLabel).join('、')}</p>}
            {!!item.fields_missing?.length && <p>缺少字段：{item.fields_missing.map(marketFinancialFieldLabel).join('、')}</p>}
          </li>
        ))}
      </ul>
      {visibleCount < items.length && <button type="button" onClick={() => setVisibleCount((value) => value + 30)} className="mt-2 underline">显示更多（还有 {items.length - visibleCount} 项）</button>}
    </details>
  )
}

function initialDates(market: InternationalMarket) {
  const end = new Intl.DateTimeFormat('en-CA', {
    timeZone: market === 'hk' ? 'Asia/Hong_Kong' : 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
  const start = new Date(end + 'T00:00:00Z')
  start.setUTCDate(start.getUTCDate() - 365)
  return { start: start.toISOString().slice(0, 10), end }
}

function CoverageCard({ label, coverage, poolSize }: {
  label: string
  coverage: MarketDataCoverage
  poolSize: number
}) {
  return (
    <div className="min-w-0 rounded border border-border/60 p-3">
      <h3 className="text-xs text-secondary">{label}</h3>
      <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">
        {count(coverage.target_symbols)} / {count(poolSize)}
        <span className="ml-1 text-xs font-normal text-muted">只当前池标的</span>
      </p>
      <p className="mt-1 text-xs text-secondary">
        缺少 {count(coverage.missing_symbols)} 只 · 当前池 {count(coverage.target_rows)} 行
      </p>
      <p className="mt-2 text-[11px] text-muted">当前池最近记录：{coverage.target_last_date ?? '暂无'}</p>
      <p className="mt-1 text-[11px] text-muted">
        池外存量 {count(coverage.extra_symbols)} 只 · 全部存量 {count(coverage.rows)} 行
      </p>
      <p className="mt-1 break-words text-[11px] text-muted">
        存量日期：{coverage.first_date ?? '暂无'} 至 {coverage.last_date ?? '暂无'}
      </p>
    </div>
  )
}

export function MarketDataStatus({ market }: { market: InternationalMarket }) {
  return <MarketDataStatusContent key={market} market={market} />
}

function MarketDataStatusContent({ market }: { market: InternationalMarket }) {
  const label = market === 'hk' ? '港股' : '美股'
  const qc = useQueryClient()
  const [dates] = useState(() => initialDates(market))
  const [startDate, setStartDate] = useState(dates.start)
  const [endDate, setEndDate] = useState(dates.end)
  const [jobId, setJobId] = useState<string | null>(null)
  const processedJob = useRef<string | null>(null)

  const status = useQuery({
    queryKey: QK.marketDataStatus(market),
    queryFn: () => api.marketDataStatus(market),
    staleTime: 30_000,
  })

  const refreshMarket = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: QK.marketDataStatus(market) }),
      qc.invalidateQueries({ queryKey: QK.marketStocks(market) }),
      qc.invalidateQueries({ queryKey: QK.marketScreener(market) }),
      qc.invalidateQueries({ queryKey: QK.screenerStrategies(market) }),
      qc.invalidateQueries({
        queryKey: QK.scoringColumnsRoot,
        predicate: (query) => query.queryKey[1] === market,
      }),
      qc.invalidateQueries({ queryKey: QK.capabilities }),
      qc.invalidateQueries({
        predicate: (query) => query.queryKey[0] === QK.kline('', '', '')[0]
          && typeof query.queryKey[1] === 'string'
          && (market === 'hk' ? query.queryKey[1].endsWith('.HK') : query.queryKey[1].endsWith('.US')),
      }),
      ...(market === 'hk' ? [
        qc.invalidateQueries({ queryKey: FINANCIAL_QK.status }),
        qc.invalidateQueries({
          predicate: (query) => query.queryKey[0] === FINANCIAL_QK.metrics()[0]
            && typeof query.queryKey[2] === 'string' && query.queryKey[2].endsWith('.HK'),
        }),
      ] : []),
      qc.invalidateQueries({ queryKey: QK.pipelineJobs }),
    ])
  }, [market, qc])

  const sync = useMutation({
    mutationFn: (operation: SyncOperation) => {
      if (operation === 'daily_download') return api.marketSyncDaily(market, { start: startDate, end: endDate })
      if (operation === 'enriched_recompute') return api.marketSyncEnriched(market)
      if (operation === 'financial_sync') return api.marketSyncFinancials()
      return api.marketSyncLotSizes()
    },
    onMutate: () => {
      setJobId(null)
      processedJob.current = null
    },
    onSuccess: async (result) => {
      if (result.status === 'started') {
        setJobId(result.job_id ?? null)
        await qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      } else {
        await refreshMarket()
      }
    },
  })

  const job = useQuery({
    queryKey: QK.pipelineJob(jobId ?? ''),
    queryFn: () => api.pipelineJob(jobId!),
    enabled: !!jobId,
    retry: false,
    refetchInterval: (query) => {
      if (query.state.error) return false
      const state = query.state.data?.status
      return state === 'succeeded' || state === 'failed' ? false : 1500
    },
  })
  const jobFinished = job.data?.status === 'succeeded' || job.data?.status === 'failed'
  const busy = sync.isPending || (!!jobId && !jobFinished)

  useEffect(() => {
    if (!jobId || !jobFinished || processedJob.current === jobId) return
    processedJob.current = jobId
    void refreshMarket()
  }, [jobId, jobFinished, refreshMarket])

  const coverage = status.data
  const capabilities = coverage?.capabilities
  const poolSize = coverage?.instruments.symbols ?? 0
  const invalidDates = !startDate || !endDate || startDate > endDate
  const result = sync.isPending ? undefined : sync.data?.status === 'started'
    ? (jobFinished ? job.data?.result : undefined)
    : sync.data
  const reportedStatus = job.data?.status === 'failed' ? 'failed' : result?.status
  const finalStatus = (reportedStatus === 'completed' || reportedStatus === 'unchanged')
    && ((result?.failed ?? 0) > 0 || result?.items?.some((item) => item.status === 'partial' || item.status === 'failed'
      || (item.status === 'skipped' && item.applicability !== 'verified_not_applicable')))
    ? 'completed_with_errors' : reportedStatus
  const fullyCompleted = finalStatus === 'completed' || finalStatus === 'unchanged'
  const resultLabel = finalStatus && finalStatus in STATUS_LABELS
    ? STATUS_LABELS[finalStatus as keyof typeof STATUS_LABELS]
    : '任务已结束，结果状态不可用'
  const failures = result?.failures ?? Object.entries(job.data?.result?.provider_errors ?? {})
    .map(([symbol, reason]) => ({ symbol, reason }))
  const items = result?.items ?? []
  const itemSymbols = new Set(items.map((item) => item.symbol))
  const failureSymbols = new Set(failures.map((item) => item.symbol))
  const details: MarketDataSyncItem[] = [
    ...items,
    ...failures.filter((failure) => !itemSymbols.has(failure.symbol))
      .map((failure) => ({ ...failure, status: 'failed' })),
    ...(job.data?.result?.skipped_symbols ?? [])
      .filter((symbol) => !itemSymbols.has(symbol) && !failureSymbols.has(symbol))
      .map((symbol) => ({ symbol, status: 'skipped', reason: '未返回跳过原因。' })),
  ]
  const operation = sync.data?.operation ?? sync.variables
  const resultMessage = result?.message ?? job.data?.error

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Database className="h-4 w-4 text-accent" aria-hidden="true" />
        <h2 className="text-sm font-semibold text-foreground">{label}数据状态与同步</h2>
        <button
          type="button"
          onClick={() => { void status.refetch() }}
          disabled={status.isFetching}
          className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1.5 text-xs text-secondary disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', status.isFetching && 'animate-spin')} aria-hidden="true" />
          刷新状态
        </button>
      </div>

      {status.isPending && <p className="mt-3 text-xs text-muted" role="status">正在读取本地数据覆盖…</p>}
      {status.isError && (
        <p className="mt-3 break-words text-xs text-danger" role="alert">
          数据状态读取失败：{status.error.message}{coverage ? '；下方保留上次读取结果。' : '，可点击刷新重试。'}
        </p>
      )}
      {coverage && (
        <>
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            <div className="min-w-0 rounded border border-border/60 p-3">
              <h3 className="text-xs text-secondary">当前股票池</h3>
              <p className="mt-1 text-lg font-semibold tabular-nums text-foreground">{count(poolSize)} <span className="text-xs font-normal text-muted">只</span></p>
              <p className="mt-2 break-words text-[11px] text-muted">日 K 数据源：{capabilities?.daily_provider ? marketDataSourceLabel(capabilities.daily_provider) : '未配置'}</p>
              <p className="mt-1 break-words text-[11px] text-muted">存量来源：{coverage.source.length ? sourceNames(coverage.source) : '暂无'}</p>
              <p className="mt-1 text-[11px] text-muted">{market === 'hk' ? '回测资金币种' : '计价币种'}：{coverage.currency}</p>
              {market === 'hk' && (
                <>
                  <p className="mt-2 text-xs text-secondary">
                    每手股数可用 {count(coverage.instruments.lot_size_available)} 只，缺少 {count(coverage.instruments.lot_size_missing)} 只
                  </p>
                  {coverage.instruments.verified_not_applicable !== undefined && (
                    <p className="mt-1 text-[11px] leading-relaxed text-muted">
                      已核实不适用 {count(coverage.instruments.verified_not_applicable)} 只（已退市或柜台交易已结束），保留在股票池中记录
                    </p>
                  )}
                  {(coverage.instruments.lot_size_future !== undefined || coverage.instruments.lot_size_conflicts !== undefined) && (
                    <p className="mt-1 text-[11px] leading-relaxed text-muted">
                      未来日期资料 {count(coverage.instruments.lot_size_future)} 只 · 资料冲突 {count(coverage.instruments.lot_size_conflicts)} 只
                    </p>
                  )}
                  {coverage.instruments.lot_size_as_of !== undefined && <p className="mt-1 text-[11px] text-muted">每手快照日期：{coverage.instruments.lot_size_as_of ?? '未记录'}</p>}
                  {coverage.instruments.currencies && (
                    <>
                      <p className="mt-2 text-[11px] leading-relaxed text-muted">
                        柜台币种：港币 {count(coverage.instruments.currencies.HKD)} · 人民币 {count(coverage.instruments.currencies.CNY)} · 美元 {count(coverage.instruments.currencies.USD)} · 未知 {count(coverage.instruments.currencies.unknown)}
                      </p>
                      <p className="mt-1 text-[11px] leading-relaxed text-muted">回测仅支持港币柜台；币种未知或每手资料尚未生效、存在冲突时不成交。</p>
                    </>
                  )}
                </>
              )}
            </div>
            <CoverageCard label="日 K 覆盖" coverage={coverage.daily} poolSize={poolSize} />
            <CoverageCard label="策略指标覆盖" coverage={coverage.enriched} poolSize={poolSize} />
          </div>
          <p className="mt-2 text-[11px] text-muted">覆盖按标的统计，最近记录日期不代表每只标的都已更新至该日。</p>
          {market === 'hk' && <HKDataDetails coverage={coverage} />}
          {poolSize === 0 && <p className="mt-2 text-xs text-warning">当前市场股票池为空，请先配置标的后再同步。</p>}
          {coverage.warnings.length > 0 && (
            <ul className="mt-3 space-y-1 rounded bg-warning/10 p-3 text-xs leading-relaxed text-warning">
              {coverage.warnings.map((warning, index) => <li key={index}>{warning}</li>)}
            </ul>
          )}
        </>
      )}

      <div className="mt-4 border-t border-border/60 pt-3">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex min-w-0 flex-col gap-1 text-[11px] text-muted" htmlFor={market + '-daily-start'}>
            日 K 起始日期
            <input id={market + '-daily-start'} type="date" value={startDate} max={endDate} disabled={busy} onChange={(event) => setStartDate(event.target.value)} className="max-w-full rounded-input border border-border bg-base px-2 py-2 text-xs text-foreground" />
          </label>
          <label className="flex min-w-0 flex-col gap-1 text-[11px] text-muted" htmlFor={market + '-daily-end'}>
            截止日期
            <input id={market + '-daily-end'} type="date" value={endDate} min={startDate} disabled={busy} onChange={(event) => setEndDate(event.target.value)} className="max-w-full rounded-input border border-border bg-base px-2 py-2 text-xs text-foreground" />
          </label>
          <button
            type="button"
            onClick={() => sync.mutate('daily_download')}
            disabled={busy || !capabilities?.daily_download || poolSize === 0 || invalidDates}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-2 text-xs text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy && operation === 'daily_download' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
            下载当前池日 K
          </button>
          <button
            type="button"
            onClick={() => sync.mutate('enriched_recompute')}
            disabled={busy || !capabilities?.recompute_enriched || poolSize === 0}
            className="inline-flex items-center gap-1.5 rounded-btn border border-border px-3 py-2 text-xs text-secondary disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy && operation === 'enriched_recompute' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            重算策略指标
          </button>
          {market === 'hk' && (
            <>
              <button
                type="button"
                onClick={() => sync.mutate('lot_size_sync')}
                disabled={busy || !capabilities?.lot_size_sync || poolSize === 0}
                className="inline-flex items-center gap-1.5 rounded-btn border border-border px-3 py-2 text-xs text-secondary disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && operation === 'lot_size_sync' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                补齐港股每手股数
              </button>
              <button
                type="button"
                onClick={() => sync.mutate('financial_sync')}
                disabled={busy || !capabilities?.financial_history_sync || poolSize === 0}
                className="inline-flex items-center gap-1.5 rounded-btn border border-border px-3 py-2 text-xs text-secondary disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy && operation === 'financial_sync' && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                同步港股历史财务
              </button>
            </>
          )}
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-muted">日期范围用于下载日 K；重算指标使用当前股票池已保存的日 K。价格与复权口径以存储日线及数据来源说明为准。</p>
        {market === 'hk' && <p className="mt-1 text-[11px] leading-relaxed text-muted">每手股数同步仅补充港交所证券资料，未来资料日期不会覆盖已核实值；当前快照不代表历史每手规则。历史财务同步按当前池获取可追溯报告，不受上方日 K 日期限制。</p>}
        {invalidDates && <p className="mt-2 text-xs text-warning">请填写有效日期，起始日期不能晚于截止日期。</p>}
        {coverage && !capabilities?.daily_download && (
          <p className="mt-2 break-words text-xs text-warning">日 K 下载不可用：{capabilities?.daily_download_reason || '当前数据源未提供下载能力。'}</p>
        )}
        {coverage && !capabilities?.recompute_enriched && <p className="mt-2 text-xs text-warning">当前没有可重算的日 K，请先准备本市场数据。</p>}
        {market === 'hk' && coverage && !capabilities?.lot_size_sync && <p className="mt-2 text-xs text-warning">当前无法同步港交所每手股数元数据。</p>}
        {market === 'hk' && coverage && !capabilities?.financial_history_sync && (
          <p className="mt-2 break-words text-xs text-warning">历史财务同步不可用：{capabilities?.financial_history_reason || '当前未提供历史财务同步能力。'}</p>
        )}
      </div>

      <div className="mt-3 text-xs" aria-live="polite">
        {busy && (
          <p className="flex items-center gap-1.5 text-accent" role="status">
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
            {operation ? OPERATION_LABELS[operation] : '处理'}中…
            {job.data && <span>{Math.round(job.data.progress)}%</span>}
          </p>
        )}
        {job.isError && (
          <div className="mt-2 text-danger" role="alert">
            任务状态读取失败：{job.error.message}。任务可能仍在后台执行。
            <button type="button" onClick={() => { void job.refetch() }} className="ml-2 underline">重试读取状态</button>
          </div>
        )}
        {sync.isError && <p className="break-words text-danger" role="alert">操作失败：{sync.error.message}</p>}
        {sync.data?.status === 'started' && !jobId && <p className="text-danger" role="alert">提交响应未返回任务编号，无法确认执行结果，请查看数据任务列表。</p>}
        {jobFinished && !result && <p className="text-danger" role="alert">{job.data?.error || '任务未返回处理结果，无法确认成功数量。'}</p>}
        {result && (
          <div className={cn('rounded border p-3', fullyCompleted ? 'border-bear/30 bg-bear/5 text-bear' : finalStatus === 'failed' ? 'border-danger/30 bg-danger/5 text-danger' : 'border-warning/30 bg-warning/5 text-warning')}>
            <p className="flex items-center gap-1.5 font-medium">
              {!fullyCompleted && <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
              {operation ? OPERATION_LABELS[operation] + '：' : ''}{resultLabel}
            </p>
            <p className="mt-2 leading-relaxed">
              请求 {count(result.requested ?? job.data?.result?.symbols_total)} 只 ·
              成功 {count(result.succeeded ?? job.data?.result?.completed_symbols?.length)} 只 ·
              失败 {count(result.failed ?? job.data?.result?.failed_symbols?.length)} 只 ·
              跳过 {count(result.skipped ?? job.data?.result?.skipped_symbols?.length)} 只
            </p>
            {result.unchanged != null && <p className="mt-1">成功项中，{count(result.unchanged)} 只已确认覆盖、无需更新。</p>}
            {!!result.verified_not_applicable && <p className="mt-1">跳过项中，{count(result.verified_not_applicable)} 只已核实不适用，已有公告依据。</p>}
            {result.enriched_dates_written != null && result.enriched_dates_written > 0 && <p className="mt-1">已更新 {count(result.enriched_dates_written)} 个标的的指标分区。</p>}
            {(result.source || result.as_of) && <p className="mt-1">{result.source && '本次来源：' + marketDataSourceLabel(result.source)}{result.source && result.as_of && ' · '}{result.as_of && '资料日期：' + result.as_of}</p>}
            {jobFinished && job.data?.result?.last_success_at && <p className="mt-1">最近成功处理：{job.data.result.last_success_at}</p>}
            {resultMessage && <p className="mt-2 break-words">{resultMessage}</p>}
            {details.length > 0 && <SyncItems items={details} />}
          </div>
        )}
      </div>
    </section>
  )
}
