"""A 股市场档案 (沪深京)。

常量与算法 1:1 迁移自 app/market_time.py 与 app/price_limits.py,
含 2026-07-06 主板 ST 涨跌幅制度切换的历史细节。

M0 迁移红线: 数值与边界语义一字不改 —
午休返回 120.0、ts 无效返回 240.0、周末视作全天 240 等
边界行为原样保留, 禁止"顺手优化"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time

from app.markets.profile import IndexRef, TradingSession

CN_TZ = timezone(timedelta(hours=8))  # A 股: 固定北京时间, 无夏令时

# ── 涨跌停制度常量 (迁移自 price_limits.py) ──────────
MAIN_BOARD_ST_LIMIT_CHANGE_DATE = date(2026, 7, 6)
MAIN_BOARD_LIMIT = 0.10
LEGACY_MAIN_BOARD_ST_LIMIT = 0.05
GROWTH_BOARD_LIMIT = 0.20
BEIJING_BOARD_LIMIT = 0.30

_MORNING = TradingSession(dt_time(9, 30), dt_time(11, 30))
_AFTERNOON = TradingSession(dt_time(13, 0), dt_time(15, 0))

_CORE_INDICES = (
    IndexRef("000001.SH", "上证指数"),
    IndexRef("399001.SZ", "深证成指"),
    IndexRef("399006.SZ", "创业板指"),
    IndexRef("000680.SH", "科创综指"),
)


class CNProfile:
    """A 股市场档案。"""

    market = "CN"
    tz_name = "Asia/Shanghai"
    tz = CN_TZ
    sessions = (_MORNING, _AFTERNOON)
    trading_minutes_total = float(_MORNING.minutes + _AFTERNOON.minutes)  # 240
    currency = "CNY"
    settlement = "T+1"
    same_day_sell_allowed = False
    lot_size = 100
    symbol_suffixes = (".SH", ".SZ", ".BJ")

    core_indices = _CORE_INDICES
    bench_rt_candidates = ("000002.SH", "000001.SH", "399107.SZ", "399001.SZ", "899050.BJ")
    benchmark_symbol = "000001.SH"
    benchmark_fallbacks = {
        "SH": ["000002.SH", "000001.SH"],  # 上证A指 → 上证指数
        "SZ": ["399107.SZ", "399001.SZ"],  # 深证A指 → 深证成指
        "BJ": ["899050.BJ", "000001.SH"],  # 北证50 → 上证指数
    }

    # ── 时钟 ─────────────────────────────────────────
    def now(self) -> datetime:
        """当前北京时间 (带时区)。"""
        return datetime.now(self.tz)

    def today(self) -> date:
        """当前北京日期。"""
        return datetime.now(self.tz).date()

    def trading_minutes_elapsed_from_dt(self, dt: datetime) -> float:
        """根据北京时间 datetime 计算当日已交易分钟数。

        交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
        - 开盘前 = 0; 午休(11:30-13:00) = 120(保持上午累计); 收盘后 = 240。
        - 非交易日(周末) = 240 (视作全天, 避免量比被折算成 0)。
        """
        t = dt.time()
        if t < _MORNING.start:
            return 0.0
        if t < _MORNING.end:
            return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
        if t < _AFTERNOON.start:
            return 120.0  # 午休, 保持上午累计
        if t < _AFTERNOON.end:
            return 120.0 + (dt.hour * 60 + dt.minute - 13 * 60) + dt.second / 60.0
        return self.trading_minutes_total

    def trading_minutes_elapsed_from_ts(self, ts_ms: int | float | None) -> float:
        """从行情时间戳(毫秒)计算当日已交易分钟数。

        优先使用此方法: 行情 timestamp 是真实成交时间, 比服务端时间更准。
        timestamp 为 None/无效时返回 240 (视作全天, 避免量比被折算成 0)。
        """
        if not ts_ms:
            return self.trading_minutes_total
        try:
            dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=self.tz)
        except (ValueError, TypeError, OSError):
            return self.trading_minutes_total
        return self.trading_minutes_elapsed_from_dt(dt)

    # ── 涨跌停 ────────────────────────────────────────
    def has_price_limit(self) -> bool:
        """A 股有涨跌停制度。"""
        return True

    @staticmethod
    def board_limit_pct(symbol: str) -> float:
        if symbol.endswith(".BJ"):
            return BEIJING_BOARD_LIMIT
        if symbol.startswith(("300", "301", "688", "689")):
            return GROWTH_BOARD_LIMIT
        return MAIN_BOARD_LIMIT

    def limit_pct(
        self,
        symbol: str,
        trade_date: date,
        *,
        is_risk_warning: bool = False,
    ) -> float:
        """个股某交易日的有效涨跌幅限制。

        主板 ST 股在 2026-07-06 制度切换前为 5%, 之后与主板同口径 10%。
        """
        base = self.board_limit_pct(symbol)
        if (
            base == MAIN_BOARD_LIMIT
            and is_risk_warning
            and trade_date < MAIN_BOARD_ST_LIMIT_CHANGE_DATE
        ):
            return LEGACY_MAIN_BOARD_ST_LIMIT
        return base

    # ── 代码路由 ─────────────────────────────────────
    @staticmethod
    def fallback_suffix(code: str) -> str:
        """6 位代码无维表命中时的交易所后缀兜底: 6 开头 → .SH, 其余 → .SZ。

        仅对 A 股代码调用; 含 .HK/.US 后缀的输入由 resolve_market 门控在外。
        """
        if code.startswith("6"):
            return ".SH"
        return ".SZ"


CN_PROFILE = CNProfile()
