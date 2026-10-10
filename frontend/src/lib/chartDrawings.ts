/**
 * K 线画图工具(趋势线 / 水平线 / 矩形 / 斐波那契回撤)。
 *
 * 定位与纪律:
 *   - 这些图形**不是数据**, 是用户在图上自己画的标注 —— 全部存在本地 localStorage,
 *     不上传、不影响任何计算结果, 也不会被当成"信号"混进 K线内标注。
 *   - 锚点存**日期 + 价格**(不存像素下标): 换区间/缩放/换主题后图形仍钉在同一根 K 上;
 *     数据里找不到这个日期了(比如缩到更短区间)就整条不画, 不猜位置。
 *   - 每条图形记下创建时的**坐标上下文**(周期 + 复权档)。复权档一换价格体系就变了,
 *     前复权下画的水平线放到不复权图上会指错价 —— 上下文不同就不渲染, 而不是画错的位置。
 */

export type DrawTool = 'cursor' | 'trend' | 'hline' | 'rect' | 'fib'

export interface DrawPoint {
  /** 锚点日期(日K=YYYY-MM-DD; 分钟K=带时间的字符串)。找不到就不画这条。 */
  date: string
  price: number
}

export interface Drawing {
  id: string
  type: Exclude<DrawTool, 'cursor'>
  a: DrawPoint
  b: DrawPoint
  /** 创建时的坐标上下文: `${period}|${mkline ?? ''}|${adjust}` */
  ctx: string
  /** 颜色, 默认取工具色 */
  color?: string
}

export const DRAW_TOOLS: { value: DrawTool; label: string; hint: string }[] = [
  { value: 'cursor', label: '选择', hint: '选择/拖动已画的图形; 点中后按 Delete 删除。此模式下滚轮缩放与拖动平移照常' },
  { value: 'trend', label: '趋势线', hint: '按下鼠标拖一条线: 两点确定, 向右无限延伸' },
  { value: 'hline', label: '水平线', hint: '在图上点一下, 在该价位画一条水平线(可上下拖动)' },
  { value: 'rect', label: '矩形', hint: '拖一个矩形: 常用来框住一段震荡区间' },
  { value: 'fib', label: '斐波那契', hint: '从低点拖到高点(或反向): 自动算出 0/23.6/38.2/50/61.8/78.6/100% 回撤位' },
]

export const DRAW_COLORS: Record<Exclude<DrawTool, 'cursor'>, string> = {
  trend: '#F59E0B',
  hline: '#38BDF8',
  rect: '#A78BFA',
  fib: '#F472B6',
}

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
    // 反序列化后做一次形状校验: 坏数据(手改/旧版本)直接丢, 别让整页渲染炸掉
    return parsed.filter((d: any) =>
      d && typeof d.id === 'string' && d.a && d.b
      && typeof d.a.date === 'string' && Number.isFinite(d.a.price)
      && typeof d.b.date === 'string' && Number.isFinite(d.b.price))
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
