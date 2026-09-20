"""P1 跨市场自选: watchlist ``market`` 字段回归测试。

契约:
  1. market 由 symbol 后缀推导 (.HK → hk / .US → us / 其余 → cn)
  2. 新增条目落盘即带 market, 且与 list_symbols 读取一致
  3. 旧 parquet (无 market 列) 读取时自动补列, 不报错 (向后兼容)
  4. /enriched 响应每行带 market, 前端据此渲染市场徽标
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import watchlist as wl_api
from app.services import watchlist as wl
from app.services.watchlist import _infer_market


def test_infer_market_by_suffix():
    assert _infer_market("00700.HK") == "hk"
    assert _infer_market("09988.HK") == "hk"
    assert _infer_market("AAPL.US") == "us"
    assert _infer_market("^GSPC.US") == "us"
    assert _infer_market("600519") == "cn"
    assert _infer_market("000001.SH") == "cn"
    assert _infer_market("300750.SZ") == "cn"
    assert _infer_market("") == "cn"


def test_add_batch_persists_market(monkeypatch, tmp_path):
    """新增港美股 + A 股混合标的, market 正确落盘并被 list_symbols 读回。"""
    monkeypatch.setattr(wl.settings, "data_dir", tmp_path)

    rows, added = wl.add_batch(["00700.HK", "AAPL.US", "600519"])
    assert added == 3
    # add_batch 保持「新处理的在前面」语义: 最后处理的排最前
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["00700.HK"]["market"] == "hk"
    assert by_symbol["AAPL.US"]["market"] == "us"
    assert by_symbol["600519"]["market"] == "cn"

    # 重读落盘文件, market 必须持久化 (而不是只在内存)
    persisted = {r["symbol"]: r["market"] for r in wl.list_symbols()}
    assert persisted == {"00700.HK": "hk", "AAPL.US": "us", "600519": "cn"}


def test_same_symbol_can_exist_in_different_markets(monkeypatch, tmp_path):
    """复合键必须允许同一 symbol 在不同市场独立存在。"""
    monkeypatch.setattr(wl.settings, "data_dir", tmp_path)

    rows, added = wl.add_batch(["ACME"], market="hk")
    assert added == 1
    rows, added = wl.add_batch([" acme "], market="us")
    assert added == 1
    assert {(row["market"], row["symbol"]) for row in rows} == {
        ("hk", "ACME"),
        ("us", "ACME"),
    }

    rows = wl.remove("ACME", market="hk")
    assert [(row["market"], row["symbol"]) for row in rows] == [("us", "ACME")]


def test_invalid_explicit_market_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(wl.settings, "data_dir", tmp_path)

    with pytest.raises(ValueError, match="不支持的自选市场"):
        wl.add("ACME", market="jp")


def test_legacy_parquet_without_market_column_migrates(monkeypatch, tmp_path):
    """旧 schema (无 market 列) 读取时按 symbol 后缀补列, 不抛错。"""
    monkeypatch.setattr(wl.settings, "data_dir", tmp_path)

    legacy = pl.DataFrame({
        "symbol": ["00700.HK", "AAPL.US", "600519"],
        "added_at": ["2026-01-01T00:00:00"] * 3,
        "note": [None, None, None],
        "group_ids": [[], [], []],
    })
    path = tmp_path / "user_data" / "watchlist.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_parquet(path)

    rows = wl.list_symbols()
    assert {r["symbol"]: r["market"] for r in rows} == {
        "00700.HK": "hk",
        "AAPL.US": "us",
        "600519": "cn",
    }


def test_enriched_rows_carry_market(monkeypatch):
    """enriched 端点每行补 market 列 (港美股/A 股混合)。"""
    today = date(2026, 8, 30)
    enriched = pl.DataFrame({"symbol": ["600519"], "close": [1700.0]})

    monkeypatch.setattr(wl_api.watchlist, "list_symbols", lambda: [
        {"symbol": "00700.HK", "added_at": "", "note": None, "group_ids": [], "market": "hk"},
        {"symbol": "AAPL.US", "added_at": "", "note": None, "group_ids": [], "market": "us"},
        {"symbol": "600519", "added_at": "", "note": None, "group_ids": [], "market": "cn"},
    ])
    monkeypatch.setattr(wl_api, "_parse_ext_columns", lambda s: [])

    repo = SimpleNamespace(
        get_etf_symbol_set=lambda: set(),
        get_index_symbol_set=lambda: set(),
        get_enriched_latest=lambda: (enriched, today),
        get_enriched_latest_asset=lambda asset: (pl.DataFrame(), None),
        get_instruments=lambda: pl.DataFrame(),
        get_name_map=lambda symbols: {},
        store=SimpleNamespace(db=None, data_dir=None),
    )
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    resp = wl_api.watchlist_enriched(request=req, ext_columns=None)
    rows = resp["rows"]
    assert len(rows) == 3, "自选每只都必须返回一行"
    assert {r["symbol"]: r["market"] for r in rows} == {
        "00700.HK": "hk",
        "AAPL.US": "us",
        "600519": "cn",
    }
