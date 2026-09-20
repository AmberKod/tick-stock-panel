import { Link } from 'react-router-dom'
import { FlaskConical } from 'lucide-react'
import type { InternationalMarket } from '@/lib/api'

export function MarketBacktestEntry({ market }: { market: InternationalMarket }) {
  const label = market === 'hk' ? '港股' : '美股'
  const query = new URLSearchParams({ tab: 'strategy', market })

  return (
    <section className="rounded-lg border border-border bg-surface p-4">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded bg-accent/10 text-accent">
          <FlaskConical className="h-5 w-5" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-foreground">{label}日线策略回测</h2>
          <p className="mt-1 text-xs leading-relaxed text-secondary">在统一回测页选择策略、当前市场标的和日期区间，运行后查看收益、交易记录与未成交原因。</p>
        </div>
      </div>
      <div className="mt-4 grid gap-2 sm:grid-cols-3">
        <div className="rounded border border-border/60 p-3">
          <p className="text-[11px] text-muted">市场与币种</p>
          <p className="mt-1 text-sm text-foreground">{label} · {market === 'hk' ? 'HKD' : 'USD'}</p>
        </div>
        <div className="rounded border border-border/60 p-3">
          <p className="text-[11px] text-muted">数量规则</p>
          <p className="mt-1 text-sm text-secondary">{market === 'hk' ? '按标的每手股数，缺失时不成交' : '最小 1 股，使用整数股数'}</p>
        </div>
        <div className="rounded border border-border/60 p-3">
          <p className="text-[11px] text-muted">数据前提</p>
          <p className="mt-1 text-sm text-secondary">本市场日 K 与策略指标覆盖目标日期</p>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <Link to={'/backtest?' + query.toString()} className="rounded-btn bg-accent px-3 py-2 text-xs text-white hover:bg-accent/90">进入日线策略回测</Link>
        <Link to={'/' + market + '/data'} className="rounded-btn border border-border px-3 py-2 text-xs text-secondary hover:border-accent/50 hover:text-accent">检查数据状态</Link>
        <Link to={'/' + market + '/screener'} className="rounded-btn border border-border px-3 py-2 text-xs text-secondary hover:border-accent/50 hover:text-accent">运行策略筛选</Link>
      </div>
      <p className="mt-3 text-[11px] leading-relaxed text-muted">日线回测默认使用信号后的下一交易日开盘成交；费用为可编辑的研究假设。当前市场入口会保留市场上下文。</p>
    </section>
  )
}
