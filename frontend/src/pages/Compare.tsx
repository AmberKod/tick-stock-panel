/**
 * 多股对比 (/compare) —— 把多只标的的走势归一化后叠在一张图上。
 *
 * 设计取舍:
 *  - **归一化基准 = 区间首日收盘 = 100**。绝对价格没法比(茅台 1400 vs 浦发 9),
 *    只有"涨跌百分比"是同量纲的。也提供"绝对价格"模式, 但那只在价格接近时有用。
 *  - **日期取所有标的的交集**, 不是并集。某只停牌那几天它没有价格, 若用并集
 *    就要插值 —— 插出来的价格是编的。交集少了几天就少几天, 并在页面上写清
 *    实际用了多少根、哪只被剔除了多少天。
 *  - 指标(区间收益/最大回撤/年化波动/相关性)全部基于**对齐后的同一段日期**算,
 *    否则不同标的用的不是同一区间, 数字之间不可比。
 *  - **基准指数**(默认沪深300)走腾讯真源: 本地 index 库实测只有创业板指一只,
 *    沪深300/上证指数都没有, 所以基准不认本地库。基准与个股一样参与**日期交集**
 *    —— 基准缺哪天, 那天整体不算, 而不是给基准插一个点位出来。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import * as echarts from 'echarts'
import { Loader2, Plus, X } from 'lucide-react'

import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'
import { BENCHMARKS, DEFAULT_BENCHMARK, benchmarkLabel } from '@/lib/benchmarks'
import { PageHeader } from '@/components/PageHeader'

const RANGES = [
  { label: '1月', days: 22 },
  { label: '3月', days: 66 },
  { label: '半年', days: 132 },
  { label: '1年', days: 250 },
  { label: '3年', days: 750 },
] as const

/** 系列配色: 8 条够用, 且都避开涨红跌绿(那是涨跌专用色, 用来标标的会混淆)。 */
const SERIES_COLORS = [
  '#3B82F6', '#F97316', '#8B5CF6', '#14B8A6',
  '#EAB308', '#EC4899', '#64748B', '#84CC16',
]

const MAX_SYMBOLS = 8

/** 基准在对齐结构里的"伪标的 key"。它跟个股一起参与日期交集, 但不算在 symList 里。 */
const BENCH_KEY = '__bench__'
const BENCH_COLOR = '#94A3B8'

interface Aligned {
  dates: string[]
  /** symbol → 与 dates 一一对应的收盘价 */
  prices: Record<string, number[]>
  names: Record<string, string>
  /** symbol → 因停牌等原因被剔除的天数 */
  dropped: Record<string, number>
}

/** 把各标的的收盘价对齐到**共同交易日**上。 */
function alignSeries(
  data: Record<string, { date: string; close: number }[]>,
  names: Record<string, string>,
): Aligned {
  const byDate: Record<string, Record<string, number>> = {}
  const counts: Record<string, number> = {}
  for (const [sym, rows] of Object.entries(data)) {
    counts[sym] = rows.length
    for (const r of rows) {
      const d = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
      if (!Number.isFinite(Number(r.close))) continue
      ;(byDate[d] ??= {})[sym] = Number(r.close)
    }
  }
  const syms = Object.keys(data)
  const dates = Object.keys(byDate)
    .filter(d => syms.every(s => byDate[d][s] != null))
    .sort()
  const prices: Record<string, number[]> = {}
  for (const s of syms) prices[s] = dates.map(d => byDate[d][s])
  const dropped: Record<string, number> = {}
  for (const s of syms) dropped[s] = Math.max(0, counts[s] - dates.length)
  return { dates, prices, names, dropped }
}

/** 最大回撤: 区间内从任一高点到之后最低点的最大跌幅(正数表示跌了多少)。 */
function maxDrawdown(series: number[]): number {
  let peak = -Infinity
  let worst = 0
  for (const v of series) {
    if (v > peak) peak = v
    if (peak > 0) worst = Math.max(worst, (peak - v) / peak)
  }
  return worst
}

