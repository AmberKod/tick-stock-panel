import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { HKMarketWidget } from '@/components/HKMarketWidget'

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

/** 港股最小可用入口 (M1)。
 *
 *  - 内置 10 龙头池, 走 GET /api/hk/stocks
 *  - 走真实后端 API, 不是占位页
 *  - 货币 HKD 原币 (M1 用户已拍板)
 *  - 涨跌停/打板相关功能对港股无意义, 留空 (后续 H4 完整门控)
 */

interface HKStock {
  symbol: string
  name: string
  code: string
  exchange: string
  market: string
}

interface HKIndex {
  symbol: string
  name: string
}

interface HKListResponse {
  results: HKStock[]
  count: number
  currency: string
  settlement: string
}

interface HKIndicesResponse {
  results: HKIndex[]
  currency: string
}

export function HKStocksPage() {
  const stocks = useQuery({
    queryKey: ['hk', 'stocks'],
    queryFn: () => fetchJson<HKListResponse>('/api/hk/stocks'),
    staleTime: 60_000,
  })
  const indices = useQuery({
    queryKey: ['hk', 'indices'],
    queryFn: () => fetchJson<HKIndicesResponse>('/api/hk/indices'),
    staleTime: 60_000,
  })

  const [search, setSearch] = useState('')
  const filtered =
    stocks.data?.results.filter(
      (s) =>
        s.symbol.includes(search.toUpperCase()) ||
        s.name.includes(search) ||
        s.code.includes(search),
    ) ?? []

  return (
    <div className="p-6 space-y-6">
      {/* H8: 顶部迷你实时看板 (3 指数 + 10 龙头涨幅榜) */}
      <HKMarketWidget />

      <div>
        <h1 className="text-2xl font-semibold text-fg">港股</h1>
        <p className="text-sm text-fg-muted mt-1">
          M1 内置 10 个港股龙头 · 货币 HKD 原币 · 港股无涨跌停
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {(indices.data?.results ?? []).map((idx: HKIndex) => (
          <div
            key={idx.symbol}
            className="rounded-lg border border-border bg-surface/60 p-4"
          >
            <div className="text-sm font-medium text-fg-muted">港股指数</div>
            <div className="text-lg font-semibold text-fg mt-1">{idx.name}</div>
            <div className="text-xs text-fg-muted mt-1 font-mono">{idx.symbol}</div>
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-fg">港股龙头池</h2>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索代码 / 名称..."
            className="px-3 py-1.5 rounded border border-border bg-surface text-fg text-sm w-64"
          />
        </div>

        {stocks.isLoading && (
          <div className="text-fg-muted text-sm py-4">加载中...</div>
        )}
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
                {filtered.map((s: HKStock) => (
                  <tr
                    key={s.symbol}
                    className="border-b border-border/50 hover:bg-elevated/50"
                  >
                    <td className="py-2 px-3 font-mono text-fg">
                      <Link
                        to={`/hk/${s.symbol}`}
                        className="hover:underline text-rose-500"
                      >
                        {s.symbol}
                      </Link>
                    </td>
                    <td className="py-2 px-3 text-fg">
                      <Link to={`/hk/${s.symbol}`} className="hover:underline">
                        {s.name}
                      </Link>
                    </td>
                    <td className="py-2 px-3">
                      <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-rose-500/10 text-rose-500">
                        HK
                      </span>
                    </td>
                    <td className="py-2 px-3 text-right text-fg">HKD</td>
                    <td className="py-2 px-3 text-fg-muted text-xs">
                      实时行情 / 日 K 已接入 (H5/H6)
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="text-xs text-fg-muted mt-3">
              共 {stocks.data.count} 只 (M1 内置) · 筛选后 {filtered.length} 只
            </div>
          </div>
        )}
      </div>

      <div className="text-xs text-fg-muted space-y-1 p-3 rounded border border-border bg-surface/40">
        <div>M1 已落地：</div>
        <ul className="list-disc pl-5 space-y-0.5">
          <li>HK_PROFILE（330 min 档期、HKD、T+0、无涨跌停）</li>
          <li>港股 10 龙头静态池 + akshare 可选全市场池</li>
          <li>涨跌停软门控（非 CN 标的 → NaN）</li>
          <li>5 位代码兜底 → .HK 后缀</li>
        </ul>
        <div className="mt-2">M1 待办：</div>
        <ul className="list-disc pl-5 space-y-0.5">
          <li>H5：实时行情（quickquote 走腾讯/新浪 HTTP）</li>
          <li>H6：自选股列表按市场分组 + 市场选择器</li>
          <li>M2：美股（US_PROFILE + yfinance）</li>
        </ul>
      </div>
    </div>
  )
}
