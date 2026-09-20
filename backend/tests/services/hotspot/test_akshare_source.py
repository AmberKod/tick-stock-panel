"""AkshareHotspotSource 单元测试 (fake akshare 模块, 不打网络)."""
from __future__ import annotations

import pytest

from app.services.hotspot.akshare_source import (
    AkshareHotspotSource,
    _row_records,
    decorate_a_share_code,
)
from app.services.hotspot.models import QUALITY_FAILED, QUALITY_PARTIAL


class _FakeDF:
    """pandas DataFrame 的最小仿真 (empty + to_dict('records'))。"""

    def __init__(self, records):
        self._records = list(records)

    @property
    def empty(self) -> bool:
        return not self._records

    def to_dict(self, orient: str):
        assert orient == "records"
        return [dict(r) for r in self._records]


class _FakeAk:
    """akshare 模块桩: 东财板块/成分接口, 行为可注入。"""

    def __init__(
        self,
        *,
        concept_rows=None,
        industry_rows=None,
        cons_concept_rows=None,
        cons_industry_rows=None,
        concept_error=None,
        industry_error=None,
    ):
        self.concept_rows = concept_rows if concept_rows is not None else []
        self.industry_rows = industry_rows if industry_rows is not None else []
        self.cons_concept_rows = cons_concept_rows
        self.cons_industry_rows = cons_industry_rows
        self.concept_error = concept_error
        self.industry_error = industry_error

    def stock_board_concept_name_em(self):
        if self.concept_error is not None:
            raise self.concept_error
        return _FakeDF(self.concept_rows)

    def stock_board_industry_name_em(self):
        if self.industry_error is not None:
            raise self.industry_error
        return _FakeDF(self.industry_rows)

    def stock_board_concept_cons_em(self, symbol=""):
        if self.cons_concept_rows is None:
            return _FakeDF([])
        return _FakeDF(self.cons_concept_rows)

    def stock_board_industry_cons_em(self, symbol=""):
        if self.cons_industry_rows is None:
            return _FakeDF([])
        return _FakeDF(self.cons_industry_rows)


_CONCEPT_ROWS = [
    {
        "排名": 1, "板块名称": "人工智能", "板块代码": "BK0800", "最新价": 1234.5,
        "涨跌额": 45.6, "涨跌幅": 8.26, "总市值": 5.6e12, "换手率": 2.5,
        "上涨家数": 80, "下跌家数": 10, "领涨股票": "景嘉微", "领涨股票-涨跌幅": 19.8,
    },
    {"排名": 2, "板块名称": "CPO", "涨跌幅": 5.12, "领涨股票": "中际旭创"},
]

_INDUSTRY_ROWS = [
    {"排名": 3, "板块名称": "半导体", "涨跌幅": 2.34, "领涨股票": "中芯国际"},
    {"排名": 4, "板块名称": "白酒", "涨跌幅": -1.28, "领涨股票": "贵州茅台"},
]

_CONS_ROWS = [
    {"序号": 1, "名称": "景嘉微", "代码": "300474", "最新价": 99.8, "涨跌额": 16.6,
     "涨跌幅": 19.8, "成交量": 2.1e6, "成交额": 12.4e8, "振幅": 8.5, "换手率": 8.5,
     "市盈率-动态": 120.5},
    {"序号": 2, "名称": "中科曙光", "代码": "603019", "涨跌幅": 7.4, "成交额": 9.1e8, "换手率": 4.1},
    {"序号": 3, "名称": "科大讯飞", "代码": "002230", "涨跌幅": 6.1, "成交额": 7.3e8, "换手率": 3.8},
]


# ----------------------------------------------------------------------
# supports / discover
# ----------------------------------------------------------------------

def test_supports_only_cn():
    src = AkshareHotspotSource(ak=_FakeAk())
    assert src.supports("cn") is True
    assert src.supports("hk") is False
    assert src.supports("us") is False
    assert src.name == "akshare"


def test_discover_cn_converts_percentage_to_ratio():
    src = AkshareHotspotSource(
        ak=_FakeAk(concept_rows=_CONCEPT_ROWS, industry_rows=_INDUSTRY_ROWS)
    )
    results = src.discover(market="cn", top=0)
    assert len(results) == 4
    by_topic = {s.topic: s for s in results}
    ai = by_topic["人工智能"]
    # 涨跌幅 8.26 (百分数) → 0.0826 (小数制)
    assert ai.change_pct == pytest.approx(0.0826)
    assert ai.rank == 1
    assert ai.leaders == ["景嘉微"]
    assert ai.source == "concept"
    assert ai.provider_used == "akshare"
    assert by_topic["白酒"].source == "industry"
    assert by_topic["白酒"].change_pct == pytest.approx(-0.0128)


def test_discover_sorts_by_heat_score_desc():
    src = AkshareHotspotSource(
        ak=_FakeAk(concept_rows=_CONCEPT_ROWS, industry_rows=_INDUSTRY_ROWS)
    )
    results = src.discover(market="cn", top=0)
    scores = [s.heat_score for s in results]
    assert scores == sorted(scores, reverse=True)


def test_discover_top_limits_but_zero_returns_all():
    src = AkshareHotspotSource(
        ak=_FakeAk(concept_rows=_CONCEPT_ROWS, industry_rows=_INDUSTRY_ROWS)
    )
    assert len(src.discover(market="cn", top=2)) == 2
    assert len(src.discover(market="cn", top=0)) == 4


def test_discover_marks_partial_without_constituent_details():
    src = AkshareHotspotSource(
        ak=_FakeAk(concept_rows=_CONCEPT_ROWS, industry_rows=_INDUSTRY_ROWS)
    )
    results = src.discover(market="cn", top=0)
    for summary in results:
        assert summary.quality_status == QUALITY_PARTIAL
        assert summary.missing_fields == ["leader_stocks"]


