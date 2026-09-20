"""策略详情 _strategy_detail 触发器合并回归测试.

回归点 (bug): entry_signals / exit_signals 原先直接返回策略源文件默认值,
没有合并 overrides, 导致用户在卡片弹窗里新选的买卖触发器 tag 保存后回显丢失.

契约:
  - 无 overrides  → 返回策略默认 entry_signals / exit_signals
  - 有 overrides  → 返回 overrides 里保存的 list (即使为空, 也代表用户主动清空)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import strategy as strategy_api
from app.api.strategy import _strategy_detail
from app.strategy import config as strategy_config
from app.strategy.engine import DEFAULT_BASIC_FILTER, StrategyDef, StrategyEngine


def _make_strategy(
    entry_signals: list[str],
    exit_signals: list[str],
) -> StrategyDef:
    """构造最小可用的 StrategyDef (只填必填字段)."""
    return StrategyDef(
        meta={"id": "test_strat", "name": "测试策略"},
        basic_filter={"enabled": True},
        entry_signals=list(entry_signals),
        exit_signals=list(exit_signals),
        stop_loss=-0.08,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=10,
        filter_fn=None,
        filter_history_fn=None,
        lookback_days=60,
        source="builtin",
    )


def test_no_overrides_returns_default_signals():
    """无 overrides 时返回策略源文件默认的触发器."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout", "signal_n_day_high"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides=None)
    assert "alerts" not in detail
    assert detail["entry_signals"] == ["signal_ma20_breakout", "signal_n_day_high"]
    assert detail["exit_signals"] == ["signal_ma20_breakdown"]


def test_overrides_signals_are_reflected():
    """核心回归: 用户保存了更多触发器, 详情必须回显保存值而非默认值."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    overrides = {
        "entry_signals": ["signal_ma20_breakout", "signal_macd_golden", "signal_n_day_high"],
        "exit_signals": ["signal_ma20_breakdown", "signal_macd_dead"],
    }
    detail = _strategy_detail(s, overrides=overrides)
    assert detail["entry_signals"] == overrides["entry_signals"]
    assert detail["exit_signals"] == overrides["exit_signals"]


def test_empty_override_signals_reflected():
    """用户主动清空所有触发器 → 保存空 list, 详情应回显空 (而非回退默认)."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides={"entry_signals": [], "exit_signals": []})
    assert detail["entry_signals"] == []
    assert detail["exit_signals"] == []


def test_partial_override_keeps_other_default():
    """只覆盖 entry_signals, exit_signals 保持默认 (key 不在 overrides 里)."""
    s = _make_strategy(
        entry_signals=["signal_ma20_breakout"],
        exit_signals=["signal_ma20_breakdown"],
    )
    detail = _strategy_detail(s, overrides={"entry_signals": ["signal_x"]})
    assert detail["entry_signals"] == ["signal_x"]
    assert detail["exit_signals"] == ["signal_ma20_breakdown"]


@pytest.mark.parametrize("asset_type", ["stock", "etf", "hk", "us"])
def test_list_and_detail_resolve_the_same_market_defaults(tmp_path, asset_type):
    strategy = _make_strategy([], [])
    strategy.meta["asset_types"] = ["stock", "etf", "hk", "us"]
    strategy.basic_filter = {
        **DEFAULT_BASIC_FILTER,
        "price_min": 7,
        "market_defaults": {"hk": {"amount_min": 1e6}, "us": {"amount_min": 2e6}},
    }
    strategy.basic_filter_explicit_keys = frozenset({"price_min", "market_defaults"})
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["test_strat"] = strategy
    strategy_config.save_override(tmp_path, "test_strat", {
        "basic_filter": {"amount_min": 9e6},
    })
    app = FastAPI()
    app.include_router(strategy_api.router)
    app.state.strategy_engine = engine
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    client = TestClient(app)

    listed = client.get("/api/strategies", params={"asset_type": asset_type})
    detailed = client.get("/api/strategies/test_strat", params={"asset_type": asset_type})

    assert listed.status_code == detailed.status_code == 200
    basic_filter = detailed.json()["basic_filter"]
    assert listed.json()["strategies"][0]["basic_filter"] == basic_filter
    assert basic_filter["price_min"] == 7
    assert basic_filter["amount_min"] == 9e6
    assert "market_defaults" not in basic_filter
    if asset_type in {"hk", "us"}:
        assert basic_filter["boards"] == []
        assert basic_filter["exclude_st"] is False
        assert basic_filter["market_cap_min"] is None
    else:
        assert basic_filter["boards"] == DEFAULT_BASIC_FILTER["boards"]
        assert basic_filter["market_cap_min"] == DEFAULT_BASIC_FILTER["market_cap_min"]
    # Old callers retain the original detail contract; resolving a market is pure.
    legacy = client.get("/api/strategies/test_strat").json()["basic_filter"]
    assert legacy["market_defaults"] == strategy.basic_filter["market_defaults"]
    assert legacy["market_cap_min"] == DEFAULT_BASIC_FILTER["market_cap_min"]


def test_market_detail_preserves_explicit_strategy_and_user_bounds():
    strategy = _make_strategy([], [])
    strategy.basic_filter = {**DEFAULT_BASIC_FILTER, "market_cap_min": 5e8}
    strategy.basic_filter_explicit_keys = frozenset({"market_cap_min"})
    assert _strategy_detail(strategy, asset_type="hk")["basic_filter"]["market_cap_min"] == 5e8
    detail = _strategy_detail(strategy, {"basic_filter": {"market_cap_min": 8e8}}, asset_type="hk")
    assert detail["basic_filter"]["market_cap_min"] == 8e8
