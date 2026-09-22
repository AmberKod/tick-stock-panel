"""市场时钟护栏 v2（提案）— 在 v3 提交版 4 用例基础上补掉 QA 实测的两个漏洞。

补的两条（对应 RECOVERY.md 六之二节）:
- **G1（市场归属）**: stub 原来是 `lambda symbol: profile`, 忽略入参, 所以把实现改成
  `profile_for_symbol("XXX")` 时 4 条用例仍全绿。现在 stub 记录被查询的 symbol,
  每条端点用例补结构断言 `seen == [symbol]`。
- **G2（调用点, 严重）**: 原来只测 `pipeline_window_end()` helper, 不测调用点,
  所以把 `run_now` 里的 `today = pipeline_window_end()` 改回 `_date.today()` 时
  4 条用例仍全绿。现在新增第 5 条用例, **真的跑进 run_now 的调用点**捕获 `today`。

确定性约束(关键): 期望值全部由 monkeypatch 钉死, 不读墙上时钟; 且钉住值刻意
避开本机 date.today()（撞上就整体后移一年）, 否则"默认 end != 本机日期"的断言
会退化成空断言。

接缝耦合声明（纪律: 改接缝必须与改实现在同一个 commit）:
- 端点用例的接缝 = 模块级 `profile_for_symbol`。
- 第 5 条用例的接缝 = `app.services.data_integrity.scan_recent_integrity`
  （在 `today` 计算完成后被调用, 用它做"记录并抛哨兵异常"的收网点）。
  若日后 run_now 不再调用它, 该用例会**响亮变红**（pytest.raises 未触发）, 不是静默失效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import indices as indices_api
from app.api import kline as kline_api
from app.jobs import daily_pipeline
from app.services import data_integrity, instrument_sync, kline_sync

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


@dataclass
class _ClockStub:
    """钉住的时钟 + 被查询过的 symbol 列表 (G1: 结构断言的数据来源)。"""

    profile: _PinnedProfile
    seen: list[str] = field(default_factory=list)


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
def pinned_market_clock(monkeypatch) -> _ClockStub:
    """把 kline / indices 的市场档案钉成固定时钟, 并阻断真实行情拉取。

    stub 会记录每次被查询的 symbol (G1): 实现若硬编码成别的标的, seen 立刻不符。
    """
    stub = _ClockStub(profile=_PinnedProfile(market="US", pinned=MARKET_PIN))

    def _fake_profile_for_symbol(symbol: str) -> _PinnedProfile:
        stub.seen.append(symbol)
        return stub.profile

    monkeypatch.setattr(kline_api, "profile_for_symbol", _fake_profile_for_symbol)
    monkeypatch.setattr(indices_api, "profile_for_symbol", _fake_profile_for_symbol)
    monkeypatch.setattr(kline_sync, "sync_daily_batch", lambda *args, **kwargs: pl.DataFrame())
    return stub


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
    assert pinned_market_clock.profile.calls >= 1, "端点没有咨询市场档案, 说明又退回本地时钟"
    # G1 结构断言: 必须按"请求里的标的"查档案, 硬编码任何别的 symbol 都会在这里变红。
    assert pinned_market_clock.seen == ["AAPL.US"]


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
    assert pinned_market_clock.profile.calls >= 1, "端点没有咨询市场档案, 说明又退回本地时钟"
    # G1 结构断言
    assert pinned_market_clock.seen == ["000001.SH"]


def test_daily_pipeline_window_end_uses_cn_today(monkeypatch):
    """pipeline: 窗口右端取值函数必须返回北京日期 (cn_today)。

    注意: 这条只钉住 helper 本身, **不足以证明调用点用了它** ——
    调用点由 test_run_now_window_end_comes_from_cn_today 守。
    """
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


class _StopAfterToday(BaseException):
    """哨兵异常: 在 run_now 算完 today 之后立即中断管道, 便于在调用点收网。

    必须继承 BaseException 而非 Exception —— 管道把 integrity 扫描包在
    `try / except Exception` 里做软失败兜底 (见 daily_pipeline 中
    "integrity scan failed (soft, 按无坏数据处理)"), 用 Exception 子类会被
    当场吞掉, 哨兵传不出来, 本条用例就失去了调用点覆盖。
    """


def test_run_now_window_end_comes_from_cn_today(monkeypatch, tmp_path):
    """G2 调用点护栏: run_now 里的 today 必须真的来自 pipeline_window_end()。

    不经 helper、直接跑真实 run_now: 把紧随 today 之后的 data_integrity 扫描
    换成"记下 today 再抛哨兵异常", 从而在调用点捕获取值。把实现改回
    _date.today() 时, 捕获到的是本机日期 != CN_PIN ⇒ 本条立刻变红。
    """
    assert date.today() != CN_PIN
    captured: dict[str, date] = {}

    def _capture_today(*args, **kwargs):
        captured["today"] = kwargs.get("today")
        raise _StopAfterToday()

    monkeypatch.setattr(data_integrity, "scan_recent_integrity", _capture_today)
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: CN_PIN)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *args, **kwargs: [])
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda *args, **kwargs: 0)

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()

        def latest_daily_date(self):
            return None

    with pytest.raises(_StopAfterToday):
        daily_pipeline.run_now(_Repo(), None)

    assert captured["today"] == CN_PIN
