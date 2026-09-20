import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { FlaskConical, HelpCircle, History, Power, RefreshCw, Search, Settings2 } from 'lucide-react'
import { api, type AbnormalOverview, type AbnormalRow, type AbnormalStatus, type NewsBatchStockItem, type NewsBatchStockResult } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPrice, fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/components/stock-table/primitives'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { Panel } from '@/components/panel'
import { tierQueryOptions } from '@/lib/refreshTiers'

/**
 * 异动监控 — 按交易所异动规则口径 (3日±20%/±30%/±40%, 10日+100%, 30日+200%)
 * 实时计算个股「偏离值/阈值」接近度, 找出处于异动边缘的标的。
 *
 * 计算量可控: 主开关默认关闭, 开启后才发起轮询 (每 60s 一次); 关闭后不再计算,
 * 但保留展示上次计算结果 (含计算时间, 取自 localStorage)。
 * 规则口径通过标题栏「?」展开查看。告警走系统监控体系: 在「监控中心」创建
 * 异动监控规则后由后端持续评估, 统一触发记录/站内通知/飞书·企微推送。
 */

const WINDOW_KEYS = ['3d', '10d', '30d'] as const
type WindowKey = (typeof WINDOW_KEYS)[number]

const WINDOW_LABELS: Record<WindowKey, string> = {
  '3d': '3日偏离',
  '10d': '10日偏离',
  '30d': '30日偏离',
}

const STATUS_META: Record<AbnormalStatus, { label: string; cls: string; bar: string }> = {
  triggered: { label: '已触发', cls: 'bg-danger/15 text-danger', bar: 'bg-danger' },
  edge: { label: '异动边缘', cls: 'bg-warning/15 text-warning', bar: 'bg-warning' },
  watch: { label: '观察', cls: 'bg-elevated text-secondary', bar: 'bg-muted' },
}

const BOARDS = ['主板', '创业板', '科创板', '北交所'] as const

const REFRESH_MS = 60_000

// ============================================================================
// 面板 2 · 异动归因 (有解释 / 待确认 / 无解释 / 反向背离)
// ----------------------------------------------------------------------------
// 判定口径纪律: 后端 news_search.py:197 `source=_extract_domain(url)` 是**纯域名**,
// 未做出版方家族归一。因此判定只能用「不同域名数 ≥ 2」, UI 上绝不能写成
// 「独立信源」/「≥2 家媒体」。后端响应的 `domain_note` 就是给 UI 用的口径提示,
// 下面原样渲染进 Panel footer。
//
// 四态不是加权打分, 是可解释规则 (PRD §3.2 面板 2)。
// ============================================================================

/** 归因新闻窗口(天)。PRD §3.2 面板 2 建议 24h。 */
const ATTRIB_DAYS = 1
/**
 * 默认自动归因条数。
 * 后端 POST /api/news/batch-stock 是串行限速 0.6s/个 + 25s 时间预算 + 单批 200 上限,
 * 实测 25s 预算 ≈ 一次请求 40 个 symbol。取 20 留一倍余量, 绝不把全表塞进去。
 * 其余条目走「单条点击再查」: 同一端点, 后端按 symbol 缓存, 不重复消耗配额。
 */
const ATTRIB_TOP_N = 20
/** 单条归因请求的新闻条数上限 */
const ATTRIB_MAX_RESULTS = 8

/** 归因四态 + 「未归因」(尚未查询) 这一非判定态。 */
type AttributionState = 'explained' | 'unconfirmed' | 'silent' | 'divergent' | 'unknown'

interface AttributionMeta {
  label: string
  cls: string
  hint: string
  /** 该档位能否由现有数据判定。false = 本页不可用, 只在图例里出现, 不参与判定。 */
  decidable: boolean
}

const ATTRIBUTION_META: Record<AttributionState, AttributionMeta> = {
  explained: {
    label: '有解释',
    cls: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
    hint: `${ATTRIB_DAYS * 24}h 窗口内 ≥2 个不同域名命中`,
    decidable: true,
  },
  unconfirmed: {
    label: '待确认',
    cls: 'border-warning/30 bg-warning/10 text-warning',
    hint: `${ATTRIB_DAYS * 24}h 窗口内恰好 1 个域名命中`,
    decidable: true,
  },
  silent: {
    label: '无解释',
    cls: 'border-danger/40 bg-danger/10 text-danger',
    hint: '窗口内 0 个域名命中 —— 没人解释的异动, 默认置顶',
    decidable: true,
  },
  divergent: {
    label: '反向背离',
    cls: 'border-border bg-elevated text-muted',
    hint: '本页不可用: 异动接口不返回题材/行业方向, 不硬算',
    decidable: false,
  },
  unknown: {
    label: '未归因',
    cls: 'border-border/60 bg-elevated/60 text-muted',
    hint: '尚未查询 (不在自动归因的前 N 条内), 点击徽标单条查询',
    decidable: true,
  },
}

/** 列表排序权重: 无解释置顶, 未归因沉底(点击徽标可补查)。 */
const ATTRIB_ORDER: Record<AttributionState, number> = {
  silent: 0,
  unconfirmed: 1,
  explained: 2,
  unknown: 3,
  divergent: 4,
}

/** 由批量端点返回的单项结果判定归因状态。拿不到结果一律 'unknown', 绝不猜。 */
function resolveAttribution(item: NewsBatchStockItem | undefined): AttributionState {
  if (!item || !item.success) return 'unknown'
  const domains = item.hit_domains?.length ?? item.hit_domain_count ?? 0
  if (domains >= 2) return 'explained'
  if (domains === 1) return 'unconfirmed'
  return 'silent'
}

