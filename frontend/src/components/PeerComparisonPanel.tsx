/**
 * 同行业对比面板 —— 东财 F10「行业对比」, 全真源。
 *
 * ⚠️ **名单口径必须讲清(界面上写死, 不是藏在 tooltip 里)**:
 *   这里列的公司**不是行业全部成分股**, 而是东财按「成长性 / 估值 / 杜邦」三块
 *   各取该指标行业前列的几家公司, 再加本公司。三块入选标准不同, 所以**并非每家
 *   每列都有数** —— 缺的显示 "—", 不插值也不补零。
 *
 * ⚠️ **"同行"不一定同三级行业**: 逐家核过 EM2016 三级行业后发现, 东财有时会把
 *   产业链相关但三级行业不同的公司算进来(例: 宁德时代的同行里有做锂电材料的
 *   天赐材料、做设备的先导智能)。这些行标一个「行业不同」的角标并写出它自己的
 *   行业, 不替东财默认它一定同行业。
 *
 * ⚠️ 估值与财务指标来自东财口径的**最新定期报告**(各块报告期可能不同, 顶部
 *   分块标出), 不是实时行情快照。
 */
import { useMemo, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, ChevronDown } from 'lucide-react'
import type { PeerRow, PeerMetricKey } from '@/lib/api'
import { cn } from '@/lib/cn'

interface Industry {
  em?: string
  em_parts?: string[]
  csrc?: string
}

interface Props {
  code: string
  name?: string | null
  industry?: Industry | null
  peers: PeerRow[]
  peerCount: number
  median: Partial<Record<PeerMetricKey, number | null>>
  avg: Partial<Record<PeerMetricKey, number | null>>
  selfSize?: {
    total_cap?: number | null
    revenue?: number | null
    net_profit?: number | null
    cap_rank?: number | null
    revenue_rank?: number | null
    profit_rank?: number | null
    report_type?: string | null
  } | null
  /** 是否成功核到各家的行业归属(核对失败时同行"同行业"是未知, 不是 false) */
  industryChecked: boolean
  reportDates?: {
    valuation?: string | null
    growth?: string | null
    dupont?: string | null
    size?: string | null
  } | null
  caveats?: string[]
  className?: string
}

/** 列定义 —— 只列我们能说清单位的指标。 */
interface Col {
  key: PeerMetricKey
  label: string
  hint: string
  /** '%' 结尾的列按涨跌着色(红涨绿跌) */
  pct?: boolean
  digits?: number
}

const COLS: Col[] = [
  { key: 'pe_ttm', label: 'PE(TTM)', hint: '滚动市盈率(东财口径)', digits: 2 },
  { key: 'pb', label: 'PB', hint: '市净率', digits: 2 },
  { key: 'ps_ttm', label: 'PS(TTM)', hint: '滚动市销率', digits: 2 },
  { key: 'peg', label: 'PEG', hint: '市盈率相对盈利增长比率', digits: 2 },
  { key: 'revenue_yoy', label: '营收同比', hint: '营业收入同比增长率', pct: true },
  { key: 'profit_yoy', label: '净利同比', hint: '归母净利润同比增长率', pct: true },
  { key: 'profit_cagr3', label: '净利3年复合', hint: '净利润 3 年复合增长率', pct: true },
  { key: 'roe_avg', label: 'ROE(平均)', hint: '杜邦口径净资产收益率(平均)', pct: true },
  { key: 'net_margin', label: '销售净利率', hint: '净利润 / 营业收入', pct: true },
]

const DASH = '—'

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function fmtCell(v: number | null, digits = 2, pct = false): string {
  if (v == null) return DASH
  return `${v.toFixed(digits)}${pct ? '%' : ''}`
}

