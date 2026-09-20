"""港美异动监控测试 — 动量口径 + 复利 mom3 + 接近度/状态分档。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services.hk_us_abnormal import (
    HK_US_THRESHOLDS,
    _cache,
    _cache_lock,
    _name_cache,
    _name_cache_lock,
    build_hk_us_abnormal_overview,
)


def _write_enriched(tmp_path, rows: list[tuple[str, date, float, float | None,
                                                float | None, float | None]]) -> None:
    """rows: (symbol, date, close, change_pct, momentum_10d, momentum_30d)。"""
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [r[1] for r in rows],
            "close": [r[2] for r in rows],
            "change_pct": [r[3] for r in rows],
            "momentum_10d": [r[4] for r in rows],
            "momentum_30d": [r[5] for r in rows],
        }
    )
    for sym in sorted({r[0] for r in rows}):
        target = tmp_path / "kline_hk_us_enriched" / f"symbol={sym}"
        target.mkdir(parents=True, exist_ok=True)
        df.filter(pl.col("symbol") == sym).write_parquet(target / "part.parquet")


def _clear_caches() -> None:
    with _cache_lock:
        _cache.clear()
    with _name_cache_lock:
        _name_cache.clear()


def test_mom3_compound_interest_math(tmp_path) -> None:
    """3 日动量必须是复利连乘 (1+x)-1, 而非裸乘积或求和。"""
    _clear_caches()
    days = [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
    # +9%/+8%/+6% → (1.09*1.08*1.06)-1 = 0.247832; 裸乘积=0.000432; 求和=0.23
    rows = [("01810.HK", d, 10.0, pct, None, None) for d, pct in zip(days, (0.09, 0.08, 0.06), strict=True)]
    _write_enriched(tmp_path, rows)

    result = build_hk_us_abnormal_overview(tmp_path, "HK", min_closeness=0.0, limit=10)
    row = result["rows"][0]
    # 接口按 4 位小数返回
    assert abs(row["windows"]["3d"]["value"] - 0.2478) < 1e-9


def test_mom3_null_rows_ignored(tmp_path) -> None:
    """窗口内 null 涨跌行被忽略 (次新股/停牌按实际行数近似)。"""
    _clear_caches()
    days = [date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
    rows = [
        ("01810.HK", days[0], 10.0, None, None, None),   # null 行忽略
        ("01810.HK", days[1], 10.0, 0.10, None, None),
        ("01810.HK", days[2], 10.0, 0.10, None, None),
        ("01810.HK", days[3], 10.0, 0.10, None, None),
    ]
    _write_enriched(tmp_path, rows)

    result = build_hk_us_abnormal_overview(tmp_path, "HK", min_closeness=0.0, limit=10)
    # 3 行有效: 1.1^3 - 1 = 0.331 (null 不参与, 也不贡献)
    assert abs(result["rows"][0]["windows"]["3d"]["value"] - 0.331) < 1e-6


def test_market_suffix_scoping(tmp_path) -> None:
    """HK 查询不得混入 .US 行, 反之亦然。"""
    _clear_caches()
    d = date(2026, 8, 28)
    rows = [
        ("01810.HK", d, 10.0, 0.10, 1.30, None),   # HK 10日触发 (1.30/1.20)
        ("AAPL.US", d, 200.0, 0.02, 1.30, None),   # US 同动量
    ]
    _write_enriched(tmp_path, rows)

    hk = build_hk_us_abnormal_overview(tmp_path, "HK", min_closeness=0.0)
    us = build_hk_us_abnormal_overview(tmp_path, "US", min_closeness=0.0)
    assert {r["symbol"] for r in hk["rows"]} == {"01810.HK"}
    assert {r["symbol"] for r in us["rows"]} == {"AAPL.US"}


def test_closeness_threshold_and_status(tmp_path) -> None:
    """阈值按动量方向取对应侧 (up/down); 状态 0.7/1.0 分档; min_closeness 过滤。"""
    _clear_caches()
    d = date(2026, 8, 28)
    up_t_10, down_t_10 = HK_US_THRESHOLDS[10]   # (1.20, 0.60)
    rows = [
        # +1.30 → 1.30/1.20 = 1.083 triggered
        ("AAAA.HK", d, 10.0, 0.01, 1.30, None),
        # -0.45 → 0.45/0.60 = 0.75 edge (负向阈值更严)
        ("BBBB.HK", d, 10.0, -0.01, -0.45, None),
        # +0.30 → 0.30/1.20 = 0.25 watch (低于 0.5 被默认门槛过滤)
        ("CCCC.HK", d, 10.0, 0.01, 0.30, None),
    ]
    _write_enriched(tmp_path, rows)

    result = build_hk_us_abnormal_overview(tmp_path, "HK", min_closeness=0.5)
    by = {r["symbol"]: r for r in result["rows"]}
    assert by["AAAA.HK"]["status"] == "triggered"
    assert abs(by["AAAA.HK"]["windows"]["10d"]["closeness"] - round(1.30 / up_t_10, 4)) < 1e-6
    assert by["BBBB.HK"]["status"] == "edge"
    assert by["BBBB.HK"]["windows"]["10d"]["threshold"] == down_t_10
    assert "CCCC.HK" not in by
    # 排序按接近度降序
    cs = [r["max_closeness"] for r in result["rows"]]
    assert cs == sorted(cs, reverse=True)


def test_schema_contract_matches_a_share(tmp_path) -> None:
    """输出 row schema 与 A股 build_overview 对齐 (前端信息条直接消费)。"""
    _clear_caches()
    d = date(2026, 8, 28)
    _write_enriched(tmp_path, [("01810.HK", d, 10.0, 0.05, 1.30, None)])

    result = build_hk_us_abnormal_overview(tmp_path, "HK")
    assert result["as_of"] == "2026-08-28"
    assert result["market"] == "HK"
    assert set(result.keys()) >= {"asof", "as_of", "market", "rules", "counts", "total", "rows"}
    row = result["rows"][0]
    assert set(row.keys()) == {
        "symbol", "name", "board", "st", "close", "rt_pct",
        "windows", "max_closeness", "status",
    }
    win = row["windows"]["10d"]
    assert set(win.keys()) == {"value", "threshold", "closeness"}


def test_missing_data_returns_empty(tmp_path) -> None:
    """无 enriched 目录时返回空 rows, 不抛异常。"""
    _clear_caches()
    result = build_hk_us_abnormal_overview(tmp_path, "US")
    assert result["rows"] == []
    assert result["total"] == 0
    assert result["as_of"] is None


def test_latest_date_only(tmp_path) -> None:
    """close/mom10/mom30 只取最新交易日行, 历史行不参与总览 (mom3 除外, 它按定义取近 3 行)。"""
    _clear_caches()
    d1, d2 = date(2026, 8, 27), date(2026, 8, 28)
    rows = [
        # 历史行 10日动量 0.90 (若混入将 0.9/1.2=0.75 上榜)
        ("01810.HK", d1, 10.0, 0.01, 0.90, None),
        # 最新行 10日动量 0.30 → 0.25; mom3=(1.01*1.01)-1≈0.02 → 0.08; 全窗口 < 0.5
        ("01810.HK", d2, 10.0, 0.01, 0.30, None),
    ]
    _write_enriched(tmp_path, rows)

    result = build_hk_us_abnormal_overview(tmp_path, "HK", min_closeness=0.5)
    assert result["rows"] == []
    assert result["as_of"] == "2026-08-28"
