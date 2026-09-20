"""Persistence guards for verified HK prices and dated board-lot metadata."""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.services import hk_data_adapter
from app.tickflow.market_daily import write_market_daily_symbol


def _bars():
    return pl.DataFrame({"symbol": ["00700.HK", "00700.HK"], "date": [date(2026, 9, 10), date(2026, 9, 11)], "open": [100.0, 101.0], "high": [102.0, 103.0], "low": [99.0, 100.0], "close": [101.0, 102.0], "volume": [1000.0, 2000.0], "amount": [None, None]}).with_columns(pl.col("amount").cast(pl.Float64))


def _verified():
    return _bars().with_columns(pl.lit("sina_hk_daily").alias("source"), pl.lit("unadjusted").alias("price_adjustment"), pl.lit(1).alias("price_schema_version"), pl.lit(True).alias("raw_price_verified"), pl.lit("HKD").alias("currency"), pl.lit("share").alias("volume_unit"))


def test_unknown_old_prices_cannot_be_merged_into_verified_raw(tmp_path):
    path = write_market_daily_symbol(tmp_path, "00700.HK", _bars().head(1))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"口径|完整|维护"):
        write_market_daily_symbol(tmp_path, "00700.HK", _verified().tail(1))
    assert path.read_bytes() == before


def test_old_qfq_cannot_be_relabelled_as_raw(tmp_path):
    raw = _bars().with_columns(pl.lit("forward_adjusted").alias("price_adjustment"))
    with pytest.raises(ValueError, match=r"原始|口径|复权"):
        hk_data_adapter.sync_hk_daily_to_enriched("00700.HK", tmp_path, raw=raw, raise_errors=True)
    assert not (tmp_path / "kline_hk_us_enriched").exists()


def test_future_lot_candidate_keeps_existing_trusted_value(tmp_path, monkeypatch):
    from app.data_providers import hkex_instruments
    path = tmp_path / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["00700.HK", "80700.HK"], "lot_size": [100, None], "currency": ["HKD", None], "lot_size_source": ["hkex_list_of_securities", None], "lot_size_as_of": ["2026-09-09", None]}).write_parquet(path)
    future = pl.DataFrame({"symbol": ["00700.HK", "80700.HK"], "lot_size": [500, 100], "currency": ["HKD", "CNY"], "lot_size_source": ["hkex_list_of_securities"] * 2, "lot_size_as_of": [date(2099, 9, 14)] * 2})
    monkeypatch.setattr(hkex_instruments, "fetch_hkex_lot_sizes", lambda: future)
    result = hk_data_adapter.sync_hk_lot_sizes(tmp_path)
    saved = pl.read_parquet(path)
    assert saved["lot_size"].to_list() == [100, None]
    assert saved["currency"].to_list() == ["HKD", None]
    assert saved["lot_size_status"].to_list() == ["verified_snapshot", "future_snapshot"]
    assert result["items"][1]["candidate_lot_size"] == 100
    assert result["items"][1]["candidate_currency"] == "CNY"
    assert result["succeeded"] == 0


def _factors(coverage=date(2026, 9, 11), version="v1", ex_factor=1.0):
    return pl.DataFrame({"symbol": ["00700.HK", "00700.HK"],
                         "trade_date": [date(1900, 1, 1), date(2026, 9, 11)],
                         "ex_factor": [1.0, ex_factor], "source": ["verified_fixture"] * 2,
                         "version": [version] * 2, "coverage_end": [coverage] * 2})


def test_snapshot_publishes_real_raw_adjusted_identity_and_is_idempotent(tmp_path):
    from app.services.market_data_status import market_data_generation

    report = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(ex_factor=2.0))
    path = tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet"
    enriched = pl.read_parquet(path)
    assert report["status"] == "ok" and report["enriched_updated"]
    assert enriched["raw_close"].to_list() == [101.0, 102.0]
    assert enriched["close"].to_list() == [50.5, 102.0]
    assert enriched["raw_open"].to_list() == [100.0, 101.0]
    assert enriched["volume"].to_list() == [1000.0, 2000.0]
    before = market_data_generation(tmp_path, "HK")
    repeated = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(ex_factor=2.0))
    assert repeated["status"] == "unchanged"
    assert market_data_generation(tmp_path, "HK") == before


