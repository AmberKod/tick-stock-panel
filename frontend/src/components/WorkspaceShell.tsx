import { useEffect, Suspense } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  CandlestickChart,
  Newspaper,
  Image as ImageIcon,
  BookOpen,
  Clapperboard,
  TrendingUp,
  LineChart,
  Loader2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Logo } from './Logo'
import { cn } from '@/lib/cn'
import { MarketTab, type Market } from './MarketTab'
import { marketFromLocation } from '@/lib/backtestMarket'

/**
 * 工作区定义 — 多维工作台的一级导航。
 * 每个工作区 = 一个路由前缀 + 一个业务域（Phase 0 只有股票域落地，
 * 其余为占位页，按设计方案 Phase 2~5 依次上线）。
 * per-workspace 强调色仅用于图标与色条，不侵入文字语义色（设计方案 §7）。
 */
export interface WorkspaceDef {
  id: string
  to: string
  label: string
  icon: LucideIcon
  color: string
  // 是否为股票域（港美股是股票域的子市场，需要在顶部显示 MarketTab）
  isStockDomain?: boolean
}

export const WORKSPACES: WorkspaceDef[] = [
  { id: 'stock', to: '/',        label: '股票', icon: CandlestickChart, color: '#3b82f6', isStockDomain: true },
  { id: 'hk',    to: '/hk',      label: '港股', icon: TrendingUp,       color: '#dc2626', isStockDomain: true },
  { id: 'us',    to: '/us',      label: '美股', icon: LineChart,        color: '#2563eb', isStockDomain: true },
  { id: 'news',  to: '/news',    label: '热点', icon: Newspaper,        color: '#f97316' },
  { id: 'image', to: '/image',   label: '图片', icon: ImageIcon,        color: '#a855f7' },
  { id: 'novel', to: '/novel',   label: '小说', icon: BookOpen,         color: '#22c55e' },
  { id: 'video', to: '/video',   label: '视频', icon: Clapperboard,     color: '#f43f5e' },
]

/** 由当前路径推断激活工作区：非 stock 前缀命中即切换，否则回落股票域 */
export function useActiveWorkspaceId(): string {
  const { pathname, search } = useLocation()
  const market = marketFromLocation(pathname, search)
  if (market !== 'cn') return market
  const hit = WORKSPACES.find(ws => ws.id !== 'stock' && pathname.startsWith(ws.to))
  return hit?.id ?? 'stock'
}

/** 由当前路径推断激活市场（仅在股票域内有效） */
function useActiveMarket(): Market {
  const { pathname, search } = useLocation()
  return marketFromLocation(pathname, search)
}

/** 工作区快捷键 Alt+1..5 — 全局监听一次 */
function useWorkspaceHotkeys() {
  const navigate = useNavigate()
  useEffect(() => {
    const onKeydown = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing) return
      const idx = Number(e.key) - 1
      if (!Number.isInteger(idx) || idx < 0 || idx >= WORKSPACES.length) return
      // 输入框内不劫持（Alt+数字在部分输入法中有含义）
      const target = e.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
      e.preventDefault()
      navigate(WORKSPACES[idx].to)
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [navigate])
}

/**
 * 桌面端左侧工作区 Rail（56px）。
 * - 激活态：左侧 3px 强调色指示条 + 背景微亮 + 图标着色
 * - 无障碍：aria-label 必填，触达目标 44×44，焦点环走全局 focus-visible
 */
