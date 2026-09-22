"""强度梯队(Strength Ladder) — 港美"动量档位"梯队。

港美无涨跌停/连板制度, A 股连板梯队不适用。沿用参考项目动量档位替代:
- m25: 20 日动量 >= 25%
- m15: 20 日动量 >= 15% (且 < 25%)
- m8 : 20 日动量 >= 8%  (且 < 15%)
- m3 : 20 日动量 >= 3%  (且 < 8%)
- 其余不计入梯队

数据源:
- cn: 不计算 (A 股用连板梯队, 由 market_phase / monitor / depth_service 协同)
- hk/us: 扫 kline_hk_us_enriched/symbol=*.{HK,US} 某日全市场行,
  按 momentum_20d 落档。

持久化:
- cn: 不写(本批次不覆盖 A 股连板梯队)
- hk/us: data/strength_ladder/{hk,us}/part.parquet, 字段:
    date, market, band, symbol, name, momentum_20d, last_close, amount
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# 动量档位阈值(从高到低); band 名 = m{阈值百分比}.
# 落档规则: 找到首个 m_b 满足 momentum >= b, 否则不入档。
_TIER_BANDS: tuple[tuple[str, float], ...] = (
    ("m25", 0.25),
    ("m15", 0.15),
    ("m8", 0.08),
    ("m3", 0.03),
)

STRENGTH_DIR = "strength_ladder"


def strength_path(data_dir: Path, market: str) -> Path:
    """强度梯队持久化路径(港美专用)。"""
    m = market.lower()
    if m not in ("hk", "us"):
        # cn 不在本批次服务范围; 调用方应自行 gate
        raise ValueError(f"strength_ladder 不支持 market={market!r}(本批次仅 hk/us)")
    return data_dir / STRENGTH_DIR / m / "part.parquet"


def _date_col(df):
    """返回 polars date 列表达式(单列 helper, 供 API filter 用)。"""
    return pl.col("date")


def _band_for(momentum: float) -> str | None:
    """根据 20 日动量返回档位名称; 不入档返回 None。"""
    if momentum is None:
        return None
    for name, th in _TIER_BANDS:
        if momentum >= th:
            return name
    return None


def compute_strength_ladder_for_day(
    data_dir: Path,
    target_date: date,
    market: str,
) -> pl.DataFrame:
    """计算某日港美的"动量档位"梯队。

    输入: data_dir/kline_hk_us_enriched/symbol=*.{HK,US}/part.parquet
          (per-symbol 全历史单文件, 含 momentum_20d 列)
    输出: {date, market, band, symbol, momentum_20d, last_close, amount}
          仅含 m25/m15/m8/m3 四档, 其余过滤掉。

    排序: 按 (band 优先级, momentum_20d 降序); 各 band 内部按动量降序。
    """
    m = market.lower()
    if m not in ("hk", "us"):
        raise ValueError(f"strength_ladder 不支持 market={market!r}")
    suffix = f".{m.upper()}"
    enriched_dir = data_dir / "kline_hk_us_enriched"
    if not enriched_dir.exists():
        logger.info("strength_ladder: enriched dir 不存在 %s", enriched_dir)
        return pl.DataFrame()

    try:
        df = pl.scan_parquet(
            str(enriched_dir / "symbol=*" / "part.parquet"),
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        ).filter(
            (pl.col("symbol").str.ends_with(suffix))
            & (pl.col("date") == target_date)
        ).collect()
    except Exception as e:
        logger.warning("strength_ladder scan failed: %s", e)
        return pl.DataFrame()
    if df.is_empty():
        return pl.DataFrame()
    if "momentum_20d" not in df.columns:
        logger.warning("strength_ladder: enriched 缺 momentum_20d 列, 跳过")
        return pl.DataFrame()

    # 必要列存在性: amount / close / name(由 instruments join, 此函数不强制; 上层选配)
    keep_cols = ["symbol", "momentum_20d"]
    if "amount" in df.columns:
        keep_cols.append("amount")
    if "close" in df.columns:
        keep_cols.append("close")
    df = df.select([c for c in keep_cols if c in df.columns])

    # 计算 band: 一次性 case-style when 链(高→低档, 命中即停)。
    # 不可循环构建 (each .when() 替换上一个, 否则最终只剩最后一档)。
    band_chain = (
        pl.when(pl.col("momentum_20d") >= _TIER_BANDS[0][1]).then(pl.lit(_TIER_BANDS[0][0], dtype=pl.Utf8))
        .when(pl.col("momentum_20d") >= _TIER_BANDS[1][1]).then(pl.lit(_TIER_BANDS[1][0], dtype=pl.Utf8))
        .when(pl.col("momentum_20d") >= _TIER_BANDS[2][1]).then(pl.lit(_TIER_BANDS[2][0], dtype=pl.Utf8))
        .when(pl.col("momentum_20d") >= _TIER_BANDS[3][1]).then(pl.lit(_TIER_BANDS[3][0], dtype=pl.Utf8))
        .otherwise(None)
        .alias("band")
    )
    df = df.with_columns(band_chain).filter(pl.col("band").is_not_null())

    if df.is_empty():
        return pl.DataFrame()

    # band 排序优先级
    band_priority = {n: i for i, (n, _) in enumerate(_TIER_BANDS)}
    df = df.with_columns(
        pl.col("band").replace_strict(band_priority).alias("_band_pri"),
    ).sort(["_band_pri", "momentum_20d"], descending=[False, True]).drop("_band_pri")

    # 附加 date + market
    df = df.with_columns([
        pl.lit(target_date).alias("date"),
        pl.lit(m).alias("market"),
    ])
    # 列顺序
    out_cols = ["date", "market", "band", "symbol", "momentum_20d"]
    if "close" in df.columns:
        out_cols.append("last_close")
        df = df.rename({"close": "last_close"})
    if "amount" in df.columns:
        out_cols.append("amount")
    return df.select(out_cols)


def load_strength_ladder_history(
    data_dir: Path,
    market: str,
    target_date: date | None = None,
) -> pl.DataFrame:
    """读取持久化后的强度梯队历史(港美); 不存在或非目标日返回空 DataFrame。"""
    m = market.lower()
    if m not in ("hk", "us"):
        return pl.DataFrame()
    p = strength_path(data_dir, m)
    if not p.exists():
        return pl.DataFrame()
    try:
        df = pl.read_parquet(p)
    except Exception as e:
        logger.warning("load_strength_ladder_history failed: %s", e)
        return pl.DataFrame()
    if target_date is not None and "date" in df.columns:
        df = df.filter(pl.col("date") == target_date)
    return df


def upsert_strength_ladder(
    data_dir: Path,
    new_rows: pl.DataFrame,
    market: str,
) -> None:
    """按 (date, symbol) 覆盖(upsert) 强度梯队。"""
    if new_rows.is_empty() or "date" not in new_rows.columns or "symbol" not in new_rows.columns:
        return
    m = market.lower()
    if m not in ("hk", "us"):
        raise ValueError(f"strength_ladder 不支持 market={market!r}")
    p = strength_path(data_dir, m)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = load_strength_ladder_history(data_dir, m)
    if old.is_empty():
        combined = new_rows
    else:
        # anti-join by (date, symbol): 删除老行中与 new_rows 主键冲突的行
        new_keys = new_rows.select(["date", "symbol"]).unique()
        kept = old.join(new_keys, on=["date", "symbol"], how="anti")
        # schema 对齐: 以 new_rows 列名+顺序为权威
        target_cols = new_rows.columns
        keep_exprs = [pl.col(c) if c in kept.columns else pl.lit(None).alias(c) for c in target_cols]
        kept = kept.select(keep_exprs)
        new_rows = new_rows.select(target_cols)
        combined = pl.concat([kept, new_rows], how="vertical_relaxed")
    combined = combined.sort(["date", "band", "momentum_20d"], descending=[False, False, True])
    combined.write_parquet(p)


def group_ladder_by_band(df: pl.DataFrame) -> dict[str, list[dict]]:
    """将梯队 DataFrame 转为 {band: [stock dict, ...]} 字典(供 API JSON 化)。

    每个 stock dict: {symbol, momentum_20d, last_close?, amount?}
    各 band 内部按 momentum_20d 降序。
    """
    if df.is_empty():
        return {}
    out: dict[str, list[dict]] = {}
    for band_name, _ in _TIER_BANDS:
        sub = df.filter(pl.col("band") == band_name).sort("momentum_20d", descending=True)
        if sub.is_empty():
            continue
        stocks: list[dict] = []
        for r in sub.iter_rows(named=True):
            stocks.append({
                "symbol": r["symbol"],
                "momentum_20d": round(float(r.get("momentum_20d") or 0), 4),
                **({"last_close": r.get("last_close")} if "last_close" in r else {}),
                **({"amount": r.get("amount")} if "amount" in r else {}),
            })
        out[band_name] = stocks
    return out


# ───────────────────────── 增量补算(调度用) ─────────────────────────

def compute_strength_ladder_incremental(
    repo,
    data_dir: Path,
    *,
    today: date | None = None,
    market: str = "hk",
    max_backfill_days: int = 30,
) -> int:
    """增量补算强度梯队(供 daily_pipeline 调度调用)。

    补算逻辑: 扫描该市场 enriched 已有的交易日, 减去 ladder 里已落盘的日期,
    剩下的就是缺口; 逐日调 compute_strength_ladder_for_day + upsert。

    与 regime 的增量不同: regime 是一次批算多天(向量化聚合), 梯队是逐日扫描
    全市场 symbol 文件, 单日成本 O(标的数)。因此这里限制回溯天数, 避免首次
    运行要补几百天 * 数千只标的(那会拖垮日管道)。

    - max_backfill_days: 最多回溯多少天(默认 30)。取 enriched 日期集合里
      最近的 N 个交易日来补。设为 0 或负数表示不限(慎用)。
    - 返回本次新写入的行数; 无缺口/市场不支持返回 0。
    - 单日失败不影响其他日(软失败, 仅记日志)。
    """
    # 市场时钟·B类: 同 regime_builder —— date.today() 只是 `today` 参数的缺省
    # 兜底 (管道侧显式传市场感知的 today); 本函数只服务 hk/us, 改成市场日期
    # 会把"补到哪天"与调用方传入的上界解耦, 产生重复补算或漏补。
    today = today or date.today()
    m = market.lower()
    if m not in ("hk", "us"):
        return 0

    # 已有日期
    existing = load_strength_ladder_history(data_dir, m)
    if existing.is_empty() or "date" not in existing.columns:
        existing_dates: set = set()
    else:
        existing_dates = set(existing["date"].to_list())

    # enriched 有哪些天(复用 regime 的日期扫描, 港美走 per-symbol 全目录)
    from app.services.regime_builder import _as_date, enriched_date_set

    enriched = enriched_date_set(repo, market=m)
    # 二次归一化: 防止上游返回 Datetime 与 date.today() 比较时抛 TypeError
    # (datetime 是 date 子类, isinstance 判断会漏; 详见 regime_builder._as_date)
    norm_dates = {_as_date(d) for d in enriched}
    norm_existing = {_as_date(d) for d in existing_dates} if existing_dates else set()
    candidates = sorted(d for d in norm_dates if d not in norm_existing and d <= today)
    if not candidates:
        logger.debug("strength_ladder incremental[%s]: nothing to compute", m)
        return 0
    if max_backfill_days > 0 and len(candidates) > max_backfill_days:
        skipped_days = len(candidates) - max_backfill_days
        candidates = candidates[-max_backfill_days:]
        logger.info(
            "strength_ladder incremental[%s]: %d 天缺口, 只补最近 %d 天(跳过 %d 天历史)",
            m, len(candidates) + skipped_days, len(candidates), skipped_days,
        )

    total = 0
    for d in candidates:
        try:
            rows = compute_strength_ladder_for_day(data_dir, d, market=m)
        except Exception as e:  # 单日失败不阻断
            logger.warning("strength_ladder[%s] %s failed (soft): %s", m, d, e)
            continue
        if rows.is_empty():
            continue
        try:
            upsert_strength_ladder(data_dir, rows, market=m)
            total += rows.height
        except Exception as e:  # upsert 失败不阻断
            logger.warning("strength_ladder[%s] %s upsert failed (soft): %s", m, d, e)
    if total:
        logger.info("strength_ladder incremental[%s]: wrote %d rows", m, total)
    return total