import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, Loader2, Newspaper, RefreshCw, Search } from 'lucide-react'
import { api, type NewsFeedEntry } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { NewsPanel } from '@/components/hotspot/NewsPanel'
import { cn } from '@/lib/cn'

/**
 * 新闻工作区 (/news) — 通用热点 + 股票视角。
 *
 * 分类由后端 `/api/news/categories` 提供, 两类:
 *  - rss    : 通用热点(国际/国内/科技/财经/能源/安全), 后端 RSS 聚合, **不需要 API Key**。
 *             热点新闻不该只有股票视角, 这条路是为了补上"今天世界发生了什么"。
 *  - search : 股市·个股, 走 Anspire 检索(需配 Key), 支持题材/自选股/自由搜索。
 *
 * 数据不可得时一律明示: 源全挂 → "源不可用"(而不是空列表), 没配 Key → 配置入口。
 */

const FALLBACK_CATEGORIES = [
  { key: 'world', label: '国际要闻', kind: 'rss' },
  { key: 'cn', label: '国内', kind: 'rss' },
  { key: 'tech', label: '科技·AI', kind: 'rss' },
  { key: 'finance', label: '财经·市场', kind: 'rss' },
  { key: 'energy', label: '能源·大宗', kind: 'rss' },
  { key: 'security', label: '安全·故障', kind: 'rss' },
  { key: 'market', label: '股市·个股', kind: 'search' },
]

const HOURS_OPTIONS = [24, 48, 72] as const

function fmtAgo(iso: string | null) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const mins = Math.floor((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

function FeedRow({ entry }: { entry: NewsFeedEntry }) {
  return (
    <a
      href={entry.url}
      target="_blank"
      rel="noreferrer noopener"
      className="group block rounded-card border border-border/70 bg-surface/60 px-3.5 py-3 transition-colors hover:border-accent/40 hover:bg-elevated"
    >
      <div className="flex items-start justify-between gap-3">
        <span className="line-clamp-2 text-sm font-medium text-foreground group-hover:text-accent">
          {entry.title || '(无标题)'}
        </span>
        <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted" />
      </div>
      {entry.summary && (
        <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-muted">{entry.summary}</p>
      )}
      <div className="mt-1.5 flex items-center gap-2 text-[10px] text-muted">
        <span className="shrink-0 rounded bg-elevated px-1.5 py-0.5 text-[10px] text-secondary">
          {entry.source}
        </span>
        {entry.published_at && <span>{fmtAgo(entry.published_at)}</span>}
      </div>
    </a>
  )
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'shrink-0 rounded-full border px-2.5 py-1 text-xs transition-colors',
        active
          ? 'border-accent bg-accent/15 text-accent'
          : 'border-border bg-surface text-secondary hover:border-accent/40 hover:text-foreground',
      )}
    >
      {children}
    </button>
  )
}

/** 股市·个股: 走 Anspire 检索的那一维(题材 / 自选股 / 自由搜索) */
function MarketView({ days }: { days: number }) {
  const [sub, setSub] = useState<'topic' | 'watchlist' | 'search'>('topic')
  const [topic, setTopic] = useState<string | null>(null)
  const [symbol, setSymbol] = useState<string | null>(null)
  const [stockName, setStockName] = useState<string | undefined>(undefined)
  const [term, setTerm] = useState('')
  const [submitted, setSubmitted] = useState('')

  const hotspots = useQuery({
    queryKey: QK.hotspots('cn', 20),
    queryFn: () => api.hotspots({ market: 'cn', top: 20 }),
    staleTime: 120_000,
    enabled: sub === 'topic',
  })
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 300_000,
    enabled: sub === 'watchlist',
  })

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Chip active={sub === 'topic'} onClick={() => setSub('topic')}>题材</Chip>
        <Chip active={sub === 'watchlist'} onClick={() => { setSub('watchlist'); setTopic(null) }}>自选股</Chip>
        <Chip active={sub === 'search'} onClick={() => { setSub('search'); setTopic(null) }}>自由搜索</Chip>
      </div>

      {sub === 'topic' && (
        <div className="flex flex-wrap gap-1.5">
          {hotspots.isLoading && <span className="text-[11px] text-muted">题材榜加载中…</span>}
          {(hotspots.data?.hotspots ?? []).map((t) => (
            <Chip key={t.topic} active={topic === t.topic} onClick={() => setTopic(t.topic)}>
              {t.topic}
              {t.change_pct != null && (
                <span className={cn('ml-1 font-mono', t.change_pct >= 0 ? 'text-bull' : 'text-bear')}>
                  {(t.change_pct * 100).toFixed(2)}%
                </span>
              )}
            </Chip>
          ))}
        </div>
      )}

      {sub === 'watchlist' && (
        <div className="flex flex-wrap gap-1.5">
          {watchlist.isLoading && <span className="text-[11px] text-muted">自选加载中…</span>}
          {(watchlist.data?.symbols ?? []).slice(0, 24).map((e) => (
            <Chip
              key={e.symbol}
              active={symbol === e.symbol}
              onClick={() => { setSymbol(e.symbol); setStockName(e.name ?? undefined) }}
            >
              {e.name || e.symbol}
            </Chip>
          ))}
        </div>
      )}

      {sub === 'search' && (
        <form
          onSubmit={(e) => { e.preventDefault(); setSubmitted(term.trim()) }}
          className="flex items-center gap-2"
        >
          <input
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="关键词, 如: 光伏 反内卷 / 英伟达 财报"
            className="h-9 flex-1 rounded-input border border-border bg-surface px-3 text-sm text-foreground outline-none placeholder:text-muted focus:border-accent"
          />
          <button
            type="submit"
            disabled={!term.trim()}
            className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-3 text-sm text-white disabled:opacity-40"
          >
            <Search className="h-3.5 w-3.5" />
            检索
          </button>
        </form>
      )}

      <div className="rounded-card border border-border bg-surface p-4">
        {sub === 'topic' && (topic
          ? <NewsPanel topic={topic} days={days} maxResults={12} />
          : <div className="py-4 text-center text-[11px] text-muted">选一个题材看它的新闻</div>)}
        {sub === 'watchlist' && (symbol
          ? <NewsPanel symbol={symbol} stockName={stockName} days={days} maxResults={12} />
          : <div className="py-4 text-center text-[11px] text-muted">选一只自选股看它的新闻</div>)}
        {sub === 'search' && (submitted
          ? <NewsPanel query={submitted} days={days} maxResults={12} />
          : <div className="py-4 text-center text-[11px] text-muted">输入关键词后回车检索</div>)}
      </div>
    </div>
  )
}

