"""市场时钟护栏: 日 K 默认截止日期不得退回宿主机 date.today()。

背景: 日 K / 指数日 K 的默认 end 原本取宿主机本地日期, 容器(多为 UTC)或美西
主机上会出现"北京/美东已翻篇而本地未翻篇", 当日 K 整根漏掉。修复后改为取
"标的所属市场的今天"(profile_for_symbol(symbol).today()); 跨市场盘后管道没有
单一 symbol 上下文, 窗口右端改用 cn_today()。本文件是防御性加固, 防止后人改回
本地时钟。

确定性约束(关键): 期望值全部由 monkeypatch 钉死, 不读墙上时钟; 且钉住值刻意
避开本机 date.today(), 否则"默认 end != 本机日期"的断言会退化成空断言(上一版
QA 用例就是写死常量恰好等于当天北京日期而失效)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import indices as indices_api
from app.api import kline as kline_api
from app.jobs import daily_pipeline
from app.services import kline_sync

_DAYS = 120


def _pinned_clocks() -> tuple[date, date]:
    """返回 (北京日期钉, 市场日期钉)。

    两者恒相差 1 天(互不相同, 用于证明两个时钟不会串味); 且都刻意避开本机
    date.today() —— 万一撞上就整体后移一年, 保证断言始终有鉴别力。
    """
    anchor = date(2026, 3, 2)
    if anchor == date.today() or anchor - timedelta(days=1) == date.today():
        anchor = anchor.replace(year=anchor.year + 1)
    return anchor, anchor - timedelta(days=1)


CN_PIN, MARKET_PIN = _pinned_clocks()


@dataclass
class _PinnedProfile:
    """只实现 today() 的市场档案替身, 把"市场今天"钉成固定值。"""

    market: str
    pinned: date
    calls: int = 0

    def today(self) -> date:
        """返回钉住的日期, 并计数以证明端点真的咨询了市场档案。"""
        self.calls += 1
        return self.pinned


class _NoCaps:
    """权限全关的能力集: 端点在无本地数据时早退, 不触发真实网络拉取。"""

    def has(self, cap: object) -> bool:
        return False


class _RecordingRepo:
    """假仓储: 记录日 K 查询窗口 (start, end), 并返回空表让端点走早退分支。

    两个 getter 都返回 0 行 DataFrame, 因此 kline 走 "无本地数据 → 拉实时 →
    实时也为空 → 早退", indices 走 "无本地数据 → 无权限 → 早退"。
    """

    def __init__(self) -> None:
        self.window: tuple[date, date] | None = None

    def resolve_asset_type(self, symbol: str) -> str:
        """固定返回 stock, 让端点走 _get_stock_info 分支。"""
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        """空维表: _get_stock_info 返回 {} (与其列缺失兜底路径一致)。"""
        return pl.DataFrame()

    def get_index_instruments(self) -> pl.DataFrame:
        """空指数维表: _index_info 返回 {}。"""
        return pl.DataFrame()

    def get_daily_asset(
        self, asset_type: str, symbol: str, start: date, end: date
    ) -> pl.DataFrame:
        """记录个股日 K 查询窗口。"""
        self.window = (start, end)
        return pl.DataFrame()

    def get_index_daily(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        """记录指数日 K 查询窗口。"""
        self.window = (start, end)
        return pl.DataFrame()


def _request(repo: _RecordingRepo) -> SimpleNamespace:
    """构造满足 request.app.state.repo / .capabilities 访问的假 Request。"""
    state = SimpleNamespace(repo=repo, capabilities=_NoCaps())
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture
def pinned_market_clock(monkeypatch) -> _PinnedProfile:
    """把 kline / indices 的市场档案钉成固定时钟, 并阻断真实行情拉取。"""
    profile = _PinnedProfile(market="US", pinned=MARKET_PIN)
    monkeypatch.setattr(kline_api, "profile_for_symbol", lambda symbol: profile)
    monkeypatch.setattr(indices_api, "profile_for_symbol", lambda symbol: profile)
    monkeypatch.setattr(kline_sync, "sync_daily_batch", lambda *args, **kwargs: pl.DataFrame())
    return profile


def test_kline_daily_default_end_uses_market_profile_today(pinned_market_clock):
    """kline: 不传 end_date 时, end 必须是标的所属市场的今天, 而非本机日期。"""
    # 前置护栏: 钉住值不能等于本机日期, 否则本用例失去鉴别力。
    assert date.today() != MARKET_PIN

    repo = _RecordingRepo()
    kline_api.get_daily(
        _request(repo),
        symbol="AAPL.US",
        days=_DAYS,
        start_date=None,
        end_date=None,
        ext_columns=None,
    )

    assert repo.window is not None, "端点未走到日 K 窗口查询分支, 用例失去意义"
    start, end = repo.window
    assert end == MARKET_PIN
    assert start == MARKET_PIN - timedelta(days=_DAYS)
    assert pinned_market_clock.calls >= 1, "端点没有咨询市场档案, 说明又退回本地时钟"


def test_index_daily_default_end_uses_market_profile_today(pinned_market_clock):
    """indices: 不传 end_date 时, end 必须是指数所属市场的今天。"""
    assert date.today() != MARKET_PIN

    repo = _RecordingRepo()
    indices_api.get_index_daily(
        _request(repo),
        symbol="000001.SH",
        days=_DAYS,
        start_date=None,
        end_date=None,
    )

    assert repo.window is not None, "端点未走到指数日 K 窗口查询分支, 用例失去意义"
    start, end = repo.window
    assert end == MARKET_PIN
    assert start == MARKET_PIN - timedelta(days=_DAYS)
    assert pinned_market_clock.calls >= 1, "端点没有咨询市场档案, 说明又退回本地时钟"


def test_daily_pipeline_window_end_uses_cn_today(monkeypatch):
    """pipeline: 跨市场管道的窗口右端必须是北京日期 (cn_today)。"""
    assert date.today() != CN_PIN
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: CN_PIN)

    assert daily_pipeline.pipeline_window_end() == CN_PIN


def test_market_clock_and_cn_clock_do_not_cross_contaminate(pinned_market_clock, monkeypatch):
    """两个时钟钉成不同值时, 各自必须取到自己那个值 (互不串味)。"""
    assert MARKET_PIN != CN_PIN
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: CN_PIN)

    repo = _RecordingRepo()
    kline_api.get_daily(
        _request(repo),
        symbol="AAPL.US",
        days=_DAYS,
        start_date=None,
        end_date=None,
        ext_columns=None,
    )
    assert repo.window is not None
    kline_end = repo.window[1]
    pipeline_end = daily_pipeline.pipeline_window_end()

    assert kline_end == MARKET_PIN
    assert kline_end != CN_PIN
    assert pipeline_end == CN_PIN
    assert pipeline_end != MARKET_PIN
