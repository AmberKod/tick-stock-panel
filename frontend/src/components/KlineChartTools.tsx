/**
 * K 线工具条(主图画法 / K线内标注 / 画图) —— **与市场无关**的那一层。
 *
 * 为什么抽出来:
 *   这三样东西吃的都是同一串 OHLC, 不碰任何交易所/数据源差异 —— 主图只是把开高低收
 *   换成四种视觉编码, 标注是 OHLCV 的确定性派生, 画图锚点存的是「日期+价格」。
 *   所以港股/美股页不需要另写一套, 复用同一份即可(A股个股页 StockDetail 里原本
 *   内联的那一套, 这里按同样口径抽出来共用)。
 *
 * 不在这里的东西:
 *   周期/复权/分布/成交明细/同行 —— 那些要分市场接源, 留在各页面自己处理。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'

import { cn } from '@/lib/cn'
import {
  DRAW_TOOLS,
  loadDrawings,
  saveDrawings,
  type DrawTool,
  type Drawing,
} from '@/lib/chartDrawings'
import { KLINE_SIGNALS, type SignalKey } from '@/lib/klineSignals'
import {
  MAIN_CHART_TYPES,
  type ChartPriceLine,
  type MainChartType,
} from '@/components/EChartsCandlestick'

export interface KlineTools {
  /** 主图画法(蜡烛/空心/美国线/收盘线)。 */
  mainType: MainChartType
  setMainType: (next: MainChartType) => void
  /** K线内标注开关。默认开「放量」。 */
  signals: ReadonlySet<SignalKey>
  toggleSignal: (key: SignalKey) => void
  /** 当前坐标系下才有效的图形(周期/复权档不匹配的会被过滤掉)。 */
  visibleDrawings: Drawing[]
  /** 这份标的上画过的全部图形数 —— 用于提示"有几条没显示"。 */
  hiddenDrawings: number
  drawTool: DrawTool | null
  setDrawTool: (next: DrawTool | null) => void
  persistDrawings: (next: Drawing[]) => void
  /** 双击图表加的水平线。 */
  priceLines: ChartPriceLine[]
  addPriceLine: (price: number) => void
  removePriceLine: (index: number) => void
  /** 坐标上下文: `${period}|${mkline ?? ''}|${adjust}` —— 图形只对同一坐标体系生效。 */
  ctx: string
}

/**
 * @param symbol 标的(画图/水平线按标的隔离存本地)
 * @param ctx    当前视图坐标上下文, 形如 `day||none`
 * @param ns     localStorage 命名空间(按市场区分口味: hkDetail / usDetail)
 */
