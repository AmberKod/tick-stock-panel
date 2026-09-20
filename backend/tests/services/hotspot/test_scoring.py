"""Hotspot 评分算法单元测试.

覆盖:
- safe_text / safe_float / clamp 防御式工具
- compute_board_heat_score 边界条件
- classify_stage 五段分类规则
- score_constituent 多维加权
- assign_roles 角色分配
- hot_stock_score_breakdown 诊断拆分
"""
from __future__ import annotations

import math
from dataclasses import asdict

import pytest

from app.services.hotspot.models import HotspotStock
from app.services.hotspot.scoring import (
    assign_role,
    assign_roles,
    clamp,
    classify_stage,
    compute_board_heat_score,
    hot_stock_score_breakdown,
    safe_float,
    safe_text,
    score_constituent,
)

# ---------------------------------------------------------------
# safe_text / safe_float
# ---------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("  hello  ", "hello"),
        ("", ""),
        (None, ""),
        ("nan", ""),
        ("NONE", ""),
        ("Null", ""),
        (float("nan"), ""),
        (float("inf"), ""),
        (123, "123"),
    ],
)
def test_safe_text(value, expected):
    assert safe_text(value) == expected


@pytest.mark.parametrize(
    "value,default,expected",
    [
        ("3.14", 0.0, 3.14),
        ("abc", 1.5, 1.5),
        (None, 0.0, None),  # None 区分异常与没值
        (float("inf"), 9.9, 9.9),
        (float("-inf"), 9.9, 9.9),
        (float("nan"), 9.9, 9.9),
        (0, 1.0, 0.0),  # 0 不应当作"没值"
        (-1.5, 9.9, -1.5),  # 负数保留
    ],
)
def test_safe_float(value, default, expected):
    if expected is None:
        assert safe_float(value, default) is None
    else:
        result = safe_float(value, default)
        assert result is not None
        assert math.isclose(result, expected, rel_tol=1e-9)


def test_clamp():
    assert clamp(5.0, 0.0, 10.0) == 5.0
    assert clamp(-1.0, 0.0, 10.0) == 0.0
    assert clamp(11.0, 0.0, 10.0) == 10.0


# ---------------------------------------------------------------
# compute_board_heat_score
# ---------------------------------------------------------------

def test_board_score_top_rank_surge_is_near_max():
    # 涨幅 9.8%、rank=1 → 应在 95 附近(不允许超过 100)
    score = compute_board_heat_score(0.098, 1)
    assert 95 <= score <= 100


def test_board_score_negative_change_clamps_to_zero():
    score = compute_board_heat_score(-0.10, 50)
    assert score >= 0
    assert score < 30


def test_board_score_rank_increases_lowers_when_change_fixed():
    high = compute_board_heat_score(0.05, 1)
    low = compute_board_heat_score(0.05, 80)
    assert high == pytest.approx(65.0)
    assert low == pytest.approx(35.375)
    assert high > low


def test_board_score_missing_rank_only_change_counts():
    score = compute_board_heat_score(0.02, None)
    # 0..1.5 倍 70 = 0..105;2% 涨幅 = 14;无 rank 时仍落 0..30 之间
    assert 0 <= score <= 30


# ---------------------------------------------------------------
# classify_stage
# ---------------------------------------------------------------

def test_classify_stage_initial_when_empty():
    assert classify_stage() == "初次异动"


def test_classify_stage_acceleration_path():
    # high latest + up trend + persistence 命中"加速主升"
    stage = classify_stage(
        latest_score=80,
        trend_score=10,
        persistence_score=70,
        cooling_score=0,
        observations=5,
    )
    assert stage == "加速主升"


def test_classify_stage_cooling_path():
    # cooling >= 8 + 最新分 < 60 → 降温退潮
    stage = classify_stage(
        latest_score=50,
        trend_score=-3,
        cooling_score=9,
        persistence_score=10,
        observations=3,
    )
    assert stage == "降温退潮"


def test_classify_stage_diffusion_path():
    stage = classify_stage(
        state="persistent_hot",
        latest_score=60,
        trend_score=2,
        cooling_score=1,
        observations=2,
    )
    assert stage == "确认扩散"


