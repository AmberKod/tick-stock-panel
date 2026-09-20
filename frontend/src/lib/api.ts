// 后端 API 客户端 — 全项目统一入口
//
// Dev: Vite 按启动脚本解析出的 BACKEND_HOST/BACKEND_PORT 代理 /api
// Prod:同源(FastAPI 托管前端 dist)

import { toast } from '@/components/Toast'

const BASE = ''

type RequestOptions = RequestInit & {
  /** 为 true 时不弹错误 toast（由调用方自行汇总提示，如多图串行队列） */
  quiet?: boolean
}

async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { quiet, ...fetchInit } = init ?? {}
  const isFormData = fetchInit.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  // 合并调用方传入的 headers (此前会被整体覆盖丢弃)
  Object.assign(headers, fetchInit.headers as Record<string, string> | undefined)
  const res = await fetch(`${BASE}${path}`, { ...fetchInit, headers })
  if (!res.ok) {
    let detail = ''
    try {
      const j = JSON.parse(await res.text())
      const raw = j.detail ?? j.message ?? ''
      if (Array.isArray(raw)) {
        // FastAPI 422 校验错误: [{type, loc, msg, input}, ...] → 取 msg 拼接
        detail = raw.map((e: any) => e?.msg || String(e)).join('; ')
      } else if (typeof raw === 'string') {
        detail = raw
      } else if (raw && typeof raw === 'object') {
        detail = JSON.stringify(raw)
      }
    } catch { /* ignore */ }
    const msg = detail || `${res.status} ${res.statusText}`
    // 401 (未登录/会话过期) 不弹 toast — 由全局认证拦截器统一跳登录页, 避免刷屏
    if (res.status !== 401 && !quiet) toast(msg, 'error')
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// ===== Capabilities =====
/** 新闻/舆情 — 热点工作区的"为什么涨"那一维 */
export interface NewsItem {
  title: string
  snippet: string
  url: string
  source: string
  published_date: string | null
}

export interface NewsResponse {
  query: string
  results: NewsItem[]
  result_count: number
  provider: string
  success: boolean
  error_message: string | null
  elapsed_s: number
  symbol?: string
  stock_name?: string | null
  topic?: string
}

export interface NewsProviderStatus {
  name: string
  label: string
  configured: boolean
  key_count: number
  masked: string
}

export interface NewsStatus {
  providers: NewsProviderStatus[]
  configured_any: boolean
}

/** 新闻批量归因 — 单个标的的结果 (POST /api/news/batch-stock) */
export interface NewsBatchStockItem {
  symbol: string
  stock_name: string | null
  query: string
  provider: string
  success: boolean
  error: string | null
  result_count: number
  /** 去重后的**域名**列表: 未做出版方家族归一, 只能数"几个不同域名" */
  hit_domains: string[]
  hit_domain_count: number
  hits: NewsItem[]
  cached: boolean
}

export interface NewsBatchStockResult {
  ok: boolean
  /** 后端给的口径提示, UI 应原样展示: 域名 != 独立信源 */
  domain_note: string
  days: number
  max_results: number
  requested: number
  processed: number
  cached_count: number
  error_count: number
  elapsed_s: number
  throttle: {
    serial: boolean
    min_interval_s: number
    max_symbols: number
    time_budget_s: number
  }
  provider: string
  results: Record<string, NewsBatchStockItem>
}

/** 跨市场态势 (GET /api/overview/posture) — 单市场条目
 *
 * 取值与后端 `market_posture.py` 常量严格对齐(不要凭印象改):
 * - posture: POSTURE_{ATTACK,BALANCED,DEFEND,UNKNOWN}
 * - vote:    VOTE_{ATTACK,NEUTRAL,DEFEND,UNAVAILABLE}
 * 收窄成联合类型是为了让拼错在编译期就报错, 而不是运行时静默走 fallback。
 */
export type PostureVerdict = 'attack' | 'balanced' | 'defend' | 'unknown'
export type PostureVote = 'attack' | 'neutral' | 'defend' | 'unavailable'

export interface MarketPostureVote {
  dim: string
  label: string
  vote: PostureVote
  vote_label: string
  detail: string
}

export interface MarketPosture {
  market: string
  market_label: string
  posture: PostureVerdict
  posture_label: string
  votes: MarketPostureVote[]
  /** 不可用的投票维度: **已从票数分母中剔除**, 不得当 0 或防守 */
  unavailable_dims: string[]
  evidence: { dim: string; text: string }[]
  veto: { dim: string; reason: string } | null
  tally: { attack: number; neutral: number; defend: number; counted: number }
  as_of: string | null
  freshness: { regime_as_of: string | null; hotspot_age_hours: number | null }
  source_errors: string[]
}

export interface MarketPostureResult {
  as_of: string | null
  markets: MarketPosture[]
}

/** 通用新闻流(RSS 聚合, 不需要 API Key) */
export interface NewsFeedEntry {
  title: string
  url: string
  source: string
  published_at: string | null
  summary: string
}

export interface NewsCategory {
  key: string
  label: string
  /** rss = 本仓聚合的 RSS 流; search = 走外部检索源(需 Key) */
  kind: 'rss' | 'search' | string
  source_count: number
  sources: string[]
  note: string
}

export interface NewsFeedResult {
  category: string
  label: string
  kind: string
  entries: NewsFeedEntry[]
  entry_count: number
  source_errors: string[]
  source_count: number
  ok_source_count: number
  fetched_at: string
  elapsed_s: number
  cached: boolean
  note: string
  /** 全源都挂掉时为 false —— 前端据此显示"源不可用"而不是"没新闻" */
  success: boolean
}

export interface CapabilityLimits {
  rpm: number | null
  batch: number | null
  subscribe: number | null
}

export interface CapabilitiesResponse {
  label: string
  capabilities: Record<string, CapabilityLimits>
}

// ===== Financials =====
export interface FinancialStatus {
  available: boolean
  tables: Record<string, { rows: number; symbols: number }>
  last_sync: Record<string, string>
  /** 服务端是否正在同步(手动触发)——驱动"同步中"UI 并防重复点击 */
  syncing?: boolean
}

export interface FinancialMetricRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  eps_basic?: number | null
  eps_diluted?: number | null
  bps?: number | null
  ocfps?: number | null
  roe?: number | null
  roe_diluted?: number | null
  roa?: number | null
  gross_margin?: number | null
  net_margin?: number | null
  debt_to_asset_ratio?: number | null
  revenue_yoy?: number | null
  net_income_yoy?: number | null
  operating_cash_to_revenue?: number | null
  inventory_turnover?: number | null
  [key: string]: any
}

export interface FinancialIncomeRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  revenue?: number | null
  operating_cost?: number | null
  operating_profit?: number | null
  total_profit?: number | null
  net_income?: number | null
  net_income_attributable?: number | null
  basic_eps?: number | null
  diluted_eps?: number | null
  [key: string]: any
}

export interface FinancialBalanceSheetRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  total_assets?: number | null
  total_current_assets?: number | null
  cash_and_equivalents?: number | null
  total_liabilities?: number | null
  total_equity?: number | null
  equity_attributable?: number | null
  [key: string]: any
}

export interface FinancialCashFlowRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  net_operating_cash_flow?: number | null
  net_investing_cash_flow?: number | null
  net_financing_cash_flow?: number | null
  capex?: number | null
  net_cash_change?: number | null
  [key: string]: any
}

export interface FinancialSharesRecord {
  symbol?: string
  period_end: string
  announce_date?: string | null
  total_shares?: number | null
  float_shares?: number | null
  [key: string]: any
}

/** AI 财务分析历史报告 */
export interface AiFinancialReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  periods?: number
  summary?: string
  created_at: string
}

// ===== 个股分析 =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3。前端按"显示到第几档"过滤。 */
  rank?: number
}

/** 带状曲线指标(布林带/Keltner/ATR)的每日时间序列,与 dates 对齐。 */
export interface LevelSeries {
  boll?: { upper: (number | null)[]; lower: (number | null)[]; mid?: (number | null)[] }
  keltner_s?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_m?: { upper: (number | null)[]; lower: (number | null)[] }
  keltner_l?: { upper: (number | null)[]; lower: (number | null)[] }
  atr?: { stop_loss: (number | null)[]; take_profit: (number | null)[] }
}

export interface StockLevels {
  levels: Record<LevelType, PriceLevel[]>
  close: number | null
  summary: string
  symbol: string
  /** dates 与 series 对齐;前端按自身 rows 的日期映射,缺失填 null */
  dates?: string[]
  series?: LevelSeries
}

export interface AiStockReport {
  id: string
  symbol: string
  name: string
  focus: string
  content: string
  summary?: string
  close?: number | null
  levels?: Record<LevelType, PriceLevel[]>
  created_at: string
}