export function useKlineTools(symbol: string, ctx: string, ns: string): KlineTools {
  const [mainType, setMainTypeState] = useState<MainChartType>(() => {
    const raw = localStorage.getItem(`${ns}:mainType`)
    return MAIN_CHART_TYPES.some(t => t.value === raw) ? (raw as MainChartType) : 'candle'
  })
  const setMainType = useCallback((next: MainChartType) => {
    setMainTypeState(next)
    try { localStorage.setItem(`${ns}:mainType`, next) } catch { /* 忽略 */ }
  }, [ns])

  const [signals, setSignals] = useState<ReadonlySet<SignalKey>>(
    () => new Set<SignalKey>(['volume']),
  )
  const toggleSignal = useCallback((key: SignalKey) => {
    setSignals(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  const [drawings, setDrawings] = useState<Drawing[]>(() => loadDrawings(symbol))
  const [drawTool, setDrawTool] = useState<DrawTool | null>(null)
  const persistDrawings = useCallback((next: Drawing[]) => {
    setDrawings(next)
    saveDrawings(symbol, next)
  }, [symbol])
  // 换标的 → 换成那只标的的图形, 并退出画图模式(免得手上的工具误画到新标的)
  useEffect(() => {
    setDrawings(loadDrawings(symbol))
    setDrawTool(null)
  }, [symbol])

  const visibleDrawings = useMemo(() => drawings.filter(d => d.ctx === ctx), [drawings, ctx])
  const hiddenDrawings = drawings.length - visibleDrawings.length

  const [priceLines, setPriceLines] = useState<ChartPriceLine[]>(() => {
    try {
      const raw = localStorage.getItem(`${ns}:priceLines:${symbol}`)
      const parsed = raw ? JSON.parse(raw) : []
      return Array.isArray(parsed) ? parsed.filter((x: any) => Number.isFinite(x?.value)) : []
    } catch {
      return []
    }
  })
  useEffect(() => {
    // 换标的 → 换成那只标的的水平线(价位体系完全不同, 混着会指错)
    try {
      const raw = localStorage.getItem(`${ns}:priceLines:${symbol}`)
      const parsed = raw ? JSON.parse(raw) : []
      setPriceLines(Array.isArray(parsed) ? parsed.filter((x: any) => Number.isFinite(x?.value)) : [])
    } catch {
      setPriceLines([])
    }
  }, [ns, symbol])
  const writePriceLines = useCallback((next: ChartPriceLine[]) => {
    setPriceLines(next)
    try {
      localStorage.setItem(`${ns}:priceLines:${symbol}`, JSON.stringify(next))
    } catch { /* 存不下不影响看图 */ }
  }, [ns, symbol])
  const addPriceLine = useCallback((price: number) => {
    writePriceLines([...priceLines, { value: price, label: price.toFixed(2), color: '#F59E0B' }])
  }, [priceLines, writePriceLines])
  const removePriceLine = useCallback((index: number) => {
    writePriceLines(priceLines.filter((_, j) => j !== index))
  }, [priceLines, writePriceLines])

  return {
    mainType,
    setMainType,
    signals,
    toggleSignal,
    visibleDrawings,
    hiddenDrawings,
    drawTool,
    setDrawTool,
    persistDrawings,
    priceLines,
    addPriceLine,
    removePriceLine,
    ctx,
  }
}

/** 工具条本体。纯展示 + 回调, 状态由调用方持有(页面要拿 state 喂给 StockDailyKChart)。 */
export function KlineToolsBar({ tools, className }: { tools: KlineTools; className?: string }) {
  const { drawTool } = tools
  return (
    <div className={className}>
      <div className="flex flex-wrap items-center gap-1.5 px-1 text-[11px]">
        {/* ── 主图画法: 四种画法共用同一份 OHLC, 换的只是视觉编码 ── */}
        <span className="text-muted">主图</span>
        <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
          {MAIN_CHART_TYPES.map(t => (
            <button
              key={t.value}
              type="button"
              onClick={() => tools.setMainType(t.value)}
              title={t.hint}
              className={cn('rounded px-2 py-0.5 transition-colors',
                tools.mainType === t.value
                  ? 'bg-surface text-foreground shadow-sm'
                  : 'text-muted hover:text-secondary')}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* ── K线内标注: 确定性派生, 不是打分也不是买卖建议 ── */}
        <span className="mx-1 h-4 w-px bg-border" />
        <span className="text-muted">标注</span>
        {KLINE_SIGNALS.map(s => (
          <button
            key={s.key}
            type="button"
            onClick={() => tools.toggleSignal(s.key)}
            title={s.desc}
            className={cn('rounded border px-1.5 py-0.5 transition-colors',
              tools.signals.has(s.key)
                ? 'border-sky-400/40 bg-sky-400/10 text-sky-500'
                : 'border-border text-muted hover:text-secondary')}
          >
            {s.label}
          </button>
        ))}

        {/* ── 画图工具: 画的是标注不是数据, 只存本地 ── */}
        <span className="mx-1 h-4 w-px bg-border" />
        <span className="text-muted">画图</span>
        {DRAW_TOOLS.map(t => (
          <button
            key={t.value}
            type="button"
            onClick={() => tools.setDrawTool(tools.drawTool === t.value ? null : t.value)}
            title={t.hint}
            className={cn('rounded border px-1.5 py-0.5 transition-colors',
              drawTool === t.value
                ? 'border-amber-400/40 bg-amber-400/10 text-amber-500'
                : 'border-border text-muted hover:text-secondary')}
          >
            {t.label}
          </button>
        ))}
        {(tools.visibleDrawings.length > 0 || tools.hiddenDrawings > 0) && (
          <button
            type="button"
            onClick={() => tools.persistDrawings([])}
            title="清除这只标的在本地存的所有图形(只清本地存储)"
            className="rounded border border-border px-1.5 py-0.5 text-muted transition-colors hover:border-red-400/40 hover:text-red-400"
          >
            清除 {tools.visibleDrawings.length + tools.hiddenDrawings}
          </button>
        )}

        <span className="ml-auto text-[11px] text-muted">
          双击图表可加水平线 · 滚轮缩放 · 拖动平移
        </span>
      </div>

      {/* 画图的代价要提前说清: 图层会吃掉鼠标事件, 这段时间缩放/平移用不了。 */}
      {drawTool && (
        <div className="flex flex-wrap items-baseline gap-x-2 px-1 text-[10.5px] text-muted">
          <span className="text-amber-500">画图模式</span>
          <span>
            滚轮缩放与拖动平移暂时不可用（鼠标事件归画图层），画完自动退出、随即恢复。
            删图形：点中它 → 按 Delete 或点红色 ✕；拖动两端圆点可改位置；画到一半按 Esc 取消。
            {drawTool === 'channel' && ' 平行通道分两步：先拖出基准线，再点一下定宽度。'}
            {drawTool === 'text' && ' 文字标注就地输入，回车确认（留空自动丢弃），双击已画的文字可改。'}
          </span>
        </div>
      )}
      {/* 别处 canvas 坐标系下画的图形不显示 —— 说明原因, 而不是默默消失 */}
      {tools.hiddenDrawings > 0 && (
        <div className="px-1 text-[10.5px] text-muted">
          另有 {tools.hiddenDrawings} 条图形是在
          <span className="text-secondary">其他周期/复权档</span>
          下画的（价格体系不同），当前视图不显示 —— 切回去就能看到。
        </div>
      )}
      {tools.priceLines.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-1 text-[11px]">
          <span className="text-muted">水平线</span>
          {tools.priceLines.map((l, i) => (
            <span key={`${l.value}-${i}`}
                  className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 font-mono text-secondary">
              {l.label ?? l.value.toFixed(2)}
              <button
                type="button"
                onClick={() => tools.removePriceLine(i)}
                className="text-muted hover:text-foreground"
                title="删除这条水平线"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
