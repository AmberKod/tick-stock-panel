import React from 'react';

export type Market = 'cn' | 'hk' | 'us';

interface MarketTabProps {
  active: Market;
  onChange: (m: Market) => void;
}

const LABELS: Record<Market, string> = { cn: 'A股', hk: '港股', us: '美股' };
const ORDER: Market[] = ['cn', 'hk', 'us'];

/**
 * 市场切换 Tab — 语义 tablist/tab + aria-selected, 触摸目标 h-11 (44px) 达标。
 * 颜色只走项目 token (surface/elevated/accent/border/foreground), 不引入
 * tailwind 不存在的 surface-800/brand-500 变体。
 */
export const MarketTab: React.FC<MarketTabProps> = ({ active, onChange }) => (
  <div
    role="tablist"
    aria-label="市场切换"
    className="inline-flex border border-border bg-surface rounded-lg overflow-hidden"
  >
    {ORDER.map((m) => {
      const selected = active === m;
      return (
        <button
          key={m}
          type="button"
          role="tab"
          aria-selected={selected}
          onClick={() => onChange(m)}
          className={`
            inline-flex h-11 items-center px-4 text-sm font-medium transition-colors duration-200 ease-smooth
            ${selected
              ? 'bg-accent text-white shadow-sm'
              : 'text-secondary hover:bg-elevated hover:text-foreground'
            }
          `}
        >
          {LABELS[m]}
        </button>
      );
    })}
  </div>
);