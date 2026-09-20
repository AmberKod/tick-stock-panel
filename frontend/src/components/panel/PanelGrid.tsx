import type { ReactNode } from 'react'
import { cn } from '@/lib/cn'

// ============================================================================
// 面板网格 (PanelGrid)
// ----------------------------------------------------------------------------
// 12 列 CSS grid + span 常量。**顺序在代码里定死, 无持久化、无拖拽、无 resize。**
//
// 断点对齐现有页面 (Dashboard.tsx:402/405/420/422, HKUSMarketOverview.tsx:143+):
//   base(1 列) → sm(2 列) → lg(12 列)
// 刻意不引入 react-grid-layout 那套「12 列像素模型」, 避免与现有
// `xl:grid-cols-[minmax(0,1fr)_20rem]` 右栏布局冲突(架构评估 §2.1)。
//
// 用法:
//   <PanelGrid>
//     <Panel className={PANEL_SPAN.quarter} title="涨跌分布" />
//     <Panel className={PANEL_SPAN.twoThirds} title="异动" />
//   </PanelGrid>
// ============================================================================

/** 网格容器基准 class: 1 列 → sm 2 列 → lg 12 列。 */
export const PANEL_GRID_CLASS = 'grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-12' as const

/**
 * span 常量。每个值都自带三档断点, 直接塞进 <Panel className>。
 * 命名按「在 lg 下占 12 列中的几列」理解。
 */
export const PANEL_SPAN = {
  /** 12/12 整行 */
  full: 'col-span-1 sm:col-span-2 lg:col-span-12',
  /** 8/12 主区(配 sidebar 组成 8+4) */
  main: 'col-span-1 sm:col-span-2 lg:col-span-8',
  /** 6/12 半行 */
  half: 'col-span-1 sm:col-span-2 lg:col-span-6',
  /** 4/12 三分一(也用作右栏) */
  third: 'col-span-1 sm:col-span-2 lg:col-span-4',
  /** 3/12 四分一 */
  quarter: 'col-span-1 sm:col-span-1 lg:col-span-3',
  /** 2/12 六分一(窄指标位) */
  sixth: 'col-span-1 sm:col-span-1 lg:col-span-2',
} as const

/** span 取值联合类型。 */
export type PanelSpan = keyof typeof PANEL_SPAN

/** 取 span class。传非法 key 抛错, 不静默退化。 */
export function panelSpan(span: PanelSpan): string {
  const cls = PANEL_SPAN[span]
  if (!cls) throw new Error(`未知的面板 span: ${span}`)
  return cls
}

export interface PanelGridProps {
  children: ReactNode
  /** 附加 class(如需改 gap 或加 margin, twMerge 会覆盖基准) */
  className?: string
}

/**
 * 12 列面板网格容器。只负责排布, 不持久化顺序、不支持拖拽。
 * 子项自行带 `className={PANEL_SPAN.xxx}` 决定占几列。
 */
export function PanelGrid({ children, className }: PanelGridProps) {
  return <div className={cn(PANEL_GRID_CLASS, className)}>{children}</div>
}
