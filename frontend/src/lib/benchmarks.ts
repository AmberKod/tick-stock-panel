/**
 * 对比页可用的**基准指数**。
 *
 * 全部走 `GET /api/astock/index/{code}`(腾讯真源, source=live-tencent);
 * 本地 index 库实测只有创业板指一个, 沪深300/上证指数都没有 —— 所以这里不认本地库,
 * 一律打腾讯。拿不到时后端 available:false, 页面就不画这条基准线。
 *
 * 🔴 代码前缀规则与个股相反: 沪市指数是 **0 开头但属于 sh**(000300 沪深300、
 * 000001 上证指数), 深市指数才是 399 开头。后端 `_tx_index_symbol` 单独处理。
 */
export interface Benchmark {
  code: string
  label: string
  /** 一句话说明这个基准代表什么, 用在按钮 title 上 */
  hint: string
}

export const BENCHMARKS: Benchmark[] = [
  { code: '000300.SH', label: '沪深300', hint: '沪深两市市值最大、流动性最好的 300 只 —— A股最通用的业绩基准' },
  { code: '000001.SH', label: '上证指数', hint: '沪市全部上市股票(含 B 股)的综合指数 —— 日常说的"大盘"' },
  { code: '399001.SZ', label: '深证成指', hint: '深市 500 只代表性成分股' },
  { code: '399006.SZ', label: '创业板指', hint: '创业板 100 只 —— 成长风格基准' },
  { code: '000905.SH', label: '中证500', hint: '沪深两市剔除沪深300 后的 500 只中盘股 —— 中盘风格基准' },
  { code: '000016.SH', label: '上证50', hint: '沪市市值最大的 50 只 —— 大盘蓝筹基准' },
  { code: '000688.SH', label: '科创50', hint: '科创板市值最大的 50 只 —— 硬科技风格基准' },
]

export const DEFAULT_BENCHMARK = '000300.SH'

export function benchmarkLabel(code: string): string {
  return BENCHMARKS.find(b => b.code === code)?.label ?? code
}
