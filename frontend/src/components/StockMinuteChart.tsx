/**
 * A股**当日分时图**(腾讯 web.ifzq.gtimg.cn) —— 分时线 / 均价线 / 分时量。
 *
 * 为什么单开一个组件而不是塞进 EChartsCandlestick: 分时是**分钟粒度 + 上下对称
 * 于昨收**的另一套坐标体系, 跟蜡烛图共享 option 只会互相迁就 —— 蜡烛要 date 轴
 * 和 log/线性切换, 分时要固定 09:30~15:00 时间轴和"昨收为中轴"的对称区间。
 *
 * 数据纪律(与后端 provider 注释一致):
 *   - 后端返回的 volume/amount 已按差分还原成**每分钟**增量(腾讯原始是累计值);
 *   - 均价线是后端按 累计成交额/累计成交量 算的, 前端不重算(重算就是另一条线);
 *   - 拿不到(非交易日/未开盘/非A股)就显式显示原因, 不画空图、不沿用上一日。
 */
import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { Loader2 } from 'lucide-react'

import { request } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { QK } from '@/lib/queryKeys'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'

/** 与后端 app/api/astock_deep.py::minute_bars 对齐。 */
interface MinutePoint {
  t: string
  price: number
  avg: number
  pct: number
  volume: number
  amount: number
}

interface MinuteResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    name: string | null
    date: string | null
    prev_close: number
    open: number | null
    last_price: number
    last_pct: number
    high: number
    low: number
    volume: number
    amount: number
    points: MinutePoint[]
    updated_at: string
  }
}

// 与 EChartsCandlestick 同一套涨跌色(A股: 红涨绿跌)。
const BULL = '#C74040'
const BEAR = '#2D9B65'
const AVG = '#F59E0B'

/** 横轴只在整点/半点打标, 267 个点全打会糊成一片。 */
const AXIS_TICKS = new Set(['09:30', '10:30', '11:30', '13:00', '14:00', '15:00'])

/** 交易时段内才轮询; 收盘后端数据不再变, 没必要继续打 provider。 */
function inTradingWindow(): boolean {
  const now = new Date()
  const dow = now.getDay()
  if (dow === 0 || dow === 6) return false
  const m = now.getHours() * 60 + now.getMinutes()
  return m >= 9 * 60 + 15 && m <= 15 * 60 + 35
}

function fmtVol(shares: number): string {
  // 腾讯给的是股; 手 = 股/100
  const hands = shares / 100
  if (hands >= 1e4) return `${(hands / 1e4).toFixed(2)}万手`
  return `${Math.round(hands).toLocaleString('zh-CN')}手`
}

interface Props {
  /** 6 位 A股代码(不带前后缀)。 */
  code: string
  height?: number
  className?: string
}

