import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, KeyRound, Loader2, Newspaper, TriangleAlert } from 'lucide-react'
import { api, type NewsItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

/**
 * 新闻/舆情面板 — 补上热点"为什么涨"的那一维。
 *
 * 三种用法(三选一传参): concept(概念) / symbol(个股) / query(自由词)。
 * 未配置搜索 Key 时不伪装成"没有新闻", 直接给出配置入口:
 * 后端 fail-closed 会返回 success=false + "未配置 Key", 据此区分。
 */

const NOT_CONFIGURED_HINT = '未配置'

interface NewsPanelProps {
  topic?: string
  symbol?: string
  stockName?: string
  query?: string
  days?: number
  maxResults?: number
  /** 未配置 Key 时是否内联显示配置入口(详情页里可能需要更紧凑的样式) */
  showSetup?: boolean
  className?: string
}

function fmtDate(iso: string | null) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString('zh-CN')
}

function SearchKeySetup({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient()
  const [value, setValue] = useState('')

  const save = useMutation({
    mutationFn: (key: string) => api.saveSearchKey(key, 'anspire'),
    onSuccess: (res) => {
      if (!res.ok) {
        toast(`Key 校验失败: ${res.error ?? '未知原因'}`, 'error')
        return
      }
      toast('搜索源已配置', 'success')
      qc.invalidateQueries({ queryKey: QK.newsStatus })
      onDone()
    },
    onError: (e: any) => toast(e?.message ?? '保存失败', 'error'),
  })

  return (
    <div className="rounded-card border border-border bg-elevated/60 px-3 py-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-foreground">
        <KeyRound className="h-3.5 w-3.5 text-accent" />
        配置新闻搜索源
      </div>
      <p className="mb-2 text-[11px] leading-relaxed text-muted">
        热点只有行情聚合出的"热度", 要看到公告/事件/催化需要接搜索源。
        当前支持 Anspire(国内, 一 Key 兼大模型与联网检索)。支持逗号分隔多个 Key 轮询。
        Key 经系统级加密存在本机, 不会写进配置文件。
      </p>
      <div className="flex items-center gap-2">
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="粘贴 Anspire API Key"
          className="flex-1 rounded border border-border bg-background px-2 py-1 text-xs text-foreground outline-none focus:border-accent"
        />
        <button
          type="button"
          onClick={() => save.mutate(value.trim())}
          disabled={save.isPending || !value.trim()}
          className="flex items-center gap-1 rounded bg-accent px-2.5 py-1 text-xs text-white disabled:opacity-50"
        >
          {save.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
          校验并保存
        </button>
      </div>
    </div>
  )
}

function NewsRow({ item }: { item: NewsItem }) {
  return (
    <a
      href={item.url}
      target="_blank"
      rel="noreferrer noopener"
      className="group block rounded border border-border/60 bg-surface px-2.5 py-2 transition-colors hover:border-accent/40 hover:bg-elevated"
    >
      <div className="flex items-start justify-between gap-2">
        <span className="line-clamp-2 text-xs font-medium text-foreground group-hover:text-accent">
          {item.title || '(无标题)'}
        </span>
        <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-muted" />
      </div>
      {item.snippet && (
        <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-muted">{item.snippet}</p>
      )}
      <div className="mt-1 flex items-center gap-2 text-[10px] text-muted">
        <span>{item.source || '未知来源'}</span>
        {item.published_date && <span>{fmtDate(item.published_date)}</span>}
      </div>
    </a>
  )
}

export function NewsPanel({
  topic,
  symbol,
  stockName,
  query,
  days = 7,
  maxResults = 8,
  showSetup = true,
  className,
}: NewsPanelProps) {
  const qc = useQueryClient()

  const status = useQuery({
    queryKey: QK.newsStatus,
    queryFn: api.newsStatus,
    staleTime: 60_000,
  })

  const key = topic ? 'concept' : symbol ? 'stock' : 'query'
  const news = useQuery({
    queryKey:
      key === 'concept' ? QK.newsConcept(topic!, days)
        : key === 'stock' ? QK.newsStock(symbol!, days)
          : QK.newsSearch(query ?? '', days),
    queryFn: () =>
      topic ? api.newsConcept(topic, days, maxResults)
        : symbol ? api.newsStock(symbol, stockName, days, maxResults)
          : api.newsSearch(query ?? '', days, maxResults),
    enabled: Boolean(topic || symbol || query) && status.data?.configured_any !== false,
    placeholderData: (prev: any) => prev,
  })

  const isNotConfigured =
    status.data?.configured_any === false ||
    (news.data?.success === false && (news.data?.error_message ?? '').includes(NOT_CONFIGURED_HINT))

  if (isNotConfigured) {
    if (!showSetup) return null
    return (
      <div className={className}>
        <SearchKeySetup onDone={() => {
          qc.invalidateQueries({ queryKey: QK.newsStatus })
          news.refetch()
        }} />
      </div>
    )
  }

  if (news.isLoading) {
    return (
      <div className={cn('flex items-center gap-1.5 px-1 py-3 text-xs text-muted', className)}>
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        检索中…
      </div>
    )
  }

  if (news.isError) {
    return (
      <div className={cn('px-1 py-2 text-[11px] text-danger', className)}>
        新闻加载失败{(news.error as Error)?.message ? `: ${(news.error as Error).message}` : ''}
      </div>
    )
  }

  if (news.data?.success === false) {
    return (
      <div className={cn('flex items-start gap-1.5 rounded border border-warning/40 bg-warning/10 px-2.5 py-2 text-[11px] text-warning', className)}>
        <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" />
        <span>搜索失败: {news.data.error_message ?? '未知原因'}</span>
      </div>
    )
  }

  const items = news.data?.results ?? []
  if (items.length === 0) {
    return (
      <div className={cn('flex items-center gap-1.5 px-1 py-3 text-[11px] text-muted', className)}>
        <Newspaper className="h-3.5 w-3.5" />
        近 {days} 天没有检索到相关新闻
      </div>
    )
  }

  return (
    <div className={cn('space-y-1.5', className)}>
      {items.map((item, i) => (
        <NewsRow key={`${item.url}-${i}`} item={item} />
      ))}
      <div className="pt-0.5 text-[10px] text-muted">
        来源 {news.data?.provider || '—'} · {items.length} 条 · {news.data?.elapsed_s?.toFixed(2) ?? '—'}s
      </div>
    </div>
  )
}
