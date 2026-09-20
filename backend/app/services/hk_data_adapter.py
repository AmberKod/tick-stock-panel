"""港股数据适配 (M1)。

职责:
- 静态内置 10 个港股龙头池 (M1 起步, 验证 quickquote 通路)
- 拉 akshare 全市场池 (用户机器装 akshare 时自动启用, 不可用时静默降级)
- 日 K 落盘接口 (走 akshare 新浪源 stock_hk_daily, 东财 stock_hk_hist 已被代理拦截, 失败时返回空, 不阻塞主流程)

设计取舍:
- 静态池不依赖外部网络, 永远可用
- akshare 全市场池作为"扩展能力", 失败时日志告警但不抛错
- 与 A 股 instruments.parquet 同 schema, market 派生列已就位 (M0)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.data_providers.normalizer import (
    normalize_instruments,
    normalize_lot_size,
    normalize_market_symbols,
)
from app.markets.hk import HK_TZ
from app.markets.registry import resolve_market
from app.tickflow.market_daily import (
    _as_plain_date,
    adapt_enriched_for_write,
    is_verified_hk_raw,
    list_market_daily_symbols,
    market_symbol_key,
    merge_market_daily_frames,
    read_market_daily_symbol,
    write_market_daily_symbol,
)

logger = logging.getLogger(__name__)

# M1 内置 10 个港股龙头 (覆盖科技/金融/消费/汽车 4 个板块)
HK_DEMO_SYMBOLS: tuple[str, ...] = (
    "00700.HK",   # 腾讯控股
    "09988.HK",   # 阿里巴巴-W
    "03690.HK",   # 美团-W
    "01211.HK",   # 比亚迪股份
    "01810.HK",   # 小米集团-W
    "00939.HK",   # 建设银行
    "01398.HK",   # 工商银行
    "00005.HK",   # 汇丰控股
    "00388.HK",   # 香港交易所
    "02318.HK",   # 中国平安
)

# 龙头名称映射 (静态; akshare 全市场拉取时会覆盖)
HK_DEMO_NAMES: dict[str, str] = {
    "00700.HK": "腾讯控股",
    "09988.HK": "阿里巴巴-W",
    "03690.HK": "美团-W",
    "01211.HK": "比亚迪股份",
    "01810.HK": "小米集团-W",
    "00939.HK": "建设银行",
    "01398.HK": "工商银行",
    "00005.HK": "汇丰控股",
    "00388.HK": "香港交易所",
    "02318.HK": "中国平安",
}


def _try_import_akshare() -> Any | None:
    """akshare 是 M1 可选 extra (pyproject [multi-market]), 不可用时返回 None。

    用 try-import 探测而不是顶层 import, 避免硬依赖。
    """
    try:
        import akshare as ak  # type: ignore[import-untyped]
        return ak
    except ImportError:
        return None


def load_demo_instruments() -> pl.DataFrame:
    """返回 M1 内置 10 个港股龙头的 instruments DataFrame (与 A 股 schema 一致)。

    永远可用 — 不依赖 akshare, 不依赖网络。
    """
    rows = [
        {
            "symbol": sym,
            "name": HK_DEMO_NAMES.get(sym, sym),
            "code": sym.split(".")[0],
            "exchange": "HK",
            "asset_type": "stock",
            "source": "hk_demo",
        }
        for sym in HK_DEMO_SYMBOLS
    ]
    return normalize_instruments(rows, asset_type="stock", source="hk_demo")


def fetch_hk_instruments_akshare() -> pl.DataFrame | None:
    """通过 AkShare 获取全市场港股池，主源失败时回退新浪源。

    ``stock_hk_spot_em`` 依赖东方财富；``stock_hk_spot`` 依赖新浪，
    两者都失败时返回 None。返回 df 与 A 股同 schema。
    """
    ak = _try_import_akshare()
    if ak is None:
        logger.info("akshare 未安装, 跳过港股全市场池拉取")
        return None

    def parse_rows(df: Any, name_key: str, source: str) -> pl.DataFrame | None:
        if df is None or len(df) == 0:
            return None
        rows: list[dict] = []
        for r in df.to_dict(orient="records"):
            code = str(r.get("代码") or "").zfill(5)
            if len(code) != 5 or not code.isdigit():
                continue
            rows.append({
                "symbol": f"{code}.HK",
                "name": r.get(name_key) or r.get("名称") or code,
                "code": code,
                "exchange": "HK",
                "asset_type": "stock",
                "source": source,
            })
        if not rows:
            return None
        return normalize_instruments(rows, asset_type="stock", source=source)

    try:
        result = parse_rows(ak.stock_hk_spot_em(), "名称", "akshare_em")
        if result is not None and not result.is_empty():
            return result
    except Exception as e:
        logger.warning("akshare 港股主源拉取失败，尝试新浪备用源: %s", e)

    try:
        result = parse_rows(ak.stock_hk_spot(), "中文名称", "akshare_sina")
        if result is not None and not result.is_empty():
            logger.info("akshare 港股新浪备用源成功: %d 只", result.height)
            return result
    except Exception as e:
        logger.warning("akshare 港股新浪备用源拉取失败: %s", e)
    return None


def sync_hk_instruments(
    data_dir: Path,
    *,
    use_akshare: bool = True,
    allow_demo: bool = True,
) -> int:
    """同步港股 instruments 维表 → data/instruments/hk_instruments.parquet。

    ``allow_demo=True`` 保留页面/开发环境的龙头池兼容行为；自动全量日 K
    调度必须传 ``allow_demo=False``，避免把 demo 池误当成生产 universe。
    """
    demo = load_demo_instruments()
    frames: list[pl.DataFrame] = []
    if use_akshare:
        full = fetch_hk_instruments_akshare()
        if full is not None and not full.is_empty():
            frames.append(full)
    if not frames:
        if not allow_demo:
            raise RuntimeError("港股全量 instruments 获取失败，拒绝写入 demo 快照")
        frames.append(demo)
    elif allow_demo:
        frames.insert(0, demo)
    df = pl.concat(frames, how="vertical_relaxed")
    if not allow_demo and df.height < 100:
        raise RuntimeError(f"港股全量 instruments 仅获取 {df.height} 只，拒绝覆盖现有快照")
    # market 列已由 normalize_instruments 派生, 这里再保险确认
    if "market" not in df.columns:
        df = df.with_columns(
            pl.col("symbol").map_elements(resolve_market, return_dtype=pl.Utf8).alias("market")
        )
    df = df.unique(subset=["symbol"], keep="last").sort("symbol")
    out = data_dir / "instruments" / "hk_instruments.parquet"
    expected_marker = _marker_bytes(data_dir)
    existing = pl.read_parquet(out) if out.exists() else pl.DataFrame()
    if not existing.is_empty():
        rows = {row["symbol"]: row for row in existing.to_dicts()}
        for incoming in df.to_dicts():
            old = rows.get(incoming["symbol"], {})
            preserved = {name: value for name, value in old.items()
                         if name.startswith(("lot_size", "candidate_", "instrument_status")) or name == "currency"}
            rows[incoming["symbol"]] = {**old, **incoming, **preserved}
        df = pl.DataFrame(list(rows.values()), infer_schema_length=None).sort("symbol")
    if existing.is_empty() or not _same_snapshot(existing, df):
        _publish_hk_files(data_dir, [(df, out)], expected_marker=expected_marker)
    logger.info("港股 instruments 同步: %d 行 → %s", df.height, out)
    return df.height


# ── 美股全量池 ──

def fetch_us_instruments_file(data_dir: Path) -> pl.DataFrame | None:
    """读取受控的本地美股 universe 文件。

    支持 ``us_universe.csv`` / ``us_universe.parquet``，字段接受
    ``symbol``/``ticker``/``code`` 和 ``name``/``company``。文件必须由用户或
    外部同步流程提供，导入结果仍由 ``sync_us_instruments`` 的严格门槛校验。
    """
    instruments_dir = data_dir / "instruments"
    candidates = (instruments_dir / "us_universe.parquet", instruments_dir / "us_universe.csv")
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        return None
    try:
        raw = pl.read_parquet(path) if path.suffix == ".parquet" else pl.read_csv(path)
        if raw.is_empty():
            return None
        columns = {column.lower().strip(): column for column in raw.columns}
        symbol_col = next((columns[key] for key in ("symbol", "ticker", "code") if key in columns), None)
        if symbol_col is None:
            logger.warning("美股本地 universe 缺少 symbol/ticker/code 字段: %s", path)
            return None
        name_col = next((columns[key] for key in ("name", "company", "中文名称") if key in columns), None)
        sector_col = next((columns[key] for key in ("sector",) if key in columns), None)
        industry_col = next((columns[key] for key in ("industry",) if key in columns), None)
        rows: list[dict] = []
        for row in raw.to_dicts():
            code = str(row.get(symbol_col) or "").strip().upper()
            if code.endswith(".US"):
                code = code[:-3]
            if not code or code.startswith("^"):
                continue
            rows.append({
                "symbol": f"{code}.US",
                "name": str(row.get(name_col) or code) if name_col else code,
                "code": code,
                "exchange": "US",
                "asset_type": "stock",
                "source": "us_universe_file",
                "sector": str(row.get(sector_col) or "") if sector_col else None,
                "industry": str(row.get(industry_col) or "") if industry_col else None,
            })
        result = normalize_instruments(rows, asset_type="stock", source="us_universe_file")
        return result if not result.is_empty() else None
    except Exception as exc:
        logger.warning("读取美股本地 universe 失败 %s: %s", path, exc)
        return None


def fetch_us_instruments_akshare() -> pl.DataFrame | None:
    """akshare 拉全市场美股池 (东财 stock_us_spot_em, 约 6000+ 只)。

    akshare 不可用或网络失败 → 返回 None (调用方降级为内置 15 龙头)。
    """
    ak = _try_import_akshare()
    if ak is None:
        logger.info("akshare 未安装, 跳过美股全市场池拉取")
        return None
    try:
        df = ak.stock_us_spot_em()
        if df is None or len(df) == 0:
            return None
        rows: list[dict] = []
        for r in df.to_dict(orient="records"):
            code = str(r.get("代码") or "").strip()
            if not code:
                continue
            rows.append({
                "symbol": f"{code}.US",
                "name": r.get("名称") or code,
                "code": code,
                "exchange": "US",
                "asset_type": "stock",
                "source": "akshare",
            })
        return normalize_instruments(rows, asset_type="stock", source="akshare")
    except Exception as e:
        logger.warning("akshare 美股池拉取失败: %s", e)
        return None


def sync_us_instruments(
    data_dir: Path,
    *,
    use_akshare: bool = True,
    allow_demo: bool = True,
) -> int:
    """同步美股 instruments 维表。

    ``allow_demo=False`` 用于自动全量日 K 闭环，获取不到真实全市场池时
    直接失败并保留已有快照，不写入 demo 数据。
    """
    from app.data_providers.yfinance_provider import US_DEMO_NAMES, US_DEMO_SYMBOLS

    demo_rows = [
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
    demo = normalize_instruments(demo_rows, asset_type="stock", source="us_demo")
    frames: list[pl.DataFrame] = []
    full = fetch_us_instruments_file(data_dir)
    if full is not None and not full.is_empty():
        frames.append(full)
    if not frames and use_akshare:
        full = fetch_us_instruments_akshare()
        if full is not None and not full.is_empty():
            frames.append(full)
    if not frames:
        if not allow_demo:
            raise RuntimeError("美股全量 instruments 获取失败，拒绝写入 demo 快照")
        frames.append(demo)
    elif allow_demo:
        frames.insert(0, demo)
    df = pl.concat(frames, how="vertical_relaxed")
    if not allow_demo and df.height < 500:
        raise RuntimeError(f"美股全量 instruments 仅获取 {df.height} 只，拒绝覆盖现有快照")
    if "market" not in df.columns:
        df = df.with_columns(
            pl.col("symbol").map_elements(resolve_market, return_dtype=pl.Utf8).alias("market")
        )
    df = df.unique(subset=["symbol"], keep="last").sort("symbol")
    out = data_dir / "instruments" / "us_instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    logger.info("美股 instruments 同步: %d 行 → %s", df.height, out)
    return df.height


def _fetch_hk_daily_sina_full(code: str) -> pl.DataFrame:
    """Compatibility reader using the same verified raw provider as batch jobs."""
    from app.data_providers.registry import get_default_provider

    symbol = normalize_market_symbols([code], "HK")[0]
    try:
        return get_default_provider("HK", dataset="daily").get_daily(
            [symbol], datetime(1970, 1, 1), datetime.now(HK_TZ), "stock",
        )
    except Exception:
        logger.warning("港股原始日线获取失败 %s", symbol, exc_info=True)
        return pl.DataFrame()


def fetch_hk_daily_akshare(
    symbol: str,
    start: date,
    end: date,
) -> pl.DataFrame:
    """Compatibility alias for normalized, unadjusted HK provider bars.

    Args:
        symbol: 5 位数字代码 (如 "00700") 或 "00700.HK" 内部格式
        start: 起始日期
        end: 截止日期
    """
    from app.data_providers.registry import get_default_provider

    key = normalize_market_symbols([symbol], "HK")[0]
    try:
        return get_default_provider("HK", dataset="daily").get_daily(
            [key], datetime.combine(start, time.min), datetime.combine(end, time.max), "stock",
        )
    except Exception:
        logger.warning("港股日线获取失败 %s", key, exc_info=True)
        return pl.DataFrame()


def fetch_hk_daily_sina(symbol: str) -> pl.DataFrame:
    """新浪源拉单只港股全量日 K (批量同步用)。失败时返回空 df。

    Args:
        symbol: 5 位数字代码 (如 "00700") 或 "00700.HK" 内部格式
    """
    code = symbol.split(".")[0]
    return _fetch_hk_daily_sina_full(code)


def _strip_market_suffix(symbol: str) -> str:
    """剥离 '.US'/'.HK' 市场后缀, 保留 class share 后缀。

    如 'BRK.A'→'BRK.A' (保留 class), 'AAPL.US'→'AAPL', 'BRK.A.US'→'BRK.A'。
    避免误删 'BRK.A' 这类含点 class share 的 '.A' 后缀 (否则会把 BRK.A 拉成
    不存在的 'BRK', 导致伯克希尔 A/B 类等标的缺失或错拉)。
    """
    code = str(symbol).strip().upper()
    if code.endswith(".US") or code.endswith(".HK"):
        code = code[:-3]
    return code


# ── 新浪美股日 K 解码器 (线程安全) ──────────────────────────────
# akshare 的 stock_us_daily 每次调用都会 new 一个 py_mini_racer.MiniRacer(V8),
# 而 V8 的 pool 初始化不是线程安全的 —— 多线程并发 new MiniRacer 会触发
# "Check failed: !IsConfigurablePoolInitialized()" 直接崩溃进程。
# 这里改用一个全局共享的 MiniRacer + 锁, 只把「解码」这段极短临界区串行化,
# 网络请求仍在各线程并发, 从而既线程安全又不牺牲吞吐。

_js_racer_lock = threading.Lock()
_js_racer: Any | None = None


def _decode_sina_us_data(encoded: str) -> list[dict]:
    """用共享 MiniRacer 解码新浪美股日 K 的 JS 编码串 (线程安全)。"""
    global _js_racer
    with _js_racer_lock:
        if _js_racer is None:
            import py_mini_racer  # type: ignore[import-untyped]
            from akshare.stock.cons import zh_js_decode  # type: ignore[import-untyped]
            _js_racer = py_mini_racer.MiniRacer()
            _js_racer.eval(zh_js_decode)
        return _js_racer.call("d", encoded)


def _fetch_us_daily_sina(code: str) -> pl.DataFrame:
    """新浪源拉单只美股日 K (未复权), 线程安全。失败返回空 df。

    直接抓 ``finance.sina.com.cn/staticdata/us/{code}`` 并用共享 MiniRacer 解码,
    绕开 akshare.stock_us_daily 每次 new V8 导致的并发崩溃。返回全历史 OHLCV
    + amount (新浪原始成交额, 非估算)。
    """
    import requests
    url = f"https://finance.sina.com.cn/staticdata/us/{code}"
    try:
        res = requests.get(url, timeout=15)
        if res.status_code != 200 or "=" not in res.text:
            return pl.DataFrame()
        encoded = res.text.split("=")[1].split(";")[0].replace('"', "")
        if not encoded:
            return pl.DataFrame()
        rows = _decode_sina_us_data(encoded)
        if not rows:
            return pl.DataFrame()
        df = pl.DataFrame(rows)
        # 归一化列: date 为 ISO 串 (如 '2026-09-03T00:00:00.000Z'), 转 μs datetime;
        # volume/amount 统一 Float64 对齐 enriched schema。
        out = pl.DataFrame({
            "symbol": f"{code}.US",
            "date": pl.Series(
                [str(r.get("date", "")).split("T")[0] for r in rows]
            ).str.to_date().cast(pl.Datetime("us")),
            "open": df["open"].cast(pl.Float64),
            "high": df["high"].cast(pl.Float64),
            "low": df["low"].cast(pl.Float64),
            "close": df["close"].cast(pl.Float64),
            "volume": df["volume"].cast(pl.Float64),
            "amount": df["amount"].cast(pl.Float64),
        })
        max_year = date.today().year + 1
        return out.filter(pl.col("date").dt.year() <= max_year)
    except Exception as e:
        logger.debug("新浪美股日 K 拉取失败 %s: %s", code, e)
        return pl.DataFrame()


def _fetch_us_daily_yfinance(code: str) -> pl.DataFrame:
    """yfinance 兜底拉单只美股日 K (覆盖新浪缺失的 class share / 次新股 / 低流动性标的)。

    class share 点号转破折号 (yfinance 用 'BRK-A' 而非 'BRK.A')。失败返回空 df。
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except Exception:
        return pl.DataFrame()
    yf_sym = code.replace(".", "-")
    try:
        hist = yf.Ticker(yf_sym).history(period="max", auto_adjust=False)
        if hist is None or hist.empty:
            return pl.DataFrame()
        hist = hist.reset_index()
        date_col = "Date" if "Date" in hist.columns else hist.columns[0]
        # date 用 .dt.date 归一化到零点 (与新浪源口径一致), 再 cast 到 μs 保证
        # 与港股/美股 enriched 分区 schema 一致 (避免 ms/μs 精度冲突)。
        # 注意: hist["Open"] 等是 pandas Series, 无 .cast 方法, 直接交给 polars
        # 自动转换; volume 需显式转 Float64 对齐新浪源 (见下方 with_columns)。
        out = pl.DataFrame({
            "symbol": f"{code}.US",
            "date": pl.Series(hist[date_col].dt.date).cast(pl.Datetime("us")),
            "open": hist["Open"],
            "high": hist["High"],
            "low": hist["Low"],
            "close": hist["Close"],
            "volume": hist["Volume"],
        })
        # yfinance Volume 原生为 Int64, 统一 cast 成 Float64 对齐新浪源,
        # 否则跨分区 scan_parquet 会因 volume Int64/Float64 不一致报 SchemaError。
        out = out.with_columns(pl.col("volume").cast(pl.Float64))
        # yfinance 不提供 amount, 用 close*volume 估算 (美元), 与新浪源口径一致。
        out = out.with_columns(
            (pl.col("close").cast(pl.Float64) * pl.col("volume").cast(pl.Float64)).alias("amount"),
            pl.lit("yfinance").alias("source"),
            pl.lit("unadjusted").alias("price_adjustment"),
            pl.lit("estimated_close_volume").alias("amount_source"),
            pl.lit("shares").alias("volume_unit"),
            pl.lit("USD").alias("currency"),
        )
        max_year = date.today().year + 1
        out = out.filter(pl.col("date").dt.year() <= max_year)
        return out
    except Exception as e:
        logger.warning("yfinance 美股日 K 拉取失败 %s: %s", code, e)
        return pl.DataFrame()


