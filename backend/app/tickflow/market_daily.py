"""HK/US daily partitions shared by downloads, indicators and chart readers."""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.data_providers.normalizer import normalize_market_symbols
from app.markets.hk import HK_TZ
from app.parquet import DAILY_STORAGE_SCHEMA, scan_daily_parquet

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.RLock()


def is_verified_hk_raw(frame: pl.DataFrame) -> bool:
    """Require an explicit version and raw identity, never upgrade old rows."""
    required = {"price_schema_version", "raw_price_verified", "price_adjustment", "volume_unit", "currency"}
    return bool(not frame.is_empty() and required.issubset(frame.columns) and frame.select(
        ((pl.col("price_schema_version") == 1) & pl.col("raw_price_verified").fill_null(False)
         & (pl.col("price_adjustment") == "unadjusted") & (pl.col("volume_unit") == "share")
         & pl.col("currency").is_in(["HKD", "CNY", "USD"])).fill_null(False).all()
    ).item())


def _as_plain_date(value: object) -> object:
    """datetime 是 date 的子类, 但哈希/相等性与 date 不互通——集合比较里
    datetime(1998,6,1) != date(1998,6,1)。legacy 分区的 date 列多为 Datetime(us),
    provider 新数据为 Date; 归一化后再做差集, 否则 missing 恒为全部旧日期,
    2792 只 legacy 港股标的被 100% 误拒 (09-15 实证)。"""
    if isinstance(value, datetime):
        return value.date()
    return value


def _legacy_gap_tolerable(
    old_dates: set, new_dates: set, missing: set, *, max_loss_ratio: float = 0.15,
) -> bool:
    """legacy 替换时容忍旧分区独有日期 (legacy 结构性不可靠, 不可能完整覆盖)。

    实证 (09-16, 全窗口 2812 只重跑, 前 31 只实测):
      - 旧分区把"无成交日"也填了行 (停牌/冷门股按前收盘补行): 00026.HK 旧分区
        5244 行中 519 天新浪不提供; 00021.HK 3567 行中 407 天 (11.4%)。
      - 另有假日行 (2009-01-01 元旦休市) 与台风停市日 (2020-10-13 浪卡 /
        2023-07-17 泰利)。
      - 36 只实测丢失比例: 中位 1.1%, 最大 11.4%, 全部 >= 0.1%。
    要求"完整覆盖"对这类 legacy 分区是数学上不可能的任务, 2794 只会全卡死。

    三重护栏, 保证丢的只能是远期不可靠行、绝不丢近期真实历史:
    1. 有界丢失: ≤15% (实测上限 11.4% + 余量)。真正抓错标的/拉取截断会远超此值;
    2. 全部远离尾端: 每个 missing 都早于旧分区最新日期前 90 天——
       数据源退化丢近期数据必然撞上这条;
    3. 新数据必须推进到旧分区最新日期, 不允许用更陈旧数据替换。
       (比较基准是 `_confirmed_old_dates`: 剔掉 volume<=0 的无成交补行与
       今日未定盘的当日行。否则 00007.HK 这类长期停牌股会被合成尾巴永久
       卡死, 而 00004.HK 这类会被"新源今日尚未发布"反复抖动。)

    放行时旧分区会被 `_repair_backup` 完整备份, 丢失可追溯可回滚。
    """
    if len(missing) > len(old_dates) * max_loss_ratio:
        return False
    newest = max(old_dates)
    stale_cutoff = newest - timedelta(days=90)
    if not all(d < stale_cutoff for d in missing):
        return False
    return max(new_dates) >= newest


def _traded_rows(frame: pl.DataFrame) -> pl.DataFrame:
    """剔除"无成交占位行" (volume<=0), 只保留真实交易日。

    legacy date 分区由更早的管线写入, 会把停牌/冷门股的"无成交日"按前收盘补行
    (OHLC 全等、volume=0)。这些行不是真实交易日, 却会被当作"旧分区最新日期",
    让 merge 闸门 (第 3 条护栏) 拒绝整个修复 —— 09-16 实证 00007.HK / 00033.HK:
    新浪真实数据止于 2024-03-28 / 2025-09-30 (长期停牌), 而旧分区有 245 行
    volume=0 的合成补行一路铺到 2026-09-03 → 两只都报 publication_failed。
    volume 为 null 的行语义未知, 保守保留 (不参与剔除)。
    """
    if frame.is_empty() or "volume" not in frame.columns:
        return frame
    return frame.filter(pl.col("volume").is_null() | (pl.col("volume") > 0))


