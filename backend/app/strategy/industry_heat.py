"""题材/行业热度列注入 — 评分体系的热度因子数据源。

借鉴参考项目 (AlphaSift) 的 theme_heat 因子: 个股所属行业的当日截面热度,
定义为该行业成分的**平均涨幅**。行业热点中的个股获得正向贡献,
冷门行业反向。与 portfolio_constraints 共用带文件版本的行业映射缓存。

用法: 策略 scoring 配置加 "industry_heat": 0.1 即可启用;
该列由引擎在任何候选过滤之前,按完整输入截面和日期物化。
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import polars as pl

from app.strategy.portfolio_constraints import (
    UNKNOWN_BUCKET,
    industry_map_for_market,
)


def attach_industry_heat(
    df: pl.DataFrame,
    data_dir: Path | None,
    *,
    market: str = "cn",
    level: int = 1,
) -> pl.DataFrame:
    """给 df 注入 industry_heat 列 (= 所属行业成分当日平均 change_pct)。

    - df 需含 symbol 与 change_pct;缺列或映射时 heat 为 null,由调用方报告不可计算
    - 未知行业的行保留;同日期至少 3 个有效成分才有 heat
    - 已有 industry_heat 列时幂等直通
    """
    if (
        df is None
        or df.is_empty()
        or "industry_heat" in df.columns
    ):
        return df
    if "symbol" not in df.columns or "change_pct" not in df.columns:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("industry_heat"))
    mapping = industry_map_for_market(data_dir, market, level)
    if not mapping:
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("industry_heat"))

    industry = pl.DataFrame({
        "symbol": list(mapping.keys()), "_heat_industry": list(mapping.values()),
    })
    with_industry = df.join(industry, on="symbol", how="left", maintain_order="left")
    group_keys = [name for name in ("datetime", "date") if name in df.columns]
    group_keys.append("_heat_industry")
    valid_members = with_industry.filter(
        pl.col("_heat_industry").is_not_null()
        & (pl.col("_heat_industry") != UNKNOWN_BUCKET)
        & pl.col("change_pct").cast(pl.Float64, strict=False).is_finite()
    )
    if valid_members.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("industry_heat"))

    heat = (
        valid_members.group_by(group_keys)
        .agg(
            pl.col("change_pct").mean().alias("industry_heat"),
            pl.col("symbol").n_unique().alias("_members"),
        )
        # 成分 < 3 的微型行业热度噪声大, 置 null
        .filter(pl.col("_members") >= 3)
        .select([*group_keys, "industry_heat"])
    )
    return (
        with_industry.join(heat, on=group_keys, how="left", maintain_order="left")
        .drop("_heat_industry")
    )


def scoring_uses_industry_heat(scoring: Mapping[str, Any] | None) -> bool:
    """评分配置是否引用了 industry_heat (权重非零)。"""
    return bool(scoring) and any(
        str(name) == "industry_heat" and weight for name, weight in (scoring or {}).items()
    )