/** 亿元金额: 1234.5 亿 → "1,234.5" */
function fmtYi(v: number | null): string {
  if (v == null) return DASH
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

export function PeerComparisonPanel({
  code, name, industry, peers, peerCount, median, avg, selfSize,
  industryChecked, reportDates, caveats, className,
}: Props) {
  const [sort, setSort] = useState<{ key: PeerMetricKey; dir: 'asc' | 'desc' } | null>(null)
  const [onlySame, setOnlySame] = useState(false)
  const [showCaveats, setShowCaveats] = useState(true)

  const industryLabel = industry?.em || industry?.csrc || null

  const rows = useMemo(() => {
    const base = onlySame ? peers.filter(p => p.is_self || p.same_industry !== false) : peers
    if (!sort) return base
    const { key, dir } = sort
    // 缺值的永远沉底 —— 不把 null 当 0 排(那等于宣称"它是最低的")
    return [...base].sort((a, b) => {
      const av = num(a[key]), bv = num(b[key])
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      return dir === 'desc' ? bv - av : av - bv
    })
  }, [peers, onlySame, sort])

  const clickSort = (key: PeerMetricKey) => {
    setSort(s => (s?.key === key
      ? (s.dir === 'desc' ? { key, dir: 'asc' } : null)
      : { key, dir: 'desc' }))
  }

  return (
    <div className={cn('rounded-md border border-border bg-surface', className)}>
      {/* ── 标题 + 口径 ───────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-2">
        <span className="text-[12px] font-medium text-fg">同行业对比</span>
        {industryLabel && (
          <span className="text-[11px] text-fg-secondary">{industryLabel}</span>
        )}
        <span className="rounded bg-emerald-400/10 px-1.5 py-0.5 text-[10px] text-emerald-500">
          东财 F10 真源
        </span>
        <span className="rounded bg-amber-400/10 px-1.5 py-0.5 text-[10px] text-amber-500">
          非全部成分股
        </span>
        <span className="text-[10px] text-fg-muted">
          {peerCount} 家同行
          {peers.length < peerCount && `（显示前 ${peers.length} 家）`}
        </span>
        <button
          type="button"
          onClick={() => setShowCaveats(v => !v)}
          className="ml-auto inline-flex items-center gap-0.5 text-[10px] text-fg-muted hover:text-fg-secondary"
        >
          口径说明
          <ChevronDown className={cn('h-3 w-3 transition-transform', showCaveats && 'rotate-180')} />
        </button>
      </div>

      {showCaveats && (
        <div className="border-b border-border bg-elevated/40 px-3 py-1.5">
          <ul className="list-disc space-y-0.5 pl-4 text-[10px] leading-relaxed text-fg-muted">
            {(caveats ?? [
              '同行名单来自东财 F10「行业对比」: 成长性/估值/杜邦三块各给出该指标行业前列的几家公司, 再加本公司。不是行业全部成分股。',
              '三块的入选标准不同, 因此不是每家公司每个指标都有数: 缺的显示 —。',
              '估值与财务指标基于东财口径的最新定期报告, 不是实时行情快照。',
            ]).map(c => <li key={c}>{c}</li>)}
            {!industryChecked && (
              <li className="text-amber-500">
                本次未能核到各家的行业归属(上游核对查询失败), 「是否同三级行业」一栏留空。
              </li>
            )}
          </ul>
        </div>
      )}

      {/* ── 本公司规模与行业名次 ──────────────────────────────── */}
      {selfSize && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5 text-[10px] text-fg-muted">
          <span>
            {name ?? code} 行业名次
            {selfSize.report_type && `（${selfSize.report_type}）`}：
          </span>
          <Stat label="总市值" value={`${fmtYi(num(selfSize.total_cap))} 亿`} rank={num(selfSize.cap_rank)} />
          <Stat label="营业收入" value={`${fmtYi(num(selfSize.revenue))} 亿`} rank={num(selfSize.revenue_rank)} />
          <Stat label="净利润" value={`${fmtYi(num(selfSize.net_profit))} 亿`} rank={num(selfSize.profit_rank)} />
        </div>
      )}

      {/* ── 报告期 + 过滤 ─────────────────────────────────────── */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-1.5 text-[10px] text-fg-muted">
        {reportDates?.valuation && <span>估值 {reportDates.valuation}</span>}
        {reportDates?.growth && <span>· 成长 {reportDates.growth}</span>}
        {reportDates?.dupont && <span>· 杜邦 {reportDates.dupont}</span>}
        {reportDates?.size && <span>· 规模 {reportDates.size}</span>}
        {industryChecked && (
          <label className="ml-auto inline-flex cursor-pointer select-none items-center gap-1">
            <input
              type="checkbox"
              checked={onlySame}
              onChange={e => setOnlySame(e.target.checked)}
              className="h-3 w-3 accent-current"
            />
            仅看同三级行业
          </label>
        )}
      </div>

      {/* ── 表格 ─────────────────────────────────────────────── */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[880px] text-[11px]">
          <thead>
            <tr className="text-fg-muted">
              <th className="px-3 py-1.5 text-left font-normal">公司</th>
              <th className="px-2 py-1.5 text-right font-normal">总市值(亿)</th>
              {COLS.map(c => (
                <th
                  key={c.key}
                  title={c.hint}
                  onClick={() => clickSort(c.key)}
                  className="cursor-pointer px-2 py-1.5 text-right font-normal hover:text-fg-secondary"
                >
                  <span className="inline-flex items-center gap-0.5">
                    {c.label}
                    {sort?.key === c.key && (
                      sort.dir === 'desc'
                        ? <ArrowDown className="h-2.5 w-2.5" />
                        : <ArrowUp className="h-2.5 w-2.5" />
                    )}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(p => (
              <tr
                key={p.code}
                className={cn(
                  'border-t border-border/60',
                  p.is_self && 'bg-amber-400/5 font-medium',
                )}
              >
                <td className="px-3 py-1.5">
                  <span className="inline-flex items-center gap-1">
                    <span className="font-mono text-[10px] text-fg-muted">{p.code}</span>
                    <span className={cn(p.is_self ? 'text-fg' : 'text-fg-secondary')}>{p.name}</span>
                    {p.is_self && (
                      <span className="rounded bg-amber-400/15 px-1 text-[9px] text-amber-500">本公司</span>
                    )}
                    {p.same_industry === false && (
                      <span
                        className="inline-flex items-center gap-0.5 rounded bg-border px-1 text-[9px] text-fg-muted"
                        title={`东财把它算作同行, 但它的 EM2016 三级行业是「${p.industry ?? '未知'}」, 与本公司不同`}
                      >
                        <AlertTriangle className="h-2.5 w-2.5" />
                        行业不同
                      </span>
                    )}
                  </span>
                </td>
                <td className="px-2 py-1.5 text-right font-mono text-fg-secondary">
                  {fmtYi(num(p.total_cap))}
                  {p.cap_rank != null && p.cap_rank > 0 && (
                    <span className="ml-1 text-[9px] text-fg-muted">#{Math.round(p.cap_rank)}</span>
                  )}
                </td>
                {COLS.map(c => {
                  const v = num(p[c.key])
                  return (
                    <td
                      key={c.key}
                      className={cn(
                        'px-2 py-1.5 text-right font-mono',
                        v == null
                          ? 'text-fg-muted'
                          : c.pct
                            ? (v >= 0 ? 'text-bull' : 'text-bear')
                            : 'text-fg-secondary',
                      )}
                    >
                      {fmtCell(v, c.digits, c.pct)}
                    </td>
                  )
                })}
              </tr>
            ))}
            <tr className="border-t border-border bg-elevated/40 text-fg-muted">
              <td className="px-3 py-1.5">行业中值</td>
              <td className="px-2 py-1.5" />
              {COLS.map(c => (
                <td key={c.key} className="px-2 py-1.5 text-right font-mono">
                  {fmtCell(num(median[c.key]), c.digits, c.pct)}
                </td>
              ))}
            </tr>
            <tr className="border-t border-border bg-elevated/40 text-fg-muted">
              <td className="px-3 py-1.5">行业平均</td>
              <td className="px-2 py-1.5" />
              {COLS.map(c => (
                <td key={c.key} className="px-2 py-1.5 text-right font-mono">
                  {fmtCell(num(avg[c.key]), c.digits, c.pct)}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>

      <div className="px-3 py-1.5 text-[10px] text-fg-muted">
        点表头按该列排序（缺值的行永远沉底，不按 0 算）。本公司那行高亮，行业中值/平均由东财给出。
      </div>
    </div>
  )
}

function Stat({ label, value, rank }: { label: string; value: string; rank: number | null }) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <span className="font-mono text-fg-secondary">{value}</span>
      {rank != null && rank > 0 && <span className="text-fg-muted">行业第 {Math.round(rank)}</span>}
    </span>
  )
}