def test_classify_stage_fallback_initial():
    stage = classify_stage(latest_score=10, observations=0)
    assert stage == "初次异动"


# ---------------------------------------------------------------
# score_constituent
# ---------------------------------------------------------------

def test_score_constituent_limit_up_beats_normal():
    row = {
        "code": "X", "change_pct": 0.10, "amount": 1e9,
        "turnover_rate": 0.05, "volume_ratio": 2.0, "net_inflow": 5e7,
        "is_limit_up": True, "active_days": 3, "evidence_count": 2,
    }
    limit_score = score_constituent(row)

    flat_row = dict(row, change_pct=0.002, is_limit_up=False)
    flat_score = score_constituent(flat_row)

    assert limit_score > flat_score
    assert 0 <= limit_score <= 100
    assert 0 <= flat_score <= 100


def test_score_constituent_handles_missing_fields():
    # 仅 code 必须存在,其它都缺失
    row = {"code": "X"}
    score = score_constituent(row)
    # 默认 base 35,缺字段不加分也不扣分
    assert math.isclose(score, 35.0, rel_tol=1e-9)


def test_hot_stock_score_breakdown_sum_matches_score():
    row = {
        "code": "X", "change_pct": 0.05, "amount": 5e8, "turnover_rate": 0.03,
        "volume_ratio": 1.2, "net_inflow": 1e7, "is_limit_up": False,
        "active_days": 1, "evidence_count": 1,
    }
    breakdown = hot_stock_score_breakdown(row)
    full = score_constituent(row)
    # base + 各维度 (近似) 应等于 full(允许 log10 浮点误差)
    total = sum(breakdown.values())
    assert math.isclose(total, full, abs_tol=0.01)


def test_score_constituent_negative_inflow_penalized():
    pos = score_constituent({"code": "X", "net_inflow": 5e8})
    neg = score_constituent({"code": "X", "net_inflow": -5e8})
    assert pos > neg


def test_score_constituent_with_hotspot_stock_dataclass():
    s = HotspotStock(code="X", change_pct=0.05, amount=2e8, turnover_rate=0.04,
                     volume_ratio=1.5, net_inflow=2e7, is_limit_up=True,
                     active_days=2, evidence_count=1)
    score = score_constituent(s)
    assert 50 < score < 100


@pytest.mark.parametrize(
    "change,turnover,expected_change,expected_turnover,expected_score",
    [
        (0.05, 0.04, 13.5, 4.4, 52.9),
        (-0.05, 0.04, -13.5, 4.4, 25.9),
        (0.001, 0.0002, 0.27, 0.022, 35.292),
        (0.20, 0.20, 32.0, 14.0, 81.0),
        (-0.20, -0.01, -18.0, 0.0, 17.0),
    ],
)
def test_decimal_inputs_match_percentage_point_reference(
    change, turnover, expected_change, expected_turnover, expected_score
):
    """The reference's 2.7/1.1 coefficients operate on percentage points."""
    stock = HotspotStock(code="SAMPLE", change_pct=change, turnover_rate=turnover)
    for row in (stock, asdict(stock)):
        assert score_constituent(row) == pytest.approx(expected_score)
        breakdown = hot_stock_score_breakdown(row)
        assert breakdown["change"] == pytest.approx(expected_change)
        assert breakdown["turnover"] == pytest.approx(expected_turnover)
        assert sum(breakdown.values()) == pytest.approx(expected_score)


@pytest.mark.parametrize("limit_up,expected_score,bonus", [(False, 71.0, 0.0), (True, 79.0, 8.0)])
def test_explicit_limit_up_agrees_for_dict_and_dataclass(limit_up, expected_score, bonus):
    stock = HotspotStock(
        code="300001.SZ", change_pct=0.10, amount=1e8, is_limit_up=limit_up
    )
    row = asdict(stock)
    assert score_constituent(stock) == pytest.approx(expected_score)
    assert score_constituent(row) == pytest.approx(expected_score)
    assert hot_stock_score_breakdown(stock) == hot_stock_score_breakdown(row)
    assert hot_stock_score_breakdown(row)["limit_up"] == bonus


