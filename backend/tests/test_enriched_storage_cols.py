"""enriched 存储列完整性回归测试。

背景 (2026-09-18): A 股 enriched 分区从 09-07 起静默退化为 12 列
(缺 turnover_rate / consecutive_limit_ups / consecutive_limit_downs),
根因是 run_pipeline 用 glob 扫描 data/instruments/**/*.parquet,
把 A 股 (region=String) 与港股 (region=Null) / 美股 (无 region) 维表一起扫,
polars 跨文件 schema 冲突导致整体失败 → 维表为空 → compute_limit_signals 被跳过 →
_select_storage_cols 静默裁剪成窄表。下游主线/热点直接 ColumnNotFoundError。

本文件锁定两道防线:
  1. _load_instruments 不受跨市场 schema 漂移影响
  2. _select_storage_cols 缺列必须 fail-loud, 绝不再静默裁剪
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.indicators import pipeline


def _cn_instruments() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["000001", "600000"],
            "market": ["cn", "cn"],
            "region": ["CN", "CN"],            # String
            "name": ["平安银行", "浦发银行"],
            "float_shares": [1.9e10, 2.9e10],  # Float64
            "limit_up": [12.1, 9.9],
            "limit_down": [9.9, 8.1],
            "as_of": [date(2026, 9, 17), date(2026, 9, 17)],
        }
    )


def _hk_instruments() -> pl.DataFrame:
    """港股维表: 与 A 股 schema 冲突 (region=Null, float_shares=Null, 列数不同)。"""
    return pl.DataFrame(
        {
            "symbol": ["00700.HK"],
            "market": ["hk"],
            "region": [None],        # Null ← 与 A 股 String 冲突
            "name": ["腾讯控股"],
            "float_shares": [None],
            "limit_up": [None],
            "limit_down": [None],
            "as_of": [None],
            "board_lot": [100],      # 额外列
        }
    )


def _us_instruments() -> pl.DataFrame:
    """美股维表: 缺 region / float_shares 等列。"""
    return pl.DataFrame(
        {
            "symbol": ["AAPL"],
            "market": ["us"],
            "name": ["Apple Inc."],
        }
    )


# ── 防线 1: 维表加载不受跨市场 schema 漂移影响 ──────────────────


def test_load_instruments_survives_foreign_schema_drift(tmp_path):
    """目录下同时存在 hk/us 维表时, 仍能拿到完整的 A 股维表。"""
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir()
    _cn_instruments().write_parquet(inst_dir / "instruments.parquet")
    _hk_instruments().write_parquet(inst_dir / "hk_instruments.parquet")
    _us_instruments().write_parquet(inst_dir / "us_instruments.parquet")

    df = pipeline._load_instruments(tmp_path)

    assert df.height == 2  # 只有 A 股两行, 港美不混入
    for c in ("symbol", "name", "float_shares", "limit_up", "limit_down"):
        assert c in df.columns
    assert df["float_shares"].dtype.is_float()


def test_glob_scan_would_have_failed(tmp_path):
    """记录旧实现为何失效: 直接 glob 扫三份维表会抛 schema 冲突。"""
    from app.parquet import scan_parquet_compat

    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir()
    _cn_instruments().write_parquet(inst_dir / "instruments.parquet")
    _hk_instruments().write_parquet(inst_dir / "hk_instruments.parquet")

    with pytest.raises(Exception):  # noqa: B017 - 断言"旧路径会炸", 具体异常类型由 polars 决定
        scan_parquet_compat(str(inst_dir / "**" / "*.parquet")).collect()


def test_load_instruments_falls_back_when_cn_missing(tmp_path):
    """A 股维表缺失时回退逐文件扫描, 不因单文件损坏而全盘失败。"""
    inst_dir = tmp_path / "instruments"
    inst_dir.mkdir()
    _hk_instruments().write_parquet(inst_dir / "hk_instruments.parquet")
    (inst_dir / "broken.parquet").write_bytes(b"not-a-parquet")

    df = pipeline._load_instruments(tmp_path)
    assert df.height == 0  # 回退结果按 market 过滤掉 hk, 坏文件被跳过


# ── 防线 2: 存储列裁剪必须 fail-loud ────────────────────────────


def _full_enriched_row() -> dict:
    return {
        "symbol": ["000001"],
        "date": [date(2026, 9, 17)],
        "open": [11.0], "high": [12.1], "low": [10.9], "close": [12.1],
        "volume": [123456.0], "amount": [1.5e9],
        "raw_close": [12.1], "raw_high": [12.1], "raw_low": [10.9],
        "turnover_rate": [6.5],
        "consecutive_limit_ups": [1], "consecutive_limit_downs": [0],
        "quote_ts": [1758000000000],
    }


def test_select_storage_cols_keeps_all_15_columns():
    df = pl.DataFrame(_full_enriched_row())
    out = pipeline._select_storage_cols(df)
    assert out.columns == pipeline.ENRICHED_STORAGE_COLS
    assert len(out.columns) == 15


def test_select_storage_cols_raises_on_missing_derived_cols():
    """缺换手率/连板数时拒绝写入, 不再静默产出 12 列窄表。"""
    row = _full_enriched_row()
    for c in ("turnover_rate", "consecutive_limit_ups", "consecutive_limit_downs"):
        del row[c]
    df = pl.DataFrame(row)

    with pytest.raises(ValueError, match="缺列"):
        pipeline._select_storage_cols(df)


def test_select_storage_cols_allows_empty_frame():
    """空分区 (无行) 不做 fail-loud, 避免阻断正常的空写入。"""
    df = pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date})
    assert pipeline._select_storage_cols(df).height == 0
