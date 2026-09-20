import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { ChevronDown, CloudOff, Inbox, RefreshCw, ShieldAlert } from 'lucide-react'
import { cn } from '@/lib/cn'

// ============================================================================
// 面板容器契约 (Panel)
// ----------------------------------------------------------------------------
// 统一承载: 标题 / 图标 / loading / error / 空态 / **不可用态** / header 右 slot /
//           可展开判据(evidence) / 刷新失败降级提示(stale)。
//
// 项目铁律: 数据不可得时必须显示「不可用」, **绝不能渲染成一片风平浪静的 0**。
// 因此 `unavailable` 命中时本组件**完全不渲染 children 与 footer** —— 调用方不需要自己判空。
// footer 常承载「分母 x/4」「口径 as-of」「时效 xh」这类**对数据的解读**, 与 children
// 同状态门控: 数据还没回来时它们会落成 0/4、— 等假定案数字, 必须一并隐藏。
//
// 刻意不做(架构评估 §2 / §3 已明确否决): 拖拽、resize、折叠、span 调整、布局持久化。
// 那是为多租户 SaaS 设计的, 我们是单用户本地终端, 做这些是零业务价值的框架成本。
// ============================================================================

/** 卡片外壳样式: 取自 Dashboard.tsx:423 / HKUSMarketOverview.tsx:159(两者原为逐字节相同)。 */
export const PANEL_SHELL_CLASS =
  'rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]' as const

/** 面板状态。`ok` 之外都不渲染 children 与 footer(除 loading 骨架外)。 */
export type PanelStatus = 'ok' | 'loading' | 'error' | 'unavailable' | 'empty'

/** 可展开判据: 面板 1 用它回答「为什么判防守」。 */
export interface PanelEvidence {
  /** 折叠头文案, 默认「判据」 */
  label?: string
  /** 逐条判据。空数组不渲染。 */
  items: readonly string[]
  /** 是否默认展开, 默认 false */
  defaultOpen?: boolean
}

export interface PanelProps {
  /** 面板标题(必填, 同时用作 aria-label) */
  title: string
  /** 标题左侧图标 */
  icon?: LucideIcon
  /** 标题右侧补充信息: 数量 / 时间 / 口径 */
  hint?: ReactNode
  /** header 最右侧 slot: 跳转链接、市场切换、时间范围等 */
  actions?: ReactNode
  /** 显式指定状态; 不传则按 loading → error → unavailable → empty 顺序推断 */
  status?: PanelStatus
  /** 加载中 */
  loading?: boolean
  /** 错误: Error 对象或文案。与 `stale` 互斥含义 —— 有 error 且无数据时用这个 */
  error?: Error | string | null
  /** 不可用: `true` 或不可用的原因。数据不可得时必须传 */
  unavailable?: boolean | string
  /** 空态: `true` 或空态文案。语义是「真的没有」, 与 unavailable 视觉上明确区分 */
  empty?: boolean | string
  /** 本次刷新失败、仍展示上一次成功数据时的降级提示(沿用 HKUSMarketOverview.tsx:241) */
  stale?: boolean | string
  /** 可展开判据, 展示在面板底部 */
  evidence?: PanelEvidence
  /** 重试回调。传了才在 error / unavailable 态显示重试按钮 */
  onRetry?: () => void
  /** 附加在外壳上的 class(可配合 `PANEL_SPAN.xxx` 控制占几列) */
  className?: string
  /** 附加在内容区上的 class */
  bodyClassName?: string
  /**
   * 底部固定区(图例 / 口径说明)。
   *
   * 与 `children` **同状态门控**: 只有 `ok` 才渲染。footer 里通常是「分母 3/4」
   * 「口径 2026-05-01」「时效 2.3h」这类对数据的解读 —— loading / error /
   * unavailable 时它们会退化成 `0/4`、`—`, 语义上就是假定案, 必须一起隐藏。
   * `stale` 态 resolved 仍是 `ok`(有上一次成功数据), footer 照常显示。
   */
  footer?: ReactNode
  /** 面板内容。unavailable / error / empty / loading 态下不渲染 */
  children?: ReactNode
}

/** 把 `boolean | string` 归一成文案; false/undefined → null。 */
function toMessage(value: boolean | string | undefined, fallback: string): string | null {
  if (value === undefined || value === false) return null
  return typeof value === 'string' ? value : fallback
}

/** 把 error 归一成文案。 */
function toErrorText(error: Error | string | null | undefined): string | null {
  if (!error) return null
  return typeof error === 'string' ? error : error.message || '请求失败'
}

/**
 * 面板容器。
 *
 * 状态优先级: 显式 `status` > `loading` > `error` > `unavailable` > `empty` > `ok`。
 * 只有 `ok` 才渲染 `children` 与 `footer` —— 保证「不可用」永远不会被误渲染成 0。
 */