def _confirmed_old_dates(old: pl.DataFrame) -> set:
    """旧分区中"已确认为真实、已完成交易日"的日期集合 (legacy 替换基准)。

    剔两类噪声 —— 它们都不是"必须被新数据覆盖"的真实历史:

    1. 无成交补行 (`volume<=0`): 停牌/冷门股旧管线按前收盘补的行。
    2. 今日及以后的当日行: 当日 K 线尚未定盘, 且免费源对"是否已发布今日"
       极不稳定。09-16 实证: 同一批 40 只标的, 20:37 那次新浪带当日行
       (36/40 ok), 20:49 新浪又回退掉 (18/40 failed) —— 旧分区里那条当天
       真实行会把 guard2 (近期缺口) 与 guard3 (必须推进) 同时点亮, 让标的
       在 ok/failed 之间无谓抖动。

    放行后当日行会随 `incoming` 重写; 待新源补齐当日 K 线, 下一轮自然取回。
    """
    rows = _traded_rows(old)
    today_hk = datetime.now(HK_TZ).date()
    return {day for day in (_as_plain_date(v) for v in rows["date"].to_list()) if day < today_hk}


def merge_market_daily_frames(
    old: pl.DataFrame, incoming: pl.DataFrame, symbol: str, *, replace_legacy: bool = False,
) -> pl.DataFrame:
    """Validate HK identities before merging; complete legacy repairs are explicit."""
    if symbol.endswith(".HK") and not old.is_empty():
        old_verified, new_verified = is_verified_hk_raw(old), is_verified_hk_raw(incoming)
        if old_verified and not new_verified:
            raise ValueError("不允许未知价格口径覆盖已核实港股原始日线")
        if new_verified and not old_verified:
            # 只对旧分区收敛基准 (剔补行 + 剔当日): incoming 必须原样参与, 因为
            # 决定"是否被覆盖"的是新源实际给了哪些日期。若连 incoming 也剔,
            # 旧分区里同日真实行会变成"缺失", 反而误拒 (09-16 实测: 两侧同剔 →
            # 36/40 掉到 18/40)。
            old_dates = _confirmed_old_dates(old)
            new_dates = {_as_plain_date(d) for d in incoming["date"].to_list()}
            missing = old_dates - new_dates
            if not replace_legacy:
                raise ValueError("旧港股日线口径未知, 必须取得完整维护窗口并备份后替换")
            if missing and not _legacy_gap_tolerable(old_dates, new_dates, missing):
                detail = ", ".join(str(d) for d in sorted(missing)[:8])
                raise ValueError(
                    f"旧港股日线口径未知, 必须取得完整维护窗口并备份后替换 (缺失 {len(missing)} 天: {detail})"
                )
            if missing:
                sample = ", ".join(str(d) for d in sorted(missing)[:5])
                logger.warning(
                    "hk %s: 替换 legacy 分区丢弃 %d/%d (%.1f%%) 旧分区独有日期 "
                    "(远端不可靠行, 旧分区已备份), 样例: %s …",
                    symbol, len(missing), len(old_dates),
                    100.0 * len(missing) / max(len(old_dates), 1), sample,
                )
            return incoming.unique(["symbol", "date"], keep="last").sort("date")
        if new_verified and set(old["currency"].to_list()) != set(incoming["currency"].to_list()):
            raise ValueError("港股日线币种发生冲突, 不能合并")
    merged = pl.concat([old, incoming], how="diagonal_relaxed") if not old.is_empty() else incoming
    return merged.unique(["symbol", "date"], keep="last").sort("date")


def market_symbol_key(symbol: str) -> str:
    """Return a validated partition key without losing class-share suffixes."""
    text = str(symbol).strip().upper()
    market = "US" if text.endswith(".US") else "HK"
    return normalize_market_symbols([text], market)[0]


