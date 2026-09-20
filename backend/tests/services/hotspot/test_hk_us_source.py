"""港美行业热点源单元测试。

全部用注入的 fake instruments / fake 行情, 不打网络。
覆盖: 行业聚合与排序、小行业门槛、等权涨幅、脏涨跌幅剔除、字段可得性契约
(换手/净流/活跃天数缺失、涨停恒 False、量比口径)、盘中实时 vs 盘后日K 双路。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services.hotspot.hk_us_source import (
    MIN_MEMBERS,
    HkUsIndustryHotspotSource,
    _industry_key,
    _is_in_session,
)
from app.services.hotspot.models import QUALITY_OK

HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")


def _instruments(market: str = "hk"):
    """港股: 科技 12 / 金融 12 / 微型 3 / 无行业 2。"""
    suffix = ".HK" if market == "hk" else ".US"
    rows = []
    for industry, count in (("科技", 12), ("金融", 12), ("微型", 3)):
        for i in range(count):
            rows.append({
                "symbol": f"{industry[0]}{i:03d}{suffix}",
                "name": f"{industry}{i}",
                "industry": industry,
            })
    rows.append({"symbol": f"X001{suffix}", "name": "无行业", "industry": ""})
    rows.append({"symbol": f"X002{suffix}", "name": "占位符", "industry": "nan"})
    return rows


def _quotes(market: str = "hk", symbols=None, mode: str = "daily", as_of: str = "2026-09-03"):
    """fake 行情; 第二位置参数对应 source 传入的 symbols (忽略)。"""
    suffix = ".HK" if market == "hk" else ".US"
    rows = []
    for industry, change in (("科技", 0.05), ("金融", -0.02), ("微型", 0.50)):
        count = 12 if industry != "微型" else 3
        for i in range(count):
            rows.append({
                "symbol": f"{industry[0]}{i:03d}{suffix}",
                "name": f"{industry}{i}",
                "change_pct": change,
                "amount": 1_000_000.0,
                "vol_ratio_5d": 1.2,
            })
    return rows, mode, as_of


def _source(**kwargs) -> HkUsIndustryHotspotSource:
    return HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=_quotes,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("科技", "科技"),
        ("  金融  ", "金融"),
        ("", ""),
        (None, ""),
        ("nan", ""),
        ("None", ""),
        ("null", ""),
        ("-", ""),
    ],
)
def test_industry_key_normalizes(value, expected):
    assert _industry_key(value) == expected


def test_is_in_session_hk_weekday_morning():
    assert _is_in_session("hk", datetime(2026, 9, 14, 10, 0, tzinfo=HK_TZ)) is True


def test_is_in_session_hk_lunch_break():
    # 12:30 午休: 不在任何 session 内
    assert _is_in_session("hk", datetime(2026, 9, 14, 12, 30, tzinfo=HK_TZ)) is False


def test_is_in_session_weekend():
    assert _is_in_session("hk", datetime(2026, 9, 12, 10, 0, tzinfo=HK_TZ)) is False


def test_is_in_session_us_regular_hours():
    assert _is_in_session("us", datetime(2026, 9, 14, 10, 0, tzinfo=US_TZ)) is True


# ---------------------------------------------------------------------------
# supports / 市场门禁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("market", "expected"), [("hk", True), ("us", True), ("cn", False), ("jp", False)])
def test_supports(market, expected):
    assert _source().supports(market) is expected


def test_discover_unsupported_market_returns_error():
    results = _source().discover(market="cn", top=10)
    assert len(results) == 0
    assert results.source_errors


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def test_discover_groups_by_industry_and_filters_small_ones():
    results = _source().discover(market="hk", top=0)
    topics = [item.topic for item in results]
    assert "科技" in topics and "金融" in topics
    assert "微型" not in topics  # 3 只 < MIN_MEMBERS


def test_discover_ranks_by_change_and_backfills_rank():
    results = _source().discover(market="hk", top=0)
    assert [item.topic for item in results] == ["科技", "金融"]
    assert [item.rank for item in results] == [1, 2]
    # 科技 +5% 应比金融 -2% 热度高
    assert results[0].heat_score > results[1].heat_score


def test_discover_change_pct_is_equal_weight_average():
    results = _source().discover(market="hk", top=0)
    tech = next(item for item in results if item.topic == "科技")
    assert abs((tech.change_pct or 0) - 0.05) < 1e-9


def test_discover_sets_snapshot_market_and_topic_date():
    results = _source().discover(market="us", top=0)
    for item in results:
        assert item.snapshot_market == "us"
        assert item.topic_date == "2026-09-03"


def test_discover_marks_missing_fields_but_not_limit_up():
    results = _source().discover(market="hk", top=0)
    tech = next(item for item in results if item.topic == "科技")
    assert "turnover_rate" in tech.missing_fields
    assert "net_inflow" in tech.missing_fields
    assert "active_days" in tech.missing_fields
    # 港美无涨跌停制度 → 不计入缺失字段 (是"不适用"而非"查不到")
    assert "is_limit_up" not in tech.missing_fields


def test_discover_daily_mode_marks_partial_quality():
    results = _source().discover(market="hk", top=0)
    assert results.provider_used == "hkus_industry:daily"
    assert results[0].quality_status != QUALITY_OK


def test_discover_realtime_mode_marks_ok_quality():
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=lambda market, symbols: _quotes(market, mode="realtime", as_of="2026-09-14"),
    )
    results = source.discover(market="hk", top=0)
    assert results.provider_used == "hkus_industry:realtime"
    assert results[0].quality_status == QUALITY_OK


def test_discover_drops_extreme_change_pct():
    """|涨跌幅| > 100% 视作退市重组/数据断层脏数据, 剔除。"""

    def quotes(market, symbols):
        rows, _mode, as_of = _quotes(market)
        for row in rows:
            if row["symbol"].startswith("科"):
                row["change_pct"] = 85.0  # 8500%
        return rows, "daily", as_of

    source = HkUsIndustryHotspotSource(instruments_loader=_instruments, quote_loader=quotes)
    results = source.discover(market="hk", top=0)
    assert "科技" not in [item.topic for item in results]  # 全被剔除后不足门槛/无有效涨幅


def test_discover_empty_quotes_returns_error():
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=lambda market, symbols: ([], "daily", ""),
    )
    results = source.discover(market="hk", top=0)
    assert len(results) == 0
    assert results.source_errors


def test_discover_no_instruments_returns_error():
    source = HkUsIndustryHotspotSource(
        instruments_loader=lambda market: [],
        quote_loader=_quotes,
    )
    results = source.discover(market="hk", top=0)
    assert len(results) == 0
    assert any("instruments" in err for err in results.source_errors)


def test_discover_top_limits_results():
    assert len(_source().discover(market="hk", top=1)) == 1


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


def test_detail_returns_constituents_with_field_contract():
    source = _source()
    source.discover(market="hk", top=0)
    detail = source.fetch_detail("科技", market="hk", top_stocks=5)
    assert detail is not None
    assert detail.stock_count == 12
    assert len(detail.stocks) == 5
    stock = detail.stocks[0]
    assert stock.code.endswith(".HK")
    assert abs((stock.change_pct or 0) - 0.05) < 1e-9
    assert stock.amount == 1_000_000.0
    assert stock.volume_ratio == 1.2  # 日K路: vol_ratio_5d
    assert stock.turnover_rate is None
    assert stock.net_inflow is None
    assert stock.is_limit_up is False
    assert stock.role  # assign_roles 回填


def test_detail_realtime_mode_has_no_volume_ratio():
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=lambda market, symbols: _quotes(market, mode="realtime", as_of="2026-09-14"),
    )
    detail = source.fetch_detail("科技", market="hk", top_stocks=3)
    assert detail is not None
    # 实时路没有 5 日量比口径, 不臆造
    for stock in detail.stocks:
        assert stock.volume_ratio is None


def test_detail_unknown_topic_returns_none():
    assert _source().fetch_detail("不存在的行业", market="hk") is None


def test_detail_small_industry_returns_none():
    assert _source().fetch_detail("微型", market="hk") is None  # 3 只 < 门槛


def test_detail_unsupported_market_returns_none():
    assert _source().fetch_detail("科技", market="cn") is None


def test_detail_leader_stocks_filled():
    source = _source()
    detail = source.fetch_detail("科技", market="hk", top_stocks=10)
    assert detail is not None
    assert len(detail.summary.leader_stocks) == 3
    assert len(detail.summary.leaders) == 3


# ---------------------------------------------------------------------------
# 盘中实时 / 盘后日K 双路
# ---------------------------------------------------------------------------


def test_realtime_preferred_during_session(monkeypatch):
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        now_fn=lambda market: datetime(2026, 9, 14, 10, 0, tzinfo=HK_TZ),  # 周一 10:00
    )
    monkeypatch.setattr(
        source,
        "_fetch_realtime",
        lambda market, symbols: {row["symbol"]: row for row in _quotes(market, mode="realtime")[0]},
    )
    results = source.discover(market="hk", top=0)
    assert results.provider_used == "hkus_industry:realtime"


def test_daily_used_outside_session(monkeypatch):
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        now_fn=lambda market: datetime(2026, 9, 12, 10, 0, tzinfo=HK_TZ),  # 周六
    )
    monkeypatch.setattr(
        source,
        "_read_daily_quotes",
        lambda market: ({row["symbol"]: row for row in _quotes(market)[0]}, "2026-09-03"),
    )
    results = source.discover(market="hk", top=0)
    assert results.provider_used == "hkus_industry:daily"
    assert results[0].topic_date == "2026-09-03"


def test_daily_snapshot_older_than_today_is_marked_stale():
    """快照日期 != 市场当地今天 → 显式 stale, 数据照出但不冒充实时。"""
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=lambda market, symbols: _quotes(market, mode="daily", as_of="2026-09-03"),
        now_fn=lambda market: datetime(2026, 9, 14, 10, 0, tzinfo=HK_TZ),
    )
    results = source.discover(market="hk", top=0)
    assert results[0].stale is True
    assert results[0].topic_date == "2026-09-03"


def test_realtime_snapshot_same_day_is_not_stale():
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        quote_loader=lambda market, symbols: _quotes(market, mode="realtime", as_of="2026-09-14"),
        now_fn=lambda market: datetime(2026, 9, 14, 10, 0, tzinfo=HK_TZ),
    )
    results = source.discover(market="hk", top=0)
    assert results[0].stale is False


def test_daily_used_when_realtime_fails(monkeypatch):
    source = HkUsIndustryHotspotSource(
        instruments_loader=_instruments,
        now_fn=lambda market: datetime(2026, 9, 14, 10, 0, tzinfo=HK_TZ),
    )

    def _boom(market, symbols):
        raise RuntimeError("network down")

    # 真实失败点在 _fetch_hk_realtime: _fetch_realtime 内部吞异常并回落日K
    monkeypatch.setattr(source, "_fetch_hk_realtime", _boom)
    monkeypatch.setattr(
        source,
        "_read_daily_quotes",
        lambda market: ({row["symbol"]: row for row in _quotes(market)[0]}, "2026-09-03"),
    )
    results = source.discover(market="hk", top=0)
    assert results.provider_used == "hkus_industry:daily"


def test_read_daily_quotes_accepts_non_empty_frame(monkeypatch):
    """回归: polars 的 is_empty 是方法, 不能用 getattr(df, 'is_empty') 判空。"""
    polars = pytest.importorskip("polars")
    builder = pytest.importorskip("app.services.hk_us_overview_builder")
    frame = polars.DataFrame({
        "symbol": ["00001.HK"], "change_pct": [0.01], "amount": [100.0], "vol_ratio_5d": [1.1],
    })
    monkeypatch.setattr(builder, "_load_latest_rows", lambda data_dir, market: (frame, date(2026, 9, 3)))

    quotes, as_of = HkUsIndustryHotspotSource(data_dir=Path("unused"))._read_daily_quotes("hk")
    assert as_of == "2026-09-03"
    assert quotes["00001.HK"]["change_pct"] == 0.01


def test_min_members_threshold_is_exported():
    assert MIN_MEMBERS == 10


def test_instruments_without_industry_are_dropped():
    source = _source()
    rows = source._load_instruments("hk")
    symbols = [row["symbol"] for row in rows]
    assert "X001.HK" not in symbols  # 空行业
    assert "X002.HK" not in symbols  # 'nan' 占位


# ---------------------------------------------------------------------------
# 美股实时: 批量化 + 单位换算 + 超时回落 (2026-09-19 修)
# ---------------------------------------------------------------------------


class _FakeTencent:
    """冒充腾讯批量 provider: 记下每次批量调用的规模, 支持按次注入延迟。"""

    def __init__(self, symbols_per_call=None, delay_s=0.0):
        self.calls = []
        self._delay = delay_s
        self._symbols_per_call = symbols_per_call

    def get_realtime(self, symbols=None, **_):
        self.calls.append(list(symbols or []))
        if self._delay:
            import time as _t

            _t.sleep(self._delay)
        got = self._symbols_per_call or symbols or []
        return _FakeFrame([{
            "symbol": s, "name": s, "last_price": 110.0, "prev_close": 100.0,
            "amount": 1.0, "change_pct": 10.0,  # 百分制 10%
        } for s in got])

    def close(self):
        return None


class _FakeFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dicts(self):
        return list(self._rows)


def test_us_realtime_uses_batched_tencent(monkeypatch):
    """回归: 逐只 yfinance 会把 5653 只灌成几十分钟的挂死请求。"""
    fake = _FakeTencent()
    monkeypatch.setattr(
        "app.data_providers.tencent_market_provider.TencentMultiMarketProvider",
        lambda *a, **kw: fake,
    )
    symbols = [f"S{i}.US" for i in range(450)]
    out = HkUsIndustryHotspotSource._fetch_us_realtime(symbols)

    assert len(out) == 450
    # 批量而非逐只: 450 只 → 3 次(200/200/50), 绝不是 450 次
    assert len(fake.calls) == 3
    assert all(len(c) <= 200 for c in fake.calls)


def test_us_realtime_converts_percent_to_fraction(monkeypatch):
    """腾讯 change_pct 是百分数(10.0 = +10%), 日K 是小数(0.1); 必须换算成小数制。"""
    fake = _FakeTencent()
    monkeypatch.setattr(
        "app.data_providers.tencent_market_provider.TencentMultiMarketProvider",
        lambda *a, **kw: fake,
    )
    out = HkUsIndustryHotspotSource._fetch_us_realtime(["AAPL.US"])
    # last_price 110 / prev_close 100 → +0.1(小数制), 不是 10.0
    assert out["AAPL.US"]["change_pct"] == pytest.approx(0.1)


def test_us_realtime_falls_back_when_deadline_hit(monkeypatch):
    """超时不硬撑: 返回空 dict 让上层回落 enriched 日K, 而不是把请求挂死。"""
    fake = _FakeTencent(delay_s=0.35)
    monkeypatch.setattr(
        "app.data_providers.tencent_market_provider.TencentMultiMarketProvider",
        lambda *a, **kw: fake,
    )
    symbols = [f"S{i}.US" for i in range(2000)]
    out = HkUsIndustryHotspotSource._fetch_us_realtime(symbols, deadline_s=0.2)
    assert out == {}
    assert len(fake.calls) < len(symbols)  # 没走成逐只


def test_pct_from_quote_row_prefers_last_over_prev_close():
    """有 prev_close 时现算, 不受 provider 单位口径影响。"""
    from app.services.hotspot.hk_us_source import _pct_from_quote_row

    assert _pct_from_quote_row({"last_price": 105.0, "prev_close": 100.0}) == pytest.approx(0.05)
    # 没有 prev_close 才退回 change_pct, 且按百分数处理
    assert _pct_from_quote_row({"change_pct": 5.0}) == pytest.approx(0.05)
