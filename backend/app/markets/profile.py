"""市场档案协议 — 时钟、交易制度、代码规则的接口定义。

所有市场行为问题的唯一出口 (见 M0 施工文档 §2.1)。
业务代码禁止再散落 "sh"/"9:30"/"0.10" 等市场字面量,
只允许依赖 MarketProfile 协议。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from typing import Protocol

MarketId = str  # "CN" | "HK" | "US" (M0 仅实现 CN; HK/US 见 M1/M2)


@dataclass(frozen=True, slots=True)
class TradingSession:
    """一个连续交易时段, 如 A 股上午 9:30-11:30。"""

    start: dt_time
    end: dt_time

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)


@dataclass(frozen=True, slots=True)
class IndexRef:
    """指数引用: symbol + 展示名。"""

    symbol: str
    name: str


class MarketProfile(Protocol):
    """市场档案协议。

    静态属性描述制度, 行为方法封装市场规则。HK(无涨跌停/每手不定) 与
    US(DST/盘前盘后) 的差异由各自实现类自治, registry 只保证接口一致。
    """

    # ── 静态属性 ─────────────────────────────────────
    market: MarketId
    tz_name: str                         # IANA 时区名, 如 "Asia/Shanghai"
    sessions: tuple[TradingSession, ...]
    trading_minutes_total: float         # 一日累计交易分钟 (A 股 240)
    currency: str                        # "CNY" / "HKD" / "USD"
    settlement: str                      # 资金/证券结算周期;不代表当日是否允许卖出
    same_day_sell_allowed: bool          # 买入当日可否卖出,与结算独立
    lot_size: int | None                 # 每手股数; None=按标的 (港股)
    symbol_suffixes: tuple[str, ...]     # 本市场合法后缀

    # ── 指数与基准 ───────────────────────────────────
    core_indices: tuple[IndexRef, ...]    # 核心指数 (概览/侧边栏/轮询共用)
    bench_rt_candidates: tuple[str, ...]  # 实时基准候选链 (abnormal_moves)
    benchmark_symbol: str                 # 默认基准指数
    benchmark_fallbacks: dict[str, list[str]]  # 交易所 → 基准回退链

    # ── 行为方法 ─────────────────────────────────────
    def now(self) -> datetime: ...
    def today(self) -> date: ...
    def trading_minutes_elapsed_from_dt(self, dt: datetime) -> float: ...
    def trading_minutes_elapsed_from_ts(self, ts_ms: int | float | None) -> float: ...
    def has_price_limit(self) -> bool: ...
    def limit_pct(
        self, symbol: str, trade_date: date, *, is_risk_warning: bool = False
    ) -> float | None: ...
    def board_limit_pct(self, symbol: str) -> float: ...
    def fallback_suffix(self, code: str) -> str | None: ...
