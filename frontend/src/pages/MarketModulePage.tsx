import { Link } from 'react-router-dom'
import { Regime } from '@/pages/Regime'
import { BarChart3, Database, FileText, Gauge, RadioTower, ScanSearch, Siren, Star, TrendingUp } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { MarketScreener } from '@/components/MarketScreener'
import { MarketWatchlist } from '@/components/MarketWatchlist'
import { MarketIndices } from '@/components/MarketIndices'
import { MarketRankings } from '@/components/MarketRankings'
import { MarketDataStatus } from '@/components/MarketDataStatus'
import { MarketStockSearch } from '@/components/MarketStockSearch'
import { MarketBacktestEntry } from '@/components/MarketBacktestEntry'

export type MarketModule =
  | 'watchlist'
  | 'screener'
  | 'backtest'
  | 'mining'
  | 'stock-analysis'
  | 'limit-ladder'
  | 'concept-analysis'
  | 'industry-analysis'
  | 'financials'
  | 'monitor'
  | 'regime'
  | 'abnormal'
  | 'review'
  | 'indices'
  | 'data'

const MODULES: Record<MarketModule, { title: string; description: string; icon: typeof BarChart3 }> = {
  watchlist: { title: '自选', description: '跨市场自选列表与分组管理', icon: Star },
  screener: { title: '策略', description: '按策略筛选港美股标的', icon: ScanSearch },
  backtest: { title: '回测', description: '使用可用历史数据验证策略', icon: BarChart3 },
  mining: { title: '挖掘', description: '从市场数据中发现候选标的', icon: TrendingUp },
  'stock-analysis': { title: '个股分析', description: '查看个股行情与分析信息', icon: TrendingUp },
  'limit-ladder': { title: '连板梯队', description: 'A股涨跌停专属模块', icon: Gauge },
  'concept-analysis': { title: '概念分析', description: '市场主题与概念强弱', icon: BarChart3 },
  'industry-analysis': { title: '行业分析', description: '行业表现与成分股分析', icon: BarChart3 },
  financials: { title: '财务分析', description: '财务指标与基本面分析', icon: FileText },
  monitor: { title: '监控中心', description: '价格、策略与信号监控', icon: RadioTower },
  regime: { title: '市场环境', description: '市场状态与风险环境', icon: Gauge },
  abnormal: { title: '异动监控', description: '异常波动与事件提醒', icon: Siren },
  review: { title: '复盘', description: '交易日复盘与市场总结', icon: BarChart3 },
  indices: { title: '指数', description: '主要市场指数行情', icon: BarChart3 },
  data: { title: '数据', description: '行情数据同步与数据源管理', icon: Database },
}

const MARKET_META = {
  hk: { label: '港股', currency: 'HKD', delay: '腾讯行情实时，日K按数据源更新' },
  us: { label: '美股', currency: 'USD', delay: '免费行情源通常延迟约 15 分钟' },
} as const

export function MarketModulePage({ market, module }: { market: 'hk' | 'us'; module: MarketModule }) {
  const meta = MARKET_META[market]
  const info = MODULES[module]
  const Icon = info.icon
  // 按市场区分真缺口, 避免把某一市场已具备的能力一刀切成"不可用":
  //  - concept-analysis: 港美都没有概念数据, hk_us_overview_builder 里 concept_rank 恒空。
  //  - industry-analysis: 港股 universe 无 sector 字段 → 行业榜必然为空; 美股有,
  //    hk_us_overview_builder._sector_rank 是真实计算, 故仅对港股置不可用。
  //  - review: 复盘走 market_recap, 无 market 参数, 是 A 股专属。
  //  - abnormal: 无港美异动页面组件, 暂不提供入口。
  const unavailable = new Set<MarketModule>(
    market === 'us'
      ? ['concept-analysis', 'abnormal', 'review']
      : ['concept-analysis', 'industry-analysis', 'abnormal', 'review'],
  )
  const isUnavailable = unavailable.has(module)
  // regime 后端三市场都已落盘, Regime 页本身就是三市场共用(自带市场切换器);
  // /hk/regime、/us/regime 的 market 由 MarketProvider 按路由推导注入。
  if (module === 'regime') return <Regime />

  return (
    <div className="min-h-full bg-base">
      <PageHeader title={info.title} subtitle={`${meta.label} · ${info.description}`} />
      <main className="space-y-3 px-3 py-3 lg:px-5">
        {module === 'watchlist' ? (
          <MarketWatchlist market={market} />
        ) : module === 'screener' ? (
          <MarketScreener market={market} />
        ) : module === 'indices' ? (
          <MarketIndices market={market} />
        ) : module === 'limit-ladder' ? (
          <MarketRankings market={market} />
        ) : module === 'data' ? (
          <MarketDataStatus market={market} />
        ) : module === 'stock-analysis' ? (
          <MarketStockSearch market={market} />
        ) : module === 'backtest' ? (
          <MarketBacktestEntry market={market} />
        ) : isUnavailable ? (
          <section className="border border-border bg-surface p-4 text-xs text-muted">{meta.label}{info.title}依赖尚未接入的市场专用数据，当前不提供模拟结果。</section>
        ) : (
          <section className="border border-border bg-surface p-4">
            <div className="flex items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-accent">
                <Icon className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <h2 className="text-sm font-semibold text-foreground">{meta.label}{info.title}</h2>
                <p className="mt-1 text-xs leading-relaxed text-secondary">{info.description}。当前市场使用 {meta.currency} 计价；{meta.delay}。</p>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <div className="border border-border/70 bg-elevated/40 p-3"><div className="text-[11px] text-muted">市场</div><div className="mt-1 text-sm font-medium text-foreground">{meta.label}</div></div>
              <div className="border border-border/70 bg-elevated/40 p-3"><div className="text-[11px] text-muted">计价货币</div><div className="mt-1 font-mono text-sm text-foreground">{meta.currency}</div></div>
              <div className="border border-border/70 bg-elevated/40 p-3"><div className="text-[11px] text-muted">数据状态</div><div className="mt-1 text-sm text-warning">按数据源能力提供</div></div>
            </div>
            <p className="mt-4 border-t border-border/60 pt-3 text-xs text-muted">统一工作台入口已启用。该模块的市场专用数据能力会沿用 A 股的页面结构逐步接入。</p>
          </section>
        )}
        <div className="flex flex-wrap gap-2 text-xs">
          <Link to={`/${market}`} className="border border-border px-3 py-2 text-secondary hover:border-accent/50 hover:text-accent">返回{meta.label}看板</Link>
          <Link to={`/${market}/watchlist`} className="border border-border px-3 py-2 text-secondary hover:border-accent/50 hover:text-accent">打开{meta.label}自选</Link>
          <Link to={`/${market}/stock-analysis`} className="border border-border px-3 py-2 text-secondary hover:border-accent/50 hover:text-accent">进入个股分析</Link>
        </div>
      </main>
    </div>
  )
}
