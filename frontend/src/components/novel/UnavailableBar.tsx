// 小说工作区 — 统一 unavailable 文案条
//
// PRD §7.4 的六场景文案集中在这里，禁止各组件自己编一套说法。
// 设计原则 P2（诚实不可用）：能力缺失/调用失败必须显式说明原因，
// 禁止静默空结果、禁止把失败包装成「暂无内容」。

import { AlertTriangle, Info, ShieldAlert, WifiOff } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

import { cn } from '@/lib/cn'

/** 后端错误码 → 展示口径。未覆盖的 code 走 `info` 通道，原样展示 message。 */
export type UnavailableCode =
  | 'ai_unavailable'
  | 'ai_error'
  | 'fact_parse_failed'
  | 'write_failed'
  | 'lint_hits'
  | 'generic'
  // 换元仿写域（backend/app/services/novel_store.py 新增的三个 422 码）
  | 'rewrite_source_rejected'
  | 'rewrite_gate_blocked'
  | 'rewrite_ack_required'

export interface UnavailableBarProps {
  code: UnavailableCode
  /** 具体原因 / 明细。ai_unavailable 未传时用 PRD 默认文案。 */
  message?: string
  /** 附加在文案右侧的操作区（如「重试」「去设置」） */
  action?: React.ReactNode
  className?: string
}

const ICONS: Record<UnavailableCode, LucideIcon> = {
  ai_unavailable: WifiOff,
  ai_error: AlertTriangle,
  fact_parse_failed: AlertTriangle,
  write_failed: AlertTriangle,
  lint_hits: Info,
  generic: Info,
  rewrite_source_rejected: ShieldAlert,
  rewrite_gate_blocked: ShieldAlert,
  rewrite_ack_required: ShieldAlert,
}

const TONES: Record<UnavailableCode, string> = {
  // 域色 #22c55e 只用于图标/状态点，不铺大面积背景 —— 提示条底色统一用中性色
  ai_unavailable: 'border-border bg-elevated text-secondary',
  ai_error: 'border-warning/40 bg-warning/10 text-foreground',
  fact_parse_failed: 'border-warning/40 bg-warning/10 text-foreground',
  write_failed: 'border-danger/40 bg-danger/10 text-foreground',
  lint_hits: 'border-border bg-elevated text-secondary',
  generic: 'border-border bg-elevated text-secondary',
  // 仿写域：预检拒绝 / 闸门未过 / 未完成勾选，都是「当前这一步被挡住」，
  // 不是系统故障 —— 一律走 warning 通道（不用 danger，避免被误读成数据丢了）。
  rewrite_source_rejected: 'border-warning/40 bg-warning/10 text-foreground',
  rewrite_gate_blocked: 'border-warning/40 bg-warning/10 text-foreground',
  rewrite_ack_required: 'border-warning/40 bg-warning/10 text-foreground',
}

const ICON_TONES: Record<UnavailableCode, string> = {
  ai_unavailable: 'text-muted',
  ai_error: 'text-warning',
  fact_parse_failed: 'text-warning',
  write_failed: 'text-danger',
  lint_hits: 'text-muted',
  generic: 'text-muted',
  rewrite_source_rejected: 'text-warning',
  rewrite_gate_blocked: 'text-warning',
  rewrite_ack_required: 'text-warning',
}

const DEFAULT_MESSAGES: Record<UnavailableCode, string> = {
  // PRD §7.4 场景 1（未配置 AI Key）
  ai_unavailable:
    'AI 网关未配置 — 续写/润色不可用。前往「设置 · AI」配置后自动解锁。写稿、大纲、导出不受影响。',
  // 场景 3（AI 调用失败）
  ai_error: '本次生成失败。已完成的步骤会保留，可重试失败的那一步。',
  // 场景 4（事实快照解析失败）
  fact_parse_failed: 'AI 返回的事实快照无法解析，未写入追踪态 — 正文不受影响。',
  // 场景 6（文件写入失败）
  write_failed: '保存失败。你的改动仍在编辑器中，未落盘。',
  // 场景 5（写后自检命中）
  lint_hits: '自检提醒 — 仅提醒，未改写你的文字。',
  generic: '该能力当前不可用。',
  // 仿写域默认文案（具体原因由调用方传 message 覆盖 —— 后端给的更准）
  rewrite_source_rejected:
    '输入疑似原文，已被预检拒绝（未保存）。请改写成结构笔记后重试 —— 本工具不接收原文。',
  rewrite_gate_blocked:
    'L3 关系拓扑与 L5 桥段序列是命门层，未填表不能发起生成。补齐后可继续。',
  rewrite_ack_required:
    '采纳前置条件未满足：硬阻断项未清零，或仍有待核项未勾选，或反向三问未勾完。',
}

export function UnavailableBar({ code, message, action, className }: UnavailableBarProps) {
  const Icon = ICONS[code]
  const text = message?.trim() ? message : DEFAULT_MESSAGES[code]
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-btn border px-2.5 py-2 text-xs leading-relaxed',
        TONES[code],
        className,
      )}
      role="status"
      aria-live="polite"
    >
      <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', ICON_TONES[code])} aria-hidden="true" />
      <span className="min-w-0 flex-1">{text}</span>
      {action ? <span className="shrink-0">{action}</span> : null}
    </div>
  )
}
