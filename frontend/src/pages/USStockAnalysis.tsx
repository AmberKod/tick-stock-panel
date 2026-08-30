import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { useState } from 'react'

interface USRealtime {
  symbol: string
  name?: string
  code?: string
  price: number | null
  pre_close: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  change_pct: number | null
  source: string
  market?: string
}

interface USDailyRow {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

interface USDailyResponse {
  symbol: string
  name: string
  rows: USDailyRow[]
  source: string
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export function USStockAnalysisPage() {
  const { symbol: rawSymbol = 'AAPL.US' } = useParams<{ symbol: string }>()
  const symbol = rawSymbol.toUpperCase()
  const [days, setDays] = useState(120)

  const realtime = useQuery({
    queryKey: ['us', 'realtime', symbol],
    queryFn: () => fetchJson<USRealtime>(`/api/us/realtime/${encodeURIComponent(symbol)}`),
    refetchInterval: 60_000,
  })

  const daily = useQuery({
    queryKey: ['us', 'daily', symbol, days],
    queryFn: () => fetchJson<USDailyResponse>(`/api/us/daily/${encodeURIComponent(symbol)}?days=${days}`),
    staleTime: 60_000,
  })

  const r = realtime.data
  const changeColor =
    r?.change_pct == null
      ? 'text-fg-muted'
      : r.change_pct >= 0
        ? 'text-emerald-500'
        : 'text-rose-500'

  // 美股惯例: 涨绿跌红 (与中国相反, 但本地视感不变 — 用绿色)
  // 注意: 港股用红涨绿跌, 美股用绿涨红跌; 代码用 绿/红 反向

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-baseline gap-3">
        <Link to="/us" className="text-sm text-fg-muted hover:text-fg">← 美股热门池</Link>
        <span className="text-fg-muted">/</span>
        <h1 className="text-2xl font-semibold text-fg">
          {r?.name || symbol} <span className="text-sm text-fg-muted font-mono">{symbol}</span>
        </h1>
        <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-blue-500/10 text-blue-500">
          US · USD
        </span>
        {r && (
          <span className="text-xs text-fg-muted">
            数据源: {r.source} {r.source === 'yfinance' ? '(延迟 15min)' : ''}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="现价">
          {r?.price != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              ${r.price.toFixed(2)}
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="涨跌幅">
          {r?.change_pct != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              {r.change_pct >= 0 ? '+' : ''}
              {r.change_pct.toFixed(2)}%
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="今开">{r?.open != null ? `$${r.open.toFixed(2)}` : '--'}</Card>
        <Card label="昨收">{r?.pre_close != null ? `$${r.pre_close.toFixed(2)}` : '--'}</Card>
        <Card label="最高">{r?.high != null ? `$${r.high.toFixed(2)}` : '--'}</Card>
        <Card label="最低">{r?.low != null ? `$${r.low.toFixed(2)}` : '--'}</Card>
        <Card label="成交量">
          {r?.volume != null ? formatVolume(r.volume) : '--'}
        </Card>
        <Card label="成交额">
          {r?.amount != null ? formatAmount(r.amount) : '--'}
        </Card>
      </div>

      <div className="text-xs text-fg-muted p-3 rounded border border-border bg-surface/40">
        美股惯例: 绿涨红跌 · T+0 交收 · 无涨跌停 · 数据源 yfinance (免费档延迟 15min) · 装 <code>yfinance</code> 后可看 5 年日 K
      </div>

      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-fg">日 K（近 {days} 日）</h2>
          <div className="flex gap-2 text-xs">
            {[60, 120, 250, 500].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-2 py-1 rounded ${days === d ? 'bg-blue-500 text-white' : 'bg-elevated text-fg-muted'}`}
              >
                {d}日
              </button>
            ))}
          </div>
        </div>
        {daily.isLoading && <div className="text-fg-muted text-sm py-4">加载中...</div>}
        {daily.error && <div className="text-rose-500 text-sm py-4">加载失败: {String((daily.error as Error).message)}</div>}
        {daily.data && daily.data.rows.length === 0 && (
          <div className="text-fg-muted text-sm py-4">
            日 K 暂不可用 (yfinance 未装 或 网络失败)。装 yfinance 后刷新即可。
          </div>
        )}
        {daily.data && daily.data.rows.length > 0 && <DailyTable rows={daily.data.rows} />}
      </div>
    </div>
  )
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface/60 p-3">
      <div className="text-xs text-fg-muted mb-1">{label}</div>
      <div className="text-lg font-semibold text-fg">{children}</div>
    </div>
  )
}

function DailyTable({ rows }: { rows: USDailyRow[] }) {
  const reversed = [...rows].reverse()
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-fg-muted border-b border-border">
          <tr>
            <th className="text-left py-2 px-3">日期</th>
            <th className="text-right py-2 px-3">开</th>
            <th className="text-right py-2 px-3">高</th>
            <th className="text-right py-2 px-3">低</th>
            <th className="text-right py-2 px-3">收</th>
            <th className="text-right py-2 px-3">量</th>
          </tr>
        </thead>
        <tbody>
          {reversed.slice(0, 60).map((r) => (
            <tr key={r.date} className="border-b border-border/50">
              <td className="py-1.5 px-3 text-fg-muted font-mono">{r.date}</td>
              <td className="py-1.5 px-3 text-right text-fg">{r.open.toFixed(2)}</td>
              <td className="py-1.5 px-3 text-right text-emerald-500">{r.high.toFixed(2)}</td>
              <td className="py-1.5 px-3 text-right text-rose-500">{r.low.toFixed(2)}</td>
              <td className="py-1.5 px-3 text-right text-fg font-medium">{r.close.toFixed(2)}</td>
              <td className="py-1.5 px-3 text-right text-fg-muted">{formatVolume(r.volume)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="text-xs text-fg-muted mt-2">共 {rows.length} 条, 仅显示最近 60 条</div>
    </div>
  )
}

function formatVolume(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(2)}K`
  return v.toFixed(0)
}

function formatAmount(a: number): string {
  if (a >= 1e9) return `$${(a / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `$${(a / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `$${(a / 1e3).toFixed(2)}K`
  return `$${a.toFixed(0)}`
}