export function Panel({
  title,
  icon: Icon,
  hint,
  actions,
  status,
  loading = false,
  error = null,
  unavailable = false,
  empty = false,
  stale = false,
  evidence,
  onRetry,
  className,
  bodyClassName,
  footer,
  children,
}: PanelProps) {
  const errorMsg = toErrorText(error)
  const unavailableMsg = toMessage(unavailable, '该维度数据当前不可得, 未纳入判断。')
  const emptyMsg = toMessage(empty, '当前口径下没有匹配的数据。')
  const staleMsg = toMessage(stale, '本次刷新失败, 当前继续展示最近一次成功数据。')

  const resolved: PanelStatus =
    status ??
    (loading
      ? 'loading'
      : errorMsg
        ? 'error'
        : unavailableMsg
          ? 'unavailable'
          : emptyMsg
            ? 'empty'
            : 'ok')

  const isUnavailable = resolved === 'unavailable'

  return (
    <section
      aria-label={title}
      aria-busy={resolved === 'loading'}
      className={cn(PANEL_SHELL_CLASS, isUnavailable && 'border-dashed border-warning/40', className)}
    >
      {/* ---- header: 图标 + 标题 + hint + 右侧 slot ---- */}
      <header className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="h-3 w-0.5 shrink-0 rounded-full bg-gradient-to-b from-accent to-accent/30" />
          {Icon && <Icon className="h-3.5 w-3.5 shrink-0 text-accent" />}
          <h2 className="truncate text-xs font-semibold text-foreground">{title}</h2>
          {isUnavailable && (
            <span className="shrink-0 rounded bg-warning/10 px-1 py-px text-[9px] font-medium text-warning">
              不可用
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {hint && <span className="font-mono text-[10px] text-muted">{hint}</span>}
          {actions}
        </div>
      </header>

      {/* ---- 刷新失败但仍有一次成功数据: 顶部降级提示(内容照常展示) ---- */}
      {staleMsg && resolved === 'ok' && (
        <div className="mb-2 border border-warning/40 bg-warning/5 px-2 py-1 text-[10px] leading-relaxed text-warning">
          {staleMsg}
        </div>
      )}

      {/* ---- body: 五态互斥 ---- */}
      <div className={cn('min-w-0', bodyClassName)}>
        {resolved === 'loading' && (
          <div className="space-y-2" aria-busy="true">
            <div className="h-3 w-1/3 animate-pulse rounded bg-elevated" />
            <div className="h-20 w-full animate-pulse rounded bg-elevated/70" />
            <div className="h-3 w-1/2 animate-pulse rounded bg-elevated/60" />
          </div>
        )}

        {resolved === 'error' && (
          <PanelNotice tone="error" icon={ShieldAlert} title="加载失败" message={errorMsg ?? '请求失败'} onRetry={onRetry} />
        )}

        {/* 不可用: 虚线框 + 橙色, 与「空」明确区分 —— 拿不到 ≠ 没有 */}
        {isUnavailable && (
          <PanelNotice tone="unavailable" icon={CloudOff} title="不可用" message={unavailableMsg ?? ''} onRetry={onRetry} />
        )}

        {/* 空: 中性灰 —— 真的没有 */}
        {resolved === 'empty' && (
          <PanelNotice tone="empty" icon={Inbox} title="暂无数据" message={emptyMsg ?? ''} />
        )}

        {/* 只有 ok 才渲染内容 —— 铁律落点 */}
        {resolved === 'ok' && children}
      </div>

      {/* ---- 可展开判据 ---- */}
      {evidence && evidence.items.length > 0 && (
        <details
          open={evidence.defaultOpen === true}
          className="group mt-2 border-t border-border pt-1.5 [&::-webkit-details-marker]:hidden"
        >
          <summary className="flex cursor-pointer list-none items-center gap-1 text-[10px] text-muted transition-colors hover:text-secondary">
            <ChevronDown className="h-3 w-3 transition-transform duration-200 group-open:rotate-180" />
            {evidence.label ?? '判据'}
            <span className="font-mono text-[9px] text-muted">({evidence.items.length})</span>
          </summary>
          <ul className="mt-1 space-y-0.5 pl-4 text-[10px] leading-relaxed text-secondary">
            {evidence.items.map((item, index) => (
              <li key={`${index}-${item}`} className="list-disc">
                {item}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* ---- footer: 与 children 同状态门控(铁律) ----
           footer 放的是对数据的解读, 非 ok 态下一律不渲染 —— 否则 loading 期间会
           输出「分母 0/4 · 口径 — · 时效 —」这种看起来像定案、实际是缺省值的假信息。
           注意 stale 态 resolved 仍为 ok, 这里不会被误伤。 */}
      {footer && resolved === 'ok' && <div className="mt-2 border-t border-border pt-1.5">{footer}</div>}
    </section>
  )
}

type NoticeTone = 'error' | 'unavailable' | 'empty'

interface PanelNoticeProps {
  tone: NoticeTone
  icon: LucideIcon
  title: string
  message: string
  onRetry?: () => void
}

const NOTICE_TONE_CLASS: Record<NoticeTone, string> = {
  // 错误: 红/危险色
  error: 'border-danger/40 bg-danger/5 text-danger',
  // 不可用: 橙/警示色 + 虚线 —— 语义是"拿不到", 不是"没有"
  unavailable: 'border-dashed border-warning/50 bg-warning/5 text-warning',
  // 空: 中性 —— 语义是"真的没有"
  empty: 'border-border bg-elevated/40 text-secondary',
}

const NOTICE_ICON_CLASS: Record<NoticeTone, string> = {
  error: 'text-danger',
  unavailable: 'text-warning',
  empty: 'text-muted',
}

/** 面板内部的非内容态展示块(不可用 / 错误 / 空)。 */
function PanelNotice({ tone, icon: Icon, title, message, onRetry }: PanelNoticeProps) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-1.5 rounded-lg border px-3 py-6 text-center', NOTICE_TONE_CLASS[tone])}>
      <Icon className={cn('h-5 w-5', NOTICE_ICON_CLASS[tone])} strokeWidth={1.5} />
      <div className="text-xs font-medium">{title}</div>
      {message && <p className="max-w-xs text-[10px] leading-relaxed text-muted">{message}</p>}
      {onRetry && tone !== 'empty' && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-0.5 inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-[10px] text-secondary transition-colors hover:text-accent hover:border-accent/40"
        >
          <RefreshCw className="h-3 w-3" />
          重试
        </button>
      )}
    </div>
  )
}
