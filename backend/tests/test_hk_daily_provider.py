"""HK raw-source and adjustment contracts, including failure isolation."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import polars as pl
import pytest

from app.data_providers.hk_daily_provider import (
    EASTMONEY_VERIFY_URL,
    HKDailyProvider,
    build_hk_raw_verification_archive,
    parse_sina_factors,
)
from app.indicators.pipeline import _apply_adj_factor


def test_cumulative_sina_factors_become_event_ratios():
    text = 'var hk00700qfq={"data":[{"d":"2014-05-16","f":"1"},{"d":"2014-05-15","f":"0.997794118"},{"d":"1900-01-01","f":"0.1995588236"}]}/* ignored */'
    factors = parse_sina_factors(text, "00700.HK", date(2014, 5, 19), "2026-09-12T00:00:00+00:00")
    split = factors.filter(pl.col("trade_date") == date(2014, 5, 15))["ex_factor"].item()
    assert split == pytest.approx(5.0)
    raw = pl.DataFrame({"symbol": ["00700.HK"] * 3, "date": [date(2014, 5, 14), date(2014, 5, 15), date(2014, 5, 16)], "close": [514.0, 108.8, 106.5]})
    adjusted = _apply_adj_factor(raw, factors)
    assert adjusted["close"].to_list() == pytest.approx([514 * 0.1995588236, 108.8 * 0.997794118, 106.5])
    assert factors["version"].n_unique() == 1
    refreshed = parse_sina_factors(text, "00700.HK", date(2014, 5, 19), "2026-09-13T00:00:00+00:00")
    assert refreshed["version"].item(0) == factors["version"].item(0)


@pytest.mark.parametrize("text", [
    'var hk00700qfq={"data":[{"d":"1900-01-01","f":0}]}',
    'var hk00005qfq={"data":[{"d":"1900-01-01","f":1}]}',
    'var hk00700qfq={"data":[{"d":"2020-01-01","f":1}]}',
    'var hk00700qfq={data:evil()}',
    'var hk00700qfq={"data":[{"d":"1900-01-01","f":1},{"d":"1900-01-01","f":2}]}',
])
def test_invalid_adjustment_snapshot_is_rejected(text):
    with pytest.raises(ValueError):
        parse_sina_factors(text, "00700.HK", date(2026, 9, 11), "now")


def _tencent(symbol="hk00700", rows=None, currency="HKD"):
    quote = [""] * 80
    quote[2] = symbol[2:]
    quote[30] = "2026/09/11 16:09:00"
    quote[75] = currency
    return {"code": 0, "data": {symbol: {"day": rows or [["2026-09-10", "100", "101", "102", "99", "1000"], ["2026-09-11", "101", "102", "103", "100", "2000"]], "qt": {symbol: quote}}}}


def _response(request):
    return httpx.Response(200, json=_tencent("hkHSI" if "hkHSI" in request.url.params.get("param", "") else "hk00700"))


def test_actual_raw_fallback_and_factor_failure_are_separate(monkeypatch):
    def handler(request):
        if "qfq.js" in request.url.path:
            return httpx.Response(503)
        if "klc2" in request.url.path:
            raise httpx.ReadTimeout("main raw timeout")
        return _response(request)
    provider = HKDailyProvider(transport=httpx.MockTransport(handler), timeout=0.01)
    result = provider.get_daily_with_report(["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), "stock")
    assert result.frame["close"].to_list() == [101.0, 102.0]
    assert result.frame["volume"].to_list() == [1000.0, 2000.0]
    assert result.frame["amount"].null_count() == 2
    assert result.frame["source"].unique().to_list() == ["tencent_hk_daily"]
    assert result.frame["price_adjustment"].unique().to_list() == ["unadjusted"]
    assert result.items[0]["fallback_used"] is True
    assert result.items[0]["status"] == "partial"
    assert result.adjustments.is_empty()


def test_tencent_request_adjust_does_not_relabel_day_data(monkeypatch):
    def handler(request):
        if "klc2" in request.url.path:
            return httpx.Response(200, text='var KLC_KL_hk00700="fixture";')
        if "qfq.js" in request.url.path:
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        return _response(request)
    monkeypatch.setattr("app.data_providers.hk_daily_provider._decode_sina_rows", lambda encoded: [{"date": "2026-09-10", "open": 100, "close": 101, "high": 102, "low": 99, "volume": 1000}, {"date": "2026-09-11", "open": 101, "close": 102, "high": 103, "low": 100, "volume": 2000}])
    result = HKDailyProvider(transport=httpx.MockTransport(handler)).get_daily_with_report(["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), "stock")
    assert result.items[0]["status"] == "ok"
    assert result.frame["raw_price_verified"].all()
    assert result.frame["currency"].unique().to_list() == ["HKD"]
    assert result.adjustments["coverage_end"].item(0) == date(2026, 9, 11)


def test_conflicting_raw_overlap_is_not_silently_merged(monkeypatch):
    def handler(request):
        if "klc2" in request.url.path:
            return httpx.Response(200, text='var KLC_KL_hk00700="fixture";')
        if "qfq.js" in request.url.path:
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        return _response(request)
    monkeypatch.setattr("app.data_providers.hk_daily_provider._decode_sina_rows", lambda encoded: [{"date": "2026-09-10", "open": 200, "close": 201, "high": 202, "low": 199, "volume": 1000}])
    result = HKDailyProvider(transport=httpx.MockTransport(handler)).get_daily_with_report(["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), "stock")
    assert result.frame.is_empty()
    assert result.items[0]["reason_code"] == "raw_source_conflict"


def test_mixed_market_input_never_makes_http_request():
    calls = []
    provider = HKDailyProvider(transport=httpx.MockTransport(lambda request: calls.append(request)))
    with pytest.raises(ValueError):
        provider.get_daily(["AAPL.US"], None, None, "stock")
    assert not calls


def test_hsi_sessions_distinguish_holiday_from_missing_trading_day(monkeypatch):
    rows = [{"date": "2026-01-02", "open": 100, "close": 101, "high": 102, "low": 99, "volume": 1000}]
    monkeypatch.setattr("app.data_providers.hk_daily_provider._decode_sina_rows", lambda encoded: rows)
    def handler(request):
        if "klc2" in request.url.path:
            return httpx.Response(200, text='var KLC_K2_00700="fixture";')
        if "qfq.js" in request.url.path:
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        symbol = "hkHSI" if "hkHSI" in request.url.params.get("param", "") else "hk00700"
        return httpx.Response(200, json=_tencent(symbol, [["2026-01-02", "100", "101", "102", "99", "1000"]]))
    result = HKDailyProvider(transport=httpx.MockTransport(handler)).get_daily_with_report(["00700.HK"], datetime(2026, 1, 1), datetime(2026, 1, 4), "stock")
    assert result.items[0]["coverage_complete"]
    assert result.items[0]["missing_dates"] == []
    assert result.items[0]["status"] == "ok"


def test_source_missing_a_real_hsi_session_remains_partial(monkeypatch):
    def handler(request):
        if "sina.com" in request.url.host:
            return httpx.Response(503)
        symbol = "hkHSI" if "hkHSI" in request.url.params.get("param", "") else "hk00700"
        payload = _tencent(symbol)
        if symbol == "hk00700":
            payload["data"][symbol]["day"] = payload["data"][symbol]["day"][:1]
        return httpx.Response(200, json=payload)
    result = HKDailyProvider(transport=httpx.MockTransport(handler)).get_daily_with_report(["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), "stock")
    assert not result.items[0]["coverage_complete"]
    assert result.items[0]["missing_dates"] == ["2026-09-11"]
    assert result.items[0]["status"] == "partial"
    assert result.items[0]["source_errors"]["sina_hk_daily"] == "HTTP 503"


def _verification_archive(*, entries=None, observed_at="2026-09-12T00:00:00+00:00"):
    body = json.dumps({"rc": 0, "data": {"code": "00700", "market": 116, "klines": entries or [
        "2026-09-10,100,101,102,99,1000", "2026-09-11,101,102,103,100,2000",
    ]}})
    source_url = str(httpx.URL(EASTMONEY_VERIFY_URL, params={
        "secid": "116.00700", "klt": "101", "fqt": "0", "beg": "20260910", "end": "20260911",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }))
    return build_hk_raw_verification_archive(
        "00700.HK", raw_response=body, source_url=source_url, observed_at=observed_at,
    )


def _conflicting_provider(monkeypatch, verifier):
    monkeypatch.setattr("app.data_providers.hk_daily_provider._decode_sina_rows", lambda encoded: [
        {"date": "2026-09-10", "open": 200, "close": 201, "high": 202, "low": 199, "volume": 1000},
        {"date": "2026-09-11", "open": 101, "close": 102, "high": 103, "low": 100, "volume": 2000},
    ])

    def handler(request):
        if request.url.host == "push2his.eastmoney.com":
            return verifier(request)
        if "klc2" in request.url.path:
            return httpx.Response(200, text='var KLC_K2_00700="fixture";')
        if "qfq.js" in request.url.path:
            return httpx.Response(200, text='var hk00700qfq={"data":[{"d":"1900-01-01","f":"1"}]}')
        return _response(request)

    return HKDailyProvider(transport=httpx.MockTransport(handler))


def test_daily_job_persists_cold_verification_and_reuses_exact_warm_archive(tmp_path, monkeypatch):
    from app.data_providers import registry
    from app.services import hk_data_adapter, kline_sync
    from app.services.market_data_status import market_data_generation

    archive = _verification_archive()
    verification_calls = []

    def verifier(request):
        verification_calls.append(str(request.url))
        assert len(verification_calls) == 1, "the warm exact archive should not request the unavailable verifier"
        return httpx.Response(200, text=archive["raw_response"])

    provider = _conflicting_provider(monkeypatch, verifier)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(registry, "get_default_provider", lambda *args, **kwargs: provider)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    first_items, first_successes = [], []
    assert kline_sync.sync_and_persist_daily_batch(
        ["00700.HK"], repo, None, asset_type="hk", start_date=datetime(2026, 9, 10),
        end_date=datetime(2026, 9, 11), items_out=first_items, successful_out=first_successes,
    ) == 2
    assert first_successes == ["00700.HK"]
    assert first_items[0]["verification_cached"] is False
    saved = hk_data_adapter.load_hk_raw_verification_archives(tmp_path, ["00700.HK"])
    assert len(saved) == 1 and saved[0]["raw_response"] == archive["raw_response"]
    before_generation = market_data_generation(tmp_path, "HK")
    second_items = []
    assert kline_sync.sync_and_persist_daily_batch(
        ["00700.HK"], repo, None, asset_type="hk", start_date=datetime(2026, 9, 10),
        end_date=datetime(2026, 9, 11), items_out=second_items,
    ) == 2
    assert len(verification_calls) == 1
    assert second_items[0]["status"] == "unchanged"
    assert second_items[0]["verification_cached"] is True
    assert second_items[0]["source_conflicts"][0]["observed_at"] == saved[0]["observed_at"]
    assert market_data_generation(tmp_path, "HK") == before_generation
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet")
    assert enriched["raw_close"].to_list() == [101.0, 102.0]
    assert enriched["volume"].to_list() == [1000.0, 2000.0]


@pytest.mark.parametrize("corruption", ["hash", "json_shape", "identity"])
def test_corrupt_archive_cannot_suppress_a_fresh_independent_check(monkeypatch, corruption):
    archive = _verification_archive()
    damaged = dict(archive)
    if corruption == "hash":
        damaged["response_sha256"] = "0" * 64
    elif corruption == "json_shape":
        damaged["raw_response"] = "[]"
        damaged["response_sha256"] = hashlib.sha256(b"[]").hexdigest()
    else:
        damaged["source_url"] = archive["source_url"].replace("fqt=0", "fqt=1")
    calls = []

    def verifier(request):
        calls.append(request)
        return httpx.Response(200, text=archive["raw_response"])

    result = _conflicting_provider(monkeypatch, verifier).get_daily_with_report(
        ["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), verification_archives=[damaged],
    )
    assert len(calls) == 1
    assert result.items[0]["status"] == "ok"
    assert result.items[0]["verification_cached"] is False
    assert len(result.verification_archives) == 1


@pytest.mark.parametrize("case", ["different_day", "wrong_volume", "conflicting_archives"])
def test_warm_archive_never_extrapolates_or_selects_ambiguous_ohlcv(monkeypatch, case):
    archives = [_verification_archive()]
    if case == "different_day":
        archives = [_verification_archive(entries=["2026-09-11,101,102,103,100,2000"])]
    elif case == "wrong_volume":
        archives = [_verification_archive(entries=["2026-09-10,100,101,102,99,9999"])]
    else:
        archives.append(_verification_archive(entries=["2026-09-10,200,201,202,199,1000"]))
    calls = []

    def unavailable(request):
        calls.append(request)
        return httpx.Response(503)

    result = _conflicting_provider(monkeypatch, unavailable).get_daily_with_report(
        ["00700.HK"], datetime(2026, 9, 10), datetime(2026, 9, 11), verification_archives=archives,
    )
    assert calls
    assert result.frame.is_empty()
    assert result.items[0]["reason_code"] == "raw_source_conflict"
    assert result.verification_archives == ()


def test_archive_import_preserves_original_provenance_and_repairs_damaged_copy(tmp_path):
    from app.services.hk_data_adapter import import_hk_raw_verification_archive

    archive = _verification_archive()
    original = import_hk_raw_verification_archive(
        tmp_path, "700", **{key: archive[key] for key in ("raw_response", "source_url", "observed_at")},
    )
    path = Path(original["archive_path"])
    before = path.read_bytes(), path.stat().st_mtime_ns
    duplicate = import_hk_raw_verification_archive(
        tmp_path, "00700.HK", raw_response=archive["raw_response"], source_url=archive["source_url"],
        observed_at="2026-09-12T01:00:00+00:00",
    )
    assert duplicate["observed_at"] == original["observed_at"]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    path.write_text("invalid JSON", encoding="utf-8")
    repaired = import_hk_raw_verification_archive(
        tmp_path, "00700.HK", **{key: archive[key] for key in ("raw_response", "source_url", "observed_at")},
    )
    assert repaired["response_sha256"] == original["response_sha256"]
    assert json.loads(path.read_text(encoding="utf-8")) == archive


def test_daily_read_api_uses_imported_evidence_without_writing_price_files(tmp_path, monkeypatch):
    from app.api.hk import get_hk_daily
    from app.data_providers import registry
    from app.services.hk_data_adapter import import_hk_raw_verification_archive

    archive = _verification_archive()
    import_hk_raw_verification_archive(
        tmp_path, "00700.HK", **{key: archive[key] for key in ("raw_response", "source_url", "observed_at")},
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    calls = []

    def unavailable(request):
        calls.append(request)
        return httpx.Response(503)

    provider = _conflicting_provider(monkeypatch, unavailable)
    monkeypatch.setattr(registry, "get_default_provider", lambda *args, **kwargs: provider)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    result = get_hk_daily("00700.HK", request, start=date(2026, 9, 10), end=date(2026, 9, 11), days=120)
    assert [row["close"] for row in result["rows"]] == [101.0, 102.0]
    assert result["items"][0]["verification_cached"] is True
    assert result["items"][0]["source_conflicts"][0]["observed_at"] == archive["observed_at"]
    assert not calls
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.fixture
def tencent_circuit_reset():
    from app.data_providers import hk_daily_provider as mod

    mod._reset_tencent_circuit()
    yield
    mod._reset_tencent_circuit()


def _waf_client(counter: dict):
    def handler(request):
        counter["n"] += 1
        return httpx.Response(501)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tencent_circuit_opens_and_skips_network(tencent_circuit_reset):
    from app.data_providers import hk_daily_provider as mod

    counter = {"n": 0}
    client = _waf_client(counter)
    for _ in range(mod._TENCENT_FAILURE_THRESHOLD):
        with pytest.raises(httpx.HTTPStatusError):
            mod._tencent_get_text(client, {"param": "hk00700,day,2026-09-01,2026-09-14,640,"})
    assert counter["n"] == mod._TENCENT_FAILURE_THRESHOLD
    with pytest.raises(ValueError, match="熔断"):
        mod._tencent_get_text(client, {"param": "hk00700,day,2026-09-01,2026-09-14,640,"})
    assert counter["n"] == mod._TENCENT_FAILURE_THRESHOLD  # 冷却期内不再打网络


def test_tencent_circuit_half_open_doubles_cooldown_then_resets(tencent_circuit_reset, monkeypatch):
    from app.data_providers import hk_daily_provider as mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod, "_tencent_now", lambda: clock["now"])
    counter = {"n": 0}
    client = _waf_client(counter)
    for _ in range(mod._TENCENT_FAILURE_THRESHOLD):
        with pytest.raises(httpx.HTTPStatusError):
            mod._tencent_get_text(client, {"param": "p"})
    assert mod._TENCENT_CIRCUIT["opens"] == 1

    clock["now"] += mod._TENCENT_COOLDOWN_SECONDS - 1
    with pytest.raises(ValueError, match="熔断"):  # 冷却未满仍拦截
        mod._tencent_get_text(client, {"param": "p"})

    clock["now"] += 2  # 越过冷却 → 半开探测, 失败则翻倍
    with pytest.raises(httpx.HTTPStatusError):
        mod._tencent_get_text(client, {"param": "p"})
    assert mod._TENCENT_CIRCUIT["opens"] == 2
    assert mod._TENCENT_CIRCUIT["blocked_until"] == pytest.approx(
        clock["now"] + mod._TENCENT_COOLDOWN_SECONDS * 2)

    clock["now"] = mod._TENCENT_CIRCUIT["blocked_until"] + 1
    ok_counter = {"n": 0}

    def ok_handler(request):
        ok_counter["n"] += 1
        return httpx.Response(200, json=_tencent("hkHSI"))

    ok_client = httpx.Client(transport=httpx.MockTransport(ok_handler))
    mod._tencent_get_text(ok_client, {"param": "p"})  # 探测成功 → 完全复位
    assert mod._TENCENT_CIRCUIT == {"failures": 0, "opens": 0, "blocked_until": 0.0}


def test_tencent_success_resets_consecutive_failures(tencent_circuit_reset):
    from app.data_providers import hk_daily_provider as mod

    counter = {"n": 0}
    client = _waf_client(counter)
    for _ in range(mod._TENCENT_FAILURE_THRESHOLD - 1):
        with pytest.raises(httpx.HTTPStatusError):
            mod._tencent_get_text(client, {"param": "p"})

    ok_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_tencent("hkHSI"))))
    mod._tencent_get_text(ok_client, {"param": "p"})
    assert mod._TENCENT_CIRCUIT["failures"] == 0

    for _ in range(mod._TENCENT_FAILURE_THRESHOLD - 1):  # 非连续失败不触发熔断
        with pytest.raises(httpx.HTTPStatusError):
            mod._tencent_get_text(client, {"param": "p"})
    assert mod._TENCENT_CIRCUIT["opens"] == 0


def test_calendar_shares_the_tencent_circuit(tencent_circuit_reset):
    from app.data_providers import hk_daily_provider as mod

    counter = {"n": 0}
    client = _waf_client(counter)
    for _ in range(mod._TENCENT_FAILURE_THRESHOLD):
        with pytest.raises(httpx.HTTPStatusError):
            mod._tencent_get_text(client, {"param": "p"})

    provider = HKDailyProvider(transport=httpx.MockTransport(
        lambda r: httpx.Response(501)), timeout=0.01)
    before = counter["n"]
    with pytest.raises(ValueError, match="熔断"):  # 日历调用同样被熔断拦截
        provider._calendar(client, date(2026, 9, 1), date(2026, 9, 14))
    assert counter["n"] == before
