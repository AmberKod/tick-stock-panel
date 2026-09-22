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

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import indices as indices_api
from app.api import kline as kline_api
from app.api import regime as regime_api
from app.api import us as us_api
from app.jobs import daily_pipeline
from app.market_time import cn_today
from app.markets import registry
from app.services import (
    abnormal_moves,
    data_integrity,
    extend_history,
    hk_data_adapter,
    instrument_sync,
    kline_sync,
    market_daily_sync,
    market_mainline,
    pipeline_jobs,
    regime_builder,
)
from app.services import repair_daily as repair_daily_svc

_DAYS = 120


def _pinned_clocks() -> tuple[date, date, date, date]:
    """返回 (CN, HK, US, JP) 四个互异的市场日期钉。

    CN > HK > US 依次递减 1 天 —— 互不相同才能判定"取到了哪个市场"、也才能
    让"取最大值"成为一条有鉴别力的断言; JP 比 CN 再领先 1 天 (UTC+9: 北京
    23:00 起 JP 日期领先 CN), 用于覆盖"UTC+8 以东"市场。
    四个值都刻意避开本机 date.today() —— 万一撞上就整体后移一年, 保证断言
    始终有鉴别力。

    【为什么必须同时避开 cn_today()】可分性判据要防的泄漏源不止一个: 除了
    date.today(), 还有**未被 patch、真实调用的 cn_today()** (北京日期)。
    UTC 容器上两者在一天里约 1/3 的时间不相等, 只查 date.today() 会漏掉
    "实现退回 cn_today()"这一路。漏判的后果是**静默绿**: 若某个钉值恰好等于
    真实的 cn_today(), 实现退回 cn_today() 时捕获值仍然 == CN_PIN, 护栏失效
    且没有任何人看得出来 —— 这正是本批用例从头到尾在消灭的失败模式。
    """
    anchor = date(2026, 3, 2)
    candidates = (
        anchor + timedelta(days=1),   # JP
        anchor,                       # CN
        anchor - timedelta(days=1),   # HK
        anchor - timedelta(days=2),   # US
    )
    if any(c in {date.today(), cn_today()} for c in candidates):
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

    def get_daily_batch(
        self, symbols: list[str], start: date, end: date, columns: list[str] | None = None
    ) -> pl.DataFrame:
        """记录 /daily-batch 的批量日 K 查询窗口 (跨市场共用同一个窗口)。"""
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
    # 未钉哨兵自检: 漏钉的市场会走真实 profile, max()/min() 取到真实当日 ⇒
    # 断言必然不等 ⇒ 仍然会红。本条不补覆盖缺口, 只把失败信息从
    # "assert 2026-03-02 == 2026-09-22" 变成自解释, 让加市场的人一眼看懂。
    unpinned = set(registry._PROFILES) - set(pins)
    assert not unpinned, (
        f"以下已注册市场未钉哨兵, max()/min() 会取到真实当日而退化: {sorted(unpinned)}"
    )
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


# ══════════════════════════════════════════════════════════════════════════
# 第二批 (mkt-clock-sites): 逐个站点的**真实调用点**护栏
#
# 与第一批同样的两条硬约束, 这里再复述一次以免后人只改一半:
# - 维度 A(断言形式): 有单一 symbol 上下文的站点 → 「记录入参的 spy」+ 结构断言
#   `seen == [symbol]`; 无单一 symbol 的跨市场站点 → 在 registry._PROFILES 层钉
#   互异哨兵, 断言取 max/min, 且哨兵必须与本机 date.today() 可分。
# - 维度 B(执行落点): 每条都跑进真实端点 / 服务 / 调度函数, 不测 helper。
#
# 变异验证: 把对应站点改回 date.today() 后, 下列每条都必须变红 (红灯证据见
# 提交说明)。跨时区站点 (us / indices-minute / live-candle) 的哨兵取 US_PIN,
# 与 CN_PIN 相差 2 天, 足以区分"取到哪个市场"。
# ══════════════════════════════════════════════════════════════════════════


def _spy_on(stub: _ClockStub, monkeypatch, module) -> None:
    """把某个端点模块的模块级 `profile_for_symbol` 换成记录入参的 spy。

    stub 复用同一个 seen 列表, 因此断言 `seen == [symbol]` 直接证明"端点按请求
    里的标的查了档案", 硬编码任何别的 symbol 都会在这里变红。
    """

    def _fake_profile_for_symbol(symbol: str) -> _PinnedProfile:
        stub.seen.append(symbol)
        return stub.profile

    monkeypatch.setattr(module, "profile_for_symbol", _fake_profile_for_symbol)


