"""Public-disclosure parsing and isolated, version-preserving HK financial sync."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import pytest

from app.data_providers.hk_financial_provider import HKFinancialProvider, parse_hk_announcement
from app.services import financial_sync


def disclosure(symbol="00700.HK", *, period="2024-12-31", announce="2025-03-19", revenue="660,257", prior="609,015"):
    title = "截至二零二四年十二月三十一日止年度全年业绩公布"
    header = "截至十二月三十一日止年度\n二零二四年 二零二三年"
    if period == "2025-06-30":
        title = "截至二零二五年六月三十日止三个月及六个月业绩公布"
        header = "截至下列日期止六個月\n二零二五年 二零二四年\n六月三十日 六月三十日"
    art_code = "AN" + announce.replace("-", "") + "1644721554"
    listing = {"art_code": art_code, "notice_date": announce + " 00:00:00", "title": title,
               "codes": [{"stock_code": symbol[:5], "market_code": "116"}],
               "columns": [{"column_code": "011001003005"}], "eiTime": announce + " 16:31:02:000"}
    content = (header + "\n\uff08人民幣百萬元\uff0c另有指明者除外\uff09\n"
               f"收入  {revenue}  {prior}  8%\n毛利  349,246  293,109  19%\n"
               "年度盈利  196,467  118,048  66%\n本公司權益持有人應佔盈利  194,073  115,216  68%\n"
               "非國際財務報告準則經營盈利 237,811 191,886 24%\n")
    detail = {"art_code": art_code, "notice_date": announce + " 00:00:00", "notice_title": title,
              "notice_content": content, "attach_url": f"https://pdf.dfcfw.com/pdf/H2_{art_code}_1.pdf",
              "page_size": 1, "security": [{"stock": symbol[:5], "market_uni": "116"}]}
    return listing, detail


def record(symbol="00700.HK", period="2024-12-31", announce="2025-03-19", revision="original", **values):
    return {
        "symbol": symbol, "period_end": period, "announce_date": announce,
        "revision_id": revision, "source": "fixture_public_report", "report_currency": "CNY",
        "observed_at": "2026-09-12T00:00:00+00:00", "source_url": "https://example.com/report",
        "publication_source": "issuer_disclosure",
        "field_provenance": json.dumps({field: {"source": "fixture_public_report", "source_url": "https://example.com/report",
            "announce_date": announce, "unit": "percent_number", "currency": "CNY", "basis": "as_reported"}
            for field, value in values.items() if value is not None}),
        **values,
    }


def test_original_annual_report_produces_four_ratios_and_no_current_per_share_data():
    listing, detail = disclosure()
    row = parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00")
    assert row["period_end"] == date(2024, 12, 31)
    assert row["announce_date"] == date(2025, 3, 19)
    assert row["report_currency"] == "CNY"
    assert row["gross_margin"] == pytest.approx(349246 / 660257 * 100)
    assert row["net_margin"] == pytest.approx(196467 / 660257 * 100)
    assert row["revenue_yoy"] == pytest.approx((660257 / 609015 - 1) * 100)
    assert row["net_income_yoy"] == pytest.approx((194073 / 115216 - 1) * 100)
    assert row.get("bps") is None and row.get("eps_ttm") is None
    provenance = json.loads(row["field_provenance"])
    assert provenance["gross_margin"]["unit"] == "percent_number"
    assert provenance["net_income_yoy"]["formula"] == "(parent_net_profit / prior_parent_net_profit - 1) * 100"


def test_parser_is_not_bound_to_a_ticker_or_fixed_values_and_ignores_quarter_table():
    listing, detail = disclosure("00001.HK", period="2025-06-30", announce="2025-08-13", revenue="400", prior="320")
    detail["notice_content"] = (
        "截至下列日期止三個月\n二零二五年 二零二四年\n\uff08人民幣百萬元\uff09\n收入 100 80\n毛利 20 10\n"
        + detail["notice_content"].replace("349,246  293,109", "120  64")
        .replace("年度盈利  196,467  118,048", "期內盈利  40  24")
        .replace("194,073  115,216", "36  20")
    )
    row = parse_hk_announcement("00001.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00")
    assert row["gross_margin"] == pytest.approx(30)
    assert row["net_margin"] == pytest.approx(10)
    assert row["revenue_yoy"] == pytest.approx(25)
    assert row["net_income_yoy"] == pytest.approx(80)


@pytest.mark.parametrize("prior", ["0", "(609,015)"])
def test_nonpositive_comparison_base_is_missing(prior):
    listing, detail = disclosure(prior=prior)
    row = parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00")
    assert row["revenue_yoy"] is None
    assert row["gross_margin"] is not None


def test_ambiguous_duplicate_table_never_chooses_an_arbitrary_value():
    listing, detail = disclosure()
    detail["notice_content"] += detail["notice_content"].replace("660,257", "660,999")
    row = parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00")
    assert row is None or row["gross_margin"] is None


def test_reversed_year_columns_are_mapped_from_header():
    listing, detail = disclosure()
    detail["notice_content"] = detail["notice_content"].replace("二零二四年 二零二三年", "二零二三年 二零二四年")
    row = parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00")
    assert row["revenue_yoy"] == pytest.approx((609015 / 660257 - 1) * 100)
    assert row["gross_margin"] == pytest.approx(293109 / 609015 * 100)


@pytest.mark.parametrize("replacement", ["收入 6 660,257 609,015", "收入 660,257 609,015 8", "收入 660,257 609,015 610,000"])
def test_unidentified_numeric_columns_cannot_pollute_ratios(replacement):
    listing, detail = disclosure()
    detail["notice_content"] = detail["notice_content"].replace("收入  660,257  609,015  8%", replacement)
    assert parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00") is None


def test_three_year_header_is_not_a_two_column_comparison():
    listing, detail = disclosure()
    detail["notice_content"] = detail["notice_content"].replace("二零二四年 二零二三年", "二零二四年 二零二三年 二零二二年")
    assert parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00") is None


@pytest.mark.parametrize("changed", ["symbol", "date", "future", "report_date"])
def test_unverifiable_publication_identity_is_rejected(changed):
    listing, detail = disclosure()
    if changed == "symbol":
        listing["codes"][0]["stock_code"] = "00005"
    elif changed == "date":
        detail["notice_date"] = "2025-03-20 00:00:00"
    elif changed == "future":
        listing["notice_date"] = detail["notice_date"] = "2027-03-19 00:00:00"
    else:
        listing.pop("notice_date")
        detail.pop("notice_date")
    assert parse_hk_announcement("00700.HK", listing, detail, observed_at="2026-09-12T00:00:00+00:00") is None


def test_provider_uses_disclosures_without_requesting_current_valuation_snapshots():
    listing, detail = disclosure()
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if request.url.path == "/api/security/ann":
            return httpx.Response(200, json={"success": 1, "data": {"list": [listing], "total_hits": 1}})
        assert request.url.path == "/api/content/ann"
        return httpx.Response(200, json={"success": 1, "data": detail})

    provider = HKFinancialProvider(transport=httpx.MockTransport(transport))
    frame = provider.get_financials("metrics", ["00700.HK"])
    assert frame.height == 1
    assert frame["gross_margin"].item() == pytest.approx(349246 / 660257 * 100)
    assert all("MAININDICATOR" not in call for call in calls)


def test_hk_version_merge_preserves_revisions_and_quarantines_same_key_conflicts():
    old = pl.DataFrame([record(gross_margin=20)])
    revision = pl.DataFrame([record(announce="2025-03-22", revision="revised", gross_margin=21)])
    merged, conflicts = financial_sync._merge_hk_report_history(old, revision)
    assert merged.height == 2 and conflicts == []
    duplicate, conflicts = financial_sync._merge_hk_report_history(merged, revision)
    assert duplicate.height == 2 and conflicts == []
    changed = old.with_columns(pl.lit(999.0).alias("gross_margin"))
    protected, conflicts = financial_sync._merge_hk_report_history(merged, changed)
    assert conflicts
    assert protected.filter(pl.col("revision_id") == "original")["gross_margin"].item() == 20


def test_hk_market_file_never_overwrites_or_imports_cn_records(tmp_path: Path):
    directory = tmp_path / "financials" / "metrics"
    directory.mkdir(parents=True)
    cn_file = directory / "part.parquet"
    pl.DataFrame([{"symbol": "600000.SH", "announce_date": "2025-03-01", "gross_margin": 2.0}]).write_parquet(cn_file)
    original = cn_file.read_bytes()
    pl.DataFrame([record(gross_margin=30)]).write_parquet(directory / "hk.parquet")
    assert financial_sync.get_financial_df(tmp_path, "metrics", market="HK")["symbol"].to_list() == ["00700.HK"]
    assert financial_sync.get_financial_df(tmp_path, "metrics")["symbol"].to_list() == ["600000.SH"]
    assert cn_file.read_bytes() == original


def test_primary_missing_fields_are_filled_from_versioned_history(tmp_path: Path, monkeypatch):
    class Provider:
        name = "mock_verified_fallback"

        def get_financials(self, table, symbols, latest_only=False):
            return pl.DataFrame([record(gross_margin=20, net_margin=10, revenue_yoy=8, net_income_yoy=9),
                                 record(period="2025-06-30", announce="2025-08-13", revision="interim", gross_margin=30, net_margin=12, revenue_yoy=15, net_income_yoy=18)])

    monkeypatch.setattr(financial_sync, "_get_hk_primary_provider", lambda: None)
    monkeypatch.setattr(financial_sync, "_get_hk_fallback_provider", lambda: Provider())
    result = financial_sync.sync_hk_financial_history(tmp_path, symbols=["00700.HK"])
    assert result["operation"] == "financial_sync"
    assert result["succeeded"] == result["requested"] == 1
    assert result["items"][0]["fallback_used"] is True
    status = financial_sync.get_hk_financial_status(tmp_path)
    assert status["rows"] == 2 and status["symbols"] == 1
    assert status["fields"]["gross_margin"]["available_symbols"] == 1
    assert not (tmp_path / "financials" / "metrics" / "part.parquet").exists()
    assert financial_sync.sync_hk_financial_history(tmp_path, symbols=["00700.HK"])["unchanged"] == 1


@pytest.mark.parametrize("existing", [False, True])
def test_financial_commit_failure_restores_bytes_mtime_and_generation(tmp_path, monkeypatch, existing):
    from app.enriched_generation import EnrichedPublication, get_enriched_generation

    target = tmp_path / "financials" / "metrics" / "hk.parquet"
    marker = tmp_path / ".matrix_generation_hk.json"
    if existing:
        target.parent.mkdir(parents=True)
        pl.DataFrame([record(gross_margin=20)]).write_parquet(target)
        get_enriched_generation(tmp_path, "hk")
    original = target.read_bytes() if existing else None
    original_mtime = target.stat().st_mtime_ns if existing else None
    original_marker = marker.read_bytes() if existing else None

    class Provider:
        name = "test_disclosure"

        def get_financials(self, table, symbols, latest_only=False):
            return pl.DataFrame([record(announce="2025-03-22", revision="revised", gross_margin=25)])

    def commit_failure(self):
        raise OSError("injected financial commit failure")

    monkeypatch.setattr(financial_sync, "_get_hk_primary_provider", lambda: None)
    monkeypatch.setattr(financial_sync, "_get_hk_fallback_provider", lambda: Provider())
    monkeypatch.setattr(EnrichedPublication, "commit", commit_failure)
    with pytest.raises(OSError, match="injected financial commit"):
        financial_sync.sync_hk_financial_history(tmp_path, symbols=["00700.HK"])
    assert (target.read_bytes() if target.exists() else None) == original
    assert (target.stat().st_mtime_ns if target.exists() else None) == original_mtime
    assert (marker.read_bytes() if marker.exists() else None) == original_marker
    assert not list(target.parent.glob(".*.tmp"))
    assert not list(target.parent.glob(".*.rollback"))


def test_financial_staging_cannot_overwrite_a_competing_publication(tmp_path, monkeypatch):
    from app.enriched_generation import EnrichedPublication, get_enriched_generation

    target = tmp_path / "financials" / "metrics" / "hk.parquet"
    target.parent.mkdir(parents=True)
    initial = pl.DataFrame([record(gross_margin=20)])
    initial.write_parquet(target)
    get_enriched_generation(tmp_path, "hk")
    winner = pl.DataFrame([record(gross_margin=20), record(announce="2025-03-23", revision="winner", gross_margin=27)])
    winner_state = {}
    write_parquet = pl.DataFrame.write_parquet

    def competing_write(frame, output, *args, **kwargs):
        result = write_parquet(frame, output, *args, **kwargs)
        if not winner_state and Path(output).name.startswith(".hk.parquet."):
            winner_state["entered"] = True
            publication = EnrichedPublication(tmp_path, "hk")
            publication.write_parquet(winner, target)
            publication.commit()
            winner_state.update(data=target.read_bytes(), mtime=target.stat().st_mtime_ns,
                                marker=(tmp_path / ".matrix_generation_hk.json").read_bytes())
        return result

    class Provider:
        name = "test_disclosure"

        def get_financials(self, table, symbols, latest_only=False):
            return pl.DataFrame([record(announce="2025-03-22", revision="loser", gross_margin=25)])

    monkeypatch.setattr(financial_sync, "_get_hk_primary_provider", lambda: None)
    monkeypatch.setattr(financial_sync, "_get_hk_fallback_provider", lambda: Provider())
    monkeypatch.setattr(pl.DataFrame, "write_parquet", competing_write)
    with pytest.raises(ValueError, match="版本在准备期间已变化"):
        financial_sync.sync_hk_financial_history(tmp_path, symbols=["00700.HK"])
    assert target.read_bytes() == winner_state["data"]
    assert target.stat().st_mtime_ns == winner_state["mtime"]
    assert (tmp_path / ".matrix_generation_hk.json").read_bytes() == winner_state["marker"]
    assert set(pl.read_parquet(target)["revision_id"].to_list()) == {"original", "winner"}