def test_discover_concept_failure_degrades_to_industry_partial():
    src = AkshareHotspotSource(
        ak=_FakeAk(
            concept_error=TimeoutError("concept timeout"),
            industry_rows=_INDUSTRY_ROWS,
        )
    )
    results = src.discover(market="cn", top=0)
    assert len(results) == 2
    assert results.quality_status == QUALITY_PARTIAL
    assert any("stock_board_concept_name_em" in err and "TimeoutError" in err
               for err in results.source_errors)
    assert results.provider_used == "akshare"


def test_discover_all_failures_return_failed_empty():
    src = AkshareHotspotSource(
        ak=_FakeAk(
            concept_error=TimeoutError("c"),
            industry_error=ConnectionError("i"),
        )
    )
    results = src.discover(market="cn", top=20)
    assert not results.is_usable
    assert results.quality_status == QUALITY_FAILED
    assert len(results.source_errors) == 2


def test_discover_import_failure_returns_error_not_crash(monkeypatch):
    src = AkshareHotspotSource()  # ak=None → 延迟 import
    monkeypatch.setattr(
        AkshareHotspotSource, "_load_ak",
        lambda self: (_ for _ in ()).throw(RuntimeError("akshare unavailable")),
    )
    results = src.discover(market="cn", top=20)
    assert not results.is_usable
    assert any("akshare unavailable" in err for err in results.source_errors)


def test_discover_hk_returns_error_empty():
    src = AkshareHotspotSource(ak=_FakeAk())
    results = src.discover(market="hk", top=10)
    assert len(results) == 0
    assert any("hk" in err for err in results.source_errors)


# ----------------------------------------------------------------------
# fetch_detail
# ----------------------------------------------------------------------

def test_fetch_detail_decorates_code_and_converts_ratios():
    src = AkshareHotspotSource(ak=_FakeAk(cons_concept_rows=_CONS_ROWS))
    detail = src.fetch_detail("人工智能", market="cn", top_stocks=10)
    assert detail is not None
    assert detail.stock_count == 3
    by_code = {s.code: s for s in detail.stocks}
    assert "300474.SZ" in by_code
    assert "603019.SH" in by_code
    assert "002230.SZ" in by_code
    leader = by_code["300474.SZ"]
    assert leader.change_pct == pytest.approx(0.198)
    assert leader.turnover_rate == pytest.approx(0.085)
    assert leader.amount == pytest.approx(12.4e8)
    # 东财成分接口不提供涨停标志 → 显式 False, 不推断
    assert leader.is_limit_up is False
    # 角色已分配
    assert all(s.role for s in detail.stocks)
    assert detail.summary.leaders


def test_fetch_detail_reuses_discovered_summary_metadata():
    src = AkshareHotspotSource(
        ak=_FakeAk(concept_rows=_CONCEPT_ROWS, cons_concept_rows=_CONS_ROWS)
    )
    src.discover(market="cn", top=0)
    detail = src.fetch_detail("人工智能", market="cn", top_stocks=10)
    assert detail is not None
    assert detail.summary.change_pct == pytest.approx(0.0826)
    assert detail.summary.rank == 1
    # leader_stocks 补齐后不再标记缺失
    assert "leader_stocks" not in detail.summary.missing_fields


def test_fetch_detail_unknown_topic_returns_none():
    src = AkshareHotspotSource(ak=_FakeAk())  # cons 均为空
    assert src.fetch_detail("不存在的题材", market="cn") is None


def test_fetch_detail_concept_error_falls_back_to_industry_cons():
    class _BrokenConcept(_FakeAk):
        def stock_board_concept_cons_em(self, symbol=""):
            raise RuntimeError("concept cons down")

    ak = _BrokenConcept(
        concept_rows=_CONCEPT_ROWS, cons_industry_rows=_CONS_ROWS
    )
    src = AkshareHotspotSource(ak=ak)
    src.discover(market="cn", top=0)  # 人工智能 缓存为 concept
    detail = src.fetch_detail("人工智能", market="cn", top_stocks=10)
    assert detail is not None
    assert detail.stock_count == 3
    assert detail.summary.source == "industry"


def test_fetch_detail_hk_returns_none():
    src = AkshareHotspotSource(ak=_FakeAk())
    assert src.fetch_detail("人工智能", market="hk") is None


def test_fetch_detail_import_failure_returns_none(monkeypatch):
    src = AkshareHotspotSource()
    monkeypatch.setattr(
        AkshareHotspotSource, "_load_ak",
        lambda self: (_ for _ in ()).throw(RuntimeError("akshare unavailable")),
    )
    assert src.fetch_detail("人工智能", market="cn") is None


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("688981", "688981.SH"),
        ("000001", "000001.SZ"),
        ("002230", "002230.SZ"),
        ("300474", "300474.SZ"),
        ("430047", "430047.BJ"),
        ("832000", "832000.BJ"),
        ("920001", "920001.BJ"),
        ("900901", "900901"),      # 沪 B 不在板块, 不猜
        ("600519.SH", "600519.SH"),  # 已带后缀
        ("", ""),
        ("00700", "00700"),          # 非标准
    ],
)
def test_decorate_a_share_code(raw, expected):
    assert decorate_a_share_code(raw) == expected


@pytest.mark.parametrize(
    ("df", "expected_len"),
    [
        (None, 0),
        (_FakeDF([]), 0),
        (_FakeDF([{"a": 1}, {"a": 2}]), 2),
        ([{"a": 1}], 1),        # list[dict] 兼容
        (("not", "df"), 0),      # 非法输入
    ],
)
def test_row_records_tolerates_various_inputs(df, expected_len):
    assert len(_row_records(df)) == expected_len