class _JsonRequest:
    """能被 `await request.json()` 的假 Request (kline /repair_daily 是 async 端点)。"""

    def __init__(self, body: dict, repo: object, caps: object) -> None:
        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=caps))

    async def json(self) -> dict:
        return self._body


class _AllCaps:
    """权限全开的权限集 (让 extend_history 走到除权因子分支以便收网)。"""

    def has(self, cap: object) -> bool:
        return True


class _FakeJobStore:
    """假 job_store: create 恒返回新 job, 其余全 no-op (不落真实任务文件)。"""

    def create(self, *args, **kwargs) -> tuple[str, bool]:
        return ("job-guard", True)

    def start(self, job_id: str) -> None:
        return None

    def succeed(self, job_id: str, result: object) -> None:
        return None

    def fail(self, job_id: str, error: str) -> None:
        return None

    def progress(self, *args, **kwargs) -> None:
        return None


def test_us_daily_default_end_uses_us_market_today(pinned_market_clock, monkeypatch):
    """us: /api/us/daily 默认截止日必须是**美股当天** —— 本批最要命的一处。

    改前是 `end or date.today()`: 美股接口却读服务器本地日期。UTC 容器在美东
    19:00-24:00 段本地日期已领先美东一天, 当日K被整根漏掉且不报错。
    """
    assert date.today() != MARKET_PIN
    _spy_on(pinned_market_clock, monkeypatch, us_api)

    captured: dict[str, object] = {}

    class _FakeYF:
        """假 yfinance provider: 记下真实拉取窗口后返回空帧 (端点随即早退)。"""

        def get_daily(self, symbols, start_time=None, end_time=None, asset_type=None):
            captured["symbols"] = symbols
            captured["start"] = start_time
            captured["end"] = end_time
            return pl.DataFrame()

    monkeypatch.setattr(us_api, "YFinanceProvider", lambda: _FakeYF())

    us_api.get_us_daily("AAPL.US", start=None, end=None, days=_DAYS)

    assert captured["end"] is not None, "端点未走到 provider 调用, 用例失去意义"
    assert captured["end"].date() == MARKET_PIN, (
        f"美股日K默认截止日必须是美股当天 {MARKET_PIN}, 实际 {captured['end'].date()}"
    )
    assert captured["start"].date() == MARKET_PIN - timedelta(days=_DAYS)
    assert captured["symbols"] == ["AAPL.US"]
    assert pinned_market_clock.profile.calls >= 1, "美股接口没有咨询市场档案, 说明又退回本地时钟"
    # G1 结构断言: 必须按"请求里的标的"查档案 (_norm 后为 AAPL.US)
    assert pinned_market_clock.seen == ["AAPL.US"]


def test_index_minute_default_day_uses_market_profile_today(pinned_market_clock, monkeypatch):
    """indices: /minute 默认交易日必须是指数所属市场的当天。

    美股指数 (^GSPC.US) 场景: 用宿主机日期会去拉一个美东尚不存在(或已过去)的
    交易日, 返回 0 行, 前端表现为"分时图全天空白"。
    """
    assert date.today() != MARKET_PIN
    captured: dict[str, object] = {}

    def _fake_minute(symbol, day, asset_type=None):
        captured["symbol"] = symbol
        captured["day"] = day
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "fetch_minute_single", _fake_minute)

    repo = _RecordingRepo()
    indices_api.get_index_minute(_request(repo), symbol="^GSPC.US", trade_date=None)

    assert captured["day"] == MARKET_PIN, (
        f"指数分钟K默认日必须是市场当天 {MARKET_PIN}, 实际 {captured['day']}"
    )
    assert captured["symbol"] == "^GSPC.US"
    assert pinned_market_clock.profile.calls >= 1, "指数端点没有咨询市场档案"
    assert pinned_market_clock.seen == ["^GSPC.US"]


