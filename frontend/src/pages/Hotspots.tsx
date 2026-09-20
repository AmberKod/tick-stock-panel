import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MarketTab, type Market } from '@/components/MarketTab'
import { HotspotList } from '@/components/hotspot/HotspotList'
import { HotspotDetailDrawer } from '@/components/hotspot/HotspotDetailDrawer'
import { NewsPanel } from '@/components/hotspot/NewsPanel'
import { qualityOf } from '@/components/hotspot/labels'

const TOP_OPTIONS = [20, 50, 100] as const

/** 视图: 主题热度(行情聚合) / 新闻流(搜索源检索) */
type View = 'topics' | 'news'

const VIEW_TABS: { key: View; label: string }[] = [
  { key: 'topics', label: '主题热度' },
  { key: 'news', label: '新闻流' },
]

// 新闻流默认检索词: 不编造具体事件, 只给市场级宽 query
const MARKET_QUERY: Record<Market, string> = {
  cn: 'A股 市场 今日 要闻 政策',
  hk: '港股 市场 今日 要闻',
  us: 'US stock market news today',
}

const MARKET_LABEL: Record<Market, string> = { cn: 'A 股', hk: '港股', us: '美股' }

const STATUS_LABEL: Record<string, { text: string; className: string }> = {
  ok: { text: '同步成功', className: 'text-bull' },
  degraded: { text: '部分成功', className: 'text-warning' },
  empty: { text: '无数据', className: 'text-muted' },
  failed: { text: '同步失败', className: 'text-danger' },
  error: { text: '同步异常', className: 'text-danger' },
  skipped: { text: '已跳过', className: 'text-muted' },
}

function fmtTime(iso: string | null | undefined) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 热点工作区页面 — 市场切换 + 主题列表 + 详情抽屉。
 *
 * 三市场口径:
 *  - A 股: akshare 东财概念/行业板块 (外部板块列表)。
 *  - 港美: 后端本地聚合 —— instruments 行业分类 x 行情 (盘中实时 / 盘后回落上一交易日快照)。
 *    快照不是当日时后端会打 stale, 页面照出数据但显式标注快照日期, 不冒充实时。
 *  - 任一市场缺数据源时后端 fail-closed 返回 missing_mapping 空列表, 不降级成 Demo。
 */
