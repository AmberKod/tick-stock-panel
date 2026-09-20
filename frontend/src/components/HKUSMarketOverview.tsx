import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Flame, Gauge, Info, LineChart, Loader2, RefreshCw, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import {
  scoreColor, fmtPrice, fmtStockPct, pctClass, compactCount,
  SectionTitle, KpiCell, IndexTicker, BreadthBar, DistributionBars,
  EmotionRadar, LadderMini, MiniMetric, StockList, HotRankCard,
} from '@/components/overview/OverviewKit'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { fmtBigNum } from '@/lib/format'

type Market = 'hk' | 'us'

interface Props {
  market: Market
}

const MARKET_META = {
  hk: {
    title: '港股市场看板',
    shortName: '港股',
    currency: 'HKD',
    source: '新浪日K · 盘后快照',
    strongLabel: '强势股(≥5%)',
    weakLabel: '弱势股(≤-5%)',
  },
  us: {
    title: '美股市场看板',
    shortName: '美股',
    currency: 'USD',
    source: '新浪日K · 盘后快照',
    strongLabel: '强势股(≥5%)',
    weakLabel: '弱势股(≤-5%)',
  },
} as const

const fetchOverview = (market: Market, asOf?: string) =>
  market === 'hk' ? api.overviewHk(asOf) : api.overviewUs(asOf)

/**
 * 港美市场深度看板 (阶段 C)。
 *
 * 复用 A股 Dashboard 的视觉组件 (OverviewKit), 对齐同一套看板 schema:
 * 指数 → 广度/涨跌分布 → 情绪雷达 → 趋势强度 → 三榜 → 强势梯队。
 *
 * 与 A股的语义差异:
 * - 无涨停制度 → 涨停/连板 语义替换为 强势股(涨幅≥5%)/强势梯队(涨幅分档)。
 * - 无概念 ext_data → 不渲染概念热度。
 * - 美股行业热度按 NASDAQ sector 聚合; 港股行业字段暂缺, 不渲染该卡片。
 * - 指数实时行情已接入 (港股 akshare 新浪源 / 美股新浪 hq.sinajs.cn)。
 * - 数据为盘后日K快照, 非盘中实时。
 */
