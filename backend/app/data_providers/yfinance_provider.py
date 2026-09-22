"""美股数据 provider — yfinance 适配层。

M2 范围: 复用 yfinance 库, 实现 instruments/realtime/daily/adj_factors 4 项。
- realtime: yfinance.Ticker.fast_info (15min 延迟, 免费档)
- daily: yfinance.Ticker.history(period='max', auto_adjust=False)
- adj_factors: daily close / raw_close 计算 ex_factor
- instruments: 静态热门池 (yfinance 没有全市场列表; 美股 S&P500 + NASDAQ100 + 自选足以覆盖)

M2 已知约束:
- yfinance 免费档 realtime 延迟 15min; tick/minute 不支持
- 全市场扫描不可行 (yfinance 无), universe 限制为热门股池
- DST 由 yfinance 内部处理, 落盘用 ZoneInfo("America/New_York") 一致化
"""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import normalize_adj_factors

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# yfinance 限流熔断 (与腾讯 WAF 熔断 c267f6a 同构):
# Yahoo 免费档为 IP 级滑动窗口限流, 全量 6071 只美股逐标的请求时必然触发。
# 触发后无退避的全速重试 = 纯空烧 (每只 1 次无效请求, 0 数据收益),
# 还会延长 IP 被拦时长。09-15 实测: 105 次/分钟的连续 429 重试把限流
# 时间拖得更长。熔断策略: 连续 N 次 429 后打开, 冷却期内本地快速失败
# (0 网络请求); 半开探测失败则冷却翻倍 (10→20→40 分钟封顶 60), 成功复位。
# 进程级状态, 模块级单例 (服务 reload 会重置, 无跨进程持久化需求)。
_YF_RATE_LIMIT_MARKERS = (
    "Too Many Requests",
    "Rate limited",
    "rate limit",
    "429",
    # 2026-09-22 数据地基批 #5: Yahoo 对本部署 IP 已持续 403 (Forbidden)。
    # 403 空烧与 429 空烧同罪: else 分支只 warning 后 continue, 全量循环
    # 烧完一轮 0 数据收益, 还可能延长封禁 —— 并入熔断计数。
    "403",
    "Forbidden",
)
_YF_CIRCUIT = {"failures": 0, "opens": 0, "blocked_until": 0.0}
_YF_CIRCUIT_LOCK = threading.Lock()
_YF_FAILURES_TO_OPEN = 5
_YF_COOLDOWN_BASE_SECONDS = 600.0  # 10 分钟
_YF_COOLDOWN_MAX_SECONDS = 3600.0  # 60 分钟封顶


def _yf_is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker.lower() in msg for marker in _YF_RATE_LIMIT_MARKERS)


def _yf_circuit_blocked() -> bool:
    """冷却期内返回 True。冷却期已过但 opens>0 时视为半开: 放行本次
    请求做探测 (返回 False), 成功复位 / 失败翻倍由 record_* 决定。"""
    return _time.monotonic() < _YF_CIRCUIT["blocked_until"]


def _yf_circuit_record_failure() -> None:
    with _YF_CIRCUIT_LOCK:
        _YF_CIRCUIT["failures"] += 1
        # opens>0 说明此前已熔断过: 冷却期结束后首个请求即半开探测,
        # 探测再失败立即翻倍冷却 (真半开, 不重新数 5 次); 成功才会复位 opens。
        probe_failed = _YF_CIRCUIT["opens"] > 0
        if _YF_CIRCUIT["failures"] >= _YF_FAILURES_TO_OPEN or probe_failed:
            opens = _YF_CIRCUIT["opens"]
            cooldown = min(
                _YF_COOLDOWN_BASE_SECONDS * (2 ** opens),
                _YF_COOLDOWN_MAX_SECONDS,
            )
            _YF_CIRCUIT.update(
                failures=0,
                opens=opens + 1,
                blocked_until=_time.monotonic() + cooldown,
            )
            logger.warning(
                "yfinance 熔断打开: 限流连续命中 (第 %d 次打开), 冷却 %.0f 分钟",
                opens + 1, cooldown / 60,
            )


def _yf_circuit_record_success() -> None:
    with _YF_CIRCUIT_LOCK:
        if _YF_CIRCUIT["failures"] or _YF_CIRCUIT["opens"]:
            _YF_CIRCUIT.update(failures=0, opens=0, blocked_until=0.0)


def yf_circuit_blocked() -> bool:
    """当前是否处于限流冷却期 (供调度层判断"此刻补跑必然空转")。

    冷却期内每个标的都会本地快速失败 (0 网络请求、0 数据), 全量 6071 只
    走一遍只是白烧一次 job 槽与日志。调度层据此延后补跑即可。
    """
    return _yf_circuit_blocked()