def test_daily_batch_end_uses_cross_market_today(pinned_registry):
    """kline: /daily-batch 一次查多只标的(可能跨市场), 右端取跨市场最大值。

    判据: US 哨兵严格落后于 CN, 所以"右端 == CN_PIN 且 != US_PIN"同时排除了
    "退回宿主机 date.today()"与"退化成单一市场时钟"两种改坏方式。
    """
    assert date.today() != CN_PIN
    assert CN_PIN > HK_PIN > US_PIN, "哨兵必须互异, 否则本用例失去鉴别力"

    repo = _RecordingRepo()
    kline_api.get_daily_batch(_request(repo), {"symbols": ["AAPL.US", "600000.SH"], "days": 12})

    assert repo.window is not None, "批量端点未走到日 K 窗口查询分支, 用例失去意义"
    _start, end = repo.window
    assert end == CN_PIN, f"批量端点窗口右端必须是跨市场最大值 {CN_PIN}, 实际 {end}"
    assert end != US_PIN, "批量端点退化成单一市场时钟: 会漏掉领先市场的当日K"


def test_live_candle_injection_uses_market_profile_today(pinned_market_clock):
    """kline: 实时蜡烛新鲜度必须用**标的所属市场**的当天判断。

    改前 `enriched_date != date.today()`: 美股场景下本地日期与美东日期差一天时,
    会把**真实存在**的当日实时蜡烛误判成陈旧数据而整根丢弃 (当日K永远缺一根)。
    """
    assert date.today() != MARKET_PIN

    df_today = pl.DataFrame({
        "symbol": ["AAPL.US"], "close": [123.0], "open": [120.0],
        "high": [125.0], "low": [119.0], "volume": [1000],
        "amount": [123000.0], "change_pct": [0.02],
    })
    qs = SimpleNamespace(get_enriched_today=lambda: (df_today, MARKET_PIN))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=qs)))
    rows = [{"date": str(MARKET_PIN - timedelta(days=1)), "symbol": "AAPL.US", "close": 120.0}]

    out = kline_api._maybe_inject_live_candle(req, "AAPL.US", list(rows), asset_type="stock")

    assert len(out) == 2, f"美股当日实时蜡烛被误判为陈旧而丢弃: {out}"
    assert str(out[-1]["date"]) == str(MARKET_PIN)
    assert out[-1]["is_live"] is True
    assert pinned_market_clock.profile.calls >= 1, "新鲜度判断没有咨询市场档案"
    assert pinned_market_clock.seen == ["AAPL.US"]


def test_live_candle_injection_still_skips_stale_cache(pinned_market_clock):
    """反向护栏: 缓存日期早于市场当天时**必须**仍然跳过注入 (防过度纠偏)。"""
    assert date.today() != MARKET_PIN
    stale = MARKET_PIN - timedelta(days=1)

    df_today = pl.DataFrame({"symbol": ["AAPL.US"], "close": [123.0]})
    qs = SimpleNamespace(get_enriched_today=lambda: (df_today, stale))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=qs)))
    rows = [{"date": str(stale), "symbol": "AAPL.US", "close": 120.0}]

    out = kline_api._maybe_inject_live_candle(req, "AAPL.US", list(rows), asset_type="stock")
    assert out == rows, "陈旧缓存被当成当日注入, 会产生重复蜡烛"


def test_repair_daily_endpoint_boundary_is_cross_market_today(pinned_registry):
    """kline: /repair_daily "起始日不能晚于今天"的上界必须是跨市场当天。

    【为什么是成对断言】单点断言无法区分"上界 = 跨市场当天"和"上界 = 本机日期":
    两者只在本机日期恰好等于哨兵时不可分, 而 _pinned_clocks 保证了不等。改成
    `_date.today()` 后, CN_PIN 与 CN_PIN+1 必然被**同时放行或同时拦截**, 至少
    一条立刻变红 —— 于是上界被精确钉死在 cross_market_today()。
    """
    assert date.today() != CN_PIN
    repo = _RecordingRepo()

    ok_req = _JsonRequest({"start_date": CN_PIN.isoformat()}, repo, _NoCaps())
    with pytest.raises(HTTPException) as accepted:
        asyncio.run(kline_api.repair_daily(ok_req))
    assert accepted.value.status_code == 403, (
        f"上界应放行 start_date={CN_PIN} (403=已越过日期校验, 停在权限校验), "
        f"实际 {accepted.value.status_code}"
    )

    over = CN_PIN + timedelta(days=1)
    bad_req = _JsonRequest({"start_date": over.isoformat()}, repo, _NoCaps())
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(kline_api.repair_daily(bad_req))
    assert rejected.value.status_code == 400, (
        f"上界应拦截 start_date={over} (400=起始日越界), 实际 {rejected.value.status_code}"
    )


