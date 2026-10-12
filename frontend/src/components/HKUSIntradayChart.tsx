/**
 * 港美股**当日分时** —— 数据来自 /api/{hk|us}/deep/intraday(通达信扩展市场 MAC-EX
 * 的 1 分钟 K, 由后端按会话合成)。
 *
 * 与 A股 StockMinuteChart 的区别不只是市场: A股那条路是腾讯直接给"当日价格序列",
 * 这一份是 m1 合成的, 因此有三处必须照着数据说:
 *   - **会话可能跨零点**(美股夏令时北京时间 21:30 ~ 次日 04:00), 横轴不能被零点
 *     切断 —— 跨日处在轴上标一根 "次日" 竖线, 否则时间看起来会像倒着走;
 *   - **昨收**来自上一会话(或落盘真日K), 后端在 prev_close_from 里写明出处;
 *   - **成交量已换算成股数**(港股上游按每手 100 股计数), 均价 = 累计额/累计股数。
 *
 * 会话没走完时后端给 session_complete:false + note —— 这里照抄显示, 不补齐、
 * 不外推, 也不把上一会话的曲线说成"今日分时"。
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

/** 与后端 app/data_providers/hk_us_deep_provider.py::intraday 对齐。 */
interface IntradayPoint {
  /** '2026-10-12 13:37:00' —— 北京时间, 会话跨零点时需要它判跨日 */
  dt: string
  /** '13:37' —— 横轴标签用 */
  t: string
  price: number
  avg: number
  pct: number
  /** 股(港股的「手」已在后端 ×100 换成股) */
  volume: number
  amount: number
}

interface IntradayResp {
  available: boolean
  source: string
  reason?: string
  market?: string
  symbol?: string
  session_date?: string
  prev_close?: number
  prev_close_from?: string
  open?: number | null
  high?: number | null
  low?: number | null
  last?: number
  pct?: number
  volume?: number
  amount?: number
  session_complete?: boolean
  covered_minutes?: number
  expected_minutes?: number
  as_of?: string
  bars?: number
  note?: string | null
  points?: IntradayPoint[]
}

// 与页面其余图表同一套涨跌色(港股习惯同样是红涨绿跌)。
const BULL = '#C74040'
const BEAR = '#2D9B65'
const AVG = '#F59E0B'

/** 昨收出处 → 人话。后端给的是机器值, 这里决定让用户看见什么。 */
function prevCloseLabel(from: string | undefined): string {
  if (from === 'prev_session_close') return '上一会话收盘'
  if (from?.startsWith('daily:')) return `日K(${from.slice(6)})`
  return from ?? '未知'
}

interface Props {
  /** 带市场后缀的符号: 00700.HK / AAPL.US */
  symbol: string
  height?: number
  className?: string
}

