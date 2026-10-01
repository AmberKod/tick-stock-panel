// 小说工作区 · 换元仿写 — 结构蓝图编辑表单
//
// 从 RewritePanel 拆出来的原因：蓝图是**五层结构笔记**，字段多（来源标注、
// 功能位、L1/L2 黑名单、L3 两张关系图、L5 两条桥段序列、抽象层节拍），
// 混在主面板里会把「生成 / 质检 / 采纳」这条主线淹掉。
//
// 一条硬纪律：**这里只收结构笔记，不收原文**。粘贴原文会在保存时被后端
// 预检（R-len / R-quote / R-para）以 422 `rewrite_source_rejected` 拒绝，
// 一个字节都不落盘 —— 前端不替后端做这个判断，只在文案上明确告知。

import { Loader2, Save, ShieldCheck } from 'lucide-react'

import { cn } from '@/lib/cn'
import type {
  Blueprint,
  FunctionSlot,
  PrecheckResult,
  RewriteRelationEdge,
} from '@/lib/novelTypes'

export const INPUT_CLS =
  'w-full rounded-input border border-border bg-base px-2 py-1 text-[11px] outline-none placeholder:text-muted focus:border-accent/60'

export const BTN_CLS =
  'inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-foreground transition-colors hover:border-accent/40 disabled:cursor-not-allowed disabled:opacity-50'

interface LinesFieldProps {
  label: string
  hint?: string
  value: string[]
  onChange: (value: string[]) => void
  placeholder?: string
  rows?: number
}

/** 一行一项的文本列表（黑名单 / 节拍 / 桥段序列都用它）。 */
function LinesField({ label, hint, value, onChange, placeholder, rows = 3 }: LinesFieldProps) {
  return (
    <label className="block">
      <span className="mb-0.5 block text-[11px] text-secondary">{label}</span>
      <textarea
        value={value.join('\n')}
        rows={rows}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value.split('\n'))}
        className={cn(INPUT_CLS, 'font-mono')}
      />
      {hint ? <span className="mt-0.5 block text-[10px] text-muted">{hint}</span> : null}
    </label>
  )
}

interface KvFieldProps {
  label: string
  hint?: string
  value: Record<string, string>
  onChange: (value: Record<string, string>) => void
}

/** `键=值` 形式的字典（新词表：原名=新名）。 */
function KvField({ label, hint, value, onChange }: KvFieldProps) {
  const text = Object.entries(value)
    .map(([key, item]) => `${key}=${item}`)
    .join('\n')
  return (
    <label className="block">
      <span className="mb-0.5 block text-[11px] text-secondary">{label}</span>
      <textarea
        value={text}
        rows={3}
        placeholder={'原名=新名'}
        onChange={(e) => {
          const next: Record<string, string> = {}
          for (const line of e.target.value.split('\n')) {
            const at = line.indexOf('=')
            if (at <= 0) continue
            next[line.slice(0, at).trim()] = line.slice(at + 1).trim()
          }
          onChange(next)
        }}
        className={cn(INPUT_CLS, 'font-mono')}
      />
      {hint ? <span className="mt-0.5 block text-[10px] text-muted">{hint}</span> : null}
    </label>
  )
}

interface EdgeEditorProps {
  title: string
  edges: RewriteRelationEdge[]
  onChange: (edges: RewriteRelationEdge[]) => void
}

