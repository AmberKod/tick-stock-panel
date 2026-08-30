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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import normalize_adj_factors, normalize_daily
from app.markets.registry import resolve_market

logger = logging.getLogger(__name__)

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
        universes: list[str] | None = None,  # noqa: ARG002
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
            yf_sym = sym.split(".")[0]
            try:
                tk = yf.Ticker(yf_sym)
                info = tk.fast_info
                price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                pre_close = getattr(info, "previous_close", None)
                change_pct = None
                if price is not None and pre_close:
                    change_pct = (price - pre_close) / pre_close * 100
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
            except Exception as e:  # noqa: BLE001
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
        asset_type: AssetType,  # noqa: ARG002
    ) -> pl.DataFrame:
        """拉取多只美股日 K, 落 A 股 schema (含 quote_ts)。"""
        yf = _try_import_yf()
        if yf is None or not symbols:
            return pl.DataFrame()
        all_frames = []
        for sym in symbols:
            yf_sym = sym.split(".")[0]
            try:
                tk = yf.Ticker(yf_sym)
                hist = tk.history(period="5y", auto_adjust=False)
                if hist is None or hist.empty:
                    continue
                # 标准化: yfinance index 是 DatetimeIndex, 改 date
                hist = hist.reset_index()
                date_col = "Date" if "Date" in hist.columns else hist.columns[0]
                df = pl.DataFrame({
                    "symbol": self._to_internal_symbol(yf_sym),
                    "date": hist[date_col].dt.date,
                    "open": hist["Open"],
                    "high": hist["High"],
                    "low": hist["Low"],
                    "close": hist["Close"],
                    "volume": hist["Volume"].cast(pl.Float64) if "Volume" in hist.columns else None,
                })
                if start_time:
                    df = df.filter(pl.col("date") >= start_time.date())
                if end_time:
                    df = df.filter(pl.col("date") <= end_time.date())
                all_frames.append(df)
            except Exception as e:  # noqa: BLE001
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
        asset_type: AssetType,  # noqa: ARG002
    ) -> pl.DataFrame:
        """计算 ex_factor = adj_close / close (yfinance 自带 Adj Close)。"""
        yf = _try_import_yf()
        if yf is None or not symbols:
            return pl.DataFrame()
        rows = []
        for sym in symbols:
            yf_sym = sym.split(".")[0]
            try:
                tk = yf.Ticker(yf_sym)
                hist = tk.history(period="5y", auto_adjust=False)
                if hist is None or hist.empty or "Adj Close" not in hist.columns:
                    continue
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
            except Exception as e:  # noqa: BLE001
                logger.debug("yfinance adj_factors failed %s: %s", yf_sym, e)
                continue
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        return normalize_adj_factors(df, source=self.name)