def read_legacy_market_daily(data_dir: Path, market: str, symbols: list[str] | None = None) -> pl.DataFrame:
    """Read old date partitions once; new market writers use symbol partitions."""
    files = sorted((data_dir / "kline_daily").glob("date=*/*.parquet"))
    if not files:
        return pl.DataFrame()
    metadata_schema = {name: pl.String for name in ("source", "currency", "volume_unit", "amount_source", "price_adjustment", "adjustment_source", "adjustment_version", "observed_at")}
    metadata_schema.update(price_schema_version=pl.Int64, raw_price_verified=pl.Boolean, adjustment_as_of=pl.Date)
    lazy = scan_daily_parquet([str(path) for path in files], schema={**DAILY_STORAGE_SCHEMA, **metadata_schema},
                             cast_options=pl.ScanCastOptions(integer_cast=["upcast", "allow-float"]))
    predicate = pl.col("symbol").str.ends_with(f".{market.upper()}") | pl.col("symbol").is_null()
    if symbols is not None:
        predicate = predicate & pl.col("symbol").is_in(symbols)
    frame = lazy.filter(predicate).collect()
    if frame.get_column("symbol").null_count() or frame.get_column("date").null_count():
        raise ValueError("旧日 K 分区包含空证券代码或交易日期")
    return frame


def list_market_daily_symbols(data_dir: Path, market: str) -> list[str]:
    """List only one market, including historical date-partition downloads."""
    market = market.upper()
    if market not in {"HK", "US"}:
        raise ValueError("市场只支持 HK 或 US")
    symbols = {
        path.parent.name.removeprefix("symbol=")
        for path in (data_dir / "kline_daily").glob(f"symbol=*.{market}/*.parquet")
    }
    legacy = read_legacy_market_daily(data_dir, market)
    if not legacy.is_empty():
        symbols.update(legacy.get_column("symbol").to_list())
    return normalize_market_symbols(sorted(symbols), market)


