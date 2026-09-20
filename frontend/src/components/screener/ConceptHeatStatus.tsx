import { useEffect, useState } from 'react'
import type { ConceptHeatMetadata, StrategyBacktestAsset } from '@/lib/api'
import { marketToday } from '@/lib/backtestMarket'

export function useConceptMarketDate(assetType: StrategyBacktestAsset) {
  const [clock, setClock] = useState(() => ({ assetType, date: marketToday(assetType) }))
  useEffect(() => {
    const refresh = () => {
      const date = marketToday(assetType)
      setClock(previous => previous.assetType === assetType && previous.date === date
        ? previous : { assetType, date })
    }
    refresh()
    const timer = window.setInterval(refresh, 60_000)
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [assetType])
  return clock.assetType === assetType ? clock.date : marketToday(assetType)
}

export function conceptHeatNeedsRefresh(metadata: ConceptHeatMetadata | null | undefined, marketDate: string) {
  return (metadata?.status === 'available' || metadata?.status === 'partial')
    && !!metadata.current_market_date && metadata.current_market_date !== marketDate
}

interface Props {
  metadata?: ConceptHeatMetadata | null
  currentMarketDate?: string
  showFormula?: boolean
  title?: string
}

export function ConceptHeatStatus({ metadata, currentMarketDate, showFormula = false, title = '概念热度' }: Props) {
  if (!metadata?.status) return null
  const needsRefresh = !!currentMarketDate && conceptHeatNeedsRefresh(metadata, currentMarketDate)
  const status = needsRefresh ? 'unavailable' : metadata.status
  const reason = needsRefresh
    ? '市场日期已更新，请刷新评分能力并重新运行策略。'
    : metadata.reason
  const statusLabel = { available: '可用', partial: '部分缺失', unavailable: '不可用' }[status]
  const statusClass = status === 'available' ? 'border-emerald-400/20 bg-emerald-400/5 text-emerald-400' : 'border-warning/25 bg-warning/5 text-warning'

  return (
    <div className={`min-w-0 rounded-lg border px-3 py-2 text-[11px] leading-5 ${statusClass}`} role="status">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="break-words font-medium">{title}</span>
        <span className="rounded border border-current/20 px-1.5 text-[10px]">{statusLabel}</span>
      </div>
      {reason && <p className="mt-1 break-words">{reason}</p>}
      {showFormula && (
        <p className="mt-1 break-words text-secondary">
          同市场、同日概念内至少 {metadata.min_members ?? 3} 只有效成员的涨幅取均值，再对个股所属有效概念取均值；缺失值不计为零。
        </p>
      )}
      <div className="mt-1 flex flex-wrap gap-x-3 text-muted">
        <span>行情日期：{metadata.quote_date || '未提供'}</span>
        <span>市场当前日期：{currentMarketDate || metadata.current_market_date || '未提供'}</span>
      </div>
      {(metadata.input_symbols != null || metadata.computable_symbols != null) && (
        <div className="flex flex-wrap gap-x-3 text-muted">
          {metadata.input_symbols != null && <span>输入 {metadata.input_symbols} 只</span>}
          {metadata.mapped_symbols != null && <span>有映射 {metadata.mapped_symbols} 只</span>}
          {metadata.computable_symbols != null && <span>可计算 {metadata.computable_symbols} 只</span>}
          {metadata.unmapped_symbols != null && metadata.unmapped_symbols > 0 && <span>缺映射 {metadata.unmapped_symbols} 只</span>}
          {metadata.missing_valid_concept_symbols != null && metadata.missing_valid_concept_symbols > 0 && <span>缺有效概念 {metadata.missing_valid_concept_symbols} 只</span>}
        </div>
      )}
      <details className="mt-1 text-muted">
        <summary className="cursor-pointer select-none text-secondary">查看映射与数据说明</summary>
        <div className="mt-1 space-y-0.5 break-words">
          <p>来源：同花顺概念快照</p>
          <p>映射更新时间：{metadata.mapping_updated_at?.replace('T', ' ') || '未提供'}</p>
          <p className="break-all">映射版本：{metadata.mapping_version || '未提供'}</p>
          {metadata.valid_concepts != null && <p>有效概念：{metadata.valid_concepts} 个</p>}
          <p>快照更新时间不代表历史成分生效日期，当前快照不能用于历史回测。</p>
        </div>
      </details>
    </div>
  )
}