def fetch_us_daily_akshare(symbol: str) -> pl.DataFrame:
    """新浪源拉单只美股日 K, 失败时 yfinance 兜底。

    新浪源是免费美股日 K 主源 (线程安全, 见 _fetch_us_daily_sina); yfinance 作
    兜底 (新浪对 class share/次新股/低流动性标的覆盖不全)。两者都失败时返回
    空 df, 不抛错。

    Args:
        symbol: 裸 ticker (如 "AAPL" / "BRK.A") 或 "AAPL.US" 内部格式
    """
    code = _strip_market_suffix(symbol)
    df = _fetch_us_daily_sina(code)
    if not df.is_empty():
        return df
    return _fetch_us_daily_yfinance(code)


def _daily_symbol_key(symbol: str) -> str:
    """Validate the partition key while preserving US class-share dots."""
    return market_symbol_key(symbol)


# ── H6 日 K 落盘 ──

def sync_hk_daily_to_parquet(
    df: pl.DataFrame, symbol: str, data_dir: Path | None = None,
) -> Path | None:
    """单只港股/美股日 K 写入分区 parquet: data/kline_daily/symbol={symbol}/part.parquet。

    与 A 股同分区格式, DuckDB view 自动覆盖 (无需改 schema)。
    akshare 返回的 df 字段: symbol/date/open/high/low/close/volume。
    symbol 带后缀决定分区 (00700.HK → symbol=00700.HK, AAPL.US → symbol=AAPL.US)。
    """
    from app.config import settings
    return write_market_daily_symbol(data_dir or settings.data_dir, symbol, df)