@pytest.mark.parametrize("code", ["600000.SH", "300001.SZ", "688001.SH", "430047.BJ"])
def test_unknown_limit_up_is_not_inferred_from_change(code):
    row = {"code": code, "change_pct": 0.10, "amount": 1e8}
    assert score_constituent(row) == pytest.approx(71.0)
    assert score_constituent(HotspotStock(**row)) == pytest.approx(71.0)
    assert hot_stock_score_breakdown(row)["limit_up"] == 0.0


@pytest.mark.parametrize("flag", [None, "false", "unknown", "0"])
def test_unconfirmed_limit_up_flags_do_not_add_bonus(flag):
    row = {"code": "300001.SZ", "change_pct": 0.10, "amount": 1e8, "is_limit_up": flag}
    assert score_constituent(row) == pytest.approx(71.0)
    assert hot_stock_score_breakdown(row)["limit_up"] == 0.0


def test_score_and_breakdown_do_not_mutate_input():
    stock = HotspotStock(code="SAMPLE", change_pct=0.05, turnover_rate=0.04)
    before = asdict(stock)
    score_constituent(stock)
    hot_stock_score_breakdown(stock)
    assert asdict(stock) == before
    row = dict(before)
    score_constituent(row)
    hot_stock_score_breakdown(row)
    assert row == before


# ---------------------------------------------------------------
# assign_role(s)
# ---------------------------------------------------------------

def test_assign_roles_orders_and_tags():
    a = HotspotStock(code="A", change_pct=0.10, hot_stock_score=85)
    b = HotspotStock(code="B", change_pct=0.05, hot_stock_score=72)
    c = HotspotStock(code="C", change_pct=0.03, hot_stock_score=64)
    d = HotspotStock(code="D", change_pct=0.01, hot_stock_score=50)
    e = HotspotStock(code="E", change_pct=-0.01, hot_stock_score=42)
    f = HotspotStock(code="F", change_pct=-0.10, hot_stock_score=20)

    sorted_stocks = assign_roles([d, a, f, b, e, c])
    assert [s.code for s in sorted_stocks] == ["A", "B", "C", "D", "E", "F"]

    # A 是 top1 且 score>=70: 核心龙头
    assert sorted_stocks[0].role == "核心龙头"
    # B.score=72 < top_score-8=77 → 不满足 top3 核心龙头, 改走 助攻 (score>=62, change>=3%)
    assert sorted_stocks[1].role == "助攻"
    # C.score=64, change=3%: 助攻
    assert sorted_stocks[2].role == "助攻"
    # D.score=50, change=1%: 补涨
    assert sorted_stocks[3].role == "补涨"
    # E.score=42, change=-1%: 后排
    assert sorted_stocks[4].role == "后排"
    # F.score=20: 掉队
    assert sorted_stocks[5].role == "掉队"


def test_assign_roles_top3_can_promote_secondary_leader():
    """当 top2 跟 top1 接近 (>=top_score-8) 时, 也可标记核心龙头."""
    a = HotspotStock(code="A", change_pct=0.10, hot_stock_score=80)
    b = HotspotStock(code="B", change_pct=0.06, hot_stock_score=75)  # 75 >= 80-8=72 ✓
    c = HotspotStock(code="C", change_pct=0.05, hot_stock_score=70)  # 70 < 80-8=72 ✗ → 助攻

    sorted_stocks = assign_roles([c, a, b])
    assert sorted_stocks[1].role == "核心龙头"  # B
    assert sorted_stocks[2].role == "助攻"     # C


def test_assign_roles_zero_scores_get_recomputed():
    """hot_stock_score=0 时 assign_role 会触发回填。"""
    s = HotspotStock(code="X", change_pct=0.05, amount=1e8)
    assert s.hot_stock_score == 0.0
    assign_roles([s])
    assert s.hot_stock_score > 0
    # 默认 role 应该是后排或更好
    assert s.role in {"后排", "补涨", "助攻", "核心龙头"}


@pytest.mark.parametrize(
    "change,score,expected_role", [(0.05, 72.0, "核心龙头"), (0.03, 64.0, "助攻")]
)
def test_assign_role_preserves_decimal_thresholds(change, score, expected_role):
    stock = HotspotStock(code="SAMPLE", change_pct=change, hot_stock_score=score)
    assert assign_role(stock, top_score=75.0) == expected_role
