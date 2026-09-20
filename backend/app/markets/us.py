"""美股市场档案。

迁移参考: ai_stock_tools/src/backend/app/services/trading_hours.py
美股档期: 常规 9:30-16:00 ET (390 min), 盘前/盘后时段 M2 再细化。
关键差异:
- DST 时钟: ZoneInfo("America/New_York") 自动处理 EDT/EST (比固定 UTC 更准)
- 无涨跌停: has_price_limit()=False, limit_pct()=None
- 可当日卖出,标准证券结算周期 T+1(自 2024-05-28 起;回测不模拟结算台账)
- 字母代码: AAPL / MSFT (1-5 位字母), 与 A 股 6 位数字/HK 5 位数字区分
"""
from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from app.markets.profile import IndexRef, TradingSession

US_TZ = ZoneInfo("America/New_York")  # 自动处理 DST (3月~11月 EDT = UTC-4)

_SESSION = TradingSession(dt_time(9, 30), dt_time(16, 0))  # 常规单一时段 390 min


class USProfile:
    """美股市场档案。

    实现 MarketProfile 协议: DST 时钟, 无涨跌停, 字母代码路由。
    """

    market = "US"
    tz_name = "America/New_York"
    tz = US_TZ
    sessions = (_SESSION,)
    trading_minutes_total = float(_SESSION.minutes)  # 390.0
    currency = "USD"
    settlement = "T+1"
    same_day_sell_allowed = True
    lot_size = 1  # 本批按整数股成交,不模拟零股
    symbol_suffixes = (".US",)

    # 美股核心指数 (标准普尔/纳斯达克/道琼斯)
    core_indices = (
        IndexRef("^GSPC.US", "标普500"),
        IndexRef("^IXIC.US", "纳斯达克综合"),
        IndexRef("^DJI.US", "道琼斯"),
    )
    bench_rt_candidates: tuple[str, ...] = ()
    benchmark_symbol = "^GSPC.US"
    benchmark_fallbacks: dict[str, list[str]] = {}

    # ── 时钟 ─────────────────────────────────────────
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def trading_minutes_elapsed_from_dt(self, dt: datetime) -> float:
        """常规时段 9:30-16:00 (0~390)。开盘前=0, 收盘后=390。

        注意: 美股无午休, 单一连续时段。
        """
        t = dt.time()
        if t < _SESSION.start:
            return 0.0
        if t < _SESSION.end:
            return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
        return self.trading_minutes_total

    def trading_minutes_elapsed_from_ts(self, ts_ms: int | float | None) -> float:
        """从行情时间戳 (毫秒, Unix) 计算当日已交易分钟数 (DST 已由 tz 处理)。

        timestamp 缺失/无效 → 390 (视作全天, 避免量比折算成 0)。
        """
        if not ts_ms:
            return self.trading_minutes_total
        try:
            dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=self.tz)
        except (ValueError, TypeError, OSError):
            return self.trading_minutes_total
        return self.trading_minutes_elapsed_from_dt(dt)

    # ── 涨跌停 (美股无) ────────────────────────────────
    def has_price_limit(self) -> bool:
        return False  # 美股无涨跌停, 只有熔断 (Level 1: 7% 指数级, 个股无)

    @staticmethod
    def board_limit_pct(symbol: str) -> float:
        return 0.0  # 占位

    def limit_pct(
        self,
        symbol: str,
        trade_date: date,
        *,
        is_risk_warning: bool = False,
    ) -> float | None:
        return None  # 美股无涨跌停

    # ── 代码路由 ─────────────────────────────────────
    @staticmethod
    def fallback_suffix(code: str) -> str:
        """字母代码 → .US (美股代码 1-5 位字母, 无数字规则)。

        不接受数字代码; 与 A 股 6 位/HK 5 位区分。
        """
        if code.isalpha() and 1 <= len(code) <= 5:
            return ".US"
        return ""


US_PROFILE = USProfile()
