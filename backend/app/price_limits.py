"""A-share price-limit rules shared by indicators, backtests, and APIs.

[M0 已迁移] 标量判定 (board_limit_pct / price_limit_pct) 的单一事实源
在 app/markets/cn.py; 本模块保留同名函数作兼容转发, 既有 import 不变。

polars_* / numpy_* 向量化系列仍为本文件实现 — A 股专用,
M1 随市场功能门控一起改造 (港股无涨跌停、美股无涨跌停)。
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import polars as pl

from app.markets.cn import (
    BEIJING_BOARD_LIMIT,
    GROWTH_BOARD_LIMIT,
    LEGACY_MAIN_BOARD_ST_LIMIT,
    MAIN_BOARD_LIMIT,
    MAIN_BOARD_ST_LIMIT_CHANGE_DATE,
    CN_PROFILE,
)


def is_risk_warning_name(name: str | None) -> bool:
    return "ST" in str(name or "").upper()


def board_limit_pct(symbol: str) -> float:
    """板块基础涨跌幅限制。 (转发 CN_PROFILE)"""
    return CN_PROFILE.board_limit_pct(symbol)


def price_limit_pct(
    symbol: str,
    trade_date: date,
    *,
    is_risk_warning: bool = False,
) -> float:
    """个股某交易日的有效涨跌幅限制。 (转发 CN_PROFILE)"""
    return CN_PROFILE.limit_pct(symbol, trade_date, is_risk_warning=is_risk_warning)


def polars_price_limit_pct(
    symbol: pl.Expr,
    trade_date: pl.Expr,
    is_risk_warning: pl.Expr,
) -> pl.Expr:
    """Return a vectorized Polars expression for the effective daily limit.

    H4 软门控: 非 CN 市场 (symbol 带 .HK/.US 后缀, 或不带后缀但非 6 位数字) → null。
    A 股: 6 位数字代码 (含无后缀 / .SH / .SZ / .BJ) → 按板块规则。
    """
    # CN 判定: 有 .SH/.SZ/.BJ 后缀 → CN; .HK/.US 后缀 → 非 CN;
    # 无后缀: 6 位纯数字 (裸代码) → CN; 其他 → 非 CN
    has_cn_suffix = (
        symbol.str.ends_with(".SH")
        | symbol.str.ends_with(".SZ")
        | symbol.str.ends_with(".BJ")
    )
    has_foreign_suffix = symbol.str.ends_with(".HK") | symbol.str.ends_with(".US")
    is_bare_cn_code = (
        ~has_cn_suffix
        & ~has_foreign_suffix
        & symbol.str.contains(r"^\d+$", literal=False)
        & (symbol.str.len_chars().cast(pl.Int64) >= 6)
    )
    is_cn = has_cn_suffix | is_bare_cn_code
    is_growth = symbol.str.starts_with("300") | symbol.str.starts_with("301")
    is_star = symbol.str.starts_with("688") | symbol.str.starts_with("689")
    is_beijing = symbol.str.ends_with(".BJ")
    is_non_main = is_growth | is_star | is_beijing
    base = (
        pl.when(is_growth | is_star).then(GROWTH_BOARD_LIMIT)
        .when(is_beijing).then(BEIJING_BOARD_LIMIT)
        .otherwise(MAIN_BOARD_LIMIT)
    )
    legacy_main_st = (
        is_risk_warning.fill_null(False)
        & ~is_non_main
        & (trade_date < pl.lit(MAIN_BOARD_ST_LIMIT_CHANGE_DATE))
    )
    return (
        pl.when(~is_cn).then(pl.lit(None))
        .when(legacy_main_st).then(LEGACY_MAIN_BOARD_ST_LIMIT)
        .otherwise(base)
        .cast(pl.Float64)
    )


def polars_is_risk_warning_name(name: pl.Expr) -> pl.Expr:
    """Return whether an instrument name contains the ST risk-warning marker."""
    return name.fill_null("").str.to_uppercase().str.contains("ST", literal=True)


def polars_limit_price(previous: pl.Expr, limit_pct: pl.Expr, *, up: bool) -> pl.Expr:
    """Calculate exchange half-up prices with integer-cent arithmetic."""
    sign = 1 if up else -1
    numerator = ((1 + sign * limit_pct) * 100).round(0).cast(pl.Int64)
    cents = (previous * 100 + 0.5).floor().cast(pl.Int64)
    return ((cents * numerator + 50) // 100) / 100


def numpy_limit_pct_vectors(
    symbols: Sequence[str],
    names: Sequence[str],
) -> tuple[np.ndarray, np.ndarray]:
    """Return pre/post-change vectors once; callers select one per date.

    M1 H4 软门控: 对 has_price_limit()=False 的市场 (HK/US) 标的返回 NaN,
    业务层 (回测矩阵、打板信号) 看到 NaN 自动跳过 — 不需要每个调用点单独过滤。
    """
    from app.markets.registry import resolve_market  # 局部 import 避免循环

    current = np.fromiter(
        (board_limit_pct(str(symbol)) for symbol in symbols),
        dtype=np.float64,
        count=len(symbols),
    )
    legacy = current.copy()
    for asset_id, (_symbol, name) in enumerate(zip(symbols, names, strict=True)):
        # H4 软门控: 非 A 股 → NaN (无涨跌停)
        if resolve_market(str(_symbol)) != "CN":
            current[asset_id] = np.nan
            legacy[asset_id] = np.nan
            continue
        if current[asset_id] == MAIN_BOARD_LIMIT and is_risk_warning_name(name):
            legacy[asset_id] = LEGACY_MAIN_BOARD_ST_LIMIT
    return legacy, current


def numpy_price_limit_matrix(
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
) -> np.ndarray:
    """Build a float32 time-by-asset matrix only for strategies that request it."""
    result = np.empty((len(trading_dates), len(symbols)), dtype=np.float32)
    return write_numpy_price_limit_matrix(result, trading_dates, symbols, names)


def write_numpy_price_limit_matrix(
    target: np.ndarray,
    trading_dates: Sequence[date],
    symbols: Sequence[str],
    names: Sequence[str],
    *,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """Write date-aware limits directly into an existing matrix or memmap."""
    expected_shape = (len(trading_dates), len(symbols))
    if target.shape != expected_shape:
        raise ValueError("price-limit output shape mismatch")
    if valid is not None and valid.shape != expected_shape:
        raise ValueError("price-limit validity mask shape mismatch")

    legacy, current = numpy_limit_pct_vectors(symbols, names)
    target[:] = current.astype(np.float32, copy=False)
    legacy_rows = np.fromiter(
        (value < MAIN_BOARD_ST_LIMIT_CHANGE_DATE for value in trading_dates),
        dtype=bool,
        count=len(trading_dates),
    )
    if legacy_rows.any():
        target[legacy_rows] = legacy.astype(np.float32, copy=False)
    if valid is not None:
        target[~valid] = np.nan
    return target


def numpy_limit_price(
    previous: np.ndarray,
    limit_pct: np.ndarray,
    *,
    up: bool,
) -> np.ndarray:
    """NumPy counterpart of :func:`polars_limit_price`."""
    sign = 1 if up else -1
    numerator = np.rint((1.0 + sign * limit_pct) * 100.0).astype(np.int64)
    result = np.full(previous.shape, np.nan, dtype=np.float64)
    finite = np.isfinite(previous)
    cents = np.floor(previous[finite] * 100.0 + 0.5).astype(np.int64)
    result[finite] = (
        ((cents * numerator[finite] + 50) // 100).astype(np.float64) / 100.0
    )
    return result