export function NewsPage() {
  const [category, setCategory] = useState('world')
  const [hours, setHours] = useState<number>(48)

  const cats = useQuery({
    queryKey: QK.newsCategories,
    queryFn: api.newsCategories,
    staleTime: 600_000,
  })

  const list = cats.data?.categories ?? FALLBACK_CATEGORIES.map((c) => ({
    ...c, source_count: 0, sources: [], note: '',
  }))
  const current = list.find((c) => c.key === category) ?? list[0]
  const isSearch = current?.kind === 'search'

  const feed = useQuery({
    queryKey: QK.newsFeeds(category, hours),
    queryFn: () => api.newsFeeds(category, hours, 40),
    enabled: !isSearch,
    staleTime: 120_000,
  })

  return (
    <div className="h-full overflow-auto bg-base">
      <div className="mx-auto w-full max-w-4xl px-5 py-6">
        {/* 头部 */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <Newspaper className="h-5 w-5 text-accent" />
            <h1 className="text-lg font-semibold text-foreground">新闻工作区</h1>
            <span className="text-[11px] text-muted">
              {isSearch ? '股票视角 · 走检索源(需配 Key)' : `RSS 聚合 · 不需要 API Key · 近 ${hours} 小时`}
            </span>
          </div>
          {!isSearch && (
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1 rounded-btn border border-border bg-surface px-1 py-0.5">
                {HOURS_OPTIONS.map((h) => (
                  <button
                    key={h}
                    type="button"
                    onClick={() => setHours(h)}
                    className={cn(
                      'rounded px-2 py-0.5 text-[11px] transition-colors',
                      hours === h ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground',
                    )}
                  >
                    {h}h
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={() => feed.refetch()}
                disabled={feed.isFetching}
                className="inline-flex h-7 items-center gap-1 rounded-btn border border-border bg-surface px-2 text-[11px] text-secondary hover:text-foreground disabled:opacity-50"
              >
                <RefreshCw className={cn('h-3 w-3', feed.isFetching && 'animate-spin')} />
                刷新
              </button>
            </div>
          )}
        </div>

        {/* 分类 */}
        <div className="mt-4 flex flex-wrap gap-1.5">
          {list.map((c) => (
            <Chip key={c.key} active={c.key === category} onClick={() => setCategory(c.key)}>
              {c.label}
            </Chip>
          ))}
        </div>
        {current?.note && !isSearch && (
          <div className="mt-2 text-[10px] text-muted">{current.note}</div>
        )}

        {/* 内容 */}
        <div className="mt-4">
          {isSearch ? (
            <MarketView days={7} />
          ) : feed.isLoading ? (
            <div className="flex items-center gap-2 py-8 text-xs text-muted">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              抓取中…
            </div>
          ) : feed.isError ? (
            <div className="py-8 text-center text-xs text-danger">
              新闻流加载失败{(feed.error as Error)?.message ? `: ${(feed.error as Error).message}` : ''}
            </div>
          ) : !feed.data?.success ? (
            <div className="flex items-start gap-2 rounded-card border border-warning/40 bg-warning/10 px-3 py-3 text-[11px] text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div>
                <div>所有源都抓失败了(不是"没新闻", 是源不可用)</div>
                {(feed.data?.source_errors ?? []).slice(0, 4).map((e) => (
                  <div key={e} className="mt-0.5 font-mono text-[10px] opacity-80">{e}</div>
                ))}
              </div>
            </div>
          ) : (feed.data?.entries?.length ?? 0) === 0 ? (
            <div className="py-8 text-center text-[11px] text-muted">
              近 {hours} 小时该分类没有新条目
              {feed.data && feed.data.source_count > 0 && (
                <span className="ml-1">
                  ({feed.data.ok_source_count}/{feed.data.source_count} 个源已成功抓取)
                </span>
              )}
            </div>
          ) : (
            <div className="space-y-2">
              {feed.data!.entries.map((e, i) => (
                <FeedRow key={`${e.url}-${i}`} entry={e} />
              ))}
              <div className="pt-1 text-[10px] text-muted">
                {feed.data!.entry_count} 条 · {feed.data!.ok_source_count}/{feed.data!.source_count} 源 ·
                {' '}{feed.data!.elapsed_s?.toFixed(2) ?? '—'}s
                {feed.data!.cached && ' · 缓存'}
                {feed.data!.source_errors.length > 0 && (
                  <span className="ml-1 text-warning">({feed.data!.source_errors.length} 个源失败)</span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