def read_hk_daily(symbol: str, data_dir: Path | None = None) -> pl.DataFrame:
    """从落盘分区读取港股/美股日 K (供 dashboard 加速)。

    无数据 → 返回空 df, 不抛错。symbol 带后缀决定分区 (.HK / .US)。
    """
    from app.config import settings
    try:
        return read_market_daily_symbol(data_dir or settings.data_dir, symbol)
    except Exception as e:
        logger.warning("港美日 K 读取失败 %s: %s", symbol, e)
        return pl.DataFrame()


# ── H9/U9: 港美分时 (腾讯 minute/query, 分钟级, 不落盘) ──

_TENCENT_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/minute/query"


def _tencent_minute_code(symbol: str) -> str | None:
    """内部 symbol (00700.HK / AAPL.US) → 腾讯 minute/query 代码 (hk00700 / usAAPL)。

    BRK.A.US → usBRK.A (腾讯保留点号); 指数 ^HSI → hkHSI 不走本函数 (分时仅个股)。
    """
    s = str(symbol or "").strip().upper()
    if s.endswith(".HK"):
        code = s[:-3]
        if len(code) == 5 and code.isdigit():
            return f"hk{code}"
        return None
    if s.endswith(".US"):
        code = s[:-3]
        if code:
            return f"us{code}"
        return None
    return None


