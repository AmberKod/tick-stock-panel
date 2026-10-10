/**
 * K 线画图工具(趋势线 / 水平线 / 垂直线 / 矩形 / 平行通道 / 斐波那契回撤 / 文字)。
 *
 * 定位与纪律:
 *   - 这些图形**不是数据**, 是用户在图上自己画的标注 —— 全部存在本地 localStorage,
 *     不上传、不影响任何计算结果, 也不会被当成"信号"混进 K线内标注。
 *   - 锚点存**日期 + 价格**(不存像素下标): 换区间/缩放/换主题后图形仍钉在同一根 K 上;
 *     数据里找不到这个日期了(比如缩到更短区间)就整条不画, 不猜位置。
 *   - 每条图形记下创建时的**坐标上下文**(周期 + 复权档)。复权档一换价格体系就变了,
 *     前复权下画的水平线放到不复权图上会指错价 —— 上下文不同就不渲染, 而不是画错的位置。
 */

export type DrawTool =
  | 'cursor' | 'trend' | 'hline' | 'vline' | 'rect' | 'channel' | 'fib' | 'text'

/** 能真正画出来的类型(cursor 只是编辑模式) */
export type DrawShape = Exclude<DrawTool, 'cursor'>

export interface DrawPoint {
  /** 锚点日期(日K=YYYY-MM-DD; 分钟K=带时间的字符串)。找不到就不画这条。 */
  date: string
  price: number
}

export interface Drawing {
  id: string
  type: DrawShape
  a: DrawPoint
  b: DrawPoint
  /** 平行通道的第三个点(定通道宽度)。只有 channel 用得到。 */
  c?: DrawPoint
  /** 文字标注的内容。只有 text 用得到。 */
  text?: string
  /** 创建时的坐标上下文: `${period}|${mkline ?? ''}|${adjust}` */
  ctx: string
  /** 颜色, 默认取工具色 */
  color?: string
}

/** 点一下就成的工具(不需要拖出距离) */
export const SINGLE_CLICK_TOOLS: DrawShape[] = ['hline', 'vline', 'text']

export const DRAW_TOOLS: { value: DrawTool; label: string; hint: string }[] = [
  { value: 'cursor', label: '选择', hint: '选择/拖动已画的图形; 点中后按 Delete 删除。此模式下滚轮缩放与拖动平移照常' },
  { value: 'trend', label: '趋势线', hint: '按下鼠标拖一条线: 两点确定, 向右无限延伸, 端点标出这段的涨跌幅' },
  { value: 'hline', label: '水平线', hint: '在图上点一下, 在该价位画一条水平线(可上下拖动)' },
  { value: 'vline', label: '垂直线', hint: '在图上点一下, 在该日期画一条竖线(可左右拖动), 用来标记某一天' },
  { value: 'rect', label: '矩形', hint: '拖一个矩形框住一段震荡区间, 自动标出根数与振幅' },
  { value: 'channel', label: '平行通道', hint: '两步: 先拖出基准趋势线, 再点一下定通道宽度 —— 上下轨平行, 中间是等距中线' },
  { value: 'fib', label: '斐波那契', hint: '从低点拖到高点(或反向): 自动算出 0/23.6/38.2/50/61.8/78.6/100% 回撤位' },
  { value: 'text', label: '文字', hint: '点一下落一个文字标注, 就地输入(不弹窗); 双击已画的文字可改' },
]

export const DRAW_COLORS: Record<DrawShape, string> = {
  trend: '#F59E0B',
  hline: '#38BDF8',
  vline: '#22D3EE',
  rect: '#A78BFA',
  channel: '#34D399',
  fib: '#F472B6',
  text: '#FBBF24',
}

const DRAW_SHAPE_SET = new Set<string>(Object.keys(DRAW_COLORS))

/** 斐波那契回撤比例(经典 7 档) */
export const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const

export function newDrawingId(): string {
  return `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
}

/** 每条图形的持久化 key 按标的隔离 —— A股换 B股不该看到别人的线。 */
export function drawingsKey(symbol: string): string {
  return `stockDetail:drawings:${symbol}`
}

export function loadDrawings(symbol: string): Drawing[] {
  try {
    const raw = localStorage.getItem(drawingsKey(symbol))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    // 反序列化后做一次形状校验: 坏数据(手改/旧版本)直接丢, 别让整页渲染炸掉。
    // 类型也要认: 旧版本没有的类型(或手改成不认识的值)不能被当成"默认线"画出来。
    return parsed.filter((d: any) =>
      d && typeof d.id === 'string' && typeof d.type === 'string'
      && DRAW_SHAPE_SET.has(d.type) && d.a && d.b
      && typeof d.a.date === 'string' && Number.isFinite(d.a.price)
      && typeof d.b.date === 'string' && Number.isFinite(d.b.price))
      .map((d: any) => {
        const out: Drawing = { ...d }
        // channel 的第三点丢了就退化成普通趋势线 —— 至少还在正确的位置上
        if (out.type === 'channel' && !(out.c && Number.isFinite(out.c?.price))) {
          delete out.c
          out.type = 'trend'
        }
        return out
      })
  } catch {
    return []
  }
}

export function saveDrawings(symbol: string, list: Drawing[]): void {
  try {
    if (list.length === 0) localStorage.removeItem(drawingsKey(symbol))
    else localStorage.setItem(drawingsKey(symbol), JSON.stringify(list))
  } catch { /* 隐私模式下写不进去就算了, 不影响看图 */ }
}

/** 斐波那契各档的价格: 从 a.price 到 b.price 之间按 ratio 插值 */
export function fibLevels(a: number, b: number): { ratio: number; price: number }[] {
  return FIB_RATIOS.map(r => ({ ratio: r, price: a + (b - a) * r }))
}

/**
 * 平行通道的宽度(价格差)。
 *
 * 斜率按**每根 K**算(不是按像素): 像素斜率会随缩放变化, 但每根的涨跌是数据本身,
 * 换区间/缩放后通道宽度不该跟着变。基准线在 c 那根上的价位 = a.price + 斜率 × 根数差。
 * 两根在同一天(除数为 0)时斜率无定义 → 退化成"水平通道", 宽度就是两点价差。
 */
export function channelWidth(
  a: DrawPoint, b: DrawPoint, c: DrawPoint,
  idxA: number, idxB: number, idxC: number,
): number {
  if (idxA === idxB) return c.price - a.price
  const slope = (b.price - a.price) / (idxB - idxA)
  return c.price - (a.price + slope * (idxC - idxA))
}
