import type { FrontendExtension } from '@/extensions/types'
import {
  AnnouncementsPanel,
  DragonTigerPanel,
  ResearchPanel,
} from '@/components/astock/AstockDeepPanels'

/**
 * A股深度数据 — 个股详情弹窗的 footer slot (九章融合 P2-2 试点)。
 *
 * 渲染逻辑与整页 /stock/:symbol **共用** `components/astock/AstockDeepPanels.tsx`
 * —— 一份实现两处用, 避免同一份后端契约出现两套渲染而漂移。
 *
 * 全部为真实数据(过 source_gate 白名单), 无任何 demo/随机兜底:
 *   龙虎榜=东财 datacenter · 研报=东财 reportapi · 公告=巨潮 cninfo
 */

const extension: FrontendExtension = {
  id: 'astock.deep',
  apiVersion: 1,
  slots: [
    {
      name: 'stock-preview.footer',
      id: 'astock-deep-dragon',
      order: 50,
      component: DragonTigerPanel,
    },
    {
      name: 'stock-preview.footer',
      id: 'astock-deep-research',
      order: 51,
      component: ResearchPanel,
    },
    {
      name: 'stock-preview.footer',
      id: 'astock-deep-announcements',
      order: 52,
      component: AnnouncementsPanel,
    },
  ],
}

export default extension
