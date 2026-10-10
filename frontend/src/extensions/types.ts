import type { ComponentType } from 'react'
import type { LucideIcon } from 'lucide-react'

export const FRONTEND_EXTENSION_API_VERSION = 1 as const

export interface FrontendSlotContextMap {
  'layout.navigation.extra': {
    collapsed: boolean
    pathname: string
  }
  /** 个股详情对话框底部扩展区 (日K/分时图表下方) */
  'stock-preview.footer': {
    symbol: string
    name: string | null
    view: 'daily' | 'intraday'
  }
  /** 自选页工具栏扩展区 (按钮行末尾) */
  'watchlist.toolbar': {
    /** 当前筛选/排序后视图中的标的 */
    symbols: string[]
    viewMode: 'table' | 'card'
    selectedGroup: string
    /** 刷新自选增强数据 (扩展修改数据后调用) */
    refresh: () => void
  }
}

export type FrontendSlotName = keyof FrontendSlotContextMap

/**
 * 插槽注册项 — 必须是**判别联合**(按 name 分发), 不能写成
 * `{ name: K; component: ComponentType<Ctx[K]> }`: 后者在 K 为联合时
 * component 会被推断成"能同时吃三种 context"的函数类型, 而任何真实扩展
 * 组件只处理其中一种 —— 函数参数逆变导致**所有**具体 slot 实现都无法通过
 * 类型检查(2026-10-10 首个真实扩展 astock-deep 撞出此问题; 模板是
 * .example 不参与编译, 所以此前没暴露)。
 */
export type FrontendSlotRegistration<K extends FrontendSlotName = FrontendSlotName> = {
  [P in FrontendSlotName]: {
    name: P
    id: string
    order?: number
    component: ComponentType<FrontendSlotContextMap[P]>
  }
}[K]

export interface FrontendExtensionRoute {
  id: string
  path: `/${string}`
  component: ComponentType
}

export interface FrontendExtensionNavigation {
  id: string
  routeId: string
  label: string
  icon: LucideIcon
  order?: number
  badge?: string
}

export interface FrontendExtension {
  id: string
  apiVersion: typeof FRONTEND_EXTENSION_API_VERSION
  routes?: FrontendExtensionRoute[]
  navigation?: FrontendExtensionNavigation[]
  slots?: FrontendSlotRegistration[]
}

export interface FrontendExtensionModule {
  default: FrontendExtension
}

export interface FrontendExtensionLoadError {
  source: string
  extensionId?: string
  error: string
}
