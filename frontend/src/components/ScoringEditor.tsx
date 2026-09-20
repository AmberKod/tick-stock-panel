import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Pencil, Plus, Save, Trash2, X } from 'lucide-react'
import { api, type ScoringColumn, type ScoringContext, type ScoringDirection, type StrategyBacktestAsset } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ConceptHeatStatus, conceptHeatNeedsRefresh, useConceptMarketDate } from '@/components/screener/ConceptHeatStatus'

interface Props {
  value: Record<string, number>
  directions: Record<string, ScoringDirection>
  onChange: (value: Record<string, number>, directions: Record<string, ScoringDirection>) => void
  fallbackLabels?: Record<string, string>
  assetType?: StrategyBacktestAsset
  context?: ScoringContext
  asOf?: string
}

function weightsToPercentages(values: Record<string, number>) {
  const entries = Object.entries(values).map(([name, value]) => [
    name,
    Math.max(0, Number(value) || 0),
  ] as const)
  const total = entries.reduce((sum, [, value]) => sum + value, 0)
  if (total <= 0) {
    return Object.fromEntries(entries.map(([name]) => [name, 0])) as Record<string, number>
  }

  const shares = entries.map(([name, value], index) => {
    const exact = value / total * 100
    return { name, index, value: Math.floor(exact), remainder: exact - Math.floor(exact) }
  })
  let remaining = 100 - shares.reduce((sum, item) => sum + item.value, 0)
  for (const item of [...shares].sort((a, b) => b.remainder - a.remainder || a.index - b.index)) {
    if (remaining <= 0) break
    item.value += 1
    remaining -= 1
  }
  return Object.fromEntries(shares.map(item => [item.name, item.value])) as Record<string, number>
}

function normalizePercentages(values: Record<string, number>) {
  const entries = Object.entries(values).map(([name, value]) => [name, Math.max(0, Number(value) || 0)] as const)
  const total = entries.reduce((sum, [, value]) => sum + value, 0)
  return Object.fromEntries(
    entries.map(([name, value]) => [name, total > 0 ? +(value / total).toFixed(6) : 0]),
  ) as Record<string, number>
}

function ScoringRow({ name, label, weight, direction, editing, onWeightChange, onDirectionChange, onRemove }: {
  name: string
  label: string
  weight: number
  direction: ScoringDirection
  editing: boolean
  onWeightChange: (value: number) => void
  onDirectionChange: (value: ScoringDirection) => void
  onRemove: () => void
}) {
  return (
    <div className="grid min-h-8 grid-cols-[minmax(3rem,6.5rem)_3.25rem_minmax(1.5rem,1fr)_2rem_1.75rem] items-center gap-1">
      <span className="truncate text-right text-[11px] text-secondary" title={`${label} · ${name}`}>{label}</span>
      {editing ? (
        <div className="grid h-6 grid-cols-2 overflow-hidden rounded border border-border bg-base">
          {([['high', ArrowUp, '偏好高值'], ['low', ArrowDown, '偏好低值']] as const).map(([value, Icon, title]) => (
            <button
              key={value}
              type="button"
              onClick={() => onDirectionChange(value)}
              className={`flex items-center justify-center transition-colors ${direction === value
                ? value === 'high' ? 'bg-emerald-400/15 text-emerald-400' : 'bg-cyan-400/15 text-cyan-400'
                : 'text-muted hover:bg-elevated hover:text-secondary'
              }`}
              title={title}
              aria-label={`${label}${title}`}
              aria-pressed={direction === value}
            >
              <Icon className="h-3 w-3" />
            </button>
          ))}
        </div>
      ) : (
        <span className={`flex items-center justify-center gap-1 text-[10px] ${direction === 'low' ? 'text-cyan-400' : 'text-emerald-400'}`}>
          {direction === 'low' ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />}
          {direction === 'low' ? '低值' : '高值'}
        </span>
      )}
      {editing ? (
        <input
          type="range"
          min={0}
          max={100}
          step={1}
          value={weight}
          onChange={event => onWeightChange(Number(event.target.value))}
          className="h-1 min-w-0 cursor-pointer accent-amber-400"
          aria-label={`${label}权重`}
        />
      ) : (
        <div className="h-1.5 min-w-0 overflow-hidden rounded-full bg-elevated">
          <div className="h-full rounded-full bg-amber-400/70" style={{ width: `${Math.min(weight, 100)}%` }} />
        </div>
      )}
      <span className="text-right font-mono text-[10px] text-muted">{weight}%</span>
      {editing ? (
        <button
          type="button"
          onClick={onRemove}
          className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-danger/10 hover:text-danger"
          title={`移除${label}`}
          aria-label={`移除评分因子${label}`}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      ) : <span aria-hidden="true" />}
    </div>
  )
}