export function StockMinuteChart({ code, height = 430, className }: Props) {
  const theme = useTheme()
  const boxRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)

  const query = useQuery({
    queryKey: QK.astockMinute(code),
    queryFn: () => request<MinuteResp>(`/api/astock/minute/${encodeURIComponent(code)}`),
    staleTime: 10_000,
    refetchInterval: inTradingWindow() ? 30_000 : false,
    retry: false,
  })

  const data = query.data?.available ? query.data.data : undefined

  const option = useMemo<EChartsOption | null>(() => {
    if (!data || data.points.length === 0) return null
    const ct = chartTheme(getTheme())
    const pts = data.points
    const prev = data.prev_close

    // ── y 轴: 以昨收为中轴上下对称(通达信分时的标准画法) ──
    const dev = Math.max(
      Math.abs(data.high - prev),
      Math.abs(data.low - prev),
      prev * 0.002,   // 一字板/横盘时也要有一点区间, 否则线贴边
    ) * 1.08
    const yMin = prev - dev
    const yMax = prev + dev

    const times = pts.map(p => p.t)
    const priceH = Math.round(height * 0.66)
    const volH = Math.round(height * 0.2)
    const gridTop = 18
    const gridGap = 22

    const volumeBars = pts.map(p => ({
      value: p.volume,
      itemStyle: { color: p.price >= prev ? BULL : BEAR },
    }))

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: [
        { left: 52, right: 54, top: gridTop, height: priceH },
        { left: 52, right: 54, top: gridTop + priceH + gridGap, height: volH },
      ],
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        label: { backgroundColor: ct.crosshairLabelBg },
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: ct.crosshair } },
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (params: any) => {
          const arr = Array.isArray(params) ? params : [params]
          const i = arr[0]?.dataIndex ?? 0
          const p = pts[i]
          if (!p) return ''
          const c = p.pct >= 0 ? BULL : BEAR
          const avgDiff = ((p.avg - prev) / prev) * 100
          return [
            `<b>${p.t}</b>`,
            `价格 <b style="color:${c}">${p.price.toFixed(2)}</b>`,
            `涨跌 <span style="color:${c}">${p.pct >= 0 ? '+' : ''}${p.pct.toFixed(2)}%</span>`,
            `均价 <span style="color:${AVG}">${p.avg.toFixed(2)}</span> (${avgDiff >= 0 ? '+' : ''}${avgDiff.toFixed(2)}%)`,
            `分时量 ${fmtVol(p.volume)} · ${fmtBigNum(p.amount)}`,
          ].join('<br/>')
        },
      },
      xAxis: [
        {
          type: 'category',
          gridIndex: 0,
          data: times,
          boundaryGap: false,
          axisLine: { lineStyle: { color: ct.border } },
          axisTick: { show: false },
          axisLabel: {
            color: ct.text,
            fontSize: 10,
            interval: (_i: number, v: string) => AXIS_TICKS.has(v),
          },
          splitLine: { show: true, lineStyle: { color: ct.grid } },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: times,
          boundaryGap: false,
          axisLine: { lineStyle: { color: ct.border } },
          axisTick: { show: false },
          axisLabel: {
            color: ct.text,
            fontSize: 10,
            interval: (_i: number, v: string) => AXIS_TICKS.has(v),
          },
        },
      ],
      yAxis: [
        {
          gridIndex: 0,
          min: yMin,
          max: yMax,
          splitNumber: 4,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          // 右轴: 与左轴同一区间, 只改标签为涨跌幅%(通达信分时右轴口径)
          gridIndex: 0,
          position: 'right',
          min: yMin,
          max: yMax,
          splitNumber: 4,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: ct.text,
            fontSize: 10,
            formatter: (v: number) => {
              const pct = ((v - prev) / prev) * 100
              return `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
            },
          },
          splitLine: { show: false },
        },
        {
          gridIndex: 1,
          splitNumber: 2,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: ct.text,
            fontSize: 10,
            formatter: (v: number) => (v >= 1e4 ? `${(v / 1e4).toFixed(0)}万` : `${v}`),
          },
          splitLine: { lineStyle: { color: ct.grid } },
        },
      ],
      series: [
        {
          name: '分时',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: pts.map(p => p.price),
          showSymbol: false,
          lineStyle: { width: 1.4, color: data.last_pct >= 0 ? BULL : BEAR },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: data.last_pct >= 0 ? 'rgba(199,64,64,0.22)' : 'rgba(45,155,101,0.22)' },
              { offset: 1, color: 'rgba(0,0,0,0)' },
            ]),
          },
          markLine: {
            silent: true,
            symbol: 'none',
            label: {
              formatter: `昨收 ${prev.toFixed(2)}`,
              color: ct.text,
              fontSize: 10,
              position: 'insideEndTop',
            },
            lineStyle: { color: ct.text, type: 'dashed', width: 1 },
            data: [{ yAxis: prev }],
          },
        },
        {
          name: '均价',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: pts.map(p => p.avg),
          showSymbol: false,
          lineStyle: { width: 1, color: AVG },
        },
        {
          name: '分时量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: volumeBars,
          barWidth: '70%',
        },
      ],
    } as EChartsOption
  }, [data, height])

  // ── 图表实例: 初始化 / 主题&数据变更时重建 / 容器尺寸变化自适应 ──
  useEffect(() => {
    const el = boxRef.current
    if (!el || !option) return
    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    chart.setOption(option, true)

    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [option, theme])

  if (query.isLoading) {
    return (
      <div className={cn('flex items-center justify-center gap-2 text-xs text-muted', className)}
           style={{ height }}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 分时数据加载中…
      </div>
    )
  }

  if (!data) {
    return (
      <div className={cn('flex flex-col items-center justify-center gap-1 px-4 text-center text-xs text-muted', className)}
           style={{ height }}>
        <span>当日分时不可用</span>
        <span className="text-[11px] opacity-70">
          {query.data?.reason ?? '未开盘或非交易日'} —— 不画空图，也不拿上一交易日冒充今天
        </span>
      </div>
    )
  }

  const up = data.last_pct >= 0
  const dateStr = data.date && data.date.length === 8
    ? `${data.date.slice(0, 4)}-${data.date.slice(4, 6)}-${data.date.slice(6, 8)}`
    : data.date

  return (
    <div className={cn('w-full', className)}>
      {/* 摘要条: 最新/均价/量额 —— 与九章分时页头一致, 全部来自同一份真数据 */}
      <div className="mb-1 flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs">
        <span className={cn('font-mono text-base font-semibold', up ? 'text-bull' : 'text-bear')}>
          {data.last_price.toFixed(2)}
        </span>
        <span className={cn('font-mono', up ? 'text-bull' : 'text-bear')}>
          {up ? '+' : ''}{data.last_pct.toFixed(2)}%
        </span>
        <span className="text-muted">
          均价 <span className="font-mono text-secondary">{data.points[data.points.length - 1].avg.toFixed(2)}</span>
        </span>
        <span className="text-muted">
          最高 <span className="font-mono text-bull">{data.high.toFixed(2)}</span>
        </span>
        <span className="text-muted">
          最低 <span className="font-mono text-bear">{data.low.toFixed(2)}</span>
        </span>
        <span className="text-muted">
          昨收 <span className="font-mono text-secondary">{data.prev_close.toFixed(2)}</span>
        </span>
        <span className="text-muted">
          量 <span className="font-mono text-secondary">{fmtVol(data.volume)}</span>
        </span>
        <span className="text-muted">
          额 <span className="font-mono text-secondary">{fmtBigNum(data.amount)}</span>
        </span>
        <span className="ml-auto inline-flex items-center gap-2 text-[11px] text-muted">
          {dateStr} · {data.points.length} 个分钟点
          {inTradingWindow() && <span className="text-muted">· 30s 自动刷新</span>}
          <span>· {data.code}</span>
        </span>
      </div>
      <div ref={boxRef} style={{ height: height - 28 }} className="w-full" />
    </div>
  )
}