def test_cached_factor_cannot_be_extended_to_new_daily_session(tmp_path):
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified().head(1), factors=_factors(coverage=date(2026, 9, 10)))
    path = tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet"
    before = path.read_bytes()
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified().tail(1))
    assert result["status"] == "partial"
    assert result["reason_code"] == "adjustment_unavailable"
    assert not result["enriched_updated"]
    assert path.read_bytes() == before


def test_cached_factor_supports_same_covered_window_on_raw_source_switch(tmp_path):
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(ex_factor=2.0))
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified().with_columns(pl.lit("tencent_hk_daily").alias("source")))
    assert result["status"] == "ok"
    assert result["adjustment_cached"] is True
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet")
    assert enriched["close"].to_list() == [50.5, 102.0]


def _backup_outage_frame():
    """Simulate a sina-only update when the tencent backup (sole currency source) is WAF-blocked."""
    return pl.DataFrame({"symbol": ["00700.HK"], "date": [date(2026, 9, 14)], "open": [104.0], "high": [105.0], "low": [103.0], "close": [104.5], "volume": [3000.0], "amount": [None]}).with_columns(pl.col("amount").cast(pl.Float64), pl.lit("sina_hk_daily").alias("source"), pl.lit("unadjusted").alias("price_adjustment"), pl.lit(1).alias("price_schema_version"), pl.lit(True).alias("raw_price_verified"), pl.lit(None, dtype=pl.String).alias("currency"), pl.lit("share").alias("volume_unit"))


def test_backup_outage_currency_is_reused_from_verified_partition(tmp_path):
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(ex_factor=2.0))
    report = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _backup_outage_frame(), factors=_factors(coverage=date(2026, 9, 14)))
    assert report["raw_updated"]
    assert report["currency"] == "HKD"
    raw = pl.read_parquet(tmp_path / "kline_daily" / "symbol=00700.HK" / "part.parquet")
    assert raw.filter(pl.col("date") == date(2026, 9, 14))["currency"].to_list() == ["HKD"]


def test_backup_outage_without_verified_history_still_rejected(tmp_path):
    with pytest.raises(ValueError, match=r"币种|量单位|价格口径"):
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _backup_outage_frame())
    assert not (tmp_path / "kline_daily" / "symbol=00700.HK").exists()


def test_backup_outage_does_not_reuse_unverified_legacy_currency(tmp_path):
    write_market_daily_symbol(tmp_path, "00700.HK", _bars())
    with pytest.raises(ValueError, match=r"币种|量单位|价格口径"):
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _backup_outage_frame())


def test_new_factor_recomputes_entire_maintained_history(tmp_path):
    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified().tail(1), factors=_factors(version="split-v2", ex_factor=5.0))
    assert result["enriched_updated"]
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet")
    assert enriched["close"].to_list() == [20.200000000000003, 102.0]
    assert enriched["adjustment_version"].unique().to_list() == ["split-v2"]


def test_cancellation_after_fetch_prevents_any_publication(tmp_path):
    from app.services.pipeline_jobs import JobCancelledError

    def cancel():
        raise JobCancelledError("fixture")
    with pytest.raises(JobCancelledError):
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(), before_publish=cancel)
    assert not list(tmp_path.iterdir())


def test_full_legacy_repair_keeps_backup_and_partial_repair_does_not_write(tmp_path):
    path = write_market_daily_symbol(tmp_path, "00700.HK", _bars())
    before = path.read_bytes()
    with pytest.raises(ValueError, match="完整维护窗口"):
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified().tail(1), factors=_factors())
    assert path.read_bytes() == before
    result = hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    backup = tmp_path / "hk_data_audit" / "backups" / result["repair_id"]
    assert (backup / "kline_daily.parquet").read_bytes() == before
    assert (backup / "manifest.json").exists()
    assert pl.read_parquet(path)["raw_price_verified"].all()


def test_current_archive_lots_and_verified_inactive_keep_frozen_pool(tmp_path):
    path = tmp_path / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["80700.HK", "02903.HK"], "name": ["柜台", "旧临时代码"]}).write_parquet(path)
    archived = pl.DataFrame({"symbol": ["80700.HK", "02903.HK"], "lot_size": [100, None], "currency": ["CNY", None],
                             "lot_size_as_of": [date(2026, 9, 9)] * 2, "lot_size_source": ["hkex_list_of_securities", "issuer_announcement"],
                             "instrument_status": [None, "temporary_counter_closed"], "instrument_status_as_of": [None, date(2026, 9, 9)],
                             "instrument_status_source": [None, "https://example.test/announcement"], "instrument_status_reason": [None, "9月8日收市后终止"]})
    result = hk_data_adapter.sync_hk_lot_sizes(tmp_path, metadata=archived)
    saved = pl.read_parquet(path)
    assert saved["symbol"].to_list() == ["80700.HK", "02903.HK"]
    assert saved["lot_size"].to_list() == [100, None]
    assert result["verified_not_applicable"] == 1
    assert result["items"][1]["applicability"] == "verified_not_applicable"
    assert result["requested"] == result["succeeded"] + result["failed"] + result["skipped"]
    assert result["status"] == "completed"
    assert result["failures"] == []
    refreshed = hk_data_adapter.sync_hk_lot_sizes(tmp_path, metadata=archived.head(1))
    assert refreshed["status"] == "completed"
    assert refreshed["verified_not_applicable"] == 1
    assert refreshed["failures"] == []


