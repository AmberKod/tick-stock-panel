/**
 * 日K复权。
 *
 * ⚠️ 为什么是"系数"而不是"另一串K":
 *     复权是**价格缩放**, 不是换数据源。本地 enriched 那串 K 上还挂着指标、涨跌停
 *     信号、成交额、换手、ext 列 —— 整串换成腾讯的复权K会把这些一起丢掉。
 *     后端只给 ``ratio = 复权收盘 / 不复权收盘``, 这里乘到本地 K 的价格列上,
 *     然后**在新序列上重算指标**(跟周/月/年、分钟K一个道理)。
 *
 * ⚠️ 为什么不能直接用 enriched 里那串"前复权":
 *     本地 adj_factor 表是空的(TickFlow ex_factors 对本账号返回 0 行),
 *     compute_enriched 的复权是空转 —— enriched 里存的实际是**不复权价**。
 *     所以「不复权」档 = 原样不动; 「前/后复权」档 = 乘腾讯给的系数。
 */
import type { OHLC } from '@/components/EChartsCandlestick'

/** qfq 前复权(最新价不动, 历史价下移) / hfq 后复权(最早价不动, 之后上移) / none 不复权。 */
export type AdjustMode = 'qfq' | 'hfq' | 'none'

export const ADJUST_MODES: { label: string; value: AdjustMode; hint: string }[] = [
  { label: '前复权', value: 'qfq', hint: '前复权: 以最新价为基准, 消除除权缺口(看长期趋势用这个)' },
  { label: '不复权', value: 'none', hint: '不复权: 原始成交价, 除权日会有跳空缺口' },
  { label: '后复权', value: 'hfq', hint: '后复权: 以最早价为基准, 之后价格整体上移(算累计涨幅用这个)' },
]

/**
 * 复权只覆盖 A股。写法兼容 `688825.SH` / `sh600519` / 裸 `600519`
 * (应用内主流是带后缀的 ts-code, 只认裸 6 位会整档失效)。
 */
export function isAdjustableCode(symbol: string): boolean {
  const s = (symbol || '').trim().toUpperCase()
  if (!s || /\.(HK|US)$/.test(s)) return false
  const core = s.replace(/^(SH|SZ|BJ)[.\-]?/, '').replace(/[.\-](SH|SZ|BJ)$/, '')
  return /^\d{6}$/.test(core)
}

export interface AdjRatioResp {
  available?: boolean
  reason?: string
  source?: string
  data?: {
    code?: string
    name?: string | null
    mode?: AdjustMode
    ratios?: { date: string; ratio: number }[]
    has_adjust?: boolean
  }
}

/**
 * 把复权系数乘到价格列上。
 *
 * 只动 open/high/low/close —— 成交量**不缩放**(腾讯三档返回的量本来就是同一个值,
 * 复权不改成交手数)。某天没有系数(区间外的日期)就**保持原价**, 不插值、不外推。
 */
export function applyAdjust(rows: OHLC[], ratios: { date: string; ratio: number }[]): OHLC[] {
  if (rows.length === 0 || ratios.length === 0) return rows
  const map = new Map<string, number>()
  for (const r of ratios) {
    if (Number.isFinite(r.ratio) && r.ratio > 0) map.set(r.date, r.ratio)
  }
  if (map.size === 0) return rows

  return rows.map(r => {
    const k = map.get(r.date)
    if (k == null || k === 1) return r
    return {
      ...r,
      open: r.open * k,
      close: r.close * k,
      high: r.high * k,
      low: r.low * k,
    }
  })
}