def test_repair_daily_service_boundary_is_cross_market_today(pinned_registry, monkeypatch):
    """services/repair_daily: 与 API 层必须用同一个时钟, 否则两层结论打架。

    API 放行而服务层回退(或反之)会让同一请求在不同时区宿主机上给出不同结果,
    所以这里同样用成对断言把上界钉死。
    """
    assert date.today() != CN_PIN
    calls: list[date] = []

    def _fake_run_now(repo, capset, on_progress=None, override_start_date=None) -> dict:
        calls.append(override_start_date)
        return {"status": "ok"}

    monkeypatch.setattr(daily_pipeline, "run_now", _fake_run_now)

    ok = repair_daily_svc.run_repair_daily(_RecordingRepo(), _NoCaps(), CN_PIN)
    assert "error" not in ok, f"上界应放行 start_date={CN_PIN}, 实际 {ok}"
    assert calls == [CN_PIN], "放行的请求必须原样透传给 run_now"

    over = CN_PIN + timedelta(days=1)
    bad = repair_daily_svc.run_repair_daily(_RecordingRepo(), _NoCaps(), over)
    assert bad.get("error") == "起始日期不能晚于今天", f"上界应拦截 start_date={over}, 实际 {bad}"
    assert calls == [CN_PIN], "被拦截的请求不得进入 run_now"


def test_extend_history_adj_end_uses_cross_market_today(pinned_registry, monkeypatch, tmp_path):
    """services/extend_history: 除权因子区间上界取跨市场当天 (不是本机日期)。

    标的池 = CN_Equity_A + watchlist/instruments 兜底 (可能含 .HK/.US), 无单一
    symbol 上下文 ⇒ 跨市场口径。上界取最大值只会多读不会截断。
    """
    assert date.today() != CN_PIN
    captured: dict[str, object] = {}

    def _fake_adj(*args, **kwargs):
        captured["end_time"] = kwargs.get("end_time")
        raise _StopAfterToday()

    monkeypatch.setattr(kline_sync, "sync_adj_factor", _fake_adj)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", lambda *a, **k: 0)
    monkeypatch.setattr(extend_history, "_refresh_single_view", lambda *a, **k: None)
    monkeypatch.setattr(extend_history, "_invalidate", lambda *a, **k: None)
    monkeypatch.setattr(extend_history, "_resolve_universe", lambda capset: ["600000.SH"])

    repo = SimpleNamespace(
        earliest_daily_date=lambda: CN_PIN - timedelta(days=400),
        store=SimpleNamespace(data_dir=tmp_path),
    )

    with pytest.raises(_StopAfterToday):
        extend_history.run_extend_history(repo, _AllCaps(), 30, "day")

    assert captured["end_time"] is not None, "未走到除权因子分支, 用例失去意义"
    assert captured["end_time"].date() == CN_PIN, (
        f"除权因子区间上界必须是跨市场当天 {CN_PIN}, 实际 {captured['end_time'].date()}"
    )


def test_regime_recompute_end_uses_market_today(pinned_registry, monkeypatch, tmp_path):
    """regime: /recompute 是按 market 分别计算的, 右端必须是该市场当天。

    【加值断言】同一端点用 market=us 与 market=cn 各跑一次, 两个期望值 (US_PIN /
    CN_PIN) 互异 —— 这同时排除了"取常量"和"取跨市场最大值"两种改法: 前者两次
    同值, 后者两次都等于 CN_PIN。
    """
    assert date.today() != CN_PIN
    assert US_PIN != CN_PIN, "US/CN 哨兵必须互异, 否则加值断言退化"

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        regime_builder, "earliest_enriched_date",
        lambda repo, market=None: CN_PIN - timedelta(days=10),
    )

    def _fake_batch(repo, start, end, market=None):
        captured["end"] = end
        captured["market"] = market
        return pl.DataFrame()

    monkeypatch.setattr(regime_builder, "run_regime_batch", _fake_batch)
    monkeypatch.setattr(regime_builder, "refresh_phase_labels", lambda *a, **k: 0)
    monkeypatch.setattr(regime_builder, "upsert_regime_history", lambda *a, **k: None)
    monkeypatch.setattr(market_mainline, "compute_mainline_range", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(regime_api, "invalidate_regime_cache", lambda: None)

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    )))

    regime_api.regime_recompute(req, market="us", start=None, end=None)
    assert captured["market"] == "us"
    assert captured["end"] == US_PIN, f"us 右端必须是美股当天 {US_PIN}, 实际 {captured['end']}"

    regime_api.regime_recompute(req, market="cn", start=None, end=None)
    assert captured["market"] == "cn"
    assert captured["end"] == CN_PIN, f"cn 右端必须是A股当天 {CN_PIN}, 实际 {captured['end']}"


