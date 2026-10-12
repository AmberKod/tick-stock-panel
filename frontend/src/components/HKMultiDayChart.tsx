import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, LineChart, Loader2 } from 'lucide-react'

import { request } from '@/lib/api'
import { chartTheme, useTheme } from '@/lib/theme'

/**
 * 港股**多日分时** —— /api/hk/deep/multiday(通达信 MAC-EX, 上游直接给每日一块)。
 *
 * 与"当日分时"的关系: 那一份是 m1 合成的单个会话; 这一份是上游按交易日切好的
 * 5 天, 每天自带昨收(pre_close), 并且后端逐日校验过「较晚那天的昨收 == 相邻
 * 较早那天的收盘」—— 链对不上就 fail-closed, 不会画出错日期的曲线。
 *
 * 美股这里拿不到: 上游那几块不是"一天一块"(详见 provider 注释), 端点会返回
 * available:false + reason, 组件照显示原因, 不用当日曲线拼凑冒充多日。
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
  points: Point[]
}

interface Resp {
  available: boolean
  source?: string
  reason?: string
  name?: string | null
  days?: Day[]
}

const LINE_COLORS = ['#2563EB', '#16A34A', '#F59E0B', '#DB2777', '#7C3AED']

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-start gap-2 py-3 text-xs leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-fg-muted'}`}>
      {loading ? <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        : warn ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

export function HKMultiDayChart({ symbol, className = '' }: {
  symbol: string
  className?: string
}) {
  const theme = useTheme()  // 订阅主题变化 → 下面 setOption 重跑, 图跟着换色
  const hostRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)
  const [relative, setRelative] = useState(true)

  const isHK = symbol.trim().toUpperCase().endsWith('.HK')
  const query = useQuery({
    queryKey: ['hkus', 'multiday', symbol],
    queryFn: () => request<Resp>(
      `/api/hk/deep/multiday/${encodeURIComponent(symbol)}?days=5`),
    enabled: isHK,
    staleTime: 60_000,
  })

  const days = query.data?.available ? query.data.days ?? [] : []

  // 以最新一天的时间轴为基准: 港股每天 330 点(09:30~15:59), 跨日天然对齐。
  const axis = useMemo(() => {
    if (!days.length) return [] as string[]
    const lens = days.map(d => d.points.length)
    return days[lens.indexOf(Math.max(...lens))]?.points.map(p => p.t) ?? []
  }, [days])

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
    if (!chart || !days.length) return
    const t = chartTheme(theme)
    const series = days.map((d, i) => ({
      name: d.date ?? `D${i}`,
      type: 'line' as const,
      smooth: false,
      symbol: 'none' as const,
      sampling: 'lttb' as const,
      lineStyle: { width: i === 0 ? 2 : 1.2, color: LINE_COLORS[i % LINE_COLORS.length] },
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
        valueFormatter: v => (v == null ? '—' : `${Number(v).toFixed(relative ? 2 : 2)}${relative ? '%' : ''}`),
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
  }, [days, axis, relative, theme])

  useEffect(() => {
    const el = hostRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => chartRef.current?.resize())
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  if (!isHK) return null

  return (
    <section className={`rounded-lg border border-border bg-surface p-3 ${className}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <LineChart className="h-3.5 w-3.5 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">多日分时</h3>
        <span className="text-xs text-fg-muted">MAC-EX · 最近 5 个交易日</span>
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
      {!query.isLoading && !query.data?.available && (
        <StateLine warn text={`不可用: ${query.data?.reason ?? query.error?.message ?? '无返回'}`} />
      )}
      {days.length > 0 && (
        <>
          <div ref={hostRef} className="h-[240px] w-full" />
          <p className="mt-1.5 text-[11px] leading-relaxed text-fg-muted">
            时间为北京时间；每日昨收由上游自带，后端逐日校验「较晚那天的昨收 = 相邻较早那天的收盘」，
            对不上就不下发。「相对昨收%」模式下 0% 虚线即昨收基线。
          </p>
        </>
      )}
    </section>
  )
}
