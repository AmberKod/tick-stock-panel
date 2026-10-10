/**
 * 「非真源 / 模型估算」的显式标注组件。
 *
 * 项目纪律: 凡是**不是行情源直接给的数**(模型推算、假设分摊、统计近似),
 * 必须在看到图的同一屏里看到这句话 —— 不能只写在 tooltip 里, 更不能不写。
 * 筹码图尤其危险: 它长得像"各价位的真实持仓成本", 但交易所根本不公开这个数据。
 *
 * 所以这里给两种强度:
 *   - <EstimateTag />    : 小徽章, 挂在按钮/标题旁, 让人在点开之前就知道性质。
 *   - <EstimateBanner /> : 整条警示栏, 摆在面板顶部, 说清「是什么 + 怎么算的 + 不能当什么」。
 */
import type { ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/cn'

/**
 * 小徽章。默认写「估算」, 调用方可以传更具体的词(如「模型」「均摊」)。
 * 用 amber(警告色)而不是 muted(次要色): 它是口径提示, 不是装饰。
 */
export function EstimateTag({
  children = '估算',
  title,
  className,
}: {
  children?: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span
      title={title ?? '这不是行情源直接给出的数据, 是按公开价量数据推算的模型结果'}
      className={cn(
        'inline-flex shrink-0 items-center rounded border border-amber-400/40 bg-amber-400/10 px-1 py-[1px]',
        'align-middle text-[9px] leading-none text-amber-500',
        className,
      )}
    >
      {children}
    </span>
  )
}

/**
 * 警示栏。三行结构: 性质 / 算法口径 / 使用边界。
 * 不做「我知道了」的关闭按钮 —— 关掉一次就看不到了, 而这行字每次看图都该看到。
 */
export function EstimateBanner({
  title = '模型估算 · 非交易所持仓数据',
  how,
  limit,
  className,
}: {
  /** 一句话定性 */
  title?: ReactNode
  /** 怎么算出来的(输入 + 假设) */
  how?: ReactNode
  /** 不能当什么用 */
  limit?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-start gap-1.5 rounded border border-amber-400/40 bg-amber-400/10 px-2 py-1.5',
        className,
      )}
    >
      <AlertTriangle className="mt-[1px] h-3.5 w-3.5 shrink-0 text-amber-500" />
      <div className="min-w-0 text-[10.5px] leading-relaxed">
        <div className="font-medium text-amber-500">{title}</div>
        {how && <div className="mt-0.5 text-fg-secondary">{how}</div>}
        {limit && <div className="mt-0.5 text-fg-muted">{limit}</div>}
      </div>
    </div>
  )
}

/** 图上淡淡的水印, 让截图/转发时也带着这个前提。 */
export function EstimateWatermark({ text = '模型估算' }: { text?: string }) {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden">
      <span className="select-none whitespace-nowrap text-[15px] font-semibold tracking-[0.3em] text-fg-muted/15">
        {text}
      </span>
    </div>
  )
}
