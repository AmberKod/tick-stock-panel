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
- **管道侧（本次迁移后）的接缝 = `registry._PROFILES` 的 key**: 把各市场哨兵
  档案 setitem 进注册表, 由 `cross_market_today()` 逐个 `get_profile(m).today()`
  读出。注入在 registry 层、**不 stub 被测函数本身** —— 把 `max()` 整段 stub 掉
  会让断言退化成 plumbing 检查（只证明"调用了", 不证明"取了最大值"）。
  哨兵互异 (CN > HK > US), 断言取最大值。
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
from app.markets import registry
from app.services import data_integrity, instrument_sync, kline_sync

_DAYS = 120


def _pinned_clocks() -> tuple[date, date, date, date]:
    """返回 (CN, HK, US, JP) 四个互异的市场日期钉。

    CN > HK > US 依次递减 1 天 —— 互不相同才能判定"取到了哪个市场"、也才能
    让"取最大值"成为一条有鉴别力的断言; JP 比 CN 再领先 1 天 (UTC+9: 北京
    23:00 起 JP 日期领先 CN), 用于覆盖"UTC+8 以东"市场。
    四个值都刻意避开本机 date.today() —— 万一撞上就整体后移一年, 保证断言
    始终有鉴别力。
    """
    anchor = date(2026, 3, 2)
    candidates = (
        anchor + timedelta(days=1),   # JP
        anchor,                       # CN
        anchor - timedelta(days=1),   # HK
        anchor - timedelta(days=2),   # US
    )
    if any(c == date.today() for c in candidates):
        anchor = anchor.replace(year=anchor.year + 1)
    return (
        anchor,                       # CN
        anchor - timedelta(days=1),   # HK
        anchor - timedelta(days=2),   # US
        anchor + timedelta(days=1),   # JP
    )


CN_PIN, HK_PIN, US_PIN, JP_PIN = _pinned_clocks()

# 端点用例沿用旧名 MARKET_PIN (= 美股市场钉), 保持既有断言不受本次迁移影响。
MARKET_PIN = US_PIN


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


def _pin_registry(monkeypatch, pins: dict[str, date]) -> dict[str, _PinnedProfile]:
    """在 registry 层钉住各市场时钟 —— 注入点是 `registry._PROFILES` 的 key。

    cross_market_today() / cross_market_window() 逐个 get_profile(m).today()
    遍历这张表, 所以哨兵能穿过 helper 直达断言; 不 stub 被测函数本身,
    max()/min() 的真实性仍在覆盖范围内。
    """
    profiles = {
        market: _PinnedProfile(market=market, pinned=pinned)
        for market, pinned in pins.items()
    }
    for market, profile in profiles.items():
        monkeypatch.setitem(registry._PROFILES, market, profile)
    return profiles


@pytest.fixture
def pinned_registry(monkeypatch) -> dict[str, _PinnedProfile]:
    """CN / HK / US 三市场哨兵: 互异且递减 (CN > HK > US), 断言取最大值。"""
    return _pin_registry(monkeypatch, {"CN": CN_PIN, "HK": HK_PIN, "US": US_PIN})


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


def test_daily_pipeline_window_end_uses_cross_market_today(pinned_registry):
    """pipeline: 窗口右端必须返回**跨市场最大**当日日期, 而不是北京日期。

    哨兵互异 (CN > HK > US), 期望值 = 三者最大值 CN_PIN。若实现退回
    cn_today() / 宿主机 date.today(), 取到的是真实日期 != CN_PIN ⇒ 红;
    若实现只遍历了部分市场(如硬编码 CN_PROFILE), 同样取不到 CN_PIN。

    注意: 这条只钉住 helper 本身, **不足以证明调用点用了它** ——
    调用点由 test_run_now_window_end_comes_from_cross_market_today 守。
    """
    assert date.today() != CN_PIN
    assert CN_PIN > HK_PIN > US_PIN, "哨兵必须互异, 否则 max() 断言退化成空断言"

    assert daily_pipeline.pipeline_window_end() == CN_PIN


