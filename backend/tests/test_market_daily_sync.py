from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import market_daily_sync


def test_strict_instrument_sync_rejects_demo_fallback(monkeypatch, tmp_path):
    from app.services import hk_data_adapter

    monkeypatch.setattr(hk_data_adapter, "fetch_hk_instruments_akshare", lambda: None)
    with pytest.raises(RuntimeError, match="拒绝写入 demo"):
        hk_data_adapter.sync_hk_instruments(tmp_path, use_akshare=True, allow_demo=False)
    assert not (tmp_path / "instruments" / "hk_instruments.parquet").exists()


def test_strict_instrument_sync_rejects_small_real_snapshot(monkeypatch, tmp_path):
    from app.services import hk_data_adapter

    monkeypatch.setattr(hk_data_adapter, "fetch_hk_instruments_akshare", lambda: hk_data_adapter.load_demo_instruments())
    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        hk_data_adapter.sync_hk_instruments(tmp_path, use_akshare=True, allow_demo=False)


def test_strict_us_instrument_sync_rejects_demo_fallback(monkeypatch, tmp_path):
    from app.services import hk_data_adapter

    monkeypatch.setattr(hk_data_adapter, "fetch_us_instruments_akshare", lambda: None)
    with pytest.raises(RuntimeError, match="拒绝写入 demo"):
        hk_data_adapter.sync_us_instruments(tmp_path, use_akshare=True, allow_demo=False)
    assert not (tmp_path / "instruments" / "us_instruments.parquet").exists()


def test_scheduler_registers_market_daily_jobs(monkeypatch):
    from app.jobs import daily_pipeline

    class _Scheduler:
        def __init__(self, **kwargs):
            self.jobs = []
        def add_job(self, fn, **kwargs):
            self.jobs.append((fn, kwargs))
        def start(self):
            pass

    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", _Scheduler)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_schedule", lambda: {"hour": 15, "minute": 30})
    monkeypatch.setattr(daily_pipeline._prefs, "get_instruments_schedule", lambda: {"hour": 9, "minute": 10})
    monkeypatch.setattr(daily_pipeline._prefs, "get_depth_finalize_time", lambda: {"hour": 15, "minute": 2})
    monkeypatch.setattr(daily_pipeline._prefs, "get_review_schedule", lambda: {"enabled": False})
    scheduler = daily_pipeline.start_scheduler(SimpleNamespace(), SimpleNamespace())
    ids = {kwargs["id"] for _, kwargs in scheduler.jobs}
    assert {"market_daily_hk", "market_daily_us"}.issubset(ids)


def test_api_request_allows_automatic_universe():
    from app.api.pipeline import _parse_market_daily_request

    market, symbols, start_date, end_date, mode, batch_size = _parse_market_daily_request({
        "market": "HK",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    })
    assert market == "HK"
    assert symbols is None
    assert mode == "full"
    assert batch_size is None
    assert start_date.date().isoformat() == "2026-01-01"
    assert end_date.date().isoformat() == "2026-01-31"


class _Repo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)


def _write_universe(tmp_path, rows):
    path = tmp_path / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    import polars as pl
    pl.DataFrame(rows).write_parquet(path)


def test_load_market_universe_rejects_demo_only(tmp_path):
    _write_universe(tmp_path, [{"symbol": "00700.HK", "source": "hk_demo"}])
    with pytest.raises(market_daily_sync.UniverseUnavailableError, match="demo"):
        market_daily_sync.load_market_universe(tmp_path, "HK")


def test_load_market_universe_reads_real_snapshot(tmp_path):
    rows = [
        {"symbol": f"{index:05d}.HK", "source": "akshare"}
        for index in range(100)
    ]
    _write_universe(tmp_path, rows)
    result = market_daily_sync.load_market_universe(tmp_path, "HK")
    assert len(result) == 100
    assert result == sorted(result)


def test_checkpoint_roundtrip_is_atomic(tmp_path):
    path = tmp_path / "checkpoints" / "job.json"
    market_daily_sync.write_checkpoint(path, {"job_id": "job1", "status": "running"})
    assert market_daily_sync.read_checkpoint(path) == {"job_id": "job1", "status": "running"}
    assert list(path.parent.glob("*.tmp")) == []


def test_partial_chunk_only_marks_returned_symbols_completed(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)

    def fake_sync(symbols, repo, capset, **kwargs):
        kwargs["successful_out"].append(symbols[0])
        return 2

    monkeypatch.setattr(market_daily_sync.kline_sync, "sync_and_persist_daily_batch", fake_sync)
    result = market_daily_sync.run_market_daily_sync(
        repo=repo,
        capset=object(),
        job_id="job1",
        market="HK",
        symbols=["00001.HK", "00002.HK"],
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 31),
        batch_size=2,
    )

    assert result["completed_symbols"] == ["00001.HK"]
    assert result["failed_symbols"] == ["00002.HK"]
    assert result["provider_errors"]["00002.HK"] == "missing_from_result"
    assert result["status"] == "completed_with_errors"


def test_retry_only_requests_failed_symbols(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    calls = []
    symbols = ["A.US", "B.US", "C.US"]
    checkpoint = {
        "market": "US",
        "universe_fingerprint": market_daily_sync.universe_fingerprint(symbols),
        "completed_symbols": ["A.US", "C.US"],
        "failed_symbols": ["B.US"],
    }

    def fake_sync(chunk, repo, capset, **kwargs):
        calls.append(list(chunk))
        kwargs["successful_out"].extend(chunk)
        return len(chunk)

    monkeypatch.setattr(market_daily_sync.kline_sync, "sync_and_persist_daily_batch", fake_sync)
    result = market_daily_sync.run_market_daily_sync(
        repo=repo,
        capset=object(),
        job_id="retry1",
        market="US",
        symbols=symbols,
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 31),
        mode="retry_only",
        resume_checkpoint=checkpoint,
    )

    assert calls == [["B.US"]]
    assert result["completed_symbols"] == symbols
    assert result["failed_symbols"] == []
    assert result["status"] == "completed"