def yf_circuit_remaining_seconds() -> float:
    """冷却期剩余秒数 (非冷却期返回 0), 供日志与测试断言。"""
    remaining = _YF_CIRCUIT["blocked_until"] - _time.monotonic()
    return max(0.0, remaining)


def yf_circuit_reset() -> None:
    """测试注入用: 复位熔断状态。"""
    with _YF_CIRCUIT_LOCK:
        _YF_CIRCUIT.update(failures=0, opens=0, blocked_until=0.0)


def yf_circuit_open_for_test(seconds: float = 60.0) -> None:
    """测试注入用: 强制打开熔断并设定冷却时长。"""
    with _YF_CIRCUIT_LOCK:
        _YF_CIRCUIT.update(failures=0, opens=1, blocked_until=_time.monotonic() + seconds)

US_TZ = ZoneInfo("America/New_York")

# M2 内置美股热门池 (S&P 500 头部 + 科技七姐妹 + 知名中概)
US_DEMO_SYMBOLS: tuple[str, ...] = (
    "AAPL.US",   # Apple
    "MSFT.US",   # Microsoft
    "GOOGL.US",  # Alphabet
    "AMZN.US",   # Amazon
    "NVDA.US",   # NVIDIA
    "META.US",   # Meta
    "TSLA.US",   # Tesla
    "BRK-B.US",  # Berkshire Hathaway B
    "JPM.US",    # JPMorgan
    "V.US",      # Visa
    "BABA.US",   # Alibaba
    "PDD.US",    # Pinduoduo
    "XOM.US",    # Exxon Mobil
    "WMT.US",    # Walmart
    "JNJ.US",    # Johnson & Johnson
)

US_DEMO_NAMES: dict[str, str] = {
    "AAPL.US": "Apple Inc.",
    "MSFT.US": "Microsoft Corp.",
    "GOOGL.US": "Alphabet Inc.",
    "AMZN.US": "Amazon.com Inc.",
    "NVDA.US": "NVIDIA Corp.",
    "META.US": "Meta Platforms",
    "TSLA.US": "Tesla Inc.",
    "BRK-B.US": "Berkshire Hathaway",
    "JPM.US": "JPMorgan Chase",
    "V.US": "Visa Inc.",
    "BABA.US": "Alibaba Group",
    "PDD.US": "PDD Holdings",
    "XOM.US": "Exxon Mobil",
    "WMT.US": "Walmart Inc.",
    "JNJ.US": "Johnson & Johnson",
}


def _try_import_yf() -> object | None:
    """yfinance 是 M2 可选依赖 (与 akshare 同), 不可用时返回 None。"""
    try:
        import yfinance as yf  # type: ignore[import-untyped]
        return yf
    except ImportError:
        return None