/**
 * ⚠️ fail-closed 纪律 (勿删):
 * 后端 POST /api/news/batch-stock 的顶层 `ok` **恒为 true**, 即使 provider 全挂
 * (未配置 Key / 配额耗尽 / 代理 502 / 余额不足) 也返回 ok —— **不可用于任何判定**。
 * 数据源健康只能看: `GET /api/news/status` 的 `configured_any` (配置态) +
 * 批量结果里的 `results[].success` / `error_count` / `processed` (运行态)。
 * `processed > 0 && error_count === processed` = 全部失败 → 整块 unavailable,
 * 绝不能渲染成一片「未归因」灰 —— 那会把"我们没查到"伪装成"市场没消息"。
 */

export function AbnormalMoves() {
  // 主开关: 默认关闭, 开启后才轮询计算 (仅控制本页计算, 后台告警由监控规则驱动)
  const [enabled, setEnabled] = useState(() => storage.abnormalEnabled.get(false))
  // 规则口径面板 (标题栏「?」)
  const [rulesOpen, setRulesOpen] = useState(false)
  // 上次计算结果: 开启时每次成功计算都落本地, 关闭后仍展示
  const [lastResult, setLastResult] = useState<AbnormalOverview | null>(
    () => (storage.abnormalLastResult.get(null) as AbnormalOverview | null) ?? null,
  )
  const [windowFilter, setWindowFilter] = useState<'all' | WindowKey>('all')
  const [direction, setDirection] = useState<'both' | 'up' | 'down'>('both')
  const [boardFilter, setBoardFilter] = useState<'all' | (typeof BOARDS)[number]>('all')
  const [minCloseness, setMinCloseness] = useState(0.5)
  const [query, setQuery] = useState('')
  const [watchlistOnly, setWatchlistOnly] = useState(false)
  // 默认过滤 ST/*ST 风险警示股票 (口径与后端 is_st_name 一致: 名称含 ST)
  const [excludeSt, setExcludeSt] = useState(true)
  const [preview, setPreview] = useState<{ symbol: string; name: string } | null>(null)
  // ── 面板 2 · 异动归因 ──
  const [silentOnly, setSilentOnly] = useState(false)
  /** 单条点击再查的 symbol 追加集合 (与自动前 N 条合并进同一批请求, 命中后端缓存) */
  const [extraSymbols, setExtraSymbols] = useState<string[]>([])
  /** 展开判据(命中的新闻)的 symbol */
  const [expandedSymbol, setExpandedSymbol] = useState<string | null>(null)

  const overview = useQuery({
    queryKey: QK.abnormalOverview(minCloseness, 300),
    queryFn: () => api.abnormalOverview(minCloseness, 300),
    enabled, // 关闭时零计算
    refetchInterval: enabled ? REFRESH_MS : false,
  })
  // 自选过滤在关闭 (查看上次结果) 时也可用: 自选列表是轻量接口, 不涉及全市场计算
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    enabled: watchlistOnly,
  })

  const toggleEnabled = (v: boolean) => {
    setEnabled(v)
    storage.abnormalEnabled.set(v)
    if (v) {
      overview.refetch()
    }
  }

  const data = overview.data
  useEffect(() => {
    if (!data) return
    setLastResult(data)
    storage.abnormalLastResult.set(data)
  }, [data])

  // 展示数据源: 开启 → 实时结果; 关闭 → 上次计算结果 (可能为空)
  const view = enabled ? data : lastResult
  const stale = !enabled && lastResult != null

  const watchSymbols = useMemo(() => {
    const set = new Set((watchlist.data?.symbols ?? []).map(e => e.symbol))
    return set
  }, [watchlist.data])

  const rows = useMemo(() => {
    let list = view?.rows ?? []
    if (windowFilter !== 'all') {
      list = list.filter(r => {
        const w = r.windows[windowFilter]
        return w != null && w.closeness >= minCloseness
      })
    }
    if (direction !== 'both') {
      list = list.filter(r => {
        const w = windowFilter !== 'all' ? r.windows[windowFilter] : dominantWindow(r)
        const v = w?.value ?? 0
        return direction === 'up' ? v > 0 : v < 0
      })
    }
    if (boardFilter !== 'all') list = list.filter(r => r.board === boardFilter)
    if (watchlistOnly) list = list.filter(r => watchSymbols.has(r.symbol))
    if (excludeSt) list = list.filter(r => !(r.name ?? '').toUpperCase().includes('ST'))
    const q = query.trim().toLowerCase()
    if (q) {
      list = list.filter(r => `${r.symbol} ${r.name ?? ''}`.toLowerCase().includes(q))
    }
    return list
  }, [view, windowFilter, direction, boardFilter, watchlistOnly, excludeSt, watchSymbols, query, minCloseness])

  // ── 归因查询 (面板 2) ──────────────────────────────
  // 新闻源配置状态: 未配置 Anspire Key 时整块归因不可用 (不能渲染成"0 个命中")
  const newsStatus = useQuery({
    queryKey: QK.newsStatus,
    queryFn: api.newsStatus,
    staleTime: 60_000,
  })
  /** null = 还没查到配置状态 */
  const newsConfigured: boolean | null = newsStatus.data?.configured_any ?? null

  /** 按异动幅度排序: 主序=最高档接近度, 同分比今日涨跌绝对值 */
  const rankedRows = useMemo(
    () => [...rows].sort(
      (a, b) => closenessOf(b) - closenessOf(a) || Math.abs(b.rt_pct ?? 0) - Math.abs(a.rt_pct ?? 0),
    ),
    [rows],
  )
  const autoSymbols = useMemo(
    () => rankedRows.slice(0, ATTRIB_TOP_N).map(r => r.symbol),
    [rankedRows],
  )
  /** 本次批量请求的 symbol 集合: 自动前 N 条 + 用户单条点击补查 */
  const batchSymbols = useMemo(
    () => Array.from(new Set([...autoSymbols, ...extraSymbols])),
    [autoSymbols, extraSymbols],
  )
  const batchNames = useMemo(() => {
    const map: Record<string, string> = {}
    for (const r of rows) {
      if (r.name) map[r.symbol] = r.name
    }
    return map
  }, [rows])

  const attribution = useQuery({
    queryKey: QK.newsBatchStock(batchSymbols, ATTRIB_DAYS),
    // 走批量端点: 后端串行限速 + 结果缓存。前端禁止并发逐条打 /api/news/stock。
    // ⚠️ 主开关关闭 (stale 模式) 时不再发归因请求 —— 关了监控还偷偷花新闻配额不合理。
    queryFn: () => api.newsBatchStock(batchSymbols, batchNames, ATTRIB_DAYS, ATTRIB_MAX_RESULTS),
    enabled: enabled && newsConfigured === true && batchSymbols.length > 0,
    // T3 日级: 结论类数据, 一天内基本不变, 不进 SSE 失效列表
    ...tierQueryOptions('T3'),
    // 刷新期间容器不卸载、不闪烁
    placeholderData: (prev: NewsBatchStockResult | undefined) => prev,
  })

  /**
   * fail-closed 判定: 数据源挂没挂。
   * ⚠️ 后端 `ok` 字段**恒为 true** (即使 provider 全挂), 不可用于任何判定 ——
   * 只能看 `results[].success` / `error_count` / `processed`。
   * 三档 + 一态:
   *   'config'   Key 没配 (configured_any=false) → 不可用
   *   'all-fail' 配了但全部失败 (配额耗尽/代理 502/余额不足) → 不可用, 带 error 文案
   *   'paused'   主开关关闭且无缓存结果 → 不发请求, 徽标禁用 "—" (提示开启后归因)
   *   'ok'       全部成功或部分成功 (部分失败维持现状: 成功出徽标、失败灰「未归因」+ hint 计数)
   */
  const attributionHealth = useMemo<'config' | 'all-fail' | 'paused' | 'ok'>(() => {
    if (newsConfigured === false) return 'config'
    const d = attribution.data
    if (d && d.processed > 0 && d.error_count === d.processed) return 'all-fail'
    // 主开关关闭: 不发新请求, 只展示缓存里的历史结果; 无缓存时不伪装成「未归因」
    if (!enabled && d == null) return 'paused'
    return 'ok'
  }, [newsConfigured, attribution.data, enabled])

  /** 全部失败时取第一条 error 给用户看真实原因 (余额不足/代理 502 等) */
  const attributionErrorSample = useMemo(() => {
    if (attributionHealth !== 'all-fail') return null
    const results = attribution.data?.results ?? {}
    for (const symbol of batchSymbols) {
      const err = results[symbol]?.error
      if (err) return { symbol, err }
    }
    return null
  }, [attributionHealth, attribution.data, batchSymbols])

  const attributionBySymbol = useMemo(() => {
    const map = new Map<string, { state: AttributionState; item?: NewsBatchStockItem }>()
    const results = attribution.data?.results ?? {}
    for (const symbol of batchSymbols) {
      const item = results[symbol]
      map.set(symbol, { state: resolveAttribution(item), item })
    }
    return map
  }, [batchSymbols, attribution.data])

  /** 展示顺序: 无解释置顶; 开启「只看无解释」时只留该档 */
  const displayRows = useMemo(() => {
    if (silentOnly) {
      return rankedRows.filter(r => attributionBySymbol.get(r.symbol)?.state === 'silent')
    }
    return [...rankedRows].sort((a, b) => {
      const sa = attributionBySymbol.get(a.symbol)?.state ?? 'unknown'
      const sb = attributionBySymbol.get(b.symbol)?.state ?? 'unknown'
      return ATTRIB_ORDER[sa] - ATTRIB_ORDER[sb]
    })
  }, [rankedRows, silentOnly, attributionBySymbol])

  const attribStats = useMemo(() => {
    const counts: Record<AttributionState, number> = {
      explained: 0, unconfirmed: 0, silent: 0, divergent: 0, unknown: 0,
    }
    for (const entry of attributionBySymbol.values()) counts[entry.state] += 1
    return counts
  }, [attributionBySymbol])

  /** 已提交但后端明确失败的条数 (区分「还在等」和「失败」, 不能把等待渲染成失败) */
  const attribFailed = useMemo(() => {
    let n = 0
    for (const entry of attributionBySymbol.values()) {
      if (entry.item && !entry.item.success) n += 1
    }
    return n
  }, [attributionBySymbol])

  /** 面板内「无人解释」清单: 本面板的核心产出 */
  const silentPreviewRows = useMemo(
    () => displayRows.filter(r => attributionBySymbol.get(r.symbol)?.state === 'silent').slice(0, 10),
    [displayRows, attributionBySymbol],
  )

  /** 单条点击再查: 追加进同一批 (后端按 symbol 缓存, 已有的不消耗配额) */
  const requestAttribution = (symbol: string) => {
    setExtraSymbols(prev => (prev.includes(symbol) ? prev : [...prev, symbol]))
  }

  const counts = view?.counts
  const updating = overview.isFetching

  return (
    // 整页占满视口: 头部/筛选固定, 只有表格列表区滚动
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0">
        <PageHeader
        title="异动监控"
        subtitle="3日异常波动 / 10日·30日严重异常波动 · 偏离值接近度"
        right={
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-pressed={rulesOpen}
              aria-label="查看异动规则口径"
              title="交易所异动规则口径 (阈值 / 偏离值计算方式)"
              onClick={() => setRulesOpen(v => !v)}
              className={`inline-flex h-7 w-7 items-center justify-center rounded border transition-colors ${
                rulesOpen
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-border bg-base text-secondary hover:text-foreground'
              }`}
            >
              <HelpCircle className="h-3.5 w-3.5" />
            </button>
            {enabled && (
              <button
                type="button"
                onClick={() => overview.refetch()}
                className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
                title="立即刷新"
              >
                <RefreshCw className={`h-3 w-3 ${updating ? 'animate-spin' : ''}`} />
                刷新
              </button>
            )}
          <Link
            to="/monitor"
            className="inline-flex h-7 items-center gap-1 rounded border border-border bg-base px-2 text-[11px] text-secondary transition-colors hover:text-foreground"
            title="在监控中心创建「异动监控」规则: 后台持续评估, 触发时统一走触发记录/站内通知/飞书·企微推送, 无需保持本页打开"
          >
            <Settings2 className="h-3 w-3" />
            告警规则
          </Link>
          {/* 主开关: 开启后才开始轮询计算 */}
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              aria-label="启用异动监控计算"
              onClick={() => toggleEnabled(!enabled)}
              className={`inline-flex h-7 items-center gap-2 rounded border px-2.5 text-[11px] font-medium transition-colors ${
                enabled
                  ? 'border-accent/40 bg-accent/12 text-accent'
                  : 'border-border bg-base text-secondary hover:text-foreground'
              }`}
            >
              <Power className="h-3 w-3" />
              {enabled ? '监控中 · 每60秒计算' : '开启监控'}
            </button>
          </div>
        }
      />
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 px-5 py-4">

      {/* 规则口径面板 (标题栏「?」展开) */}
      {rulesOpen && (
        <div className="shrink-0 rounded-card border border-border bg-surface p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {ruleChips()}
          </div>
          <p className="mt-2.5 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted">
            口径说明: 偏离值 = 个股 N 日累计涨跌幅 − 对应指数同期涨跌幅 (沪: 上证A指/上证指数,
            深: 深证A指/深证成指, 北: 北证50)。阈值为交易所异常波动披露标准的近似值, 仅供风险提示,
            不构成监管认定。每只股票在 3日/10日/30日 三档各算一个接近度 (|偏离值| ÷ 该档阈值,
            阈值随板块不同; 2026-07-06 起主板风险警示股票与普通股票同口径), 表格「接近度」列与状态取三档中的最高值,
            来源窗口的偏离值颜色加重显示、其余窗口淡化; ≥100% 已触发、≥70% 边缘、≥50% 观察。
            偏离列亦可在自选/选股的「异动」列组中启用, 并可作为监控规则与自定义信号的阈值字段。
          </p>
        </div>
      )}

      {/* 未开启且无历史结果: 说明 + 开启入口 (有上次结果时直接展示数据, 见下方 stale 横幅) */}
      {!enabled && !stale ? (
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <div className="m-auto rounded-card border border-border bg-surface p-8 text-center">
            <FlaskConical className="mx-auto h-8 w-8 text-muted/70" />
            <div className="mt-3 text-sm font-medium text-foreground">监控未开启</div>
            <p className="mx-auto mt-2 max-w-lg text-xs leading-relaxed text-muted">
              开启后按交易所异动规则实时计算全市场个股的涨跌幅偏离值 (个股 N 日累计涨跌 −
              对应指数同期), 找出接近触发「异常波动 / 严重异常波动」的标的。
              计算量较大, 默认关闭; 每次计算的结果会保留, 关闭后仍可查看 (不再实时更新)。
            </p>
            <p className="mx-auto mt-2 max-w-lg text-[11px] leading-relaxed text-muted/80">
              需要告警推送时, 在<Link to="/monitor?new=abnormal" className="text-accent hover:underline">监控中心</Link>
              新建「异动监控」规则 —— 后台持续评估, 触发时统一走触发记录 / 站内通知 / 飞书·企微推送,
              与本页开关互不影响。
            </p>
            <button
              type="button"
              onClick={() => toggleEnabled(true)}
              className="mt-5 inline-flex h-9 items-center gap-2 rounded-btn bg-accent px-4 text-xs font-medium text-base"
            >
              <Power className="h-4 w-4" />
              开启监控
            </button>
          </div>
        </div>
      ) : (
        <>
          {/* 关闭后展示上次计算结果 */}
          {stale && (
            <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-card border border-warning/25 bg-warning/5 px-3 py-2">
              <History className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-[11px] font-medium text-warning">已暂停计算 · 展示上次结果</span>
              <span className="text-[11px] text-secondary">
                上次计算 {fmtCalcTime(lastResult.asof)} · 数据截至 {lastResult.cache_date ?? '—'}
                {lastResult.includes_today ? ' (含今日收盘)' : ''}
              </span>
              <button
                type="button"
                onClick={() => toggleEnabled(true)}
                className="ml-auto inline-flex h-7 shrink-0 items-center gap-1 rounded border border-accent/40 bg-accent/10 px-2.5 text-[11px] font-medium text-accent transition-colors hover:bg-accent/15"
              >
                <Power className="h-3 w-3" />
                开启实时计算
              </button>
            </div>
          )}

          {/* 统计 + 筛选 */}
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <StatusChip label="已触发" count={counts?.triggered} tone="danger" />
            <StatusChip label="异动边缘" count={counts?.edge} tone="warning" />
            <StatusChip label="观察" count={counts?.watch} tone="muted" />
            {enabled && (
              <span className="text-[10px] text-muted">
                数据截至 {data?.cache_date ?? '—'}
                {data?.includes_today ? ' (含今日收盘)' : ' · 已叠加今日实时涨跌'}
                {data ? ` · 基准指数今日 ${(data.bench_rt_pct * 100).toFixed(2)}%` : ''}
              </span>
            )}
          </div>

          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <SegmentedControl
              value={windowFilter}
              onChange={v => setWindowFilter(v)}
              options={[
                { value: 'all', label: '全部窗口' },
                ...WINDOW_KEYS.map(w => ({ value: w, label: WINDOW_LABELS[w] })),
              ]}
            />
            <SegmentedControl
              value={direction}
              onChange={v => setDirection(v)}
              options={[
                { value: 'both', label: '双向' },
                { value: 'up', label: '正向' },
                { value: 'down', label: '负向' },
              ]}
            />
            <SegmentedControl
              value={boardFilter}
              onChange={v => setBoardFilter(v)}
              options={[
                { value: 'all' as const, label: '全板块' },
                ...BOARDS.map(b => ({ value: b, label: b })),
              ]}
            />
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="只看自选列表中的标的">
              <input
                type="checkbox"
                checked={watchlistOnly}
                onChange={e => setWatchlistOnly(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              只看自选
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="过滤 ST/*ST 风险警示股票">
              <input
                type="checkbox"
                checked={excludeSt}
                onChange={e => setExcludeSt(e.target.checked)}
                className="h-3 w-3 accent-accent"
              />
              过滤ST
            </label>
            <label
              className="flex items-center gap-1.5 text-[11px] text-secondary"
              title="只看窗口内 0 个域名命中的异动 —— 没人解释的才最值得看 (新闻源未配置/全部失败/监控暂停时不可用)"
            >
              <input
                type="checkbox"
                checked={silentOnly}
                disabled={attributionHealth !== 'ok'}
                onChange={e => setSilentOnly(e.target.checked)}
                className="h-3 w-3 accent-accent disabled:opacity-40"
              />
              只看无解释
            </label>
            <label className="flex items-center gap-1.5 text-[11px] text-secondary" title="接近度下限 (|偏离|/阈值)">
              接近度 ≥ {(minCloseness * 100).toFixed(0)}%
              <input
                type="range"
                min={30}
                max={100}
                step={5}
                value={minCloseness * 100}
                onChange={e => setMinCloseness(Number(e.target.value) / 100)}
                className="h-1 w-24 accent-accent"
              />
            </label>
            <div className="relative ml-auto">
              <Search className="absolute left-2 top-1.5 h-3.5 w-3.5 text-muted" />
              <input
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="搜索代码/名称"
                className="h-7 w-40 rounded border border-border bg-base pl-7 pr-2 text-[11px] text-foreground"
              />
            </div>
          </div>

          {/* 面板 2 · 异动归因: 五态(ok/loading/error/unavailable/empty) 由 Panel 统一承载,
              拿不到数据时显示「不可用」, 绝不渲染成一片"0 个命中"的风平浪静。
              fail-closed 两种都走 unavailable: ① Key 没配 (configured_any=false);
              ② 配了但全部失败 (配额耗尽/代理 502/余额不足) —— 后端 ok 恒 true 不可信。 */}
          <div className="shrink-0">
            <Panel
              title="异动归因"
              icon={Search}
              hint={`自动 ${Math.min(ATTRIB_TOP_N, autoSymbols.length)} / ${rows.length} 条 · 窗口 ${ATTRIB_DAYS * 24}h${
                attribution.data ? ` · ${attribution.data.elapsed_s.toFixed(1)}s` : ''
              }${attribFailed > 0 && attributionHealth === 'ok' ? ` · ${attribFailed} 条查询失败` : ''}${
                !enabled ? ' · 监控已暂停, 不发归因请求' : ''
              }`}
              loading={newsStatus.isLoading || (enabled && newsConfigured === true && batchSymbols.length > 0 && attribution.isLoading)}
              error={newsStatus.error ?? (newsConfigured === true && attributionHealth === 'ok' ? attribution.error : null)}
              unavailable={
                attributionHealth === 'config'
                  ? '未配置新闻搜索源 (Anspire Key), 归因不可得。请在「设置 · 密钥」中配置后重试。'
                  : attributionHealth === 'all-fail'
                    ? `新闻检索全部失败 (${attribution.data?.processed ?? 0} 条无一成功): ${attributionErrorSample?.err ?? '未知错误'}。请检查 Key 配额/代理后重试。`
                    : attributionHealth === 'paused'
                      ? '监控已暂停, 归因未查询。开启实时计算后自动归因前 20 条。'
                      : false
              }
              empty={rows.length === 0 ? '当前口径下没有异动条目可归因。' : false}
              stale={attribution.isError && attribution.data != null ? '本次归因刷新失败, 当前展示上一次成功结果。' : false}
              onRetry={() => {
                void newsStatus.refetch()
                void attribution.refetch()
              }}
              evidence={{
                label: '判定规则',
                items: [
                  `有解释: ${ATTRIB_DAYS * 24}h 窗口内 ≥2 个不同域名命中 (域名 ≠ 独立信源, 见面板底部口径)`,
                  '待确认: 窗口内恰好 1 个域名命中',
                  '无解释: 窗口内 0 个域名命中 —— 默认置顶, 没人解释的异动才最值得看',
                  '反向背离 (个股涨 vs 题材跌): 本页不可用 —— 异动接口不返回题材/行业方向, 不硬算',
                  `自动归因范围: 按异动幅度(最高档接近度)取前 ${ATTRIB_TOP_N} 条, 受批量端点 25s 预算约束; 其余条目点击行内徽标单条补查`,
                ],
              }}
              footer={
                <p className="text-[10px] leading-relaxed text-muted">
                  {/* 后端给的口径提示, 原样渲染 —— 不能把"不同域名"包装成"独立信源" */}
                  {attribution.data?.domain_note ??
                    '口径: 命中数按「不同域名」计, 后端未做出版方家族归一, 同一媒体多站点可能重复计数。'}
                </p>
              }
            >
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-1.5">
                  {(['silent', 'unconfirmed', 'explained', 'divergent'] as const).map(state => (
                    <span
                      key={state}
                      title={ATTRIBUTION_META[state].hint}
                      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium ${ATTRIBUTION_META[state].cls}`}
                    >
                      {ATTRIBUTION_META[state].label}
                      <span className="font-mono">
                        {ATTRIBUTION_META[state].decidable ? attribStats[state] : '不可用'}
                      </span>
                    </span>
                  ))}
                  <span className="ml-auto text-[10px] text-muted">
                    已归因 {attribStats.explained + attribStats.unconfirmed + attribStats.silent} 条
                    {attribFailed > 0 && ` · 失败 ${attribFailed} 条`}
                    {rows.length > autoSymbols.length && ` · 其余 ${rows.length - autoSymbols.length} 条点击徽标补查`}
                  </span>
                </div>
                {silentPreviewRows.length > 0 ? (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-[10px] text-danger">无人解释的异动:</span>
                    {silentPreviewRows.map(r => (
                      <button
                        key={r.symbol}
                        type="button"
                        onClick={() => setPreview({ symbol: r.symbol, name: r.name ?? r.symbol })}
                        title="窗口内 0 个域名命中 —— 点击查看详情"
                        className="inline-flex items-center gap-1 rounded border border-danger/30 bg-danger/5 px-1.5 py-0.5 text-[10px] transition-colors hover:border-danger/60 cursor-pointer"
                      >
                        <span className="font-mono text-foreground">{r.symbol}</span>
                        <span className="max-w-24 truncate text-secondary">{r.name ?? '—'}</span>
                        <span className={`font-mono ${priceColorClass(r.rt_pct)}`}>{fmtPct(r.rt_pct)}</span>
                      </button>
                    ))}
                    {silentPreviewRows.length < displayRows.filter(r => attributionBySymbol.get(r.symbol)?.state === 'silent').length && (
                      <button
                        type="button"
                        onClick={() => setSilentOnly(true)}
                        className="text-[10px] text-accent hover:underline cursor-pointer"
                      >
                        查看全部
                      </button>
                    )}
                  </div>
                ) : (
                  <p className="text-[10px] text-muted">
                    {attribution.data ? '已归因的条目里没有「无解释」异动。' : '—'}
                  </p>
                )}
              </div>
            </Panel>
          </div>

          {/* 主表: 剩余空间内滚动 (页面本身不滚动) */}
          <div className="min-h-0 flex-1 overflow-auto rounded-card border border-border bg-surface">
            <table className="w-full min-w-[860px] text-xs">
              <thead className="sticky top-0 z-10 bg-surface">
                <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted">
                  <th className="w-10 px-2 py-2 text-right">#</th>
                  <th className="px-2 py-2 text-left">代码 / 名称</th>
                  <th className="px-2 py-2 text-right">现价</th>
                  <th className="px-2 py-2 text-right">今日</th>
                  {WINDOW_KEYS.map(w => (
                    <th key={w} className="px-2 py-2 text-right">
                      {WINDOW_LABELS[w]}
                      <span className="ml-1 normal-case text-muted/80">(阈值)</span>
                    </th>
                  ))}
                  <th
                    className="w-36 px-2 py-2 text-left"
                    title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
                  >
                    接近度
                    <span className="ml-1 normal-case text-muted/80">(最高档)</span>
                  </th>
                  <th className="px-2 py-2 text-center">状态</th>
                  <th
                    className="px-2 py-2 text-center"
                    title="归因四态(可解释规则, 非打分): 有解释=≥2 个不同域名命中; 待确认=1 个; 无解释=0 个(默认置顶); 反向背离=本页不可用"
                  >
                    归因
                  </th>
                </tr>
              </thead>
              <tbody>
                {overview.isLoading ? (
                  <tr>
                    <td colSpan={TABLE_COL_COUNT} className="px-3 py-10 text-center text-muted">
                      正在计算全市场偏离值…
                    </td>
                  </tr>
                ) : displayRows.length === 0 ? (
                  <tr>
                    <td colSpan={TABLE_COL_COUNT} className="px-3 py-10 text-center text-muted">
                      {view ? (silentOnly ? '当前没有「无解释」的异动' : '当前没有满足条件的标的') : '暂无数据'}
                    </td>
                  </tr>
                ) : (
                  displayRows.map((r, i) => {
                    const entry = attributionBySymbol.get(r.symbol)
                    return (
                      <AbnormalRowView
                        key={r.symbol}
                        row={r}
                        rank={i + 1}
                        attribution={entry}
                        // 数据源挂掉 (未配置/全部失败) 时徽标一律禁用 "—", 不能渲染成「未归因」灰
                        attributionDisabled={attributionHealth !== 'ok'}
                        attributionDisabledReason={
                          attributionHealth === 'config'
                            ? '未配置新闻搜索源 (Anspire Key), 归因不可用'
                            : attributionHealth === 'all-fail'
                              ? `新闻检索全部失败: ${attributionErrorSample?.err ?? '未知错误'}`
                              : attributionHealth === 'paused'
                                ? '监控已暂停, 归因未查询'
                                : null
                        }
                        pending={attribution.isFetching && attributionHealth === 'ok' && entry == null && batchSymbols.includes(r.symbol)}
                        expanded={expandedSymbol === r.symbol}
                        onBadgeClick={() => {
                          // 未归因 → 单条补查; 已归因 → 展开/收起判据
                          if (entry == null) {
                            requestAttribution(r.symbol)
                            setExpandedSymbol(r.symbol)
                          } else {
                            setExpandedSymbol(prev => (prev === r.symbol ? null : r.symbol))
                          }
                        }}
                        onPreview={() => setPreview({ symbol: r.symbol, name: r.name ?? r.symbol })}
                      />
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      </div>
      {preview && (
        <StockPreviewDialog
          symbol={preview.symbol}
          name={preview.name}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  )

  function ruleChips() {
    return (view?.rules ?? FALLBACK_RULES).map((rule, i) => {
      // 对称窗口 (3日) 显示 ±X%; 严重异动窗口正负阈值不同, 显示 +X%/−Y%
      const thr = WINDOW_KEYS.map(w => {
        const t = rule.thresholds[w]
        const s = t.up === t.down ? `±${fmtThreshold(t.up)}` : `+${fmtThreshold(t.up)}/−${fmtThreshold(t.down)}`
        return `${w.replace('d', '日')}${s}`
      }).join(' / ')
      return (
        <div key={i} className="rounded border border-border bg-base px-2.5 py-2">
          <div className="text-[11px] font-medium text-foreground">
            {rule.board}
            {rule.st && <span className="ml-1 text-danger">ST</span>}
          </div>
          <div className="mt-0.5 font-mono text-[10px] text-muted">{thr}</div>
        </div>
      )
    })
  }
}

/** 上次计算时间 (服务端 asof 秒级时间戳 → 本地日期时间) */
function fmtCalcTime(asofSec: number): string {
  const d = new Date(asofSec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtThreshold(v: number | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(0)}%`
}

/** 全窗口里接近度最高的窗口 */
function dominantWindow(r: AbnormalRow): { key: WindowKey; value: number; threshold: number; closeness: number } | undefined {
  let best: { key: WindowKey; value: number; threshold: number; closeness: number } | undefined
  for (const w of WINDOW_KEYS) {
    const info = r.windows[w]
    if (info && (!best || info.closeness > best.closeness)) best = { key: w, ...info }
  }
  return best
}

/** 异动幅度口径 = 三档中最高的接近度 (|偏离值| ÷ 该档阈值), 用于归因优先级排序 */
function closenessOf(r: AbnormalRow): number {
  return dominantWindow(r)?.closeness ?? 0
}

function AbnormalRowView({ row, rank, onPreview, attribution, attributionDisabled, attributionDisabledReason, pending, expanded, onBadgeClick }: {
  row: AbnormalRow
  rank: number
  onPreview: () => void
  /** undefined = 尚未查询 (不在自动归因的前 N 条内) */
  attribution?: { state: AttributionState; item?: NewsBatchStockItem }
  /** 新闻源不可用 (未配置 / 全部失败): 徽标不可点、显示 "—", 不假装是「无解释」 */
  attributionDisabled?: boolean
  /** 数据源不可用的原因 (悬停提示用) */
  attributionDisabledReason?: string | null
  /** 该 symbol 正在查询中 */
  pending?: boolean
  /** 判据(命中新闻)是否已展开 */
  expanded?: boolean
  onBadgeClick: () => void
}) {
  const board = boardTag(row.symbol)
  const dominant = dominantWindow(row)
  const meta = STATUS_META[row.status]
  const state: AttributionState = attribution?.state ?? 'unknown'
  const attrMeta = ATTRIBUTION_META[attributionDisabled ? 'unknown' : state]
  const domainCount = attribution?.item?.hit_domain_count ?? 0
  const badgeTitle = attributionDisabled
    ? attributionDisabledReason ?? '新闻检索不可用, 归因不可用'
    : attribution == null
      ? `${ATTRIBUTION_META.unknown.hint} (不在自动归因的前 ${ATTRIB_TOP_N} 条内)`
      : attribution.item == null
        ? '已提交查询, 等待结果'
        : !attribution.item.success
          ? `归因失败: ${attribution.item.error ?? '未知错误'}`
          : `${attrMeta.hint} · 不同域名 ${domainCount} 个${attribution.item.hit_domains.length > 0 ? `: ${attribution.item.hit_domains.join('、')}` : ''}`
  return (
    <>
    <tr className="group border-b border-border/40 transition-colors last:border-0 hover:bg-elevated/50">
      <td className="px-2 py-1.5 text-right font-mono text-[10px] text-muted/70">{rank}</td>
      <td className="px-2 py-1.5">
        {/* 仅代码/名称可点击打开详情 (与自选列表一致), 其余单元格不可点 */}
        <button
          type="button"
          onClick={onPreview}
          title="查看个股详情"
          className="flex min-w-0 items-center gap-1.5 text-left"
        >
          <span className="shrink-0 font-mono text-xs text-foreground group-hover:text-accent transition-colors duration-150">{row.symbol}</span>
          <span className="min-w-0 max-w-40 truncate text-xs text-secondary group-hover:text-foreground transition-colors duration-150">{row.name ?? '—'}</span>
          {board && (
            <span className={`shrink-0 rounded px-1 text-[9px] font-bold leading-tight border ${board.color}`}>
              {board.label}
            </span>
          )}
          {row.st && (
            <span className="shrink-0 rounded border border-danger/30 bg-danger/10 px-1 text-[9px] font-bold text-danger">
              ST
            </span>
          )}
        </button>
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-xs text-secondary">{fmtPrice(row.close)}</td>
      <td className={`px-2 py-1.5 text-right font-mono text-xs font-medium ${priceColorClass(row.rt_pct)}`}>
        {fmtPct(row.rt_pct)}
      </td>
      {WINDOW_KEYS.map(w => {
        const info = row.windows[w]
        // 接近度取最高档: 来源窗口颜色加重 (加粗), 其余窗口淡化, 以此区分「哪一档」
        const isDominant = dominant?.key === w
        // 后端 threshold 已按偏离方向取对应侧 (严重异动负向阈值更严)
        const sign = info && info.value >= 0 ? '+' : '−'
        return (
          <td key={w} className="px-2 py-1.5 text-right">
            {info ? (
              <span
                className={`font-mono text-xs tabular-nums ${priceColorClass(info.value)} ${isDominant ? 'font-semibold' : 'opacity-45'}`}
                title={`阈值 ${sign}${fmtThreshold(info.threshold)} · 接近度 ${(info.closeness * 100).toFixed(0)}%${isDominant ? ' · 本行接近度来源' : ''}`}
              >
                {fmtPct(info.value)}
                <span className="ml-1 text-[9px] text-muted/80">/{sign}{fmtThreshold(info.threshold)}</span>
              </span>
            ) : (
              <span className="text-muted/40">—</span>
            )}
          </td>
        )
      })}
      <td className="px-2 py-1.5">
        <div
          className="flex items-center gap-1.5"
          title="取 3日/10日/30日 三档中最高的 |偏离值|÷对应档阈值; ≥100% 已触发, ≥70% 边缘, ≥50% 观察"
        >
          <div className="h-1.5 w-20 overflow-hidden rounded-full bg-elevated">
            <div
              className={`h-full rounded-full transition-all ${meta.bar}`}
              style={{ width: `${Math.min(100, (dominant?.closeness ?? 0) * 100)}%}` }}
            />
          </div>
          <span className="font-mono text-[10px] tabular-nums text-secondary">
            {((dominant?.closeness ?? 0) * 100).toFixed(0)}%
          </span>
        </div>
      </td>
      <td className="px-2 py-1.5 text-center">
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${meta.cls}`}>{meta.label}</span>
      </td>
      {/* 归因徽标 (面板 2): 点击 = 未归因时单条补查 / 已归因时展开判据 */}
      <td className="px-2 py-1.5 text-center">
        <button
          type="button"
          onClick={onBadgeClick}
          disabled={attributionDisabled}
          title={badgeTitle}
          className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium transition-opacity ${attrMeta.cls} ${
            attributionDisabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer hover:brightness-110'
          }`}
        >
          {pending && <RefreshCw className="h-2.5 w-2.5 animate-spin" />}
          {pending ? '查询中' : attributionDisabled ? '—' : attrMeta.label}
          {!attributionDisabled && domainCount > 0 && (
            <span className="font-mono opacity-70">{domainCount}</span>
          )}
        </button>
      </td>
    </tr>
    {expanded && (
      <AttributionDetailRow
        item={attribution?.item}
        requested={attribution != null}
        colSpan={TABLE_COL_COUNT}
      />
    )}
    </>
  )
}

/** 表格列数 (归因判据展开行用它做 colSpan) */
const TABLE_COL_COUNT = 10

/** 展开行: 该条异动到底被哪些域名解释了 —— 判据必须可点开 (PRD §3.2 面板 2) */
function AttributionDetailRow({ item, requested, colSpan }: {
  item?: NewsBatchStockItem
  requested: boolean
  colSpan: number
}) {
  return (
    <tr className="border-b border-border/40 bg-elevated/30">
      <td colSpan={colSpan} className="px-3 py-2">
        {!requested ? (
          <span className="text-[10px] text-muted">尚未查询, 点击上方徽标单条归因。</span>
        ) : !item ? (
          <span className="text-[10px] text-muted">已提交查询, 等待结果…</span>
        ) : !item.success ? (
          <span className="text-[10px] text-warning">该条归因失败: {item.error ?? '未知错误'}</span>
        ) : item.hits.length === 0 ? (
          <span className="text-[10px] text-muted">
            窗口内无新闻命中 ({item.result_count} 条) —— 没有任何域名解释这次异动。
          </span>
        ) : (
          <div className="space-y-1">
            <div className="text-[10px] text-muted">
              命中 {item.result_count} 条 · 不同域名 {item.hit_domain_count} 个: {item.hit_domains.join('、')}
              {item.cached && <span className="ml-1 text-muted/70">· 命中后端缓存</span>}
            </div>
            <ul className="space-y-0.5">
              {item.hits.slice(0, 8).map((hit, i) => (
                <li key={`${hit.url}-${i}`} className="truncate text-[10px]">
                  <a
                    href={hit.url}
                    target="_blank"
                    rel="noreferrer"
                    title={hit.snippet || hit.title}
                    className="text-secondary hover:text-accent hover:underline"
                  >
                    <span className="mr-1 rounded bg-elevated px-1 text-[9px] text-muted">{hit.source}</span>
                    {hit.title}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}
      </td>
    </tr>
  )
}

function StatusChip({ label, count, tone }: { label: string; count?: number; tone: 'danger' | 'warning' | 'muted' }) {
  const toneCls =
    tone === 'danger'
      ? 'border-danger/30 bg-danger/8 text-danger'
      : tone === 'warning'
        ? 'border-warning/30 bg-warning/8 text-warning'
        : 'border-border bg-elevated text-secondary'
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] ${toneCls}`}>
      <span className="font-mono text-sm font-semibold tabular-nums">{count ?? '—'}</span>
      {label}
    </span>
  )
}

function SegmentedControl<T extends string>({ value, onChange, options }: {
  value: T
  onChange: (v: T) => void
  options: Array<{ value: T; label: string }>
}) {
  return (
    <div className="inline-flex h-7 overflow-hidden rounded border border-border bg-base">
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`px-2.5 text-[11px] transition-colors ${
            value === o.value ? 'bg-accent/10 text-accent' : 'text-muted hover:text-foreground'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** 后端数据未到时的规则表兜底 (与后端 RULES_META 同步维护)
 *  阈值为 {正, 负} 双侧: 3日对称, 严重异动 10日+100%/−50%、30日+200%/−70% (负向更严)
 *  2026-07-06 起主板风险警示(ST)股票与普通股票同标准 (原±15%特别规定已废止) */
const FALLBACK_RULES: Array<{ board: string; st: boolean; thresholds: Record<string, { up: number; down: number }>; note: string }> = [
  { board: '主板', st: false, thresholds: { '3d': { up: 0.2, down: 0.2 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '创业板/科创板', st: false, thresholds: { '3d': { up: 0.3, down: 0.3 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
  { board: '北交所', st: false, thresholds: { '3d': { up: 0.4, down: 0.4 }, '10d': { up: 1.0, down: 0.5 }, '30d': { up: 2.0, down: 0.7 } }, note: '' },
]
