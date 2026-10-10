/**
 * K 线画图图层 —— 覆盖在 ECharts canvas 之上的 SVG 叠加层。
 *
 * 为什么不用 ECharts 的 graphic / markLine 去画:
 *   1. 画图要"按下→拖动→松手"的连续交互 + 端点把手 + 选中/删除, ECharts graphic 的
 *      拖放会和 dataZoom 的拖动平移抢同一批鼠标事件, 很难两全;
 *   2. 图形锚点要钉在**日期 + 价格**上, 缩放/平移后由 convertToPixel 现算像素,
 *      独立图层跟着重绘最直观。
 * 所以: canvas 只画 K 线, 这层只画用户自己画的东西, 互不污染。
 *
 * 事件纪律: 只有画图模式打开时这层才吃鼠标事件(pointer-events: auto);
 * 关掉时整层 pointer-events: none, 滚轮缩放 / 拖动平移 / 十字光标全部照旧。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ECharts } from 'echarts'
import {
  DRAW_COLORS,
  DRAW_TOOLS,
  fibLevels,
  newDrawingId,
  type DrawPoint,
  type Drawing,
  type DrawTool,
} from '@/lib/chartDrawings'

interface OHLCLike {
  date: string
  open: number
  high: number
  low: number
  close: number
}

interface Props {
  chart: ECharts | null
  /** 与 rows 同序的日期数组 */
  dates: string[]
  dateIndexMap: Map<string, number>
  /** 原始行, 用于把锚点价格吸附到开/高/低/收 */
  rows: OHLCLike[]
  drawings: Drawing[]
  /** 新建图形记下的坐标上下文(周期 + 复权档) */
  ctx: string
  /** null = 画图功能关闭(只显示已画的, 不吃鼠标事件) */
  tool: DrawTool | null
  onChange: (next: Drawing[]) => void
}

interface Draft {
  type: Exclude<DrawTool, 'cursor'>
  a: DrawPoint
  b: DrawPoint
}

interface EditState {
  id: string
  part: 'a' | 'b' | 'body'
  /** 拖动开始时的原始锚点 —— 整条平移要按"起点偏移"算, 不能逐帧累加 */
  origin: { a: DrawPoint; b: DrawPoint }
  start: DrawPoint
  a: DrawPoint
  b: DrawPoint
}

const HANDLE_R = 4

/** 价格吸附: 靠近某根 K 的开/高/低/收(相对差 < 0.6%)就吸上去。
 *  画趋势线就是想贴着高点/低点走 —— 手抖一个像素就偏了, 吸附省掉这一下。 */
function snapPrice(price: number, row: OHLCLike | undefined): number {
  if (!row || !Number.isFinite(price) || price <= 0) return price
  let best = price
  let bestDiff = 1
  for (const v of [row.open, row.high, row.low, row.close]) {
    if (!Number.isFinite(v) || v <= 0) continue
    const diff = Math.abs(price - v) / price
    if (diff < bestDiff && diff < 0.006) {
      best = v
      bestDiff = diff
    }
  }
  return best
}

