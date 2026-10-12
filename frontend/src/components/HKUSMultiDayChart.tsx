import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, LineChart, Loader2 } from 'lucide-react'

import { request } from '@/lib/api'
import { chartTheme, useTheme } from '@/lib/theme'

/**
 * 港美股**多日分时** —— /api/{hk,us}/deep/multiday(通达信 MAC-EX)。
 *
 * 与"当日分时"的关系: 那一份是 m1 合成的单个会话; 这一份是最近 N 个交易日,
 * 每天自带昨收, 后端三重守卫校验过才下发(详见 provider):
 *   ① 0x240C 的每一天必须落在日K 交易日历里;
 *   ② 当日最后一根价格 == 日K 当日收盘(容差 0.5%);
 *   ③ 相邻两天在日历里必须是相邻交易日(中间不漏)。
 * 任一不满足就 available:false, 组件照显示原因, 不用当日曲线拼凑冒充多日。
 *
 * ⚠️ 美股会话跨北京时间零点: 21:30 开盘 → 次日 03:59/04:00 收盘, 所以时间轴
 * 会走到 00:00 以后; 这不是日期错乱, 是"同一会话的北京钟点"。
 */
interface Point {
  t: string
  price: number | null
  avg: number | null
  volume: number | null
}

interface Day {
  date: string | null
  prev_close: number | null
  close?: number | null
  points: Point[]
}

interface Resp {
  available: boolean
  source?: string
  reason?: string
  protocol?: string
  prev_close_source?: string
  days?: Day[]
}

