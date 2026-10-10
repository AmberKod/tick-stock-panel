/**
 * 筹码分布(成本分布)面板。
 *
 * 画的是**模型估算**结果 —— 见 lib/chips.ts 顶部的完整口径说明。
 * 这里刻意把"这是估算、不是交易所持仓数据"写在面板上, 而不是藏进 tooltip:
 * 筹码图最容易被当成"真实持仓成本"来用, 必须在看到图的同一时刻看到这个前提。
 */
import type { ChipResult } from '@/lib/chips'
import { cn } from '@/lib/cn'

interface Props {
  result: ChipResult
  /** 股票名称, 仅用于标题 */
  name?: string
  className?: string
}

function fmtPrice(v: number): string {
  return v >= 100 ? v.toFixed(1) : v.toFixed(2)
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded border border-border bg-elevated px-2 py-1" title={hint}>
      <div className="text-[10px] leading-tight text-fg-muted">{label}</div>
      <div className="font-mono text-[12px] leading-tight text-fg">{value}</div>
    </div>
  )
}

export function ChipDistributionPanel({ result, name, className }: Props) {
  const rows = result.bins
  const maxPct = Math.max(...rows.map(r => r.pct), 1e-9)
  const n = rows.length

  // 纵轴标注: 只标最高/最低/中位/均成本/现价, 标满会糊成一片
  const marks: { price: number; label: string; cls: string }[] = [
    { price: rows[n - 1].hi, label: fmtPrice(rows[n - 1].hi), cls: 'text-fg-muted' },
    { price: result.lastPrice, label: `现价 ${fmtPrice(result.lastPrice)}`, cls: 'text-sky-500' },
    { price: result.avgCost, label: `均成本 ${fmtPrice(result.avgCost)}`, cls: 'text-amber-500' },
    { price: rows[0].lo, label: fmtPrice(rows[0].lo), cls: 'text-fg-muted' },
  ]
  const span = rows[n - 1].hi - rows[0].lo || 1
  const posOf = (p: number) => ((rows[n - 1].hi - p) / span) * 100

  return (
    <div className={cn('rounded-md border border-border bg-surface p-2', className)}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-[12px] font-medium text-fg">筹码分布</span>
        {name && <span className="text-[11px] text-fg-muted">{name}</span>}
        <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-500">
          模型估算 · 非交易所持仓数据
        </span>
        <span className="text-[10px] text-fg-muted">
          {result.from} ~ {result.to} · {result.bars}根 · 衰减 {result.decay}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="平均成本" value={fmtPrice(result.avgCost)} hint="按筹码占比加权的价格均值" />
        <Stat
          label="获利盘"
          value={`${(result.profitRatio * 100).toFixed(1)}%`}
          hint="成本低于现价的筹码占比"
        />
        <Stat label="筹码峰" value={fmtPrice(result.peak)} hint="占比最大的价箱" />
        <Stat
          label="90%集中度"
          value={`${(result.concentration90 * 100).toFixed(1)}%`}
          hint="覆盖 90% 筹码所需的最窄价格区间 / 现价。越小越集中"
        />
        <Stat
          label="70%集中度"
          value={`${(result.concentration70 * 100).toFixed(1)}%`}
          hint="同上, 覆盖 70% 筹码"
        />
        <Stat
          label="区间累计换手"
          value={`${result.turnoverSum.toFixed(2)}×`}
          hint="窗口内成交量/流通股本之和。<1 说明上市以来的老筹码仍有残留, 结果偏'历史'"
        />
      </div>

      <div className="mt-2 flex gap-2">
        {/* 价格标尺 */}
        <div className="relative w-16 shrink-0" style={{ height: 230 }}>
          {marks.map((m, i) => (
            <span
              key={`${m.label}-${i}`}
              className={cn('absolute right-0 -translate-y-1/2 whitespace-nowrap font-mono text-[10px]', m.cls)}
              style={{ top: `${Math.max(0, Math.min(100, posOf(m.price)))}%` }}
            >
              {m.label}
            </span>
          ))}
        </div>

        {/* 筹码柱: 顶端 = 最高价, 底端 = 最低价 */}
        <div className="relative flex-1 overflow-hidden rounded border border-border/60 bg-elevated/40" style={{ height: 230 }}>
          {rows.map((r, i) => (
            <div
              key={i}
              className={r.side === 'profit' ? 'bg-[rgba(240,68,56,0.62)]' : 'bg-[rgba(59,130,246,0.45)]'}
              style={{
                position: 'absolute',
                left: 0,
                top: `${(i / n) * 100}%`,
                height: `${(1 / n) * 100 + 0.35}%`,
                width: `${Math.max((r.pct / maxPct) * 100, r.pct > 0 ? 0.8 : 0)}%`,
              }}
              title={`${fmtPrice(r.lo)} ~ ${fmtPrice(r.hi)}: ${(r.pct * 100).toFixed(2)}%`}
            />
          ))}
          {/* 现价基准线 */}
          <div
            className="pointer-events-none absolute inset-x-0 border-t border-dashed border-sky-400"
            style={{ top: `${Math.max(0, Math.min(100, posOf(result.lastPrice)))}%` }}
          />
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[10px] text-fg-muted">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 bg-[rgba(240,68,56,0.62)]" /> 获利盘(成本低于现价)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 bg-[rgba(59,130,246,0.45)]" /> 套牢盘(成本高于现价)
        </span>
        <span>
          口径: 每日换手率 = 成交量 / 流通股本({(result.floatShares / 1e8).toFixed(2)}亿股);
          新筹码在当日 [low, high] 呈三角形分布(中点最密); 老筹码按 (1 - 换手率 × {result.decay}) 逐日衰减
        </span>
      </div>
    </div>
  )
}