def fetch_hk_us_minute_tencent(symbol: str, timeout: float = 6.0) -> tuple[pl.DataFrame, str | None]:
    """拉取港美股当日 1 分钟分时 (腾讯 ifzq minute/query)。

    返回 (df, trade_date)。df 列: datetime, price, volume, avg_price(均价)。
    失败/无数据 → (空 df, None)。不落盘 —— 与 A股 /api/kline/minute 的 live 模式对齐,
    每次请求现拉 (前端分时刷新间隔 >= 6s, 单股单请求可承受)。
    """
    import httpx

    tc_code = _tencent_minute_code(symbol)
    if tc_code is None:
        return pl.DataFrame(), None
    try:
        r = httpx.get(_TENCENT_MINUTE_URL, params={"code": tc_code}, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        logger.warning("腾讯分时拉取失败 %s (%s): %s", symbol, tc_code, e)
        return pl.DataFrame(), None

    node = ((payload.get("data") or {}).get(tc_code) or {})
    data = node.get("data") or {}
    points: list[str] = data.get("data") or []
    date_str: str = str(data.get("date") or "")
    if not points or len(date_str) != 8:
        # 美股盘后/周末只回 1 点收盘快照且无 date → 视为无有效分时
        return pl.DataFrame(), None

    trade_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}" if len(date_str) == 8 else None
    rows = []
    for p in points:
        parts = p.split()
        if len(parts) < 3:
            continue
        hhmm, price_s, vol_s = parts[0], parts[1], parts[2]
        try:
            rows.append({
                "datetime": f"{trade_date} {hhmm[:2]}:{hhmm[2:]}:00" if trade_date else hhmm,
                "price": float(price_s),
                "volume": float(vol_s),
                "avg_price": float(parts[3]) if len(parts) >= 4 else None,
            })
        except (ValueError, IndexError):
            continue
    if not rows:
        return pl.DataFrame(), None
    df = pl.DataFrame(rows, schema={
        "datetime": pl.String, "price": pl.Float64, "volume": pl.Float64, "avg_price": pl.Float64,
    })
    return df, trade_date


# ── U3: 港股/美股 enriched 落盘 (独立目录, 供港美 overview/Screener 复用) ──

# 港美 enriched 独立顶层目录, 与 A 股 kline_daily_enriched 隔离。
# 根因: A 股 overview 走 kline_daily_enriched/date=* 分区, 若港美混入会
# 污染 as_of(max date) 与 board 映射。港美无复权/涨停/流通股本, 直接存
# compute_enriched 全量指标列, 按 symbol 分区 (每个标的全历史)。
_HK_US_ENRICHED_DIR = "kline_hk_us_enriched"


def _hk_audit_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _same_snapshot(old: pl.DataFrame, new: pl.DataFrame) -> bool:
    volatile = {"observed_at", "lot_size_observed_at"}
    columns = sorted(set(new.columns) - volatile)
    return bool(set(old.columns) - volatile == set(columns)
                and old.select(columns).equals(new.select(columns), null_equal=True))


def _marker_bytes(root: Path) -> bytes | None:
    path = root / ".matrix_generation_hk.json"
    return path.read_bytes() if path.exists() else None


def import_hk_raw_verification_archive(
    data_dir: Path, symbol: str, *, raw_response: bytes | str, source_url: str, observed_at: str,
) -> dict:
    """Import a dated original third-source response for exact conflict checks."""
    from app.data_providers.hk_daily_provider import (
        _validated_verification_archive,
        build_hk_raw_verification_archive,
    )

    archive = build_hk_raw_verification_archive(
        symbol, raw_response=raw_response, source_url=source_url, observed_at=observed_at,
    )
    path = (data_dir / "hk_data_audit" / "raw_verification" / archive["symbol"]
            / f"{archive['response_sha256']}.json")
    existing_valid = False
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            _validated_verification_archive(existing, archive["symbol"])
            if existing["response_sha256"] == archive["response_sha256"]:
                # The same original response remains the same evidence; a later
                # import must not re-date its first verified observation.
                archive = existing
                existing_valid = True
        except (OSError, KeyError, TypeError, ValueError):
            existing_valid = False
    if not existing_valid:
        _hk_audit_write(path, archive)
    return {**{key: value for key, value in archive.items() if key != "raw_response"},
            "archive_path": str(path)}


def load_hk_raw_verification_archives(data_dir: Path, symbols: list[str]) -> list[dict]:
    """Read evidence only; the provider independently checks its original hash."""
    archives: list[dict] = []
    for symbol in normalize_market_symbols(symbols, "HK"):
        directory = data_dir / "hk_data_audit" / "raw_verification" / symbol
        for path in sorted(directory.glob("*.json")):
            try:
                if path.stat().st_size > 16 * 1024 * 1024:
                    continue
                archive = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(archive, dict) and archive.get("symbol") == symbol:
                    archives.append(archive)
            except (OSError, ValueError):
                logger.warning("港股原始核验资料无法读取: %s", path.name)
    return archives


