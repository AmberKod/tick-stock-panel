import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ArrowDownRight, ArrowUpRight, Flame } from 'lucide-react'
import type { MarketSnapshotRow, OverviewDimensionRankItem, OverviewMarket } from '@/lib/api'
import { fmtBigNum } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'

// ============================================================================
// 市场深度看板 · 纯展示组件集 (OverviewKit)
// ----------------------------------------------------------------------------
// 从 A股 Dashboard.tsx 抽取, 供 A股 / 港股 / 美股 三市场复用同一套视觉与语义色。
// 约定:
//   - 个股涨跌幅 change_pct 为小数 (0.03 = +3%), 用 fmtStockPct 渲染;
//   - 指数涨跌幅 change_pct 为百分数 (1.5 = +1.5%), 用 fmtIndexPct 渲染。
// ============================================================================

export function n(v: number | null | undefined) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function scoreColor(v: number) {
  // A 股惯例: 强势=红, 弱式=绿
  if (v >= 70) return '#F04438'
  if (v >= 55) return '#FB923C'
  if (v >= 45) return '#F59E0B'
  if (v >= 30) return '#84CC16'
  return '#12B76A'
}

export function fmtPrice(v: number | null | undefined, digits = 2) {
  const x = n(v)
  return x == null ? '—' : x.toFixed(digits)
}