def test_abnormal_overview_includes_today_uses_cn_market_today(monkeypatch):
    """abnormal_moves: 新鲜度基准必须是**北京当天** (本模块是 A 股专属口径)。

    判为 A 类的依据: 阈值按沪深/创业板/科创板/北交所规则, 基准指数取
    CN_PROFILE.bench_rt_candidates —— 没有港美股的事。改成市场日期(此处即北京
    日期)后, UTC 容器在北京 00:00-08:00 段的误判被消除: 那段时间本地日期落后
    一天, 会把"昨日收盘已入 enriched"判成未入, 于是把昨日 rt_pct 又叠加一次
    ⇒ 异动接近度虚高。

    【成对断言】cache_date == CN_PIN 判 True、CN_PIN-1 判 False: 改成
    date.today() 后两者必然同时为 True 或同时为 False (本机日期 != CN_PIN),
    至少一条变红。
    """
    assert date.today() != CN_PIN
    sentinel = _PinnedProfile(market="CN", pinned=CN_PIN)
    monkeypatch.setattr(abnormal_moves, "CN_PROFILE", sentinel)
    monkeypatch.setattr(abnormal_moves, "_hist_cache", {})

    df = pl.DataFrame({
        "symbol": ["600000.SH"], "name": ["浦发银行"], "close": [10.0],
        "change_pct": [0.05], "deviate_3d": [0.0], "deviate_10d": [0.0],
        "deviate_30d": [0.0],
    })

    repo_fresh = SimpleNamespace(get_enriched_latest=lambda: (df, CN_PIN))
    fresh = abnormal_moves.build_overview(repo_fresh, None)
    assert fresh["includes_today"] is True, (
        f"北京当日收盘已入 enriched (cache_date={CN_PIN}), 必须判 includes_today=True"
    )

    monkeypatch.setattr(abnormal_moves, "_hist_cache", {})
    repo_stale = SimpleNamespace(get_enriched_latest=lambda: (df, CN_PIN - timedelta(days=1)))
    stale = abnormal_moves.build_overview(repo_stale, None)
    assert stale["includes_today"] is False, (
        f"cache_date={CN_PIN - timedelta(days=1)} 早于北京当天, 必须判 includes_today=False"
    )
    assert sentinel.calls >= 1, "新鲜度判断没有咨询市场档案, 说明又退回本地时钟"


def test_scheduled_hk_us_window_uses_cross_market_window(pinned_registry, monkeypatch, tmp_path):
    """daily_pipeline: 港美日K调度窗口 = cross_market_window(365) —— 首个生产调用方。

    窗口语义是**超集**: 左端 = 最落后市场 - 365d, 右端 = 最靠前市场当天。断言
    两端分别等于 min/max 哨兵, 一次排除三种改坏方式 —— 退回宿主机 date.today()
    (两端同值且不是哨兵)、只取 HK 单一市场 (右端 = HK_PIN)、两端都取同一市场
    (左端 != US_PIN-365)。
    """
    assert CN_PIN > HK_PIN > US_PIN, "哨兵必须互异, 否则 min/max 断言退化"
    captured: dict[str, object] = {}

    def _fake_sync(**kwargs):
        captured["start_date"] = kwargs.get("start_date")
        captured["end_date"] = kwargs.get("end_date")
        captured["market"] = kwargs.get("market")
        raise _StopAfterToday()

    monkeypatch.setattr(market_daily_sync, "run_market_daily_sync", _fake_sync)
    monkeypatch.setattr(hk_data_adapter, "sync_hk_instruments", lambda *a, **k: 0)
    monkeypatch.setattr(hk_data_adapter, "sync_us_instruments", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline_jobs, "job_store", _FakeJobStore())
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda job_id: True)
    monkeypatch.setattr(pipeline_jobs, "release_run_slot", lambda job_id: None)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(_StopAfterToday):
        daily_pipeline._run_market_daily_scheduled(repo, _NoCaps(), "HK")

    assert captured["market"] == "HK"
    assert captured["start_date"] is not None, "未走到市场日K同步, 用例失去意义"
    assert captured["start_date"].date() == US_PIN - timedelta(days=365), (
        f"窗口左端必须按最落后市场回退 {US_PIN - timedelta(days=365)}, "
        f"实际 {captured['start_date'].date()}"
    )
    assert captured["end_date"].date() == CN_PIN, (
        f"窗口右端必须是跨市场最大值 {CN_PIN}, 实际 {captured['end_date'].date()}"
    )
    assert captured["end_date"].date() != HK_PIN, "右端退化成单一市场时钟: 会漏掉领先市场当日K"


