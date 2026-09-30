// 小说工作区 — AI 任务轮询 hook
//
// 权威状态在磁盘（后端每步都原子写 checkpoints/<job_id>.json），所以刷新页面 /
// 重开浏览器后只要拿到 job_id 就能继续轮询，不需要任何内存态。
//
// 终态集合固定 {done, failed, cancelled}：进入终态立即停轮询，不空转。

import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { novelApi } from './novelApi'
import { QK } from './queryKeys'
import type { JobStatus, NovelJob } from './novelTypes'

/** 终态集合 —— 与后端 `novel_jobs.TERMINAL_JOB_STATUSES` 严格一致。 */
export const NOVEL_JOB_TERMINAL: readonly JobStatus[] = ['done', 'failed', 'cancelled'] as const

export function isJobTerminal(status: JobStatus | undefined | null): boolean {
  return !!status && NOVEL_JOB_TERMINAL.includes(status)
}

/** 轮询间隔（ms） */
const POLL_INTERVAL_MS = 1500

export interface UseNovelJobResult {
  job: NovelJob | null
  /** 是否仍在轮询中（非终态） */
  polling: boolean
  query: UseQueryResult<NovelJob, Error>
}

/**
 * 轮询单个 job。jobId 为 null 时不发请求。
 *
 * 后端把 resume/cancel 的返回值直接交还给调用方；调用方如需立刻恢复轮询，
 * 用 `queryClient.setQueryData(QK.novelJob(jobId), 新job)` 写回缓存即可
 * （终态 → queued 会让下面的 refetchInterval 重新生效）。
 */
export function useNovelJob(jobId: string | null): UseNovelJobResult {
  const query = useQuery<NovelJob, Error>({
    queryKey: QK.novelJob(jobId ?? ''),
    queryFn: () => novelApi.getJob(jobId as string),
    enabled: !!jobId,
    // 权威数据在磁盘且每步都在变，禁止缓存兜底
    staleTime: 0,
    gcTime: 5 * 60 * 1000,
    refetchInterval: (q) => {
      const status = q.state.data?.status
      if (isJobTerminal(status)) return false
      return POLL_INTERVAL_MS
    },
    // 失败时不要无限重试到刷屏；轮询本身会再拉
    retry: 1,
  })

  const job = query.data ?? null
  return {
    job,
    polling: !!jobId && !isJobTerminal(job?.status),
    query,
  }
}
