"""港股数据补全的独立 QA: 金融边界与发布失败, 不访问网络或正式数据。"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import suppress
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.fundamentals import (
    attach_hk_financial_fields,
    build_fundamental_matrices,
    load_fundamental_snapshot,
)
from app.backtest.matrix import build_market_data_matrix, make_signal_matrix
from app.data_providers.hk_daily_provider import HKDailyProvider, parse_sina_factors
from app.data_providers.hk_financial_provider import parse_hk_announcement
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    EnrichedPublication,
    get_enriched_generation,
)
from app.services import financial_sync, hk_data_adapter
from app.strategy.engine import (
    CompositeChild,
    CompositeSpec,
    StrategyDataContext,
    StrategyDef,
    StrategyEngine,
)
from app.tickflow.market_daily import read_market_daily_symbol
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "00700.HK"
OBSERVED_AT = "2026-09-12T16:00:00+00:00"


def _financial_row(
    period: str,
    announced: str,
    *,
    revision: str = "original",
    source: str = "qa_original_announcement",
    **values: float | None,
) -> dict:
    return {
        "symbol": SYMBOL,
        "period_end": period,
        "announce_date": announced,
        "revision_id": revision,
        "source": source,
        "source_url": "https://example.test/original-announcement",
        "publication_source": "https://example.test/announcement-index",
        "report_currency": "HKD",
        "observed_at": OBSERVED_AT,
        "field_provenance": json.dumps({
            name: {
                "source": source,
                "source_url": "https://example.test/original-announcement",
                "announce_date": announced,
                "unit": "currency_per_share" if name in {"bps", "eps_ttm"} else "percent_number",
                "currency": "HKD",
                "basis": "as_reported",
                "per_share_basis": {
                    "verified": True,
                    "valid_from": "2025-01-01",
                    "valid_to": "2025-12-31",
                },
            }
            for name, value in values.items() if value is not None
        }),
        **values,
    }


def _write_history(root: Path, rows: list[dict]) -> Path:
    path = root / "financials" / "metrics" / "hk.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, infer_schema_length=None).write_parquet(path)
    return path


def _panel(days: list[date]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [SYMBOL] * len(days),
        "date": days,
        "open": [10.0] * len(days),
        "high": [11.0] * len(days),
        "low": [9.0] * len(days),
        "close": [10.0] * len(days),
        "raw_close": [20.0] * len(days),
        "volume": [1000.0] * len(days),
        "currency": ["HKD"] * len(days),
        "raw_price_verified": [True] * len(days),
    })


def test_latest_report_missing_field_does_not_borrow_an_older_period(tmp_path):
    """新报告缺毛利率不能把前一期毛利率当成本期值。"""
    _write_history(tmp_path, [
        _financial_row("2024-12-31", "2025-03-19", gross_margin=20.0),
        _financial_row("2025-06-30", "2025-08-13", net_margin=12.0),
    ])
    days = [date(2025, 8, 13), date(2025, 8, 14)]
    names = ["gross_margin_latest", "net_margin_latest"]
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=names)
    values = attach_hk_financial_fields(_panel(days), snapshot, names)
    assert values["gross_margin_latest"].to_list() == [20.0, None]
    assert values["net_margin_latest"].to_list() == [None, 12.0]
    matrices = build_fundamental_matrices(build_market_data_matrix(_panel(days)), snapshot, names)
    for name in names:
        np.testing.assert_allclose(matrices[name][:, 0], values[name].to_numpy(), equal_nan=True)


def test_same_period_late_fallback_field_keeps_its_own_publication_date(tmp_path):
    """允许同一期补字段; 晚公开的备用字段不能继承较早主行日期。"""
    _write_history(tmp_path, [
        _financial_row("2024-12-31", "2025-03-19", gross_margin=20.0),
        _financial_row("2024-12-31", "2025-03-21", source="qa_verified_fallback", net_margin=12.0),
    ])
    names = ["gross_margin_latest", "net_margin_latest"]
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=names)
    days = [date(2025, 3, 19), date(2025, 3, 20), date(2025, 3, 21), date(2025, 3, 24)]
    values = attach_hk_financial_fields(_panel(days), snapshot, names)
    assert values["gross_margin_latest"].to_list() == [None, 20.0, 20.0, 20.0]
    assert values["net_margin_latest"].to_list() == [None, None, None, 12.0]


def _announcement(content: str) -> tuple[dict, dict]:
    title = "截至二零二四年十二月三十一日止年度全年业绩公布"
    art_code = "AN202503191644721554"
    listing = {
        "art_code": art_code,
        "notice_date": "2025-03-19 00:00:00",
        "title": title,
        "codes": [{"stock_code": "00700", "market_code": "116"}],
    }
    detail = {
        "art_code": art_code,
        "notice_date": listing["notice_date"],
        "notice_title": title,
        "notice_content": content,
    }
    return listing, detail


@pytest.mark.parametrize("variant", ["prior_year_first", "note_column"])
def test_announcement_table_never_guesses_column_order_or_note_numbers(variant):
    """无法证明表头映射时可拒收; 禁止把上期或附注序号作本期金额。"""
    if variant == "prior_year_first":
        content = (
            "截至十二月三十一日止年度\n二零二三年 二零二四年\n(港幣百萬元)\n"
            "收入  80  100\n毛利  16  30\n年度盈利  8  15\n"
            "本公司權益持有人應佔盈利  6  12\n"
        )
    else:
        content = (
            "截至十二月三十一日止年度\n附註 二零二四年 二零二三年\n(港幣百萬元)\n"
            "收入  6  100  80\n毛利  7  30  16\n年度盈利  8  15  8\n"
            "本公司權益持有人應佔盈利  9  12  6\n"
        )
    listing, detail = _announcement(content)
    row = parse_hk_announcement(SYMBOL, listing, detail, observed_at=OBSERVED_AT)
    expected = {"gross_margin": 30.0, "net_margin": 15.0, "revenue_yoy": 25.0, "net_income_yoy": 100.0}
    if row is not None:
        for field, value in expected.items():
            assert row[field] is None or row[field] == pytest.approx(value), (variant, field, row[field])


@pytest.mark.parametrize("untrusted", [None, "wrong_currency", "unknown_raw", "expired_share_basis", "negative_bps"])
def test_pb_uses_raw_price_only_with_verified_currency_and_share_basis(tmp_path, untrusted):
    row = _financial_row("2024-12-31", "2025-03-19", bps=-4.0 if untrusted == "negative_bps" else 4.0)
    if untrusted == "expired_share_basis":
        provenance = json.loads(row["field_provenance"])
        provenance["bps"]["per_share_basis"]["valid_to"] = "2025-03-19"
        row["field_provenance"] = json.dumps(provenance)
    _write_history(tmp_path, [row])
    data = _panel([date(2025, 3, 19), date(2025, 3, 20)])
    if untrusted == "wrong_currency":
        data = data.with_columns(pl.lit("CNY").alias("currency"))
    elif untrusted == "unknown_raw":
        data = data.with_columns(pl.lit(False).alias("raw_price_verified"))
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["pb_latest"])
    values = attach_hk_financial_fields(data, snapshot, ["pb_latest"])
    assert values["pb_latest"].to_list() == [None, 5.0 if untrusted is None else None]


def test_verified_pb_has_equal_panel_and_matrix_values(tmp_path):
    _write_history(tmp_path, [_financial_row("2024-12-31", "2025-03-19", bps=4.0)])
    names = ["pb_latest"]
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=names)
    data = _panel([date(2025, 3, 19), date(2025, 3, 20)])
    values = attach_hk_financial_fields(data, snapshot, names)
    market = build_market_data_matrix(data, field_columns={"raw_close", "currency", "raw_price_verified"})
    matrix = build_fundamental_matrices(market, snapshot, names)["pb_latest"]
    np.testing.assert_allclose(matrix[:, 0], values["pb_latest"].to_numpy(), equal_nan=True)


class _GrossMarginEntries:
    def required_fields(self):
        return frozenset({"gross_margin_latest"})

    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        return make_signal_matrix(
            market.shape, entry=(market.fields["gross_margin_latest"] >= 22).astype(np.uint8),
        )


@pytest.mark.parametrize("as_of,expected", [
    (date(2025, 3, 19), None),
    (date(2025, 3, 20), "00941.HK"),
    (date(2025, 8, 13), "00941.HK"),
    (date(2025, 8, 14), SYMBOL),
])
def test_historical_financial_candidates_match_all_strategy_paths(tmp_path, as_of, expected):
    """同一个财务条件真实进入四条策略路径, 公告边界和缺值结果必须一致。"""
    _write_history(tmp_path, [
        _financial_row("2024-12-31", "2025-03-19", gross_margin=20.0),
        _financial_row("2025-06-30", "2025-08-13", gross_margin=30.0),
        {**_financial_row("2024-12-31", "2025-03-19", gross_margin=25.0), "symbol": "00941.HK"},
        {**_financial_row("2025-06-30", "2025-08-13", net_margin=12.0), "symbol": "00941.HK"},
    ])
    days = [date(2025, 3, 19), date(2025, 3, 20), date(2025, 8, 13), date(2025, 8, 14)]
    first = _panel([day for day in days if day <= as_of])
    history = pl.concat([first, first.with_columns(pl.lit("00941.HK").alias("symbol"))]).sort("date", "symbol")
    context = StrategyDataContext(
        asset_type="hk", timeframe="1d", as_of=as_of, is_historical=True,
        current=history.filter(pl.col("date") == as_of), history=history,
    )
    engine = StrategyEngine(data_dir=tmp_path)
    ordinary = StrategyDef(
        meta={"id": "qa_financial", "name": "历史毛利率", "asset_types": ["hk"],
              "timeframes": ["1d"], "scoring": {"gross_margin_latest": 1.0}, "order_by": "score"},
        basic_filter={"enabled": False}, entry_signals=[], exit_signals=[],
        stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=lambda frame, params: pl.col("gross_margin_latest") >= 22,
        filter_history_fn=None, required_features=frozenset({"gross_margin_latest"}),
        lookback_days=1, source="custom",
    )
    engine._strategies["qa_financial"] = ordinary
    engine._strategies["qa_financial_matrix"] = replace(
        ordinary, meta={**ordinary.meta, "id": "qa_financial_matrix"}, filter_fn=None,
        execution_backend="matrix_native", matrix_strategy=_GrossMarginEntries(),
    )
    engine._strategies["qa_financial_blend"] = replace(
        ordinary, meta={**ordinary.meta, "id": "qa_financial_blend"}, filter_fn=None,
        execution_backend="composite", composite=CompositeSpec((
            CompositeChild("qa_financial", 1.0), CompositeChild("qa_financial_matrix", 1.0),
        )),
    )
    shared = build_market_data_matrix(history)
    results = []
    for strategy_id, prepared in (
        ("qa_financial", context),
        ("qa_financial_matrix", context),
        ("qa_financial_matrix", replace(context, market=shared)),
        ("qa_financial_blend", context),
    ):
        if expected is None:
            with pytest.raises(ValueError, match="gross_margin_latest") as error:
                engine.run(strategy_id, prepared)
            assert as_of.isoformat() in str(error.value)
        else:
            result = engine.run(strategy_id, prepared)
            assert [row["symbol"] for row in result.rows] == [expected]
            results.append(result)
    if results:
        assert results[0].scores == pytest.approx(results[1].scores)
        assert results[0].scores == pytest.approx(results[2].scores)
    assert "gross_margin_latest" not in shared.fields
    assert "gross_margin_latest" not in history.columns


def test_financial_source_outage_keeps_local_history_and_generation(tmp_path, monkeypatch):
    rows = [_financial_row("2024-12-31", "2025-03-19", gross_margin=20.0, net_margin=10.0,
                           revenue_yoy=8.0, net_income_yoy=9.0)]
    path = _write_history(tmp_path, rows)
    before = path.read_bytes()
    generation = get_enriched_generation(tmp_path, "hk")

    class OfflineProvider:
        name = "qa_offline"

        def get_financials(self, table, symbols, latest_only=False):
            raise TimeoutError("injected network outage")

    monkeypatch.setattr(financial_sync, "_get_hk_primary_provider", lambda: OfflineProvider())
    monkeypatch.setattr(financial_sync, "_get_hk_fallback_provider", lambda: OfflineProvider())
    result = financial_sync.sync_hk_financial_history(tmp_path, symbols=[SYMBOL])
    assert result["items"][0]["status"] == "partial"
    assert result["items"][0]["fallback_used"] is True
    assert path.read_bytes() == before
    assert get_enriched_generation(tmp_path, "hk") == generation
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["gross_margin_latest"])
    assert attach_hk_financial_fields(_panel([date(2025, 3, 20)]), snapshot, ["gross_margin_latest"])["gross_margin_latest"].item() == 20.0


@pytest.fixture
def financial_publication_input(tmp_path, monkeypatch):
    original = _financial_row("2024-12-31", "2025-03-19", gross_margin=20.0)
    path = _write_history(tmp_path, [original])
    generation = get_enriched_generation(tmp_path, "hk")

    class NewReportProvider:
        name = "qa_new_original_report"

        def get_financials(self, table, symbols, latest_only=False):
            return pl.DataFrame([_financial_row(
                "2025-06-30", "2025-08-13", gross_margin=30.0, net_margin=10.0,
                revenue_yoy=8.0, net_income_yoy=9.0,
            )])

    monkeypatch.setattr(financial_sync, "_get_hk_primary_provider", lambda: None)
    monkeypatch.setattr(financial_sync, "_get_hk_fallback_provider", NewReportProvider)
    return SimpleNamespace(path=path, original=original, generation=generation)


def test_financial_commit_failure_restores_old_file_and_generation(tmp_path, monkeypatch, financial_publication_input):
    """财务单文件也不能在版本提交失败后留下已替换数据和 publishing 状态。"""
    path = financial_publication_input.path
    before = path.read_bytes(), path.stat().st_mtime_ns

    def fail_commit(self):
        raise OSError("qa financial commit failure")

    monkeypatch.setattr(EnrichedPublication, "commit", fail_commit)
    with pytest.raises(OSError, match="qa financial commit failure"):
        financial_sync.sync_hk_financial_history(tmp_path, symbols=[SYMBOL])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert get_enriched_generation(tmp_path, "hk") == financial_publication_input.generation


def test_financial_staging_cannot_overwrite_an_intervening_committed_report(tmp_path, monkeypatch, financial_publication_input):
    """模拟另一个进程在暂存期间完成发布; 旧任务必须保留其新版本。"""
    latest = _financial_row("2025-09-30", "2025-11-13", gross_margin=40.0)
    original_write = pl.DataFrame.write_parquet
    intervened = False

    def competing_write(self, file, *args, **kwargs):
        nonlocal intervened
        result = original_write(self, file, *args, **kwargs)
        if not intervened and "hk.parquet" in str(file):
            intervened = True
            other = EnrichedPublication(tmp_path, "hk")
            other.write_parquet(
                pl.DataFrame([financial_publication_input.original, latest]),
                financial_publication_input.path,
            )
            other.commit()
        return result

    monkeypatch.setattr(pl.DataFrame, "write_parquet", competing_write)
    # Both rejecting a stale preparation and re-reading/re-merging it are safe.
    with suppress(ValueError, EnrichedGenerationUnavailableError):
        financial_sync.sync_hk_financial_history(tmp_path, symbols=[SYMBOL])
    assert intervened
    history = financial_sync.get_financial_df(tmp_path, "metrics", market="HK")
    assert date(2024, 12, 31) in history["period_end"].to_list()
    assert history.filter(pl.col("period_end") == date(2025, 9, 30))["gross_margin"].to_list() == [40.0]
    assert get_enriched_generation(tmp_path, "hk") != financial_publication_input.generation


def _raw_bars(days: list[date], closes: list[float] | None = None) -> pl.DataFrame:
    values = closes or [100.0 + index for index in range(len(days))]
    return pl.DataFrame({
        "symbol": [SYMBOL] * len(days), "date": days,
        "open": values, "high": values, "low": values, "close": values,
        "volume": [1000.0 + index for index in range(len(days))],
        "amount": pl.Series([None] * len(days), dtype=pl.Float64),
        "currency": ["HKD"] * len(days), "source": ["sina_hk_daily"] * len(days),
        "volume_unit": ["share"] * len(days), "price_adjustment": ["unadjusted"] * len(days),
        "price_schema_version": [1] * len(days), "raw_price_verified": [True] * len(days),
    })


def _factors(end: date, events: list[dict] | None = None) -> pl.DataFrame:
    payload = {"data": events or [{"d": "1900-01-01", "f": "1"}]}
    return parse_sina_factors("var hk00700qfq=" + json.dumps(payload), SYMBOL, end, OBSERVED_AT)


def _published_bytes(root: Path) -> dict[str, bytes]:
    datasets = ("kline_daily", "adj_factor_hk", "kline_hk_us_enriched")
    return {
        dataset: (root / dataset / f"symbol={SYMBOL}" / "part.parquet").read_bytes()
        for dataset in datasets
    }


def test_cancelled_daily_publish_keeps_all_previous_files_and_generation(tmp_path):
    days = [date(2026, 9, 10), date(2026, 9, 11)]
    raw = _raw_bars(days)
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, raw, factors=_factors(days[-1]))
    before = _published_bytes(tmp_path)
    generation = get_enriched_generation(tmp_path, "hk")

    def cancelled():
        raise RuntimeError("qa task cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, SYMBOL, _raw_bars(days, [102.0, 103.0]),
            factors=_factors(days[-1]), before_publish=cancelled,
        )
    assert _published_bytes(tmp_path) == before
    assert get_enriched_generation(tmp_path, "hk") == generation


def test_multifile_write_failure_does_not_replace_a_previous_good_snapshot(tmp_path, monkeypatch):
    days = [date(2026, 9, 10), date(2026, 9, 11)]
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days), factors=_factors(days[-1]))
    before = _published_bytes(tmp_path)
    original = pl.DataFrame.write_parquet

    def fail_factors(self, file, *args, **kwargs):
        if "adj_factor_hk" in str(file):
            raise OSError("qa factor write failure")
        return original(self, file, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", fail_factors)
    changed_factors = _factors(days[-1], [{"d": "1900-01-01", "f": "0.99"}, {"d": "2026-09-11", "f": "1"}])
    with pytest.raises(OSError, match="qa factor write failure"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, SYMBOL, _raw_bars(days, [102.0, 103.0]), factors=changed_factors,
        )
    assert _published_bytes(tmp_path) == before


@pytest.mark.parametrize("failure_point", ["factor_replace", "enriched_replace", "audit_replace", "audit_prepare", "commit"])
def test_failed_atomic_replacement_restores_snapshot_bytes_mtime_and_generation(tmp_path, monkeypatch, failure_point):
    """替换后段或提交失败时, 已替换的文件和版本也须恢复。"""
    days = [date(2026, 9, 10), date(2026, 9, 11)]
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days), factors=_factors(days[-1]))
    targets = {
        "factor_replace": tmp_path / "adj_factor_hk" / f"symbol={SYMBOL}" / "part.parquet",
        "enriched_replace": tmp_path / "kline_hk_us_enriched" / f"symbol={SYMBOL}" / "part.parquet",
        "audit_replace": tmp_path / "hk_data_audit" / "prices" / f"{SYMBOL}.json",
    }
    tracked = [tmp_path / "kline_daily" / f"symbol={SYMBOL}" / "part.parquet", *targets.values()]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked}
    generation = get_enriched_generation(tmp_path, "hk")
    original_replace = os.replace
    original_commit = EnrichedPublication.commit
    original_write_text = Path.write_text
    failed_once = False

    def fail_replace(source, destination, *args, **kwargs):
        nonlocal failed_once
        if not failed_once and failure_point in targets and Path(destination) == targets[failure_point]:
            failed_once = True
            raise OSError("qa replacement failure")
        return original_replace(source, destination, *args, **kwargs)

    def fail_commit(self):
        nonlocal failed_once
        if failure_point == "commit" and not failed_once:
            failed_once = True
            raise OSError("qa replacement failure")
        return original_commit(self)

    def fail_audit_prepare(self, data, *args, **kwargs):
        nonlocal failed_once
        if (failure_point == "audit_prepare" and not failed_once
                and self.parent == targets["audit_replace"].parent):
            failed_once = True
            raise OSError("qa replacement failure")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(EnrichedPublication, "commit", fail_commit)
    monkeypatch.setattr(Path, "write_text", fail_audit_prepare)
    changed_factors = _factors(days[-1], [{"d": "1900-01-01", "f": "0.99"}, {"d": "2026-09-11", "f": "1"}])
    with pytest.raises(OSError, match="qa replacement failure"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, SYMBOL, _raw_bars(days, [102.0, 103.0]), factors=changed_factors,
        )
    assert failed_once
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tracked} == before
    assert get_enriched_generation(tmp_path, "hk") == generation


def test_stale_prepared_daily_snapshot_cannot_overwrite_a_newer_publication(tmp_path):
    """准备阶段发生更新时, 旧任务须重取版本, 不能丢掉新交易日。"""
    first_day, second_day, third_day = date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars([first_day]), factors=_factors(first_day))
    newer_published = False
    newer_snapshot = None
    newer_generation = None

    def publish_newer():
        nonlocal newer_published, newer_snapshot, newer_generation
        if newer_published:
            return
        newer_published = True
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars([third_day]), factors=_factors(third_day))
        newer_snapshot = _published_bytes(tmp_path)
        newer_generation = get_enriched_generation(tmp_path, "hk")

    with pytest.raises(ValueError, match=r"版本|变化|重试"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, SYMBOL, _raw_bars([second_day]), factors=_factors(third_day), before_publish=publish_newer,
        )
    assert newer_published
    assert _published_bytes(tmp_path) == newer_snapshot
    assert get_enriched_generation(tmp_path, "hk") == newer_generation
    assert read_market_daily_symbol(tmp_path, SYMBOL)["date"].to_list() == [first_day, third_day]


def test_stale_factor_cache_never_extends_adjusted_history_to_a_new_day(tmp_path):
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days), factors=_factors(days[-1]))
    enriched = tmp_path / "kline_hk_us_enriched" / f"symbol={SYMBOL}" / "part.parquet"
    before = enriched.read_bytes()
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars([date(2026, 9, 11)]))
    assert result["status"] == "partial"
    assert result["raw_updated"] is True
    assert result["enriched_updated"] is False
    assert enriched.read_bytes() == before
    assert pl.read_parquet(enriched)["date"].max() == date(2026, 9, 10)


@pytest.mark.parametrize("corroboration", ["primary", "fallback", "mixed_row", "offline"])
def test_third_source_must_confirm_one_complete_ohlcv_row_before_publication(tmp_path, monkeypatch, corroboration):
    """独立核验不能拼凑不同来源字段; 未唯一确认时完整保留旧快照。"""
    from app.data_providers import hk_daily_provider, registry
    from app.services import kline_sync

    days = [date(2026, 9, 10), date(2026, 9, 11)]
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days, [90.0, 91.0]), factors=_factors(days[-1]))
    before = _published_bytes(tmp_path)
    verification_calls = []
    monkeypatch.setattr(hk_daily_provider, "_decode_sina_rows", lambda encoded: [
        {"date": "2026-09-10", "open": 100.0, "close": 101.0, "high": 103.0, "low": 99.0, "volume": 1000.0},
        {"date": "2026-09-11", "open": 103.0, "close": 103.0, "high": 104.0, "low": 100.0, "volume": 2000.0},
    ])

    def response(request):
        if request.url.path.endswith("klc2_kl.js"):
            return httpx.Response(200, text='var KLC_KL_hk00700="qa encoded fixture";')
        if request.url.path.endswith("qfq.js"):
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        if request.url.path.endswith("/fqkline/get"):
            symbol = "hkHSI" if "hkHSI" in request.url.params["param"] else "hk00700"
            quote = [""] * 80
            quote[2], quote[75] = symbol[2:], "HKD"
            return httpx.Response(200, json={"code": 0, "data": {symbol: {
                "day": [["2026-09-10", "102", "101", "103", "99", "1000"],
                        ["2026-09-11", "103", "103", "104", "100", "2000"]],
                "qt": {symbol: quote},
            }}})
        assert request.url.path == "/api/qt/stock/kline/get"
        assert request.url.params["fqt"] == "0"
        verification_calls.append(str(request.url))
        if corroboration == "offline":
            return httpx.Response(503)
        open_price = "100" if corroboration == "primary" else "102"
        volume = "1002" if corroboration == "mixed_row" else "1000"
        return httpx.Response(200, json={"rc": 0, "data": {
            "code": "00700", "market": 116,
            "klines": [f"2026-09-10,{open_price},101,103,99,{volume},100000,4,1,1,0.1"],
        }})

    provider = HKDailyProvider(transport=httpx.MockTransport(response), timeout=0.01)
    monkeypatch.setattr(registry, "get_default_provider", lambda market, **kwargs: provider)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    successful, failed, items = [], [], []
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    kline_sync.sync_and_persist_daily_batch(
        [SYMBOL], repo, None, asset_type="hk", start_date=datetime(2026, 9, 10),
        end_date=datetime(2026, 9, 11), successful_out=successful, failed_out=failed,
        items_out=items, request_timeout_seconds=None,
    )
    assert verification_calls
    if corroboration in {"primary", "fallback"}:
        assert successful == [SYMBOL] and failed == []
        source = "sina_hk_daily" if corroboration == "primary" else "tencent_hk_daily"
        raw = read_market_daily_symbol(tmp_path, SYMBOL)
        first = raw.filter(pl.col("date") == days[0]).row(0, named=True)
        assert first["open"] == (100.0 if corroboration == "primary" else 102.0)
        assert first["source"] == source
        assert first["verification_source"] == "eastmoney_hk_daily_check"
        assert items[0]["source_conflicts"][0]["selected_source"] == source
    else:
        assert successful == [] and failed == [SYMBOL]
        assert items[0]["reason_code"] == "raw_source_conflict"
        assert _published_bytes(tmp_path) == before


def _verification_url(**overrides: str) -> str:
    params = {
        "secid": "116.00700", "klt": "101", "fqt": "0",
        "beg": "20260910", "end": "20260911", "lmt": "1000",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        **overrides,
    }
    return str(httpx.URL("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params))


def _verification_body(
    *, open_price: float = 100.0, volume: float = 1000.0,
    trading_day: str = "2026-09-10", code: str = "00700", market: int = 116,
) -> bytes:
    return json.dumps({"rc": 0, "data": {
        "code": code, "market": market, "name": "腾讯控股",
        "klines": [f"{trading_day},{open_price},101,104,99,{volume},100000,4,1,1,0.1"],
    }}, ensure_ascii=False).encode("utf-8")


def test_verification_archive_import_preserves_original_bytes_and_is_idempotent(tmp_path):
    """原始包 hash 包含 BOM 和换行; 重复导入不改观察时间或行情版本。"""
    raw = b"\xef\xbb\xbf" + _verification_body() + b"\n"
    generation = get_enriched_generation(tmp_path, "hk")
    arguments = {"raw_response": raw, "source_url": _verification_url(), "observed_at": OBSERVED_AT}
    result = hk_data_adapter.import_hk_raw_verification_archive(tmp_path, "700", **arguments)
    path = Path(result["archive_path"])
    before = path.read_bytes(), path.stat().st_mtime_ns
    repeated = hk_data_adapter.import_hk_raw_verification_archive(tmp_path, SYMBOL, **arguments)
    assert repeated == result
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert result["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["symbol"] == SYMBOL
    assert result["observed_at"] == OBSERVED_AT
    assert result["source"] == "eastmoney_hk_daily_check"
    archives = hk_data_adapter.load_hk_raw_verification_archives(tmp_path, ["700"])
    assert len(archives) == 1
    assert archives[0]["raw_response"].encode("utf-8") == raw
    assert archives[0]["source_url"] == _verification_url()
    assert get_enriched_generation(tmp_path, "hk") == generation
    assert not (tmp_path / "kline_daily").exists()


@pytest.mark.parametrize("invalid", [
    "adjusted_url", "wrong_host", "wrong_url_symbol", "ambiguous_fqt",
    "wrong_frequency", "reordered_fields", "wrong_body_symbol", "wrong_market",
    "missing_timezone", "invalid_time", "future_observation", "observed_before_rows",
])
def test_verification_archive_import_rejects_unproven_identity_basis_and_time(tmp_path, invalid):
    arguments = {"raw_response": _verification_body(), "source_url": _verification_url(), "observed_at": OBSERVED_AT}
    if invalid == "adjusted_url":
        arguments["source_url"] = _verification_url(fqt="1")
    elif invalid == "wrong_host":
        arguments["source_url"] = _verification_url().replace("push2his.eastmoney.com", "example.test")
    elif invalid == "wrong_url_symbol":
        arguments["source_url"] = _verification_url(secid="116.00941")
    elif invalid == "ambiguous_fqt":
        arguments["source_url"] += "&fqt=1"
    elif invalid == "wrong_frequency":
        arguments["source_url"] = _verification_url(klt="102")
    elif invalid == "reordered_fields":
        arguments["source_url"] = _verification_url(fields2="f51,f53,f52,f54,f55,f56")
    elif invalid == "wrong_body_symbol":
        arguments["raw_response"] = _verification_body(code="00941")
    elif invalid == "wrong_market":
        arguments["raw_response"] = _verification_body(market=1)
    elif invalid == "missing_timezone":
        arguments["observed_at"] = "2026-09-12T16:00:00"
    elif invalid == "invalid_time":
        arguments["observed_at"] = "unknown"
    elif invalid == "future_observation":
        arguments["observed_at"] = "2099-01-01T00:00:00+00:00"
    else:
        arguments["observed_at"] = "2026-09-09T00:00:00+00:00"
    with pytest.raises(ValueError):
        hk_data_adapter.import_hk_raw_verification_archive(tmp_path, SYMBOL, **arguments)
    assert not (tmp_path / "hk_data_audit" / "raw_verification").exists()


@pytest.fixture
def archived_conflict_sync(tmp_path, monkeypatch):
    """只替换两个源的下载, 保留档案解析、冲突选择与整条发布链。"""
    from app.data_providers import registry
    from app.services import kline_sync

    days = [date(2026, 9, 10), date(2026, 9, 11)]
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days, [90.0, 91.0]), factors=_factors(days[-1]))
    current = {"primary_open": 100.0, "fallback_open": 102.0}
    verification_calls = []

    def bars(source, open_price):
        return _raw_bars(days, [101.0, 103.0]).with_columns(
            pl.Series("open", [open_price, 103.0]), pl.Series("high", [104.0, 104.0]),
            pl.Series("low", [99.0, 100.0]), pl.Series("volume", [1000.0, 2000.0]),
            pl.lit(source).alias("source"),
        )

    def response(request):
        if request.url.path.endswith("qfq.js"):
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        assert request.url.path == "/api/qt/stock/kline/get"
        assert request.url.params["fqt"] == "0"
        verification_calls.append(str(request.url))
        return httpx.Response(503)

    provider = HKDailyProvider(transport=httpx.MockTransport(response), timeout=0.01)
    monkeypatch.setattr(provider, "_sina", lambda *args: bars("sina_hk_daily", current["primary_open"]).with_columns(
        pl.lit(days[0], dtype=pl.Date).alias("source_first_date"),
    ))
    monkeypatch.setattr(provider, "_tencent", lambda *args: (bars("tencent_hk_daily", current["fallback_open"]), "HKD"))
    monkeypatch.setattr(provider, "_calendar", lambda *args: set(days))
    monkeypatch.setattr(registry, "get_default_provider", lambda market, **kwargs: provider)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")

    def run():
        successful, failed, items = [], [], []
        kline_sync.sync_and_persist_daily_batch(
            [SYMBOL], SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), None,
            asset_type="hk", start_date=datetime(2026, 9, 10), end_date=datetime(2026, 9, 11),
            successful_out=successful, failed_out=failed, items_out=items, request_timeout_seconds=None,
        )
        return successful, failed, items

    return SimpleNamespace(run=run, current=current, verification_calls=verification_calls)


@pytest.mark.parametrize("confirmed", ["primary", "fallback"])
def test_valid_archived_full_row_resolves_current_conflict_with_original_provenance(tmp_path, archived_conflict_sync, confirmed):
    open_price = 100.0 if confirmed == "primary" else 102.0
    imported = hk_data_adapter.import_hk_raw_verification_archive(
        tmp_path, SYMBOL, raw_response=_verification_body(open_price=open_price),
        source_url=_verification_url(), observed_at=OBSERVED_AT,
    )
    successful, failed, items = archived_conflict_sync.run()
    assert successful == [SYMBOL] and failed == []
    assert archived_conflict_sync.verification_calls == []
    first = read_market_daily_symbol(tmp_path, SYMBOL).sort("date").row(0, named=True)
    source = "sina_hk_daily" if confirmed == "primary" else "tencent_hk_daily"
    assert first["open"] == open_price
    assert first["source"] == source
    assert first["verification_source"] == "eastmoney_hk_daily_check"
    evidence = items[0]["source_conflicts"][0]
    assert evidence["selected_source"] == source
    assert evidence["observed_at"] == OBSERVED_AT
    assert evidence["response_sha256"] == imported["response_sha256"]
    assert evidence["source_url"] == imported["source_url"]
    assert evidence["verification_cached"] is True


@pytest.mark.parametrize("invalid", [
    "symbol", "source", "schema", "hash", "body_tampered", "adjusted_url", "future_observation",
    "missing_conflict_day", "mixed_row", "current_rows_changed", "conflicting_archives", "ambiguous_full_row",
])
def test_invalid_or_nonmatching_archive_cannot_authorize_a_price_update(tmp_path, archived_conflict_sync, invalid):
    raw = _verification_body()
    if invalid == "missing_conflict_day":
        raw = _verification_body(trading_day="2026-09-09")
    elif invalid == "mixed_row":
        raw = _verification_body(volume=1002.0)
    elif invalid == "current_rows_changed":
        archived_conflict_sync.current["primary_open"] = 103.0
    elif invalid == "ambiguous_full_row":
        archived_conflict_sync.current["fallback_open"] = 100.008
        raw = _verification_body(open_price=100.004)
    imported = hk_data_adapter.import_hk_raw_verification_archive(
        tmp_path, SYMBOL, raw_response=raw, source_url=_verification_url(), observed_at=OBSERVED_AT,
    )
    path = Path(imported["archive_path"])
    archive = json.loads(path.read_text(encoding="utf-8"))
    modifications = {
        "symbol": ("symbol", "00941.HK"), "source": ("source", "sina_hk_daily"),
        "schema": ("schema_version", 2), "hash": ("response_sha256", "0" * 64),
        "body_tampered": ("raw_response", _verification_body(open_price=102.0).decode("utf-8")),
        "adjusted_url": ("source_url", _verification_url(fqt="1")),
        "future_observation": ("observed_at", "2099-01-01T00:00:00+00:00"),
    }
    if invalid in modifications:
        key, value = modifications[invalid]
        archive[key] = value
        path.write_text(json.dumps(archive), encoding="utf-8")
    if invalid == "conflicting_archives":
        hk_data_adapter.import_hk_raw_verification_archive(
            tmp_path, SYMBOL, raw_response=_verification_body(open_price=102.0),
            source_url=_verification_url(), observed_at=OBSERVED_AT,
        )
    before = _published_bytes(tmp_path)
    generation = get_enriched_generation(tmp_path, "hk")
    successful, failed, items = archived_conflict_sync.run()
    assert successful == [] and failed == [SYMBOL]
    assert items[0]["reason_code"] == "raw_source_conflict"
    assert archived_conflict_sync.verification_calls
    assert _published_bytes(tmp_path) == before
    assert get_enriched_generation(tmp_path, "hk") == generation


def test_full_legacy_repair_preserves_date_files_and_records_backup(tmp_path):
    days = [date(2026, 9, 10), date(2026, 9, 11)]
    legacy = _raw_bars(days).drop("raw_price_verified", "price_schema_version").with_columns(
        pl.lit("forward_adjusted").alias("price_adjustment"), (pl.col("close") / 2).alias("close"),
    )
    legacy_path = tmp_path / "kline_daily" / "date=2026-09-10" / "part.parquet"
    legacy_path.parent.mkdir(parents=True)
    legacy.write_parquet(legacy_path)
    before = legacy_path.read_bytes()
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, _raw_bars(days), factors=_factors(days[-1]))
    assert result["status"] == "ok"
    assert legacy_path.read_bytes() == before
    merged = read_market_daily_symbol(tmp_path, SYMBOL)
    assert merged["raw_price_verified"].all()
    assert merged["close"].to_list() == [100.0, 101.0]
    manifest = tmp_path / "hk_data_audit" / "backups" / result["repair_id"] / "manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["state"] == "completed"


def test_historical_company_actions_recompute_ohlc_but_never_scale_volume(tmp_path):
    """真实参考值来自本批独立源探测; 这里只测试数值变换, 不宣称实时下载。"""
    days = [date(2014, 5, 14), date(2014, 5, 15), date(2026, 5, 14), date(2026, 5, 15)]
    raw = _raw_bars(days, [514.0, 108.8, 460.20001, 456.39999]).with_columns(
        pl.Series("volume", [4010970.0, 51693834.0, 1000.0, 2000.0]),
    )
    factors = _factors(days[-1], [
        {"d": "1900-01-01", "f": "0.1734950314"},
        {"d": "2014-05-15", "f": "0.8674751572"},
        {"d": "2014-05-16", "f": "0.8693929358"},
        {"d": "2025-05-16", "f": "0.9884832681"},
        {"d": "2026-05-15", "f": "1"},
    ])
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, SYMBOL, raw, factors=factors)
    assert result["status"] == "ok"
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / f"symbol={SYMBOL}" / "part.parquet")
    expected = [514.0 * 0.1734950314, 108.8 * 0.8674751572, 460.20001 * 0.9884832681, 456.39999]
    for field in ("open", "high", "low", "close"):
        assert enriched[field].to_list() == pytest.approx(expected)
    assert enriched["raw_close"].to_list() == raw["close"].to_list()
    assert enriched["volume"].to_list() == raw["volume"].to_list()
    assert enriched["close"][-1] / enriched["close"][-2] - 1 == pytest.approx(456.39999 / (460.20001 - 5.3) - 1, abs=5e-8)


def test_hk_financial_publication_changes_only_hk_generation(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    try:
        before_hk = repo.get_matrix_data_generation("hk")
        before_us = repo.get_matrix_data_generation("us")
        frame = pl.DataFrame([_financial_row("2024-12-31", "2025-03-19", gross_margin=20.0)])
        publication = EnrichedPublication(tmp_path, "hk")
        publication.write_parquet(frame, tmp_path / "financials" / "metrics" / "hk.parquet")
        publication.commit()
        assert repo.get_matrix_data_generation("hk") != before_hk
        assert repo.get_matrix_data_generation("us") == before_us
    finally:
        repo.db.close()


def test_legacy_hk_financial_update_invalidates_status_and_repository_together(tmp_path):
    from app.services.market_data_status import get_market_data_status

    repo = KlineRepository(DataStore(tmp_path))
    path = tmp_path / "financials" / "metrics" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = _financial_row("2024-12-31", "2025-03-19", gross_margin=20.0)
    pl.DataFrame([original]).write_parquet(path)
    try:
        first_generation = repo.get_matrix_data_generation("hk")
        first_status = get_market_data_status(tmp_path, "HK")
        assert first_status["financials"]["rows"] == 1
        latest = _financial_row("2025-06-30", "2025-08-13", gross_margin=30.0)
        pl.DataFrame([original, latest]).write_parquet(path)
        assert repo.get_matrix_data_generation("hk") != first_generation
        updated_status = get_market_data_status(tmp_path, "HK")
        assert updated_status["data_generation"] != first_status["data_generation"]
        assert updated_status["financials"]["rows"] == 2
        assert updated_status["financials"]["last_period_end"] == "2025-06-30"
    finally:
        repo.db.close()


def test_hk_financial_api_normalizes_codes_and_rejects_mixed_markets(tmp_path, monkeypatch):
    from app.api import hk, pipeline
    from app.services import market_data_status

    calls = []

    async def start_job(request, *, symbols):
        calls.append(symbols)
        return {"status": "started", "job_id": "qa-financial"}

    monkeypatch.setattr(pipeline, "_start_hk_financial_job", start_job)
    monkeypatch.setattr(market_data_status, "hk_financial_capability", lambda: (True, None))
    app = FastAPI()
    app.include_router(hk.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        for invalid in ("AAPL.US", "../00700", "700,AAPL.US"):
            response = client.post("/api/hk/financials/sync", params={"symbols": invalid})
            assert response.status_code == 400
        response = client.post("/api/hk/financials/sync", params={"symbols": "700,00700.HK"})
        assert response.status_code == 200
        assert response.json()["operation"] == "financial_sync"
        assert response.json()["job_id"] == "qa-financial"
        assert response.json()["requested"] == 1
        assert calls == [[SYMBOL]]
        empty = client.post("/api/hk/financials/sync", params={"symbols": ""})
        assert empty.json()["status"] == "empty"
        monkeypatch.setattr(market_data_status, "hk_financial_capability", lambda: (False, "qa unavailable"))
        unsupported = client.post("/api/hk/financials/sync", params={"symbols": SYMBOL})
        assert unsupported.json()["status"] == "unsupported"
        assert unsupported.json()["skipped"] == 1
        assert calls == [[SYMBOL]]


@pytest.mark.parametrize("path", ["portfolio", "portfolio_legacy", "independent_candidates", "independent_candidates_legacy"])
def test_future_lot_effective_date_cannot_be_bypassed_by_explicit_quantity(path):
    class Repo:
        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({
                "symbol": [SYMBOL], "lot_size": [100], "currency": ["HKD"],
                "lot_size_status": ["verified_snapshot"], "lot_size_as_of": [date(2026, 9, 9)],
                "lot_size_effective_from": [date(2099, 1, 1)],
            })

    data = _panel([date(2025, 3, 19), date(2025, 3, 20), date(2025, 3, 21)])
    entries = data["date"] == date(2025, 3, 19)
    exits = data["date"] == date(2025, 3, 20)
    config = MatcherConfig(asset_type="hk", matching="open_t+1", lot_sizes={SYMBOL: 1})
    result = getattr(BacktestEngine(Repo()), f"simulate_{path}")(data, entries, exits, config)
    assert result.trades == []
    assert result.stats["execution"]["buy_lot_size_unverified"] == 1
