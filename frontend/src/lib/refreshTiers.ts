// ============================================================================
// 刷新档位常量表 (T0 ~ T4)
// ----------------------------------------------------------------------------
// 目的: 终结「40 处 refetchInterval 各填各的数字」(架构评估 §1.3)。
// 纪律: 面板/页面一律引用档位名, 禁止在组件里写裸数字。
//
// 机制背景: 主机制是 SSE invalidation (lib/useQuoteStream.ts), 轮询只是兜底。
//   - T0 走 SSE, refetchInterval 必须为 false, 不能再叠一层轮询;
//   - T3 (日级) 一律不进 SSE_INVALIDATE_PREFIXES (lib/queryKeys.ts),
//     否则会被 1s/次的 quotes_updated 打中, 重演「每秒重拉日K」的踩坑。
//
// 另: 每个面板还要配 placeholderData: (prev) => prev (Dashboard.tsx:185 已在用),
//     保证刷新期间容器不卸载、不闪烁 —— 这条由调用方负责, 不在本表内。
// ============================================================================

/** 刷新档位。数字越小越实时。 */
export type RefreshTier = 'T0' | 'T1' | 'T2' | 'T3' | 'T4'

/** 单个档位的完整定义。 */
export interface RefreshTierSpec {
  /** 档位名 */
  tier: RefreshTier
  /** 中文语义, 可直接用于 UI 提示 */
  label: string
  /** TanStack Query `staleTime`(ms) */
  staleTimeMs: number
  /**
   * TanStack Query `refetchInterval`(ms)。
   * `false` = 不轮询, 完全交给 SSE invalidation。
   */
  refetchIntervalMs: number | false
  /** 是否由 SSE 驱动。true 时禁止再配轮询。 */
  sse: boolean
  /** 适用数据(举例), 新增面板时照此归类 */
  scope: string
}

/** 档位表。所有数字只允许在这里出现。 */
export const REFRESH_TIERS: Record<RefreshTier, RefreshTierSpec> = {
  // 实时: A 股盘中, SSE `quotes_updated` → invalidate(前缀已在 queryKeys.ts)
  T0: { tier: 'T0', label: '实时', staleTimeMs: 5_000, refetchIntervalMs: false, sse: true, scope: 'A股 overview-market / index-quotes / quote-status' },
  // 盘中: 秒级无意义、分钟级要跟上的信号类
  T1: { tier: 'T1', label: '盘中', staleTimeMs: 10_000, refetchIntervalMs: 15_000, sse: false, scope: 'alerts / limit-ladder / abnormal-overview' },
  // 慢快照: 港美日K盘后口径, 盘中不会变
  T2: { tier: 'T2', label: '慢快照', staleTimeMs: 30_000, refetchIntervalMs: 60_000, sse: false, scope: 'hk-us-overview' },
  // 日级: 结论类/重算代价高, 一天内基本不变
  T3: { tier: 'T3', label: '日级', staleTimeMs: 120_000, refetchIntervalMs: 300_000, sse: false, scope: 'regime-latest / hotspots / overview-posture' },
  // 元数据: 数据源健康度、抓取状态等轻量状态位
  T4: { tier: 'T4', label: '元数据', staleTimeMs: 15_000, refetchIntervalMs: 30_000, sse: false, scope: 'data-freshness / news-status' },
} as const

/** 档位顺序(由快到慢), 供 UI 下拉/排序使用。 */
export const REFRESH_TIER_ORDER: readonly RefreshTier[] = ['T0', 'T1', 'T2', 'T3', 'T4'] as const

/** 取档位定义。传非法值会抛错, 避免静默退化成默认值。 */
export function getTier(tier: RefreshTier): RefreshTierSpec {
  const spec = REFRESH_TIERS[tier]
  if (!spec) throw new Error(`未知的刷新档位: ${tier}`)
  return spec
}

/** 便捷取法: `staleTime: tierStaleTime('T2')` */
export function tierStaleTime(tier: RefreshTier): number {
  return getTier(tier).staleTimeMs
}

/** 便捷取法: `refetchInterval: tierRefetchInterval('T2')` */
export function tierRefetchInterval(tier: RefreshTier): number | false {
  return getTier(tier).refetchIntervalMs
}

/** 一次拿全 useQuery 需要的刷新参数(不含 placeholderData)。 */
export function tierQueryOptions(tier: RefreshTier): { staleTime: number; refetchInterval: number | false } {
  const spec = getTier(tier)
  return { staleTime: spec.staleTimeMs, refetchInterval: spec.refetchIntervalMs }
}