def test_hk_refresh_preserves_trusted_lots_and_inactive_rows(tmp_path, monkeypatch):
    path = tmp_path / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["00700.HK", "02903.HK"], "lot_size": [100, None], "currency": ["HKD", None],
                  "lot_size_as_of": [date(2026, 9, 9), None], "lot_size_status": ["verified_snapshot", "missing"],
                  "instrument_status": [None, "temporary_counter_closed"]}).write_parquet(path)
    monkeypatch.setattr(hk_data_adapter, "fetch_hk_instruments_akshare", lambda: hk_data_adapter.load_demo_instruments())
    hk_data_adapter.sync_hk_instruments(tmp_path)
    saved = pl.read_parquet(path)
    assert saved.filter(pl.col("symbol") == "00700.HK")["lot_size"].item() == 100
    assert saved.filter(pl.col("symbol") == "02903.HK")["instrument_status"].item() == "temporary_counter_closed"


@pytest.mark.parametrize("failure_target", ["factor", "enriched", "audit", "commit"])
def test_failed_multifile_publish_restores_all_bytes_and_generation(tmp_path, monkeypatch, failure_target):
    from app.enriched_generation import EnrichedPublication

    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    paths = {
        "raw": tmp_path / "kline_daily" / "symbol=00700.HK" / "part.parquet",
        "factor": tmp_path / "adj_factor_hk" / "symbol=00700.HK" / "part.parquet",
        "enriched": tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet",
        "audit": tmp_path / "hk_data_audit" / "prices" / "00700.HK.json",
        "marker": tmp_path / ".matrix_generation_hk.json",
    }
    before = {name: (path.read_bytes(), path.stat().st_mtime_ns) for name, path in paths.items()}
    original_replace = hk_data_adapter.os.replace
    injected = []
    def fail_once(source, target):
        if failure_target != "commit" and target == paths[failure_target] and not injected:
            injected.append(True)
            raise OSError("injected atomic replacement failure")
        return original_replace(source, target)
    monkeypatch.setattr(hk_data_adapter.os, "replace", fail_once)
    if failure_target == "commit":
        monkeypatch.setattr(EnrichedPublication, "commit", lambda self: (_ for _ in ()).throw(OSError("injected commit failure")))
    with pytest.raises(OSError, match="injected"):
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors(version="v2", ex_factor=2.0))
    for name, path in paths.items():
        assert path.read_bytes() == before[name][0]
        if name != "marker":
            assert path.stat().st_mtime_ns == before[name][1]
    assert not list(tmp_path.rglob("*.rollback"))
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("seed_existing", [False, True])
def test_prepared_snapshot_cannot_overwrite_a_competing_commit(tmp_path, monkeypatch, seed_existing):
    from app.enriched_generation import EnrichedPublication

    if seed_existing:
        hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    original_begin = EnrichedPublication.begin
    committed = {}
    injected = []

    def competing_commit(publication):
        if not injected:
            injected.append(True)
            hk_data_adapter.publish_hk_daily_snapshot(
                tmp_path, "00700.HK", _verified(), factors=_factors(version="winner", ex_factor=3.0),
            )
            for path in tmp_path.rglob("*"):
                if path.suffix in {".parquet", ".json"}:
                    committed[path] = (path.read_bytes(), path.stat().st_mtime_ns)
        return original_begin(publication)

    monkeypatch.setattr(EnrichedPublication, "begin", competing_commit)
    with pytest.raises(ValueError, match="版本在准备期间已变化"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, "00700.HK", _verified(), factors=_factors(version="stale", ex_factor=2.0),
        )
    assert injected and committed
    for path, expected in committed.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == expected
    assert not list(tmp_path.rglob("*.rollback"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_late_cancellation_after_staging_preserves_previous_snapshot(tmp_path):
    from app.services.pipeline_jobs import JobCancelledError

    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    checks = []

    def cancel_before_claim():
        checks.append(True)
        if len(checks) == 3:
            raise JobCancelledError("cancelled after staging")

    with pytest.raises(JobCancelledError):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, "00700.HK", _verified(), factors=_factors(version="v2", ex_factor=2.0),
            before_publish=cancel_before_claim,
        )
    assert len(checks) == 3
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_legacy_hk_financial_updates_invalidate_read_only_generation(tmp_path):
    from app.services.market_data_status import market_data_generation

    initial = market_data_generation(tmp_path, "HK")
    us_initial = market_data_generation(tmp_path, "US")
    path = tmp_path / "financials" / "metrics" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["00700.HK"], "eps": [1.0]}).write_parquet(path)
    assert market_data_generation(tmp_path, "HK") != initial
    assert market_data_generation(tmp_path, "US") == us_initial
    assert not list(tmp_path.glob(".matrix_generation_*.json"))


