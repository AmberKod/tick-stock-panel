// 小说工作区 · 换元仿写 — 风险告知卡（**不可关闭、不可折叠到消失**）
//
// 三条硬约束（PRD §2.5 / 主理人裁定）：
//   1. **免责声明物理单点**：文案只来自后端 `GET .../rewrite/disclaimer`
//      或报告里的 `report.disclaimer`。本文件**不写任何免责措辞** ——
//      唯一的自撰字符串是 `DISCLAIMER_LOAD_FAILED`，而它是「状态句」（声明没取到），
//      不是声明本身。
//   2. 卡片**没有关闭按钮**：用户可以折叠详情，但折叠态仍保留一行摘要，
//      绝不让它从视野里消失。
//   3. 反向校验三问是**纯人工项**，没有默认值、不预勾选 —— 预勾选等于替用户签字。

import { ChevronDown, ChevronUp, RefreshCw, ShieldAlert } from 'lucide-react'
import { useState } from 'react'

import { cn } from '@/lib/cn'
import type { Disclaimer, ReverseQuestion } from '@/lib/novelTypes'

/**
 * 取不到声明时的**状态句**。
 *
 * ★这不是免责声明的替代文案★ —— 它只说明「声明没加载出来」。
 * 之所以不能在这里备一份声明：生成前恰恰是告知最该生效的时机，
 * 备一份就意味着将来改后端文案时前端会静默过期。
 */
const DISCLAIMER_LOAD_FAILED =
  '免责声明加载失败，请重试。在拿到后端声明之前，本页不展示任何替代文案 —— 声明只有后端单点来源。'

export interface RiskNoticeCardProps {
  /** 后端单点下发的免责声明；null = 还没取到 / 取失败了 */
  disclaimer: Disclaimer | null
  /** 反向校验三问（纯人工项） */
  reverse: ReverseQuestion[]
  /** 采纳前的风险确认勾选（与后端 `ack` 是两件事：这里是 UI 侧闸门） */
  riskAck: boolean
  onRiskAckChange: (value: boolean) => void
  onToggleReverse: (index: number, checked: boolean) => void
  /** 声明取失败时的重试入口（由父级 invalidate 该查询） */
  onRetryDisclaimer?: () => void
  className?: string
}

export function RiskNoticeCard({
  disclaimer,
  reverse,
  riskAck,
  onRiskAckChange,
  onToggleReverse,
  onRetryDisclaimer,
  className,
}: RiskNoticeCardProps) {
  const [collapsed, setCollapsed] = useState(false)

  return (
    <section
      className={cn('rounded-card border border-warning/40 bg-warning/5', className)}
      aria-label="风险告知"
    >
      {/* 抬头：常驻。折叠只是收起正文，摘要行永远在。 */}
      <div className="flex items-start gap-2 border-b border-warning/30 px-2.5 py-2">
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
        <span className="min-w-0 flex-1 text-xs font-medium text-foreground">
          风险告知 · 不可关闭
        </span>
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          className="inline-flex shrink-0 items-center gap-0.5 rounded-btn px-1 py-0.5 text-[11px] text-muted transition-colors hover:text-foreground"
          aria-expanded={!collapsed}
        >
          {collapsed ? (
            <>
              展开 <ChevronDown className="h-3 w-3" aria-hidden="true" />
            </>
          ) : (
            <>
              收起 <ChevronUp className="h-3 w-3" aria-hidden="true" />
            </>
          )}
        </button>
      </div>

      {collapsed ? (
        // 折叠态：一句话摘要仍在（不是空、不是「已隐藏」）。
        // 措辞同样只做「指路」，不复述声明内容 —— 展开后才是后端原文。
        <p className="px-2.5 py-2 text-[11px] leading-relaxed text-secondary">
          风险告知已收起：展开可看后端下发的免责声明与反向三问；未展开时不要勾选下方确认。
        </p>
      ) : (
        <div className="space-y-2 px-2.5 py-2">
          {disclaimer ? (
            <>
              <p className="text-[11px] leading-relaxed text-secondary">{disclaimer.text}</p>
              {disclaimer.version ? (
                <p className="font-mono text-[10px] text-muted">
                  声明版本 {disclaimer.version}（后端单点下发）
                </p>
              ) : null}
            </>
          ) : (
            <div className="space-y-1">
              <p className="text-[11px] leading-relaxed text-warning">{DISCLAIMER_LOAD_FAILED}</p>
              {onRetryDisclaimer ? (
                <button
                  type="button"
                  onClick={onRetryDisclaimer}
                  className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-1.5 py-0.5 text-[10px] text-foreground transition-colors hover:border-accent/40"
                >
                  <RefreshCw className="h-3 w-3" aria-hidden="true" />
                  重试
                </button>
              ) : null}
            </div>
          )}

          {/* 反向校验三问 —— 纯人工，不预勾选 */}
          <div className="space-y-1.5 border-t border-warning/20 pt-2">
            <div className="text-[11px] font-medium text-foreground">反向校验三问（人工核对）</div>
            {reverse.length === 0 ? (
              <p className="text-[11px] text-muted">本次报告未附带反向三问。</p>
            ) : (
              reverse.map((question, index) => (
                <label
                  key={`${question.q}-${index}`}
                  className="flex cursor-pointer items-start gap-2 text-[11px] leading-relaxed text-secondary"
                >
                  <input
                    type="checkbox"
                    checked={question.human_checked}
                    onChange={(e) => onToggleReverse(index, e.target.checked)}
                    className="mt-0.5 h-3 w-3 shrink-0 accent-warning"
                  />
                  <span className="min-w-0 flex-1">
                    {index + 1}. {question.q}
                    <span className="ml-1 text-muted">（预期答案：{question.expect}）</span>
                  </span>
                </label>
              ))
            )}
          </div>

          {/* 采纳前的风险确认 */}
          <label className="flex cursor-pointer items-start gap-2 border-t border-warning/20 pt-2 text-[11px] leading-relaxed text-secondary">
            <input
              type="checkbox"
              checked={riskAck}
              onChange={(e) => onRiskAckChange(e.target.checked)}
              className="mt-0.5 h-3 w-3 shrink-0 accent-warning"
            />
            <span className="min-w-0 flex-1">
              我已逐条看过上面的命中项与人工核对项，理解这份报告只提示风险、不替代我自己的判断。
            </span>
          </label>
        </div>
      )}
    </section>
  )
}
