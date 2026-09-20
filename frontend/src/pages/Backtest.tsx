import { useEffect, useState } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { BarChart3, BookmarkCheck, FlaskConical, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { FactorDiscovery } from './backtest/FactorDiscovery'
import { ResearchCandidatesDialog } from './backtest/ResearchCandidatesDialog'
import { RobustnessValidation } from './backtest/RobustnessValidation'
import { StrategyBacktest } from './backtest/StrategyBacktest'
import { BACKTEST_MARKETS, backtestAssetFromQuery, isInternationalAsset } from '@/lib/backtestMarket'
import type { StrategyBacktestAsset } from '@/lib/api'

type Tab = 'factor' | 'strategy' | 'robustness'

const MODES: Record<Tab, { title: string; subtitle: string; icon: typeof BarChart3 }> = {
  factor: {
    title: '因子',
    subtitle: '批量筛选与单因子检验',
    icon: BarChart3,
  },
  strategy: {
    title: '策略',
    subtitle: '现有策略评估与候选沉淀',
    icon: FlaskConical,
  },
  robustness: {
    title: '验证',
    subtitle: '参数敏感性与滚动样本外',
    icon: ShieldCheck,
  },
}

export function Backtest() {
  const [searchParams, setSearchParams] = useSearchParams()
  const requestedTab = searchParams.get('tab')
  const assetType = backtestAssetFromQuery(searchParams)
  const international = isInternationalAsset(assetType)
  const [candidatesOpen, setCandidatesOpen] = useState(false)

  useEffect(() => { setCandidatesOpen(false) }, [assetType])
  const changeAsset = (asset: StrategyBacktestAsset) => {
    setSearchParams(new URLSearchParams({ tab: 'strategy', market: asset }), { replace: true })
  }

  // 旧链接兼容: 挖掘已升级为一级路由 /mining, 保留 run/candidate 参数重定向
  if (requestedTab === 'mining' && !international) {
    const next = new URLSearchParams(searchParams)
    next.delete('tab')
    const search = next.toString()
    return <Navigate to={search ? `/mining?${search}` : '/mining'} replace />
  }

  const activeTab: Tab = !international && requestedTab && requestedTab in MODES
    ? requestedTab as Tab
    : 'strategy'

  const changeTab = (tab: Tab) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', tab)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="flex min-h-full flex-col bg-base">
      <PageHeader
        title={BACKTEST_MARKETS[assetType].label + '回测'}
        subtitle={<span className="hidden md:inline">{MODES[activeTab].subtitle}</span>}
        className="shrink-0 flex-wrap gap-x-4 gap-y-2 bg-base/95 px-3 lg:flex-nowrap lg:px-5"
        right={(
          <div className="flex w-full min-w-0 items-center gap-1.5 sm:gap-2 lg:w-auto">
            <button
              type="button"
              onClick={() => setCandidatesOpen(true)}
              aria-label="打开候选方案"
              title={international ? '港美候选研究流程当前未开放' : '候选方案'}
              disabled={international}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-[11px] font-medium text-secondary transition-colors hover:border-accent/40 hover:text-accent sm:px-2.5 sm:text-xs"
            >
              <BookmarkCheck className="h-3.5 w-3.5" />
              <span>候选方案</span>
            </button>
            <span className="h-5 w-px shrink-0 bg-border" aria-hidden="true" />
            <nav className="min-w-0 flex-1 overflow-x-auto lg:flex-none" aria-label="回测视图">
              <div className="inline-flex min-w-max items-center gap-0.5 rounded-btn border border-border bg-surface/80 p-0.5">
                {(Object.keys(MODES) as Tab[]).map(tab => {
                  const mode = MODES[tab]
                  const Icon = mode.icon
                  const active = activeTab === tab
                  return (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => changeTab(tab)}
                      disabled={international && tab !== 'strategy'}
                      title={international && tab !== 'strategy' ? '当前市场仅支持日线策略回测' : mode.subtitle}
                      aria-current={active ? 'page' : undefined}
                      className={`inline-flex h-7 items-center gap-1 disabled:cursor-not-allowed disabled:opacity-40 rounded-[5px] px-1.5 text-[11px] font-medium transition-colors sm:gap-1.5 sm:px-2.5 sm:text-xs ${active
                        ? 'bg-accent text-white shadow-sm'
                        : 'text-secondary hover:bg-elevated hover:text-foreground'
                      }`}
                    >
                      <Icon className="hidden h-3.5 w-3.5 sm:block" />
                      {mode.title}
                    </button>
                  )
                })}
              </div>
            </nav>
          </div>
        )}
      />

      <main className="min-h-0 flex-1 px-3 pb-3 pt-3 lg:px-4 lg:pb-4">
        {activeTab === 'factor' && <FactorDiscovery />}
        {international && requestedTab && requestedTab !== 'strategy' && <p className="mb-3 text-xs text-warning">当前市场仅支持日线策略回测；因子发现和稳健性验证暂未开放。</p>}
        {activeTab === 'strategy' && <StrategyBacktest key={JSON.stringify([assetType, searchParams.get('strategy_id'), searchParams.get('symbols'), searchParams.get('start'), searchParams.get('end')])} assetType={assetType} initialQuery={searchParams} onAssetTypeChange={changeAsset} />}
        {activeTab === 'robustness' && <RobustnessValidation />}
      </main>

      {candidatesOpen && !international && <ResearchCandidatesDialog onClose={() => setCandidatesOpen(false)} />}
    </div>
  )
}