export function ChartDrawingLayer({
  chart, dates, dateIndexMap, rows, drawings, ctx, tool, onChange,
}: Props) {
  const [tick, setTick] = useState(0)
  const [size, setSize] = useState({ w: 0, h: 0 })
  const [draft, setDraft] = useState<Draft | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [edit, setEdit] = useState<EditState | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  // 事件回调里要读最新值, 但不能因为更新它们就重建监听
  const stateRef = useRef({ drawings, tool, draft, edit, selected })
  stateRef.current = { drawings, tool, draft, edit, selected }

  const active = tool !== null
  const hasAny = drawings.length > 0 || draft !== null
  void tick   // 缩放/重绘后让像素坐标重算

  // 尺寸: 画布宽度用来把"向右延伸"的线画到边
  useEffect(() => {
    const el = rootRef.current
    if (!el) return
    const read = () => setSize({ w: el.clientWidth, h: el.clientHeight })
    read()
    const ro = new ResizeObserver(read)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 图重绘 / 缩放 → 重新取一次像素坐标。
  // 没有任何图形时不订阅, 免得每次 hover 都白跑一次 rAF。
  useEffect(() => {
    if (!chart || !hasAny) return
    let raf = 0
    const bump = () => {
      if (raf) return
      raf = window.requestAnimationFrame(() => {
        raf = 0
        setTick(t => t + 1)
      })
    }
    chart.on('finished', bump)
    chart.on('dataZoom', bump)
    return () => {
      if (raf) window.cancelAnimationFrame(raf)
      chart.off('finished', bump)
      chart.off('dataZoom', bump)
    }
  }, [chart, hasAny])

  // ===== 坐标换算 =====
  const idxOf = useCallback((date: string) => dateIndexMap.get(date) ?? -1, [dateIndexMap])

  const toPixel = useCallback((date: string, price: number): [number, number] | null => {
    if (!chart) return null
    const idx = idxOf(date)
    if (idx < 0) return null
    const p = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [idx, price]) as unknown
    if (!Array.isArray(p)) return null
    const [x, y] = p as number[]
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null
    return [x, y]
  }, [chart, idxOf])

  /** 鼠标位置 → (日期, 吸附后的价格) */
  const pointAt = useCallback((clientX: number, clientY: number): DrawPoint | null => {
    const el = rootRef.current
    if (!el || !chart || dates.length === 0) return null
    const rect = el.getBoundingClientRect()
    const px: [number, number] = [clientX - rect.left, clientY - rect.top]
    const c = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, px) as unknown
    if (!Array.isArray(c)) return null
    const [idxF, price] = c as number[]
    if (!Number.isFinite(idxF) || !Number.isFinite(price)) return null
    const idx = Math.max(0, Math.min(dates.length - 1, Math.round(idxF)))
    return { date: dates[idx], price: snapPrice(price, rows[idx]) }
  }, [chart, dates, rows])

  /** 落点必须在主图(蜡烛区)里 —— 在成交量副图上按下不该画出一条价格线 */
  const insideMainGrid = useCallback((clientX: number, clientY: number): boolean => {
    const el = rootRef.current
    if (!el || !chart) return false
    const rect = el.getBoundingClientRect()
    return !!chart.containPixel({ gridIndex: 0 }, [clientX - rect.left, clientY - rect.top])
  }, [chart])

  const pixelDist = useCallback((a: DrawPoint, b: DrawPoint): number => {
    const pa = toPixel(a.date, a.price)
    const pb = toPixel(b.date, b.price)
    if (!pa || !pb) return 0
    return Math.hypot(pb[0] - pa[0], pb[1] - pa[1])
  }, [toPixel])

  // ===== 画新图形 =====
  const startDraft = useCallback((e: React.MouseEvent) => {
    if (!tool || tool === 'cursor') return
    if (!insideMainGrid(e.clientX, e.clientY)) return
    const p = pointAt(e.clientX, e.clientY)
    if (!p) return
    e.preventDefault()
    setSelected(null)
    setDraft({ type: tool, a: p, b: p })
  }, [tool, pointAt, insideMainGrid])

  useEffect(() => {
    if (!draft) return
    const move = (e: MouseEvent) => {
      const p = pointAt(e.clientX, e.clientY)
      if (p) setDraft(d => (d ? { ...d, b: p } : d))
    }
    const up = () => {
      const d = stateRef.current.draft
      setDraft(null)
      if (!d) return
      // 水平线"点一下就成"; 其余要有拖动距离才算, 防手滑点出一个点
      if (d.type !== 'hline' && pixelDist(d.a, d.b) < 6) return
      const next: Drawing = {
        id: newDrawingId(), type: d.type, a: d.a, b: d.b, ctx,
        color: DRAW_COLORS[d.type],
      }
      setSelected(next.id)
      onChange([...stateRef.current.drawings, next])
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [draft, pointAt, pixelDist, onChange, ctx])

  // ===== 编辑(拖端点 / 整条平移) =====
  useEffect(() => {
    if (!edit) return
    const move = (e: MouseEvent) => {
      const p = pointAt(e.clientX, e.clientY)
      if (!p) return
      setEdit(prev => {
        if (!prev) return prev
        if (prev.part === 'a') return { ...prev, a: p, b: prev.origin.b }
        if (prev.part === 'b') return { ...prev, a: prev.origin.a, b: p }
        const dPrice = p.price - prev.start.price
        const dIdx = (idxOf(p.date) >= 0 ? idxOf(p.date) : 0) - idxOf(prev.start.date)
        const shift = (pt: DrawPoint): DrawPoint => {
          const i0 = idxOf(pt.date)
          const i1 = i0 < 0 ? i0 : Math.max(0, Math.min(dates.length - 1, i0 + dIdx))
          return { date: i1 >= 0 ? dates[i1] : pt.date, price: pt.price + dPrice }
        }
        return { ...prev, a: shift(prev.origin.a), b: shift(prev.origin.b) }
      })
    }
    const up = () => {
      const ed = stateRef.current.edit
      setEdit(null)
      if (!ed) return
      const cur = stateRef.current.drawings
      if (ed.a.price === ed.origin.a.price && ed.b.price === ed.origin.b.price
          && ed.a.date === ed.origin.a.date && ed.b.date === ed.origin.b.date) return
      onChange(cur.map(d => (d.id === ed.id ? { ...d, a: ed.a, b: ed.b } : d)))
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
    return () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
  }, [edit, pointAt, onChange, dates, idxOf])

  // ===== 删除 / 取消 =====
  useEffect(() => {
    if (!selected) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const tag = (e.target as HTMLElement | null)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return
      e.preventDefault()
      onChange(stateRef.current.drawings.filter(d => d.id !== selected))
      setSelected(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected, onChange])

  const remove = (id: string) => {
    onChange(drawings.filter(d => d.id !== id))
    if (selected === id) setSelected(null)
  }

  const startEdit = (e: React.MouseEvent, d: Drawing, part: 'a' | 'b' | 'body') => {
    if (tool !== 'cursor') return
    e.stopPropagation()
    e.preventDefault()
    setSelected(d.id)
    const start = pointAt(e.clientX, e.clientY) ?? d.a
    setEdit({ id: d.id, part, origin: { a: d.a, b: d.b }, start, a: d.a, b: d.b })
  }

  const renderHandle = (p: DrawPoint, onDown: (e: React.MouseEvent) => void) => {
    const px = toPixel(p.date, p.price)
    if (!px) return null
    return (
      <circle cx={px[0]} cy={px[1]} r={HANDLE_R}
              fill="#fff" stroke="#F59E0B" strokeWidth={1.5}
              className="cursor-move" onMouseDown={onDown} />
    )
  }

  const renderX = (x: number, y: number, id: string) => (
    <g className="cursor-pointer" onMouseDown={e => { e.stopPropagation(); remove(id) }}>
      <circle cx={x} cy={y} r={7} fill="#EF4444" />
      <text x={x} y={y + 3.5} fontSize={9} textAnchor="middle" fill="#fff">✕</text>
    </g>
  )

  /** 一条图形。选中时显示端点把手 + 删除按钮。 */
  const renderShape = (d: Drawing) => {
    const color = d.color ?? DRAW_COLORS[d.type]
    const pa = toPixel(d.a.date, d.a.price)
    const pb = toPixel(d.b.date, d.b.price)
    if (!pa || !pb) return null   // 锚点日期不在当前数据里 → 不画, 不猜位置
    // 端点把手只在"选择"模式下给 —— 画图模式下弹出把手会挡住下一次落笔
    const isSel = selected === d.id && tool === 'cursor'
    const sw = isSel ? 2 : 1.4
    const right = Math.max(size.w - 4, 0)

    if (d.type === 'hline') {
      const y = pa[1]
      return (
        <g key={d.id}>
          <line x1={0} x2={size.w} y1={y} y2={y} stroke={color} strokeWidth={sw} strokeDasharray="5 4"
                className="cursor-ns-resize" onMouseDown={e => startEdit(e, d, 'body')} />
          <rect x={2} y={y - 8} width={46} height={15} rx={2} fill={color} opacity={0.92} />
          <text x={5} y={y + 3} fontSize={10} fill="#0b1020" fontFamily="ui-monospace,monospace">
            {d.a.price.toFixed(2)}
          </text>
          {isSel && (
            <g>
              {renderHandle(d.a, e => startEdit(e, d, 'body'))}
              {renderX(56, y, d.id)}
            </g>
          )}
        </g>
      )
    }

    if (d.type === 'rect') {
      const x0 = Math.min(pa[0], pb[0]); const x1 = Math.max(pa[0], pb[0])
      const y0 = Math.min(pa[1], pb[1]); const y1 = Math.max(pa[1], pb[1])
      return (
        <g key={d.id}>
          <rect x={x0} y={y0} width={Math.max(x1 - x0, 0)} height={Math.max(y1 - y0, 0)}
                fill={color} fillOpacity={0.14} stroke={color} strokeWidth={sw}
                strokeDasharray={isSel ? '4 3' : undefined} className="cursor-move"
                onMouseDown={e => startEdit(e, d, 'body')} />
          {isSel && (
            <g>
              {renderHandle(d.a, e => startEdit(e, d, 'a'))}
              {renderHandle(d.b, e => startEdit(e, d, 'b'))}
              {renderX(x1, y0, d.id)}
            </g>
          )}
        </g>
      )
    }

    if (d.type === 'fib') {
      const xStart = Math.min(pa[0], pb[0])
      const yTop = Math.min(pa[1], pb[1])
      const yBot = Math.max(pa[1], pb[1])
      return (
        <g key={d.id} className="cursor-move" onMouseDown={e => startEdit(e, d, 'body')}>
          {fibLevels(d.a.price, d.b.price).map(({ ratio, price }) => {
            const p = toPixel(d.a.date, price)
            if (!p) return null
            const strong = ratio === 0 || ratio === 0.5 || ratio === 1
            return (
              <g key={ratio}>
                <line x1={xStart} x2={right} y1={p[1]} y2={p[1]} stroke={color}
                      strokeWidth={strong ? sw : 1} opacity={0.9}
                      strokeDasharray={strong ? undefined : '4 4'} />
                <text x={xStart + 3} y={p[1] - 2} fontSize={9.5} fill={color}
                      fontFamily="ui-monospace,monospace">
                  {(ratio * 100).toFixed(1)}% · {price.toFixed(2)}
                </text>
              </g>
            )
          })}
          <line x1={xStart} x2={xStart} y1={yTop} y2={yBot} stroke={color} strokeWidth={1} opacity={0.55} />
          {isSel && (
            <g>
              {renderHandle(d.a, e => startEdit(e, d, 'a'))}
              {renderHandle(d.b, e => startEdit(e, d, 'b'))}
              {renderX(xStart + 8, yTop, d.id)}
            </g>
          )}
        </g>
      )
    }

    // trend: 两点确定, 向右延伸到画布右边缘
    const dx = pb[0] - pa[0]
    const dy = pb[1] - pa[1]
    const x2 = dx === 0 ? pb[0] : right
    const y2 = dx === 0 ? pb[1] : pb[1] + (right - pb[0]) * (dy / dx)
    return (
      <g key={d.id}>
        <line x1={pa[0]} y1={pa[1]} x2={x2} y2={y2} stroke={color} strokeWidth={sw}
              className="cursor-move" onMouseDown={e => startEdit(e, d, 'body')} />
        {isSel && (
          <g>
            {renderHandle(d.a, e => startEdit(e, d, 'a'))}
            {renderHandle(d.b, e => startEdit(e, d, 'b'))}
            {renderX(pb[0] + 10, pb[1], d.id)}
          </g>
        )}
      </g>
    )
  }

  /** 画到一半的预览 */
  const renderDraft = () => {
    if (!draft) return null
    const pa = toPixel(draft.a.date, draft.a.price)
    const pb = toPixel(draft.b.date, draft.b.price)
    if (!pa || !pb) return null
    const color = DRAW_COLORS[draft.type]
    if (draft.type === 'hline') {
      return <line x1={0} x2={size.w} y1={pa[1]} y2={pa[1]} stroke={color}
                   strokeWidth={1.4} strokeDasharray="5 4" opacity={0.8} />
    }
    if (draft.type === 'rect') {
      return (
        <rect x={Math.min(pa[0], pb[0])} y={Math.min(pa[1], pb[1])}
              width={Math.abs(pb[0] - pa[0])} height={Math.abs(pb[1] - pa[1])}
              fill={color} fillOpacity={0.12} stroke={color} strokeWidth={1.2} strokeDasharray="4 3" />
      )
    }
    if (draft.type === 'fib') {
      const x0 = Math.min(pa[0], pb[0])
      return fibLevels(draft.a.price, draft.b.price).map(({ ratio, price }) => {
        const p = toPixel(draft.a.date, price)
        if (!p) return null
        return (
          <g key={ratio}>
            <line x1={x0} x2={Math.max(size.w - 4, 0)} y1={p[1]} y2={p[1]} stroke={color}
                  strokeWidth={ratio === 0.5 ? 1.4 : 1} opacity={0.75} strokeDasharray="4 4" />
            <text x={x0 + 3} y={p[1] - 2} fontSize={9.5} fill={color}
                  fontFamily="ui-monospace,monospace">
              {(ratio * 100).toFixed(1)}% · {price.toFixed(2)}
            </text>
          </g>
        )
      })
    }
    const dx = pb[0] - pa[0]
    const dy = pb[1] - pa[1]
    const x2 = dx === 0 ? pb[0] : Math.max(size.w - 4, 0)
    const y2 = dx === 0 ? pb[1] : pb[1] + (x2 - pb[0]) * (dy / dx)
    return <line x1={pa[0]} y1={pa[1]} x2={x2} y2={y2} stroke={color} strokeWidth={1.4} opacity={0.85} />
  }

  const cursorCls = tool && tool !== 'cursor' ? 'cursor-crosshair' : 'cursor-default'

  return (
    <div
      ref={rootRef}
      className={`absolute inset-0 ${cursorCls}`}
      style={{ pointerEvents: active ? 'auto' : 'none' }}
      onMouseDown={e => {
        if (tool === 'cursor') setSelected(null)   // 点空白 = 取消选中
        startDraft(e)
      }}
    >
      <svg width={size.w || '100%'} height={size.h || '100%'} className="overflow-visible">
        {drawings.map(d => renderShape(edit && edit.id === d.id
          ? { ...d, a: edit.a, b: edit.b }
          : d))}
        {renderDraft()}
      </svg>
      {tool && tool !== 'cursor' && (
        <div className="pointer-events-none absolute left-1/2 top-1 -translate-x-1/2 rounded bg-fg/85 px-2 py-0.5 text-[10px] text-bg">
          {DRAW_TOOLS.find(t => t.value === tool)?.hint}
        </div>
      )}
    </div>
  )
}