/** 年化波动率: 日收益标准差 × √250。 */
function annualVol(series: number[]): number {
  if (series.length < 3) return 0
  const rets: number[] = []
  for (let i = 1; i < series.length; i++) {
    if (series[i - 1] > 0) rets.push(Math.log(series[i] / series[i - 1]))
  }
  if (rets.length < 2) return 0
  const mean = rets.reduce((s, v) => s + v, 0) / rets.length
  const varSum = rets.reduce((s, v) => s + (v - mean) ** 2, 0) / (rets.length - 1)
  return Math.sqrt(varSum) * Math.sqrt(250)
}

/** 皮尔逊相关系数(基于对数收益)。日收益比价格更适合算相关性。 */
function correlation(a: number[], b: number[]): number {
  const n = Math.min(a.length, b.length)
  if (n < 4) return NaN
  const ra: number[] = []
  const rb: number[] = []
  for (let i = 1; i < n; i++) {
    if (a[i - 1] > 0 && b[i - 1] > 0) {
      ra.push(Math.log(a[i] / a[i - 1]))
      rb.push(Math.log(b[i] / b[i - 1]))
    }
  }
  if (ra.length < 3) return NaN
  const ma = ra.reduce((s, v) => s + v, 0) / ra.length
  const mb = rb.reduce((s, v) => s + v, 0) / rb.length
  let num = 0
  let da = 0
  let db = 0
  for (let i = 0; i < ra.length; i++) {
    const x = ra[i] - ma
    const y = rb[i] - mb
    num += x * y
    da += x * x
    db += y * y
  }
  if (da <= 0 || db <= 0) return NaN
  return num / Math.sqrt(da * db)
}

/**
 * Beta = cov(个股日收益, 基准日收益) / var(基准日收益)。
 * 这是**确定性统计量**, 不是模型估计 —— 输入就是两段对齐后的收盘价。
 * 基准日收益方差为 0(区间内基准纹丝不动)时无定义, 返回 NaN 由前端显示 '—'。
 */
function beta(a: number[], b: number[]): number {
  const n = Math.min(a.length, b.length)
  if (n < 4) return NaN
  const ra: number[] = []
  const rb: number[] = []
  for (let i = 1; i < n; i++) {
    if (a[i - 1] > 0 && b[i - 1] > 0) {
      ra.push(Math.log(a[i] / a[i - 1]))
      rb.push(Math.log(b[i] / b[i - 1]))
    }
  }
  if (ra.length < 3) return NaN
  const ma = ra.reduce((s, v) => s + v, 0) / ra.length
  const mb = rb.reduce((s, v) => s + v, 0) / rb.length
  let cov = 0
  let vb = 0
  for (let i = 0; i < ra.length; i++) {
    cov += (ra[i] - ma) * (rb[i] - mb)
    vb += (rb[i] - mb) ** 2
  }
  if (vb <= 0) return NaN
  return cov / vb
}

