"""Fixed ledgers for single-market daily matching and unavailable trading inputs."""
from __future__ import annotations

from datetime import date, timedelta
from math import floor

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.markets.registry import get_profile

PATHS = ("portfolio", "portfolio_legacy", "independent_candidates", "independent_candidates_legacy")


class InstrumentRepo:
    def __init__(self, lots: dict[str, int | float | None]):
        self.lots = lots
        self.reads: list[str] = []

    def get_instruments_asset(self, asset_type: str) -> pl.DataFrame:
        self.reads.append(asset_type)
        return pl.DataFrame({"symbol": list(self.lots), "lot_size": list(self.lots.values()),
                             "currency": ["HKD"] * len(self.lots), "lot_size_status": ["verified_snapshot"] * len(self.lots)})


def sample(symbols: list[str], days: int = 4) -> tuple[pl.DataFrame, pl.Series, pl.Series]:
    rows = [
        {
            "symbol": symbol, "date": date(2024, 1, 2) + timedelta(days=day),
            "open": 13.0 if day < 2 else 17.0, "close": 13.0 if day < 2 else 17.0,
            "high": 13.0 if day < 2 else 17.0, "low": 13.0 if day < 2 else 17.0,
            "volume": 10000.0, "score": float(len(symbols) - rank),
            "signal_limit_up": True, "signal_limit_down": True,
        }
        for rank, symbol in enumerate(symbols) for day in range(days)
    ]
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    entries = panel["date"] == date(2024, 1, 2)
    exits = panel["date"] == date(2024, 1, 3)
    return panel, entries, exits


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("asset_type,symbol,lot", [("us", "BRK.A.US", 1), ("hk", "00700.HK", 20), ("hk", "00005.HK", 50)])
def test_market_quantity_and_costs_reconcile_to_cash(path, asset_type, symbol, lot):
    repo = InstrumentRepo({symbol: lot})
    engine = BacktestEngine(repo)
    cfg = MatcherConfig(
        asset_type=asset_type, matching="open_t+1", initial_capital=1000,
        max_positions=1, commission_pct=0.001, buy_stamp_tax_pct=0.002,
        stamp_tax_pct=0.003, slippage_bps=5,
    )
    result = getattr(engine, f"simulate_{path}")(*sample([symbol]), cfg)
    assert len(result.trades) == 1
    trade = result.trades[0]
    shares = lot if path.startswith("independent") else floor(1000 / (13 * 1.0035) / lot) * lot
    entry_value = shares * 13 * 1.0035
    exit_value = shares * 17 * 0.9955
    assert trade.shares == shares
    assert trade.lots == shares / lot
    assert str(trade.entry_date) == "2024-01-03"
    assert str(trade.exit_date) == "2024-01-04"
    assert trade.entry_value == pytest.approx(round(entry_value, 2))
    assert trade.exit_value == pytest.approx(round(exit_value, 2))
    assert trade.pnl_amount == pytest.approx(round(exit_value - entry_value, 2))
    if path.startswith("portfolio"):
        assert result.equity_curve[1]["cash"] == pytest.approx(round(1000 - entry_value, 2))
        assert result.equity_curve[-1]["cash"] == pytest.approx(round(1000 - entry_value + exit_value, 2))
        assert result.equity_curve[-1]["value"] == result.equity_curve[-1]["cash"]
    assert repo.reads == (["hk"] if asset_type == "hk" else [])


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("lot", [None, 0, -1, 2.5])
def test_missing_or_invalid_hk_lot_never_defaults_to_100(path, lot):
    symbol = "00700.HK"
    repo = InstrumentRepo({symbol: lot})
    result = getattr(BacktestEngine(repo), f"simulate_{path}")(
        *sample([symbol]), MatcherConfig(asset_type="hk", matching="open_t+1"),
    )
    assert result.trades == []
    assert result.stats["execution"]["buy_lot_size_missing"] == 1
    assert any(item["symbol"] == symbol and item["reason"] == "buy_lot_size_missing"
               for item in result.stats["execution_diagnostics"])
    assert repo.reads == ["hk"]


@pytest.mark.parametrize("path", ["portfolio", "portfolio_legacy"])
def test_us_one_share_and_insufficient_cash(path):
    engine = BacktestEngine(None)
    sufficient = getattr(engine, f"simulate_{path}")(
        *sample(["AAPL.US"]), MatcherConfig(asset_type="us", matching="open_t+1", initial_capital=14,
                                            max_positions=1, fees_pct=0, slippage_bps=0),
    )
    insufficient = getattr(engine, f"simulate_{path}")(
        *sample(["AAPL.US"]), MatcherConfig(asset_type="us", matching="open_t+1", initial_capital=12,
                                            max_positions=1, fees_pct=0, slippage_bps=0),
    )
    assert sufficient.trades[0].shares == 1
    assert insufficient.trades == []
    assert insufficient.stats["execution"]["buy_lot_size"] == 1


