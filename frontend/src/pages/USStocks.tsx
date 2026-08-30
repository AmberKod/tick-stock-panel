import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { USMarketWidget } from '@/components/USMarketWidget'

interface USStock {
  symbol: string
  name: string
  code: string
  exchange: string
  market: string
}

interface USListResponse {
  results: USStock[]
  count: number
  currency: string
  currency_label: string
  realtime_delay_min: number
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export function USStocksPage() {
  const stocks = useQuery({
    queryKey: ['us', 'stocks'],
    queryFn: () => fetchJson<USListResponse>('/api/us/stocks'),
    staleTime: 60_000,
  })
  const [search, setSearch] = useState('')
  const filtered =
    stocks.data?.results.filter(
      (s) =>
        s.symbol.includes(search.toUpperCase()) ||
        s.name.toLowerCase().includes(search.toLowerCase()),
    ) ?? []

  return (
    <div className="p-6 space-y-6">
      {/* 顶部迷你实时看板 (3 指数 + 15 龙头) */}
      <USMarketWidget />

      <div>
        <h1 className="text-2xl font-semibold text-fg">美股</h1>
        <p className="text-sm text-fg-muted mt-1">
          M2 内置 15 热门龙头 · 货币 USD 原币 · 免费行情延迟 15 分钟 · 美股无涨跌停
        </p>
      </div>

      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-fg">美股热门池</h2>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索代码 / 名称..."
            className="px-3 py-1.5 rounded border border-border bg-surface text-fg text-sm w-64"
          />
        </div>

        {stocks.isLoading && <div className="text-fg-muted text-sm py-4">加载中...</div>}
        {stocks.error && (
          <div className="text-rose-500 text-sm py-4">
            加载失败：{String((stocks.error as Error).message)}
          </div>
        )}
        {stocks.data && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-fg-muted border-b border-border">
                <tr>
                  <th className="text-left py-2 px-3">代码</th>
                  <th className="text-left py-2 px-3">名称</th>
                  <th className="text-left py-2 px-3">市场</th>
                  <th className="text-right py-2 px-3">货币</th>
                  <th className="text-left py-2 px-3">备注</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((s: USStock) => (
                  <tr
                    key={s.symbol}
                    className="border-b border-border/50 hover:bg-elevated/50"
                  >
                    <td className="py-2 px-3 font-mono text-fg">
                      <Link
                        to={`/us/${s.symbol}`}
                        className="hover:underline text-blue-500"
                      >
                        {s.symbol}
                      </Link>
                    </td>
                    <td className="py-2 px-3 text-fg">
                      <Link to={`/us/${s.symbol}`} className="hover:underline">
                        {s.name}
                      </Link>
                    </td>
                    <td className="py-2 px-3">
                      <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-blue-500/10 text-blue-500">
                        US
                      </span>
                    </td>
                    <td className="py-2 px-3 text-right text-fg">$</td>
                    <td className="py-2 px-3 text-fg-muted text-xs">
                      实时行情延迟 15min (yfinance)
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="text-xs text-fg-muted mt-3">
              共 {stocks.data.count} 只 (M2 内置) · 筛选后 {filtered.length} 只
            </div>
          </div>
        )}
      </div>

      <div className="text-xs text-fg-muted space-y-1 p-3 rounded border border-border bg-surface/40">
        <div>M2 已落地：</div>
        <ul className="list-disc pl-5 space-y-0.5">
          <li>US_PROFILE（DST 时钟、390min 档期、USD、T+0、无涨跌停）</li>
          <li>yfinance provider（日K 5年 + 实时 15min 延迟）</li>
          <li>15 个美股热门池（标普头部 + 科技七姐妹 + 中概）</li>
        </ul>
        <div className="mt-3">限制：</div>
        <ul className="list-disc pl-5 space-y-0.5 text-amber-500">
          <li>免费实时延迟 15min（付费聚合源才实时）</li>
          <li>无分钟/无 tick（免费源不支持）</li>
          <li>全市场扫描不可用（yfinance 无全列表）</li>
        </ul>
      </div>
    </div>
  )
}