export function Compare() {
  const [params, setParams] = useSearchParams()
  const [days, setDays] = useState<number>(() => {
    const raw = Number(params.get('days'))
    return RANGES.some(r => r.days === raw) ? raw : 250
  })
  const [symbols, setSymbols] = useState<string[]>(() =>
    (params.get('symbols') ?? '').split(',').map(s => s.trim()).filter(Boolean).slice(0, MAX_SYMBOLS),
  )
  const [draft, setDraft] = useState('')
  /** 'norm' = 首日=100; 'abs' = 绝对价格; 'rel' = 相对基准的超额收益(百分点) */
  const [mode, setMode] = useState<'norm' | 'abs' | 'rel'>('norm')
  /** 基准指数代码, 空串 = 不要基准 */
  const [bench, setBench] = useState<string>(
    () => (params.get('bench') ?? DEFAULT_BENCHMARK),
  )

  // URL 与 state 双向同步 —— 对比结果要能直接把链接发给别人
  const syncUrl = useCallback((nextSymbols: string[], nextDays: number, nextBench: string) => {
    const p = new URLSearchParams()
    if (nextSymbols.length > 0) p.set('symbols', nextSymbols.join(','))
    p.set('days', String(nextDays))
    if (nextBench) p.set('bench', nextBench)
    setParams(p, { replace: true })
  }, [setParams])

  useEffect(() => {
    syncUrl(symbols, days, bench)
  }, [symbols, days, bench, syncUrl])

  const addSymbol = useCallback(() => {
    const raw = draft.trim()
    if (!raw) return
    // 允许直接粘 "600519" / "600519.SH" / "sh600519"; 统一补成 A股 ts 后缀
    const m = raw.match(/(\d{6})/)
    const code = m ? m[1] : raw
    const symbol = /^\d{6}$/.test(code) ? `${code}.SH` : code
    setSymbols(prev => (prev.includes(symbol) || prev.length >= MAX_SYMBOLS ? prev : [...prev, symbol]))
    setDraft('')
  }, [draft])

  const removeSymbol = useCallback((s: string) => {
    setSymbols(prev => prev.filter(x => x !== s))
  }, [])

  const query = useQuery({
    queryKey: QK.klineCompare(symbols, days),
    queryFn: () => api.klineCompare(symbols, days),
    enabled: symbols.length > 0,
    placeholderData: prev => prev,
  })

  /** 基准指数(腾讯真源)。取不到就 available:false —— 页面不画基准线, 不拿别的指数顶替。 */
  const benchQuery = useQuery({
    queryKey: QK.astockIndexDaily(bench || 'none', Math.min(1000, Math.max(320, days + 40))),
    queryFn: () => api.astockIndexDaily(bench, Math.min(1000, Math.max(320, days + 40))),
    enabled: !!bench && symbols.length > 0,
    staleTime: 600_000,
    placeholderData: prev => prev,
  })
  // ⚠️ 必须再判一次 bench: 这个 query 带了 placeholderData(换区间时不想闪空),
  // 而切到"无基准"只是把 enabled 关掉、key 变掉 —— 上一份数据会作为 placeholder
  // 继续留着。少了这个判断, 点了"无"基准线还挂在图上。
  const benchData = bench && benchQuery.data?.available ? benchQuery.data.data : null
  const benchName = benchData?.name || benchmarkLabel(bench)

  const aligned = useMemo(() => {
    const data = query.data?.data
    if (!data || Object.keys(data).length === 0) return null
    const norm: Record<string, { date: string; close: number }[]> = {}
    for (const [sym, rows] of Object.entries(data)) {
      norm[sym] = (rows ?? []).map(r => ({
        date: String(r.date).slice(0, 10),
        close: Number(r.close),
      }))
    }
    const names = { ...(query.data?.names ?? {}) }
    // 基准当一只"标的"一起对齐: 它缺哪天, 那天整体不算(不插点位)
    if (benchData?.rows?.length) {
      norm[BENCH_KEY] = benchData.rows.map(r => ({
        date: String(r.date).slice(0, 10),
        close: Number(r.close),
      }))
      names[BENCH_KEY] = benchName
    }
    return alignSeries(norm, names)
  }, [query.data, benchData, benchName])

  const symList = useMemo(() => symbols.filter(s => aligned?.prices[s]?.length), [symbols, aligned])

  // ───────── 图表 ─────────
  const boxRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  /** 基准序列(已与其它标的对齐到同一批日期) */
  const benchRaw = useMemo(
    () => (aligned?.prices[BENCH_KEY]?.length ? aligned.prices[BENCH_KEY] : null),
    [aligned],
  )
  const benchNorm = useMemo(
    () => (benchRaw && benchRaw[0] > 0 ? benchRaw.map(v => (v / benchRaw[0]) * 100) : null),
    [benchRaw],
  )

  const seriesData = useMemo(() => {
    if (!aligned) return null
    return symList.map((sym, i) => {
      const raw = aligned.prices[sym]
      const base = raw[0]
      const norm = raw.map(v => (base > 0 ? (v / base) * 100 : 100))
      // rel: 各标的归一化值 - 基准归一化值 = 相对基准的超额(百分点)。
      // 没有基准时 rel 无意义, 退回归一化(按钮此时也置灰)。
      const vals = mode === 'abs' ? raw
        : mode === 'rel' && benchNorm ? norm.map((v, j) => v - benchNorm[j])
        : norm
      return {
        symbol: sym,
        name: aligned.names[sym] ?? sym,
        color: SERIES_COLORS[i % SERIES_COLORS.length],
        values: vals,
        raw,
        norm,
      }
    })
  }, [aligned, symList, mode, benchNorm])

  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const chart = chartRef.current ?? echarts.init(el)
    chartRef.current = chart
    if (!seriesData || !aligned) {
      chart.clear()
      return
    }
    chart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      grid: { left: 56, right: 20, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(15,23,42,0.94)',
        borderWidth: 0,
        textStyle: { color: '#E5E7EB', fontSize: 11 },
        axisPointer: { type: 'line', lineStyle: { color: 'rgba(148,163,184,0.5)' } },
      },
      legend: {
        top: 0,
        right: 8,
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { fontSize: 11, color: '#94A3B8' },
      },
      xAxis: {
        type: 'category',
        data: aligned.dates,
        boundaryGap: false,
        axisLine: { lineStyle: { color: 'rgba(148,163,184,0.35)' } },
        axisLabel: { color: '#94A3B8', fontSize: 10 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: true,
        splitLine: { lineStyle: { color: 'rgba(148,163,184,0.14)' } },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: '#94A3B8',
          fontSize: 10,
          formatter: (v: number) => (mode === 'abs' ? v.toFixed(2) : v.toFixed(0)),
        },
      },
      series: [
        ...seriesData.map(s => ({
          name: `${s.name} ${s.symbol}`,
          type: 'line',
          data: s.values,
          smooth: false,
          symbol: 'none',
          lineStyle: { width: 1.6, color: s.color },
          itemStyle: { color: s.color },
        })),
        // 基准: 归一化模式下与个股同坐标系(首日也是 100); 相对收益模式下是恒为 0
        // 的那条线; 绝对价格模式下量纲不同, 不画(画了会压扁个股曲线)。
        ...((mode === 'norm' && benchNorm) || (mode === 'rel' && benchNorm)
          ? [{
              name: `基准 ${benchName}`,
              type: 'line',
              data: mode === 'rel' ? benchNorm.map(() => 0) : benchNorm,
              smooth: false,
              symbol: 'none',
              lineStyle: { width: 1.4, color: BENCH_COLOR, type: 'dashed' as const },
              itemStyle: { color: BENCH_COLOR },
              // 归一化模式下首日全是 100 —— 把这条基准线画出来,
              // 否则"所有线都从同一点出发"会被误读成某种共同起点规律。
              markLine: {
                silent: true, symbol: 'none', animation: false,
                data: [{ yAxis: mode === 'rel' ? 0 : 100 }],
                lineStyle: { color: 'rgba(148,163,184,0.55)', type: 'dashed' as const, width: 1 },
                label: {
                  show: true,
                  formatter: mode === 'rel' ? '持平基准' : '基准 100',
                  position: 'insideStartTop',
                  color: '#94A3B8', fontSize: 10,
                },
              },
            }]
          : []),
      ],
    }, true)
    chart.resize()
  }, [seriesData, aligned, mode, symList.length, benchNorm, benchName])

  useEffect(() => {
    const el = boxRef.current
    if (!el) return
    const ro = new ResizeObserver(() => chartRef.current?.resize())
    ro.observe(el)
    return () => {
      ro.disconnect()
      chartRef.current?.dispose()
      chartRef.current = null
    }
  }, [])

  const missing = useMemo(() => {
    if (!query.data) return []
    const got = new Set(Object.keys(query.data.data ?? {}))
    return symbols.filter(s => !got.has(s))
  }, [query.data, symbols])

  const pct = (v: number) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}%`

  return (
    <div className="mx-auto flex w-full max-w-[1800px] flex-col gap-3 px-3 py-3">
      <PageHeader
        title="多股对比"
        subtitle={`${symList.length} / ${MAX_SYMBOLS} 只 · 归一化基准 = 区间首日收盘 = 100`}
      />

      {/* ── 标的输入 ── */}
      <div className="rounded-md border border-border bg-surface p-2">
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') addSymbol() }}
            placeholder="输入代码添加，如 600519 / 000651 / 688825"
            className="h-7 w-56 rounded border border-border bg-elevated px-2 text-[12px] text-foreground outline-none placeholder:text-muted focus:border-sky-400/60"
          />
          <button
            type="button"
            onClick={addSymbol}
            className="inline-flex h-7 items-center gap-1 rounded border border-border px-2 text-[12px] text-secondary hover:text-foreground"
          >
            <Plus className="h-3 w-3" /> 添加
          </button>
          <span className="mx-1 h-4 w-px bg-border" />
          <span className="text-[11px] text-muted">区间</span>
          <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
            {RANGES.map(r => (
              <button
                key={r.days}
                type="button"
                onClick={() => setDays(r.days)}
                className={cn('rounded px-2 py-0.5 text-[11px] transition-colors',
                  days === r.days ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary')}
              >
                {r.label}
              </button>
            ))}
          </div>
          <span className="mx-1 h-4 w-px bg-border" />
          <span className="text-[11px] text-muted">纵轴</span>
          <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
            {([['norm', '归一化'], ['rel', '相对基准'], ['abs', '绝对价格']] as const).map(([v, label]) => {
              // 没有基准时"相对基准"无从谈起 —— 置灰而不是画一条错的 0 线
              const disabled = v === 'rel' && !benchNorm
              return (
                <button
                  key={v}
                  type="button"
                  disabled={disabled}
                  onClick={() => setMode(v)}
                  title={disabled ? '需要先拿到基准指数才谈得上相对收益' : undefined}
                  className={cn('rounded px-2 py-0.5 text-[11px] transition-colors',
                    disabled && 'cursor-not-allowed opacity-40',
                    mode === v ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary')}
                >
                  {label}
                </button>
              )
            })}
          </div>
          {query.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
        </div>

        {/* ── 基准指数 ──
            默认沪深300。全部走腾讯真源(本地 index 库只有创业板指一只);
            取不到就明说, 不拿别的指数顶替, 也不画一条"看起来像基准"的线。 */}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-muted">基准</span>
          {BENCHMARKS.map(b => (
            <button
              key={b.code}
              type="button"
              onClick={() => setBench(b.code)}
              title={b.hint}
              className={cn('rounded border px-1.5 py-0.5 text-[11px] transition-colors',
                bench === b.code
                  ? 'border-slate-400/50 bg-slate-400/10 text-foreground'
                  : 'border-border text-muted hover:text-secondary')}
            >
              {b.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => { setBench(''); if (mode === 'rel') setMode('norm') }}
            title="不加基准: 只看这几只之间的相对走势"
            className={cn('rounded border px-1.5 py-0.5 text-[11px] transition-colors',
              !bench ? 'border-border bg-elevated text-foreground' : 'border-border text-muted hover:text-secondary')}
          >
            无
          </button>
          {bench && benchQuery.isFetching && <Loader2 className="h-3 w-3 animate-spin text-muted" />}
          {bench && benchQuery.data && !benchQuery.data.available && (
            <span className="text-[11px] text-amber-500">
              基准取不到（{benchQuery.data.reason ?? '未知原因'}）—— 不画基准线
            </span>
          )}
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {symbols.length === 0 && (
            <span className="text-[11px] text-muted">
              还没有标的 —— 上面输入框加几只。链接里的 ?symbols= 会记住这次对比。
            </span>
          )}
          {symbols.map((s, i) => (
            <span
              key={s}
              className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 text-[11px]"
            >
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }}
              />
              <span className="text-secondary">{aligned?.names?.[s] ?? s}</span>
              <span className="font-mono text-muted">{s}</span>
              <button
                type="button"
                onClick={() => removeSymbol(s)}
                className="text-muted hover:text-foreground"
                title="移除"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      </div>

      {/* ── 走势图 ── */}
      <div className="rounded-md border border-border bg-surface p-2">
        <div ref={boxRef} className="w-full" style={{ height: 420 }} />
        {aligned && (
          <div className="px-1 pt-1 text-[11px] text-muted">
            对齐后 {aligned.dates.length} 个共同交易日（{aligned.dates[0]} ~ {aligned.dates[aligned.dates.length - 1]}）
            {symList.some(s => (aligned.dropped[s] ?? 0) > 0) && (
              <> · 被剔除的天数:
                {symList.filter(s => (aligned.dropped[s] ?? 0) > 0).map(s => (
                  <span key={s} className="ml-1 font-mono">
                    {aligned.names[s] ?? s} {aligned.dropped[s]}天
                  </span>
                ))}
                （该标的在这些日期没有收盘价，不插值）
              </>
            )}
          </div>
        )}
        {/* 基准的来源与口径: 图上那条灰虚线到底是哪来的, 必须写在同一屏 */}
        {bench && benchData && (
          <div className="px-1 pt-1 text-[11px] text-muted">
            基准 <span className="text-secondary">{benchName}</span>
            <span className="font-mono"> {benchData.code}</span>
            {' '}· 腾讯实时源 <span className="font-mono">live-tencent</span>
            {' '}· 覆盖 {benchData.coverage?.from} ~ {benchData.coverage?.to}（{benchData.coverage?.bars}根）
            {mode === 'rel'
              ? ' · 纵轴 = 各标的归一化值 − 基准归一化值，即**相对基准的超额收益（百分点）**，0 = 与基准持平'
              : mode === 'abs'
                ? ' · 绝对价格模式下量纲不同，基准线不画（画了会把个股曲线压平）'
                : ' · 基准与个股一样以区间首日 = 100 归一化'}
          </div>
        )}
        {missing.length > 0 && (
          <div className="px-1 pt-1 text-[11px] text-amber-500">
            库里没有这些标的的日K，已从图中排除：
            <span className="font-mono"> {missing.join(' / ')}</span>
          </div>
        )}
        {symbols.length > 0 && symList.length === 0 && !query.isFetching && (
          <div className="flex h-[420px] items-center justify-center text-[12px] text-muted">
            这些代码在本地日K库里没有数据 —— 不拿别的品种的曲线冒充
          </div>
        )}
      </div>

      {/* ── 指标表 ── */}
      {seriesData && seriesData.length > 0 && (
        <div className="overflow-x-auto rounded-md border border-border bg-surface">
          <table className="w-full min-w-[640px] text-[12px]">
            <thead>
              <tr className="border-b border-border text-left text-[11px] text-muted">
                <th className="px-3 py-1.5 font-normal">标的</th>
                <th className="px-3 py-1.5 text-right font-normal">区间收益</th>
                <th className="px-3 py-1.5 text-right font-normal">最大回撤</th>
                <th className="px-3 py-1.5 text-right font-normal">年化波动</th>
                {benchRaw && (
                  <>
                    <th className="px-3 py-1.5 text-right font-normal" title="个股区间收益 − 基准区间收益。>0 = 跑赢基准">超额收益</th>
                    <th className="px-3 py-1.5 text-right font-normal" title="Beta = cov(个股日收益, 基准日收益) / var(基准日收益)。>1 = 波动比基准大">β</th>
                    <th className="px-3 py-1.5 text-right font-normal" title="与基准的日对数收益相关系数">与基准相关</th>
                  </>
                )}
                <th className="px-3 py-1.5 text-right font-normal">最新</th>
              </tr>
            </thead>
            <tbody>
              {seriesData.map(s => {
                const last = s.raw[s.raw.length - 1]
                const first = s.raw[0]
                const ret = first > 0 ? last / first - 1 : 0
                const bRet = benchRaw && benchRaw[0] > 0
                  ? benchRaw[benchRaw.length - 1] / benchRaw[0] - 1
                  : null
                const excess = bRet == null ? null : ret - bRet
                const b = benchRaw ? beta(s.raw, benchRaw) : NaN
                const c = benchRaw ? correlation(s.raw, benchRaw) : NaN
                return (
                  <tr key={s.symbol} className="border-b border-border/60 last:border-0">
                    <td className="px-3 py-1.5">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />
                        <span className="text-foreground">{s.name}</span>
                        <span className="font-mono text-[11px] text-muted">{s.symbol}</span>
                      </span>
                    </td>
                    <td className={cn('px-3 py-1.5 text-right font-mono',
                      ret >= 0 ? 'text-[#C74040]' : 'text-[#2D9B65]')}>
                      {pct(ret)}
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-secondary">
                      -{(maxDrawdown(s.raw) * 100).toFixed(2)}%
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono text-secondary">
                      {(annualVol(s.raw) * 100).toFixed(1)}%
                    </td>
                    {benchRaw && (
                      <>
                        <td className={cn('px-3 py-1.5 text-right font-mono',
                          excess == null ? 'text-muted'
                            : excess >= 0 ? 'text-[#C74040]' : 'text-[#2D9B65]')}>
                          {excess == null ? '—' : `${excess >= 0 ? '+' : ''}${(excess * 100).toFixed(2)}pp`}
                        </td>
                        <td className="px-3 py-1.5 text-right font-mono text-secondary">
                          {Number.isFinite(b) ? b.toFixed(2) : '—'}
                        </td>
                        <td className="px-3 py-1.5 text-right font-mono text-secondary">
                          {Number.isFinite(c) ? c.toFixed(2) : '—'}
                        </td>
                      </>
                    )}
                    <td className="px-3 py-1.5 text-right font-mono text-secondary">
                      {last.toFixed(2)}
                    </td>
                  </tr>
                )
              })}
              {/* 基准自己也是一行 —— 不然"跑赢多少"没有参照 */}
              {benchRaw && (
                <tr className="border-t border-border bg-elevated/40">
                  <td className="px-3 py-1.5">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full" style={{ background: BENCH_COLOR }} />
                      <span className="text-secondary">基准 {benchName}</span>
                      <span className="font-mono text-[11px] text-muted">{benchData?.code ?? bench}</span>
                    </span>
                  </td>
                  <td className={cn('px-3 py-1.5 text-right font-mono',
                    (benchRaw[benchRaw.length - 1] / benchRaw[0] - 1) >= 0
                      ? 'text-[#C74040]' : 'text-[#2D9B65]')}>
                    {pct(benchRaw[benchRaw.length - 1] / benchRaw[0] - 1)}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-secondary">
                    -{(maxDrawdown(benchRaw) * 100).toFixed(2)}%
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-secondary">
                    {(annualVol(benchRaw) * 100).toFixed(1)}%
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono text-muted">—</td>
                  <td className="px-3 py-1.5 text-right font-mono text-muted">1.00</td>
                  <td className="px-3 py-1.5 text-right font-mono text-muted">1.00</td>
                  <td className="px-3 py-1.5 text-right font-mono text-secondary">
                    {benchRaw[benchRaw.length - 1].toFixed(2)}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {benchRaw && (
            <div className="border-t border-border px-3 py-1.5 text-[10px] leading-relaxed text-muted">
              超额收益 = 个股区间收益 − 基准区间收益（pp = 百分点，不是相对百分比）。
              β 与相关系数都基于对齐区间内的**日对数收益**算，是确定性统计量，不是模型估计；
              基准自身的超额/β 那两格恒为 0 / 1，写出来是为了让表格对齐可读。
            </div>
          )}
        </div>
      )}

      {/* ── 相关性矩阵(基于日对数收益) ── */}
      {seriesData && seriesData.length >= 2 && (
        <div className="overflow-x-auto rounded-md border border-border bg-surface p-2">
          <div className="mb-1 text-[12px] text-foreground">相关系数</div>
          <div className="mb-1 text-[10px] leading-relaxed text-muted">
            基于对齐区间内的**日对数收益**算（不是价格本身 —— 两只都涨的票价格序列相关性会虚高）。
            1 = 完全同涨同跌，-1 = 完全反向，0 = 无关。
          </div>
          <table className="w-full min-w-[420px] text-[12px]">
            <thead>
              <tr className="text-[11px] text-muted">
                <th className="px-2 py-1 font-normal" />
                {seriesData.map(s => (
                  <th key={s.symbol} className="px-2 py-1 text-center font-normal">
                    <span className="inline-flex items-center gap-1">
                      <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />
                      {s.name}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {seriesData.map(a => (
                <tr key={a.symbol}>
                  <td className="px-2 py-1">
                    <span className="inline-flex items-center gap-1">
                      <span className="h-2 w-2 rounded-full" style={{ background: a.color }} />
                      {a.name}
                    </span>
                  </td>
                  {seriesData.map(b => {
                    const c = a.symbol === b.symbol ? 1 : correlation(a.raw, b.raw)
                    return (
                      <td
                        key={b.symbol}
                        className="px-2 py-1 text-center font-mono text-secondary"
                        title={`${a.name} vs ${b.name}`}
                      >
                        {Number.isFinite(c) ? c.toFixed(2) : '—'}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