def _publish_hk_files(
    root: Path, writes: list[tuple[pl.DataFrame | dict, Path]], *,
    expected_marker: bytes | None, before_publish: Callable[[], None] | None = None,
) -> None:
    """Stage every file before claiming a generation; roll back failed commits.

    EnrichedPublication alone replaces files immediately. Its begin/commit gate
    is reused here, while old bytes and mtimes stay available until all writes,
    including the audit, have committed. Readers never see a mixed generation.
    """
    from app.enriched_generation import (
        EnrichedPublication,
        _exclusive_generation_lock,
        _read_marker,
    )

    if not writes:
        return
    root = root.resolve()
    if before_publish:
        before_publish()
    prepared: list[tuple[Path, Path, Path | None]] = []
    temporary_files: list[Path] = []
    class SnapshotPublication(EnrichedPublication):
        def _claim_or_verify(self) -> None:
            # begin() holds the generation lock across this check and the claim.
            # A competing commit cannot be overwritten by an older preparation.
            if not self._publishing and _marker_bytes(root) != expected_marker:
                raise ValueError("港股数据版本在准备期间已变化, 请重试本次同步")
            super()._claim_or_verify()

    publication = SnapshotPublication(root, "hk", recover=False)
    claimed = False
    rollback_complete = False
    try:
        for data, target in writes:
            target = target.resolve()
            if not target.is_relative_to(root):
                raise ValueError("港股发布目标不在数据目录内")
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary_files.append(staged)
            if isinstance(data, pl.DataFrame):
                data.write_parquet(staged)
            else:
                staged.write_text(json.dumps(data, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
            with staged.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            backup: Path | None = None
            if target.exists():
                backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.rollback")
                temporary_files.append(backup)
                shutil.copy2(target, backup)
            prepared.append((target, staged, backup))
        if before_publish:
            before_publish()
        publication.begin()
        claimed = True
        for target, staged, _ in prepared:
            os.replace(staged, target)
            publication.mark_changed()
        publication.commit()
        rollback_complete = True
    except BaseException:
        if claimed or publication._publishing:
            # The same generation lock prevents a simultaneous recovery writer
            # from declaring a failed multi-file snapshot ready during rollback.
            with _exclusive_generation_lock(root, "hk"):
                current = _read_marker(root / ".matrix_generation_hk.json")
                if current is None or current.get("publication_id") != publication._publication_id:
                    # Never undo a newer writer or a commit that already became
                    # ready. Preserve recovery copies for a manual investigation.
                    raise
                for target, _, backup in reversed(prepared):
                    if backup is None:
                        target.unlink(missing_ok=True)
                    elif backup.exists():
                        os.replace(backup, target)
                marker = root / ".matrix_generation_hk.json"
                if expected_marker is None:
                    marker.unlink(missing_ok=True)
                else:
                    restored = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
                    temporary_files.append(restored)
                    restored.write_bytes(expected_marker)
                    os.replace(restored, marker)
                publication._publishing = False
                rollback_complete = True
        else:
            rollback_complete = True
        raise
    finally:
        # Keep recovery copies if the filesystem also prevented rollback.
        if rollback_complete:
            for temporary in temporary_files:
                temporary.unlink(missing_ok=True)


def _hk_factors_for_window(root: Path, symbol: str, raw: pl.DataFrame, factors: pl.DataFrame | None) -> pl.DataFrame:
    if factors is None or factors.is_empty():
        path = root / "adj_factor_hk" / f"symbol={symbol}" / "part.parquet"
        factors = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    required = {"symbol", "trade_date", "ex_factor", "source", "version", "coverage_end"}
    if factors.is_empty() or not required.issubset(factors.columns):
        raise ValueError("港股复权因子缺失, 保留上次有效指标")
    if factors.filter((pl.col("symbol") != symbol).fill_null(True)).height:
        raise ValueError("港股复权因子证券身份冲突")
    factors = factors.with_columns(pl.col("trade_date").cast(pl.Date), pl.col("coverage_end").cast(pl.Date))
    if (factors["version"].null_count() or factors["version"].n_unique() != 1
            or factors["source"].null_count() or factors["source"].n_unique() != 1
            or factors["coverage_end"].null_count() or factors["coverage_end"].min() < raw["date"].max()):
        raise ValueError("港股复权快照覆盖不足或版本冲突, 不能将末条因子延伸到新日期")
    if factors.filter(pl.col("ex_factor").is_null() | ~pl.col("ex_factor").is_finite() | (pl.col("ex_factor") <= 0)).height:
        raise ValueError("港股复权因子必须为有限正数")
    if factors["trade_date"].min() > raw["date"].min():
        raise ValueError("港股复权快照缺少维护窗口的起始基准")
    return factors.sort("trade_date")


def _compute_market_enriched(root: Path, symbol: str, raw: pl.DataFrame, factors: pl.DataFrame | None = None) -> pl.DataFrame:
    from app.data_providers.hk_daily_provider import PRICE_METADATA_COLUMNS
    from app.indicators.pipeline import compute_enriched

    raw = raw.with_columns(pl.col("date").cast(pl.Date))
    if "amount" not in raw.columns:
        raw = raw.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    if symbol.endswith(".HK"):
        if not is_verified_hk_raw(raw):
            raise ValueError("港股历史价格口径未知或已复权, 不能冒充原始价格重算; 请完整重取维护窗口")
        factors = _hk_factors_for_window(root, symbol, raw, factors)
        active = factors.filter(pl.col("trade_date") <= raw["date"].max())
        raw = raw.with_columns(
            pl.col("open").alias("raw_open"),
            pl.lit(factors["source"][0]).alias("adjustment_source"),
            pl.lit(factors["version"][0]).alias("adjustment_version"),
            pl.lit(factors["coverage_end"].min()).cast(pl.Date).alias("adjustment_as_of"),
        )
        enriched = compute_enriched(raw, factors=active, instruments=None)
    else:
        enriched = compute_enriched(raw, factors=None, instruments=None)
    if enriched.is_empty():
        return enriched
    metadata = [name for name in PRICE_METADATA_COLUMNS if name in raw.columns and name not in enriched.columns]
    if metadata:
        enriched = enriched.join(raw.select("symbol", "date", *metadata), on=["symbol", "date"], how="left")
    if symbol.endswith(".HK"):
        enriched = enriched.with_columns(pl.lit("forward_adjusted").alias("price_adjustment"))
    return enriched


def _repair_backup(root: Path, symbol: str, old: pl.DataFrame, incoming: pl.DataFrame) -> dict:
    """Prepare a reviewable backup before replacing a complete legacy window."""
    repair_id = uuid.uuid4().hex
    directory = root / "hk_data_audit" / "backups" / repair_id
    directory.mkdir(parents=True, exist_ok=False)
    originals: list[dict] = []
    for dataset in ("kline_daily", _HK_US_ENRICHED_DIR, "adj_factor_hk"):
        source = root / dataset / f"symbol={symbol}" / "part.parquet"
        if source.exists():
            backup = directory / f"{dataset}.parquet"
            shutil.copy2(source, backup)
            originals.append({"path": source.relative_to(root).as_posix(),
                              "backup": backup.relative_to(root).as_posix(),
                              "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    # Date partitions remain untouched; retain the actual per-symbol rows used
    # to define the replacement range without copying unrelated CN history.
    old.write_parquet(directory / "legacy-symbol-rows.parquet")
    manifest = {"repair_id": repair_id, "symbol": symbol, "state": "prepared",
                "created_at": datetime.now(UTC).isoformat(), "originals": originals,
                "old_start": old["date"].min(), "old_end": old["date"].max(), "old_rows": old.height,
                "new_start": incoming["date"].min(), "new_end": incoming["date"].max(), "new_rows": incoming.height,
                "legacy_date_partitions": "preserved"}
    _hk_audit_write(directory / "manifest.json", manifest)
    return manifest


_COVERAGE_GAP_RECENT_DAYS = 90


def _coverage_gap_only_historical(report: dict) -> tuple[bool, int, str | None]:
    """coverage 缺口是否全部落在远期 (日历/源口径差异), 而非近期数据丢失。

    09-16 全窗口重跑实证: 00005.HK / 00002.HK 等出现 12~13 天缺口, 日期是
    2023-07-17 (台风泰利停市) / 2020-10-13 (台风浪卡停市) / 2009-01-01 (元旦)
    / 2007-12-24 (平安夜半日市) 之类 —— 新浪没有这些行是对的, 是交易日历
    把它们算了进去。这类缺口若阻断 enriched, 2794 只 legacy 标的会全停摆
    (coverage_complete=False 直接跳过指标重算, 随后 repair 分支抛错)。

    近期 (默认 90 天) 缺口仍然阻断: 那才是数据源退化/拉取截断的真信号。
    返回 (是否仅远期缺口, 缺口天数, 最新缺口日期)。
    """
    missing = report.get("missing_dates")
    if not missing:
        return False, 0, None  # 无明细不擅自放行 (fail closed)
    latest: date | None = None
    count = 0
    for value in missing:
        try:
            day = date.fromisoformat(str(value)[:10])
        except ValueError:
            return False, 0, None
        count += 1
        if latest is None or day > latest:
            latest = day
    cutoff = datetime.now(HK_TZ).date() - timedelta(days=_COVERAGE_GAP_RECENT_DAYS)
    return latest < cutoff, count, latest.isoformat()


def _drop_unclosed_session_tail(symbol: str, merged: pl.DataFrame, incoming_max: date) -> pl.DataFrame:
    """剔除旧分区残留的"未收盘当日"占位行 (只删今日及以后, 其余仍 fail-closed)。

    09-16 实证 00005.HK: 腾讯备用源盘中会返回当日未完成 K 线, 曾落盘为
    2026-09-16 (成交量仅为前一日的 1/4)。腾讯随后被 WAF 全站 501, 新拉取只剩
    新浪 (止于已收盘的 2026-09-15)。于是 merged.max (09-16) 高于内存复权快照的
    coverage_end (09-15), `_hk_factors_for_window` 报"覆盖不足或版本冲突" →
    该标的永久卡死, 无法自愈 (旧分区里那行永远比新源新一天)。

    当日占位行本质是"未收盘会话", 新拉取不再产出它就应剔除而非阻断。
    护栏: 只剔除晚于新拉取最新日期、且不早于今日 (HK) 的行。今日之前的日期
    不可能是"未收盘当日", 被新源漏掉属于真实近期历史丢失, 依然 fail-closed。
    """
    if merged.is_empty():
        return merged
    dated = merged.with_columns(pl.col("date").cast(pl.Date))
    extra = dated.filter(pl.col("date") > incoming_max)
    if extra.is_empty():
        return dated
    stale_days = {_as_plain_date(value) for value in extra["date"].to_list()}
    today_hk = datetime.now(HK_TZ).date()
    if not all(day >= today_hk for day in stale_days):
        raise ValueError(
            "旧分区含新拉取未覆盖的更近历史日期 (非当日占位行), 拒绝发布: "
            + ", ".join(str(day) for day in sorted(stale_days)[:8])
        )
    logger.warning(
        "hk %s: 剔除 %d 个未收盘当日占位行 (%s), 新拉取仅到 %s",
        symbol, extra.height, ", ".join(str(day) for day in sorted(stale_days)[:5]), incoming_max,
    )
    return dated.filter(pl.col("date") <= incoming_max)


def publish_hk_daily_snapshot(
    root: Path, symbol: str, raw: pl.DataFrame, *, factors: pl.DataFrame | None = None,
    item: dict | None = None, before_publish: Callable[[], None] | None = None,
    legacy: pl.DataFrame | None = None,
    verification_archives: list[dict] | None = None,
) -> dict:
    """Prepare raw/factors/enriched before versioned publication of one symbol."""
    symbol = normalize_market_symbols([symbol], "HK")[0]
    expected_marker = _marker_bytes(root)
    report = {**(item or {}), "symbol": symbol, "raw_updated": False, "enriched_updated": False}
    if raw.is_empty() or raw.filter((pl.col("symbol") != symbol).fill_null(True)).height:
        raise ValueError("港股原始日线证券身份无效或为空")
    raw = raw.with_columns(pl.col("date").cast(pl.Date))
    old = read_market_daily_symbol(root, symbol, legacy=legacy)
    if "currency" in raw.columns and raw.get_column("currency").null_count() == raw.height:
        # A backup-source outage leaves currency unknown; a listed counter's
        # currency never changes, so reuse the previously verified partition
        # identity instead of discarding the whole primary-source update.
        legacy_currencies = (
            old.get_column("currency").drop_nulls().unique().to_list()
            if not old.is_empty() and "currency" in old.columns else []
        )
        if is_verified_hk_raw(old) and len(legacy_currencies) == 1:
            raw = raw.with_columns(pl.lit(legacy_currencies[0], dtype=pl.String).alias("currency"))
    if not is_verified_hk_raw(raw):
        raise ValueError("港股原始日线的币种、量单位或价格口径未核实")
    report.update(currency=raw["currency"][0], volume_unit="share", price_adjustment="unadjusted")
    repair = not old.is_empty() and not is_verified_hk_raw(old)
    merged = merge_market_daily_frames(old, raw, symbol, replace_legacy=True)
    merged = _drop_unclosed_session_tail(symbol, merged, raw["date"].max())
    validated_factors: pl.DataFrame | None = None
    enriched = pl.DataFrame()
    try:
        validated_factors = _hk_factors_for_window(root, symbol, merged, factors)
        coverage_ok = bool(report.get("coverage_complete", True))
        if not coverage_ok:
            historical_only, gap_days, latest_gap = _coverage_gap_only_historical(report)
            if historical_only:
                logger.warning(
                    "hk %s: coverage 缺口 %d 天均为远期日历/源口径差异 (最新 %s, 早于近 %d 天), "
                    "不阻断指标重算",
                    symbol, gap_days, latest_gap, _COVERAGE_GAP_RECENT_DAYS,
                )
                coverage_ok = True
            elif report.get("calendar_error"):
                # 交易日历来自腾讯 hkHSI; 腾讯 WAF 501 时日历不可用 (09-16 实证
                # 全市场 501), coverage 无法判定。此时不阻断: 防"数据源退化丢
                # 近期历史"的责任由 merge 闸门承担 (新数据必须推进到旧分区最新
                # 日期, 且近期缺口一律拒绝)。
                logger.warning(
                    "hk %s: 交易日历不可用 (%s), coverage 无法判定, 不阻断指标重算",
                    symbol, report.get("calendar_error"),
                )
                coverage_ok = True
        if coverage_ok:
            enriched = _compute_market_enriched(root, symbol, merged, validated_factors)
            # 增量写盘前对齐目录既有 schema, 防止新旧混存炸全目录 scan。
            enriched = adapt_enriched_for_write(enriched, root, symbol)
        else:
            report.update(status="partial", reason_code="coverage_incomplete",
                          reason="原始日线的请求覆盖尚未确认, 保留上次有效指标")
    except ValueError as exc:
        report.update(status="partial", reason_code="adjustment_unavailable", reason=str(exc))
    if repair and enriched.is_empty():
        raise ValueError("旧价格口径维护窗口尚未具备完整原始价与复权因子, 已保留原文件")
    if validated_factors is not None:
        merged = merged.with_columns(
            pl.lit(validated_factors["source"][0]).alias("adjustment_source"),
            pl.lit(validated_factors["version"][0]).alias("adjustment_version"),
            pl.lit(validated_factors["coverage_end"].min()).cast(pl.Date).alias("adjustment_as_of"),
        )
        report.update(adjustment_source=validated_factors["source"][0],
                      adjustment_version=validated_factors["version"][0],
                      adjustment_as_of=validated_factors["coverage_end"].min().isoformat(),
                      adjustment_cached=factors is None or factors.is_empty())
    if before_publish:
        before_publish()
    manifest = _repair_backup(root, symbol, old, merged) if repair else None
    paths = [(merged, root / "kline_daily" / f"symbol={symbol}" / "part.parquet", "raw_updated")]
    if validated_factors is not None:
        paths.append((validated_factors, root / "adj_factor_hk" / f"symbol={symbol}" / "part.parquet", "factor_updated"))
    if not enriched.is_empty():
        paths.append((enriched, root / _HK_US_ENRICHED_DIR / f"symbol={symbol}" / "part.parquet", "enriched_updated"))
    writes: list[tuple[pl.DataFrame | dict, Path]] = []
    for frame, path, flag in paths:
        previous = pl.read_parquet(path) if path.exists() else pl.DataFrame()
        if not previous.is_empty() and _same_snapshot(previous, frame):
            continue
        writes.append((frame, path))
        report[flag] = True
    if verification_archives:
        from app.data_providers.hk_daily_provider import _validated_verification_archive

        for archive in verification_archives:
            _validated_verification_archive(archive, symbol)
            path = (root / "hk_data_audit" / "raw_verification" / symbol
                    / f"{archive['response_sha256']}.json")
            existing_valid = False
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    _validated_verification_archive(existing, symbol)
                    existing_valid = existing["response_sha256"] == archive["response_sha256"]
                except (OSError, KeyError, TypeError, ValueError):
                    pass
            if not existing_valid:
                writes.append((archive, path))
    if not enriched.is_empty():
        changed = any(report.get(flag) for flag in ("raw_updated", "factor_updated", "enriched_updated"))
        report.update(status="ok" if changed else "unchanged", reason=None, reason_code=None,
                      enriched_available=True)
    else:
        report["enriched_available"] = False
    audit = {"symbol": symbol, "status": "verified" if not enriched.is_empty() else "partial",
             "verified_start": merged["date"].min() if not enriched.is_empty() else None,
             "verified_end": merged["date"].max() if not enriched.is_empty() else None,
             "raw_start": merged["date"].min(), "raw_end": merged["date"].max(),
             "source": merged["source"].unique().sort().to_list(),
             "adjustment_source": report.get("adjustment_source"),
             "adjustment_version": report.get("adjustment_version"),
             "adjustment_as_of": report.get("adjustment_as_of"),
             "currency": merged["currency"][0], "volume_unit": "share",
             "last_checked_at": datetime.now(UTC).isoformat(), "reason": report.get("reason"),
             "verification_source": report.get("verification_source"),
             "verification_cached": report.get("verification_cached", False),
             "source_conflicts": report.get("source_conflicts", []),
             "repair_id": manifest["repair_id"] if manifest else None}
    if any(report.get(flag) for flag in ("raw_updated", "factor_updated", "enriched_updated")):
        writes.append((audit, root / "hk_data_audit" / "prices" / f"{symbol}.json"))
    if manifest:
        manifest["state"] = "completed"
        writes.append((manifest, root / "hk_data_audit" / "backups" / manifest["repair_id"] / "manifest.json"))
        report["repair_id"] = manifest["repair_id"]
    _publish_hk_files(root, writes, expected_marker=expected_marker, before_publish=before_publish)
    return report


def _list_market_daily_symbols(root: Path, market: str = "HK") -> list[str]:
    """Scan only the requested market, including the older date layout."""
    return list_market_daily_symbols(root, market)


def sync_hk_daily_to_enriched(
    symbol: str,
    data_dir: Path | None = None,
    *,
    raw: pl.DataFrame | None = None,
    raise_errors: bool = False,
    before_publish: Callable[[], None] | None = None,
) -> int:
    """把单只港股/美股日 K 计算成 enriched 并落盘独立目录 kline_hk_us_enriched/symbol={key}/part.parquet。

    前置: data/kline_daily/symbol={code}.HK|.US/part.parquet 存在 (H6 已写)。
    复用 compute_enriched (A 股同款, 无 instruments → 涨停/换手字段为 null)。
    存 compute_enriched 全量指标列 (ma/change_pct/annual_vol 等), 按 symbol 分区
    存全历史, 与 A 股 kline_daily_enriched 完全隔离。

    Returns:
        写入的分区数 (1=成功, 0=无数据/失败)。
    """
    from app.config import settings as _settings

    root = data_dir or _settings.data_dir
    key = _daily_symbol_key(symbol)
    expected_marker = _marker_bytes(root) if key.endswith(".HK") else None
    try:
        df = raw if raw is not None else read_market_daily_symbol(root, key)
    except Exception:
        if raise_errors:
            raise
        logger.warning("港美日 K 读取失败 %s", key, exc_info=True)
        return 0
    if df.is_empty():
        return 0
    # Missing turnover cannot be replaced with a synthetic traded amount. Older
    # files with explicit estimates stay readable; provenance is shown in status.
    if "amount" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    try:
        enriched = _compute_market_enriched(root, key, df)
        # 增量写盘前对齐目录既有 schema (旧 64 列 Datetime / 新 76 列 Date),
        # 防止单只增量引入第二套 schema 炸全目录 scan_parquet。
        enriched = adapt_enriched_for_write(enriched, root, key)
    except Exception as e:
        if raise_errors:
            raise
        logger.warning("港美 enriched 计算失败 %s: %s", symbol, e)
        return 0
    if enriched.is_empty():
        return 0

    key = _daily_symbol_key(symbol)  # AAPL.US / 00001.HK
    out_dir = root / _HK_US_ENRICHED_DIR / f"symbol={key}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "part.parquet"
    if out.exists() and _same_snapshot(pl.read_parquet(out), enriched):
        return 1
    if before_publish:
        before_publish()
    if key.endswith(".HK"):
        _publish_hk_files(root, [(enriched, out)], expected_marker=expected_marker,
                          before_publish=before_publish)
    else:
        from app.enriched_generation import EnrichedPublication

        publication = EnrichedPublication(root, "us", recover=True)
        publication.write_parquet(enriched, out)
        publication.commit()
    logger.info("港美 enriched 写入 %s: %d 行", key, enriched.height)
    return 1


def sync_all_hk_daily_to_enriched(
    symbols: list[str] | None = None,
    *,
    market: str = "HK",
    data_dir: Path | None = None,
) -> int:
    """Recompute one market only; callers selecting US must pass market='US'."""
    from app.config import settings as _settings
    from app.tickflow.market_daily import read_legacy_market_daily

    root = data_dir or _settings.data_dir
    if symbols is None:
        symbols = _list_market_daily_symbols(root, market)
    symbols = normalize_market_symbols(symbols, market)
    legacy = read_legacy_market_daily(root, market, symbols)
    written = 0
    for sym in symbols:
        raw = read_market_daily_symbol(root, sym, legacy=legacy)
        written += sync_hk_daily_to_enriched(sym, root, raw=raw)
    return written


def sync_hk_lot_sizes(
    data_dir: Path, *, metadata: pl.DataFrame | None = None,
    observed_at: str | None = None, before_publish: Callable[[], None] | None = None,
) -> dict:
    """Enrich the frozen pool from current or explicitly supplied archived evidence.

    ``metadata`` accepts the same HKEX parser output. It is an internal import
    boundary for a verifiable archive, not a path embedded in production code.
    Future or conflicting evidence cannot become an executable board lot.
    """
    from app.data_providers.hkex_instruments import fetch_hkex_lot_sizes
    from app.services.market_data_status import market_data_generation

    path = data_dir / "instruments" / "hk_instruments.parquet"
    if not path.exists():
        return {"status": "empty", "market": "HK", "operation": "lot_size_sync",
                "requested": 0, "succeeded": 0, "failed": 0, "skipped": 0,
                "items": [], "failures": [], "enriched_dates_written": 0,
                "data_generation": market_data_generation(data_dir, "HK"),
                "message": "请先同步港股标的池"}
    expected_marker = _marker_bytes(data_dir)
    existing = pl.read_parquet(path)
    if "symbol" not in existing.columns:
        raise ValueError("港股标的池缺少 symbol 字段")
    symbols = normalize_market_symbols(existing["symbol"].to_list(), "HK")
    if len(symbols) != existing.height:
        raise ValueError("港股标的池有重复代码, 不能隐式合并每手元数据")
    lots = fetch_hkex_lot_sizes() if metadata is None else metadata
    if lots.is_empty() or not {"symbol", "lot_size", "lot_size_source", "lot_size_as_of"}.issubset(lots.columns):
        raise ValueError("每手来源没有完整的来源、资料日期和每手列")
    source_rows: dict[str, list[dict]] = {}
    for candidate in lots.to_dicts():
        key = normalize_market_symbols([candidate["symbol"]], "HK")[0]
        source_rows.setdefault(key, []).append(candidate)
    today = datetime.now(HK_TZ).date()
    now = observed_at or datetime.now(UTC).isoformat()
    legacy_hkd = "currency" not in existing.columns and "lot_size_status" not in existing.columns

    def as_date(value: Any) -> date | None:
        try:
            return date.fromisoformat(str(value)[:10]) if value is not None else None
        except ValueError:
            return None

    rows: list[dict] = []
    items: list[dict] = []
    before_available = 0
    for row in existing.to_dicts():
        symbol = row["symbol"]
        old_lot = normalize_lot_size(row.get("lot_size"))
        old_date = as_date(row.get("lot_size_as_of"))
        old_status = row.get("lot_size_status")
        trusted = bool(old_lot and old_date and old_date <= today and row.get("lot_size_source")
                       and old_status not in {"future_snapshot", "missing", "conflict"})
        before_available += int(trusted)
        row["lot_size"] = old_lot
        row["lot_size_as_of"] = old_date
        row["lot_size_effective_from"] = as_date(row.get("lot_size_effective_from"))
        row["lot_size_observed_at"] = row.get("lot_size_observed_at")
        row["lot_size_status"] = "verified_snapshot" if trusted else old_status or "missing"
        if legacy_hkd and trusted and row.get("lot_size_source") == "hkex_list_of_securities":
            # The previous HKEX adapter explicitly accepted only HKD rows.
            row["currency"] = "HKD"
        else:
            row.setdefault("currency", None)
        row.setdefault("lot_size_source", None)
        candidates = source_rows.get(symbol, [])
        item: dict = {"symbol": symbol, "status": "skipped", "reason": "官方资料未匹配此代码; 保留原证券及可信值", "reason_code": "instrument_unmatched"}
        if candidates:
            candidate = candidates[0]
            lot = normalize_lot_size(candidate.get("lot_size"))
            currency = str(candidate.get("currency") or "").upper()
            currency = "CNY" if currency == "RMB" else currency
            currency = currency if currency in {"HKD", "CNY", "USD"} else None
            as_of = as_date(candidate.get("lot_size_as_of"))
            effective = as_date(candidate.get("lot_size_effective_from"))
            source = candidate.get("lot_size_source")
            candidate_conflict = (candidate.get("lot_size_status") == "conflict"
                                  or len({(value.get("lot_size"), value.get("currency"), str(value.get("lot_size_as_of"))) for value in candidates}) > 1
                                  or bool(trusted and old_date == as_of and (old_lot != lot or row.get("currency") not in (None, currency))))
            item.update(source=source, source_as_of=as_of.isoformat() if as_of else None,
                        candidate_lot_size=lot, candidate_currency=currency,
                        candidate_as_of=as_of.isoformat() if as_of else None,
                        observed_at=candidate.get("lot_size_observed_at") or now)
            row.update(candidate_lot_size=lot, candidate_currency=currency, candidate_as_of=as_of)
            instrument_status = candidate.get("instrument_status")
            status_as_of = as_date(candidate.get("instrument_status_as_of"))
            status_source = candidate.get("instrument_status_source")
            inactive = (instrument_status in {"delisted", "temporary_counter_closed", "rights_trading_ended"}
                        and status_as_of is not None and status_as_of <= today and bool(status_source))
            if inactive:
                for name in ("instrument_status", "instrument_status_source", "instrument_status_reason"):
                    row[name] = candidate.get(name)
                row["instrument_status_as_of"] = status_as_of
                row["lot_size_effective_to"] = as_date(candidate.get("lot_size_effective_to"))
                item.update(applicability="verified_not_applicable", reason=candidate.get("instrument_status_reason") or "公告确认该柜台目前已停止交易", reason_code="instrument_inactive")
                item.update({name: row.get(name) for name in ("instrument_status", "instrument_status_as_of", "instrument_status_source")})
                if isinstance(item.get("instrument_status_as_of"), date):
                    item["instrument_status_as_of"] = item["instrument_status_as_of"].isoformat()
            elif candidate_conflict:
                row["lot_size_status"] = "conflict"
                item.update(status="failed", reason="同一来源时点存在每手或币种冲突, 禁止成交", reason_code="lot_size_conflict")
            elif as_of is None or lot is None or currency is None or not source:
                if not trusted:
                    row["lot_size_status"] = "missing"
                item.update(reason="官方资料缺少可信每手、币种或资料日期", reason_code="lot_size_missing")
            elif as_of > today or (effective is not None and effective > today):
                if not trusted:
                    row["lot_size_status"] = "future_snapshot"
                item.update(reason="资料日期或生效日期尚未到达, 候选值未覆盖当前可信每手", reason_code="lot_size_future")
            elif trusted and old_date > as_of:
                item.update(status="unchanged", reason="保留资料日期更新的可信快照", reason_code="older_snapshot")
            else:
                row.update(lot_size=lot, currency=currency, lot_size_source=source,
                           lot_size_as_of=as_of, lot_size_observed_at=candidate.get("lot_size_observed_at") or now,
                           lot_size_effective_from=effective, lot_size_status="verified_snapshot")
                item.update(status="ok", reason=None, reason_code=None)
        elif (row.get("instrument_status") in {"delisted", "temporary_counter_closed", "rights_trading_ended"}
              and row.get("instrument_status_source")
              and (as_date(row.get("instrument_status_as_of")) or date.max) <= today):
            item.update(applicability="verified_not_applicable", reason_code="instrument_inactive",
                        reason=row.get("instrument_status_reason") or "已有公告确认该柜台目前已停止交易",
                        instrument_status=row["instrument_status"],
                        instrument_status_as_of=as_date(row["instrument_status_as_of"]).isoformat(),
                        instrument_status_source=row["instrument_status_source"])
        item.update(lot_size=row["lot_size"], currency=row["currency"], lot_size_status=row["lot_size_status"])
        rows.append(row)
        items.append(item)
    joined = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.col("lot_size").cast(pl.Int64), pl.col("lot_size_as_of").cast(pl.Date),
        pl.col("lot_size_effective_from").cast(pl.Date), pl.col("currency").cast(pl.String),
        pl.col("lot_size_source").cast(pl.String), pl.col("lot_size_observed_at").cast(pl.String),
    )
    if "candidate_lot_size" in joined.columns:
        joined = joined.with_columns(pl.col("candidate_lot_size").cast(pl.Int64), pl.col("candidate_currency").cast(pl.String), pl.col("candidate_as_of").cast(pl.Date))
    if "instrument_status_as_of" in joined.columns:
        joined = joined.with_columns(pl.col("instrument_status_as_of").cast(pl.Date), pl.col("lot_size_effective_to").cast(pl.Date))
    changed = not _same_snapshot(existing, joined)
    succeeded = sum(item["status"] in {"ok", "unchanged"} for item in items)
    failed = sum(item["status"] == "failed" for item in items)
    skipped = len(items) - succeeded - failed
    inactive_symbols = {item["symbol"] for item in items if item.get("applicability") == "verified_not_applicable"}
    available = sum(row["symbol"] not in inactive_symbols and normalize_lot_size(row.get("lot_size")) is not None
                    and row["lot_size_status"] == "verified_snapshot" for row in rows)
    result = {"status": "completed" if succeeded + len(inactive_symbols) == len(symbols) else "completed_with_errors" if succeeded else "failed" if failed else "empty",
              "market": "HK", "operation": "lot_size_sync", "requested": len(symbols),
              "succeeded": succeeded, "failed": failed, "skipped": skipped, "items": items,
              "source": "hkex_list_of_securities", "enriched_dates_written": 0,
              "data_generation": market_data_generation(data_dir, "HK"),
              "before_available": before_available, "after_available": available,
              "verified_not_applicable": len(inactive_symbols),
              "pool_version": hashlib.sha256("\n".join(sorted(symbols)).encode()).hexdigest(),
              "failures": [{"symbol": item["symbol"], "reason": item["reason"]} for item in items
                           if item["status"] not in {"ok", "unchanged"} and item["symbol"] not in inactive_symbols],
              "as_of": str(lots["lot_size_as_of"].max()), "observed_at": now}
    audit_path = data_dir / "hk_data_audit" / "lot_sizes" / "latest.json"
    audit = {key: value for key, value in result.items() if key != "data_generation"}
    if changed:
        _publish_hk_files(data_dir, [(joined, path), (audit, audit_path)],
                          expected_marker=expected_marker, before_publish=before_publish)
    else:
        _hk_audit_write(audit_path, audit)
    result["data_generation"] = market_data_generation(data_dir, "HK")
    return result
