// 小说工作区 · 换元仿写 — 关系图并排比对（③ 的人工核对入口）
//
// 零第三方依赖：圆形布局自己算（极角均分），箭头用 SVG marker，不引图布局库。
// 目的不是「画得好看」，而是让人能**并排看两句话就能回答**反向三问第 2 问：
// 「关系图与原作并排，是同一张图吗？」
//
// 展示的三件事：节点数 / 边数、度数序列（降序）、每条边的类型与权力流向。

import { useId } from 'react'

import { cn } from '@/lib/cn'
import {
  CHECK_STATUS_LABELS,
  CHECK_STATUS_TONE,
} from '@/lib/novelTypes'
import type { CheckStatus, RewriteRelationEdge } from '@/lib/novelTypes'

const SVG_W = 200
const SVG_H = 132
const RADIUS = 44
const CX = SVG_W / 2
const CY = 62
const NODE_R = 8

/** 取图里出现过的所有节点（保持首次出现顺序，布局才稳定、不跳动）。 */
function nodeList(edges: RewriteRelationEdge[]): string[] {
  const nodes: string[] = []
  for (const edge of edges) {
    for (const name of [edge.from, edge.to]) {
      if (name && !nodes.includes(name)) nodes.push(name)
    }
  }
  return nodes
}

/** 极角均分的圆形布局（无布局引擎，结果确定可复现）。 */
function positions(nodes: string[]): Record<string, { x: number; y: number }> {
  const total = nodes.length
  if (total === 0) return {}
  if (total === 1) return { [nodes[0]]: { x: CX, y: CY } }
  const map: Record<string, { x: number; y: number }> = {}
  nodes.forEach((name, index) => {
    const angle = -Math.PI / 2 + (2 * Math.PI * index) / total
    map[name] = { x: CX + RADIUS * Math.cos(angle), y: CY + RADIUS * Math.sin(angle) }
  })
  return map
}

/** 无向度数列，降序（与后端 `RelationFingerprint.degrees` 同口径）。 */
function degreeSeq(edges: RewriteRelationEdge[], nodes: string[]): number[] {
  const counter: Record<string, number> = {}
  for (const name of nodes) counter[name] = 0
  for (const edge of edges) {
    if (edge.from) counter[edge.from] = (counter[edge.from] ?? 0) + 1
    if (edge.to) counter[edge.to] = (counter[edge.to] ?? 0) + 1
  }
  return Object.values(counter).sort((a, b) => b - a)
}

function clip(name: string): string {
  return name.length > 4 ? `${name.slice(0, 4)}…` : name
}

export interface RelationGraphCompareProps {
  sourceGraph: RewriteRelationEdge[]
  newGraph: RewriteRelationEdge[]
  /** ③ 的判定状态（用于抬头芯片；不传则只做并排展示） */
  status?: CheckStatus
  className?: string
}

interface CanvasProps {
  title: string
  edges: RewriteRelationEdge[]
  markerId: string
  tone: string
}

function GraphCanvas({ title, edges, markerId, tone }: CanvasProps) {
  const nodes = nodeList(edges)
  const pos = positions(nodes)
  const degrees = degreeSeq(edges, nodes)

  return (
    <div className="min-w-0 flex-1 rounded-btn border border-border bg-base p-1.5">
      <div className="mb-1 flex flex-wrap items-baseline gap-x-1.5 px-0.5">
        <span className="text-[11px] text-foreground">{title}</span>
        <span className="text-[10px] text-muted">
          {nodes.length} 点 / {edges.length} 边
        </span>
      </div>
      <svg
        viewBox={`0 0 ${SVG_W} ${SVG_H}`}
        className={cn('h-auto w-full', tone)}
        role="img"
        aria-label={`${title}：${nodes.length} 个节点，${edges.length} 条边`}
      >
        <defs>
          <marker
            id={markerId}
            markerWidth="7"
            markerHeight="7"
            refX="6"
            refY="3"
            orient="auto"
            markerUnits="userSpaceOnUse"
          >
            <path d="M0,0 L0,6 L6,3 z" fill="currentColor" />
          </marker>
        </defs>

        {nodes.length === 0 ? (
          <text x={CX} y={CY} textAnchor="middle" fontSize="9" fill="currentColor">
            （空图）
          </text>
        ) : null}

        {/* 边：两端各缩进 NODE_R+1，避免压在节点圆上 */}
        {edges.map((edge, index) => {
          if (edge.from === edge.to) return null
          const a = pos[edge.from]
          const b = pos[edge.to]
          if (!a || !b) return null
          const dx = b.x - a.x
          const dy = b.y - a.y
          const length = Math.hypot(dx, dy) || 1
          const ux = dx / length
          const uy = dy / length
          return (
            <line
              key={`${edge.from}-${edge.to}-${index}`}
              x1={a.x + ux * (NODE_R + 2)}
              y1={a.y + uy * (NODE_R + 2)}
              x2={b.x - ux * (NODE_R + 2)}
              y2={b.y - uy * (NODE_R + 2)}
              stroke="currentColor"
              strokeWidth="1"
              strokeOpacity="0.55"
              markerEnd={`url(#${markerId})`}
            />
          )
        })}

        {/* 节点 + 名称 */}
        {nodes.map((name) => {
          const point = pos[name]
          if (!point) return null
          const labelY = point.y > CY ? point.y + NODE_R + 10 : point.y - NODE_R - 4
          return (
            <g key={name}>
              <circle
                cx={point.x}
                cy={point.y}
                r={NODE_R}
                fill="none"
                stroke="currentColor"
                strokeWidth="1.2"
              />
              <text
                x={point.x}
                y={labelY}
                textAnchor="middle"
                fontSize="8"
                fill="currentColor"
              >
                {clip(name)}
              </text>
            </g>
          )
        })}
      </svg>
      <div className="mt-1 break-all px-0.5 font-mono text-[10px] text-muted">
        度数序列 [{degrees.join(', ')}]
      </div>
    </div>
  )
}

