import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'

/**
 * 打开个股的统一入口 —— **整页**, 不是弹窗。
 *
 * 之前各列表/榜单点击个股是弹一个 StockPreviewDialog 快速预览。弹窗受 max-width
 * 和最大高度限制, 装不下 K线 + 深度面板这类研究型内容, 观感永远是"缩水版"。
 * 个股详情整页(/stock/:symbol)落地后, 所有入口统一跳整页。
 */
export function useOpenStock(): (symbol: string) => void {
  const navigate = useNavigate()
  return useCallback((symbol: string) => {
    const code = (symbol ?? '').trim()
    if (!code) return
    navigate(`/stock/${encodeURIComponent(code)}`)
  }, [navigate])
}