def test_endpoint_end_must_not_degrade_to_cross_market_today(pinned_registry, monkeypatch):
    """反向护栏: 端点默认截止日期**不得**改用 cross_market_today()。

    【旧语义 — 已随本次迁移有计划地废除】迁移前 pipeline 用 cn_today()、端点
    用市场档案, 是两个独立时钟; 本用例原名
    test_market_clock_and_cn_clock_do_not_cross_contaminate, 守的是"两个时钟
    钉成不同值时互不串味"。迁移后 pipeline 不再有独立第二时钟 (它现在也读
    registry), 该语义已不存在, 因此**必须反向重写**而不是换个 patch 目标。

    【新语义 — 守反向风险】cross_market_today() 一旦存在且"看起来更统一",
    下一个动 kline.py get_daily / indices.py get_index_daily 的人很可能顺手把
    `profile_for_symbol(symbol).today()` 换成它。后果: 美股窗口右端被抬到跨
    市场最大值 (本例 CN_PIN), 而美东当天可能尚未翻篇 —— 等于把"日K默认截止
    日期用错时钟"这批刚修好的 bug **反向改回去**。

    判据: US 哨兵严格落后于跨市场最大值, 于是"端点值 == 跨市场最大值" 就是
    退化的充要信号 (现有端点用例钉的是"等于市场档案的 today", 拦不住这种改法)。
    """
    assert US_PIN < HK_PIN < CN_PIN, "US 哨兵必须严格落后, 否则本用例无法识别退化"
    monkeypatch.setattr(kline_sync, "sync_daily_batch", lambda *args, **kwargs: pl.DataFrame())

    cross_market = registry.cross_market_today()
    assert cross_market == CN_PIN, "registry 层哨兵未生效, 本用例失去鉴别力"

    repo = _RecordingRepo()
    kline_api.get_daily(
        _request(repo),
        symbol="AAPL.US",
        days=_DAYS,
        start_date=None,
        end_date=None,
        ext_columns=None,
    )
    assert repo.window is not None, "kline 端点未走到日 K 窗口查询分支, 用例失去意义"
    kline_end = repo.window[1]

    index_repo = _RecordingRepo()
    indices_api.get_index_daily(
        _request(index_repo),
        symbol="^GSPC.US",
        days=_DAYS,
        start_date=None,
        end_date=None,
    )
    assert index_repo.window is not None, "indices 端点未走到指数日 K 窗口查询分支"
    index_end = index_repo.window[1]

    assert kline_end == US_PIN, f"kline 端点必须取美股市场当日 {US_PIN}, 实际 {kline_end}"
    assert index_end == US_PIN, f"indices 端点必须取美股市场当日 {US_PIN}, 实际 {index_end}"
    assert kline_end != cross_market, "kline 端点退化成 cross_market_today(): bug 反向回归"
    assert index_end != cross_market, "indices 端点退化成 cross_market_today(): bug 反向回归"


class _StopAfterToday(BaseException):
    """哨兵异常: 在 run_now 算完 today 之后立即中断管道, 便于在调用点收网。

    必须继承 BaseException 而非 Exception —— 管道把 integrity 扫描包在
    `try / except Exception` 里做软失败兜底 (见 daily_pipeline 中
    "integrity scan failed (soft, 按无坏数据处理)"), 用 Exception 子类会被
    当场吞掉, 哨兵传不出来, 本条用例就失去了调用点覆盖。
    """


def test_run_now_window_end_comes_from_cross_market_today(pinned_registry, monkeypatch, tmp_path):
    """G2 调用点护栏: run_now 里的 today 必须真的来自 pipeline_window_end()。

    不经 helper、直接跑真实 run_now: 把紧随 today 之后的 data_integrity 扫描
    换成"记下 today 再抛哨兵异常", 从而在调用点捕获取值。把实现改回
    _date.today() 或 cn_today() 时, 捕获到的是真实日期 != CN_PIN ⇒ 本条立刻
    变红 (报错形如 "捕获=某真实日期 vs 期望=哨兵")。
    """
    assert date.today() != CN_PIN
    captured: dict[str, date] = {}

    def _capture_today(*args, **kwargs):
        captured["today"] = kwargs.get("today")
        raise _StopAfterToday()

    monkeypatch.setattr(data_integrity, "scan_recent_integrity", _capture_today)
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