export function fmtIndexPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${x.toFixed(2)}%`
}

export function fmtStockPct(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  return `${x >= 0 ? '+' : ''}${(x * 100).toFixed(2)}%`
}

export function pctClass(v: number | null | undefined) {
  const x = n(v)
  if (x == null || x === 0) return 'text-muted'
  return x > 0 ? 'text-bull' : 'text-bear'
}

export function compactCount(v: number | null | undefined) {
  const x = n(v)
  if (x == null) return '—'
  if (x >= 1000) return `${(x / 1000).toFixed(1)}k`
  return x.toFixed(0)
}

export function SectionTitle({ icon: Icon, title, hint }: { icon: typeof Activity; title: string; hint?: ReactNode }) {
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <div className="flex items-center gap-1.5">
        <span className="h-3 w-0.5 rounded-full bg-gradient-to-b from-accent to-accent/30" />
        <Icon className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-xs font-semibold text-foreground">{title}</h2>
      </div>
      {hint && <span className="font-mono text-[10px] text-muted">{hint}</span>}
    </div>
  )
}

export function KpiCell({ label, value, sub, tone = 'neutral' }: { label: ReactNode; value: ReactNode; sub?: string; tone?: 'bull' | 'bear' | 'accent' | 'neutral' }) {
  const isPlain = typeof value === 'string' || typeof value === 'number'
  const color = tone === 'bull' ? 'text-bull' : tone === 'bear' ? 'text-bear' : tone === 'accent' ? 'text-accent' : 'text-foreground'
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-border bg-surface/80 px-2 py-1 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-all hover:border-accent/30 hover:shadow-[0_2px_8px_hsl(var(--accent)/0.15)]">
      <div className="flex items-center gap-1 text-[11px] text-muted">{label}</div>
      <div className={`mt-1 truncate font-mono text-lg font-semibold leading-none tabular-nums ${isPlain ? color : 'text-foreground'}`}>{value}</div>
      {sub && <div className="mt-1 truncate text-[10px] text-muted">{sub}</div>}
    </div>
  )
}

export function IndexTicker({ item, link = true }: { item: OverviewMarket['indices'][number]; link?: boolean }) {
  const pct = item.change_pct
  const isUp = (n(pct) ?? 0) >= 0
  const cls = 'grid min-w-0 grid-cols-[1fr_auto] items-center gap-x-2 gap-y-0.5 rounded-lg border border-border bg-elevated/60 px-2 py-1.5 shadow-[0_1px_1px_hsl(var(--border)/0.3)] backdrop-blur-sm transition-all hover:border-accent/40 hover:bg-elevated hover:shadow-[0_2px_6px_hsl(var(--accent)/0.15)]'
  const inner = (
    <>
      <div className="truncate text-[13px] font-semibold text-foreground">{item.name || item.symbol}</div>
      <div className={`font-mono text-sm font-bold tabular-nums ${pctClass(pct)}`}>{fmtIndexPct(pct)}</div>
      <div className="font-mono text-[10px] text-muted">{item.symbol}</div>
      <div className={`flex items-center gap-1 font-mono text-xs tabular-nums ${pctClass(pct)}`}>
        {isUp ? <ArrowUpRight className="h-3 w-3" /> : <ArrowDownRight className="h-3 w-3" />}
        {fmtPrice(item.last_price)}
      </div>
    </>
  )
  if (!link) return <div className={cls}>{inner}</div>
  return (
    <Link to={`/indices?symbol=${encodeURIComponent(item.symbol)}`} className={cls}>
      {inner}
    </Link>
  )
}

export function BreadthBar({ data }: { data: OverviewMarket['breadth'] }) {
  const denom = Math.max(data.total, 1)
  const upW = data.up / denom * 100
  const downW = data.down / denom * 100
  const flatW = Math.max(0, 100 - upW - downW)
  return (
    <div className="space-y-2">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-elevated">
        <div className="bg-bull/85" style={{ width: `${upW}%` }} />
        <div className="bg-muted/45" style={{ width: `${flatW}%` }} />
        <div className="bg-bear/85" style={{ width: `${downW}%` }} />
      </div>
      <div className="grid grid-cols-3 gap-1.5 text-[11px]">
        <div className="rounded bg-bull/8 px-2 py-1 text-bull">涨 <span className="font-mono">{data.up}</span></div>
        <div className="rounded bg-elevated/70 px-2 py-1 text-muted">平 <span className="font-mono">{data.flat}</span></div>
        <div className="rounded bg-bear/8 px-2 py-1 text-bear">跌 <span className="font-mono">{data.down}</span></div>
      </div>
    </div>
  )
}

export function DistributionBars({ rows }: { rows: OverviewMarket['distribution'] }) {
  const maxCount = Math.max(...rows.map(r => r.count), 1)
  return (
    <div className="grid h-24 grid-cols-8 items-end gap-1 pt-1">
      {rows.map((r, i) => {
        const positive = i >= 4
        return (
          <div key={r.label} className="flex h-full min-w-0 flex-col items-center justify-end gap-0.5">
            <div className="font-mono text-[9px] text-muted">{r.count || ''}</div>
            <div
              className={`w-2 rounded-full ${positive ? 'bg-gradient-to-t from-bull/45 to-bull/90' : 'bg-gradient-to-t from-bear/45 to-bear/90'}`}
              style={{ height: `${Math.max(4, r.count / maxCount * 86)}%` }}
              title={`${r.label}: ${r.count}只`}
            />
            <div className="truncate text-[9px] text-muted">{r.label}</div>
          </div>
        )
      })}
    </div>
  )
}

export function EmotionRadar({ radar, score }: { radar: OverviewMarket['radar']; score: number }) {
  const size = 240
  const cx = size / 2
  const cy = size / 2
  const maxR = 78
  const color = scoreColor(score)
  if (!radar.length) return <div className="flex h-52 items-center justify-center text-xs text-muted">暂无雷达数据</div>
  const points = radar.map((r, i) => {
    const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
    const radius = maxR * Math.max(0, Math.min(100, r.value)) / 100
    return {
      ...r,
      x: cx + Math.cos(angle) * radius,
      y: cy + Math.sin(angle) * radius,
      lx: cx + Math.cos(angle) * (maxR + 27),
      ly: cy + Math.sin(angle) * (maxR + 27),
      gx: cx + Math.cos(angle) * maxR,
      gy: cy + Math.sin(angle) * maxR,
    }
  })
  const polygon = points.map(p => `${p.x},${p.y}`).join(' ')
  const gridPolygons = [1, 0.66, 0.33].map((level, idx) => ({
    level,
    idx,
    points: radar.map((_, i) => {
      const angle = -Math.PI / 2 + i * 2 * Math.PI / radar.length
      return `${cx + Math.cos(angle) * maxR * level},${cy + Math.sin(angle) * maxR * level}`
    }).join(' '),
  }))
  return (
    <div className="flex justify-center">
      <svg viewBox={`0 0 ${size} ${size}`} className="h-56 w-full">
        <defs>
          <radialGradient id="emotionRadarFill" cx="50%" cy="45%" r="70%">
            <stop offset="0%" stopColor={`${color}57`} />
            <stop offset="100%" stopColor={`${color}1f`} />
          </radialGradient>
          {/* 中心/网格用 CSS 变量取色, 亮暗主题自动切换 (SVG 属性支持 hsl(var(--x))) */}
          <radialGradient id="emotionRadarCenter" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor="hsl(var(--surface) / 0.92)" />
            <stop offset="68%" stopColor="hsl(var(--surface) / 0.70)" />
            <stop offset="100%" stopColor="hsl(var(--surface) / 0)" />
          </radialGradient>
        </defs>
        {gridPolygons.map(g => (
          <polygon
            key={g.level}
            points={g.points}
            fill={g.idx % 2 === 0 ? 'hsl(var(--elevated) / 0.55)' : 'hsl(var(--elevated) / 0.3)'}
            stroke={g.level === 1 ? 'hsl(var(--border) / 0.9)' : 'hsl(var(--border) / 0.5)'}
            strokeWidth={g.level === 1 ? 1.2 : 0.8}
          />
        ))}
        {points.map(p => <line key={p.key} x1={cx} y1={cy} x2={p.gx} y2={p.gy} stroke="hsl(var(--border) / 0.4)" />)}
        <polygon points={polygon} fill="url(#emotionRadarFill)" stroke={color} strokeWidth="2" />
        {points.map(p => <circle key={p.key} cx={p.x} cy={p.y} r="2.8" fill={color} stroke="hsl(var(--surface) / 0.9)" strokeWidth="1" />)}
        <circle cx={cx} cy={cy} r="29" fill="url(#emotionRadarCenter)" />
        <text x={cx} y={cy + 7} textAnchor="middle" className="fill-foreground font-mono text-[24px] font-bold">{score}</text>
        {points.map(p => (
          <text key={`${p.key}-label`} x={p.lx} y={p.ly + 4} textAnchor="middle" className="fill-secondary text-[10px] font-medium">{p.label}</text>
        ))}
      </svg>
    </div>
  )
}

// 涨停/连板梯队 (A股); 港美复用为「强势股梯队」, 用 sealLabel/boardUnit/minBoards 定制语义。
export function LadderMini({ limit, sealLabel = '封板率', boardUnit = '板', minBoards = 2 }: {
  limit: OverviewMarket['limit']
  sealLabel?: string
  boardUnit?: string
  minBoards?: number
}) {
  const tiers = limit.tiers.filter(t => t.boards >= minBoards).slice(0, 6)
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between rounded bg-elevated/55 px-2 py-1.5 text-[11px]">
        <span className="text-muted">{sealLabel}</span>
        <span className="font-mono text-accent">{(limit.seal_rate ?? 0).toFixed(0)}%</span>
      </div>
      {tiers.length === 0 && <div className="rounded border border-dashed border-border py-5 text-center text-xs text-muted">暂无 {minBoards}{boardUnit} 以上</div>}
      {tiers.map(t => {
        const stocks = t.stocks ?? []
        const showStocks = stocks.length > 0 && stocks.length <= 3
        return (
          <div key={t.boards} className="rounded bg-elevated/35 px-2 py-1.5">
            <div className="grid grid-cols-[42px_1fr_auto] items-center gap-2">
              <span className={`font-mono text-sm font-bold ${t.boards >= 5 ? 'text-bull' : t.boards >= 3 ? 'text-accent' : 'text-secondary'}`}>{t.boards}{boardUnit}</span>
              <div className="h-1.5 overflow-hidden rounded-full bg-base">
                <div className="h-full rounded-full bg-bull/70" style={{ width: `${Math.min(100, t.count * 12)}%` }} />
              </div>
              <span className="font-mono text-xs text-foreground">{t.count}</span>
            </div>
            {showStocks && (
              <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 pl-[50px]">
                {stocks.map(s => (
                  <span key={s.symbol} className="inline-flex items-center gap-0.5 text-[9px] text-secondary">
                    {s.name || s.symbol}
                  </span>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

export function MiniMetric({ label, value, cls = 'text-foreground' }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-md bg-elevated/45 px-2 py-1.5 border border-border/40">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`mt-0.5 font-mono text-xs font-semibold ${cls}`}>{value}</div>
    </div>
  )
}

export function StockList({ title, rows, mode, onStockClick }: {
  title: string; rows: MarketSnapshotRow[]; mode: 'gain' | 'loss' | 'amount' | 'active';
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  return (
    <div className="rounded-card border border-border/60 bg-surface/45 p-1.5 backdrop-blur-sm">
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold text-foreground">{title}</h3>
        <span className="text-[9px] text-muted">TOP {Math.min(rows.length, 8)}</span>
      </div>
      <div className="space-y-1">
        {rows.slice(0, 8).map((r, idx) => (
          <div
            key={`${r.symbol}-${idx}`}
            className="grid grid-cols-[18px_1fr_auto] items-center gap-1.5 rounded-md bg-elevated/40 px-1.5 py-1 cursor-pointer hover:bg-elevated hover:brightness-110 transition-colors border border-transparent hover:border-border/60"
            onClick={() => onStockClick?.(r.symbol, r.name ?? undefined)}
          >
            <span className="text-center font-mono text-[10px] text-muted">{idx + 1}</span>
            <div className="min-w-0">
              <div className="flex items-center gap-1">
                <span className="truncate text-[11px] text-foreground">{r.name || r.symbol}</span>
                {(() => {
                  const board = boardTag(r.symbol)
                  return board ? (
                    <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                      {board.label}
                    </span>
                  ) : null
                })()}
              </div>
              <span className="font-mono text-[9px] text-muted">{r.symbol}</span>
            </div>
            <div className="text-right">
              {mode === 'amount' ? (
                <>
                  <div className="font-mono text-[11px] text-foreground">{fmtBigNum(r.amount)}</div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : mode === 'active' ? (
                <>
                  <div className="font-mono text-[11px] text-accent">
                    {r.turnover_rate != null
                      ? `${fmtPrice(r.turnover_rate, 1)}%`
                      : r.vol_ratio_5d != null
                        ? fmtPrice(r.vol_ratio_5d, 2)
                        : '—'}
                  </div>
                  <div className={`font-mono text-[9px] ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                </>
              ) : (
                <>
                  <div className={`font-mono text-[11px] font-semibold ${pctClass(r.change_pct)}`}>{fmtStockPct(r.change_pct)}</div>
                  <div className="font-mono text-[9px] text-muted">{fmtPrice(r.close)}</div>
                </>
              )}
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="py-5 text-center text-xs text-muted">暂无数据</div>}
      </div>
    </div>
  )
}