/** 关系边编辑：`from / to / kind / power` 四格一行（③ 关系拓扑的判据）。 */
function EdgeEditor({ title, edges, onChange }: EdgeEditorProps) {
  const update = (index: number, patch: Partial<RewriteRelationEdge>) =>
    onChange(edges.map((edge, i) => (i === index ? { ...edge, ...patch } : edge)))

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] text-secondary">{title}</span>
        <button
          type="button"
          className={BTN_CLS}
          onClick={() => onChange([...edges, { from: '', to: '', kind: '', power: '' }])}
        >
          + 加一条边
        </button>
      </div>
      {edges.length === 0 ? (
        <p className="text-[10px] text-muted">（未填写）</p>
      ) : (
        <ul className="space-y-1">
          {edges.map((edge, index) => (
            <li key={index} className="flex items-center gap-1">
              <input
                value={edge.from}
                placeholder="起点"
                onChange={(e) => update(index, { from: e.target.value })}
                className={cn(INPUT_CLS, 'min-w-0 flex-1')}
              />
              <span className="shrink-0 text-[10px] text-muted">→</span>
              <input
                value={edge.to}
                placeholder="终点"
                onChange={(e) => update(index, { to: e.target.value })}
                className={cn(INPUT_CLS, 'min-w-0 flex-1')}
              />
              <input
                value={edge.kind}
                placeholder="类型"
                onChange={(e) => update(index, { kind: e.target.value })}
                className={cn(INPUT_CLS, 'w-16 shrink-0')}
              />
              <input
                value={edge.power}
                placeholder="权力"
                onChange={(e) => update(index, { power: e.target.value })}
                className={cn(INPUT_CLS, 'w-16 shrink-0')}
              />
              <button
                type="button"
                className="shrink-0 rounded-btn border border-border px-1.5 py-1 text-[11px] text-muted hover:text-danger"
                onClick={() => onChange(edges.filter((_, i) => i !== index))}
                aria-label="删除这条边"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

interface SlotEditorProps {
  title: string
  slots: FunctionSlot[]
  onChange: (slots: FunctionSlot[]) => void
}

/** 功能位编辑：`slot / trait` 两格一行（⑦ 一对一映射的判据）。 */
function SlotEditor({ title, slots, onChange }: SlotEditorProps) {
  const update = (index: number, patch: Partial<FunctionSlot>) =>
    onChange(slots.map((slot, i) => (i === index ? { ...slot, ...patch } : slot)))

  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] text-secondary">{title}</span>
        <button
          type="button"
          className={BTN_CLS}
          onClick={() => onChange([...slots, { slot: '', trait: '' }])}
        >
          + 加一个功能位
        </button>
      </div>
      {slots.length === 0 ? (
        <p className="text-[10px] text-muted">（未填写）</p>
      ) : (
        <ul className="space-y-1">
          {slots.map((slot, index) => (
            <li key={index} className="flex items-center gap-1">
              <input
                value={slot.slot}
                placeholder="功能位"
                onChange={(e) => update(index, { slot: e.target.value })}
                className={cn(INPUT_CLS, 'min-w-0 flex-1')}
              />
              <input
                value={slot.trait}
                placeholder="特征"
                onChange={(e) => update(index, { trait: e.target.value })}
                className={cn(INPUT_CLS, 'min-w-0 flex-1')}
              />
              <button
                type="button"
                className="shrink-0 rounded-btn border border-border px-1.5 py-1 text-[11px] text-muted hover:text-danger"
                onClick={() => onChange(slots.filter((_, i) => i !== index))}
                aria-label="删除这个功能位"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export interface BlueprintFormProps {
  blueprint: Blueprint
  onChange: (blueprint: Blueprint) => void
  /** 最近一次预检结果（null = 没跑过） */
  precheck: PrecheckResult | null
  /** 当前是否有动作在进行（禁用按钮，避免连点） */
  busy: boolean
  /** 预检中的标记 */
  prechecking: boolean
  saving: boolean
  onPrecheck: () => void
  onSave: () => void
  /** 已有报告时默认收起（用户的注意力应该在报告上） */
  defaultOpen?: boolean
}

export function BlueprintForm({
  blueprint,
  onChange,
  precheck,
  busy,
  prechecking,
  saving,
  onPrecheck,
  onSave,
  defaultOpen = true,
}: BlueprintFormProps) {
  const setSourceRef = (patch: Partial<Blueprint['source_ref']>) =>
    onChange({ ...blueprint, source_ref: { ...blueprint.source_ref, ...patch } })
  const setAbstract = (patch: Partial<Blueprint['abstract']>) =>
    onChange({ ...blueprint, abstract: { ...blueprint.abstract, ...patch } })
  const setRebuild = (patch: Partial<Blueprint['rebuild']>) =>
    onChange({ ...blueprint, rebuild: { ...blueprint.rebuild, ...patch } })

  return (
    <details className="rounded-card border border-border bg-base" open={defaultOpen}>
      <summary className="cursor-pointer px-2.5 py-2 text-xs text-foreground">
        ① 结构蓝图（只填结构笔记，**不要粘原文**）
      </summary>

      <div className="space-y-2 border-t border-border px-2.5 py-2">
        {/* 来源标注 */}
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="block">
            <span className="mb-0.5 block text-[11px] text-secondary">来源标注</span>
            <input
              value={blueprint.source_ref.label}
              placeholder="如：某修真小说（结构笔记）"
              onChange={(e) => setSourceRef({ label: e.target.value })}
              className={INPUT_CLS}
            />
          </label>
          <label className="block">
            <span className="mb-0.5 block text-[11px] text-secondary">作品类型</span>
            <input
              value={blueprint.source_ref.work_type}
              placeholder="如：长篇 / 网文"
              onChange={(e) => setSourceRef({ work_type: e.target.value })}
              className={INPUT_CLS}
            />
          </label>
          <label className="block">
            <span className="mb-0.5 block text-[11px] text-secondary">备注</span>
            <input
              value={blueprint.source_ref.note}
              placeholder="一句话说清你借的是什么结构"
              onChange={(e) => setSourceRef({ note: e.target.value })}
              className={INPUT_CLS}
            />
          </label>
        </div>

        <SlotEditor
          title="原作功能位（⑦ 的判据）"
          slots={blueprint.abstract.function_slots}
          onChange={(function_slots) => setAbstract({ function_slots })}
        />

        <div className="grid gap-2 sm:grid-cols-2">
          <LinesField
            label="L1 原作专名黑名单"
            hint="每行一个。产物里出现即硬阻断。"
            value={blueprint.rebuild.L1_symbols.banned}
            onChange={(banned) =>
              setRebuild({ L1_symbols: { ...blueprint.rebuild.L1_symbols, banned } })
            }
          />
          <KvField
            label="L1 新词表（原名=新名）"
            value={blueprint.rebuild.L1_symbols.new_lexicon}
            onChange={(new_lexicon) =>
              setRebuild({ L1_symbols: { ...blueprint.rebuild.L1_symbols, new_lexicon } })
            }
          />
          <LinesField
            label="L2 标志场景黑名单"
            value={blueprint.rebuild.L2_scenes.banned}
            onChange={(banned) =>
              setRebuild({ L2_scenes: { ...blueprint.rebuild.L2_scenes, banned } })
            }
          />
          <LinesField
            label="L2 新场景"
            value={blueprint.rebuild.L2_scenes.new_scenes}
            onChange={(new_scenes) =>
              setRebuild({ L2_scenes: { ...blueprint.rebuild.L2_scenes, new_scenes } })
            }
          />
        </div>

        {/* L3 —— 命门层 */}
        <div className="grid gap-2 sm:grid-cols-2">
          <EdgeEditor
            title="L3 原作关系图"
            edges={blueprint.rebuild.L3_relations.source_graph}
            onChange={(source_graph) =>
              setRebuild({ L3_relations: { ...blueprint.rebuild.L3_relations, source_graph } })
            }
          />
          <EdgeEditor
            title="L3 新作关系图"
            edges={blueprint.rebuild.L3_relations.new_graph}
            onChange={(new_graph) =>
              setRebuild({ L3_relations: { ...blueprint.rebuild.L3_relations, new_graph } })
            }
          />
        </div>

        {/* L5 —— 命门层 */}
        <div className="grid gap-2 sm:grid-cols-2">
          <LinesField
            label="L5 原作桥段序列"
            hint="按发生顺序，一行一个（④ 的判据）。"
            value={blueprint.rebuild.L5_beats.source_seq}
            onChange={(source_seq) =>
              setRebuild({ L5_beats: { ...blueprint.rebuild.L5_beats, source_seq } })
            }
          />
          <LinesField
            label="L5 新作桥段序列"
            value={blueprint.rebuild.L5_beats.new_seq}
            onChange={(new_seq) =>
              setRebuild({ L5_beats: { ...blueprint.rebuild.L5_beats, new_seq } })
            }
          />
        </div>

        {/* 抽象层 */}
        <div className="grid gap-2 sm:grid-cols-2">
          <LinesField
            label="原作反转类型（⑧ 的判据）"
            value={blueprint.abstract.reversal_types}
            onChange={(reversal_types) => setAbstract({ reversal_types })}
            rows={2}
          />
          <LinesField
            label="原作反转位置（0-1 进度，一行一个）"
            hint="留空则 ⑧ 的位置维度无法自动比对。"
            value={blueprint.abstract.reversal_positions.map((value) => String(value))}
            onChange={(lines) =>
              setAbstract({
                reversal_positions: lines
                  .map((line) => Number(line.trim()))
                  .filter((value) => Number.isFinite(value)),
              })
            }
            rows={2}
          />
          <LinesField
            label="情绪节拍"
            value={blueprint.abstract.emotion_beats}
            onChange={(emotion_beats) => setAbstract({ emotion_beats })}
            rows={2}
          />
          <LinesField
            label="信息差"
            value={blueprint.abstract.info_gap}
            onChange={(info_gap) => setAbstract({ info_gap })}
            rows={2}
          />
        </div>

        {/* 预检结果：命中时不回显原文全文（后端只给 ≤40 字片段 + 引导示例） */}
        {precheck ? (
          precheck.ok ? (
            <div className="rounded-btn border border-border bg-elevated px-2 py-1.5 text-[11px] text-secondary">
              预检通过 —— 形态上像结构笔记。{precheck.honesty_note}
            </div>
          ) : (
            <div className="space-y-1 rounded-btn border border-warning/40 bg-warning/5 px-2 py-1.5">
              <div className="text-[11px] font-medium text-foreground">
                输入疑似原文，已被拒绝（未保存）
              </div>
              <ul className="space-y-0.5">
                {precheck.hits.map((hit, index) => (
                  <li key={index} className="text-[11px] leading-relaxed text-secondary">
                    <span className="text-muted">{hit.field}</span> · {hit.rule} ·{' '}
                    <span className="font-mono">{hit.excerpt}</span>
                    {hit.hint ? <span className="text-muted"> — {hit.hint}</span> : null}
                  </li>
                ))}
              </ul>
              {precheck.sample ? (
                <pre className="whitespace-pre-wrap rounded-btn border border-dashed border-border bg-base px-2 py-1 font-mono text-[10px] text-secondary">
                  {precheck.sample}
                </pre>
              ) : null}
              {precheck.honesty_note ? (
                <p className="text-[10px] text-muted">{precheck.honesty_note}</p>
              ) : null}
            </div>
          )
        ) : null}

        <div className="flex flex-wrap gap-1.5">
          <button type="button" className={BTN_CLS} disabled={busy} onClick={onPrecheck}>
            {prechecking ? (
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            ) : (
              <ShieldCheck className="h-3 w-3" aria-hidden="true" />
            )}
            预检（不保存）
          </button>
          <button type="button" className={BTN_CLS} disabled={busy} onClick={onSave}>
            {saving ? (
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            ) : (
              <Save className="h-3 w-3" aria-hidden="true" />
            )}
            保存蓝图
          </button>
        </div>
      </div>
    </details>
  )
}