function DesktopRail() {
  useWorkspaceHotkeys()
  const activeId = useActiveWorkspaceId()

  return (
    <nav
      aria-label="工作区切换"
      className="hidden h-full w-14 shrink-0 flex-col items-center border-r border-border bg-surface py-2 md:flex"
    >
      {/* 品牌锚点 */}
      <Link
        to="/"
        aria-label="返回股票看板"
        title="TickStock 工作台"
        className="mb-2 flex h-10 w-10 items-center justify-center rounded-xl transition-colors hover:bg-elevated/60"
      >
        <Logo size={22} />
      </Link>

      <div className="h-px w-8 shrink-0 bg-border/60" aria-hidden="true" />

      <div className="flex flex-1 flex-col items-center gap-1 pt-2">
        {WORKSPACES.map((ws, idx) => {
          const active = activeId === ws.id
          const Icon = ws.icon
          return (
            <Link
              key={ws.id}
              to={ws.to}
              aria-label={`${ws.label}工作区${active ? '（当前）' : ''}`}
              aria-current={active ? 'page' : undefined}
              title={`${ws.label} · Alt+${idx + 1}`}
              className={cn(
                'group relative flex h-11 w-11 items-center justify-center rounded-xl transition-all duration-150 ease-smooth',
                active ? 'bg-elevated' : 'hover:bg-elevated/60 hover:scale-105 active:scale-95',
              )}
            >
              {/* 左侧强调色指示条 — 激活工作区的域色 */}
              <span
                aria-hidden="true"
                className={cn(
                  'absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full transition-opacity duration-150',
                  active ? 'opacity-100' : 'opacity-0',
                )}
                style={{ background: ws.color, boxShadow: `0 0 8px ${ws.color}80` }}
              />
              <Icon
                className="h-[22px] w-[22px] shrink-0 transition-colors duration-150"
                style={{ color: active ? ws.color : undefined }}
                aria-hidden="true"
              />
            </Link>
          )
        })}
      </div>
    </nav>
  )
}

/** 移动端底部工作区 Tab Bar（<768px）— Rail 的等价物，44px 触达目标 */
function MobileWorkspaceBar() {
  const activeId = useActiveWorkspaceId()
  return (
    <nav
      aria-label="工作区切换"
      className="absolute inset-x-0 bottom-0 z-40 flex h-14 items-stretch border-t border-border bg-surface/95 backdrop-blur-md md:hidden"
    >
      {WORKSPACES.map(ws => {
        const active = activeId === ws.id
        const Icon = ws.icon
        return (
          <Link
            key={ws.id}
            to={ws.to}
            aria-label={`${ws.label}工作区${active ? '（当前）' : ''}`}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 transition-colors',
              active ? 'bg-elevated/60' : 'active:bg-elevated/40',
            )}
          >
            <Icon
              className="h-[18px] w-[18px] shrink-0"
              style={{ color: active ? ws.color : undefined }}
              aria-hidden="true"
            />
            <span className={cn('text-[10px] leading-none', active ? 'font-medium text-foreground' : 'text-muted')}>
              {ws.label}
            </span>
          </Link>
        )
      })}
    </nav>
  )
}

/**
 * 工作台外壳 — Rail + 内容区。
 * 桌面：左侧 56px Rail；移动：底部 Tab Bar（内容区预留 3.5rem 底边距）。
 * 股票域的 Layout 整体作为「股票工作区」挂在本壳的内容区里，零侵入。
 *
 * 股票域顶部显示 MarketTab（A股/港股/美股 切换），其他域不显示。
 */
export function WorkspaceShell() {
  const activeId = useActiveWorkspaceId()
  const currentWorkspace = WORKSPACES.find(ws => ws.id === activeId)
  const showMarketTab = currentWorkspace?.isStockDomain ?? false
  const activeMarket = useActiveMarket()
  const navigate = useNavigate()
  const location = useLocation()

  const handleMarketChange = (m: Market) => {
    const path = location.pathname
    if (path === '/backtest' || /^\/(hk|us)\/backtest$/.test(path)) {
      if (m === activeMarket) return
      const query = new URLSearchParams({ tab: 'strategy', market: m === 'cn' ? 'stock' : m })
      navigate('/backtest?' + query.toString())
      return
    }
    const module = path.match(/^\/(?:hk|us)(\/[^/]+)?/)?.[1] ?? ''
    const routeByMarket: Record<Market, string> = {
      cn: module || '/',
      hk: `/hk${module}` || '/hk',
      us: `/us${module}` || '/us',
    }
    navigate(routeByMarket[m])
  }

  return (
    <div className="flex h-screen w-full overflow-hidden bg-base text-foreground">
      <DesktopRail />
      <div className="relative flex min-w-0 flex-1 flex-col pb-14 md:pb-0">
        {showMarketTab && (
          <div className="border-b border-border bg-surface/80 backdrop-blur-sm px-4 py-2 sticky top-0 z-10">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted/80">市场</span>
              <MarketTab active={activeMarket} onChange={handleMarketChange} />
            </div>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-hidden">
          <Suspense
            fallback={
              <div className="flex h-full items-center justify-center">
                <Loader2 className="h-5 w-5 animate-spin text-muted" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </div>
        <MobileWorkspaceBar />
      </div>
    </div>
  )
}
