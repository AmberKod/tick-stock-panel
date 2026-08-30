import { Link } from 'react-router-dom'
import {
  Newspaper,
  Image as ImageIcon,
  BookOpen,
  Clapperboard,
  ArrowLeft,
  CheckCircle2,
  Sparkles,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Logo } from '@/components/Logo'

/**
 * 新工作区占位页（Phase 0 壳改造）。
 * 设计原则：诚实占位 — 明确告知「已纳入路线图、第几阶段上线」，
 * 展示该维度的 MVP 边界，避免「半成品空页」的观感。
 * 各工作区正式落地（Phase 2~5）时整页替换。
 */

interface FeatureItem {
  name: string
  desc: string
}

interface WorkspacePlaceholderProps {
  icon: LucideIcon
  title: string
  tagline: string
  color: string
  phase: string
  features: FeatureItem[]
  /** 与其他工作区的联动说明（多维工作台差异化卖点） */
  synergy?: string
}

function WorkspacePlaceholder({ icon: Icon, title, tagline, color, phase, features, synergy }: WorkspacePlaceholderProps) {
  return (
    <div className="h-full overflow-auto bg-base">
      <div className="mx-auto flex min-h-full w-full max-w-2xl flex-col justify-center px-6 py-16">
        {/* 域标识 */}
        <div className="flex flex-col items-center text-center">
          <div
            className="flex h-16 w-16 items-center justify-center rounded-2xl"
            style={{ background: `${color}1f`, boxShadow: `0 0 32px ${color}26` }}
            aria-hidden="true"
          >
            <Icon className="h-8 w-8" style={{ color }} />
          </div>
          <h1 className="mt-5 text-2xl font-bold tracking-tight text-foreground">{title}工作区</h1>
          <p className="mt-2 text-sm leading-relaxed text-secondary">{tagline}</p>
          <span
            className="mt-4 inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-medium"
            style={{ borderColor: `${color}55`, background: `${color}14`, color }}
          >
            <Sparkles className="h-3 w-3" aria-hidden="true" />
            已纳入工作台路线图 · {phase} 上线
          </span>
        </div>

        {/* MVP 能力预告 */}
        <div className="mt-10">
          <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted/80">
            <span>规划能力（MVP）</span>
            <span className="h-px flex-1 bg-border/50" aria-hidden="true" />
          </div>
          <ul className="grid gap-2 sm:grid-cols-2">
            {features.map(f => (
              <li
                key={f.name}
                className="flex items-start gap-2.5 rounded-card border border-border/70 bg-surface/60 p-3.5 backdrop-blur-sm"
              >
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" style={{ color }} aria-hidden="true" />
                <div className="min-w-0">
                  <div className="text-sm font-medium text-foreground">{f.name}</div>
                  <div className="mt-0.5 text-xs leading-relaxed text-muted">{f.desc}</div>
                </div>
              </li>
            ))}
          </ul>
        </div>

        {/* 跨域联动说明 */}
        {synergy && (
          <div className="mt-4 flex items-start gap-2.5 rounded-card border border-dashed border-border p-3.5 text-xs leading-relaxed text-secondary">
            <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
            <span>{synergy}</span>
          </div>
        )}

        {/* 返回股票工作区 */}
        <div className="mt-10 flex flex-col items-center gap-3">
          <Link
            to="/"
            className="inline-flex items-center gap-2 rounded-btn border border-border bg-surface px-4 py-2 text-sm text-secondary transition-colors hover:border-accent/40 hover:bg-elevated hover:text-foreground"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
            返回股票看板
          </Link>
          <div className="flex items-center gap-1.5 text-[10px] text-muted/80">
            <Logo size={12} />
            <span>多维工作台 · Phase 0 壳已就绪</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export function NewsWorkspace() {
  return (
    <WorkspacePlaceholder
      icon={Newspaper}
      title="热点"
      tagline="聚合热点资讯流，AI 一键摘要，与股票域概念/个股深度联动"
      color="#f97316"
      phase="Phase 2"
      features={[
        { name: '热点流', desc: '按时间/热度聚合公开热点，数据源可插拔' },
        { name: 'AI 摘要', desc: '每条热点生成要点摘要，快速过滤噪音' },
        { name: '概念联动', desc: '热点关联概念板块与个股，一键跳转分析' },
        { name: '今日流接入', desc: '热点摘要进入跨域聚合首屏' },
      ]}
      synergy="差异化亮点：热点模块不是孤岛 —— 热点事件可直接喂给股票域的「概念热度」，这是纯资讯工具做不到的联动。"
    />
  )
}

export function ImageWorkspace() {
  return (
    <WorkspacePlaceholder
      icon={ImageIcon}
      title="图片"
      tagline="生成式图片创作：提示词任务 → 统一任务中心 → 资产库画廊"
      color="#a855f7"
      phase="Phase 3"
      features={[
        { name: '生图任务', desc: '提示词 + 尺寸 + 风格预设，走统一任务中心' },
        { name: '画廊管理', desc: '网格浏览 + Lightbox 预览，生成物自动入库' },
        { name: '打标溯源', desc: '保留 prompt / 模型 / 来源，可检索可复用' },
        { name: '跨域引用', desc: '小说封面、视频封面帧直接引用资产库' },
      ]}
    />
  )
}

export function NovelWorkspace() {
  return (
    <WorkspacePlaceholder
      icon={BookOpen}
      title="小说"
      tagline="本地优先的写作台：书架 / 大纲树 / 章节编辑 + AI 续写"
      color="#22c55e"
      phase="Phase 4"
      features={[
        { name: '书架与大纲', desc: '每本书独立目录，大纲树 + 章节 Markdown 管理' },
        { name: '章节编辑器', desc: 'Markdown 编辑，天然可 git、可 diff' },
        { name: 'AI 续写/润色', desc: '走统一 AI 网关，按大纲上下文续写' },
        { name: '导出', desc: '章节产物支持导出 Markdown / 文本' },
      ]}
      synergy="本地优先：小说稿是纯 Markdown + JSON 大纲，数据始终在你自己的 data/ 目录里，任何编辑器都能打开。"
    />
  )
}

export function VideoWorkspace() {
  return (
    <WorkspacePlaceholder
      icon={Clapperboard}
      title="视频"
      tagline="文本/图片 → 视频生成任务：预览、下载、封面帧引用"
      color="#f43f5e"
      phase="Phase 5"
      features={[
        { name: '生成任务', desc: '文本/图片驱动，外部模型 API 适配器接入' },
        { name: '统一任务中心', desc: '与其他生成任务共享进度/取消/历史体验' },
        { name: '预览与下载', desc: '资产库在线预览，一键下载到本地' },
        { name: '封面帧引用', desc: '从图片工作区资产库引用封面帧' },
      ]}
      synergy="克制边界：MVP 只做单段生成 + 预览 + 下载，不做剪辑时间线 —— 那是专业剪辑工具的事。"
    />
  )
}