export function ScoringEditor({ value, directions, onChange, fallbackLabels = {}, assetType = 'stock', context = 'current', asOf }: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, number>>(() => weightsToPercentages(value))
  const [directionDraft, setDirectionDraft] = useState<Record<string, ScoringDirection>>(directions)
  const [factorToAdd, setFactorToAdd] = useState('')
  const currentMarketDate = useConceptMarketDate(assetType)
  const queryDate = asOf || (context === 'current' ? currentMarketDate : undefined)
  const factors = useQuery({
    queryKey: QK.scoringColumns(assetType, context, queryDate),
    queryFn: () => api.scoringColumns({ assetType, context, asOf: queryDate }),
    staleTime: 60_000,
    refetchInterval: context === 'current' ? 60_000 : false,
    refetchOnWindowFocus: 'always',
    retry: false,
  })
  const factorLabels = useMemo(() => Object.fromEntries(
    (factors.data?.columns ?? []).map(item => [item.id, item.label]),
  ), [factors.data])
  const factorGroups = useMemo(() => {
    const groups: Record<string, ScoringColumn[]> = {}
    for (const item of factors.data?.columns ?? []) {
      ;(groups[item.group] ??= []).push(item)
    }
    return groups
  }, [factors.data])
  const conceptFactor = factors.data?.columns.find(item => item.id === 'concept_heat')
  const canAdd = (item: ScoringColumn) => !factors.isPending && !factors.isError
    && (item.id === 'concept_heat'
      ? item.available === true
        && (item.metadata?.status === 'available' || item.metadata?.status === 'partial')
        && !conceptHeatNeedsRefresh(item.metadata, currentMarketDate)
      : item.available !== false)
  const selectedFactor = factors.data?.columns.find(item => item.id === factorToAdd)
  const canAddSelected = !!selectedFactor && canAdd(selectedFactor)

  useEffect(() => {
    setEditing(false)
    setFactorToAdd('')
  }, [assetType, context, asOf])

  useEffect(() => {
    if (editing) return
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
  }, [directions, editing, value])

  const startEditing = () => {
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
    setFactorToAdd('')
    setEditing(true)
  }
  const cancelEditing = () => {
    setDraft(weightsToPercentages(value))
    setDirectionDraft(directions)
    setFactorToAdd('')
    setEditing(false)
  }
  const saveDraft = () => {
    const originalDraft = weightsToPercentages(value)
    const weightsUnchanged = Object.keys(draft).length === Object.keys(originalDraft).length
      && Object.entries(draft).every(([name, weight]) => weight === originalDraft[name])
    const normalized = weightsUnchanged ? { ...value } : normalizePercentages(draft)
    const nextDirections = Object.fromEntries(
      Object.keys(normalized).map(name => [name, directionDraft[name] ?? 'high']),
    ) as Record<string, ScoringDirection>
    onChange(normalized, nextDirections)
    setFactorToAdd('')
    setEditing(false)
  }
  const addFactor = () => {
    if (!factorToAdd || factorToAdd in draft || !canAddSelected) return
    setDraft(current => ({ ...current, [factorToAdd]: Object.keys(current).length > 0 ? 10 : 100 }))
    setDirectionDraft(current => ({ ...current, [factorToAdd]: 'high' }))
    setFactorToAdd('')
  }
  const removeFactor = (name: string) => {
    setDraft(current => Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== name),
    ))
    setDirectionDraft(current => Object.fromEntries(
      Object.entries(current).filter(([key]) => key !== name),
    ) as Record<string, ScoringDirection>)
  }

  const visibleWeights = editing ? draft : weightsToPercentages(value)
  const visibleDirections = editing ? directionDraft : directions
  const visibleKeys = Object.keys(visibleWeights)
  const draftTotal = Object.values(visibleWeights).reduce((sum, weight) => sum + weight, 0)

  return (
    <div className="min-w-0 space-y-3">
      {factors.isPending && <p className="text-[11px] text-muted" role="status">正在加载评分因子目录… 已有配置仍可编辑。</p>}
      {factors.isError && (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-warning" role="alert">
          <span>评分因子目录加载失败，已有配置仍可编辑或移除。</span>
          <button type="button" onClick={() => { void factors.refetch() }} disabled={factors.isFetching} className="underline disabled:opacity-50">{factors.isFetching ? '重试中…' : '重试加载'}</button>
        </div>
      )}
      {factors.isSuccess && factors.data.columns.length === 0 && (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted" role="status">
          <span>评分因子目录为空，已有配置仍可编辑或移除。</span>
          <button type="button" onClick={() => { void factors.refetch() }} disabled={factors.isFetching} className="underline disabled:opacity-50">重新加载</button>
        </div>
      )}
      {editing && (
        <div className="flex gap-2 border-b border-border/40 pb-3">
          <select
            value={factorToAdd}
            onChange={event => setFactorToAdd(event.target.value)}
            disabled={factors.isPending || factors.isError || !factors.data?.columns.length}
            className="h-8 min-w-0 flex-1 rounded-input border border-border bg-base px-2 text-xs text-secondary focus:border-accent focus:outline-none disabled:opacity-50"
            aria-label="选择要添加的评分因子"
          >
            <option value="">
              {factors.isLoading ? '加载因子目录…' : factors.isError ? '因子目录加载失败' : '选择评分因子'}
            </option>
            {Object.entries(factorGroups).map(([group, items]) => {
              const available = items.filter(item => !(item.id in draft))
              return available.length > 0 ? (
                <optgroup key={group} label={group}>
                  {available.map(item => <option key={item.id} value={item.id} disabled={!canAdd(item)}>{item.label}{canAdd(item) ? '' : '（不可用）'}</option>)}
                </optgroup>
              ) : null
            })}
          </select>
          <button
            type="button"
            onClick={addFactor}
            disabled={!canAddSelected}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-btn border border-accent/30 bg-accent/10 text-accent transition-colors hover:bg-accent/15 disabled:cursor-not-allowed disabled:opacity-40"
            title="添加评分因子"
            aria-label="添加评分因子"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {visibleKeys.length > 0 ? (
        <div className="space-y-2">
          {visibleKeys.map(name => (
            <ScoringRow
              key={name}
              name={name}
              label={factorLabels[name] ?? fallbackLabels[name] ?? (name === 'concept_heat' ? '概念热度' : name)}
              weight={visibleWeights[name] ?? 0}
              direction={visibleDirections[name] ?? 'high'}
              editing={editing}
              onWeightChange={weight => setDraft(current => ({ ...current, [name]: Math.max(0, weight) }))}
              onDirectionChange={direction => setDirectionDraft(current => ({ ...current, [name]: direction }))}
              onRemove={() => removeFactor(name)}
            />
          ))}
        </div>
      ) : (
        <div className="border-y border-border/40 py-5 text-center text-xs text-muted">
          {editing ? '请选择评分因子' : '当前策略不使用因子评分'}
        </div>
      )}

      {conceptFactor?.metadata?.status ? (
        <ConceptHeatStatus
          metadata={factors.isError ? { ...conceptFactor.metadata, status: 'unavailable', reason: '评分能力暂时无法确认，请重试加载。' } : conceptFactor.metadata}
          currentMarketDate={currentMarketDate}
          showFormula
        />
      ) : conceptFactor && (
        <p className="break-words text-[11px] leading-5 text-muted">概念热度：{conceptFactor.reason || conceptFactor.desc}</p>
      )}
      {'concept_heat' in visibleWeights && (!conceptFactor || !canAdd(conceptFactor)) && (
        <p className="break-words text-[11px] leading-5 text-warning">概念热度当前不可用。已有权重已保留，可调整为零或移除后使用其他因子。</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/40 pt-2">
        <div className="text-[10px] text-muted">
          权重 <span className={`font-mono text-xs font-medium ${editing && draftTotal !== 100 ? 'text-amber-400' : 'text-emerald-400'}`}>
            {editing ? draftTotal : draftTotal > 0 ? 100 : 0}%
          </span>
        </div>
        <div className="flex items-center gap-1">
          {editing && (
            <button
              type="button"
              onClick={cancelEditing}
              className="flex h-7 w-7 items-center justify-center rounded-btn text-muted transition-colors hover:bg-elevated hover:text-foreground"
              title="取消编辑"
              aria-label="取消编辑评分方案"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={editing ? saveDraft : startEditing}
            className="inline-flex h-7 items-center gap-1.5 rounded-btn border border-amber-400/40 bg-amber-400/10 px-2.5 text-[11px] text-amber-400 transition-colors hover:bg-amber-400/15"
          >
            {editing ? <Save className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
            {editing ? '保存方案' : '编辑方案'}
          </button>
        </div>
      </div>
    </div>
  )
}