def test_verification_archive_write_failure_rolls_back_entire_price_snapshot(tmp_path, monkeypatch):
    from app.data_providers.hk_daily_provider import build_hk_raw_verification_archive

    hk_data_adapter.publish_hk_daily_snapshot(tmp_path, "00700.HK", _verified(), factors=_factors())
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in tmp_path.rglob("*") if path.suffix in {".parquet", ".json"}}
    archive = build_hk_raw_verification_archive(
        "00700.HK",
        raw_response='{"rc":0,"data":{"code":"00700","market":116,"klines":["2026-09-10,100,101,102,99,1000"]}}',
        source_url="https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=116.00700&klt=101&fqt=0&fields2=f51,f52,f53,f54,f55,f56",
        observed_at="2026-09-12T00:00:00+00:00",
    )
    archive_path = tmp_path / "hk_data_audit" / "raw_verification" / "00700.HK" / f"{archive['response_sha256']}.json"
    original_replace = hk_data_adapter.os.replace

    def fail_archive(source, target):
        if target == archive_path:
            raise OSError("archive publication failed")
        return original_replace(source, target)

    monkeypatch.setattr(hk_data_adapter.os, "replace", fail_archive)
    with pytest.raises(OSError, match="archive publication failed"):
        hk_data_adapter.publish_hk_daily_snapshot(
            tmp_path, "00700.HK", _verified(), factors=_factors(version="v2", ex_factor=2.0),
            verification_archives=[archive],
        )
    assert not archive_path.exists()
    for path, (content, mtime) in before.items():
        assert path.read_bytes() == content
        if path.name != ".matrix_generation_hk.json":
            assert path.stat().st_mtime_ns == mtime
    assert not list(tmp_path.rglob("*.rollback"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_status_separates_verified_inactive_counters_from_missing_lots(tmp_path, monkeypatch):
    from app.services import market_data_status

    path = tmp_path / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({
        "symbol": ["00700.HK", "00007.HK", "02903.HK", "08578.HK", "80700.HK"],
        "lot_size": [100, None, 100, None, None],
        "lot_size_status": ["verified_snapshot", "missing", "verified_snapshot", "missing", "missing"],
        "currency": ["HKD", "HKD", "HKD", "HKD", "CNY"],
        "instrument_status": [None, "delisted", "temporary_counter_closed", "rights_trading_ended", "delisted"],
        "instrument_status_as_of": [None, date(2026, 1, 6), date(2026, 9, 8), date(2099, 9, 1), date(2026, 9, 8)],
        "instrument_status_source": [None, "https://example.test/cancellation", "https://example.test/temporary", "https://example.test/rights", None],
    }).write_parquet(path)
    monkeypatch.setattr(market_data_status, "daily_sync_capability", lambda *args: ("hk_daily", True, None))
    result = market_data_status.get_market_data_status(tmp_path, "HK")
    stats = result["instruments"]
    assert stats["symbols"] == 5
    assert stats["lot_size_available"] == 1
    assert stats["verified_not_applicable"] == 2
    assert stats["lot_size_missing"] == 2
    assert stats["symbols"] == stats["lot_size_available"] + stats["verified_not_applicable"] + stats["lot_size_missing"]
    assert any(warning.startswith("2 只缺少有效每手数量") for warning in result["warnings"])
    assert not any(warning.startswith("3 只缺少有效每手数量") for warning in result["warnings"])