def test_cross_market_today_and_window_are_max_min_over_registry(pinned_registry, monkeypatch):
    """cross_market_today / cross_market_window 纯 helper 单测: 哨兵互异 + 等值断言。

    额外钉一条"新市场自动纳入": 注册 UTC+9 的 JP (领先 CN 一天) 后, 最大值必须
    立刻变成 JP_PIN —— 这正是本次迁移要消除的隐含前提; 若实现走了 CN_PROFILE
    这类常量引用, 这里会红。
    """
    assert CN_PIN > HK_PIN > US_PIN, "哨兵必须互异, 否则 max()/min() 断言退化成空断言"

    assert registry.cross_market_today() == CN_PIN
    assert registry.cross_market_today() == max(p.pinned for p in pinned_registry.values())

    start, end = registry.cross_market_window(_DAYS)
    assert end == CN_PIN
    assert start == US_PIN - timedelta(days=_DAYS), "窗口左端必须按最落后的市场回退"

    monkeypatch.setitem(registry._PROFILES, "JP", _PinnedProfile(market="JP", pinned=JP_PIN))
    assert JP_PIN > CN_PIN
    assert registry.cross_market_today() == JP_PIN, "新注册的 UTC+9 以东市场必须自动纳入最大值"


def test_pipeline_window_must_cover_markets_east_of_cn(monkeypatch, tmp_path):
    """盘后管道窗口右端必须覆盖「UTC+8 以东」市场的当日 —— 与真实系统日期无关。

    【CI 定时炸弹已随本次迁移拆除】同名用例最早在 qa_recovery_kit 里带
    `@pytest.mark.xfail(strict=True)`, 其 xfail 理由原文就是"修复方式: registry
    增加 max_market_today() 之类 helper, 管道改用它" —— 正是本次迁移。迁移一
    落地它必然转 XPASS, 而 strict=True 把 XPASS 当 CI 错误 ⇒ 合并那一刻 CI 就
    红, 而且红得让人误以为是迁移写错了。**必须进同一个 commit 去掉 xfail**,
    所以本条落地时**不带**任何 xfail 标记。

    【为什么四市场必须全钉死】旧版只钉 JP, CN/HK/US 用真实 profile ⇒ 真实日期
    恒 >= JP 钉值 ⇒ 断言 `>= JP` 恒真 (假绿, 拦不住任何东西)。现在
    CN/HK/US/JP 全部钉死且 JP 领先 CN 一天, 断言改成**等值** == JP_PIN。
    """
    _pin_registry(monkeypatch, {"CN": CN_PIN, "HK": HK_PIN, "US": US_PIN, "JP": JP_PIN})
    assert JP_PIN > CN_PIN > HK_PIN > US_PIN, "四市场哨兵必须互异且 JP 领先"

    captured: list[date] = []

    def fake_batch(*args, **kwargs):
        end_date = kwargs.get("end_date")
        captured.append(end_date.date() if hasattr(end_date, "date") else end_date)
        raise _StopAfterToday()

    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda *args, **kwargs: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *args, **kwargs: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: None,
    )
    capset = SimpleNamespace(has=lambda key: False)

    with pytest.raises(_StopAfterToday):
        daily_pipeline.run_now(repo, capset, override_start_date=CN_PIN)

    assert captured, "应发起日K范围拉取"
    assert captured[0] == JP_PIN, (
        f"窗口右端必须是跨市场最大值 (UTC+8 以东 JP 当日) {JP_PIN}, 实际 {captured[0]}"
    )