export function RankColumn({ title, rows, tone, onStockClick }: {
  title: string; rows: OverviewDimensionRankItem[]; tone: 'bull' | 'bear';
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <div className={`text-[10px] font-medium ${tone === 'bull' ? 'text-bull' : 'text-bear'}`}>{title}</div>
      {rows.slice(0, 5).map((r, idx) => (
        <div key={`${title}-${r.name}-${idx}`} className="grid grid-cols-[14px_1fr_auto] items-center gap-1 rounded-md bg-elevated/40 px-1.5 py-1 border border-transparent hover:border-border/60 transition-colors">
          <span className="text-center font-mono text-[9px] text-muted">{idx + 1}</span>
          <div className="min-w-0">
            <div className="truncate text-[11px] text-foreground" title={r.name}>{r.name}</div>
            <div className="mt-0.5 flex items-center gap-1">
              <span className="shrink-0 font-mono text-[9px] text-muted">{r.count}只</span>
              <span className="text-muted">·</span>
              {r.leader?.symbol ? (
                <button
                  onClick={(e) => { e.stopPropagation(); onStockClick?.(r.leader!.symbol!, r.leader!.name ?? undefined) }}
                  className="truncate text-[10px] font-medium text-secondary hover:text-accent cursor-pointer transition-colors"
                  title={r.leader?.symbol ?? undefined}
                >{r.leader?.name ?? '—'}</button>
              ) : (
                <span className="truncate text-[10px] text-muted">{r.leader?.name ?? '—'}</span>
              )}
              {r.leader?.symbol && (() => {
                const board = boardTag(r.leader!.symbol!)
                return board ? (
                  <span className={`shrink-0 inline-flex items-center justify-center h-3 px-1 rounded text-[8px] font-bold leading-none border ${board.color}`}>
                    {board.label}
                  </span>
                ) : null
              })()}
            </div>
          </div>
          <div className={`font-mono text-[10px] font-semibold ${pctClass(r.avg_pct)}`}>{fmtStockPct(r.avg_pct)}</div>
        </div>
      ))}
      {rows.length === 0 && <div className="rounded border border-dashed border-border py-4 text-center text-xs text-muted">暂无数据</div>}
    </div>
  )
}

export function HotRankCard({ title, rank, configUrl, onStockClick }: {
  title: string; rank?: OverviewMarket['concept_rank']; configUrl: string;
  onStockClick?: (symbol: string, name?: string) => void;
}) {
  const hasData = (rank?.leading?.length ?? 0) > 0 || (rank?.lagging?.length ?? 0) > 0
  return (
    <section className="rounded-card border border-border/60 bg-surface/45 p-1.5 backdrop-blur-sm">
      <SectionTitle icon={Flame} title={title} hint="领涨/领跌" />
      {hasData ? (
        <div className="grid grid-cols-2 gap-2">
          <RankColumn title="领涨" rows={rank?.leading ?? []} tone="bull" onStockClick={onStockClick} />
          <RankColumn title="领跌" rows={rank?.lagging ?? []} tone="bear" onStockClick={onStockClick} />
        </div>
      ) : (
        <div className="py-4 text-center">
          <p className="text-[11px] text-muted">未配置扩展数据源</p>
          <Link
            to={configUrl}
            className="mt-1.5 inline-block text-[11px] text-accent hover:text-accent/80 transition-colors"
          >
            前往配置 →
          </Link>
        </div>
      )}
    </section>
  )
}