class YFinanceProvider:
    """美股 yfinance provider (M2 阶段: 基础数据能力, 不含 tick/分钟级)。"""

    name = "yfinance"
    capabilities = ProviderCapabilities(
        instruments=True,   # 静态池 + yf.Tickers 索引
        daily=True,
        adj_factor=True,
        realtime=True,      # fast_info 延迟 15min
        financial=False,    # yfinance .info 太慢, M2.1 单独做
    )

    def _to_internal_symbol(self, yf_symbol: str) -> str:
        """yfinance 用 'AAPL' / 'BRK-B' (无后缀) → 内部 'AAPL.US' / 'BRK-B.US'。"""
        return f"{yf_symbol}.US"

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """返回 M2 内置 15 龙头 + 名字映射, schema 与 A 股一致 (含 market 派生列)。"""
        from app.data_providers.normalizer import normalize_instruments
        rows = [
            {
                "symbol": sym,
                "name": US_DEMO_NAMES.get(sym, sym),
                "code": sym.split(".")[0],
                "exchange": "US",
                "asset_type": "stock",
                "source": "us_demo",
            }
            for sym in US_DEMO_SYMBOLS
        ]
        return normalize_instruments(rows, asset_type="stock", source="us_demo")

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """批量实时 (yfinance fast_info 延迟 15min)。

        yfinance 不可用 → 返回空 df, 不抛错。
        """
        yf = _try_import_yf()
        if yf is None or not symbols:
            return pl.DataFrame()
        rows = []
        for sym in symbols:
            if _yf_circuit_blocked():
                break
            yf_sym = sym.split(".")[0]
            try:
                tk = yf.Ticker(yf_sym)
                info = tk.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                pre_close = getattr(info, "previous_close", None)
                change_pct = None
                if price is not None and pre_close:
                    change_pct = (price - pre_close) / pre_close * 100
                _yf_circuit_record_success()
                rows.append({
                    "symbol": self._to_internal_symbol(yf_sym),
                    "name": US_DEMO_NAMES.get(sym, yf_sym),
                    "code": yf_sym,
                    "price": float(price) if price else None,
                    "pre_close": float(pre_close) if pre_close else None,
                    "open": None, "high": None, "low": None,
                    "volume": None, "amount": None,
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                    "source": "yfinance",
                })
            except Exception as e:
                if _yf_is_rate_limited(e):
                    _yf_circuit_record_failure()
                else:
                    logger.debug("yfinance quote failed %s: %s", yf_sym, e)
                continue
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        df = df.with_columns(pl.lit(int(datetime.now(US_TZ).timestamp() * 1000)).alias("quote_ts"))
        return df

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """拉取多只美股日 K, 落 A 股 schema (含 quote_ts)。"""
        yf = _try_import_yf()
        if yf is None or not symbols:
            return pl.DataFrame()
        all_frames = []
        for sym in symbols:
            # 熔断打开期间本地快速失败, 不再空烧 Yahoo 端点
            if _yf_circuit_blocked():
                break
            # Yahoo uses BRK-A; keep the requested BRK.A.US identity internally.
            code = sym[:-3] if sym.upper().endswith(".US") else sym
            yf_sym = code.replace(".", "-")
            try:
                tk = yf.Ticker(yf_sym)
                history_options = {"auto_adjust": False}
                if start_time is None and end_time is None:
                    history_options["period"] = "5y"
                else:
                    if start_time is not None:
                        history_options["start"] = start_time.date().isoformat()
                    if end_time is not None:
                        # Yahoo's end is exclusive; the internal range is inclusive.
                        history_options["end"] = (end_time.date() + timedelta(days=1)).isoformat()
                hist = tk.history(**history_options)
                if hist is None or hist.empty:
                    continue
                _yf_circuit_record_success()
                # 标准化: yfinance index 是 DatetimeIndex, 改 date
                hist = hist.reset_index()
                date_col = "Date" if "Date" in hist.columns else hist.columns[0]
                df = pl.DataFrame({
                    "symbol": f"{code.upper()}.US",
                    "date": hist[date_col].dt.date,
                    "open": hist["Open"],
                    "high": hist["High"],
                    "low": hist["Low"],
                    "close": hist["Close"],
                    "volume": hist["Volume"] if "Volume" in hist.columns else None,
                    "amount": None,
                })
                df = df.with_columns(
                    pl.col("volume").cast(pl.Float64), pl.col("amount").cast(pl.Float64),
                    pl.lit("yfinance").alias("source"),
                    pl.lit("unadjusted").alias("price_adjustment"),
                    pl.lit("unavailable").alias("amount_source"),
                    pl.lit("shares").alias("volume_unit"), pl.lit("USD").alias("currency"),
                )
                if start_time:
                    df = df.filter(pl.col("date") >= start_time.date())
                if end_time:
                    df = df.filter(pl.col("date") <= end_time.date())
                all_frames.append(df)
            except Exception as e:
                if _yf_is_rate_limited(e):
                    _yf_circuit_record_failure()
                    logger.warning(
                        "yfinance rate-limited %s: 熔断计数 %d/%d", yf_sym,
                        _YF_CIRCUIT["failures"], _YF_FAILURES_TO_OPEN,
                    )
                else:
                    logger.warning("yfinance daily failed %s: %s", yf_sym, e)
                continue
        if not all_frames:
            return pl.DataFrame()
        return pl.concat(all_frames, how="vertical_relaxed")

    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType,
    ) -> pl.DataFrame:
        """计算 ex_factor = adj_close / close (yfinance 自带 Adj Close)。"""
        yf = _try_import_yf()
        if yf is None or not symbols:
            return pl.DataFrame()
        rows = []
        for sym in symbols:
            if _yf_circuit_blocked():
                break
            yf_sym = sym.split(".")[0]
            try:
                tk = yf.Ticker(yf_sym)
                hist = tk.history(period="5y", auto_adjust=False)
                if hist is None or hist.empty or "Adj Close" not in hist.columns:
                    continue
                _yf_circuit_record_success()
                hist = hist.reset_index()
                date_col = "Date" if "Date" in hist.columns else hist.columns[0]
                # ex_factor = close / adj_close (A 股惯例)
                for row in hist.iter_rows(named=True):
                    close = row.get("Close")
                    adj = row.get("Adj Close")
                    if close and adj:
                        ef = close / adj
                    else:
                        continue
                    rows.append({
                        "symbol": self._to_internal_symbol(yf_sym),
                        "trade_date": row[date_col].date() if hasattr(row[date_col], "date") else row[date_col],
                        "ex_factor": float(ef),
                    })
            except Exception as e:
                if _yf_is_rate_limited(e):
                    _yf_circuit_record_failure()
                else:
                    logger.debug("yfinance adj_factors failed %s: %s", yf_sym, e)
                continue
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        return normalize_adj_factors(df, source=self.name)