def test_custom_provider_uses_timeout_and_tracks_partial_symbols(monkeypatch, tmp_path):
    from app.services import kline_sync
    calls = []

    class _Provider:
        def get_daily(self, symbols, **kwargs):
            calls.append(list(symbols))
            return __import__("polars").DataFrame([{
                "symbol": symbols[0],
                "date": "2026-01-02",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 10,
                "amount": 20,
            }])

    class _Repo:
        def __init__(self):
            self.store = SimpleNamespace(data_dir=tmp_path)
            self.db = SimpleNamespace(execute=lambda *args, **kwargs: None)

        def append_daily(self, frame):
            self.frame = frame

    repo = _Repo()
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "custom")
    successful, failed = [], []
    # Patch the module-level provider lookup used by sync_and_persist.
    from app.data_providers import custom
    monkeypatch.setattr(custom, "get_provider", lambda name: _Provider())
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: True)
    written = kline_sync.sync_and_persist_daily_batch(
        ["A.US", "B.US"], repo, object(), start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 31), successful_out=successful, failed_out=failed,
        request_timeout_seconds=1,
    )
    assert written == 1
    assert successful == ["A.US"]
    assert failed == ["B.US"]
    assert calls == [["A.US", "B.US"]]


def test_provider_timeout_isolated(monkeypatch):
    from app.services import kline_sync

    def slow_fetch():
        import time
        time.sleep(0.05)
        return None

    with pytest.raises(TimeoutError, match="timeout"):
        kline_sync._fetch_batch_with_timeout(slow_fetch, 0.001)


def test_provider_circuit_opens_after_threshold():
    circuit = market_daily_sync.ProviderCircuit(failure_threshold=2, cooldown_seconds=60)
    assert circuit.allow()
    assert not circuit.record_failure()
    assert circuit.record_failure()
    assert not circuit.allow()
    circuit.record_success()
    assert circuit.allow()


def test_daily_batch_retries_transient_timeout(monkeypatch):
    from app.services import kline_sync
    calls = []

    class _Klines:
        def batch(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("temporary timeout")
            import polars as pl
            return {"A.US": pl.DataFrame([{"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "amount": 20}])}

    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=_Klines()))
    result = kline_sync.sync_daily_batch(
        ["A.US"], batch_size=1, rpm=0, max_retries=1, retry_backoff_seconds=0
    )
    assert len(calls) == 2
    assert result.height == 1


def test_load_market_universe_rejects_small_real_snapshot(tmp_path):
    _write_universe(tmp_path, [{"symbol": "00700.HK", "source": "akshare"}])
    with pytest.raises(market_daily_sync.UniverseUnavailableError, match="最低门槛"):
        market_daily_sync.load_market_universe(tmp_path, "HK")


def test_provider_and_error_code_are_persisted(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    monkeypatch.setattr(market_daily_sync.preferences, "get_daily_data_provider", lambda: "yfinance")

    def fake_sync(*args, **kwargs):
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(market_daily_sync.kline_sync, "sync_and_persist_daily_batch", fake_sync)
    result = market_daily_sync.run_market_daily_sync(
        repo=repo,
        capset=object(),
        job_id="job-timeout",
        market="US",
        symbols=["A.US"],
        start_date=datetime(2026, 1, 1),
        end_date=datetime(2026, 1, 31),
    )

    assert result["provider"] == "yfinance"
    assert result["provider_errors"] == {"A.US": "timeout"}
    assert result["status"] == "failed"
    assert result["succeeded"] == 0
    assert result["failed"] == 1


def test_cancel_persists_remaining_symbols(monkeypatch, tmp_path):
    repo = _Repo(tmp_path)
    from app.services import pipeline_jobs

    pipeline_jobs._register_cancel_flag("job-cancel")
    pipeline_jobs.request_cancel("job-cancel")
    monkeypatch.setattr(market_daily_sync.kline_sync, "sync_and_persist_daily_batch", lambda *args, **kwargs: 0)

    with pytest.raises(pipeline_jobs.JobCancelledError):
        market_daily_sync.run_market_daily_sync(
            repo=repo,
            capset=object(),
            job_id="job-cancel",
            market="HK",
            symbols=["00001.HK", "00002.HK"],
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 31),
        )

    checkpoint = market_daily_sync.read_checkpoint(
        market_daily_sync.checkpoint_path(tmp_path, "job-cancel")
    )
    assert checkpoint["status"] == "cancelled"
    assert checkpoint["failed_symbols"] == ["00001.HK", "00002.HK"]


def test_resume_rejects_changed_universe(tmp_path):
    repo = _Repo(tmp_path)
    checkpoint = {
        "market": "HK",
        "universe_fingerprint": market_daily_sync.universe_fingerprint(["00001.HK"]),
        "completed_symbols": [],
        "failed_symbols": ["00001.HK"],
    }

    try:
        market_daily_sync.run_market_daily_sync(
            repo=repo,
            capset=object(),
            job_id="job2",
            market="HK",
            symbols=["00001.HK", "00002.HK"],
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 31),
            resume_checkpoint=checkpoint,
        )
    except ValueError as exc:
        assert "universe" in str(exc)
    else:
        raise AssertionError("changed universe should be rejected")
