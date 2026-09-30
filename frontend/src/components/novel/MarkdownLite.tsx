// 小说工作区 — 自研极简 Markdown 渲染（零新增依赖）
//
// 支持：# ~ ###### 标题、**粗体**、*斜体*、`行内代码`、> 引用、-/*/+ 无序列表、
// 1. 有序列表、--- 分割线、段落。
//
// **不支持的语法（表格、[链接](url)、![图片]、脚注、删除线…）一律原样输出** ——
// 这是 PRD P0-5④ 的诚实要求：不假装渲染，也不静默丢弃。
// 渲染走 React 节点（不用 dangerouslySetInnerHTML），因此不存在注入风险。

import { Fragment, type ReactNode } from 'react'

export interface MarkdownLiteProps {
  source: string
  className?: string
}

const INLINE_RE = /(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`)/g

/** 行内标记 → React 节点；其余文字原样保留。 */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null
  INLINE_RE.lastIndex = 0
  while ((match = INLINE_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(<Fragment key={`${keyPrefix}-t${lastIndex}`}>{text.slice(lastIndex, match.index)}</Fragment>)
    }
    const token = match[0]
    const key = `${keyPrefix}-m${match.index}`
    if (token.startsWith('**')) {
      nodes.push(<strong key={key} className="font-semibold text-foreground">{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('`')) {
      nodes.push(
        <code key={key} className="rounded-[3px] bg-elevated px-1 py-0.5 font-mono text-[0.9em]">
          {token.slice(1, -1)}
        </code>,
      )
    } else {
      nodes.push(<em key={key} className="italic">{token.slice(1, -1)}</em>)
    }
    lastIndex = match.index + token.length
  }
  if (lastIndex < text.length) {
    nodes.push(<Fragment key={`${keyPrefix}-tail`}>{text.slice(lastIndex)}</Fragment>)
  }
  return nodes
}

type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'quote'; lines: string[] }
  | { kind: 'ul'; items: string[] }
  | { kind: 'ol'; items: string[] }
  | { kind: 'hr' }
  | { kind: 'p'; lines: string[] }

/** 把纯文本切成块级结构（只认上面那几种，其余按段落原样保留）。 */
function parseBlocks(source: string): Block[] {
  const blocks: Block[] = []
  const lines = source.split('\n')
  let paragraph: string[] = []

  const flushParagraph = () => {
    if (paragraph.length > 0) {
      blocks.push({ kind: 'p', lines: paragraph })
      paragraph = []
    }
  }

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '')
    if (line.trim() === '') {
      flushParagraph()
      continue
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushParagraph()
      blocks.push({ kind: 'hr' })
      continue
    }
    const heading = /^\s*(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushParagraph()
      blocks.push({ kind: 'heading', level: heading[1].length, text: heading[2] })
      continue
    }
    const quote = /^\s*>\s?(.*)$/.exec(line)
    if (quote) {
      flushParagraph()
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'quote') last.lines.push(quote[1])
      else blocks.push({ kind: 'quote', lines: [quote[1]] })
      continue
    }
    const ul = /^\s*[-*+]\s+(.*)$/.exec(line)
    if (ul) {
      flushParagraph()
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'ul') last.items.push(ul[1])
      else blocks.push({ kind: 'ul', items: [ul[1]] })
      continue
    }
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line)
    if (ol) {
      flushParagraph()
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'ol') last.items.push(ol[1])
      else blocks.push({ kind: 'ol', items: [ol[1]] })
      continue
    }
    // 其它一律按段落原文保留（表格、链接等不支持语法就落在这里，原样呈现）
    paragraph.push(line)
  }
  flushParagraph()
  return blocks
}

const HEADING_CLASS: Record<number, string> = {
  1: 'mt-4 mb-2 text-xl font-bold text-foreground first:mt-0',
  2: 'mt-4 mb-2 text-lg font-bold text-foreground first:mt-0',
  3: 'mt-3 mb-1.5 text-base font-semibold text-foreground first:mt-0',
  4: 'mt-3 mb-1.5 text-sm font-semibold text-foreground first:mt-0',
  5: 'mt-2 mb-1 text-sm font-semibold text-secondary first:mt-0',
  6: 'mt-2 mb-1 text-xs font-semibold text-secondary first:mt-0',
}

export function MarkdownLite({ source, className }: MarkdownLiteProps) {
  const blocks = parseBlocks(source ?? '')
  if (blocks.length === 0) {
    return <div className={className ? `${className} text-xs text-muted` : 'text-xs text-muted'}>（本章暂无正文）</div>
  }

  return (
    <div className={className}>
      {blocks.map((block, index) => {
        const key = `b${index}`
        switch (block.kind) {
          case 'heading': {
            const Tag = (`h${Math.min(block.level, 6)}`) as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
            return (
              <Tag key={key} className={HEADING_CLASS[block.level]}>
                {renderInline(block.text, key)}
              </Tag>
            )
          }
          case 'quote':
            return (
              <blockquote
                key={key}
                className="my-2 border-l-2 border-border pl-3 text-secondary"
              >
                {block.lines.map((line, i) => (
                  <div key={`${key}-l${i}`}>{renderInline(line, `${key}-l${i}`)}</div>
                ))}
              </blockquote>
            )
          case 'ul':
            return (
              <ul key={key} className="my-2 list-disc space-y-1 pl-5">
                {block.items.map((item, i) => (
                  <li key={`${key}-i${i}`}>{renderInline(item, `${key}-i${i}`)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={key} className="my-2 list-decimal space-y-1 pl-5">
                {block.items.map((item, i) => (
                  <li key={`${key}-i${i}`}>{renderInline(item, `${key}-i${i}`)}</li>
                ))}
              </ol>
            )
          case 'hr':
            return <hr key={key} className="my-4 border-border" />
          case 'p':
          default:
            return (
              <p key={key} className="my-2 leading-7 text-foreground/90">
                {block.lines.map((line, i) => (
                  <Fragment key={`${key}-l${i}`}>
                    {i > 0 ? <br /> : null}
                    {renderInline(line, `${key}-l${i}`)}
                  </Fragment>
                ))}
              </p>
            )
        }
      })}
    </div>
  )
}
