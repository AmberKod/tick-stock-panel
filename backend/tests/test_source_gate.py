"""source_gate 真实性闸门测试 (九章融合 P0-1 配套)。

覆盖: 白名单各值通过、demo/fail/timeout 剔除、缺失 source 剔除、
未知 source 剔除、None/空列表边界、组合 source、reject_reasons 明细。
"""
from __future__ import annotations

import pytest

from app.services.source_gate import (
    REAL_SOURCES,
    FilterResult,
    filter_records,
    is_real,
)


# ───────────────────────── is_real: 白名单 ─────────────────────────


class TestWhitelistPass:
    def test_all_whitelisted_sources_pass(self):
        for s in sorted(REAL_SOURCES):
            assert is_real(s), f"白名单成员 {s!r} 必须通过 is_real"

    def test_jiuzhang_live_sources_pass(self):
        for s in ("live-tencent", "live-akshare", "live-eastmoney",
                  "live-hexin", "live-tencent-proxy", "sec-edgar"):
            assert is_real(s)

    def test_tsp_provider_sources_pass(self):
        for s in ("tencent", "sina", "tickflow", "yfinance",
                  "tencent_allstock_compat", "sina_hk_qfq", "sina_hk_daily",
                  "tencent_hk_daily", "eastmoney_hk_daily_check",
                  "eastmoney_hk_announcement", "akshare", "akshare_em",
                  "akshare_sina", "hk_universe_file", "us_universe_file"):
            assert is_real(s)

    def test_record_dict_passes(self):
        assert is_real({"source": "sec-edgar", "pe": 30.1})

    def test_record_object_attribute_passes(self):
        class Rec:
            source = "live-tencent"
        assert is_real(Rec())

    def test_combo_source_all_real_passes(self):
        assert is_real("sina_hk_daily+tencent_hk_daily")


# ───────────────────────── is_real: 剔除 ─────────────────────────


class TestReject:
    def test_demo_rejected(self):
        assert not is_real("demo")

    def test_demo_variants_rejected(self):
        for s in ("us_demo", "hk_demo", "DEMO", "demo_fallback"):
            assert not is_real(s), f"{s!r} 应被剔除"

    def test_fail_rejected(self):
        assert not is_real("fail")

    def test_timeout_rejected(self):
        assert not is_real("timeout")

    def test_unavailable_rejected(self):
        assert not is_real("unavailable")

    def test_unknown_rejected(self):
        assert not is_real("some-random-feed")

    def test_missing_source_dict_rejected(self):
        assert not is_real({"pe": 30.1})

    def test_none_source_dict_rejected(self):
        assert not is_real({"source": None, "pe": 30.1})

    def test_missing_attribute_rejected(self):
        class Rec:
            pe = 30.1  # 没有 source 属性
        assert not is_real(Rec())

    def test_none_rejected(self):
        assert not is_real(None)

    def test_non_string_source_rejected(self):
        assert not is_real({"source": 123})
        assert not is_real({"source": ["live-tencent"]})

    def test_empty_string_rejected(self):
        assert not is_real("")

    def test_combo_with_fake_part_rejected(self):
        assert not is_real("sina_hk_daily+demo")

    def test_combo_with_unknown_part_rejected(self):
        assert not is_real("tencent+mystery")

    def test_combo_empty_part_rejected(self):
        assert not is_real("sina_hk_daily+")
        assert not is_real("+tencent")


# ───────────────────────── filter_records ─────────────────────────


class TestFilterRecords:
    def test_empty_list(self):
        r = filter_records([])
        assert isinstance(r, FilterResult)
        assert r.accepted == []
        assert r.rejected_count == 0
        assert r.reject_reasons == []

    def test_none_input(self):
        r = filter_records(None)
        assert r.accepted == []
        assert r.rejected_count == 0
        assert r.reject_reasons == []

    def test_mixed_records(self):
        records = [
            {"source": "sec-edgar", "v": 1},
            {"source": "demo", "v": 2},
            {"source": "fail", "v": 3},
            {"source": "timeout", "v": 4},
            {"v": 5},                      # 缺 source
            {"source": "mystery", "v": 6},  # 未知
            {"source": "live-akshare", "v": 7},
        ]
        r = filter_records(records)
        assert [x["v"] for x in r.accepted] == [1, 7]
        assert r.rejected_count == 5
        assert len(r.reject_reasons) == 5

    def test_reject_reasons_content(self):
        records = [
            {"source": "demo", "id": "a"},
            {"source": "fail", "id": "b"},
            {"source": "timeout", "id": "b2"},
            {"id": "c"},
            {"source": "mystery", "id": "d"},
        ]
        r = filter_records(records)
        assert r.reject_reasons == [
            "records[0]: demo_source:'demo'",
            "records[1]: unavailable_source:'fail'",
            "records[2]: unavailable_source:'timeout'",
            "records[3]: missing_source",
            "records[4]: unknown_source:'mystery'",
        ]

    def test_reject_reasons_include_index_and_value(self):
        r = filter_records([{"source": "who-knows"}])
        assert r.reject_reasons == ["records[0]: unknown_source:'who-knows'"]
        assert r.rejected_count == 1
        assert r.accepted == []

    def test_all_pass_no_rejects(self):
        records = [{"source": s} for s in ("tencent", "sina", "tickflow")]
        r = filter_records(records)
        assert r.accepted == records
        assert r.rejected_count == 0
        assert r.reject_reasons == []

    def test_accepted_records_unchanged(self):
        """通过的记录必须原样透传 (不加占位字段、不修改)。"""
        rec = {"source": "sec-edgar", "metrics": {"revenue": 109.42e9}}
        r = filter_records([rec])
        assert r.accepted == [rec]
        assert r.accepted[0] is rec

    def test_string_sources_batch(self):
        r = filter_records(["live-tencent", "demo", "us_demo", "sec-edgar"])
        assert r.accepted == ["live-tencent", "sec-edgar"]
        assert r.rejected_count == 2
        assert "records[1]: demo_source:'demo'" in r.reject_reasons
        assert "records[2]: demo_source:'us_demo'" in r.reject_reasons

    def test_demo_not_in_whitelist(self):
        assert "demo" not in REAL_SOURCES
        assert "us_demo" not in REAL_SOURCES
        assert "hk_demo" not in REAL_SOURCES

    def test_filter_result_is_frozen_dataclass(self):
        r = filter_records([])
        with pytest.raises(Exception):
            r.rejected_count = 1  # type: ignore[misc]
