import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ScanSearch, History, RefreshCw, Loader2, Database, Settings2 } from 'lucide-react'
import { api, type InternationalMarket } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { StrategySettingsDialog } from '@/components/screener/StrategySettingsDialog'
import { ConceptHeatStatus, conceptHeatNeedsRefresh, useConceptMarketDate } from '@/components/screener/ConceptHeatStatus'

interface MarketScreenerProps {
  market: InternationalMarket
}

interface ScreenerRow {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  score?: number | null
}

function fmtPct(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '—'
  return (value >= 0 ? '+' : '') + (value * 100).toFixed(2) + '%'
}

function pctColor(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return 'text-muted'
  if (value > 0) return 'text-bull'
  if (value < 0) return 'text-bear'
  return 'text-foreground'
}

export function MarketScreener({ market }: MarketScreenerProps) {
  return <MarketScreenerContent key={market} market={market} />
}

function MarketScreenerContent({ market }: MarketScreenerProps) {
  const label = market === 'hk' ? '港股' : '美股'
  const currency = market === 'hk' ? 'HKD' : 'USD'
  const navigate = useNavigate()
  const [strategyId, setStrategyId] = useState('')
  const [runRequested, setRunRequested] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const currentMarketDate = useConceptMarketDate(market)

  const poolQuery = useQuery({
    queryKey: QK.marketStocks(market),
    queryFn: () => api.marketStocks(market),
    staleTime: 60_000,
  })
  const poolSymbols = useMemo(
    () => (poolQuery.data?.results ?? []).map((stock) => stock.symbol),
    [poolQuery.data],
  )
  const strategiesQuery = useQuery({
    queryKey: QK.screenerStrategies(market),
    queryFn: () => api.screenerStrategies(market),
    staleTime: 120_000,
  })
  const strategies = useMemo(
    () => (strategiesQuery.data?.presets ?? []).filter((strategy) => strategy.asset_types.includes(market)),
    [strategiesQuery.data, market],
  )
  const canRun = poolQuery.isSuccess && poolSymbols.length > 0 && strategies.some((strategy) => strategy.id === strategyId)
  const dataStatusQuery = useQuery({
    queryKey: QK.marketDataStatus(market),
    queryFn: () => api.marketDataStatus(market),
    enabled: settingsOpen,
    staleTime: 60_000,
    refetchInterval: settingsOpen ? 60_000 : false,
  })
  const runQuery = useQuery({
    queryKey: QK.marketScreener(market, strategyId, poolSymbols, currentMarketDate),
    queryFn: () => api.screenerRunPreset(strategyId, poolSymbols, undefined, undefined, market),
    enabled: runRequested && canRun,
    retry: false,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })

  const result = runRequested && !runQuery.isError && !conceptHeatNeedsRefresh(runQuery.data?.concept_heat_metadata, currentMarketDate) ? runQuery.data : undefined
  const settingsAsOf = dataStatusQuery.data?.enriched.target_last_date ?? dataStatusQuery.data?.enriched.last_date ?? result?.as_of
  const resultMetadata = runRequested ? runQuery.data?.concept_heat_metadata : undefined
  const rows: ScreenerRow[] = result?.rows ?? []
  const hasScores = rows.some((row) => row.score != null && Number.isFinite(row.score))
  const strategyName = strategies.find((strategy) => strategy.id === strategyId)?.name ?? result?.strategy ?? ''

  const goBacktest = (symbols: string[]) => {
    const query = new URLSearchParams({
      tab: 'strategy',
      market,
      strategy_id: strategyId,
      symbols: symbols.join(','),
    })
    navigate('/backtest?' + query.toString())
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-surface/60">
      <div className="flex flex-wrap items-center gap-2 border-b border-border/50 px-4 py-3">
        <ScanSearch className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <h3 className="text-sm font-medium text-foreground">
          {label}策略筛选
          <span className="ml-2 text-xs font-normal text-muted">
            当前股票池 · {poolQuery.isPending ? '加载中…' : poolSymbols.length + ' 只'}
          </span>
        </h3>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Link to={'/' + market + '/data'} className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary hover:border-accent/40 hover:text-accent">
            <Database className="h-3.5 w-3.5" aria-hidden="true" />
            检查与同步数据
          </Link>
          <button
            type="button"
            onClick={() => { setRunRequested(true); void runQuery.refetch() }}
            disabled={!canRun || runQuery.isFetching}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {runQuery.isFetching ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            运行筛选
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-border/50 px-4 py-2.5">
        <label className="text-xs text-muted" htmlFor={market + '-strategy'}>策略</label>
        <select
          id={market + '-strategy'}
          value={strategyId}
          onChange={(event) => { setRunRequested(false); setStrategyId(event.target.value) }}
          disabled={strategiesQuery.isPending}
          className="w-full min-w-0 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-xs text-foreground focus:border-accent/50 focus:outline-none sm:w-64"
        >
          <option value="">{strategiesQuery.isPending ? '正在加载策略…' : '选择策略…'}</option>
          {strategies.map((strategy) => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}
        </select>
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          disabled={!strategies.some(strategy => strategy.id === strategyId)}
          className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border px-2.5 text-xs text-secondary hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
          aria-label={`配置${label}策略`}
          title={strategyId ? '配置当前策略的参数与评分权重' : '请先选择策略'}
        >
          <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />策略设置
        </button>
        <span className="text-[11px] text-muted">使用当前市场的本地日线与策略指标。</span>
      </div>

      <div className="px-4 py-3">
        {poolQuery.isPending && <p className="py-3 text-xs text-muted" role="status">加载当前股票池…</p>}
        {(poolQuery.isError || strategiesQuery.isError) && (
          <div className="py-3 text-xs text-danger" role="alert">
            加载失败：{poolQuery.error?.message ?? strategiesQuery.error?.message}
            <button type="button" onClick={() => { void Promise.all([poolQuery.refetch(), strategiesQuery.refetch()]) }} className="ml-2 underline">重试</button>
          </div>
        )}
        {poolQuery.isSuccess && poolSymbols.length === 0 && <p className="py-3 text-xs text-muted">当前市场股票池为空，请先配置标的并准备日线数据。</p>}
        {strategiesQuery.isSuccess && strategies.length === 0 && <p className="py-3 text-xs text-muted">当前市场暂无可用策略。</p>}
        {runRequested && runQuery.isFetching && <p className="py-3 text-xs text-muted" role="status">正在筛选 {poolSymbols.length} 只标的…</p>}
        {runRequested && runQuery.isError && (
          <p className="break-words py-3 text-xs text-danger" role="alert">
            筛选失败：{runQuery.error.message}
          </p>
        )}
        {!!result?.warnings?.length && (
          <div className="mb-3 rounded border border-warning/20 bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning" role="status">
            <p className="font-medium">数据与评分提示</p>
            <ul className="mt-1 space-y-1">{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
          </div>
        )}
        {result && (
          <ConceptHeatStatus metadata={resultMetadata} currentMarketDate={currentMarketDate} />
        )}
        {runQuery.isError && resultMetadata && (
          <ConceptHeatStatus metadata={{ ...resultMetadata, status: 'unavailable', reason: '本次筛选失败，请处理上方提示后重新运行。' }} currentMarketDate={currentMarketDate} />
        )}
        {result && (
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
            <span>{strategyName} · {result.as_of} · 命中 {result.total} 只</span>
            {rows.length > 0 && (
              <button type="button" onClick={() => goBacktest(rows.map((row) => row.symbol))} className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1.5 text-secondary hover:border-accent/40 hover:text-accent">
                <History className="h-3 w-3" aria-hidden="true" />
                回测此候选集
              </button>
            )}
          </div>
        )}
        {result && rows.length === 0 && <p className="py-3 text-xs text-muted">当前策略没有匹配标的。可检查数据覆盖和上方提示后重新运行。</p>}
        {result && rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full whitespace-nowrap text-sm">
              <thead className="border-b border-border text-muted">
                <tr>
                  <th className="py-2 pr-3 text-left">代码</th>
                  <th className="py-2 pr-3 text-left">名称</th>
                  <th className="py-2 pr-3 text-right">日线价格（{currency}）</th>
                  <th className="py-2 pr-3 text-right">涨跌幅</th>
                  {hasScores && <th className="py-2 pr-3 text-right">评分</th>}
                  <th className="py-2 text-right">回测</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.symbol} className="border-b border-border/40 hover:bg-elevated/40">
                    <td className="py-2 pr-3 font-mono text-foreground">{row.symbol}</td>
                    <td className="max-w-40 truncate py-2 pr-3 text-foreground" title={row.name ?? undefined}>{row.name ?? '—'}</td>
                    <td className={cn('py-2 pr-3 text-right font-mono tabular-nums', pctColor(row.change_pct))}>
                      {row.close != null && Number.isFinite(row.close) ? row.close.toFixed(2) : '—'}
                    </td>
                    <td className={cn('py-2 pr-3 text-right font-mono tabular-nums', pctColor(row.change_pct))}>{fmtPct(row.change_pct)}</td>
                    {hasScores && <td className="py-2 pr-3 text-right font-mono tabular-nums text-foreground">{row.score != null && Number.isFinite(row.score) ? row.score.toFixed(2) : '—'}</td>}
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        onClick={() => goBacktest([row.symbol])}
                        className="inline-flex h-8 items-center gap-1 rounded-btn border border-border px-2 text-[11px] text-secondary hover:border-accent/40 hover:text-accent"
                        title={'使用 ' + strategyName + ' 回测 ' + row.symbol}
                      >
                        <History className="h-3 w-3" aria-hidden="true" />
                        回测
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-muted">策略中的行业限制属于候选集行业约束。回测会按历史数据重新生成信号，历史行业或估值数据不可追溯时会明确提示。</p>
      </div>
      <StrategySettingsDialog
        strategyId={settingsOpen ? strategyId : null}
        assetType={market}
        context="current"
        asOf={settingsAsOf}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => setRunRequested(true)}
        onDeleted={() => { setRunRequested(false); setStrategyId('') }}
      />
    </div>
  )
}
