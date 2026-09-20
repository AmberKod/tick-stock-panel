import { useSyncExternalStore } from 'react'
import { api, type StrategyBacktestAsset, type StrategyBacktestRequest, type StrategyBacktestResult } from './api'
import { storage } from './storage'

export interface BacktestProgress {
  day: number
  total: number
  date: string
  equity: number
}

export interface BacktestTask {
  id: number
  assetType: StrategyBacktestAsset
  isPending: boolean
  result: StrategyBacktestResult | null
  progress: BacktestProgress | null
  error: string | null
  reconnecting: boolean
}

const MAX_RECONNECT_ATTEMPTS = 5
const tasks = new Map<StrategyBacktestAsset, BacktestTask>()
const connections = new Map<StrategyBacktestAsset, EventSource>()
const listeners = new Set<() => void>()
let taskSeq = 0

function emit() { listeners.forEach((listener) => listener()) }
function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
function getServerSnapshot() { return null }

function closeConnection(asset: StrategyBacktestAsset) {
  connections.get(asset)?.close()
  connections.delete(asset)
}

function connectSSE(asset: StrategyBacktestAsset, qs: string) {
  const id = tasks.get(asset)!.id
  closeConnection(asset)
  const es = new EventSource(api.strategyBacktestStreamUrl(qs))
  connections.set(asset, es)
  let reconnectAttempts = 0

  const update = (patch: Partial<BacktestTask>) => {
    const current = tasks.get(asset)
    if (!current || current.id !== id) return false
    tasks.set(asset, { ...current, ...patch })
    emit()
    return true
  }
  const finish = () => {
    es.close()
    if (connections.get(asset) === es) connections.delete(asset)
    storage.backtestReconnect(asset).set(null)
  }

  es.onopen = () => {
    reconnectAttempts = 0
    if (tasks.get(asset)?.reconnecting) update({ reconnecting: false })
  }
  es.addEventListener('progress', (event: MessageEvent) => {
    if (tasks.get(asset)?.id !== id) return
    try {
      const progress = JSON.parse(event.data) as BacktestProgress
      reconnectAttempts = 0
      update({ progress, reconnecting: false })
    } catch { /* Wait for the next complete progress event. */ }
  })
  es.addEventListener('done', (event: MessageEvent) => {
    if (tasks.get(asset)?.id !== id) return
    try {
      const result = JSON.parse(event.data) as StrategyBacktestResult
      const resultAsset = result.config?.asset_type ?? 'stock'
      if (resultAsset !== asset) throw new Error('回测结果市场与请求不一致')
      update({ isPending: false, result, error: null, reconnecting: false })
    } catch (error) {
      update({ isPending: false, error: error instanceof Error ? error.message : '结果解析失败', reconnecting: false })
    }
    finish()
  })
  es.addEventListener('error', (event: MessageEvent) => {
    if (tasks.get(asset)?.id !== id) return
    if (event.data) {
      let message = '回测出错'
      try { message = JSON.parse(event.data)?.message ?? message } catch { /* Use the fallback message. */ }
      update({ isPending: false, error: message, reconnecting: false })
      finish()
      return
    }
    reconnectAttempts += 1
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      es.close()
      if (connections.get(asset) === es) connections.delete(asset)
      update({ isPending: false, error: '连接中断，可重连当前市场任务或重试。', reconnecting: false })
      return
    }
    update({ reconnecting: true })
  })
}

export function startBacktest(params: StrategyBacktestRequest): void {
  const asset = params.asset_type ?? 'stock'
  closeConnection(asset)
  const qs = api.strategyBacktestStreamUrl({ ...params, asset_type: asset }).split('?')[1]
  storage.backtestReconnect(asset).set(qs)
  tasks.set(asset, { id: ++taskSeq, assetType: asset, isPending: true, result: null, progress: null, error: null, reconnecting: false })
  emit()
  connectSSE(asset, qs)
}

export async function stopBacktest(asset: StrategyBacktestAsset = 'stock'): Promise<void> {
  const current = tasks.get(asset)
  const qs = storage.backtestReconnect(asset).get(null)
  if (!current?.isPending || !qs) return
  try {
    const response = await api.strategyBacktestCancel(qs)
    if (!response.ok) throw new Error(response.message ?? '无法确认任务取消')
  } catch (error) {
    if (tasks.get(asset)?.id === current.id && tasks.get(asset)?.isPending) {
      tasks.set(asset, { ...tasks.get(asset)!, error: '取消失败：' + (error instanceof Error ? error.message : String(error)) })
      emit()
    }
    return
  }
  if (tasks.get(asset)?.id !== current.id || !tasks.get(asset)?.isPending) return
  closeConnection(asset)
  tasks.set(asset, { ...current, isPending: false, error: '已取消', reconnecting: false })
  storage.backtestReconnect(asset).set(null)
  emit()
}

export function clearBacktest(asset: StrategyBacktestAsset = 'stock'): void {
  if (tasks.get(asset)?.isPending) return
  tasks.delete(asset)
  emit()
}

export function tryReconnect(asset: StrategyBacktestAsset = 'stock'): boolean {
  if (tasks.get(asset)?.isPending || tasks.get(asset)?.result) return true
  const qs = storage.backtestReconnect(asset).get(null)
  if (!qs) return false
  if ((new URLSearchParams(qs).get('asset_type') ?? 'stock') !== asset) {
    storage.backtestReconnect(asset).set(null)
    return false
  }
  tasks.set(asset, { id: ++taskSeq, assetType: asset, isPending: true, result: null, progress: null, error: null, reconnecting: false })
  emit()
  connectSSE(asset, qs)
  return true
}

export function useBacktestTask(asset: StrategyBacktestAsset = 'stock'): BacktestTask | null {
  return useSyncExternalStore(subscribe, () => tasks.get(asset) ?? null, getServerSnapshot)
}
