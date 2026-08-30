"""HK 数据适配器 (M1 H3c) 单测。

akshare 在沙箱环境下不可装 (jsonpath 0.82 setup.py 旧式打包被 safe-delete 拦截),
本测试只验证不依赖 akshare 的内置 10 龙头池路径。
akshare 集成测试在用户机器 (akshare 已装) 单独跑。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services.hk_data_adapter import (
    HK_DEMO_NAMES,
    HK_DEMO_SYMBOLS,
    fetch_hk_daily_akshare,
    fetch_hk_instruments_akshare,
    load_demo_instruments,
    sync_hk_instruments,
)


def test_demo_symbols_count():
    """M1 内置 10 个港股龙头。"""
    assert len(HK_DEMO_SYMBOLS) == 10
    assert all(s.endswith(".HK") for s in HK_DEMO_SYMBOLS)


def test_load_demo_instruments_schema():
    df = load_demo_instruments()
    assert not df.is_empty()
    assert set(df.columns) == set([
        "symbol", "name", "code", "exchange", "asset_type", "source", "market"
    ])
    assert df["market"].unique().to_list() == ["HK"]
    assert df["source"].unique().to_list() == ["hk_demo"]
    # 静态池 10 行
    assert df.height == 10


def test_load_demo_instruments_names_aligned():
    """10 龙头符号与名称一一对应。"""
    df = load_demo_instruments()
    for sym in HK_DEMO_SYMBOLS:
        row = df.filter(pl.col("symbol") == sym).row(0, named=True)
        assert row["name"] == HK_DEMO_NAMES[sym]


def test_akshare_unavailable_returns_none():
    """沙箱无 akshare, fetch_hk_instruments_akshare 必须返回 None (不抛错)。"""
    result = fetch_hk_instruments_akshare()
    assert result is None


def test_akshare_daily_unavailable_returns_empty():
    """沙箱无 akshare, fetch_hk_daily_akshare 必须返回空 df (不抛错)。"""
    result = fetch_hk_daily_akshare("00700", date(2024, 1, 1), date(2024, 12, 31))
    assert result.is_empty()


def test_sync_hk_instruments_uses_demo_only(tmp_path: Path):
    """akshare 不可用时, sync_hk_instruments 仅写 10 龙头。"""
    count = sync_hk_instruments(tmp_path, use_akshare=True)
    assert count == 10
    out = tmp_path / "instruments" / "hk_instruments.parquet"
    assert out.exists()
    df = pl.read_parquet(out)
    assert df.height == 10
    assert df["market"].unique().to_list() == ["HK"]


def test_sync_hk_instruments_akshare_disabled(tmp_path: Path):
    """显式 use_akshare=False 时, 仍写 10 龙头 (skip akshare 分支)。"""
    count = sync_hk_instruments(tmp_path, use_akshare=False)
    assert count == 10


def test_demo_instruments_uses_normalizer():
    """H3c 经 normalize_instruments, 必须含 market 派生列 (M0 行为持续生效)。"""
    df = load_demo_instruments()
    assert "market" in df.columns
    assert df.filter(pl.col("market") != "HK").is_empty()


# ── H4: 涨跌停软门控 ──

def test_numpy_limit_pct_nan_for_hk_symbols():
    """H4 软门控: HK 标的 (无涨跌停) → NaN, 不污染 CN 标的。"""
    import numpy as np
    from app.price_limits import numpy_limit_pct_vectors
    symbols = ["600519.SH", "00700.HK", "09988.HK", "000001.SZ"]
    names = ["贵州茅台", "腾讯控股", "阿里", "平安银行"]
    legacy, current = numpy_limit_pct_vectors(symbols, names)
    # CN 标的: 0.10 (主板)/ 0.10 (主板, 非 ST)
    assert current[0] == 0.10   # 600519.SH
    assert current[3] == 0.10   # 000001.SZ
    # HK 标的: NaN
    assert np.isnan(current[1])  # 00700.HK
    assert np.isnan(current[2])  # 09988.HK
    assert np.isnan(legacy[1]) and np.isnan(legacy[2])


def test_numpy_limit_pct_cn_unchanged():
    """H4 不影响 CN 标的: 老测试预期 (主板 10% / ST 5%) 全部保留。"""
    from app.price_limits import numpy_limit_pct_vectors
    symbols = ["600519.SH", "300750.SZ", "688981.SH", "832000.BJ"]
    names = ["茅台", "宁德", "中芯", "北证"]
    legacy, current = numpy_limit_pct_vectors(symbols, names)
    assert current[0] == 0.10  # 主板
    assert current[1] == 0.20  # 创业板
    assert current[2] == 0.20  # 科创板
    assert current[3] == 0.30  # 北交
    # legacy 与 current 一致 (无 ST 触发)
    assert legacy[0] == current[0]


def test_profile_has_price_limit_soft_gate():
    """H4 软门控信号源: profile.has_price_limit() 显式表达。"""
    assert load_demo_instruments() is not None  # 仅作为测试有序性占位
    from app.markets import get_profile
    assert get_profile("CN").has_price_limit() is True
    assert get_profile("HK").has_price_limit() is False


# ── H6 日 K 落盘 ──

def test_sync_hk_daily_writes_partition(tmp_path: Path, monkeypatch):
    """H6: 港股日 K 写入 data/kline_daily/symbol={code}.HK/part.parquet。

    通过 monkeypatch 改 app.config.settings.data_dir 到 tmp_path。
    """
    import polars as pl
    from app.services import hk_data_adapter
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)

    df = pl.DataFrame({
        "symbol": ["00700.HK"] * 3,
        "date": pl.date_range(date(2024, 1, 1), date(2024, 1, 3), eager=True),
        "open": [300.0, 305.0, 310.0],
        "high": [305.0, 310.0, 315.0],
        "low": [298.0, 303.0, 308.0],
        "close": [304.0, 309.0, 314.0],
        "volume": [1000.0, 2000.0, 3000.0],
    })
    out = hk_data_adapter.sync_hk_daily_to_parquet(df, "00700.HK")
    assert out is not None
    assert out.exists()
    assert out == tmp_path / "kline_daily" / "symbol=00700.HK" / "part.parquet"
    # 再读, 数据一致
    re = pl.read_parquet(out)
    assert re.height == 3


def test_sync_hk_daily_merge_dedup(tmp_path: Path, monkeypatch):
    """二次写入相同 date → 覆盖, 不重复。"""
    import polars as pl
    from app.services import hk_data_adapter
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)

    df1 = pl.DataFrame({
        "symbol": ["00700.HK"] * 2,
        "date": pl.date_range(date(2024, 1, 1), date(2024, 1, 2), eager=True),
        "open": [300.0, 305.0], "high": [305.0, 310.0],
        "low": [298.0, 303.0], "close": [304.0, 309.0],
        "volume": [1000.0, 2000.0],
    })
    hk_data_adapter.sync_hk_daily_to_parquet(df1, "00700.HK")
    # 第二次: 新增 1/3 + 覆盖 1/2
    df2 = pl.DataFrame({
        "symbol": ["00700.HK"] * 2,
        "date": [date(2024, 1, 2), date(2024, 1, 3)],
        "open": [999.0, 310.0], "high": [999.0, 315.0],
        "low": [999.0, 308.0], "close": [999.0, 314.0],
        "volume": [999.0, 3000.0],
    })
    hk_data_adapter.sync_hk_daily_to_parquet(df2, "00700.HK")
    re = hk_data_adapter.read_hk_daily("00700.HK")
    # 1/2 应是新值 999, 不是 305
    row_2 = re.filter(pl.col("date") == date(2024, 1, 2)).row(0, named=True)
    assert row_2["open"] == 999.0
    # 总行数 3 (去重)
    assert re.height == 3


def test_read_hk_daily_missing_returns_empty(tmp_path: Path, monkeypatch):
    from app.services import hk_data_adapter
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path, raising=False)
    assert hk_data_adapter.read_hk_daily("00700.HK").is_empty()
