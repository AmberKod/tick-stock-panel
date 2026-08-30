"""港股数据适配 (M1)。

职责:
- 静态内置 10 个港股龙头池 (M1 起步, 验证 quickquote 通路)
- 拉 akshare 全市场池 (用户机器装 akshare 时自动启用, 不可用时静默降级)
- 日 K 落盘接口占位 (走 akshare stock_hk_hist, 失败时返回空, 不阻塞主流程)

设计取舍:
- 静态池不依赖外部网络, 永远可用
- akshare 全市场池作为"扩展能力", 失败时日志告警但不抛错
- 与 A 股 instruments.parquet 同 schema, market 派生列已就位 (M0)
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.data_providers.normalizer import INSTRUMENT_COLS, normalize_instruments
from app.markets.registry import resolve_market

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
    """akshare 拉全市场港股池。akshare 不可用或网络失败时返回 None。

    返回 df 与 A 股同 schema (含 market 派生列, 经 normalize_instruments 处理)。
    """
    ak = _try_import_akshare()
    if ak is None:
        logger.info("akshare 未安装, 跳过港股全市场池拉取")
        return None
    try:
        df = ak.stock_hk_spot_em()
        if df is None or len(df) == 0:
            return None
        # akshare 列名约定: 代码(5位)/名称
        rows: list[dict] = []
        for r in df.to_dict(orient="records"):
            code = str(r.get("代码") or "").zfill(5)
            if len(code) != 5 or not code.isdigit():
                continue
            rows.append({
                "symbol": f"{code}.HK",
                "name": r.get("名称") or code,
                "code": code,
                "exchange": "HK",
                "asset_type": "stock",
                "source": "akshare",
            })
        return normalize_instruments(rows, asset_type="stock", source="akshare")
    except Exception as e:  # noqa: BLE001
        logger.warning("akshare 港股池拉取失败: %s", e)
        return None


def sync_hk_instruments(data_dir: Path, *, use_akshare: bool = True) -> int:
    """同步港股 instruments 维表 → data/instruments/hk_instruments.parquet。

    落盘 schema 与 A 股一致 (含 market 派生列), 供跨市场查询统一接口使用。
    M1 行为: 写内置 10 龙头 + (可选) akshare 全市场池。

    Returns:
        写入的行数。
    """
    demo = load_demo_instruments()
    frames: list[pl.DataFrame] = [demo]
    if use_akshare:
        full = fetch_hk_instruments_akshare()
        if full is not None and not full.is_empty():
            frames.append(full)
    df = pl.concat(frames, how="vertical_relaxed")
    # market 列已由 normalize_instruments 派生, 这里再保险确认
    if "market" not in df.columns:
        df = df.with_columns(
            pl.col("symbol").map_elements(resolve_market, return_dtype=pl.Utf8).alias("market")
        )
    df = df.unique(subset=["symbol"], keep="last").sort("symbol")
    out = data_dir / "instruments" / "hk_instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    logger.info("港股 instruments 同步: %d 行 → %s", df.height, out)
    return df.height


def fetch_hk_daily_akshare(
    symbol: str,
    start: date,
    end: date,
) -> pl.DataFrame:
    """akshare 拉单只港股日 K。M1 占位, 失败时返回空 df。

    Args:
        symbol: 5 位数字代码 (如 "00700") 或 "00700.HK" 内部格式
        start: 起始日期
        end: 截止日期
    """
    ak = _try_import_akshare()
    if ak is None:
        return pl.DataFrame()
    code = symbol.split(".")[0]  # 兼容 .HK 后缀
    try:
        df = ak.stock_hk_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",  # 前复权 (ex_factor 由调用方按需要换算)
        )
        if df is None or len(df) == 0:
            return pl.DataFrame()
        # 标准化为内部 schema
        return pl.DataFrame({
            "symbol": f"{code}.HK",
            "date": df["日期"],
            "open": df["开盘"],
            "high": df["最高"],
            "low": df["最低"],
            "close": df["收盘"],
            "volume": df["成交量"],
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("akshare 港股日 K 拉取失败 %s: %s", code, e)
        return pl.DataFrame()


# ── H6 日 K 落盘 ──

def sync_hk_daily_to_parquet(df: pl.DataFrame, symbol: str) -> Path | None:
    """单只港股日 K 写入分区 parquet: data/kline_daily/symbol={symbol}/part.parquet。

    与 A 股同分区格式, DuckDB view 自动覆盖 (无需改 schema)。
    akshare 返回的 df 字段: symbol/date/open/high/low/close/volume。
    """
    if df.is_empty():
        return None
    code = symbol.split(".")[0]
    from app.config import settings
    out_dir = settings.data_dir / "kline_daily" / f"symbol={code}.HK"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "part.parquet"
    # 已有数据则 merge 新行 (按 date 去重, 保留最后写入)
    if out.exists():
        try:
            old = pl.read_parquet(out)
            merged = pl.concat([old, df], how="vertical_relaxed")
            merged = merged.unique(subset=["date"], keep="last").sort("date")
            merged.write_parquet(out)
            logger.info("港股日 K 合并写入: %d 行 → %s", merged.height, out)
            return out
        except Exception as e:  # noqa: BLE001
            logger.warning("港股日 K 合并失败, 全量重写: %s", e)
    df.write_parquet(out)
    logger.info("港股日 K 写入: %d 行 → %s", df.height, out)
    return out


def read_hk_daily(symbol: str) -> pl.DataFrame:
    """从落盘分区读取港股日 K (供 M2 dashboard 加速)。

    无数据 → 返回空 df, 不抛错。
    """
    code = symbol.split(".")[0]
    from app.config import settings
    path = settings.data_dir / "kline_daily" / f"symbol={code}.HK" / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股日 K 读取失败 %s: %s", code, e)
        return pl.DataFrame()


# ── U3: 港股 enriched 落盘 (供 Screener/Monitor/回测复用) ──

def sync_hk_daily_to_enriched(symbol: str, data_dir: Path | None = None) -> int:
    """把单只港股日 K 计算成 enriched 并落盘 kline_daily_enriched/date=*/part.parquet。

    前置: data/kline_daily/symbol={code}.HK/part.parquet 存在 (H6 已写)。
    复用 compute_enriched (A 股同款), 港股涨跌停字段经 H4 软门控返回 null。

    Returns:
        写入的日期分区数 (A 股同格式 date=YYYY-MM-DD)。
    """
    from app.config import settings as _settings
    from app.indicators.pipeline import compute_enriched

    root = data_dir or _settings.data_dir
    df = read_hk_daily(symbol)
    if df.is_empty():
        return 0
    try:
        enriched = compute_enriched(df, factors=None, instruments=None)
    except Exception as e:  # noqa: BLE001
        logger.warning("港股 enriched 计算失败 %s: %s", symbol, e)
        return 0
    if enriched.is_empty():
        return 0

    # 按 max(date) 写入对应分区 (与 A 股 enriched 单标的写入格式一致)
    batch_date = enriched["date"].max()
    if hasattr(batch_date, "date"):
        ds = str(batch_date.date())
    else:
        ds = str(batch_date)
    out_dir = root / "kline_daily_enriched" / f"date={ds}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "part.parquet"
    # 已有分区则合并去重
    if out.exists():
        try:
            old = pl.read_parquet(out)
            merged = pl.concat([old, enriched], how="vertical_relaxed")
            merged = merged.unique(subset=["symbol", "date"], keep="last")
            merged.write_parquet(out)
            logger.info("港股 enriched 合并写入 date=%s: %d 行 → %s", ds, merged.height, out)
            return 1
        except Exception as e:  # noqa: BLE001
            logger.warning("港股 enriched 合并失败, 全量重写: %s", e)
    enriched.write_parquet(out)
    logger.info("港股 enriched 写入 date=%s: %d 行 → %s", ds, enriched.height, out)
    return 1


def sync_all_hk_daily_to_enriched(symbols: list[str] | None = None) -> int:
    """批量把港股日 K 都算成 enriched 落盘 (供定期任务/首次初始化用)。

    symbols=None 时扫描 data/kline_daily/symbol=*.HK 全部分区。
    """
    from app.config import settings as _settings
    root = _settings.data_dir
    if symbols is None:
        import glob as _glob
        paths = _glob.glob(str(root / "kline_daily" / "symbol=*.HK" / "part.parquet"))
        symbol_list = []
        for p in paths:
            # symbol=00700.HK/part.parquet → 00700.HK
            parts = p.replace("\\", "/").split("/")
            for part in parts:
                if part.startswith("symbol=") and part.endswith(".HK"):
                    symbol_list.append(part[len("symbol="):])
                    break
        symbols = symbol_list
    written = 0
    for sym in symbols or []:
        written += sync_hk_daily_to_enriched(sym, root)
    return written
