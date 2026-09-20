"""选股组合约束 + 行业热度因子 + basic_filter 扩展测试。"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.strategy.engine import StrategyEngine
from app.strategy.industry_heat import (
    attach_industry_heat,
    scoring_uses_industry_heat,
)
from app.strategy.portfolio_constraints import (
    _parse_industry,
    apply_portfolio_constraints,
    clear_industry_cache,
    infer_market_of_rows,
    normalize_portfolio_config,
)


@pytest.fixture(autouse=True)
def _clean_industry_cache():
    """行业映射模块级缓存会跨用例污染, 每用例前清空。"""
    clear_industry_cache()
    yield
    clear_industry_cache()


# ── _parse_industry 分级 ──────────────────────────────────

def test_parse_industry_levels() -> None:
    raw = "医药生物-医疗器械-医疗耗材"
    assert _parse_industry(raw, 1) == "医药生物"
    assert _parse_industry(raw, 2) == "医药生物-医疗器械"
    assert _parse_industry(raw, 3) == raw
    assert _parse_industry(None, 1) == "__unknown__"
    assert _parse_industry("", 1) == "__unknown__"
    assert _parse_industry("银行", 3) == "银行"


# ── normalize_portfolio_config ────────────────────────────

def test_portfolio_config_normalization() -> None:
    assert normalize_portfolio_config(None) is None
    assert normalize_portfolio_config({}) is None
    assert normalize_portfolio_config({"enabled": False, "max_same_industry": 3}) is None
    cfg = normalize_portfolio_config({"max_same_industry": "3", "concentration_penalty": "0.5", "industry_level": 2})
    assert cfg == {"max_same_industry": 3, "concentration_penalty": 0.5, "industry_level": 2}
    # 边界收敛
    cfg2 = normalize_portfolio_config({"max_same_industry": 0, "concentration_penalty": 9, "industry_level": 7})
    assert cfg2["max_same_industry"] == 1
    assert cfg2["concentration_penalty"] == 1.0
    assert cfg2["industry_level"] == 1
    cfg3 = normalize_portfolio_config({"enabled": True})
    assert cfg3["max_same_industry"] == 2  # 默认
    assert cfg3["concentration_penalty"] == 0.0


# ── apply_portfolio_constraints ───────────────────────────

_MAP = {
    "600000.SH": "银行",
    "600016.SH": "银行",
    "600030.SH": "银行",
    "000001.SZ": "银行",
    "688013.SH": "医药",
    "300164.SZ": "石油",
}


def _rows(*pairs: tuple[str, float]) -> list[dict]:
    return [{"symbol": s, "score": v} for s, v in pairs]


def test_portfolio_drop_mode() -> None:
    rows = _rows(
        ("600000.SH", 99), ("688013.SH", 95), ("600016.SH", 90),
        ("600030.SH", 85), ("000001.SZ", 80), ("300164.SZ", 70),
    )
    out = apply_portfolio_constraints(rows, _MAP, {"max_same_industry": 2})
    # 银行 4 只 → 保前 2 (600000/600016), 剔除 600030/000001
    assert [r["symbol"] for r in out] == ["600000.SH", "688013.SH", "600016.SH", "300164.SZ"]


def test_portfolio_penalty_mode_resort() -> None:
    rows = _rows(
        ("600000.SH", 99), ("600016.SH", 90), ("600030.SH", 85), ("000001.SZ", 80),
    )
    out = apply_portfolio_constraints(
        rows, _MAP, {"max_same_industry": 2, "concentration_penalty": 0.5},
    )
    assert len(out) == 4
    by = {r["symbol"]: r for r in out}
    # 前 2 名原分不变; 第 3 名 85×(1-0.5×1)=42.5; 第 4 名 80×(1-0.5×2)=0
    assert by["600000.SH"]["score"] == 99
    assert abs(by["600030.SH"]["score"] - 42.5) < 1e-9
    assert by["600030.SH"].get("portfolio_constrained") is True
    assert abs(by["000001.SZ"]["score"] - 0.0) < 1e-9
    # 惩罚模式重排序
    assert [r["symbol"] for r in out] == ["600000.SH", "600016.SH", "600030.SH", "000001.SZ"]


def test_portfolio_unknown_bucket_free_pass() -> None:
    rows = _rows(("999999.SH", 99), ("888888.SH", 90), ("777777.SH", 80))
    out = apply_portfolio_constraints(rows, {}, {"max_same_industry": 1})
    # 映射缺失 → 全部 __unknown__ 不受约束
    assert len(out) == 3


def test_portfolio_none_config_passthrough() -> None:
    rows = _rows(("600000.SH", 99), ("600016.SH", 90), ("600030.SH", 85))
    assert apply_portfolio_constraints(rows, _MAP, None) is rows
    assert apply_portfolio_constraints(rows, _MAP, {}) is rows


# ── infer_market_of_rows ─────────────────────────────────

def test_infer_market() -> None:
    assert infer_market_of_rows([{"symbol": "600000.SH"}]) == "cn"
    assert infer_market_of_rows([{"symbol": "00700.HK"}]) == "hk"
    assert infer_market_of_rows([{"symbol": "AAPL.US"}, {"symbol": "MSFT.US"}]) == "us"
    assert infer_market_of_rows([{"symbol": "600000.SH"}, {"symbol": "00700.HK"}, {"symbol": "BRK.A.US"}]) == "cn"
    assert infer_market_of_rows([]) == "cn"


# ── basic_filter 扩展: change_pct / pe / pb ───────────────

def test_basic_filter_change_pct_range() -> None:
    df = pl.DataFrame(
        {"symbol": ["A", "B", "C", "D"], "change_pct": [0.09, 0.02, -0.03, 0.06]}
    )
    out = StrategyEngine._apply_basic_filter(
        df, {"change_pct_min": 0.0, "change_pct_max": 0.08},
    )
    assert set(out["symbol"]) == {"B", "D"}


def test_basic_filter_change_pct_missing_column_rejected() -> None:
    df = pl.DataFrame({"symbol": ["A", "B"], "close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="change_pct"):
        StrategyEngine._apply_basic_filter(df, {"change_pct_min": -1.0})


def test_basic_filter_pe_pb() -> None:
    df = pl.DataFrame(
        {"symbol": ["A", "B", "C"], "pe_ttm": [10.0, 50.0, None], "pb": [0.5, 8.0, 2.0]}
    )
    out = StrategyEngine._apply_basic_filter(
        df, {"pe_ttm_min": 0, "pe_ttm_max": 30, "pb_min": 0, "pb_max": 5.0},
    )
    # C 的 pe null → 条件为 null → 过滤掉; B pe=50 超限
    assert set(out["symbol"]) == {"A"}


def test_basic_filter_pe_pb_missing_columns_rejected() -> None:
    df = pl.DataFrame({"symbol": ["A", "B"], "close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="pb, pe_ttm"):
        StrategyEngine._apply_basic_filter(df, {"pe_ttm_max": 30, "pb_max": 5.0})


# ── industry_heat 注入 ────────────────────────────────────

def _heat_dir(tmp_path: Path) -> Path:
    ext = tmp_path / "ext_data" / "ext_hy_ths"
    ext.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600000.SH", "600016.SH", "600030.SH", "688013.SH"],
            "所属同花顺行业": [
                "银行-国有大型银行", "银行-股份制银行", "银行-城商行", "医药生物-医疗器械",
            ],
        }
    ).write_parquet(ext / "part.parquet")
    return tmp_path


def test_attach_industry_heat_math(tmp_path) -> None:
    data_dir = _heat_dir(tmp_path)
    df = pl.DataFrame(
        {
            "symbol": ["600000.SH", "600016.SH", "600030.SH", "688013.SH"],
            "change_pct": [0.01, 0.02, 0.03, -0.04],
        }
    )
    out = attach_industry_heat(df, data_dir)
    assert "industry_heat" in out.columns
    heat = dict(zip(out["symbol"].to_list(), out["industry_heat"].to_list(), strict=True))
    # 银行 3 成分均值 = 0.02; 医药 1 成分 < 3 → null
    assert abs(heat["600000.SH"] - 0.02) < 1e-9
    assert heat["688013.SH"] is None


def test_attach_industry_heat_idempotent_and_missing(tmp_path) -> None:
    data_dir = _heat_dir(tmp_path)
    df = pl.DataFrame({"symbol": ["600000.SH"], "change_pct": [0.01]})
    once = attach_industry_heat(df, data_dir)
    twice = attach_industry_heat(once, data_dir)
    assert twice is once  # 幂等直通
    # 缺目录/映射时明确物化空值,由调用方报告不可计算。
    assert attach_industry_heat(df, None)["industry_heat"][0] is None
    empty_dir = tmp_path / "nowhere"
    out = attach_industry_heat(df, empty_dir)
    assert "industry_heat" in out.columns
    assert out["industry_heat"][0] is None


def test_scoring_uses_industry_heat() -> None:
    assert scoring_uses_industry_heat({"industry_heat": 0.1}) is True
    assert scoring_uses_industry_heat({"industry_heat": 0}) is False
    assert scoring_uses_industry_heat({"momentum_10d": 0.3}) is False
    assert scoring_uses_industry_heat(None) is False
