import { Link } from 'react-router-dom'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import {
  Activity,
  ArrowUpRight,
  BarChart3,
  CloudOff,
  Gauge,
  Globe,
  HelpCircle,
  RefreshCw,
  Rocket,
  Scale,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  api,
  type MarketPosture,
  type MarketPostureVote,
  type OverviewMarket,
  type PostureVerdict,
  type PostureVote,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { getTier, tierQueryOptions, type RefreshTier } from '@/lib/refreshTiers'
import { Panel, PanelGrid, PANEL_SPAN } from '@/components/panel'
import { SectionTitle } from '@/components/overview/OverviewKit'
import { cn } from '@/lib/cn'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'

// ============================================================================
// 跨市场总览页 (/markets)
// ----------------------------------------------------------------------------
// 定位: single pane of glass —— 一屏并置 A股/港股/美股, 先给「判断」(面板 1 三市场
// 态势), 再给「量」(各市场关键量)。**不是导航页**: 不靠跳转去凑信息。
//
// 数据源与刷新档位(一律走 refreshTiers, 不写裸数字):
//   - 态势: api.overviewPosture()  → T3 日级 (结论类, 一天内基本不变)
//   - A股量: api.overviewMarket()  → T0 实时 (走 SSE invalidation, 无轮询)
//   - 港美量: api.overviewHk/Us()  → T2 慢快照 (盘后日K口径, 盘中不会变)
// 刻意**不引入** /api/overview/all 聚合端点: A股 overview 有后端缓存而港美没有,
// 合并成一个请求会让港美被 A股 的刷新节奏带着每轮全市场重算。
//
// 铁律: 「不可用」绝不能渲染成 0 或中性。本页对 unavailable 的处理见
// `collectUnavailable` 与 `PostureCard` 中的显式不可用区块 —— 它既是独立徽标,
// 也在计票里被排除出分母, 且 unknown 态明确写成「不是防守」。
// ============================================================================

/** 三市场固定顺序(与后端 ALLOWED_MARKETS 一致), 顺序在代码里定死。 */
const MARKETS = [
  { key: 'cn', label: 'A 股', href: '/', tier: 'T0' },
  { key: 'hk', label: '港股', href: '/hk', tier: 'T2' },
  { key: 'us', label: '美股', href: '/us', tier: 'T2' },
] as const satisfies readonly { key: string; label: string; href: string; tier: RefreshTier }[]

type MarketRow = (typeof MARKETS)[number]

/** 态势档位: 面板 1 是日级判断。 */
const POSTURE_TIER: RefreshTier = 'T3'

/** 投票维度的固定顺序(后端 VOTING_DIMS), 用于稳定渲染与不可用维度排序。 */
const DIM_ORDER: readonly string[] = ['regime', 'breadth', 'hotspots', 'industry']

/** 维度中文名兜底表 —— 后端 votes[].label 已带, 这里只给 unavailable_dims 用。 */
const DIM_LABELS: Record<string, string> = {
  regime: '市场环境',
  breadth: '涨跌广度',
  hotspots: '热点阶段',
  industry: '行业强弱',
}

/** 港美概览缓存 key —— 与 HKUSMarketOverview 保持同一份, 切页不重复请求。 */
const hkUsOverviewKey = (market: 'hk' | 'us') => ['hk-us-overview', market] as const

interface PostureVisual {
  icon: LucideIcon
  /** 主文字色 */
  text: string
  /** 主视觉外框(姿态是卡片里最大的元素) */
  box: string
}

/**
 * 4 个态势的视觉。**刻意让 unknown 与 defend 完全不像**:
 * defend 用 bear 绿实线, unknown 用中性灰虚线 —— 避免"维度全不可用"被读成防守。
 */
// 键型收窄成联合类型(而不是 Record<string, ...>): 后端若新增一个 posture 取值,
// 这里会**编译期报缺键**, 而不是运行时静默 fallback 成 unknown。
const POSTURE_VISUAL: Record<PostureVerdict, PostureVisual> = {
  attack: { icon: Rocket, text: 'text-bull', box: 'border-bull/40 bg-bull/10' },
  balanced: { icon: Scale, text: 'text-accent', box: 'border-accent/40 bg-accent/10' },
  defend: { icon: ShieldCheck, text: 'text-bear', box: 'border-bear/40 bg-bear/10' },
  unknown: { icon: HelpCircle, text: 'text-muted', box: 'border-dashed border-border bg-elevated/50' },
}

/** 投票徽标样式。unavailable 是虚线橙 —— 与 neutral(实心灰)一眼可分。 */
const VOTE_CHIP_CLASS: Record<PostureVote, string> = {
  attack: 'border-bull/30 bg-bull/10 text-bull',
  neutral: 'border-border bg-elevated/60 text-secondary',
  defend: 'border-bear/30 bg-bear/10 text-bear',
  unavailable: 'border-dashed border-warning/50 bg-warning/10 text-warning',
}

/** 计票数字颜色。 */
const VOTE_NUMBER_CLASS: Record<PostureVote, string> = {
  attack: 'text-bull',
  neutral: 'text-secondary',
  defend: 'text-bear',
  unavailable: 'text-warning',
}

/** 指数涨跌幅是百分数(1.5 = +1.5%), 与个股的小数口径不同, 不要混用 fmtPct。 */
function fmtIndexPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

function numOrNull(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * 汇总不可用维度。
 *
 * 取 votes 中 vote==='unavailable' 的 dim(运行时真相) 与后端 unavailable_dims
 * 的**并集**: 任一侧先感知到不可用都会显示, 不会出现"后端漏报就静默消失"。
 */
function collectUnavailable(item: MarketPosture | undefined): string[] {
  if (!item) return []
  const merged = new Set<string>()
  for (const v of item.votes ?? []) {
    if (v.vote === 'unavailable') merged.add(v.dim)
  }
  for (const dim of item.unavailable_dims ?? []) merged.add(dim)
  return [...merged].sort((a, b) => dimRank(a) - dimRank(b))
}

function dimRank(dim: string): number {
  const index = DIM_ORDER.indexOf(dim)
  return index < 0 ? DIM_ORDER.length : index
}

function dimLabel(dim: string): string {
  return DIM_LABELS[dim] ?? dim
}

// ============================================================================
// 面板 1: 单市场态势卡片
// ============================================================================

interface PostureCardProps {
  /** 市场 key, 用于兜底标题与"未返回该市场"文案 */
  market: MarketRow
  /** 后端返回的该市场态势; undefined = 后端没给这个市场 */
  item: MarketPosture | undefined
  loading: boolean
  error: Error | null
  onRetry: () => void
}

function PostureCard({ market, item, loading, error, onRetry }: PostureCardProps) {
  const title = item?.market_label || market.label
  const postureKey = item?.posture ?? 'unknown'
  const visual = POSTURE_VISUAL[postureKey] ?? POSTURE_VISUAL.unknown
  const PostureIcon = visual.icon

  const votes: MarketPostureVote[] = item?.votes ?? []
  const tally = item?.tally ?? { attack: 0, neutral: 0, defend: 0, counted: 0 }
  const unavailable = collectUnavailable(item)
  const veto = item?.veto ?? null

  // 判据: 后端已按「维度 → 理由 → 投什么」写成人话, 最后一条 _verdict 是定案说明。
  // 数据源错误单独追加, 让用户知道结论是在有缺失的数据上得出的。
  const evidenceItems: string[] = [
    ...(item?.evidence ?? []).map(e => e.text),
    ...(item?.source_errors ?? []).map(text => `数据源错误: ${text}`),
  ]

  const freshness = item?.freshness

  return (
    <Panel
      title={`${title} · 态势`}
      icon={PostureIcon}
      hint={item?.as_of ?? undefined}
      className={PANEL_SPAN.third}
      loading={loading}
      error={error}
      // 后端没返回这个市场的条目 ≠ 判防守: 明确标不可用
      unavailable={!item && !loading && !error ? `后端未返回${title}的态势条目, 无法定案。` : false}
      onRetry={onRetry}
      evidence={evidenceItems.length > 0 ? { label: '判据', items: evidenceItems } : undefined}
      footer={
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-muted">
          <span>
            分母 <span className="font-mono text-secondary">{tally.counted}</span>/{votes.length || DIM_ORDER.length}
          </span>
          <span aria-hidden>·</span>
          <span>regime 口径 <span className="font-mono text-secondary">{freshness?.regime_as_of ?? '—'}</span></span>
          <span aria-hidden>·</span>
          <span>
            热点时效{' '}
            <span className="font-mono text-secondary">
              {freshness?.hotspot_age_hours == null ? '—' : `${Number(freshness.hotspot_age_hours).toFixed(1)}h`}
            </span>
          </span>
        </div>
      }
    >
      <div className="space-y-1.5">
        {/* ---- 主视觉: 今天该进攻还是防守 ---- */}
        <div className={cn('flex items-center gap-2 rounded-lg border px-2 py-2', visual.box)}>
          <PostureIcon className={cn('h-6 w-6 shrink-0', visual.text)} strokeWidth={1.6} />
          <div className="min-w-0">
            <div className={cn('text-xl font-bold leading-tight tracking-tight', visual.text)}>
              {item?.posture_label ?? '未知'}
            </div>
            <div className="text-[10px] text-muted">
              今日姿态 · 有效票 {tally.counted}/{votes.length || DIM_ORDER.length}
            </div>
          </div>
          {veto && (
            <span
              className="ml-auto inline-flex shrink-0 items-center gap-1 rounded border border-warning/50 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning"
              title={veto.reason}
            >
              <ShieldAlert className="h-3 w-3" />
              一票否决
            </span>
          )}
        </div>

        {/* ---- unknown 必须说清「不是防守」 ---- */}
        {postureKey === 'unknown' && (
          <div className="flex items-start gap-1.5 rounded-lg border border-dashed border-border bg-elevated/50 px-2 py-1.5 text-[10px] leading-relaxed text-secondary">
            <CloudOff className="mt-px h-3 w-3 shrink-0 text-muted" />
            <span>
              投票维度全部不可用 → 定案「未知」。这不是防守, 也不是中性, 只是没有可用判据;
              一旦有维度恢复, 结论会立刻重算。
            </span>
          </div>
        )}

        {/* ---- 一票否决: 优先级高于投票 ---- */}
        {veto && (
          <div className="rounded-lg border border-warning/50 bg-warning/5 px-2 py-1.5">
            <div className="flex items-center gap-1 text-[10px] font-semibold text-warning">
              <ShieldAlert className="h-3 w-3" />
              一票否决 · {dimLabel(veto.dim)}
            </div>
            <p className="mt-0.5 text-[10px] leading-relaxed text-secondary">{veto.reason}</p>
            <p className="mt-0.5 text-[10px] leading-relaxed text-muted">
              否决票优先于下面的计票结果, 定案以否决为准。
            </p>
          </div>
        )}

        {/* ---- 计票: 让用户看见分母 ---- */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
          <span className="text-muted">计票</span>
          <span className={cn('font-mono font-semibold', VOTE_NUMBER_CLASS.attack)}>进攻 {tally.attack}</span>
          <span className={cn('font-mono font-semibold', VOTE_NUMBER_CLASS.neutral)}>中性 {tally.neutral}</span>
          <span className={cn('font-mono font-semibold', VOTE_NUMBER_CLASS.defend)}>防守 {tally.defend}</span>
          <span className="ml-auto font-mono text-muted">
            有效 {tally.counted}/{votes.length || DIM_ORDER.length}
          </span>
        </div>

        {/* ---- 不可用维度: 铁律落点(既不是 0, 也不是中性) ---- */}
        {unavailable.length > 0 && (
          <div className="rounded-lg border border-dashed border-warning/50 bg-warning/5 px-2 py-1.5">
            <div className="flex items-center gap-1 text-[10px] font-semibold text-warning">
              <CloudOff className="h-3 w-3" />
              不可用维度 · 已不计入分母
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {unavailable.map(dim => (
                <span
                  key={dim}
                  className="inline-flex items-center gap-0.5 rounded border border-dashed border-warning/50 bg-warning/10 px-1.5 py-px text-[10px] font-medium text-warning"
                >
                  {dimLabel(dim)} · 不可用
                </span>
              ))}
            </div>
            <p className="mt-1 text-[10px] leading-relaxed text-muted">
              这些维度既不算进攻也不算防守, 更不会按「中性/0」参与计票 —— 分母只数有效票。
            </p>
          </div>
        )}

        {/* ---- 逐维度投票明细 ---- */}
        <ul className="space-y-0.5">
          {votes.map(vote => (
            <li key={vote.dim} className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-[11px] text-secondary">{vote.label || dimLabel(vote.dim)}</div>
                <div className="truncate text-[10px] text-muted" title={vote.detail}>
                  {vote.detail}
                </div>
              </div>
              <span
                className={cn(
                  'shrink-0 rounded border px-1.5 py-px text-[10px] font-medium',
                  VOTE_CHIP_CLASS[vote.vote] ?? VOTE_CHIP_CLASS.unavailable,
                )}
              >
                {vote.vote_label || vote.vote}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  )
}

// ============================================================================
// 各市场关键量卡片(不是判断, 是量)
// ============================================================================

interface MetricsCardProps {
  market: MarketRow
  data: OverviewMarket | undefined
  loading: boolean
  error: Error | null
  /** 本次刷新失败但仍展示上一次成功数据 */
  stale: boolean
  onRetry: () => void
}

function MiniStat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="min-w-0 rounded border border-border bg-elevated/40 px-1.5 py-1">
      <div className="truncate text-[9px] text-muted">{label}</div>
      <div className={cn('truncate font-mono text-[11px] font-semibold tabular-nums text-foreground', cls)}>
        {value}
      </div>
    </div>
  )
}

function MetricsCard({ market, data, loading, error, stale, onRetry }: MetricsCardProps) {
  const indices = data?.indices ?? []
  const breadth = data?.breadth
  const total = Math.max(breadth?.total ?? 0, 1)
  const upWidth = ((breadth?.up ?? 0) / total) * 100
  const flatWidth = ((breadth?.flat ?? 0) / total) * 100
  const downWidth = Math.max(0, 100 - upWidth - flatWidth)
  const avgPct = numOrNull(breadth?.avg_pct)

  return (
    <Panel
      title={`${market.label} · 关键量`}
      icon={BarChart3}
      hint={data?.as_of ?? undefined}
      className={PANEL_SPAN.third}
      loading={loading}
      error={error}
      unavailable={!data && !loading && !error ? `${market.label}概览数据暂不可得。` : false}
      stale={stale}
      onRetry={onRetry}
      actions={
        <Link
          to={market.href}
          className="inline-flex items-center gap-0.5 rounded border border-border px-1 py-px text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent"
          title={`进入${market.label}深度看板`}
        >
          深度看板
          <ArrowUpRight className="h-3 w-3" />
        </Link>
      }
      footer={
        <div className="text-[10px] text-muted">
          刷新档位 <span className="font-mono text-secondary">{market.tier} · {getTier(market.tier).label}</span>
        </div>
      }
    >
      <div className="space-y-1.5">
        {/* 指数: 涨跌幅是百分数口径 */}
        <div className="space-y-0.5">
          {indices.length === 0 && <div className="text-[10px] text-muted">暂无指数行情</div>}
          {indices.slice(0, 3).map(item => {
            const pct = numOrNull(item.change_pct)
            const price = numOrNull(item.last_price ?? item.close)
            return (
              <div key={item.symbol} className="flex items-baseline justify-between gap-2">
                <span className="min-w-0 truncate text-[11px] text-secondary">{item.name || item.symbol}</span>
                <span className="flex shrink-0 items-baseline gap-1.5">
                  <span className={cn('font-mono text-[10px] text-muted', price == null && 'text-muted')}>
                    {price == null ? '—' : price.toFixed(2)}
                  </span>
                  <span className={cn('font-mono text-[11px] font-semibold tabular-nums', priceColorClass(pct))}>
                    {fmtIndexPct(pct)}
                  </span>
                </span>
              </div>
            )
          })}
        </div>

        {/* 涨跌家数 + 广度条 */}
        <div>
          <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-elevated">
            <div className="bg-bull" style={{ width: `${upWidth}%` }} />
            <div className="bg-muted/50" style={{ width: `${flatWidth}%` }} />
            <div className="bg-bear" style={{ width: `${downWidth}%` }} />
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px]">
            <span className="font-mono text-bull">涨 {breadth?.up ?? 0}</span>
            <span className="font-mono text-muted">平 {breadth?.flat ?? 0}</span>
            <span className="font-mono text-bear">跌 {breadth?.down ?? 0}</span>
            <span className="ml-auto font-mono text-muted">共 {breadth?.total ?? 0}</span>
          </div>
        </div>

        {/* 成交额 / 均涨幅 / 情绪 */}
        <div className="grid grid-cols-3 gap-1">
          <MiniStat label="成交额" value={fmtBigNum(numOrNull(data?.amount?.total))} />
          <MiniStat label="均涨幅" value={fmtPct(avgPct)} cls={priceColorClass(avgPct)} />
          <MiniStat
            label="情绪"
            value={data?.emotion?.score == null ? '—' : String(data.emotion.score)}
          />
        </div>
        {data?.emotion?.label && (
          <div className="text-[10px] text-muted">
            情绪口径 <span className="text-secondary">{data.emotion.label}</span>
          </div>
        )}
      </div>
    </Panel>
  )
}

// ============================================================================
// 页面
// ============================================================================

export function Markets() {
  // ---- 面板 1: 三市场态势 (T3 日级) ----
  const posture = useQuery({
    queryKey: QK.overviewPosture(),
    queryFn: () => api.overviewPosture(),
    ...tierQueryOptions(POSTURE_TIER),
    placeholderData: (prev) => prev,
  })

  // ---- 各市场关键量: 三个请求并行发出(刻意不合并成聚合端点) ----
  const cnOverview = useQuery({
    queryKey: QK.overviewMarket(),
    queryFn: () => api.overviewMarket(),
    ...tierQueryOptions('T0'),
    placeholderData: (prev) => prev,
  })
  const hkOverview = useQuery({
    queryKey: hkUsOverviewKey('hk'),
    queryFn: () => api.overviewHk(),
    ...tierQueryOptions('T2'),
    placeholderData: (prev) => prev,
  })
  const usOverview = useQuery({
    queryKey: hkUsOverviewKey('us'),
    queryFn: () => api.overviewUs(),
    ...tierQueryOptions('T2'),
    placeholderData: (prev) => prev,
  })

  const metricsByMarket: Record<string, UseQueryResult<OverviewMarket, Error>> = {
    cn: cnOverview,
    hk: hkOverview,
    us: usOverview,
  }

  const postureLoading = posture.isLoading && !posture.data
  const postureError = posture.isError && !posture.data ? (posture.error as Error) : null
  const postureStale = posture.isError && !!posture.data
  const refreshAll = () => {
    void posture.refetch()
    void cnOverview.refetch()
    void hkOverview.refetch()
    void usOverview.refetch()
  }

  return (
    <div className="min-h-full bg-base p-1.5">
      {/* ---- 页头 ---- */}
      <header className="mb-1.5 flex flex-wrap items-center justify-between gap-2 overflow-hidden rounded-card border border-border bg-gradient-to-r from-surface/90 to-surface/70 px-3 py-1.5 shadow-[0_1px_3px_hsl(var(--border)/0.4)] backdrop-blur-sm">
        <div className="flex min-w-0 items-center gap-2">
          <Globe className="h-4 w-4 shrink-0 text-accent" />
          <h1 className="truncate text-base font-semibold text-foreground">跨市场总览</h1>
          <span className="hidden text-[10px] text-muted sm:inline">A股 / 港股 / 美股 · 一屏并置</span>
          {postureStale && (
            <span className="shrink-0 rounded border border-warning/40 bg-warning/10 px-1.5 py-px text-[10px] text-warning">
              态势刷新失败, 展示上一次结果
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[10px] text-muted">
          <span>
            定案时间 <span className="font-mono text-secondary">{posture.data?.as_of ?? '—'}</span>
          </span>
          <span className="rounded border border-border px-1.5 py-px font-mono text-secondary">
            {POSTURE_TIER} · {getTier(POSTURE_TIER).label}
          </span>
          <button
            type="button"
            onClick={refreshAll}
            className="inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:border-accent/40 hover:text-accent"
            title="重新拉取态势与三市场关键量"
          >
            <RefreshCw className={cn('h-3 w-3', posture.isFetching && 'animate-spin')} />
            刷新
          </button>
        </div>
      </header>

      {/* ---- 面板 1: 三市场态势 ---- */}
      <SectionTitle
        icon={Gauge}
        title="三市场态势"
        hint={`固定 4 维度投票 · ${POSTURE_TIER}(${getTier(POSTURE_TIER).label})`}
      />
      <PanelGrid>
        {MARKETS.map(market => (
          <PostureCard
            key={market.key}
            market={market}
            item={posture.data?.markets?.find(m => m.market === market.key)}
            loading={postureLoading}
            error={postureError}
            onRetry={() => void posture.refetch()}
          />
        ))}
      </PanelGrid>

      {/* ---- 各市场关键量 ---- */}
      <div className="h-1.5" aria-hidden />
      <SectionTitle icon={Activity} title="各市场关键量" hint="A股 T0 实时 · 港美 T2 慢快照" />
      <PanelGrid>
        {MARKETS.map(market => {
          const q = metricsByMarket[market.key]
          const data = q.data as OverviewMarket | undefined
          return (
            <MetricsCard
              key={market.key}
              market={market}
              data={data}
              loading={q.isLoading && !data}
              // 有旧数据时降级为 stale 提示, 不把整张卡片打成错误态
              error={q.isError && !data ? (q.error as Error) : null}
              stale={q.isError && !!data}
              onRetry={() => void q.refetch()}
            />
          )
        })}
      </PanelGrid>
    </div>
  )
}
