"""A 股本地概念热点源单测。

锁定: 概念展开(分号分隔)、等权平均涨幅、成分数门槛、涨停阈值按板块
(主板 10% / 创业板科创板 20%)、行情不足时 fail-closed 而非编造。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.services.hotspot.cn_concept_source import (
    MIN_MEMBERS,
    CnConceptHotspotSource,
    _limit_up_threshold,
)

PREV = "2026-09-16"
LATEST = "2026-09-17"


def _ext(root: Path, rows: list[tuple[str, str, str]]) -> None:
    """(symbol, 简称, 所属概念) — 概念用分号分隔多值。"""
    path = root / "ext_data" / "ext_gn_ths" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "股票代码": [r[0] for r in rows],
        "股票简称": [r[1] for r in rows],
        "所属概念": [r[2] for r in rows],
        "symbol": [r[0] for r in rows],
    }).write_parquet(path)


def _quotes(root: Path, day: str, rows: list[tuple[str, float, float]]) -> None:
    """(symbol, close, amount)"""
    path = root / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "close": [r[1] for r in rows],
        "amount": [r[2] for r in rows],
        "open": [r[1] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[1] for r in rows],
        "volume": [100.0] * len(rows),
    }).write_parquet(path)


def _limit_up_threshold_cases() -> None:
    assert _limit_up_threshold("600354.SH") == pytest.approx(0.098)   # 主板
    assert _limit_up_threshold("300001.SZ") == pytest.approx(0.198)   # 创业板
    assert _limit_up_threshold("688001.SH") == pytest.approx(0.198)   # 科创板
    assert _limit_up_threshold("920087.BJ") == pytest.approx(0.298)   # 北交所


def test_limit_up_threshold_by_board():
    _limit_up_threshold_cases()


def test_concept_split_and_equal_weight_average(tmp_path):
    # 转基因: 3 只 (+10% / +20% / +5%) → 等权平均 11.67%
    # 农业:   2 只, 低于 MIN_MEMBERS → 不进榜
    _ext(tmp_path, [
        ("000001.SZ", "甲", "农业;转基因"),
        ("600354.SH", "乙", "转基因"),
        ("300001.SZ", "丙", "转基因"),
        ("000002.SZ", "丁", "农业"),
    ])
    _quotes(tmp_path, PREV, [("000001.SZ", 10.0, 1e8), ("600354.SH", 10.0, 1e8),
                             ("300001.SZ", 10.0, 1e8), ("000002.SZ", 10.0, 1e8)])
    _quotes(tmp_path, LATEST, [("000001.SZ", 11.0, 1e8), ("600354.SH", 12.0, 1e8),
                               ("300001.SZ", 10.5, 1e8), ("000002.SZ", 10.5, 1e8)])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert res.provider_used == "cn_local_concept"
    assert [s.topic for s in res] == ["转基因"]  # 农业被 MIN_MEMBERS 过滤
    top = res[0]
    assert top.change_pct == pytest.approx((0.10 + 0.20 + 0.05) / 3)
    assert top.sample_stock_count == 3
    assert top.topic_date == LATEST
    # 000001(+10% 主板涨停) + 600354(+20% 主板也判涨停) = 2
    assert "limit_up=2" in top.state


def test_board_specific_limit_up(tmp_path):
    """创业板 +5% 不算涨停 (阈值 19.8%), 主板 +10% 算。"""
    _ext(tmp_path, [("300001.SZ", "创", "X"), ("600001.SH", "主", "X"), ("000001.SZ", "深", "X")])
    _quotes(tmp_path, PREV, [("300001.SZ", 10.0, 1.0), ("600001.SH", 10.0, 1.0), ("000001.SZ", 10.0, 1.0)])
    _quotes(tmp_path, LATEST, [("300001.SZ", 10.5, 1.0), ("600001.SH", 11.0, 1.0), ("000001.SZ", 11.0, 1.0)])

    detail = CnConceptHotspotSource(tmp_path).fetch_detail("X", top_stocks=10)
    assert detail is not None
    board = {s.code: s.is_limit_up for s in detail.stocks}
    assert board["300001.SZ"] is False
    assert board["600001.SH"] is True
    assert board["000001.SZ"] is True


def test_min_members_filter(tmp_path):
    """不足 MIN_MEMBERS 的概念不进榜 — 小样本平均涨幅噪声太大。"""
    _ext(tmp_path, [("000001.SZ", "甲", "小"), ("000002.SZ", "乙", "小")])
    _quotes(tmp_path, PREV, [("000001.SZ", 10.0, 1.0), ("000002.SZ", 10.0, 1.0)])
    _quotes(tmp_path, LATEST, [("000001.SZ", 11.0, 1.0), ("000002.SZ", 11.0, 1.0)])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert len(res) == 0
    assert MIN_MEMBERS >= 3


def test_fail_closed_when_quotes_insufficient(tmp_path):
    """enriched 不足两个交易日 → 报 source_errors, 绝不编造涨幅。"""
    _ext(tmp_path, [("000001.SZ", "甲", "X")])
    _quotes(tmp_path, LATEST, [("000001.SZ", 11.0, 1.0)])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert len(res) == 0
    assert res.source_errors
    assert any("quotes" in e for e in res.source_errors)


def test_fail_closed_when_ext_missing(tmp_path):
    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert len(res) == 0
    assert any("members" in e for e in res.source_errors)


def test_detail_sorted_by_change_and_roles(tmp_path):
    _ext(tmp_path, [("000001.SZ", "甲", "X"), ("000002.SZ", "乙", "X"), ("000003.SZ", "丙", "X")])
    _quotes(tmp_path, PREV, [("000001.SZ", 10.0, 1.0), ("000002.SZ", 10.0, 1.0), ("000003.SZ", 10.0, 1.0)])
    _quotes(tmp_path, LATEST, [("000001.SZ", 11.0, 1.0), ("000002.SZ", 12.0, 1.0), ("000003.SZ", 10.5, 1.0)])

    detail = CnConceptHotspotSource(tmp_path).fetch_detail("X", top_stocks=10)
    assert detail is not None
    assert [s.code for s in detail.stocks] == ["000002.SZ", "000001.SZ", "000003.SZ"]
    assert {s.role for s in detail.stocks}  # assign_roles 已回填
    assert detail.stock_count == 3


def test_unsupported_market(tmp_path):
    res = CnConceptHotspotSource(tmp_path).discover(market="hk", top=10)
    assert len(res) == 0
    assert any("unsupported" in e for e in res.source_errors)


def test_missing_board_column_marks_missing_field(tmp_path):
    """enriched 缺连板列时照常出榜, 但显式标 missing_fields (不静默当 0)。"""
    _ext(tmp_path, [("000001.SZ", "甲", "X"), ("000002.SZ", "乙", "X"), ("000003.SZ", "丙", "X")])
    _quotes(tmp_path, PREV, [("000001.SZ", 10.0, 1.0), ("000002.SZ", 10.0, 1.0), ("000003.SZ", 10.0, 1.0)])
    _quotes(tmp_path, LATEST, [("000001.SZ", 11.0, 1.0), ("000002.SZ", 11.0, 1.0), ("000003.SZ", 10.5, 1.0)])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert len(res) == 1
    assert "consecutive_limit_ups" in res[0].missing_fields