export function HKUSMarketOverview({ market }: Props) {
  const meta = MARKET_META[market]
  const [previewStock, setPreviewStock] = useState<{ symbol: string; name?: string } | null>(null)

  const overview = useQuery({
    queryKey: ['hk-us-overview', market],
    queryFn: () => fetchOverview(market),
    staleTime: 60_000,
    refetchInterval: 60_000,
  })
  const data = overview.data

  // 点击个股 → A股同款弹窗 (K线/分时/自选/监控一体); detail 页仍保留给直链场景
  const openPreview = (symbol: string, name?: string) => setPreviewStock({ symbol, name })

  const strongRate = useMemo(() => {
    const d = data
    if (!d || d.breadth.total <= 0) return 0
    return (d.breadth.strong_up ?? 0) / d.breadth.total * 100
  }, [data])

  if (overview.isLoading && !data) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载{meta.shortName}市场看板…
        </div>
      </div>
    )
  }

  if (overview.isError && !data) {
    return (
      <div className="flex h-full items-center justify-center bg-base p-6">
        <div className="rounded-card border border-border bg-surface p-6 text-center">
          <div className="text-sm text-danger">{meta.shortName}看板加载失败</div>
          <p className="mt-1 text-xs text-muted">{overview.error instanceof Error ? overview.error.message : '后端服务暂不可用'}</p>
          <button onClick={() => overview.refetch()} className="mt-3 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white">重试</button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="flex h-full items-center justify-center bg-base p-6">
        <div className="rounded-card border border-border bg-surface p-6 text-center text-sm text-muted">暂无{meta.shortName}看板数据</div>
      </div>
    )
  }

  const score = data.emotion?.score ?? 50
  const total = data.breadth.total ?? 0
  const maxTier = data.limit.tiers.reduce((m, t) => Math.max(m, t.boards), 0)
  const limitWithStrongRate = { ...data.limit, seal_rate: strongRate }

  return (
    <div className="min-h-full bg-base p-1.5">
      {/* 顶栏 */}
      <div className="relative mb-1.5 flex flex-wrap items-center justify-between gap-2 overflow-hidden rounded-card border border-border bg-gradient-to-r from-surface/90 to-surface/70 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-sm">
        <div className="pointer-events-none absolute left-0 top-0 h-full w-1 bg-gradient-to-b from-accent to-accent/20" aria-hidden />
        <div className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-accent" />
          <h1 className="text-base font-semibold text-foreground">{meta.title}</h1>
          <span
            className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
            style={{ color: scoreColor(score), borderColor: `${scoreColor(score)}40`, background: `${scoreColor(score)}14` }}
          >
            {data.emotion.label} · {score}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          <span className="font-mono text-secondary">{data.as_of ?? '—'}</span>
          <span className="inline-flex items-center gap-1 rounded-full border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
            <span className="h-1.5 w-1.5 rounded-full bg-warning" />
            盘后快照
          </span>
          <button
            onClick={() => overview.refetch()}
            disabled={overview.isFetching}
            className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
          >
            <RefreshCw className={`h-3 w-3 ${overview.isFetching ? 'animate-spin' : ''}`} />重载
          </button>
        </div>
      </div>

      {/* 指数 + 核心指标 */}
      <section className="mb-1.5 rounded-card border border-border bg-surface/60 p-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.5)]">
        <div className="grid grid-cols-2 gap-1 sm:grid-cols-4">
          {data.indices.map(item => <IndexTicker key={item.symbol} item={item} link={false} />)}
        </div>
        <div className="mt-1.5 grid grid-cols-3 gap-1 sm:grid-cols-6">
          <KpiCell label="个股涨 / 平 / 跌" value={<><span className="text-bull">{data.breadth.up}</span><span className="text-muted">/</span><span className="text-muted">{data.breadth.flat}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.down}</span></>} sub={`上涨率 ${data.breadth.up_pct.toFixed(1)}%`} />
          <KpiCell label={meta.strongLabel} value={<><span className="text-bull">{data.breadth.strong_up ?? 0}</span><span className="text-muted">/</span><span className="text-bear">{data.breadth.strong_down ?? 0}</span></>} sub={meta.weakLabel} />
          <KpiCell label="成交额" value={fmtBigNum(data.amount.total)} sub={`均额 ${fmtBigNum(data.amount.avg)}`} />
          <KpiCell label="平均涨跌" value={fmtStockPct(data.breadth.avg_pct)} sub={`中位 ${fmtStockPct(data.breadth.median_pct)}`} tone={pctClass(data.breadth.avg_pct) === 'text-bull' ? 'bull' : pctClass(data.breadth.avg_pct) === 'text-bear' ? 'bear' : 'neutral'} />
          <KpiCell label="量比 / 放量" value={`${fmtPrice(data.activity.vol_ratio, 2)} / ${fmtPrice(data.activity.high_vol_ratio, 1)}%`} sub={`全市场 ${total} 只`} tone="accent" />
          <KpiCell label="强势梯队" value={`${data.limit.tiers.length} 档`} sub={maxTier > 0 ? `最高 ${maxTier} 档 (≥${maxTier * 5}%)` : '无 ≥5% 强势股'} tone="accent" />
        </div>
      </section>

      <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <main className="min-w-0 space-y-1.5">
          <div className="grid grid-cols-1 gap-1.5 lg:grid-cols-3">
            <section className="rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm">
              <SectionTitle icon={BarChart3} title="涨跌分布 / 广度" hint={`${total}只`} />
              <DistributionBars rows={data.distribution} />
              <div className="mt-2">
                <BreadthBar data={data.breadth} />
              </div>
              <div className="mt-2 grid grid-cols-2 gap-1.5">
                <MiniMetric label="平均涨跌" value={fmtStockPct(data.breadth.avg_pct)} cls={pctClass(data.breadth.avg_pct)} />
                <MiniMetric label="中位涨跌" value={fmtStockPct(data.breadth.median_pct)} cls={pctClass(data.breadth.median_pct)} />
              </div>
            </section>

            <section
              className="rounded-card border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm"
              style={{ borderColor: `${scoreColor(score)}40` }}
            >
              <SectionTitle icon={Sparkles} title="情绪雷达" hint={`情绪评分 ${score}`} />
              <EmotionRadar radar={data.radar} score={score} />
            </section>

            <section className="flex flex-col rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm">
              <div>
                <SectionTitle icon={LineChart} title="趋势强度" hint="均线/新高低" />
                <div className="grid grid-cols-3 gap-1.5">
                  <MiniMetric label="站上MA5" value={`${data.trend.above_ma5_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="站上MA20" value={`${data.trend.above_ma20_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="站上MA60" value={`${data.trend.above_ma60_pct.toFixed(0)}%`} cls="text-accent" />
                  <MiniMetric label="60日新高" value={compactCount(data.trend.new_high)} cls="text-bull" />
                  <MiniMetric label="60日新低" value={compactCount(data.trend.new_low)} cls="text-bear" />
                  <MiniMetric label="高低比" value={`${data.trend.new_high + data.trend.new_low > 0 ? Math.round(data.trend.new_high / (data.trend.new_high + data.trend.new_low) * 100) : 50}%`} cls={data.trend.new_high >= data.trend.new_low ? 'text-bull' : 'text-bear'} />
                </div>
              </div>
              <div className="mt-1.5 border-t border-border pt-1.5">
                <SectionTitle icon={Gauge} title="波动 / 强弱" hint="量价结构" />
                <div className="grid grid-cols-3 gap-1.5">
                  <MiniMetric label="强势股" value={`${data.breadth.strong_up ?? 0}`} cls="text-bull" />
                  <MiniMetric label="弱势股" value={`${data.breadth.strong_down ?? 0}`} cls="text-bear" />
                  <MiniMetric label="强势率" value={`${strongRate.toFixed(1)}%`} cls="text-accent" />
                </div>
              </div>
            </section>
          </div>

          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-4">
            <StockList title="涨幅榜" rows={data.top_gainers} mode="gain" onStockClick={openPreview} />
            <StockList title="跌幅榜" rows={data.top_losers} mode="loss" onStockClick={openPreview} />
            <StockList title="成交额榜" rows={data.turnover_leaders} mode="amount" onStockClick={openPreview} />
            <StockList title="放量榜" rows={data.active_leaders} mode="active" onStockClick={openPreview} />
          </div>
        </main>

        <aside className="min-w-0 space-y-1.5">
          {(market === 'us' || market === 'hk') && data.industry_rank && (
            <HotRankCard
              title="行业热度"
              rank={data.industry_rank}
              configUrl="/industry-analysis"
              onStockClick={openPreview}
            />
          )}
          <section className="rounded-card border border-border/70 bg-surface/55 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm">
            <SectionTitle icon={Flame} title="强势梯队" hint={`强势股 ${data.breadth.strong_up ?? 0}`} />
            <LadderMini limit={limitWithStrongRate} sealLabel="强势股率" boardUnit="档" minBoards={1} />
            <p className="mt-1.5 text-[9px] leading-relaxed text-muted">
              港美无涨跌停制度, 以涨幅分档替代连板: 1档≥5% · 2档≥10% · 3档≥15% · 4档≥20%。
            </p>
          </section>
          <section className="rounded-card border border-border/70 bg-surface/55 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm">
            <div className="mb-2 flex items-center gap-1.5">
              <Info className="h-3.5 w-3.5 text-accent" />
              <h2 className="text-xs font-semibold text-foreground">数据说明</h2>
            </div>
            <dl className="space-y-2 text-[10px] leading-relaxed text-muted">
              <div><dt className="inline text-secondary">口径：</dt><dd className="inline">全市场日K盘后快照, 覆盖 {total} 只</dd></div>
              <div><dt className="inline text-secondary">行情源：</dt><dd className="inline">{meta.source}</dd></div>
              <div><dt className="inline text-secondary">语义：</dt><dd className="inline">无涨停/连板, 强势股按涨幅≥5% 界定</dd></div>
              <div><dt className="inline text-secondary">指数：</dt><dd className="inline">实时行情源待接入, 当前仅展示标的</dd></div>
            </dl>
          </section>
        </aside>
      </div>

      {overview.isError && data && (
        <div className="border border-warning/40 bg-warning/5 px-3 py-2 text-xs text-warning">本次刷新失败, 当前继续展示最近一次成功数据。</div>
      )}
      {total === 0 && (
        <div className="border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
          暂无{meta.shortName}日K数据, 请先完成 {meta.shortName}日K同步与 enriched 计算。
        </div>
      )}

      {/* A股同款个股弹窗: K线/分时/自选/监控一体 (数据层 get_daily 已对 .HK/.US 分流) */}
      <StockPreviewDialog
        symbol={previewStock?.symbol ?? null}
        name={previewStock?.name}
        onClose={() => setPreviewStock(null)}
      />
    </div>
  )
}