export function RelationGraphCompare({
  sourceGraph,
  newGraph,
  status,
  className,
}: RelationGraphCompareProps) {
  const rawId = useId()
  const uid = rawId.replace(/[^a-zA-Z0-9_-]/g, '')
  const srcDegrees = degreeSeq(sourceGraph, nodeList(sourceGraph))
  const newDegrees = degreeSeq(newGraph, nodeList(newGraph))
  const sameShape =
    srcDegrees.length === newDegrees.length &&
    srcDegrees.every((value, index) => value === newDegrees[index])

  return (
    <section className={cn('rounded-card border border-border bg-surface', className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-2.5 py-2 text-[11px]">
        <span className="text-foreground">③ 关系图并排比对</span>
        {status ? (
          <span className={cn('text-[11px]', CHECK_STATUS_TONE[status])}>
            {CHECK_STATUS_LABELS[status]}
          </span>
        ) : null}
        <span className={sameShape ? 'text-warning' : 'text-muted'}>
          度数序列{sameShape ? '一致（需人工看边的类型与权力流向）' : '不同'}
        </span>
      </div>
      <p className="border-b border-border px-2.5 pb-1.5 text-[10px] text-muted">
        这里画的是<span className="text-secondary">当前蓝图里的两张图</span>；报告生成之后若改过蓝图，画面会跟着变
        —— 判定以报告里 ③ 那一项为准。
      </p>

      <div className="flex flex-col gap-2 p-2 sm:flex-row">
        <GraphCanvas
          title="原作关系图"
          edges={sourceGraph}
          markerId={`${uid}-src-arrow`}
          tone="text-muted"
        />
        <GraphCanvas
          title="新作关系图"
          edges={newGraph}
          markerId={`${uid}-new-arrow`}
          tone="text-foreground"
        />
      </div>

      {/* 边清单（图的文本等价物 —— 读不出图时也能逐条核对） */}
      <div className="grid gap-2 border-t border-border p-2 sm:grid-cols-2">
        <EdgeList title="原作边" edges={sourceGraph} />
        <EdgeList title="新作边" edges={newGraph} />
      </div>

      {status === 'pass' ? (
        <p className="border-t border-border px-2.5 py-2 text-[11px] text-secondary">
          自动比对未命中。仍请你自己按上面两张图回答反向三问第 2 问 ——
          判定为通过不等于没有风险。
        </p>
      ) : null}
    </section>
  )
}

function EdgeList({ title, edges }: { title: string; edges: RewriteRelationEdge[] }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[10px] text-muted">{title}</div>
      {edges.length === 0 ? (
        <div className="text-[11px] text-muted">（未填写）</div>
      ) : (
        <ul className="space-y-0.5">
          {edges.map((edge, index) => (
            <li key={index} className="truncate text-[11px] text-secondary">
              · {edge.from} → {edge.to}
              <span className="ml-1 text-muted">
                {edge.kind || '未标类型'}
                {edge.power ? ` / ${edge.power}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