@pytest.mark.parametrize("path", PATHS)
def test_terminal_entry_without_future_bar_is_not_fabricated(path):
    panel, _, exits = sample(["AAPL.US"])
    entries = panel["date"] == panel["date"].max()
    result = getattr(BacktestEngine(None), f"simulate_{path}")(
        panel, entries, exits, MatcherConfig(asset_type="us", matching="open_t+1"),
    )
    assert result.trades == []


@pytest.mark.parametrize("asset_type", ["hk", "us"])
@pytest.mark.parametrize("kwargs", [{"minute_fill": True}, {"exit_fill": "signal_next_minute"}])
def test_hk_us_matchers_reject_minute_modes(asset_type, kwargs):
    with pytest.raises(ValueError, match="分钟"):
        MatcherConfig(asset_type=asset_type, **kwargs)


def test_market_profiles_separate_sell_permission_from_settlement():
    assert get_profile("CN").same_day_sell_allowed is False
    assert get_profile("HK").same_day_sell_allowed is True
    assert get_profile("US").same_day_sell_allowed is True
    assert get_profile("HK").settlement == "T+2"
    assert get_profile("US").settlement == "T+1"
    assert get_profile("US").lot_size == 1


def test_market_benchmark_never_falls_back_to_cn():
    from app.backtest.strategy import StrategyBacktestService

    class Repo:
        def __init__(self):
            self.calls = []

        def get_index_daily(self, symbol, *args, **kwargs):
            self.calls.append(symbol)
            return pl.DataFrame({"date": [date(2024, 1, 2), date(2024, 1, 3)], "close": [100.0, 120.0]})

        def get_daily(self, symbol, *args, **kwargs):
            self.calls.append(symbol)
            return pl.DataFrame()

    repo = Repo()
    service = StrategyBacktestService(BacktestEngine(repo), None)
    assert service._build_benchmark_curve(date(2024, 1, 2), date(2024, 1, 3), asset_type="us") == []
    assert service._build_benchmark_curve(date(2024, 1, 2), date(2024, 1, 3), asset_type="hk") == []
    assert repo.calls == ["^GSPC.US", "HSI.HK"]


def test_foreign_default_cost_is_explicit_and_worker_roundtrip_preserves_buy_cost():
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import _decode_backtest_config, encode_backtest_config

    config = StrategyBacktestConfig("test", ["AAPL.US"], date(2024, 1, 2), date(2024, 1, 5),
                                    asset_type="us", buy_stamp_tax_pct=0.003)
    restored = _decode_backtest_config(encode_backtest_config(config))
    assert restored.asset_type == "us"
    assert restored.fees_pct == 0
    assert restored.buy_stamp_tax_pct == 0.003
    assert restored.entry_fill == "open_t+1"


def test_request_rejects_mixed_markets_and_minute_modes():
    from pydantic import ValidationError

    from app.api.backtest import StrategyBacktestRequest

    for params in (
        {"symbols": ["AAPL.US", "00700.HK"]}, {"minute_fill": True},
        {"exit_fill": "signal_next_minute"}, {"symbols": ["AAPL"]},
    ):
        with pytest.raises(ValidationError):
            StrategyBacktestRequest(strategy_id="test", asset_type="us", **params)
    request = StrategyBacktestRequest(strategy_id="test", asset_type="us", symbols=["BRK.A.US"])
    assert request.symbols == ["BRK.A.US"]


def test_buy_cost_changes_job_identity():
    from app.api.backtest import _make_job_key

    args = ("s", None, None, None, "open_t+1", None, None, 0.0, 0, 1, 1, 1000, "equal", None, None)
    assert _make_job_key(*args, asset_type="hk") != _make_job_key(*args, asset_type="hk", buy_stamp_tax_pct=0.001)


@pytest.mark.parametrize("path", PATHS)
def test_us_open_entry_can_hit_same_day_protective_stop(path):
    panel, entries, exits = sample(["AAPL.US"])
    panel = panel.with_columns(
        pl.when(pl.col("date") == date(2024, 1, 3)).then(10.0).otherwise(pl.col("low")).alias("low")
    )
    result = getattr(BacktestEngine(None), f"simulate_{path}")(
        panel, entries, exits, MatcherConfig(asset_type="us", matching="open_t+1", stop_loss_pct=0.1),
    )
    assert result.trades[0].entry_date == result.trades[0].exit_date
    assert result.trades[0].exit_price == 11.7
    assert result.trades[0].exit_reason == "stop_loss"


@pytest.mark.parametrize("path", PATHS)
def test_close_entry_does_not_use_pre_entry_daily_high_for_trailing_stop(path):
    panel, entries, _ = sample(["AAPL.US"])
    panel = panel.with_columns(
        pl.when(pl.col("date") == date(2024, 1, 2)).then(100.0).otherwise(pl.col("high")).alias("high")
    )
    exits = pl.Series([False] * panel.height)
    result = getattr(BacktestEngine(None), f"simulate_{path}")(
        panel, entries, exits, MatcherConfig(asset_type="us", matching="close_t", trailing_stop_pct=0.1),
    )
    assert result.trades[0].exit_reason == "end"