export function Hotspots() {
  const qc = useQueryClient()
  const [market, setMarket] = useState<Market>('cn')
  const [top, setTop] = useState<number>(20)
  const [selected, setSelected] = useState<string | null>(null)
  const [view, setView] = useState<View>('topics')
  // 新闻流里点某个主题 → 按该主题检索
  const [newsTopic, setNewsTopic] = useState<string | null>(null)

  const list = useQuery({
    queryKey: QK.hotspots(market, top),
    queryFn: () => api.hotspots({ market, top }),
    placeholderData: prev => prev,
  })

  const jobState = useQuery({
    queryKey: QK.hotspotJobState,
    queryFn: api.hotspotJobState,
  })

  const refresh = useMutation({
    mutationFn: () => api.hotspotsRefresh(market),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.hotspots(market, top) })
      qc.invalidateQueries({ queryKey: QK.hotspotJobState })
    },
  })

  const rows = list.data?.hotspots ?? []
  const state = jobState.data
  const lastStatus = state?.last_status ? STATUS_LABEL[state.last_status] : undefined
  const lastSuccess = state?.last_success_at?.[market]

  // 数据状态按后端实际返回判断, 不按市场硬编码
  const quality = list.data?.quality_status ?? null
  const missingSource = quality === 'missing_mapping'
  const snapshotDate = rows.find(item => item.topic_date)?.topic_date ?? null
  const isStale = Boolean(list.data?.stale) || rows.some(item => item.stale)

  return (
    <div className="h-full overflow-auto bg-base p-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">热点工作区</h1>
          <p className="mt-1 text-xs text-muted">
            A 股来源为东财概念/行业板块,盘中每 30 分钟自动同步;港美为本地行业分类 × 行情聚合(盘中实时 / 盘后回落上一交易日快照)。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex overflow-hidden rounded-btn border border-border">
            {VIEW_TABS.map(t => (
              <button
                key={t.key}
                type="button"
                onClick={() => setView(t.key)}
                className={`px-2.5 py-1.5 text-xs transition-colors ${
                  view === t.key ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated hover:text-foreground'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <MarketTab active={market} onChange={m => { setMarket(m); setSelected(null); setNewsTopic(null) }} />
          <div className="inline-flex overflow-hidden rounded-btn border border-border">
            {TOP_OPTIONS.map(n => (
              <button
                key={n}
                type="button"
                onClick={() => setTop(n)}
                className={`px-2.5 py-1.5 text-xs transition-colors ${
                  top === n ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated hover:text-foreground'
                }`}
              >
                {n}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-base transition-colors hover:bg-accent/90 disabled:opacity-50"
          >
            {refresh.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
            同步
          </button>
        </div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-card border border-border bg-surface px-3 py-2 text-[11px] text-muted">
        <span>
          数据源 <span className="text-secondary">{list.data?.provider_used || '—'}</span>
        </span>
        <span>
          主题数 <span className="font-mono text-secondary">{list.data?.hotspot_count ?? 0}</span>
        </span>
        <span>
          最近同步 <span className="text-secondary">{fmtTime(state?.last_run)}</span>
        </span>
        <span>
          状态 <span className={lastStatus?.className ?? 'text-secondary'}>{lastStatus?.text ?? '—'}</span>
        </span>
        <span>
          本市场最近成功 <span className="text-secondary">{fmtTime(lastSuccess)}</span>
        </span>
        <span>
          快照日期{' '}
          <span className={isStale ? 'text-warning' : 'text-secondary'}>{snapshotDate || '—'}</span>
          {isStale && <span className="ml-1 text-warning">非当日</span>}
        </span>
        <span>
          数据质量{' '}
          <span className={`rounded px-1.5 py-0.5 ${qualityOf(quality).className}`}>{qualityOf(quality).text}</span>
        </span>
        {!!list.data?.source_errors?.length && (
          <span className="text-danger">源错误: {list.data.source_errors.join('; ')}</span>
        )}
      </div>

      {missingSource && (
        <div className="mb-3 flex items-start gap-2 rounded-card border border-border bg-elevated px-3 py-2 text-xs text-secondary">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <span>
            {MARKET_LABEL[market]}暂无热点 topic 数据源映射,后端按 fail-closed 返回空列表(不使用 Demo 数据)。
          </span>
        </div>
      )}
      {!missingSource && isStale && (
        <div className="mb-3 flex items-start gap-2 rounded-card border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-secondary">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
          <span>
            当前展示 {snapshotDate || '未知日期'} 的快照(非当日):
            {MARKET_LABEL[market]}热点由本地行业分类聚合行情得出,盘中取实时、盘后回落上一交易日快照。
            数据照出但不冒充实时,可点「同步」重试。
          </span>
        </div>
      )}
      {!missingSource && quality === 'failed' && (
        <div className="mb-3 flex items-start gap-2 rounded-card border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{MARKET_LABEL[market]}热点同步失败且无可用历史快照。</span>
        </div>
      )}
      {list.isError && (
        <div className="mb-3 rounded-card border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          热点列表加载失败{(list.error as Error)?.message ? `: ${(list.error as Error).message}` : ''}
        </div>
      )}

      {refresh.isError && (
        <div className="mb-3 rounded-card border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
          手动同步失败{(refresh.error as Error)?.message ? `: ${(refresh.error as Error).message}` : ''}
        </div>
      )}
      {refresh.data && (
        <div className="mb-3 rounded-card bg-elevated px-3 py-2 text-xs text-secondary">
          同步结果: {STATUS_LABEL[refresh.data.status]?.text ?? refresh.data.status} · {refresh.data.rows} 条 · 来源{' '}
          {refresh.data.provider || '—'}
        </div>
      )}

      {view === 'topics' ? (
        <HotspotList
          items={rows}
          loading={list.isLoading}
          selectedTopic={selected}
          onSelect={setSelected}
        />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => setNewsTopic(null)}
              className={`rounded border px-2 py-0.5 text-[11px] transition-colors ${
                newsTopic === null
                  ? 'border-accent bg-accent/10 text-accent'
                  : 'border-border text-muted hover:bg-elevated hover:text-foreground'
              }`}
            >
              市场要闻
            </button>
            {rows.slice(0, 12).map(item => (
              <button
                key={item.topic}
                type="button"
                onClick={() => setNewsTopic(item.topic)}
                className={`rounded border px-2 py-0.5 text-[11px] transition-colors ${
                  newsTopic === item.topic
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-border text-muted hover:bg-elevated hover:text-foreground'
                }`}
              >
                {item.name || item.topic}
              </button>
            ))}
          </div>
          <NewsPanel
            topic={newsTopic ?? undefined}
            query={newsTopic ? undefined : MARKET_QUERY[market]}
            days={7}
            maxResults={12}
          />
        </div>
      )}

      {selected && (
        <HotspotDetailDrawer topic={selected} market={market} onClose={() => setSelected(null)} />
      )}
    </div>
  )
}