def read_market_daily_symbol(
    data_dir: Path, symbol: str, *, legacy: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Merge both layouts without changing files; symbol rows win duplicate dates."""
    key = market_symbol_key(symbol)
    old = legacy if legacy is not None else read_legacy_market_daily(data_dir, key[-2:], [key])
    frames: list[pl.DataFrame] = []
    if not old.is_empty():
        old = old.filter(pl.col("symbol") == key)
        if not old.is_empty():
            frames.append(old)
    for path in sorted((data_dir / "kline_daily" / f"symbol={key}").glob("*.parquet")):
        frame = pl.read_parquet(path)
        if "symbol" not in frame.columns:
            raise ValueError(f"日 K 分区缺少 symbol: {key}")
        if frame.filter((pl.col("symbol") != key).fill_null(True)).height:
            raise ValueError(f"日 K 分区包含其他标的: {key}")
        if "date" not in frame.columns or frame.get_column("date").null_count():
            raise ValueError(f"日 K 分区包含无效交易日期: {key}")
        frames.append(frame)
    if not frames:
        return pl.DataFrame()
    frames = [frame.with_columns(pl.col("date").cast(pl.Date)) for frame in frames]
    return (
        pl.concat(frames, how="diagonal_relaxed")
        .unique(subset=["symbol", "date"], keep="last", maintain_order=True)
        .sort("date")
    )


HK_US_ENRICHED_DIR = "kline_hk_us_enriched"

# Directory-wide legacy schema baseline, cached per root for the process lifetime.
# A deliberate full migration (rebuild --target new) runs in its own process and
# leaves no legacy partitions behind, so a stale cache can only persist in a
# long-lived service started before the migration; the per-symbol branch below
# still keeps each existing file on its own schema in that window.
_ENRICHED_LEGACY_CACHE: dict[str, list[str] | None] = {}


def legacy_enriched_columns(root: Path) -> list[str] | None:
    """Return the legacy 64-column baseline of the HK/US enriched directory.

    Legacy partitions carry no ``price_schema_version`` identity column AND keep
    ``date`` as Datetime('us'); None means the directory has no legacy partitions
    (empty, fully migrated, or non-legacy schemas like sparse test fixtures).
    """
    cache_key = str(root)
    if cache_key in _ENRICHED_LEGACY_CACHE:
        return _ENRICHED_LEGACY_CACHE[cache_key]
    cols: list[str] | None = None
    for directory in sorted((root / HK_US_ENRICHED_DIR).glob("symbol=*")):
        part = directory / "part.parquet"
        if not part.exists():
            continue
        names = _legacy_schema_names(part)
        if names is not None:
            cols = names
            break
    _ENRICHED_LEGACY_CACHE[cache_key] = cols
    return cols


def _legacy_schema_names(part: Path) -> list[str] | None:
    """Legacy detection: Datetime('us') date + no price identity column.

    A Date-typed partition without identity columns (sparse fixtures) is neither
    legacy nor current — it must not be treated as a downgrade baseline.
    """
    try:
        schema = pl.scan_parquet(part).collect_schema()
    except Exception:
        return None
    if schema.get("date") == pl.Datetime("us") and "price_schema_version" not in schema.names():
        return schema.names()
    return None


def downgrade_enriched(frame: pl.DataFrame, legacy_cols: list[str]) -> pl.DataFrame:
    missing = [name for name in legacy_cols if name not in frame.columns]
    if missing:
        raise ValueError(f"旧 enriched schema 列缺失于新产出: {missing[:5]}")
    return frame.select(legacy_cols).with_columns(pl.col("date").cast(pl.Datetime("us")))


def adapt_enriched_for_write(frame: pl.DataFrame, root: Path, symbol: str) -> pl.DataFrame:
    """Match the enriched directory's schema before an incremental per-symbol write.

    ``scan_parquet`` requires one schema across the whole directory, so an
    incremental write must never introduce a second schema. An existing legacy
    target partition pins that symbol to the legacy schema; anything else
    (current schema, non-legacy partitions, or no file) keeps the current
    output — except when the directory still holds legacy partitions and the
    symbol has no readable partition yet.
    """
    if frame.is_empty():
        return frame
    target = root / HK_US_ENRICHED_DIR / f"symbol={market_symbol_key(symbol)}" / "part.parquet"
    if target.exists():
        legacy_names = _legacy_schema_names(target)
        if legacy_names is not None:
            return downgrade_enriched(frame, legacy_names)
        return frame
    legacy_cols = legacy_enriched_columns(root)
    if legacy_cols is None:
        return frame
    return downgrade_enriched(frame, legacy_cols)


def market_enriched_as_of(data_dir: Path, market: str) -> date | None:
    """该市场 enriched 分区的最新交易日 (全市场 max), 无数据/读失败返回 None。

    legacy 分区的 ``date`` 是 Datetime('us'), 统一 cast 成 Date 后取 max, 与
    ``hk_us_overview_builder`` 的 as_of 同口径。**不可用不计入**: 读不到就返回
    None 让调用方 fail-closed, 不拿别的日期冒充"该市场最新收盘日"。

    用途: 港美异动监控的 staleness 门控 (日线收盘口径, 见
    ``quote_service.evaluate_hk_us_monitors``)。
    """
    root = Path(data_dir) / HK_US_ENRICHED_DIR
    if not root.exists():
        return None
    suffix = f".{str(market).strip().upper()}"
    try:
        frame = (
            pl.scan_parquet(
                str(root / "symbol=*" / "part.parquet"),
                # 跨分区 schema 可能由不同源写入 (volume Int64/Float64), 同
                # overview 侧一样允许整型向浮点兼容提升
                cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
            )
            .filter(pl.col("symbol").cast(pl.String).str.ends_with(suffix))
            .select(pl.col("date").cast(pl.Date, strict=False).max().alias("latest"))
            .collect()
        )
    except Exception as exc:
        logger.warning("enriched as_of 扫描失败 (%s, %s): %s", root, market, exc)
        return None
    if frame.is_empty():
        return None
    value = frame["latest"][0]
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def write_market_daily_symbol(data_dir: Path, symbol: str, frame: pl.DataFrame) -> Path | None:
    """Merge and atomically publish one symbol. A damaged old file is never overwritten."""
    if frame.is_empty():
        return None
    key = market_symbol_key(symbol)
    if "symbol" not in frame.columns or frame.filter((pl.col("symbol") != key).fill_null(True)).height:
        raise ValueError(f"下载结果与请求标的不一致: {key}")
    if "date" not in frame.columns:
        raise ValueError("下载结果缺少 date")
    incoming = frame.with_columns(pl.col("date").cast(pl.Date))
    if incoming.get_column("date").null_count():
        raise ValueError("下载结果含无效交易日期")
    path = data_dir / "kline_daily" / f"symbol={key}" / "part.parquet"
    with _WRITE_LOCK:
        # Legacy date rows are read by the common reader. Only merge this symbol's
        # partition here, so downloading a batch never scans CN history per ticker.
        old = read_market_daily_symbol(data_dir, key, legacy=pl.DataFrame())
        merged = merge_market_daily_frames(old, incoming, key)
        if not old.is_empty() and old.equals(merged, null_equal=True):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            merged.write_parquet(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path