const LINE_COLORS = ['#2563EB', '#16A34A', '#F59E0B', '#DB2777', '#7C3AED',
  '#0891B2', '#DC2626', '#4D7C0F', '#9333EA', '#EA580C']

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-start gap-2 py-3 text-xs leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-fg-muted'}`}>
      {loading ? <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        : warn ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

function marketOf(symbol: string): 'hk' | 'us' | null {
  const s = symbol.trim().toUpperCase()
  if (s.endsWith('.HK')) return 'hk'
  if (s.endsWith('.US')) return 'us'
  return null
}

export function HKUSMultiDayChart({ symbol, days = 5, className = '' }: {
  symbol: string
  days?: number
  className?: string
}) {
  const theme = useTheme()  // 订阅主题变化 → 下面 setOption 重跑, 图跟着换色
  const hostRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)
  const [relative, setRelative] = useState(true)

  const market = marketOf(symbol)
  const isUS = market === 'us'
  const query = useQuery({
    queryKey: ['hkus', 'multiday', symbol, days],
    queryFn: () => request<Resp>(
      `/api/${market}/deep/multiday/${encodeURIComponent(symbol)}?days=${days}`),
    enabled: market != null,
    staleTime: 60_000,
  })

  const resp = query.data?.available ? query.data : null
  const dayList = resp?.days ?? []

  // 以最长那天的分钟轴为基准: 港股每天 330 点、美股 390 点, 同为完整交易日时
  // 天然按 index 对齐; 当天未收盘(美股夜里进行中)会短一截, 尾部是 null。
  const axis = useMemo(() => {
    if (!dayList.length) return [] as string[]
    const lens = dayList.map(d => d.points.length)
    return dayList[lens.indexOf(Math.max(...lens))]?.points.map(p => p.t) ?? []
  }, [dayList])

  const crossingMidnight = useMemo(
    () => axis.some(t => t <= '12:00') && axis.some(t => t >= '20:00'),
    [axis])

  useEffect(() => {
    if (!hostRef.current) return
    const chart = echarts.init(hostRef.current, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    return () => {
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !dayList.length) return
    const t = chartTheme(theme)
    const series = dayList.map((d, i) => ({
      name: d.date ?? `D${i}`,
      type: 'line' as const,
      smooth: false,
      symbol: 'none' as const,
      sampling: 'lttb' as const,
      lineStyle: {
        width: i === dayList.length - 1 ? 2 : 1.2,
        color: LINE_COLORS[i % LINE_COLORS.length],
      },
      data: d.points.map(p => {
        const px = p.price
        if (px == null) return null
        if (!relative) return px
        return d.prev_close ? Number((((px - d.prev_close) / d.prev_close) * 100).toFixed(3)) : null
      }),
    }))

    const option: EChartsOption = {
      backgroundColor: 'transparent',
      animation: false,
      grid: { left: 52, right: 16, top: 24, bottom: 24 },
      legend: {
        top: 0,
        itemWidth: 14,
        itemHeight: 8,
        textStyle: { color: t.text, fontSize: 10 },
        data: series.map(s => s.name ?? ''),
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.textStrong, fontSize: 11 },
        valueFormatter: v => (v == null ? '—' : `${Number(v).toFixed(2)}${relative ? '%' : ''}`),
      },
      xAxis: {
        type: 'category',
        data: axis,
        boundaryGap: false,
        axisLabel: {
          color: t.text,
          fontSize: 10,
          interval: Math.max(1, Math.floor(axis.length / 8)),
        },
        axisLine: { lineStyle: { color: t.border } },
      },
      yAxis: {
        type: 'value',
        scale: true,
        splitLine: { lineStyle: { color: t.grid } },
        axisLabel: {
          color: t.text,
          fontSize: 10,
          formatter: (v: number) => (relative ? `${v.toFixed(1)}%` : `${v.toFixed(2)}`),
        },
      },
      series: [
        ...series,
        // 昨收基线: 相对模式下就是 0%, 直观显示每天是站上还是跌破昨收
        ...(relative ? [{
          type: 'line' as const,
          name: '昨收',
          symbol: 'none' as const,
          data: axis.map(() => 0),
          lineStyle: { color: '#9CA3AF', width: 1, type: 'dashed' as const },
          tooltip: { show: false },
        }] : []),
      ],
    }
    chart.setOption(option, true)
    chart.resize()
  }, [dayList, axis, relative, theme])

  useEffect(() => {
    const el = hostRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => chartRef.current?.resize())
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  if (market == null) return null

  return (
    <section className={`rounded-lg border border-border bg-surface p-3 ${className}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <LineChart className="h-3.5 w-3.5 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">多日分时</h3>
        <span className="text-xs text-fg-muted">MAC-EX · 最近 {days} 个交易日</span>
        <div className="ml-auto inline-flex items-center rounded border border-border bg-elevated p-0.5">
          {([['相对昨收%', true], ['绝对价格', false]] as const).map(([label, val]) => (
            <button
              key={label}
              type="button"
              onClick={() => setRelative(val)}
              className={`rounded px-2 py-0.5 text-[11px] transition-colors ${
                relative === val ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary'
              }`}
            >{label}</button>
          ))}
        </div>
      </div>

      {query.isLoading && <StateLine loading text="加载多日分时…" />}
      {!query.isLoading && !resp && (
        <StateLine warn text={`不可用: ${query.data?.reason ?? query.error?.message ?? '无返回'}`} />
      )}
      {dayList.length > 0 && (
        <>
          <div ref={hostRef} className="h-[240px] w-full" />
          <p className="mt-1.5 text-[11px] leading-relaxed text-fg-muted">
            时间为北京时间；昨收取日K 前一交易日收盘（真源）。后端三重守卫：日期必须在日K
            交易日历内、当日末价必须等于日K 收盘、相邻两日不漏 —— 任一不满足即不下发。
            「相对昨收%」模式下 0% 虚线即昨收基线。
            {crossingMidnight && (
              <span className="text-amber-600 dark:text-amber-500">
                {' '}注意：{isUS ? '美股' : '该'}会话跨北京时间零点（21:30 开盘 → 次日 04:00
                收盘），时间轴走到 00:00 之后属于同一会话，不是日期错乱。
              </span>
            )}
          </p>
        </>
      )}
    </section>
  )
}