# ══════════════════════════════════════════════════════════════════════════
# 第三批 (mkt-clock-mainline): #18 市场时钟改造的收尾 — regime.py /mainline/recompute
#
# 背景事实(推翻早前"B类保留"的判断):
# - market 参数就在端点签名上 (regime.py:307), 不存在"无 market 可传";
# - earliest (regime.py:317) 已按 market 计算 ⇒ "左端市场口径 + 右端宿主机
#   口径"的混用才是改造前的现状, 改成市场当天是消除混口径;
# - compute_mainline_range 只用传入的 start/end, 无 market 参数需求;
# - 同一个 compute_mainline_range 被两个端点喂: /recompute (:167) 早已是
#   get_profile(market.upper()).today(), 只有 /mainline/recompute 停在
#   宿主机 date.today() —— 两种口径并存才是 bug 的实质。
#
# 沿用两条硬约束: 维度 A(互异哨兵, 断言值与真实可达值可分) + 维度 B(跑真实
# 端点 mainline_recompute, 不测 helper)。变异验证: 把 :333 改回 date.today()
# 后下列用例必须变红 (红灯证据见提交说明)。
# ══════════════════════════════════════════════════════════════════════════


def test_mainline_recompute_end_uses_market_today(pinned_registry, monkeypatch, tmp_path):
    """/mainline/recompute: 右端必须是该 market 的当天, 与 /recompute 同口径。

    【加值断言】market=us 与 market=cn 各跑一次, 期望值互异 (US_PIN / CN_PIN)
    —— 同时排除三种改法: 退回宿主机 date.today() (两次同值且非哨兵)、取常量、
    取跨市场最大值 (两次都等于 CN_PIN, us 那次必红)。
    """
    assert date.today() != CN_PIN
    assert US_PIN != CN_PIN, "US/CN 哨兵必须互异, 否则加值断言退化"

    captured: list[date] = []

    def _fake_range(repo, data_dir, start, end, kind="concept", **kwargs):
        captured.append(end)
        return pl.DataFrame()

    monkeypatch.setattr(
        regime_builder, "earliest_enriched_date",
        lambda repo, market=None: CN_PIN - timedelta(days=10),
    )
    monkeypatch.setattr(market_mainline, "compute_mainline_range", _fake_range)
    monkeypatch.setattr(market_mainline, "upsert_mainline_history", lambda *a, **k: None)

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    )))

    regime_api.mainline_recompute(req, market="us")
    regime_api.mainline_recompute(req, market="cn")

    assert len(captured) >= 2, "端点未走到主线计算分支, 用例失去意义"
    # concept + industry 两轮, 取每个 market 的首次出现顺序
    us_ends = {captured[0], captured[1]}
    cn_ends = {captured[2], captured[3]}
    assert us_ends == {US_PIN}, f"us 右端必须是美股当天 {US_PIN}, 实际 {sorted(us_ends)}"
    assert cn_ends == {CN_PIN}, f"cn 右端必须是A股当天 {CN_PIN}, 实际 {sorted(cn_ends)}"


