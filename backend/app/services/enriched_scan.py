"""港美 enriched 分区的「per-symbol 最新交易日」扫描。

为什么单独放一个模块:
  这段逻辑原先只有 ``scripts/repair_hk_stale.scan_latest_dates`` 一份实现,
  而 ``backend/scripts`` 整个目录被 .dockerignore 排除 (镜像内 /app/scripts
  不存在), 于是容器内 ``from scripts.repair_hk_stale import ...`` 必然 ImportError
  → daily_pipeline 每次启动都打印 "enriched 扫描不可用" 并退化成**只看 H6 单侧**
  判定新鲜度。单侧判定的后果是补跑判据失真 (partial_sync / bulk_lag 只算一半
  分布), 是"每次重启都触发一轮同步"的诱因之一。

  修法是把实现挪进 app/ (随镜像发布), 脚本改为反向依赖 app —— 单一实现源,
  两侧都不会再漂。依赖只有 polars + 标准库, 不牵扯 provider / 网络, 放进
  app/services 不会把维护脚本的重量带进镜像。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

_MARKET_SUFFIX = {"HK": ".HK", "US": ".US"}


def _market_suffix(market: str) -> str:
    """市场代码 → 标的后缀。不支持的市场直接 ValueError, 不静默返回空。"""
    key = str(market or "").strip().upper()
    if key not in _MARKET_SUFFIX:
        raise ValueError(f"不支持的市场: {market} (可用: {sorted(_MARKET_SUFFIX)})")
    return _MARKET_SUFFIX[key]


def scan_latest_dates(data_dir: Path, market: str = "HK") -> pl.DataFrame:
    """扫 enriched 分区, 返回每只标的的最新交易日 (symbol, latest_date)。

    目录不存在 / 读失败 / 无该市场分区时返回空表 —— **不可用不计入**, 不拿
    空结果冒充"没有 stale"。legacy 分区的 date 是 Datetime('us'), 统一 cast
    成 Date 后再取 max, 与 overview 的 as_of 口径一致。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "latest_date": pl.Date})
    root = Path(data_dir) / "kline_hk_us_enriched"
    if not root.exists():
        return empty
    suffix = _market_suffix(market)
    try:
        lazy = pl.scan_parquet(
            str(root / "symbol=*" / "part.parquet"),
            # 历史分区由不同源写入 (新浪 volume=Float64 / 兜底源 Int64), 跨分区
            # scan 需允许整型向浮点兼容提升, 否则 SchemaError (同 overview 侧)
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        )
        timeline = lazy.select("symbol", "date").collect()
    except Exception as exc:  # 目录损坏/混 schema 时不炸整个调用方
        logger.warning("enriched 扫描失败 (%s): %s", root, exc)
        return empty
    if timeline.is_empty():
        return empty
    dated = (
        timeline.filter(pl.col("symbol").cast(pl.String).str.ends_with(suffix))
        .with_columns(pl.col("date").cast(pl.Date, strict=False))
        .drop_nulls("date")
    )
    if dated.is_empty():
        return empty
    return (
        dated.group_by("symbol")
        .agg(pl.col("date").max().alias("latest_date"))
        .sort("symbol")
    )