export function HKUSIntradayChart({ symbol, height = 430, className }: Props) {
  const theme = useTheme()
  const boxRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)

  const market = symbol.toUpperCase().endsWith('.HK') ? 'hk'
    : symbol.toUpperCase().endsWith('.US') ? 'us' : ''

  const query = useQuery({
    queryKey: QK.hkusIntraday(symbol),
    queryFn: () => request<IntradayResp>(`/api/${market}/deep/intraday/${encodeURIComponent(symbol)}`),
    enabled: !!market,
    staleTime: 15_000,
    // 后端短缓存 30s; 会话没走完才轮询(走完了数据不再变, 白打请求)。
    refetchInterval: (q) => (q.state.data?.session_complete === false ? 30_000 : false),
    retry: false,
  })

  const data = query.data?.available ? query.data : undefined
  const points = useMemo(() => data?.points ?? [], [data?.points])

  const option = useMemo<EChartsOption | null>(() => {
    if (!data || points.length === 0 || data.prev_close == null) return null
    const ct = chartTheme(getTheme())
    const prev = data.prev_close
    const last = points[points.length - 1]

    // ── y 轴: 以昨收为中轴上下对称(通达信分时的标准画法) ──
    const dev = Math.max(
      Math.abs((data.high ?? last.price) - prev),
      Math.abs((data.low ?? last.price) - prev),
      prev * 0.002,   // 横盘时也要有一点区间, 否则线贴边
    ) * 1.08

    const times = points.map(p => p.t)
    // 会话跨零点: 上游给的美股时间戳两段共用同一天标签, 点位已按真实成交顺序排好
    // (当晚 → 次日凌晨), 这里按"时间倒退"那一刻定位 —— 23:58 → 00:00 这种跳变。
    const wrap = points.findIndex((p, i) => i > 0 && p.t < points[i - 1].t)
    const midnight = wrap > 0 ? times[wrap] : null
    const labelStep = Math.max(1, Math.round(times.length / 7))

    const priceH = Math.round(height * 0.64)
    const volH = Math.round(height * 0.19)
    const gridTop = 18
    const gridGap = 24

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: [
        { left: 52, right: 58, top: gridTop, height: priceH },
        { left: 52, right: 58, top: gridTop + priceH + gridGap, height: volH },
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
          const p = points[i]
          if (!p) return ''
          const c = p.pct >= 0 ? BULL : BEAR
          const avgDiff = ((p.avg - prev) / prev) * 100
          return [
            `<b>${p.dt.slice(5)}</b> <span style="opacity:.7">北京时间</span>`,
            `价格 <b style="color:${c}">${p.price.toFixed(3)}</b>`,
            `涨跌 <span style="color:${c}">${p.pct >= 0 ? '+' : ''}${p.pct.toFixed(2)}%</span>`,
            `均价 <span style="color:${AVG}">${p.avg.toFixed(3)}</span> (${avgDiff >= 0 ? '+' : ''}${avgDiff.toFixed(2)}%)`,
            `该分钟 ${fmtBigNum(p.volume)} 股 · ${fmtBigNum(p.amount)}`,
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
            color: ct.text, fontSize: 10,
            interval: (i: number) => i % labelStep === 0,
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
            color: ct.text, fontSize: 10,
            interval: (i: number) => i % labelStep === 0,
          },
        },
      ],
      yAxis: [
        {
          gridIndex: 0,
          min: prev - dev,
          max: prev + dev,
          splitNumber: 4,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => v.toFixed(2) },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          // 右轴: 同一区间, 标签改成涨跌幅%(通达信分时右轴口径)
          gridIndex: 0,
          position: 'right',
          min: prev - dev,
          max: prev + dev,
          splitNumber: 4,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: {
            color: ct.text, fontSize: 10,
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
            color: ct.text, fontSize: 10,
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
          data: points.map(p => p.price),
          showSymbol: false,
          lineStyle: { width: 1.4, color: last.pct >= 0 ? BULL : BEAR },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: last.pct >= 0 ? 'rgba(199,64,64,0.22)' : 'rgba(45,155,101,0.22)' },
              { offset: 1, color: 'rgba(0,0,0,0)' },
            ]),
          },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: ct.border, type: 'dashed', width: 1 },
            label: { color: ct.text, fontSize: 10, position: 'insideEndTop' },
            data: [
              { yAxis: prev, label: { formatter: `昨收 ${prev.toFixed(2)}`, color: ct.text } },
              // 美股会话跨零点: 不标一根"次日", 00:00 会被读成往回跳
              ...(midnight ? [{ xAxis: midnight, label: { formatter: '次日' } }] : []),
            ],
          },
        },
        {
          name: '均价',
          type: 'line',
          xAxisIndex: 0,
          yAxisIndex: 0,
          data: points.map(p => p.avg),
          showSymbol: false,
          lineStyle: { width: 1, color: AVG },
        },
        {
          name: '分时量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 2,
          data: points.map(p => ({
            value: p.volume,
            itemStyle: { color: p.price >= prev ? BULL : BEAR },
          })),
          barWidth: '70%',
        },
      ],
    } as EChartsOption
  }, [data, points, height, market])

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

  if (!market) {
    return (
      <div className={cn('flex flex-col items-center justify-center gap-1 px-4 text-center text-xs text-fg-muted', className)}
           style={{ height }}>
        <span>分时只覆盖带市场后缀的代码</span>
        <span className="text-[11px] opacity-70">
          {symbol} 不是 .HK / .US 代码 —— 不猜市场，也不拿别的品种的曲线冒充
        </span>
      </div>
    )
  }

  if (query.isLoading) {
    return (
      <div className={cn('flex items-center justify-center gap-2 text-xs text-fg-muted', className)}
           style={{ height }}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" /> 分时数据加载中…
      </div>
    )
  }

  if (!data || points.length === 0 || data.prev_close == null) {
    return (
      <div className={cn('flex flex-col items-center justify-center gap-1 px-4 text-center text-xs text-fg-muted', className)}
           style={{ height }}>
        <span>当日分时不可用</span>
        <span className="text-[11px] opacity-70">
          {query.data?.reason ?? '上游暂无该标的分钟线'} —— 不画空图，也不拿日K冒充分时
        </span>
      </div>
    )
  }

  const last = points[points.length - 1]
  const up = (data.pct ?? last.pct) >= 0

  return (
    <div className={cn('w-full', className)}>
      {/* 摘要条: 全部来自同一次响应, 不另外拼别的数据 */}
      <div className="mb-1 flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs">
        <span className={cn('font-mono text-base font-semibold', up ? 'text-bull' : 'text-bear')}>
          {last.price.toFixed(3)}
        </span>
        <span className={cn('font-mono', up ? 'text-bull' : 'text-bear')}>
          {up ? '+' : ''}{last.pct.toFixed(2)}%
        </span>
        <span className="text-fg-muted">
          均价 <span className="font-mono text-fg-secondary">{last.avg.toFixed(3)}</span>
        </span>
        <span className="text-fg-muted">
          最高 <span className="font-mono text-bull">{(data.high ?? last.price).toFixed(3)}</span>
        </span>
        <span className="text-fg-muted">
          最低 <span className="font-mono text-bear">{(data.low ?? last.price).toFixed(3)}</span>
        </span>
        <span className="text-fg-muted">
          昨收 <span className="font-mono text-fg-secondary">{data.prev_close.toFixed(3)}</span>
        </span>
        <span className="text-fg-muted">
          量 <span className="font-mono text-fg-secondary">{fmtBigNum(data.volume ?? 0)}股</span>
        </span>
        <span className="text-fg-muted">
          额 <span className="font-mono text-fg-secondary">{fmtBigNum(data.amount ?? 0)}</span>
        </span>
        <span className="ml-auto text-[11px] text-fg-muted">
          {data.session_date} · {points.length} 个分钟点
          {data.session_complete === false && <span className="text-[#F59E0B]"> · 未收盘</span>}
        </span>
      </div>

      {/* 口径说明: 这条曲线是怎么来的、截至什么时候、昨收取自哪里 —— 不写就是让人默认"今天" */}
      <div className="mb-1 px-1 text-[10.5px] leading-relaxed text-fg-muted">
        分时由 1 分钟K 合成（通达信扩展市场 MAC-EX，横轴钟点为北京时间
        {market === 'us' ? '；美股会话跨零点，凌晨那段沿用同一个交易日标签' : ''}），
        昨收取自<span className="text-fg-secondary">{prevCloseLabel(data.prev_close_from)}</span>
        ，成交量已换算成股数。
        {data.session_complete === false && data.note
          ? <span className="text-[#F59E0B]"> {data.note}</span>
          : <span> 数据截至 {data.as_of}（北京时间）</span>}
      </div>

      <div ref={boxRef} style={{ height: height - 48 }} className="w-full" />
    </div>
  )
}