def test_mainline_recompute_must_not_use_cross_market_today(pinned_registry, monkeypatch, tmp_path):
    """反向护栏: 本端点按 market 分别计算, **不得**改用 cross_market_today()。

    cross_market_today() 已存在且"看起来更统一", 但它是盘后管道的超集语义;
    /mainline/recompute 的 earliest 已按 market 分流, 右端若抬到跨市场最大值,
    美股会多算一个美东尚不存在的未来日。判据: US 哨兵严格落后 CN, 所以
    "us 右端 == CN_PIN" 就是退化的充要信号。
    """
    assert US_PIN < CN_PIN, "US 哨兵必须严格落后 CN, 否则本用例无法识别退化"

    captured: list[date] = []

    def _fake_range(repo, data_dir, start, end, kind="concept", **kwargs):
        captured.append(end)
        return pl.DataFrame()

    monkeypatch.setattr(
        regime_builder, "earliest_enriched_date",
        lambda repo, market=None: CN_PIN - timedelta(days=10),
    )
    monkeypatch.setattr(market_mainline, "compute_mainline_range", _fake_range)
    monkeypatch.setattr(market_mainline, "upsert_mainline_history", lambda *a, **k: None)

    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    )))

    regime_api.mainline_recompute(req, market="us")

    assert captured, "端点未走到主线计算分支, 用例失去意义"
    assert all(end == US_PIN for end in captured), (
        f"us 右端必须停在美股当天 {US_PIN}; 出现 {sorted(set(captured))} —— "
        f"若含 {CN_PIN} 即退化成 cross_market_today()"
    )


# ══════════════════════════════════════════════════════════════════════════
# 第四批 (mkt-clock-incremental): #18 市场时钟改造收官 — 盘后主线补齐上界
#
# _compute_mainline_step 此前调用 compute_mainline_incremental 时不传 today,
# 走服务层缺省的宿主机 date.today() (market_mainline.py:288, 判 B 类未动)。
# 管道若在北京 00:00-08:00 段运行 (UTC 前一日 16:00-24:00), 本地日期比中国
# 日期落后一天 ⇒ `d <= today` 漏补中国当日主线。
#
# 改法: 调用方显式传 today=registry.cross_market_today() (超集口径, 与
# run_now 的 today 同源); 服务层缺省值不动 (保持"手动触发传 None"的契约)。
#
# 沿用两条硬约束: 维度 A(四市场互异哨兵, JP 领先) + 维度 B(跑真实调用点
# _compute_mainline_step, 不测 compute_mainline_incremental 这个 helper)。
# 变异验证: 把 :763-766 改回不传 today 后, 下列用例必须变红。
# ══════════════════════════════════════════════════════════════════════════


def test_mainline_incremental_step_today_is_cross_market_max(pinned_registry, monkeypatch, tmp_path):
    """盘后主线补齐: _compute_mainline_step 传入的 today 必须是跨市场最大值。

    【四市场全钉死 + JP 领先】注册 UTC+9 的 JP 且 JP_PIN > CN_PIN, 期望值取
    JP_PIN —— 一次排除三种退化:
      - 退回 date.today() / 服务层缺省: 捕获到真实日期 != JP_PIN ⇒ 红;
      - 改用单市场 profile.today() (CN/HK/US 任一): 最大也只能取到 CN_PIN
        < JP_PIN ⇒ 红;
      - 只遍历部分市场 (硬编码三市场清单): JP_PIN 同样取不到 ⇒ 红。
    """
    monkeypatch.setitem(registry._PROFILES, "JP", _PinnedProfile(market="JP", pinned=JP_PIN))
    assert JP_PIN > CN_PIN > HK_PIN > US_PIN, "四市场哨兵必须互异且 JP 领先"
    assert date.today() != JP_PIN

    captured: list[object] = []

    def _fake_incremental(repo, data_dir, *, today=None, kind="concept", **kwargs):
        captured.append(today)
        return pl.DataFrame()

    monkeypatch.setattr(market_mainline, "compute_mainline_incremental", _fake_incremental)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    daily_pipeline._compute_mainline_step(
        repo=repo, emit=lambda *a, **k: None, skipped=[], stage_errors=[],
    )

    assert len(captured) == 2, (
        f"应跑 concept+industry 两轮增量补齐, 实际 {len(captured)} 轮 —— "
        f"若为 0, 说明调用点未走到主线分支, 用例失去意义"
    )
    assert all(t == JP_PIN for t in captured), (
        f"补齐上界必须是跨市场最大值 {JP_PIN} (UTC+8 以东 JP 当日), "
        f"实际 {sorted(set(map(str, captured)))}"
    )
    assert not any(t is None for t in captured), (
        "捕获到 today=None: 调用点没有显式传值, 走了服务层缺省 date.today()"
    )
