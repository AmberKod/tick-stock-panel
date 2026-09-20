import React from 'react'
import { Info } from 'lucide-react'
import { cn } from '@/lib/cn'

interface FeatureGateProps {
  /** 功能是否可用 */
  available: boolean
  /** 功能标题 */
  title: string
  /** 不可用时的说明文案 */
  reason?: string
  /** 子元素（可用时显示） */
  children: React.ReactNode
  /** 自定义禁用态样式 */
  disabledClassName?: string
  /** 是否显示禁用态虚线边框 */
  showBorder?: boolean
}

/**
 * 功能门控组件 — 用于处理港美股的制度差异。
 *
 * 当功能不可用时，显示带虚线边框的禁用态容器，说明原因。
 * 当功能可用时，正常渲染子元素。
 *
 * 使用场景：
 * - 涨停梯队（港美股无涨跌停制度）
 * - 连板数据（港美股无连板机制）
 * - 异动预警（港美股异动规则不同）
 *
 * 示例：
 * ```tsx
 * <FeatureGate
 *   available={market === 'cn'}
 *   title="涨停梯队"
 *   reason="该功能仅适用于有涨跌停限制的市场（港美股无涨跌停制度）"
 * >
 *   <LadderMini />
 * </FeatureGate>
 * ```
 */
export const FeatureGate: React.FC<FeatureGateProps> = ({
  available,
  title,
  reason,
  children,
  disabledClassName,
  showBorder = true,
}) => {
  if (available) {
    return <>{children}</>
  }

  return (
    <div
      className={cn(
        'relative rounded-lg bg-elevated/40',
        showBorder && 'border-2 border-dashed border-border/50',
        disabledClassName,
      )}
      role="region"
      aria-label={`${title}（不可用）`}
    >
      <div className="flex items-center justify-center gap-2 px-4 py-8">
        <Info className="h-5 w-5 shrink-0 text-muted/60" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-muted/90">{title} [已禁用]</div>
          {reason && <div className="mt-1 text-xs text-muted/60">{reason}</div>}
        </div>
      </div>
    </div>
  )
}