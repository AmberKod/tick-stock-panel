"""港股 coverage 缺口策略回归 (09-16 全窗口重跑暴露)。

背景: 全窗口抓取下 provider 报 coverage_complete=False, 缺口是
2023-07-17 (台风泰利停市) / 2020-10-13 (台风浪卡停市) / 2009-01-01 (元旦)
/ 2007-12-24 (平安夜半日市) 之类 —— 新浪没有这些行是对的, 是交易日历把它们
算了进去。publish 里 coverage_complete=False 会直接跳过 enriched 重算, 随后
`repair and enriched.is_empty()` 抛错 → 2794 只 legacy 标的全部 publication_failed。

修复: 缺口全部落在远期 (>90 天) 时不阻断指标重算; 近期缺口仍然阻断
(那才是数据源退化/拉取截断的真信号)。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services.hk_data_adapter import _coverage_gap_only_historical


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def test_all_historical_gaps_are_accepted():
    """09-16 实测样本: 00005.HK 的 12 天缺口全在 3 年前。"""
    report = {"missing_dates": ["2023-07-17", "2020-10-13", "2009-01-01", "2007-12-24"]}
    ok, count, latest = _coverage_gap_only_historical(report)
    assert ok is True
    assert count == 4
    assert latest == "2023-07-17"


def test_recent_gap_blocks_enriched():
    """近期缺口 (数据源退化/拉取截断) → 不放行。"""
    report = {"missing_dates": ["2023-07-17", _iso(3)]}
    ok, _, latest = _coverage_gap_only_historical(report)
    assert ok is False
    assert latest == _iso(3)


def test_boundary_gap_just_inside_window_blocks():
    """正好落在 90 天窗口内 → 阻断。"""
    report = {"missing_dates": [_iso(89)]}
    ok, _, _ = _coverage_gap_only_historical(report)
    assert ok is False


def test_gap_just_outside_window_passes():
    """刚好早于 90 天 → 放行。"""
    report = {"missing_dates": [_iso(91)]}
    ok, _, _ = _coverage_gap_only_historical(report)
    assert ok is True


def test_empty_or_unparseable_gaps_fail_closed():
    """无明细或含无法解析的日期 → 不放行。"""
    assert _coverage_gap_only_historical({})[0] is False
    assert _coverage_gap_only_historical({"missing_dates": []})[0] is False
    assert _coverage_gap_only_historical({"missing_dates": ["not-a-date"]})[0] is False
