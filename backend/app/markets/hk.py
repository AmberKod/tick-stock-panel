"""港股市场档案。

常量与算法迁移参考: ai_stock_tools/src/backend/app/services/trading_hours.py
港股档期: 早市 9:30-12:00 (150 min) + 午市 13:00-16:00 (180 min) = 330 min
无涨跌停 / 无 ST 标记 / 可当日卖出、T+2 交收 / 每手股数按标的

M1 关键设计:
- has_price_limit() = False → 涨跌停计算/打板信号对港股整体门控隐藏
- limit_pct() = None → 港股无涨跌停, 显式 None (而非 0.0 防止"无变化"误用)
- lot_size = None → 港股按标的, 不在 profile 层级定
- fallback_suffix 仅对 5 位数字代码生效, 区别 CN 的 6 位规则
"""
from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from app.markets.profile import IndexRef, TradingSession

HK_TZ = ZoneInfo("Asia/Hong_Kong")  # 与北京同时区, 无夏令时

_MORNING = TradingSession(dt_time(9, 30), dt_time(12, 0))   # 早市 150 分钟
_AFTERNOON = TradingSession(dt_time(13, 0), dt_time(16, 0)) # 午市 180 分钟


class HKProfile:
    """港股市场档案。

    实现 MarketProfile 协议: 时钟/指数/代码规则, 无涨跌停特性显式表达。
    """

    market = "HK"
    tz_name = "Asia/Hong_Kong"
    tz = HK_TZ
    sessions = (_MORNING, _AFTERNOON)
    trading_minutes_total = float(_MORNING.minutes + _AFTERNOON.minutes)  # 330.0
    currency = "HKD"
    settlement = "T+2"
    same_day_sell_allowed = True
    lot_size = None  # 港股按标的, 不在 profile 层级定
    symbol_suffixes = (".HK",)

    # 港股核心指数 (恒生/恒生科技/恒生中国企业)
    core_indices = (
        IndexRef("HSI.HK", "恒生指数"),
        IndexRef("HSTECH.HK", "恒生科技指数"),
        IndexRef("HSCEI.HK", "恒生中国企业指数"),
    )
    # 港股无 ST 制度, 实时基准候选/基准回退均不适用
    bench_rt_candidates: tuple[str, ...] = ()
    benchmark_symbol = "HSI.HK"
    benchmark_fallbacks: dict[str, list[str]] = {}

    # ── 时钟 ─────────────────────────────────────────
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def trading_minutes_elapsed_from_dt(self, dt: datetime) -> float:
        """根据港股时间 datetime 计算当日已交易分钟数。

        档期: 9:30-12:00 (0~150) + 13:00-16:00 (150~330)。
        - 开盘前 = 0; 午休(12:00-13:00) = 150(保持上午累计); 收盘后 = 330。
        - 周末视作全天 330 (避免量比折算成 0)。
        """
        t = dt.time()
        if t < _MORNING.start:
            return 0.0
        if t < _MORNING.end:
            return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
        if t < _AFTERNOON.start:
            return float(_MORNING.minutes)  # 午休, 保持上午累计
        if t < _AFTERNOON.end:
            return float(_MORNING.minutes) + (dt.hour * 60 + dt.minute - 13 * 60) + dt.second / 60.0
        return self.trading_minutes_total

    def trading_minutes_elapsed_from_ts(self, ts_ms: int | float | None) -> float:
        """从行情时间戳 (毫秒) 计算当日已交易分钟数。

        移植 CN 模式, 仅切换 tz 到 Asia/Hong_Kong。
        timestamp 为 None/无效时返回 330 (视作全天, 避免量比被折算成 0)。
        """
        if not ts_ms:
            return self.trading_minutes_total
        try:
            dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=self.tz)
        except (ValueError, TypeError, OSError):
            return self.trading_minutes_total
        return self.trading_minutes_elapsed_from_dt(dt)

    # ── 涨跌停 (港股无) ────────────────────────────────
    def has_price_limit(self) -> bool:
        return False  # 港股无涨跌停制度

    @staticmethod
    def board_limit_pct(symbol: str) -> float:
        # 港股无涨跌停, 始终返回 0.0 占位 (业务层应先检查 has_price_limit)
        return 0.0

    def limit_pct(
        self,
        symbol: str,
        trade_date: date,
        *,
        is_risk_warning: bool = False,
    ) -> float | None:
        """港股无涨跌停, 始终返回 None。

        返回 None (而非 0.0) 防止业务层把"无涨跌停"误当成"涨跌停=0%"使用。
        """
        return None

    # ── 代码路由 ─────────────────────────────────────
    @staticmethod
    def fallback_suffix(code: str) -> str:
        """5 位数字代码 → .HK (港股代码统一 5 位 0 补齐)。

        不接受 CN 的 6 位规则; 港股 5 位规则区别于 A 股。
        """
        if len(code) == 5 and code.isdigit():
            return ".HK"
        return ""


HK_PROFILE = HKProfile()