// ===== Kline =====
export interface MinuteKlineRow {
  datetime: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface MinuteKlineSession {
  date: string
  prev_close: number | null
  rows: MinuteKlineRow[]
}

export interface PriceLimitInfo {
  rate: number
  limit_up: number | null
  limit_down: number | null
  source: 'rule' | 'instrument'
}

export interface KlineRow {
  symbol?: string
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  change_pct?: number
  ma5?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_14?: number | null
  vol_ratio_5d?: number | null
  [key: string]: any
}

// ===== Watchlist =====
export interface WatchlistEntry {
  symbol: string
  added_at: string
  note?: string
  name?: string | null
  /** 所属分组 id 列表 (同一标的可属于多个分组; 空数组=未分组) */
  group_ids?: string[]
  /** 市场 (P1 跨市场自选): cn / hk / us — 由 symbol 后缀推导 */
  market?: 'cn' | 'hk' | 'us'
}

export type WatchlistGroupColor =
  | 'sky'
  | 'blue'
  | 'indigo'
  | 'violet'
  | 'fuchsia'
  | 'rose'
  | 'orange'
  | 'amber'
  | 'lime'
  | 'emerald'
  | 'teal'
  | 'cyan'

export interface WatchlistGroup {
  id: string
  name: string
  color: WatchlistGroupColor
}

export interface WatchlistImportCandidate {
  code: string
  symbol: string | null
  name: string | null
  matched: boolean
  already_in_watchlist: boolean
}

export interface WatchlistImportResult {
  provider: string
  codes: string[]
  candidates: WatchlistImportCandidate[]
  matched_count: number
  unmatched_count: number
}

export interface Quote {
  symbol: string
  price?: number
  pct?: number
  close?: number
  change_pct?: number
  [key: string]: any
}

export interface IndexInstrument {
  symbol: string
  name?: string | null
  code?: string | null
  asset_type?: 'index'
  [key: string]: any
}

export interface IndexQuote {
  symbol: string
  name?: string | null
  last_price?: number | null
  close?: number | null
  prev_close?: number | null
  change_pct?: number | null
  change_amount?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  volume?: number | null
  amount?: number | null
  timestamp?: number | null
  [key: string]: any
}

// ===== Screener =====
export interface ScreenerStrategy {
  id: string
  name: string
  description: string
  source?: string
}

export interface StrategyLoadError {
  file: string
  error: string
}

export interface ScreenerResult {
  as_of: string
  strategy: string | null
  rows: any[]
  total: number
  elapsed_ms: number
  warnings?: string[]
  concept_heat_metadata?: ConceptHeatMetadata | null
}

export type InternationalMarket = 'hk' | 'us'
export type StrategyBacktestAsset = 'stock' | 'etf' | 'hk' | 'us'
export type ScoringContext = 'current' | 'historical'

export interface ConceptHeatMetadata {
  source_id?: 'ext_gn_ths'
  market?: 'cn' | 'etf' | 'hk' | 'us'
  mapping_version?: string | null
  mapping_updated_at?: string | null
  quote_date?: string | null
  current_market_date?: string
  aggregation?: 'mean'
  min_members?: number
  status?: 'available' | 'partial' | 'unavailable'
  reason?: string | null
  reason_code?: string | null
  input_symbols?: number
  mapped_symbols?: number
  computable_symbols?: number
  unmapped_symbols?: number
  missing_valid_concept_symbols?: number
  valid_concepts?: number
  config_fingerprint?: string
  children?: Record<string, ConceptHeatMetadata>
}

export interface MarketDataCoverage {
  symbols: number
  target_symbols: number
  extra_symbols: number
  missing_symbols: number
  rows: number
  target_rows: number
  first_date: string | null
  last_date: string | null
  target_last_date: string | null
}

export interface MarketDataSource {
  id: string
  label: string
  available: boolean
  reason: string | null
}

export interface MarketFinancialCoverage {
  status: 'available' | 'partial' | 'unavailable'
  reason: string | null
  sources: string[]
  rows: number
  symbols: number
  first_period_end: string | null
  last_period_end: string | null
  first_announce_date: string | null
  last_announce_date: string | null
  fields: Record<string, {
    available_symbols: number
    missing_symbols: number
    first_announce_date: string | null
    last_announce_date: string | null
  }>
}

export interface MarketPriceAudit {
  status: 'verified' | 'partial' | 'unknown'
  verified_symbols: number
  unknown_symbols: number
  mixed_basis_symbols: number
  adjustment_sources: string[]
  last_checked_at: string | null
  warnings: string[]
}

const MARKET_DATA_SOURCE_LABELS: Record<string, string> = {
  sina: '新浪财经',
  sina_hk: '新浪财经',
  sina_hk_daily: '新浪财经',
  sina_hk_qfq: '新浪财经复权资料',
  sina_hk_adj_factor: '新浪财经复权资料',
  tencent: '腾讯证券',
  tencent_hk: '腾讯证券',
  tencent_hk_daily: '腾讯证券',
  tencent_hk_qfq: '腾讯证券复权资料',
  hk_daily: '港股日线',
  hk_quickquote: '港股实时行情',
  hk_financial: '港股历史财务',
  hkex: '香港交易所',
  hkex_instruments: '香港交易所证券资料',
  hkex_list_of_securities: '香港交易所证券资料',
  eastmoney: '东方财富',
  eastmoney_hk: '东方财富港股公告',
  eastmoney_hk_announcement: '东方财富港股公告',
  eastmoney_hk_announcements: '东方财富港股公告',
  eastmoney_hk_financial: '东方财富港股财务',
  eastmoney_hk_daily_check: '东方财富港股原始日线核验',
  local: '本地历史记录',
  local_history: '本地历史记录',
  tickflow: 'TickFlow',
  yahoo: '雅虎财经',
  yfinance: '雅虎财经',
}

export function marketDataSourceLabel(source: string | null | undefined): string {
  if (!source) return '来源未记录'
  const parts = source.split(/[+,]/).map((part) => part.trim()).filter(Boolean)
  if (parts.length > 1) return [...new Set(parts.map(marketDataSourceLabel))].join('、')
  return MARKET_DATA_SOURCE_LABELS[source] ?? (/\p{Script=Han}/u.test(source) ? source : '其他数据来源')
}

const MARKET_FINANCIAL_FIELD_LABELS: Record<string, string> = {
  eps_ttm: '每股收益（过去十二个月）',
  bps: '每股净资产',
  roe: '净资产收益率',
  gross_margin: '毛利率',
  net_margin: '净利率',
  revenue_yoy: '营业收入同比',
  net_income_yoy: '净利润同比',
  debt_to_asset_ratio: '资产负债率',
  total_shares: '总股本',
  float_shares: '流通股本',
  pe_ttm: '市盈率（过去十二个月）',
  pb: '市净率',
  raw_pb: '市净率（原始价）',
  turnover_rate: '换手率',
}

export function marketFinancialFieldLabel(field: string): string {
  return MARKET_FINANCIAL_FIELD_LABELS[field] ?? '其他财务指标'
}

export function marketPriceBasisLabel(basis: string | null | undefined): string {
  if (basis === 'forward_adjusted' || basis === 'forward_adjusted_daily_ohlc' || basis === 'qfq') return '前复权研究价格'
  if (basis === 'unadjusted' || basis === 'raw') return '不复权原始价格'
  if (basis === 'backward_adjusted' || basis === 'hfq') return '后复权价格'
  return '价格口径未核实'
}

export interface MarketDataStatusResponse {
  market: 'HK' | 'US'
  checked_at: string
  currency: 'HKD' | 'USD'
  source: string[]
  data_generation: string
  instruments: {
    symbols: number
    lot_size_available: number
    lot_size_missing: number
    verified_not_applicable?: number
    currencies?: { HKD: number; CNY: number; USD: number; unknown: number }
    lot_size_future?: number
    lot_size_conflicts?: number
    lot_size_as_of?: string | null
  }
  daily: MarketDataCoverage
  enriched: MarketDataCoverage
  missing_fields: Record<'daily' | 'enriched', Record<string, { missing_rows: number; unknown_rows: number }>>
  capabilities: {
    daily_download: boolean
    daily_provider: string
    daily_download_reason: string | null
    recompute_enriched: boolean
    lot_size_sync: boolean
    financial_history_sync?: boolean
    financial_history_reason?: string | null
  }
  daily_sources?: (MarketDataSource & { role: 'primary' | 'fallback' })[]
  adjustment_sources?: MarketDataSource[]
  verification_sources?: MarketDataSource[]
  financials?: MarketFinancialCoverage | null
  price_audit?: MarketPriceAudit | null
  warnings: string[]
}

export interface MarketDataSyncItem {
  symbol: string
  status: string
  reason: string | null
  applicability?: string | null
  reason_code?: string | null
  source?: string | null
  fallback_used?: boolean
  attempted_sources?: string[]
  requested_start?: string | null
  requested_end?: string | null
  actual_start?: string | null
  actual_end?: string | null
  currency?: string | null
  volume_unit?: string | null
  price_adjustment?: string | null
  adjustment_source?: string | null
  adjustment_version?: string | null
  verification_source?: string | null
  verification_cached?: boolean
  source_conflicts?: {
    date: string
    primary?: Record<string, number>
    fallback?: Record<string, number>
    verification?: Record<string, number>
    verification_source?: string
    selected_source?: string
    source_url?: string
    observed_at?: string
    response_sha256?: string
    verification_cached?: boolean
  }[]
  raw_updated?: boolean
  enriched_updated?: boolean
  source_as_of?: string | null
  observed_at?: string | null
  fields_available?: string[]
  fields_missing?: string[]
}

export interface MarketDataSyncResult {
  operation: 'daily_download' | 'enriched_recompute' | 'lot_size_sync' | 'financial_sync'
  market: 'HK' | 'US'
  status: 'started' | 'completed' | 'completed_with_errors' | 'empty' | 'unsupported' | 'failed' | 'unchanged'
  requested: number
  succeeded: number
  failed: number
  skipped: number
  unchanged?: number
  verified_not_applicable?: number
  enriched_dates_written: number
  data_generation: string
  failures: { symbol: string; reason: string }[]
  items?: MarketDataSyncItem[]
  job_id?: string
  message?: string
  source?: string
  as_of?: string
}

export interface ScreenerResultSummary {
  total: number
  as_of: string
  warnings?: string[]
  concept_heat_metadata?: ConceptHeatMetadata | null
}

export interface ScreenerCachedSummary {
  as_of: string | null
  results: Record<string, ScreenerResultSummary>
  today_ever_counts: Record<string, number>
  updated_at: number | null
}

export interface ScreenerCachedResult {
  result: ScreenerResult | null
  today_ever_rows: Record<string, any> | null
  strategy_ids_by_symbol: Record<string, string[]>
  updated_at: number | null
}

export interface MarketSnapshotRow {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  amount?: number | null
  volume?: number | null
  turnover_rate?: number | null
  vol_ratio_5d?: number | null
  total_shares?: number | null
  float_shares?: number | null
  market_cap?: number | null
  float_market_cap?: number | null
  consecutive_limit_ups?: number | null
  [key: string]: any
}

export interface OverviewDimensionRankItem {
  name: string
  count: number
  avg_pct: number
  up_count: number
  down_count: number
  amount: number
  leader?: {
    symbol?: string | null
    name?: string | null
    change_pct?: number | null
  } | null
}

export interface OverviewMarket {
  as_of: string | null
  quote_status: {
    enabled?: boolean
    running?: boolean
    quote_age_ms?: number | null
    is_trading_hours?: boolean
    [key: string]: any
  }
  indices: IndexQuote[]
  breadth: {
    total: number
    up: number
    down: number
    flat: number
    up_pct: number
    down_pct: number
    avg_pct?: number | null
    median_pct?: number | null
    strong_up?: number
    strong_down?: number
  }
  amount: { total: number; avg: number }
  boards: { board: string; count: number; up: number; down: number; up_pct: number; amount: number }[]
  limit: { limit_up: number; broken: number; failed: number; limit_down: number; max_boards: number; seal_rate?: number; tiers: { boards: number; count: number; stocks?: { symbol: string; name?: string; amount?: number }[] }[]; sealed_ready?: boolean; fake_up?: number; fake_down?: number }
  distribution: { label: string; count: number; pct: number }[]
  trend: { above_ma5: number; above_ma20: number; above_ma60: number; above_ma5_pct: number; above_ma20_pct: number; above_ma60_pct: number; new_high: number; new_low: number }
  activity: { avg_turnover: number; high_turnover: number; high_vol_ratio: number; vol_ratio: number }
  radar: { key: string; label: string; value: number }[]
  emotion: { score: number; label: string }
  top_gainers: MarketSnapshotRow[]
  top_losers: MarketSnapshotRow[]
  turnover_leaders: MarketSnapshotRow[]
  active_leaders: MarketSnapshotRow[]
  concept_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
  industry_rank: { leading: OverviewDimensionRankItem[]; lagging: OverviewDimensionRankItem[] }
}

// ===== 概念涨幅轮动矩阵 =====
// dates: 日期字符串列表(最新在最前); columns: {日期: [[概念名, 涨幅小数], ...]} 每列各自降序
export interface RpsRotationData {
  dates: string[]
  columns: Record<string, [string, number][]>
  concept_count: number
}

// ===== 市场环境(Regime) =====

/**
 * 市场标识 — 与后端 regime / strength_ladder API 的 market 查询参数一致。
 * cn = A 股, hk = 港股, us = 美股。默认 cn 保持老调用零回归。
 */
export type MarketCode = 'cn' | 'hk' | 'us'

export type RegimeState = 'strong' | 'lean_strong' | 'range' | 'lean_weak' | 'weak'

export const REGIME_STATE_LABELS: Record<RegimeState, string> = {
  strong: '强势',
  lean_strong: '偏强',
  range: '震荡',
  lean_weak: '偏弱',
  weak: '弱势',
}

export const REGIME_STATE_COLORS: Record<RegimeState, string> = {
  strong: '#ef4444',      // 红(强)
  lean_strong: '#f97316', // 橙
  range: '#6b7280',       // 灰
  lean_weak: '#3b82f6',   // 蓝
  weak: '#10b981',        // 绿(弱)
}

export interface RegimeRow {
  date: string
  state: RegimeState
  score: number
  limit_up: number
  limit_down: number
  broken_limit: number
  max_consecutive: number
  seal_rate: number
  up_count: number
  down_count: number
  up_ratio: number
  index_pct: number
  above_ma20_pct: number
  total_amount: number
  avg_turnover: number
  // 4 个子维度分(0-100, 重算后才有; 旧数据可能缺) — 综合分的加权来源
  avg_pct?: number
  median_pct?: number
  strong_up_pct?: number
  strong_down_pct?: number
  profit_score?: number
  speculation_score?: number
  resilience_score?: number
  trend_score?: number
  // 情绪周期阶段与梯队指标(重算后才有; 旧数据可能缺)
  phase?: MarketPhase | null
  first_board?: number | null
  ge2_count?: number | null
  ge3_count?: number | null
  ge5_count?: number | null
  ladder_completeness?: number | null
  promo_rate?: number | null
  promo_pool?: number | null
}

export interface RegimeHistory {
  rows: RegimeRow[]
  total: number
}

export interface RegimeStateItem {
  state: RegimeState
  label: string
  count: number
  pct: number
}

export interface RegimeStates {
  distribution: RegimeStateItem[]
  days: number
}

export interface RegimeCoverage {
  rows: number
  earliest_date: string | null
  latest_date: string | null
}

// ── 强度梯队(动量档位) ──
// 港美无涨跌停/连板制度, 用 20 日动量档位替代 A 股连板层级:
// m25 (>=25%) / m15 (>=15%) / m8 (>=8%) / m3 (>=3%), 动量 <3% 不入档。
// 后端 market=cn 会返回 400 (A 股走连板梯队, 由 market_phase + monitor 协同)。
export type StrengthBand = 'm25' | 'm15' | 'm8' | 'm3'

export const STRENGTH_BANDS: StrengthBand[] = ['m25', 'm15', 'm8', 'm3']

/** 档位展示元信息 — 名称与配色按"动量强度"递减 */
export const STRENGTH_BAND_META: Record<StrengthBand, { label: string; color: string; desc: string }> = {
  m25: { label: '强动量 ≥25%', color: '#dc2626', desc: '20 日动量 ≥ 25%' },
  m15: { label: '中强 15~25%', color: '#ea580c', desc: '20 日动量 15% ~ 25%' },
  m8:  { label: '温和 8~15%', color: '#d97706', desc: '20 日动量 8% ~ 15%' },
  m3:  { label: '弱动量 3~8%', color: '#0891b2', desc: '20 日动量 3% ~ 8%' },
}

export interface StrengthLadderRow {
  date: string
  market: string
  band: StrengthBand
  symbol: string
  momentum_20d: number
  last_close: number | null
  amount: number | null
}

export interface StrengthLadderResult {
  market: string
  date: string | null
  bands: Partial<Record<StrengthBand, StrengthLadderRow[]>>
  total_count: number
}

// ── 市场阶段(情绪周期) 与 主线 ──
export type MarketPhase = 'ice' | 'ignite' | 'rally' | 'climax' | 'ebb' | 'repair'

export const MARKET_PHASE_LABELS: Record<MarketPhase, string> = {
  ice: '冰点',
  ignite: '启动',
  rally: '主升',
  climax: '高潮',
  ebb: '退潮',
  repair: '修复',
}

export const MARKET_PHASE_COLORS: Record<MarketPhase, string> = {
  ice: '#38bdf8',     // 天蓝(冻结)
  ignite: '#f59e0b',  // 琥珀(升温)
  rally: '#ef4444',   // 红(主升)
  climax: '#d946ef',  // 品红(极端)
  ebb: '#14b8a6',     // 青(退潮)
  repair: '#94a3b8',  // 灰(修复)
}

export const MARKET_PHASE_ORDER: MarketPhase[] = ['ice', 'ignite', 'rally', 'climax', 'ebb', 'repair']

export interface MainlineMemberStat {
  member: string
  top5_days: number
  score_sum: number
  max_boards: number
  leader_symbol: string
}

export interface PhaseSegment {
  phase: MarketPhase
  label: string
  start: string
  end: string
  days: number
  avg_height: number
  avg_first_board: number
  avg_ge2: number
  avg_promo: number | null
  avg_seal_rate: number
  top_mainlines: MainlineMemberStat[]
}

export interface PhaseSegments {
  segments: PhaseSegment[]
  total: number
}

export interface MainlineRow {
  date: string
  kind: string
  member: string
  limit_up_count: number
  ge2_count: number
  max_boards: number
  boards_sum: number
  rungs_filled: number
  leader_symbol: string
  score: number
  rank: number
}

export interface MainlineLeader {
  member: string
  top1_days: number
  avg_score: number
  max_boards: number
}

export interface MainlineFilter {
  min_members: number
  max_members: number
  blacklist: string[]
  exclude_st: boolean
}

export interface MainlineResult {
  rows: MainlineRow[]
  leaders: MainlineLeader[]
  membership_note: string
  filter: MainlineFilter
}

// ===== 大盘复盘 =====
export interface AiReviewReport {
  id: string
  as_of: string
  focus?: string
  content: string
  summary?: string
  emotion_score?: number | null
  emotion_label?: string
  created_at: string
}

// ===== Strategy Engine =====
export interface StrategyParamDef {
  id: string
  label: string
  type: 'float' | 'int' | 'select' | 'bool'
  default: number | string | boolean
  min?: number
  max?: number
  step?: number
  options?: string[]
}

export interface CompositeChildInfo {
  id: string
  name: string
  source: string
  weight: number
}

export interface StrategyDetail {
  id: string
  name: string
  description: string
  tags: string[]
  source: 'builtin' | 'custom' | 'ai' | 'composite'
  execution_backend: 'polars_expr' | 'matrix_native' | 'python_history_legacy' | 'composite'
  asset_types: string[]
  timeframes: string[]
  version: string
  basic_filter: Record<string, any>
  portfolio?: {
    enabled?: boolean
    max_same_industry?: number
    concentration_penalty?: number
    industry_level?: 1 | 2 | 3
  } | null
  params: StrategyParamDef[]
  params_defaults: Record<string, any>
  scoring: Record<string, number>
  scoring_directions: Record<string, ScoringDirection>
  entry_signals: string[]
  exit_signals: string[]
  minute_exit_trigger_supported_signals: string[]
  stop_loss: number | null
  take_profit: number | null
  trailing_stop: number | null
  trailing_take_profit_activate: number | null
  trailing_take_profit_drawdown: number | null
  max_hold_days: number | null
  display_limit?: number
  order_by: string
  descending: boolean
  limit: number
  // 叠加策略(composite)专属: 子策略列表与合并模式。非 composite 时为 null。
  composite_children?: CompositeChildInfo[] | null
}

export type ScoringDirection = 'high' | 'low'

export interface StrategyBuildResult {
  code: string
  meta: Record<string, any>
  valid: boolean
  error: string | null
}

export type StrategyBuildStreamEvent =
  | { type: 'meta'; strategy_id?: string; step?: number }
  | { type: 'delta'; content: string }
  | ({ type: 'result' } & StrategyBuildResult)
  | { type: 'error'; message: string }

export interface StrategyCodeSaveResult {
  ok: boolean
  strategy_id: string
  source: 'ai' | 'custom' | 'composite'
  path: string
  meta: Record<string, any>
}

// ===== Custom Signals (自定义信号) =====
export interface CustomSignalCondition {
  left: string     // 字段名
  op: string       // > >= < <= == !=
  right: string    // "field:xxx" 或数字字符串
  leftDays?: number   // 左字段取几日前 (0=当日, 默认)
  rightDays?: number  // 右字段取几日前 (仅 right 为字段时有意义)
}

export interface CustomSignal {
  id: string
  name: string
  kind: 'entry' | 'exit' | 'both'
  conditions: CustomSignalCondition[]
  enabled: boolean
}

export interface CustomSignalFieldGroup {
  key: string
  label: string
  fields: { key: string; label: string }[]
}

export interface CustomSignalOptions {
  fields: { key: string; label: string }[]
  groups?: CustomSignalFieldGroup[]
  maxDays?: number
  operators: string[]
  kinds: { key: string; label: string }[]
}

export interface CustomSignalAIGenerateResult {
  name: string
  conditions: CustomSignalCondition[]
}

// ===== Monitor (监控规则 + 触发记录) =====
export interface MonitorCondition {
  field: string
  op: string              // truth | > >= < <= == !=
  value?: number | null   // op 非 truth 时必填
}

export type StrategyNotifyEvent = 'buy_signal' | 'sell_signal' | 'pool_entry' | 'pool_exit'

export type SectorKind = 'index' | 'concept' | 'industry'

export interface SectorMonitorTarget {
  key: string
  kind: SectorKind
  name: string
  symbol?: string
  source_id?: string
  field?: string
  source_field?: string
  value?: string
  level?: number | null
  available: boolean
  member_count: number
}

export interface AbnormalWindowInfo {
  /** 实时偏离值 (小数) */
  value: number
  /** 该窗口阈值 (小数) — 后端已按偏离方向取对应侧 (严重异动负向更严) */
  threshold: number
  /** 接近度 |value|/threshold */
  closeness: number
}

export type AbnormalStatus = 'triggered' | 'edge' | 'watch'

export interface AbnormalRow {
  symbol: string
  name: string | null
  board: string
  st: boolean
  close: number | null
  rt_pct: number | null
  windows: Record<string, AbnormalWindowInfo>
  max_closeness: number
  status: AbnormalStatus
}

export interface AbnormalOverview {
  asof: number
  cache_date: string | null
  bench_rt_pct: number
  includes_today: boolean
  rules: Array<{
    board: string
    st: boolean
    /** 各窗口双侧阈值 {up: 正向, down: 负向} (小数) */
    thresholds: Record<string, { up: number; down: number }>
    note: string
  }>
  counts: { triggered: number; edge: number; watch: number }
  rows: AbnormalRow[]
}

/** 港美异动总览 (动量口径) — rows/status 与 A股同 schema, 但无指数基准字段 */
export interface AbnormalHkUsOverview {
  asof: number
  as_of: string | null
  market: 'HK' | 'US'
  rules: Array<{
    board: string
    st: boolean
    thresholds: Record<string, { up: number; down: number }>
    note: string
  }>
  counts: { triggered: number; edge: number; watch: number }
  rows: AbnormalRow[]
}

export interface MonitorRule {
  id: string
  name: string
  enabled: boolean
  type: 'strategy' | 'signal' | 'price' | 'market' | 'ladder' | 'sector' | 'abnormal'
  asset_type?: 'stock' | 'etf' | 'index'
  scope: 'symbols' | 'all' | 'sector' | 'watchlist_group'
  symbols: string[]
  /** scope=watchlist_group 时绑定的自选分组 id (成员动态解析, 增删自选自动生效) */
  group_id?: string | null
  sector?: string | null
  sector_kind?: SectorKind | null
  sector_targets?: SectorMonitorTarget[]
  sector_trigger?: 'change_pct' | 'momentum'
  threshold_pct?: number
  window_minutes?: 1 | 3 | 5 | 10 | 15
  /** abnormal 专属: 关注窗口 (any=全部) */
  abnormal_window?: 'any' | '3d' | '10d' | '30d'
  strategy_id?: string | null
  direction: 'entry' | 'exit' | 'both' | 'up' | 'down'
  notify_events?: StrategyNotifyEvent[]
  score_min?: number | null
  score_max?: number | null
  conditions: MonitorCondition[]
  logic: 'and' | 'or'
  cooldown_seconds: number
  severity: 'info' | 'warn' | 'critical'
  message: string
  webhook_url?: string
  webhook_enabled?: boolean  // 兼容老规则, 已由 webhook_channels 取代
  webhook_channels?: string[]  // 命中时推送的外部渠道 (合法值 'feishu' | 'wecom')
  created_at?: string
  runtime_warning?: string
  // ladder 专属: 封单监控
  metric?: 'sealed_vol' | 'sealed_amount'  // 量(手) / 额(元)
  threshold?: number                        // 封单 <= 此值时报警
}

export interface MonitorRuleOptions {
  threshold_fields: { key: string; label: string }[]
  builtin_signals: { key: string; label: string }[]
  custom_signals: { key: string; label: string }[]
  operators: string[]
  types: { key: string; label: string }[]
  scopes: { key: string; label: string }[]
  logics: { key: string; label: string }[]
  severities: { key: string; label: string }[]
  directions: { key: string; label: string }[]
  intraday_signal_support: {
    available: boolean
    source: string | null
    max_symbols: number
    reason: string
  }
  sector_targets: Record<SectorKind, SectorMonitorTarget[]>
}

export interface AlertEvent {
  ts: number
  rule_id?: string
  rule_name?: string
  source: string
  type: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  severity?: string
  strategy_id?: string
  conditions?: MonitorCondition[]
  logic?: 'and' | 'or'
  sector_kind?: SectorKind
  sector_key?: string
  sector_name?: string
  sector_source_field?: string
  sector_value?: string
  sector_level?: number | null
  window_change_pct?: number | null
  coverage_ratio?: number
  valid_count?: number
  total_count?: number
  up_count?: number
  down_count?: number
  leader?: { symbol?: string; name?: string; change_pct?: number } | null
  /** 异动边缘告警 (source=abnormal) 附加字段 */
  abnormal_window?: string
  abnormal_value?: number
  abnormal_threshold?: number
  abnormal_closeness?: number
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

/** 生成监控规则 id (时间戳 + 随机后缀), 用户无需手动填写。 */
export function genRuleId(): string {
  const ts = Date.now().toString(36)
  const rand = Math.random().toString(36).slice(2, 6)
  return `mr_${ts}_${rand}`
}

// ===== Limit Ladder =====
export interface LimitLadderStock {
  symbol: string
  name?: string | null
  close?: number | null
  change_pct?: number | null
  consecutive_limit_ups?: number | null
  consecutive_limit_downs?: number | null
  status?: 'limit_up' | 'broken' | 'failed' | 'limit_down' | 'recovery' | null
  /** 五档 sealed: real=真封板, fake=假涨停(已归炸板), pending=待确认, null=降级/无能力 */
  sealed_status?: 'real' | 'fake' | 'pending' | null
  /** 封单量(买一/卖一量), 仅真封板有值 */
  sealed_vol?: number | null
  /** 最终状态为涨跌停且当天开高低收四价相同 */
  is_one_word?: boolean
}

export interface LimitLadderTier {
  boards: number
  count: number
  stocks: LimitLadderStock[]
}

export interface LimitLadderResult {
  as_of: string
  tiers: LimitLadderTier[]
  /** 双方向涨跌停计数(修正后, 不论当前 direction) */
  counts?: { up: number; down: number }
  /** 双方向涨跌停原始计数(修正前, 供弹窗对比) */
  counts_raw?: { up: number; down: number }
  /** sealed 数据是否就绪(false→前端显示降级标识) */
  sealed_ready?: boolean
  /** sealed 数据 age(秒), null=盘后定版或无数据 */
  sealed_age?: number | null
  /** sealed 修正统计: real=真封板, fake=假涨停(归炸板), pending=待确认 */
  sealed_counts?: { real: number; fake: number; pending: number }
  /** 涨停侧 sealed 明细 */
  sealed_counts_up?: { real: number; fake: number; pending: number }
  /** 跌停侧 sealed 明细 */
  sealed_counts_down?: { real: number; fake: number; pending: number }
}

// ===== Backtest =====
export interface BacktestResult {
  run_id: string
  config: any
  stats: Record<string, any>
  equity_curve: { date: string; value: number }[]
  trades: any[]
  per_symbol_stats: { symbol: string; total_return: number }[]
}

// ===== Factor Backtest =====
export interface FactorColumn {
  id: string
  label: string
  group: string
  desc: string
}

export interface ScoringColumn extends FactorColumn {
  available?: boolean
  reason?: string
  metadata?: ConceptHeatMetadata
}

export interface GroupStat {
  group: number
  label: string
  total_return: number
  annual_return: number
  max_drawdown: number
  sharpe: number
  win_rate: number
}

export interface FactorBacktestResult {
  run_id: string
  config: Record<string, any>
  ic_mean: number | null
  ic_std: number | null
  ir: number | null
  ic_win_rate: number | null
  ic_series: { date: string; ic: number }[]
  group_stats: GroupStat[]
  group_nav: Record<string, any>[]
  long_short_stats: Record<string, any>
  long_short_nav: { date: string; value: number }[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

export interface FactorBatchItem {
  factor_name: string
  label: string
  group: string
  ic_mean: number | null
  ir: number | null
  ic_win_rate: number | null
  long_short_return: number | null
  long_short_max_drawdown: number | null
  n_symbols: number
  n_dates: number
  elapsed_ms: number
  error: string | null
}

export interface FactorBatchResult {
  run_id: string
  config: Record<string, any>
  results: FactorBatchItem[]
  elapsed_ms: number
  n_symbols: number
  n_dates: number
  error: string | null
}

// ===== Factor / strategy mining =====
export type MiningBudgetProfile = 'exploratory' | 'balanced' | 'strict'
export type MiningRunStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'succeeded'
  | 'succeeded_with_budget_exhausted'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'skipped_prerequisite'

export interface MiningAvailability {
  asset_type: 'stock' | 'etf'
  budget_profile: MiningBudgetProfile
  trading_bars: number
  required_bars: number
  outer_folds: number
  required_outer_folds: number
  eligible: boolean
  available_start: string | null
  available_end: string | null
  effective_start: string | null
  effective_end: string | null
  suggested_start: string | null
}

export interface MiningRequestV1 {
  factor_names: string[]
  strategy_ids?: string[]
  symbols?: string[] | null
  asset_type?: 'stock' | 'etf'
  start?: string | null
  end?: string | null
  budget_profile?: MiningBudgetProfile
  commission_pct?: number
  stamp_tax_pct?: number
  slippage_bps?: number
  correlation_threshold?: number
  max_combination_factors?: number
  beam_width?: number
  max_finalists?: number
  force?: boolean
}

export interface MiningRunProgress {
  phase: string
  label?: string
  done?: number
  total?: number
  percent?: number
  elapsed_ms?: number
  message?: string
}

export interface MiningRun {
  run_id: string
  signature: string
  status: MiningRunStatus
  request: MiningRequestV1
  source?: 'manual' | 'scheduled'
  created_at: string
  updated_at: string
  started_at?: string | null
  finished_at?: string | null
  data_as_of?: string | null
  progress?: MiningRunProgress | null
  error?: string | null
  reused?: boolean
  summary?: MiningResultSummary | null
}

export interface MiningResultSummary {
  factor_count: number
  selected_factor_count: number
  candidate_count: number
  valid_fold_count: number
  skipped_fold_count: number
  confidence: 'low' | 'standard' | 'high'
  budget_exhausted?: boolean
  elapsed_ms?: number
  peak_rss_bytes?: number
}

export interface MiningFactorRow {
  factor_name: string
  label?: string
  direction: 1 | -1
  score: number | null
  ic_mean: number | null
  ir: number | null
  coverage: number | null
  turnover: number | null
  spread_return?: number | null
  spread_sharpe?: number | null
  selected: boolean
  excluded_reason?: string | null
}

export interface MiningRegimeRow {
  state: 'overall' | 'strong' | 'range' | 'weak' | string
  label: string
  n_dates: number
  total_return: number | null
  sharpe: number | null
  max_drawdown: number | null
}

export interface MiningFoldRow {
  fold: number
  label?: string
  train_start?: string
  train_end?: string
  test_start?: string
  test_end?: string
  selected_factors?: string[]
  total_return: number | null
  sharpe: number | null
  max_drawdown?: number | null
  n_trades?: number | null
  skipped?: boolean
  reason?: string | null
  evaluation_kind?: 'selected' | 'cross' | 'benchmark' | null
}

export interface MiningCandidateGate {
  qualified: boolean
  reasons: string[]
}

export interface MiningCandidateRow {
  signature: string
  name: string
  kind: 'factor_combination' | 'existing_strategy'
  factor_names?: string[]
  strategy_id?: string | null
  regime_state?: string | null
  score: number | null
  oos_return: number | null
  oos_sharpe: number | null
  oos_max_drawdown: number | null
  oos_positive_fold_ratio: number | null
  oos_n_trades: number | null
  confidence: 'low' | 'standard' | 'high'
  valid_folds?: number | null
  skipped_folds?: number | null
  promoted_candidate_id?: string | null
  published_strategy_id?: string | null
  gate?: MiningCandidateGate | null
  folds?: MiningFoldRow[]
}

export interface MiningTelemetry {
  elapsed_ms?: number
  peak_rss_bytes?: number
  panel_scans?: number
  matrix_bytes?: number
  cache_hits?: number
  fold_reuses?: number
  serialized_result_bytes?: number
  phase_ms?: Record<string, number>
}

export interface MiningRequestSummary {
  asset_type: string
  budget_profile: string
  start: string | null
  end: string | null
  factor_count: number
  strategy_count: number
  commission_pct: number | null
  stamp_tax_pct: number | null
  slippage_bps: number | null
  correlation_threshold: number | null
}

export interface MiningResult {
  run_id: string
  methodology_version: string
  algorithm_version: string
  data_as_of: string | null
  summary: MiningResultSummary
  request_summary?: MiningRequestSummary | null
  factors: MiningFactorRow[]
  correlation: {
    labels: string[]
    matrix: (number | null)[][]
    pair_counts?: (number | null)[][]
    threshold: number
  }
  regimes: MiningRegimeRow[]
  candidates: MiningCandidateRow[]
  folds: MiningFoldRow[]
  telemetry: MiningTelemetry
}

export interface MiningEvent {
  id: number
  type: string
  timestamp?: string
  payload?: Record<string, unknown>
  message?: string
}

export interface MiningScheduleConfig {
  mining_schedule_enabled: boolean
  mining_schedule_weekday: number
  mining_budget_profile: Exclude<MiningBudgetProfile, 'exploratory'>
}

export type ResearchCandidateKind = 'factor' | 'strategy'
export type ResearchCandidateStatus = 'pending' | 'validated' | 'rejected'

export interface ResearchCandidate {
  id: string
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of: string | null
  status: ResearchCandidateStatus
  created_at: string
  updated_at: string
}

export interface ResearchCandidateCreate {
  kind: ResearchCandidateKind
  name: string
  source_id: string
  config: Record<string, unknown>
  metrics: Record<string, number | string | boolean | null>
  data_as_of?: string | null
  status?: ResearchCandidateStatus
}

// ===== Strategy Backtest =====
export interface StrategyBacktestRequest {
  strategy_id: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
  matching?: 'close_t' | 'open_t+1'
  entry_fill?: 'close_t' | 'open_t+1' | null
  exit_fill?: 'close_t' | 'open_t+1' | 'signal_next_minute' | null
  fees_pct?: number
  commission_pct?: number
  stamp_tax_pct?: number
  buy_stamp_tax_pct?: number
  slippage_bps?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: 'equal' | 'score_weight'
  mode?: 'position' | 'full'
  holding_days?: number
  asset_type?: StrategyBacktestAsset
  minute_fill?: boolean
  regime_filter?: { states?: string[]; min_score?: number } | null
}

export interface StrategyBacktestTrade {
  symbol: string
  name?: string
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  pnl_pct: number
  duration: number
  exit_reason: string
  shares?: number
  lots?: number
  position_pct?: number
  entry_value?: number
  exit_value?: number
  pnl_amount?: number
  entry_score?: number | null
  entry_signal_date?: string | null
  exit_signal_date?: string | null
  blocked_exit_days?: number
  entry_signal_id?: string | null
  exit_signal_id?: string | null
}

export interface BacktestExecutionAssumptions {
  price_basis?: string | null
  price_basis_note?: string | null
  corporate_actions_simulated?: boolean
  price_sources?: string[]
  adjustment_sources?: string[]
  adjustment_versions?: string[]
  price_verified_range?: {
    start: string | null
    end: string | null
    verified_symbols?: number
    unknown_symbols?: number
  } | null
  lot_size_snapshot_dates?: string[]
  currency_restriction?: string | null
  financial_sources?: string[]
  financial_fields?: string[]
  financial_availability_rule?: string | null
}

export interface StrategyBacktestResult {
  warnings?: string[]
  run_id: string
  config: Record<string, any> & { execution_assumptions?: BacktestExecutionAssumptions | null }
  stats: Record<string, any>
  equity_curve: { date: string; value: number; cash?: number; positions?: number; exposure?: number }[]
  drawdown_curve: { date: string; value: number }[]
  benchmark_curve?: { date: string; value: number; close?: number; name?: string; symbol?: string }[]
  trades: StrategyBacktestTrade[]
  per_symbol_stats: {
    symbol: string
    n_trades: number
    total_return: number
    win_rate: number
    best: number
    worst: number
  }[]
  strategy_info: {
    id: string
    name: string
    description: string
    entry_signals: string[]
    exit_signals: string[]
    stop_loss: number | null
    take_profit: number | null
    trailing_stop: number | null
    trailing_take_profit_activate: number | null
    trailing_take_profit_drawdown: number | null
    score_min: number | null
    score_max: number | null
    max_hold_days: number | null
    source: string
    execution_backend?: string
    // 叠加策略回测: 子策略构成与权重归因
    composite_children?: { id: string; weight: number }[]
  }
  elapsed_ms: number
  error: string | null
}

// ===== Settings =====

/** 端点发现清单 —— 对应 tickflow.org/endpoints.json */
export interface EndpointItem {
  id: string
  url: string
  label: string
  region?: string
  description?: string
  premium?: boolean
}

export interface EndpointManifest {
  version?: number
  description?: string
  healthPath?: string
  /** 每端点测试轮数,用于 /health 多轮探测取中位数 */
  testRounds?: number
  endpoints: EndpointItem[]
  /** 数据来源:remote=远程拉取 / fallback=内置回退列表 */
  source?: 'remote' | 'fallback'
}

export interface SettingsState {
  mode: 'none' | 'free' | 'api_key'
  tickflow_api_key_masked: string
  has_tickflow_key: boolean
  tier_label: string
  current_endpoint: string
  probe_log: string[]
  missing_caps: string[]
  extras_caps: string[]
  // 首次使用引导
  onboarding_completed: boolean
  // AI 配置
  ai_provider: string
  ai_base_url: string
  ai_api_key_masked: string
  has_ai_key: boolean
  ai_configured?: boolean
  ai_model: string
  ai_openai_model?: string
  ai_reasoning_effort?: string
  ai_codex_model?: string
  ai_codex_command?: string
  ai_codex_reasoning_effort?: string
  ai_user_agent: string
  ai_max_output_tokens?: number
  ai_context_window?: number
}

/** 保存 TickFlow Key 的响应(先探后存) */
export interface SaveTickflowKeyResult {
  ok: boolean
  /** ok=false 且 key 无效时的原因标识,前端据此提示「Key 无效」 */
  reason?: 'invalid'
  error?: string
  mode?: 'none' | 'free' | 'api_key'
  tier_label?: string
  current_endpoint?: string
  tickflow_api_key_masked?: string
  capabilities_count?: number
}

export interface DataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  path?: string | null
}

/** 内置可选插件数据源 (plugins/ 目录, 需手动装依赖) */
export interface PluginDataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  runtime: string          // node | python | none
  available: boolean       // 依赖是否已安装
  status: string           // 可用性原因 (供 UI 显示)
  description: string
  install_hint: string     // 未装依赖时显示的安装命令
  api_key_env?: string     // 声明后设置页提供 Key 输入框 (先探后存)
}

export interface DataSourceLoadError {
  name?: string
  path: string
  errors: string[]
}

export interface DataSourcesResponse {
  builtin: DataSourceItem[]
  plugins: PluginDataSourceItem[]
  custom: DataSourceItem[]
  errors: DataSourceLoadError[]
  config_dir: string
}

export interface DataSourceTestResult {
  provider: string
  dataset: string
  rows: number
  columns: string[]
  preview: Record<string, unknown>[]
}

/** 插件 Key 保存结果 (先探后存: 无效 Key 返回 ok=false 且不落盘) */
export interface PluginKeyResult {
  ok: boolean
  reason?: string
  error?: string
  api_key_masked?: string
  plugin_available?: boolean
  plugin?: PluginDataSourceItem | null
}

export interface DatasetConfig {
  url: string
  method: string
  batch?: number | null
  rpm?: number | null
  response_path: string
  field_map: Record<string, string>
  transforms?: Record<string, string>
  symbols_param?: string
  start_param?: string
  end_param?: string
  asset_type_param?: string | null
  freq_param?: string | null
  timeout?: number | null
}

export interface AuthConfig {
  type: string
  token_env?: string | null
  header?: string
  param?: string
}

export interface CustomSourceConfig {
  name: string
  display_name: string
  auth: AuthConfig
  datasets: Record<string, DatasetConfig>
}

export interface WecomBotStatus {
  enabled: boolean
  running: boolean
  connected: boolean
  bot_id_configured: boolean
  secret_configured: boolean
  last_error: string
}

export interface Preferences {
  realtime_quotes_enabled: boolean
  indices_nav_pinned: boolean
  watchlist_groups_in_nav: boolean
  minute_sync_enabled: boolean
  minute_sync_days: number
  minute_sync_segment_days: number
  daily_data_provider?: string
  adj_factor_provider?: string
  minute_data_provider?: string
  realtime_data_provider?: string
  financial_data_provider?: string
  data_source_job_timeout_s: number
  data_source_long_job_timeout_s: number
  realtime_watchlist_symbols?: string[]
  realtime_pull_stock?: boolean
  realtime_pull_etf?: boolean
  realtime_pull_index?: boolean
  realtime_index_mode?: 'core' | 'all'
  realtime_index_symbols?: string[]
  pipeline_pull_a_share: boolean
  pipeline_pull_etf: boolean
  pipeline_pull_index: boolean
  pipeline_regime_enabled: boolean
  regime_batch_days: number
  regime_warmup_days: number
  pipeline_index_symbols: string
  pipeline_schedule: { hour: number; minute: number }
  instruments_schedule: { hour: number; minute: number }
  enriched_batch_size: number
  index_daily_batch_size: number
  limit_ladder_monitor_enabled: boolean
  depth_polling_interval: number
  depth_finalize_time: { hour: number; minute: number }
  review_schedule: { enabled: boolean; hour: number; minute: number }
  review_push_channels: string[]
  sse_refresh_pages: Record<string, boolean>
  strategy_monitor_enabled: boolean
  strategy_monitor_ids: string[]
  system_notify_enabled: boolean
  feishu_webhook_url?: string
  feishu_webhook_secret?: string
  wecom_webhook_url?: string
  wecom_bot_id?: string
  wecom_bot_secret?: string
  wecom_bot_enabled?: boolean
  webhook_enabled_default?: boolean
  webhook_default_channels?: string[]
  sidebar_index_symbols: string[]
  nav_order: string[]
  nav_hidden: string[]
  screener_auto_run: boolean
  minute_intraday_refresh: boolean
  minute_intraday_refresh_interval: number
  monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
}

/** 监控中心 ext 字段单项配置 (行业/概念标签的来源 + 显示裁剪) */
export interface MonitorExtFieldItem {
  /** "configId.fieldName" */
  field: string
  /** 显示前N个标签, 0=不限制 */
  maxTags?: number
  /** 隐藏的位置 (0-based), 如 [0] 表示隐藏第一个 */
  hiddenIndices?: number[]
}
export interface StrategyAlertEvent {
  source: 'strategy' | 'depth'
  type: string
  strategy_id?: string
  symbol?: string
  name?: string | null
  message: string
  price?: number | null
  change_pct?: number | null
  signals?: string[]
  /** ext 富化字段 (行业/概念等), 键为 "{configId}__{fieldName}" */
  [key: string]: unknown
}

// ===== API surface =====
export const api = {
  health: () => request<{ status: string; version: string; mode: string }>('/health'),

  // ===== Auth (访问认证) =====
  authStatus: () =>
    request<{ configured: boolean; authenticated: boolean }>('/api/auth/status'),
  authSetup: (password: string) =>
    request<{ ok: boolean }>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogin: (password: string) =>
    request<{ ok: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  authLogout: () =>
    request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),

  settings: () => request<SettingsState>('/api/settings'),
  saveTickflowKey: (api_key: string) =>
    request<SaveTickflowKeyResult>('/api/settings/tickflow-key', {
      method: 'POST',
      body: JSON.stringify({ api_key }),
    }),
  clearTickflowKey: () =>
    request<any>('/api/settings/tickflow-key', { method: 'DELETE' }),

  /** 标记首次使用向导完成（持久化到后端 preferences） */
  completeOnboarding: () =>
    request<{ ok: boolean; onboarding_completed: boolean }>(
      '/api/settings/onboarding/complete', { method: 'POST' },
    ),

  /** 保存 AI 配置 */
  saveAiSettings: (ai: { provider?: string; base_url?: string; api_key?: string; model?: string; reasoning_effort?: string; codex_command?: string; codex_reasoning_effort?: string; user_agent?: string; max_output_tokens?: number; context_window?: number }) =>
    request<{ ok: boolean; ai_provider?: string; ai_model?: string; ai_openai_model?: string; ai_reasoning_effort?: string; ai_codex_model?: string; ai_codex_command?: string; ai_codex_reasoning_effort?: string; ai_configured?: boolean; ai_max_output_tokens?: number; ai_context_window?: number }>('/api/settings/ai', {
      method: 'POST',
      body: JSON.stringify(ai),
    }),

  /** 一键清空 AI 配置(保留自定义 UA) */
  clearAiSettings: () =>
    request<{ ok: boolean }>('/api/settings/ai', { method: 'DELETE' }),

  preferences: () => request<Preferences>('/api/settings/preferences'),
  dataSources: () => request<DataSourcesResponse>('/api/settings/data-sources'),
  dataSource: (name: string) => request<CustomSourceConfig>(`/api/settings/data-sources/${encodeURIComponent(name)}`),
  saveDataSource: (config: CustomSourceConfig) =>
    request<DataSourcesResponse>('/api/settings/data-sources', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  deleteDataSource: (name: string) =>
    request<DataSourcesResponse>(`/api/settings/data-sources/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  reloadDataSources: () => request<DataSourcesResponse>('/api/settings/data-sources/reload', { method: 'POST' }),
  installPlugin: (name: string) => {
    // npm install 可能耗时较长, 用 6 分钟超时
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 360_000)
    return request<DataSourcesResponse & { install_ok: boolean; install_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'POST', signal: controller.signal },
    ).finally(() => clearTimeout(timer))
  },
  uninstallPlugin: (name: string) =>
    request<DataSourcesResponse & { uninstall_ok: boolean; uninstall_message: string }>(
      `/api/settings/plugins/${encodeURIComponent(name)}/install`,
      { method: 'DELETE' },
    ),
  savePluginKey: (plugin: string, apiKey: string) => {
    // 先探后存: 后端会用候选 Key 实探一次, 探测超时 10s + 余量
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 30_000)
    return request<PluginKeyResult>('/api/settings/plugin-key', {
      method: 'POST',
      body: JSON.stringify({ plugin, api_key: apiKey }),
      signal: controller.signal,
    }).finally(() => clearTimeout(timer))
  },
  clearPluginKey: (plugin: string) =>
    request<PluginKeyResult>(`/api/settings/plugin-key/${encodeURIComponent(plugin)}`, { method: 'DELETE' }),
  testDataSource: (
    provider: string,
    dataset: string,
    symbols?: string[],
    config?: CustomSourceConfig,
  ) =>
    request<DataSourceTestResult>('/api/settings/data-sources/test', {
      method: 'POST',
      body: JSON.stringify({ provider, dataset, symbols, config }),
    }),
  updateDataProviders: (cfg: Partial<Pick<Preferences, 'daily_data_provider' | 'adj_factor_provider' | 'minute_data_provider' | 'realtime_data_provider' | 'financial_data_provider'>>) =>
    request<Pick<Preferences, 'daily_data_provider' | 'adj_factor_provider' | 'minute_data_provider' | 'realtime_data_provider'>>(
      '/api/settings/preferences/data-providers',
      { method: 'PUT', body: JSON.stringify(cfg) },
    ),
  updateDataSourceJobTimeouts: (dataSourceJobTimeoutS: number, dataSourceLongJobTimeoutS: number) =>
    request<Pick<Preferences, 'data_source_job_timeout_s' | 'data_source_long_job_timeout_s'>>(
      '/api/settings/preferences/data-source-job-timeouts',
      {
        method: 'PUT',
        body: JSON.stringify({
          data_source_job_timeout_s: dataSourceJobTimeoutS,
          data_source_long_job_timeout_s: dataSourceLongJobTimeoutS,
        }),
      },
    ),
  updateMinuteSync: (enabled: boolean, days: number, segmentDays?: number) =>
    request<Preferences>('/api/settings/preferences/minute-sync', {
      method: 'PUT',
      body: JSON.stringify({
        minute_sync_enabled: enabled,
        minute_sync_days: days,
        ...(segmentDays != null ? { minute_sync_segment_days: segmentDays } : {}),
      }),
    }),
  updatePipelinePullTypes: (cfg: Partial<Pick<Preferences, 'pipeline_pull_a_share' | 'pipeline_pull_etf' | 'pipeline_pull_index'>>) =>
    request<{
      pipeline_pull_a_share: boolean
      pipeline_pull_etf: boolean
      pipeline_pull_index: boolean
    }>('/api/settings/preferences/pipeline-pull-types', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updatePipelineRegimeEnabled: (enabled: boolean) =>
    request<{ pipeline_regime_enabled: boolean }>('/api/settings/preferences/pipeline-regime-enabled', {
      method: 'PUT',
      body: JSON.stringify({ pipeline_regime_enabled: enabled }),
    }),
  updateRegimeBatchParams: (params: { batch_days?: number; warmup_days?: number }) =>
    request<{ regime_batch_days: number; regime_warmup_days: number }>('/api/settings/preferences/regime-batch-params', {
      method: 'PUT',
      body: JSON.stringify(params),
    }),
  updatePipelineIndexSymbols: (symbols: string) =>
    request<{ pipeline_index_symbols: string }>('/api/settings/preferences/pipeline-index-symbols', {
      method: 'PUT',
      body: JSON.stringify({ symbols }),
    }),
  updateRealtimeQuotes: (enabled: boolean) =>
    request<{ realtime_quotes_enabled: boolean; realtime_allowed?: boolean; mode?: string; error?: string }>('/api/settings/preferences/realtime-quotes', {
      method: 'PUT',
      body: JSON.stringify({ realtime_quotes_enabled: enabled }),
    }),
  updateRealtimeQuoteScope: (cfg: Partial<Pick<Preferences, 'realtime_pull_stock' | 'realtime_pull_etf' | 'realtime_pull_index' | 'realtime_index_mode' | 'realtime_index_symbols'>>) =>
    request<Partial<Preferences>>('/api/settings/preferences/realtime-quote-scope', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateIndicesNavPinned: (pinned: boolean) =>
    request<{ indices_nav_pinned: boolean }>('/api/settings/preferences/indices-nav-pinned', {
      method: 'PUT',
      body: JSON.stringify({ indices_nav_pinned: pinned }),
    }),
  updateWatchlistGroupsInNav: (enabled: boolean) =>
    request<{ watchlist_groups_in_nav: boolean }>('/api/settings/preferences/watchlist-groups-in-nav', {
      method: 'PUT',
      body: JSON.stringify({ watchlist_groups_in_nav: enabled }),
    }),
  quoteStatus: () =>
    request<{
      enabled: boolean
      running: boolean
      paused?: boolean
      mode?: 'none' | 'watchlist' | 'full_market'
      realtime_allowed?: boolean
      interval_s: number
      symbol_count: number
      watchlist_symbol_count?: number
      index_symbol_count?: number
      etf_symbol_count?: number
      quote_age_ms: number | null
      is_trading_hours: boolean
      is_polling_window?: boolean
      market_phase?: string
      final_sync_done?: boolean
      final_sync_failed?: string | null
      last_fetch_ms: number | null
    }>('/api/intraday/status'),
  quoteInterval: () =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
    ),
  updateQuoteInterval: (interval: number) =>
    request<{ interval: number; min_interval: number; max_interval: number }>(
      '/api/settings/preferences/quote-interval',
      { method: 'PUT', body: JSON.stringify({ interval }) },
    ),
  intradayRefresh: () => request<{ status: string }>('/api/intraday/refresh', { method: 'POST' }),
  indexQuotes: (symbols?: string[]) =>
    request<{ rows: IndexQuote[]; count: number }>(
      `/api/intraday/indices${symbols?.length ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`,
    ),
  updateRealtimeMonitorConfig: (cfg: {
    sse_refresh_pages?: Record<string, boolean>
    strategy_monitor_enabled?: boolean
    strategy_monitor_ids?: string[]
    sidebar_index_symbols?: string[]
    screener_auto_run?: boolean
    minute_intraday_refresh?: boolean
    minute_intraday_refresh_interval?: number
    monitor_ext_fields?: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
  }) =>
    request<{
      sse_refresh_pages: Record<string, boolean>
      strategy_monitor_enabled: boolean
      strategy_monitor_ids: string[]
      sidebar_index_symbols: string[]
      screener_auto_run: boolean
      minute_intraday_refresh: boolean
      minute_intraday_refresh_interval: number
      monitor_ext_fields: { concept: MonitorExtFieldItem | null; industry: MonitorExtFieldItem | null }
    }>('/api/settings/preferences/realtime-monitor', {
      method: 'PUT',
      body: JSON.stringify(cfg),
    }),
  updateSystemNotify: (enabled: boolean) =>
    request<{ system_notify_enabled: boolean }>('/api/settings/preferences/system-notify', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateFeishuWebhook: (url: string, secret: string = '') =>
    request<{ feishu_webhook_url: string; feishu_webhook_secret: string }>('/api/settings/preferences/feishu-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url, secret }),
    }),
  updateWecomWebhook: (url: string) =>
    request<{ wecom_webhook_url: string }>('/api/settings/preferences/wecom-webhook', {
      method: 'PUT',
      body: JSON.stringify({ url }),
    }),
  updateWecomBot: (botId: string, secret: string, enabled: boolean = true) =>
    request<{
      wecom_bot_id: string
      wecom_bot_secret: string
      wecom_bot_enabled: boolean
      wecom_bot_status: WecomBotStatus
    }>('/api/settings/preferences/wecom-bot', {
      method: 'PUT',
      body: JSON.stringify({ bot_id: botId, secret, enabled }),
    }),
  toggleWecomBot: (enabled: boolean) =>
    request<{ wecom_bot_enabled: boolean; wecom_bot_status: WecomBotStatus }>('/api/settings/preferences/wecom-bot-toggle', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateWebhookDefault: (enabled: boolean) =>
    request<{ webhook_enabled_default: boolean }>('/api/settings/preferences/webhook-enabled-default', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  updateWebhookDefaultChannels: (channels: string[]) =>
    request<{ webhook_default_channels: string[] }>('/api/settings/preferences/webhook-default-channels', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updatePipelineSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/pipeline-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateReviewSchedule: (enabled: boolean, hour: number, minute: number) =>
    request<{ enabled: boolean; hour: number; minute: number }>('/api/settings/preferences/review-schedule', {
      method: 'PUT',
      body: JSON.stringify({ enabled, hour, minute }),
    }),
  updateReviewPush: (channels: string[]) =>
    request<{ review_push_channels: string[] }>('/api/settings/preferences/review-push', {
      method: 'PUT',
      body: JSON.stringify({ channels }),
    }),
  updateDepthPollingInterval: (interval: number) =>
    request<{ depth_polling_interval: number }>('/api/settings/preferences/depth-polling-interval', {
      method: 'PUT',
      body: JSON.stringify({ interval }),
    }),
  updateLimitLadderMonitor: (enabled: boolean) =>
    request<{ limit_ladder_monitor_enabled: boolean }>('/api/settings/preferences/limit-ladder-monitor', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    }),
  runLimitLadderFix: () =>
    request<{ ok: boolean; count: number; msg: string }>('/api/settings/preferences/limit-ladder-monitor/run', {
      method: 'POST',
    }),
  updateDepthFinalizeTime: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/depth-finalize-time', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  saveNavOrder: (nav_order: string[]) =>
    request<{ nav_order: string[] }>('/api/settings/preferences/nav-order', {
      method: 'PUT',
      body: JSON.stringify({ nav_order }),
    }),
  saveNavHidden: (nav_hidden: string[]) =>
    request<{ nav_hidden: string[] }>('/api/settings/preferences/nav-hidden', {
      method: 'PUT',
      body: JSON.stringify({ nav_hidden }),
    }),
  updateInstrumentsSchedule: (hour: number, minute: number) =>
    request<{ hour: number; minute: number }>('/api/settings/preferences/instruments-schedule', {
      method: 'PUT',
      body: JSON.stringify({ hour, minute }),
    }),
  updateEnrichedBatchSize: (size: number) =>
    request<{ enriched_batch_size: number }>('/api/settings/preferences/enriched-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),
  updateIndexDailyBatchSize: (size: number) =>
    request<{ index_daily_batch_size: number }>('/api/settings/preferences/index-daily-batch-size', {
      method: 'PUT',
      body: JSON.stringify({ size }),
    }),

  // 自选列表列配置
  watchlistColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/watchlist-columns'),
  updateWatchlistColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/watchlist-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  // 策略结果列表列配置
  screenerResultColumns: () =>
    request<{ columns: any[] | null }>('/api/settings/preferences/screener-result-columns'),
  updateScreenerResultColumns: (columns: any[]) =>
    request<{ columns: any[] }>('/api/settings/preferences/screener-result-columns', {
      method: 'PUT',
      body: JSON.stringify({ columns }),
    }),

  capabilities: () => request<CapabilitiesResponse>('/api/capabilities'),
  version: () => request<{ version: string }>('/api/data/version'),
  redetectCapabilities: () =>
    request<CapabilitiesResponse>('/api/capabilities/redetect', { method: 'POST' }),

  klineDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }, extColumns?: string) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number; ext?: Record<string, unknown> }
      rows: KlineRow[]
      source?: string
    }>(
      (dateRange
        ? `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/kline/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`)
      + (extColumns ? `&ext_columns=${encodeURIComponent(extColumns)}` : ''),
    ),
  klineDailyBatch: (symbols: string[], days = 12) =>
    request<{ data: Record<string, KlineRow[]> }>('/api/kline/daily-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, days }),
    }),
  klineMinuteBatch: (symbols: string[], date?: string) =>
    request<{ data: Record<string, MinuteKlineRow[]> }>('/api/kline/minute-batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, date }),
    }),
  instrumentSearch: (q: string, limit = 20, assetTypes?: string) =>
    request<{ results: { symbol: string; name: string; code: string; asset_type?: string }[] }>(
      `/api/kline/instruments/search?q=${encodeURIComponent(q)}&limit=${limit}${assetTypes ? `&asset_types=${encodeURIComponent(assetTypes)}` : ''}`,
    ),

  /** 批量查股票名称 (传入 symbol 列表, 返回 {symbol: name}) */
  instrumentNames: (symbols: string[]) =>
    request<{ names: Record<string, string> }>('/api/kline/instruments/names', {
      method: 'POST',
      body: JSON.stringify(symbols),
    }),
  klineMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      stock_info?: { name?: string; total_shares?: number; float_shares?: number }
      date: string | null
      rows: MinuteKlineRow[]
      source?: 'local' | 'live' | 'none'
      asset_type?: 'stock' | 'etf' | 'index'
      price_limit?: PriceLimitInfo | null
      prev_close?: number | null
    }>(
      `/api/kline/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  klineMinuteRange: (symbol: string, days = 10) =>
    request<{
      symbol: string
      name?: string
      asset_type: 'stock' | 'etf' | 'index'
      requested_days: number
      sessions: MinuteKlineSession[]
      source: 'local' | 'none'
    }>(
      `/api/kline/minute-range?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  indexList: () => request<{ results: IndexInstrument[]; count: number }>('/api/index/list'),
  indexSearch: (q: string, limit = 20) =>
    request<{ results: IndexInstrument[] }>(
      `/api/index/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  indexDaily: (symbol: string, days = 120, dateRange?: { start: string; end: string }) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      rows: KlineRow[]
      source?: string
    }>(
      dateRange
        ? `/api/index/daily?symbol=${encodeURIComponent(symbol)}&start_date=${dateRange.start}&end_date=${dateRange.end}`
        : `/api/index/daily?symbol=${encodeURIComponent(symbol)}&days=${days}`,
    ),
  indexMinute: (symbol: string, date?: string) =>
    request<{
      symbol: string
      name?: string
      index_info?: IndexInstrument
      date: string | null
      rows: MinuteKlineRow[]
      source?: string
    }>(
      `/api/index/minute?symbol=${encodeURIComponent(symbol)}${date ? `&date=${date}` : ''}`,
    ),
  syncIndexInstruments: () =>
    request<{ status: string; count: number }>('/api/index/sync_instruments', { method: 'POST' }),
  syncIndexDaily: (days = 365) =>
    request<{ status: string; index_count: number; rows_written: number }>(
      `/api/index/sync_daily?days=${days}`,
      { method: 'POST' },
    ),
  syncSymbol: (symbol: string, days = 250) =>
    request<{ symbol: string; rows_written: number }>(
      `/api/kline/sync?symbol=${encodeURIComponent(symbol)}&days=${days}`,
      { method: 'POST' },
    ),
  syncMinute: (days?: number, extend?: boolean) =>
    request<{ status: string; job_id: string }>('/api/kline/sync_minute', {
      method: 'POST',
      body: JSON.stringify({ ...(days ? { days } : {}), ...(extend ? { extend: true } : {}) }),
    }),
  syncMinuteSingle: (symbol: string, days?: number) =>
    request<{ status: string; symbol: string; rows: number }>('/api/kline/sync_minute_single', {
      method: 'POST',
      body: JSON.stringify({ symbol, ...(days != null ? { days } : {}) }),
    }),
  clearMinute: () =>
    request<{ status: string; removed: number }>('/api/kline/clear_minute', {
      method: 'POST',
      body: JSON.stringify({ confirm: true }),
    }),
  extendHistory: (value: number, unit: 'day' | 'month' | 'year') =>
    request<{ status: string; job_id: string }>('/api/kline/extend_history', {
      method: 'POST',
      body: JSON.stringify({ value, unit }),
    }),
  repairDaily: (startDate: string) =>
    request<{ status: string; job_id: string }>('/api/kline/repair_daily', {
      method: 'POST',
      body: JSON.stringify({ start_date: startDate }),
    }),
  rebuildEnriched: () =>
    request<{ status: string; job_id: string }>('/api/kline/rebuild_enriched', {
      method: 'POST',
    }),

  watchlistList: () => request<{ symbols: WatchlistEntry[] }>('/api/watchlist'),
  watchlistAdd: (symbol: string, note = '', groupId?: string | null, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify({ symbol, note, group_id: groupId ?? null, market: market ?? null }),
    }),
  watchlistBatchAdd: (symbols: string[], note = '', groupId?: string | null, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[]; added: number }>('/api/watchlist/batch', {
      method: 'POST',
      body: JSON.stringify({ symbols, note, group_id: groupId ?? null, market: market ?? null }),
    }),
  watchlistGroups: () =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups'),
  watchlistGroupCreate: (name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[]; group: WatchlistGroup }>('/api/watchlist/groups', {
      method: 'POST',
      body: JSON.stringify({ name, color }),
    }),
  watchlistGroupRename: (groupId: string, name: string, color: WatchlistGroupColor) =>
    request<{ groups: WatchlistGroup[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'PUT', body: JSON.stringify({ name, color }) },
    ),
  watchlistGroupReorder: (orderedIds: string[]) =>
    request<{ groups: WatchlistGroup[] }>('/api/watchlist/groups/reorder', {
      method: 'PUT',
      body: JSON.stringify({ ordered_ids: orderedIds }),
    }),
  watchlistGroupDelete: (groupId: string) =>
    request<{ groups: WatchlistGroup[]; symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}`,
      { method: 'DELETE' },
    ),
  watchlistGroupClear: (groupId: string) =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/clear`,
      { method: 'POST' },
    ),
  watchlistSetGroup: (symbol: string, groupId: string | null, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/group${market ? `?market=${market}` : ''}`,
      { method: 'PUT', body: JSON.stringify({ group_id: groupId }) },
    ),
  watchlistGroupAddMember: (groupId: string, symbol: string, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}${market ? `?market=${market}` : ''}`,
      { method: 'POST' },
    ),
  watchlistGroupRemoveMember: (groupId: string, symbol: string, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(symbol)}${market ? `?market=${market}` : ''}`,
      { method: 'DELETE' },
    ),
  watchlistOcrStatus: () =>
    request<{ provider: string; available: boolean }>('/api/watchlist/ocr-status'),
  watchlistImportImage: (file: File, signal?: AbortSignal, quiet = false) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<WatchlistImportResult>('/api/watchlist/import-image', {
      method: 'POST',
      body: fd,
      signal,
      quiet,
    })
  },
  watchlistRemove: (symbol: string, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}${market ? `?market=${market}` : ''}`,
      { method: 'DELETE' },
    ),
  watchlistMoveToTop: (symbol: string, market?: 'cn' | 'hk' | 'us') =>
    request<{ symbols: WatchlistEntry[] }>(
      `/api/watchlist/${encodeURIComponent(symbol)}/top${market ? `?market=${market}` : ''}`,
      { method: 'POST' },
    ),
  watchlistClear: () =>
    request<{ removed: number }>('/api/watchlist', { method: 'DELETE' }),
  watchlistQuotes: () => request<{ quotes: Quote[] }>('/api/watchlist/quotes'),
  watchlistEnriched: (extColumns?: string) =>
    request<{ rows: any[]; as_of: string | null; elapsed_ms: number }>(
      extColumns
        ? `/api/watchlist/enriched?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/watchlist/enriched',
    ),

  marketStocks: (market: 'hk' | 'us') =>
    request<{ results: Array<{ symbol: string; name: string; lot_size?: number | null }> }>(`/api/${market}/stocks`),
  marketDataStatus: (market: InternationalMarket) =>
    request<MarketDataStatusResponse>(`/api/${market}/data/status`),
  marketSyncEnriched: (market: InternationalMarket, symbols?: string[]) =>
    request<MarketDataSyncResult>(`/api/${market}/enriched/sync${symbols !== undefined ? `?symbols=${encodeURIComponent(symbols.join(','))}` : ''}`, {
      method: 'POST',
    }),
  marketSyncDaily: (market: InternationalMarket, options: { symbols?: string[]; start?: string; end?: string } = {}) => {
    const query = new URLSearchParams()
    if (options.symbols !== undefined) query.set('symbols', options.symbols.join(','))
    if (options.start) query.set('start', options.start)
    if (options.end) query.set('end', options.end)
    return request<MarketDataSyncResult>(`/api/${market}/daily/sync?${query}`, { method: 'POST' })
  },
  marketSyncLotSizes: () =>
    request<MarketDataSyncResult>('/api/hk/instruments/lot-sizes/sync', { method: 'POST' }),
  marketSyncFinancials: (symbols?: string[]) =>
    request<MarketDataSyncResult>(symbols === undefined
      ? '/api/hk/financials/sync'
      : '/api/hk/financials/sync?symbols=' + encodeURIComponent(symbols.join(',')),
    { method: 'POST' }),
  screenerStrategies: async (assetType?: 'stock' | 'etf' | 'index' | 'hk' | 'us') => {
    const data = await request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies?${assetType ? `asset_type=${assetType}&` : ''}timeframe=1d`,
    )
    return { presets: data.strategies, load_errors: data.load_errors }
  },
  screenerRunPreset: (strategy_id: string, pool?: string[], asOf?: string, extColumns?: string, assetType: 'stock' | 'etf' | 'hk' | 'us' = 'stock') =>
    request<ScreenerResult>('/api/screener/run_preset', {
      method: 'POST',
      body: JSON.stringify({ strategy_id, pool, as_of: asOf ?? null, ext_columns: extColumns || null, asset_type: assetType }),
    }),
  screenerRunCustom: (conditions: string[], orderBy?: string, limit = 30, pool?: string[], extColumns?: string, assetType: 'stock' | 'etf' = 'stock') =>
    request<ScreenerResult>('/api/screener/run', {
      method: 'POST',
      body: JSON.stringify({ conditions, order_by: orderBy, limit, pool, ext_columns: extColumns || null, asset_type: assetType }),
    }),
  screenerRunAll: (asOf?: string, strategyIds?: string[], assetType: 'stock' | 'etf' = 'stock') =>
    request<{ as_of: string | null; results: Record<string, ScreenerResultSummary> }>(
      '/api/screener/run_all', { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null, strategy_ids: strategyIds ?? null, asset_type: assetType, timeframe: '1d', summary_only: true }) },
    ),
  screenerCachedSummary: () =>
    request<ScreenerCachedSummary>('/api/screener/cached-summary'),
  screenerCachedResult: (strategyId: string, extColumns?: string) =>
    request<ScreenerCachedResult>(
      extColumns
        ? `/api/screener/cached-result/${encodeURIComponent(strategyId)}?ext_columns=${encodeURIComponent(extColumns)}`
        : `/api/screener/cached-result/${encodeURIComponent(strategyId)}`,
    ),
  screenerCached: (extColumns?: string) =>
    request<{ as_of: string | null; results: Record<string, ScreenerResultSummary & { rows: any[] }>; today_ever_matched: Record<string, string[]> | null; today_ever_rows: Record<string, Record<string, any>> | null; updated_at: number | null }>(
      extColumns
        ? `/api/screener/cached?ext_columns=${encodeURIComponent(extColumns)}`
        : '/api/screener/cached',
    ),
  marketSnapshot: () =>
    request<{ as_of: string | null; rows: MarketSnapshotRow[] }>('/api/screener/market-snapshot'),
  overviewMarket: (asOf?: string) => request<OverviewMarket>(`/api/overview/market${asOf ? `?as_of=${asOf}` : ''}`),
  overviewHk: (asOf?: string) => request<OverviewMarket>(`/api/hk/overview${asOf ? `?as_of=${asOf}` : ''}`),
  overviewUs: (asOf?: string) => request<OverviewMarket>(`/api/us/overview${asOf ? `?as_of=${asOf}` : ''}`),
  // 跨市场态势总览: 日级判断, 后端 60s 缓存。markets 传 'cn,hk,us'(默认全量)
  overviewPosture: (markets?: string) =>
    request<MarketPostureResult>(`/api/overview/posture${markets ? `?markets=${markets}` : ''}`),

  // 概念涨幅轮动矩阵: 每列(日期)各自把所有概念按当天涨幅从高到低排序
  rpsRotation: (days: number, kind?: 'concept' | 'industry', level?: number) =>
    request<RpsRotationData>(`/api/rps/rotation?days=${days}${kind ? `&kind=${kind}` : ''}${level ? `&level=${level}` : ''}`),

  // 市场环境(Regime) — market 透传给后端 (cn/hk/us, 默认 cn 兼容老调用)
  regimeHistory: (start?: string, end?: string, limit?: number, market: MarketCode = 'cn') => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    if (limit) params.set('limit', String(limit))
    params.set('market', market)
    const qs = params.toString()
    return request<RegimeHistory>(`/api/regime/history${qs ? `?${qs}` : ''}`)
  },
  regimeLatest: (market: MarketCode = 'cn') =>
    request<{ row: RegimeRow | null }>(`/api/regime/latest?market=${market}`),
  regimeStates: (days = 60, market: MarketCode = 'cn') =>
    request<RegimeStates>(`/api/regime/states?days=${days}&market=${market}`),
  regimeCoverage: (market: MarketCode = 'cn') =>
    request<RegimeCoverage>(`/api/regime/coverage?market=${market}`),
  regimeRecompute: (start?: string, end?: string, market: MarketCode = 'cn') => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    params.set('market', market)
    const qs = params.toString()
    return request<{ ok: boolean; computed: number; phase_days?: number; mainline_rows?: number }>(`/api/regime/recompute${qs ? `?${qs}` : ''}`, { method: 'POST' })
  },
  regimePhases: (start?: string, end?: string, market: MarketCode = 'cn') => {
    const params = new URLSearchParams()
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    params.set('market', market)
    const qs = params.toString()
    return request<PhaseSegments>(`/api/regime/phases${qs ? `?${qs}` : ''}`)
  },
  regimeMainline: (start?: string, end?: string, top = 10, kind: 'concept' | 'industry' = 'concept') => {
    const params = new URLSearchParams({ top: String(top), kind })
    if (start) params.set('start', start)
    if (end) params.set('end', end)
    return request<MainlineResult>(`/api/regime/mainline?${params.toString()}`)
  },
  regimeMainlineRecompute: () =>
    request<{ ok: boolean; rows: number }>('/api/regime/mainline/recompute', { method: 'POST' }),

  // 强度梯队(动量档位) — 仅港美, market=cn 后端返 400
  strengthLadder: (market: MarketCode = 'hk', date?: string, bands?: StrengthBand[]) => {
    const params = new URLSearchParams({ market })
    if (date) params.set('date', date)
    if (bands?.length) params.set('bands', bands.join(','))
    return request<StrengthLadderResult>(`/api/strength_ladder?${params.toString()}`)
  },
  mainlineFilterUpdate: (payload: { min_members?: number; max_members?: number; blacklist?: string[]; exclude_st?: boolean }) =>
    request<MainlineFilter>('/api/settings/preferences/mainline-filter', {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  limitLadder: (asOf?: string, extColumns?: string, direction?: 'up' | 'down') => {
    const params = new URLSearchParams()
    if (asOf) params.set('as_of', asOf)
    if (extColumns) params.set('ext_columns', extColumns)
    if (direction === 'down') params.set('direction', 'down')
    const qs = params.toString()
    return request<LimitLadderResult>(
      `/api/screener/limit-ladder${qs ? `?${qs}` : ''}`,
    )
  },

  backtestStatus: () => request<{ available: boolean }>('/api/backtest/status'),

  backtestRun: (payload: {
    symbols: string[]
    entries: string[]
    exits: string[]
    start?: string
    end?: string
    stop_loss_pct?: number
    max_hold_days?: number
    matching?: 'close_t' | 'open_t+1'
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<BacktestResult>('/api/backtest/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorColumns: () =>
    request<{ columns: FactorColumn[] }>('/api/backtest/factor/columns'),

  scoringColumns: ({ assetType = 'stock', context = 'current', asOf }: {
    assetType?: StrategyBacktestAsset
    context?: ScoringContext
    asOf?: string
  } = {}) => {
    const query = new URLSearchParams({ purpose: 'scoring', asset_type: assetType, context })
    if (asOf) query.set('as_of', asOf)
    return request<{ columns: ScoringColumn[] }>(`/api/backtest/factor/columns?${query}`)
  },

  factorRun: (payload: {
    factor_name: string
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    fees_pct?: number
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBacktestResult>('/api/backtest/factor/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  factorBatch: (payload: {
    factor_names: string[]
    symbols?: string[] | null
    start?: string | null
    end?: string | null
    n_groups?: number
    rebalance?: 'daily' | 'weekly' | 'monthly'
    weight?: 'equal' | 'factor_weight'
    fees_pct?: number
    slippage_bps?: number
    asset_type?: 'stock' | 'etf' | 'index'
  }) =>
    request<FactorBatchResult>('/api/backtest/factor/batch', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningRuns: () =>
    request<{ items: MiningRun[] }>('/api/backtest/mining/runs'),

  miningAvailability: (params: {
    assetType: 'stock' | 'etf'
    budgetProfile: MiningBudgetProfile
    start?: string
    end?: string
  }) => {
    const query = new URLSearchParams({
      asset_type: params.assetType,
      budget_profile: params.budgetProfile,
    })
    if (params.start) query.set('start', params.start)
    if (params.end) query.set('end', params.end)
    return request<MiningAvailability>(`/api/backtest/mining/availability?${query}`, {
      quiet: true,
    })
  },

  miningRun: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}`),

  miningStart: (payload: MiningRequestV1) =>
    request<MiningRun>('/api/backtest/mining/runs', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  miningResult: (runId: string) =>
    request<MiningResult>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/result`),

  miningCancel: (runId: string) =>
    request<MiningRun>(`/api/backtest/mining/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    }),

  miningPromote: (runId: string, signature: string) =>
    request<ResearchCandidate>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/promote`,
      { method: 'POST' },
    ),

  miningPublish: (runId: string, signature: string) =>
    request<{ ok: boolean; strategy_id: string }>(
      `/api/backtest/mining/runs/${encodeURIComponent(runId)}/candidates/${encodeURIComponent(signature)}/publish`,
      { method: 'POST' },
    ),

  miningConfig: () =>
    request<MiningScheduleConfig>('/api/backtest/mining/config'),

  updateMiningConfig: (payload: Partial<MiningScheduleConfig>) =>
    request<MiningScheduleConfig>('/api/backtest/mining/config', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidates: () =>
    request<{ items: ResearchCandidate[] }>('/api/backtest/candidates'),

  researchCandidateCreate: (payload: ResearchCandidateCreate) =>
    request<ResearchCandidate>('/api/backtest/candidates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  researchCandidateUpdate: (
    id: string,
    payload: { name?: string; status?: ResearchCandidateStatus },
  ) =>
    request<ResearchCandidate>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  researchCandidateDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/backtest/candidates/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  strategyBacktestRun: (payload: StrategyBacktestRequest) =>
    request<StrategyBacktestResult>('/api/backtest/strategy/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  strategyBacktestStreamUrl: (payload: StrategyBacktestRequest | string) => {
    if (typeof payload === 'string') return '/api/backtest/strategy/stream?' + payload
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries({ ...payload, asset_type: payload.asset_type ?? 'stock' })) {
      if (value == null || value === '') continue
      query.set(key, key === 'symbols'
        ? (value as string[]).join(',')
        : typeof value === 'object' ? JSON.stringify(value) : String(value))
    }
    return '/api/backtest/strategy/stream?' + query.toString()
  },
  strategyBacktestCancel: (qs: string) =>
    request<{ ok: boolean; message?: string; cancelled_count?: number }>('/api/backtest/strategy/cancel', {
      method: 'POST',
      body: JSON.stringify({ qs }),
    }),

  // ── 新闻 / 舆情 ──────────────────────────────────────────────
  newsStatus: () => request<NewsStatus>('/api/news/status'),
  newsSearch: (query: string, days = 7, maxResults = 8) =>
    request<NewsResponse>(
      `/api/news/search?query=${encodeURIComponent(query)}&days=${days}&max_results=${maxResults}`,
    ),
  newsStock: (symbol: string, name?: string, days = 7, maxResults = 8) => {
    const namePart = name ? `&name=${encodeURIComponent(name)}` : ''
    return request<NewsResponse>(
      `/api/news/stock?symbol=${encodeURIComponent(symbol)}${namePart}&days=${days}&max_results=${maxResults}`,
    )
  },
  newsConcept: (topic: string, days = 7, maxResults = 8) =>
    request<NewsResponse>(
      `/api/news/concept?topic=${encodeURIComponent(topic)}&days=${days}&max_results=${maxResults}`,
    ),
  // 批量归因: 后端串行限速(0.6s/个)+ 结果缓存, 前端**禁止**并发逐条打 /api/news/stock。
  // 单批上限 200, 时间预算 25s —— 超出部分会标 error, 所以别一次塞几百个 symbol。
  newsBatchStock: (
    symbols: string[],
    names?: Record<string, string>,
    days = 7,
    maxResults = 8,
  ) =>
    request<NewsBatchStockResult>('/api/news/batch-stock', {
      method: 'POST',
      body: JSON.stringify({
        symbols,
        names: names ?? {},
        days,
        max_results: maxResults,
      }),
    }),
  newsInvalidate: () => request<{ ok: boolean }>('/api/news/cache/invalidate', { method: 'POST' }),
  newsCategories: () => request<{ categories: NewsCategory[] }>('/api/news/categories'),
  newsFeeds: (category: string, hours = 48, limit = 40) =>
    request<NewsFeedResult>(
      `/api/news/feeds?category=${encodeURIComponent(category)}&hours=${hours}&limit=${limit}`,
    ),
  getSearchKey: (provider = 'anspire') =>
    request<{ provider: string; configured: boolean; key_count: number; masked: string }>(
      `/api/settings/search-key?provider=${encodeURIComponent(provider)}`,
    ),
  saveSearchKey: (apiKey: string, provider = 'anspire') =>
    request<{ ok: boolean; provider: string; error?: string; masked?: string; key_count?: number }>(
      '/api/settings/search-key',
      { method: 'POST', body: JSON.stringify({ provider, api_key: apiKey }) },
    ),
  deleteSearchKey: (provider = 'anspire') =>
    request<{ ok: boolean; provider: string }>(
      `/api/settings/search-key?provider=${encodeURIComponent(provider)}`,
      { method: 'DELETE' },
    ),

  pipelineRun: () => request<{ job_id: string; reused: boolean }>(
    '/api/pipeline/run', { method: 'POST' },
  ),
  marketDailyRun: (payload: {
    market: 'HK' | 'US'
    start_date: string
    end_date: string
    mode?: 'full' | 'incremental'
    batch_size?: number
  }) => request<{ status: string; job_id: string; market: string; mode: string }>(
    '/api/pipeline/market-daily/run', {
      method: 'POST',
      body: JSON.stringify(payload),
    },
  ),
  marketDailyRetry: (jobId: string, batchSize?: number) => request<{ status: string; job_id: string; market: string; mode: string }>(
    '/api/pipeline/market-daily/retry', {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, ...(batchSize ? { batch_size: batchSize } : {}) }),
    },
  ),
  pipelineJob: (id: string) => request<PipelineJob>(`/api/pipeline/jobs/${id}`),
  pipelineJobs: (limit = 20) =>
    request<{ active_id: string | null; jobs: PipelineJobSummary[] }>(
      `/api/pipeline/jobs?limit=${limit}`,
    ),

  dataStatus: () => request<DataStatus>('/api/data/status'),
  dataFreshness: () => request<DataFreshness>('/api/data/freshness'),
  invalidateFreshness: () => request<{ ok: boolean }>('/api/data/freshness/invalidate', { method: 'POST' }),
  dataClear: () => request<{ deleted_files: number }>('/api/data/clear', { method: 'POST' }),
  refreshCache: () => request<{ ok: boolean }>('/api/data/refresh-cache', { method: 'POST' }),
  enrichedSchema: (table: string) => request<EnrichedField[]>(`/api/data/schema/${table}`),

  testEndpoint: (url: string, rounds?: number) =>
    request<{
      ok: boolean
      url: string
      rounds: number
      success: number
      median_ms: number | null
      min_ms?: number | null
      max_ms?: number | null
      /** 兼容旧字段,等于 median_ms */
      latency_ms?: number | null
      error?: string
    }>(
      '/api/settings/test_endpoint', {
        method: 'POST',
        body: JSON.stringify({ url, rounds }),
      },
    ),

  // 端点发现 —— 后端代理拉取 tickflow.org/endpoints.json(前端无法跨域直连)
  listEndpoints: () =>
    request<EndpointManifest>('/api/settings/endpoints'),

  switchEndpoint: (url: string) =>
    request<{ ok: boolean; current_endpoint: string; error?: string }>(
      '/api/settings/switch_endpoint', {
        method: 'POST',
        body: JSON.stringify({ url }),
      },
    ),

  // ===== 扩展数据 =====
  extDataList: () =>
    request<{ items: ExtDataConfig[] }>('/api/ext-data'),

  extDataRows: (id: string, opts?: { date?: string; limit?: number; columns?: string[] }) => {
    const qs = new URLSearchParams()
    if (opts?.date) qs.set('date', opts.date)
    if (opts?.limit) qs.set('limit', String(opts.limit))
    if (opts?.columns?.length) qs.set('columns', opts.columns.join(','))
    const suffix = qs.toString()
    return request<ExtDataRowsResult>(`/api/ext-data/${encodeURIComponent(id)}/rows${suffix ? `?${suffix}` : ''}`)
  },

  dimensionMembers: (id: string, opts: { field: string; value: string; date?: string; limit?: number }) => {
    const qs = new URLSearchParams({ field: opts.field, value: opts.value })
    if (opts.date) qs.set('date', opts.date)
    if (opts.limit) qs.set('limit', String(opts.limit))
    return request<DimensionMembersResult>(`/api/ext-data/${encodeURIComponent(id)}/dimension-members?${qs.toString()}`)
  },

  analysisMenus: () =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus'),

  analysisMenu: (id: string) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`),

  analysisMenuSave: (id: string, body: Omit<AnalysisMenu, 'id' | 'created_at' | 'updated_at' | 'builtin'>) =>
    request<AnalysisMenu>(`/api/analysis-menus/${encodeURIComponent(id)}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  analysisMenuReorder: (ids: string[]) =>
    request<{ items: AnalysisMenu[] }>('/api/analysis-menus/reorder', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),

  analysisMenuDelete: (id: string) =>
    request<{ status: string }>(`/api/analysis-menus/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  extDataCreate: (body: { id: string; label: string; mode: 'snapshot' | 'timeseries'; fields: { name: string; dtype: string; label: string }[]; description?: string; symbol_map?: Record<string, string>; code_map?: Record<string, string> }) =>
    request<ExtDataConfig>('/api/ext-data', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataUpdate: (id: string, body: { label?: string; fields?: { name: string; dtype: string; label: string }[]; description?: string }) =>
    request<ExtDataConfig>(`/api/ext-data/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  extDataDelete: (id: string) =>
    request<{ status: string }>(`/api/ext-data/${id}`, { method: 'DELETE' }),

  extDataUpload: (id: string, file: File, snapshotDate?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/upload${snapshotDate ? `?snapshot_date=${snapshotDate}` : ''}`,
      { method: 'POST', body: fd },
    )
  },

  extDataIngest: (id: string, body: { date?: string; rows: Record<string, unknown>[] }) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/ingest`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  extDataSchemaAll: () =>
    request<{ items: { id: string; label: string; mode: string; columns: { name: string; type: string; label: string }[] }[] }>('/api/ext-data/schema-all'),

  extDataPullConfig: (id: string, body: {
    url: string; method?: string; headers?: Record<string, string>; body?: string;
    response_path?: string; field_map?: Record<string, string>;
    schedule_minutes?: number; enabled?: boolean;
    time_window_start?: string | null; time_window_end?: string | null;
  }) =>
    request<{ status: string; pull: PullConfig }>(
      `/api/ext-data/${id}/pull`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  extDataPullTest: (id: string) =>
    request<{ status: string; total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean }>(
      `/api/ext-data/${id}/pull/test`,
      { method: 'POST' },
    ),

  extDataPullRun: (id: string) =>
    request<{ status: string; rows: number; date: string }>(
      `/api/ext-data/${id}/pull/run`,
      { method: 'POST' },
    ),

  // 内置预设 (概念/行业) 手动获取数据: 走结构转换, 保证 schema 一致
  extDataPresetFetch: (id: string) =>
    request<{ status: string; rows: number }>(
      `/api/ext-data/presets/${id}/fetch`,
      { method: 'POST' },
    ),

  extDataDetectFields: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<{ fields: { name: string; dtype: string; label: string }[]; rows: number; symbol_candidates: string[]; code_candidates: string[] }>(
      '/api/ext-data/detect-fields',
      { method: 'POST', body: fd },
    )
  },

  extDataDetectUrl: (body: ExtDataDetectUrlRequest) =>
    request<ExtDataDetectUrlResult>('/api/ext-data/detect-url', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  extDataFixSymbol: (id: string) =>
    request<{ status: string; fixed_files: number }>(
      `/api/ext-data/${id}/fix-symbol`,
      { method: 'POST' },
    ),

  // ===== Financials =====
  financialStatus: () =>
    request<FinancialStatus>('/api/financials/status'),

  financialMetrics: (symbol?: string) =>
    request<{ data: FinancialMetricRecord[] }>(
      `/api/financials/metrics${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialIncome: (symbol?: string) =>
    request<{ data: FinancialIncomeRecord[] }>(
      `/api/financials/income${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialBalanceSheet: (symbol?: string) =>
    request<{ data: FinancialBalanceSheetRecord[] }>(
      `/api/financials/balance-sheet${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialCashFlow: (symbol?: string) =>
    request<{ data: FinancialCashFlowRecord[] }>(
      `/api/financials/cash-flow${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  financialShares: (symbol?: string) =>
    request<{ data: FinancialSharesRecord[] }>(
      `/api/financials/shares${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''}`,
    ),

  /** 触发财务数据同步(后台异步执行,接口立即返回 started 状态) */
  financialSync: (table: string) =>
    request<{ status: string; synced: { started: boolean; reason?: string } }>(
      `/api/financials/sync/${table}`, { method: 'POST' },
    ),

  /** AI 分析报告 CRUD */
  financialReportsList: () =>
    request<{ reports: AiFinancialReport[] }>('/api/financials/reports'),

  financialReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    periods?: number; summary?: string
  }) =>
    request<{ ok: boolean; report: AiFinancialReport }>('/api/financials/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  financialReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/financials/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 财务分析 — 流式调用。
   *
   * 返回一个可逐行读取的 async generator,每行是 JSON:
   *   {type:"meta",symbol,summary,periods}
   *   {type:"delta",content:"..."}    ← 文本片段,逐个累加
   *   {type:"error",message:"..."}
   *   {type:"done"}
   *
   * 用 ReadableStream 解析(而非 SSE EventSource),支持 POST body 且更简单。
   */
  async *financialAnalyzeStream(symbol: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    periods?: number
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/financials/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      // 按行分割(保留最后不完整的行在 buf)
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try {
          yield JSON.parse(s)
        } catch {
          // 忽略无法解析的行
        }
      }
    }
    // 处理残余
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 个股分析 =====
  stockAnalysisLevels: (symbol: string, days = 120) =>
    request<StockLevels>(`/api/stock-analysis/levels?symbol=${encodeURIComponent(symbol)}&days=${days}`),

  stockAnalysisReportsList: () =>
    request<{ reports: AiStockReport[] }>('/api/stock-analysis/reports'),

  stockAnalysisReportSave: (r: {
    symbol: string; name?: string; focus?: string; content: string
    summary?: string; close?: number | null
    levels?: Record<LevelType, PriceLevel[]>
  }) =>
    request<{ ok: boolean; report: AiStockReport }>('/api/stock-analysis/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  stockAnalysisReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/stock-analysis/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 个股四维分析 — 流式调用(NDJSON,与财务分析同协议)。
   * meta 里额外带 levels(关键价位)供图表回放。
   */
  async *stockAnalyzeStream(symbol: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    symbol?: string
    summary?: string
    levels?: Record<LevelType, PriceLevel[]>
    close?: number | null
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/stock-analysis/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== 大盘复盘 =====
  reviewReportsList: () =>
    request<{ reports: AiReviewReport[] }>('/api/market-recap/reports'),

  reviewReportSave: (r: {
    as_of: string; focus?: string; content: string
    summary?: string; emotion_score?: number | null; emotion_label?: string
  }) =>
    request<{ ok: boolean; report: AiReviewReport }>('/api/market-recap/reports', {
      method: 'POST', body: JSON.stringify(r),
    }),

  reviewReportDelete: (reportId: string) =>
    request<{ ok: boolean }>(`/api/market-recap/reports/${encodeURIComponent(reportId)}`, { method: 'DELETE' }),

  /**
   * AI 大盘复盘 — 流式调用(NDJSON,与个股/财务分析同协议)。
   * meta 里带 as_of / emotion_score / emotion_label / summary,供前端先渲染信号灯。
   */
  async *reviewStream(asOf?: string, focus?: string): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    as_of?: string
    emotion_score?: number
    emotion_label?: string
    summary?: string
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/market-recap/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ as_of: asOf ?? null, focus: focus ?? '' }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  /** AI 概念轮动分析 — 流式 NDJSON。 */
  async *rotationAnalyzeStream(days: number, focus?: string, kind?: 'concept' | 'industry', level?: number): AsyncGenerator<{
    type: 'meta' | 'delta' | 'error' | 'done'
    days?: number
    summary?: string
    content?: string
    message?: string
  }> {
    const res = await fetch('/api/rps/rotation-analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days, focus: focus ?? '', kind: kind ?? 'concept', level: level ?? null }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  // ===== Strategy Engine =====
  strategyList: (assetType?: 'stock' | 'etf', timeframe = '1d') => {
    const params = new URLSearchParams()
    if (assetType) params.set('asset_type', assetType)
    if (timeframe) params.set('timeframe', timeframe)
    const qs = params.toString()
    return request<{ strategies: StrategyDetail[]; load_errors?: StrategyLoadError[] }>(
      `/api/strategies${qs ? `?${qs}` : ''}`,
    )
  },

  strategyGet: (id: string, assetType?: StrategyBacktestAsset) =>
    request<StrategyDetail>('/api/strategies/' + encodeURIComponent(id) + (assetType ? '?asset_type=' + assetType : '')),

  strategyRun: (strategyId: string, params?: Record<string, any>, asOf?: string, pool?: string[]) =>
    request<ScreenerResult>('/api/strategies/run', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, params, as_of: asOf ?? null, pool }),
    }),

  strategyRunAll: (asOf?: string) =>
    request<{ as_of: string | null; results: Record<string, { total: number; as_of: string }> }>(
      '/api/strategies/run-all',
      { method: 'POST', body: JSON.stringify({ as_of: asOf ?? null }) },
    ),

  strategySaveConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyPatchConfig: (strategyId: string, overrides: Record<string, any>) =>
    request<{ ok: boolean }>('/api/strategies/config', {
      method: 'PATCH',
      body: JSON.stringify({ strategy_id: strategyId, overrides }),
    }),

  strategyResetConfig: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/config/${strategyId}`, { method: 'DELETE' }),

  /** 删除自定义策略（内置策略不可删除） */
  strategyDelete: (strategyId: string) =>
    request<{ ok: boolean }>(`/api/strategies/${strategyId}`, { method: 'DELETE' }),

  strategyReload: () =>
    request<{ ok: boolean; count: number }>('/api/strategies/reload', { method: 'POST' }),

  // ===== Custom Signals (自定义信号) =====
  customSignalsList: () =>
    request<{ signals: CustomSignal[] }>('/api/custom-signals'),

  customSignalsOptions: () =>
    request<CustomSignalOptions>('/api/custom-signals/options'),

  customSignalSave: (signal: CustomSignal) =>
    request<{ ok: boolean; signal: CustomSignal }>('/api/custom-signals', {
      method: 'POST',
      body: JSON.stringify(signal),
    }),

  customSignalDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/custom-signals/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  customSignalsAiGenerate: (description: string) =>
    request<CustomSignalAIGenerateResult>('/api/custom-signals/ai/generate', {
      method: 'POST',
      body: JSON.stringify({ description }),
    }),

  // ===== Abnormal Moves (异动边缘) =====
  abnormalOverview: (minCloseness = 0.5, limit = 200) =>
    request<AbnormalOverview>(
      `/api/abnormal/overview?min_closeness=${minCloseness}&limit=${limit}`,
    ),
  abnormalHkUsOverview: (market: 'HK' | 'US', minCloseness = 0.5, limit = 200) =>
    request<AbnormalHkUsOverview>(
      `/api/abnormal/hk-us/overview?market=${market}&min_closeness=${minCloseness}&limit=${limit}`,
    ),

  // ===== Monitor Rules (监控规则) =====
  monitorRulesList: () =>
    request<{ rules: MonitorRule[] }>('/api/monitor-rules'),

  monitorRuleOptions: () =>
    request<MonitorRuleOptions>('/api/monitor-rules/options'),

  monitorRuleSave: (rule: MonitorRule) =>
    request<{ ok: boolean; rule: MonitorRule }>('/api/monitor-rules', {
      method: 'POST',
      body: JSON.stringify(rule),
    }),

  monitorRuleDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/monitor-rules/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  /** 模拟触发 ladder 封单监控 (Dev 调试, 不落盘不推送) */
  monitorRuleTestLadder: () =>
    request<{
      ok: boolean
      as_of: string
      sealed_count: number
      triggered: Array<{
        rule_id: string; rule_name: string; symbol: string; name?: string
        type: string; message: string; severity: string
        sealed_value: number; sealed_metric: string
        current_sealed_vol?: number; current_sealed_amount?: number
      }>
      not_triggered: Array<{
        rule_id: string; rule_name: string; symbol: string
        metric: string; threshold: number; current_value: number | null
        current_sealed_vol?: number; current_sealed_amount?: number | null
        reason: string
      }>
    }>('/api/monitor-rules/test-ladder', { method: 'POST' }),

  /** 真实触发 ladder 预警 (落盘+飞书+SSE), Dev 调试用 */
  monitorRuleTriggerLadder: () =>
    request<{
      ok: boolean
      triggered: number
      events: Array<{ symbol: string; name: string; message: string }>
    }>('/api/monitor-rules/trigger-ladder', { method: 'POST' }),

  /** 生成演示监控规则 (Dev 页用) */
  monitorRuleSeed: () =>
    request<{ ok: boolean; generated: number }>('/api/monitor-rules/seed', { method: 'POST' }),

  // ===== Alerts (触发记录) =====
  alertsList: (params?: { days?: number; limit?: number; source?: string; type?: string; extColumns?: string }) => {
    const qs = new URLSearchParams()
    if (params?.days) qs.set('days', String(params.days))
    if (params?.limit) qs.set('limit', String(params.limit))
    if (params?.source) qs.set('source', params.source)
    if (params?.type) qs.set('type', params.type)
    if (params?.extColumns) qs.set('ext_columns', params.extColumns)
    const s = qs.toString()
    return request<{ alerts: AlertEvent[]; total: number }>(`/api/alerts${s ? `?${s}` : ''}`)
  },

  alertsClear: () =>
    request<{ ok: boolean; cleared: number }>('/api/alerts', { method: 'DELETE' }),

  alertDelete: (ts: number) =>
    request<{ ok: boolean }>(`/api/alerts/${ts}`, { method: 'DELETE' }),

  /** 生成演示触发记录 (Dev 页用) */
  alertSeed: (count = 12, recent = true) =>
    request<{ ok: boolean; generated: number }>(`/api/alerts/seed?count=${count}&recent=${recent}`, { method: 'POST' }),

  /** 检查 AI 配置状态 */
  strategyAiStatus: () =>
    request<{ configured: boolean; has_key: boolean; has_model: boolean; provider?: string }>('/api/strategies/ai/status'),

  /** 测试 AI 连通性 */
  strategyAiTest: () =>
    request<{ ok: boolean; error?: string; model?: string; response?: string; usage?: { prompt: number; completion: number } }>(
      '/api/strategies/ai/test',
      { method: 'POST' },
    ),

  /** 获取策略源文件内容 */
  strategyGetSource: (id: string) =>
    request<{ code: string; source: string }>(`/api/strategies/${id}/source`),
  strategyBuild: (step: number, payload: Record<string, any>) =>
    request<StrategyBuildResult>(
      '/api/strategies/build',
      { method: 'POST', body: JSON.stringify({ step, ...payload }) },
    ),

  async *strategyBuildStream(step: number, payload: Record<string, any>): AsyncGenerator<StrategyBuildStreamEvent> {
    const res = await fetch('/api/strategies/build/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step, ...payload }),
    })
    if (!res.ok) {
      let detail = ''
      try { const j = JSON.parse(await res.text()); detail = j.detail ?? j.message ?? '' } catch { /* ignore */ }
      const msg = detail || `${res.status} ${res.statusText}`
      toast(msg, 'error')
      throw new Error(msg)
    }
    if (!res.body) throw new Error('响应无 body')

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true })
      const lines = buf.split('\n')
      buf = lines.pop() ?? ''
      for (const line of lines) {
        const s = line.trim()
        if (!s) continue
        try { yield JSON.parse(s) } catch { /* ignore */ }
      }
    }
    if (buf.trim()) {
      try { yield JSON.parse(buf.trim()) } catch { /* ignore */ }
    }
  },

  strategyValidateCode: (payload: { code: string; strategy_id?: string; name?: string; description?: string }) =>
    request<StrategyBuildResult>('/api/strategies/code/validate', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  strategySaveCodeV2: (payload: {
    strategy_id: string
    code: string
    target_source: 'ai' | 'custom'
    mode: 'create' | 'update'
    name?: string
    description?: string
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/code/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 创建/更新叠加策略(composite): 声明式引用多个子策略 */
  strategySaveComposite: (payload: {
    strategy_id: string
    name: string
    description?: string
    children: { strategy_id: string; weight: number }[]
    merge_mode: 'union' | 'intersect'
    min_confirm?: number
    mode: 'create' | 'update'
  }) =>
    request<StrategyCodeSaveResult>('/api/strategies/composite/save', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  /** 保存 AI 生成的策略文件 */
  strategySaveCode: (strategyId: string, code: string, meta?: { name?: string; description?: string }) =>
    request<{ ok: boolean; path: string }>('/api/strategies/ai/save', {
      method: 'POST',
      body: JSON.stringify({ strategy_id: strategyId, code, name: meta?.name ?? '', description: meta?.description ?? '' }),
    }),

  // ===== 热点工作区 (hotspot) =====
  // 目前仅 A 股 (cn) 有真实数据源 (akshare 东财概念/行业板块);
  // 港美返回 quality_status = missing_mapping 的空列表, 前端按"缺失映射"提示。
  hotspots: (params?: { market?: 'cn' | 'hk' | 'us'; top?: number; refresh?: boolean; includeDetails?: boolean }) => {
    const qs = new URLSearchParams()
    if (params?.market) qs.set('market', params.market)
    if (params?.top) qs.set('top', String(params.top))
    if (params?.refresh) qs.set('refresh', 'true')
    if (params?.includeDetails) qs.set('include_details', 'true')
    const s = qs.toString()
    return request<HotspotListResult>(`/api/v1/hotspots${s ? `?${s}` : ''}`)
  },

  hotspotDetail: (topic: string, market: 'cn' | 'hk' | 'us' = 'cn') =>
    request<HotspotDetailResult>(
      `/api/v1/hotspots/${encodeURIComponent(topic)}?market=${market}`,
    ),

  hotspotsRefresh: (market: 'cn' | 'hk' | 'us' = 'cn') =>
    request<HotspotRefreshResult>(`/api/v1/hotspots/refresh?market=${market}`, { method: 'POST' }),

  hotspotJobState: () => request<HotspotJobState>('/api/v1/hotspots/job-state'),
}

// ===== Pipeline =====
export interface MarketDailySyncResult {
  operation?: MarketDataSyncResult['operation']
  requested?: number
  succeeded?: number
  failed?: number
  skipped?: number
  unchanged?: number
  verified_not_applicable?: number
  enriched_dates_written?: number
  data_generation?: string
  failures?: MarketDataSyncResult['failures']
  items?: MarketDataSyncItem[]
  message?: string
  source?: string
  as_of?: string
  job_id?: string
  market?: 'HK' | 'US' | string
  provider?: string
  mode?: 'full' | 'incremental' | 'retry_only' | string
  universe_fingerprint?: string
  coverage_start?: string
  coverage_end?: string
  symbols_total?: number
  universe_symbols?: string[]
  completed_symbols?: string[]
  failed_symbols?: string[]
  skipped_symbols?: string[]
  attempts?: Record<string, number>
  provider_errors?: Record<string, string>
  last_success_symbol?: string | null
  last_success_at?: string | null
  checkpoint_path?: string
  status?: string
  partial_success?: boolean
  circuit_open?: boolean
  finished_at?: string
}

export interface PipelineJob {
  id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  stage: string
  progress: number          // 0-100 整体进度
  stage_pct: number         // 0-100 当前阶段内进度
  log: { ts: string; stage: string; msg: string }[]
  started_at: string | null
  finished_at: string | null
  duration_s: number | null
  result: (MarketDailySyncResult & {
    universe_size?: number
    daily_days?: number
    adj_factor_symbols?: number
    enriched_days?: number
    index_count?: number
    index_daily_rows?: number
    minute_rows?: number
    skipped_stages?: string[]
  }) | null
  error: string | null
}

export type PipelineJobSummary = Omit<PipelineJob, 'log'>

// ===== Data status =====
interface TableStats {
  rows: number
  earliest_date: string | null
  latest_date: string | null
  symbols_covered: number
  trading_days: number
}

interface InstrumentsStats {
  rows: number
  symbols_covered: number
  latest_as_of: string | null
  named: number
}

/** 单个市场的数据新鲜度 — 底部状态栏消费 */
export interface MarketFreshness {
  market: string
  label: string
  today: string
  latest_date: string | null
  raw_latest_date: string | null
  earliest_date: string | null
  history_days: number | null
  history_insufficient: boolean
  stale_days: number | null
  tolerance_days: number
  coverage_ratio: number
  coverage_units: number | null
  /** A 股单位是交易日, 港美是标的 — 展示时必须带上单位 */
  coverage_unit_label: string
  status: 'ok' | 'stale' | 'shallow' | 'behind_raw' | 'partial' | 'empty' | 'unknown'
  gap: { from: string; to: string; missing_days: number; reason: string } | null
}

export interface DataFreshness {
  generated_at: string
  markets: MarketFreshness[]
  active_job: {
    id: string
    status: string | null
    stage: string | null
    progress: number | null
    message: string | null
    market: string | null
    started_at: string | null
  } | null
  cached: boolean
}

export interface DataStatus {
  daily: TableStats | null
  enriched: TableStats | null
  index_daily: TableStats | null
  index_enriched: TableStats | null
  index_instruments: InstrumentsStats | null
  etf_daily: TableStats | null
  etf_enriched: TableStats | null
  etf_instruments: InstrumentsStats | null
  minute: TableStats | null
  adj_factor: TableStats | null
  instruments: InstrumentsStats | null
  financials: { rows: number; tables: Record<string, { rows: number; symbols: number }> } | null
  storage: {
    daily_files: number
    daily_size_mb: number
    enriched_files: number
    enriched_size_mb: number
    index_daily_files?: number
    index_daily_size_mb?: number
    index_enriched_files?: number
    index_enriched_size_mb?: number
    index_instruments_files?: number
    index_instruments_size_mb?: number
    etf_daily_files?: number
    etf_daily_size_mb?: number
    etf_enriched_files?: number
    etf_enriched_size_mb?: number
    etf_instruments_files?: number
    etf_instruments_size_mb?: number
    etf_adj_factor_files?: number
    etf_adj_factor_size_mb?: number
    minute_files: number
    minute_size_mb: number
    adj_factor_files: number
    adj_factor_size_mb: number
    instruments_files: number
    instruments_size_mb: number
    financials_files?: number
    financials_size_mb?: number
    ext_data_files?: number
    ext_data_size_mb?: number
    total_size_mb: number
  }
  next_pipeline_run: string | null
  next_instruments_run: string | null
  last_pipeline_run: string | null
  last_instruments_run: string | null
  checked_at: string
  indicators_ready?: boolean
}

export interface EnrichedField {
  name: string
  type: string
  desc: string
}

// ===== 扩展数据 =====
export interface ExtDataField {
  name: string
  dtype: string
  label: string
}

export interface PullConfig {
  url: string
  method: string
  headers?: Record<string, string>
  body?: string | null
  response_path: string
  field_map?: Record<string, string>
  schedule_minutes: number
  enabled: boolean
  last_run?: string | null
  last_status?: string | null
  last_message?: string | null
  last_rows?: number | null
  next_run?: string | null
  time_window_start?: string | null
  time_window_end?: string | null
}

export interface ExtDataDetectUrlRequest {
  url: string
  method?: string
  headers?: Record<string, string>
  body?: string
  response_path?: string
  field_map?: Record<string, string>
}

export interface ExtDataDetectUrlResult {
  status: string
  total_rows: number
  response_path: string
  response_path_candidates: string[]
  fields: ExtDataField[]
  symbol_candidates: string[]
  code_candidates: string[]
  preview: Record<string, unknown>[]
}

export interface ExtDataConfig {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  fields: ExtDataField[]
  description?: string
  symbol_map?: Record<string, string>
  code_map?: Record<string, string>
  created_at: string
  updated_at: string
  latest_sync_date?: string | null
  date_range?: string[] | null
  pull?: PullConfig | null
}

export interface ExtDataRowsResult {
  id: string
  label: string
  mode: 'snapshot' | 'timeseries'
  date: string | null
  total: number
  limit: number
  fields: ExtDataField[]
  rows: Record<string, any>[]
}

export interface DimensionMembersResult {
  id: string
  label: string
  date: string | null
  field: string
  value: string
  total: number
  limit: number
  rows: Record<string, any>[]
}

export interface AnalysisColumn {
  field: string
  label?: string
  type?: 'string' | 'number' | 'percent' | 'amount' | 'date'
  width?: number | null
  sortable?: boolean
  precision?: number | null
  format?: string | null
  aggregate?: 'count' | 'avg' | 'sum' | 'min' | 'max' | null
  visible?: boolean
}

export interface AnalysisMenu {
  id: string
  label: string
  icon: string
  data_source: string
  template: 'dimension_rank' | 'ranking' | 'table'
  dimension_field?: string | null
  rank_field?: string | null
  group_columns: AnalysisColumn[]
  detail_columns: AnalysisColumn[]
  default_sort?: { field: string; order: 'asc' | 'desc' } | null
  visible: boolean
  order: number
  created_at?: string | null
  updated_at?: string | null
  builtin?: boolean
}

// ===== 热点工作区 (hotspot) =====
// 单位契约: change_pct / turnover_rate 一律为小数制 (0.0826 = +8.26%),
// 前端统一走 fmtPct(v) 渲染。成分股字段可得性受限时后端显式返回 null,
// 前端渲染 '—', 不做任何推断 (如不从涨幅推断涨停)。

export interface HotspotStockRow {
  code: string
  name: string
  change_pct: number | null
  amount: number | null
  turnover_rate: number | null
  volume_ratio: number | null
  net_inflow: number | null
  is_limit_up: boolean
  active_days: number | null
  evidence_count: number
  role: string | null
  hot_stock_score: number | null
  source: string | null
  source_confidence: number | null
  fallback_used: boolean
}

export interface HotspotSummaryRow {
  topic: string
  name: string
  source: string | null
  rank: number | null
  change_pct: number | null
  heat_score: number | null
  trend_score: number | null
  persistence_score: number | null
  cooling_score: number | null
  observations: number
  state: string | null
  stage: string | null
  sample_stock_count: number
  leaders: string[]
  leader_stocks: HotspotStockRow[]
  quality_status: string | null
  missing_fields: string[]
  provider_used: string | null
  fallback_used: boolean
  source_errors: string[]
  stale: boolean
  stale_age_hours: number | null
  topic_date: string | null
  snapshot_at: string | null
  snapshot_market: string | null
  canonical_topic: string | null
  aliases: string[]
}

export interface HotspotDetailResult {
  enabled: boolean
  provider: string
  topic: string
  name: string | null
  canonical_topic: string | null
  aliases: string[]
  summary: HotspotSummaryRow
  route: Record<string, any>[]
  timeline: Record<string, any>[]
  stocks: HotspotStockRow[]
  leader_stocks: HotspotStockRow[]
  stock_count: number
  quality_status: string | null
  missing_fields: string[]
  provider_used: string | null
  fallback_used: boolean
  source_errors: string[]
  stale: boolean
  stale_age_hours: number | null
  cache_used: boolean
  news_search_requested: boolean
  news_search_status: string
}

export interface HotspotListResult {
  enabled: boolean
  provider: string
  provider_used: string
  fallback_used: boolean
  cache_used: boolean
  cached_at: number | null
  stale: boolean
  stale_age_hours: number | null
  quality_status: string | null
  source_errors: string[]
  market: string
  hotspots: HotspotSummaryRow[]
  hotspot_count: number
  details?: Record<string, HotspotDetailResult | { missing: boolean }>
}

/** job-state: 最近一次同步状态 (storage.job_state 直出) */
export interface HotspotJobState {
  last_run: string | null
  last_status: string | null
  rows: number
  markets: Record<string, number>
  last_success_at: Record<string, string>
  provider_used?: string | null
  message?: string | null
}

/** status: ok / degraded / empty / error / skipped */
export interface HotspotRefreshResult {
  status: string
  rows: number
  provider: string | null
  last_run?: string | null
}
