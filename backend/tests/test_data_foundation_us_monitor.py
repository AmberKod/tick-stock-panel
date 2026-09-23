"""数据地基批 — 美股 provider 根治 (#5) + 港美监控时段门控 (#6)。

二维验收:
- 维度 A (断言形式): 互异哨兵 (四市场 pinned sessions + pinned now, 与真实
  系统时间无关), 注册断言用字面量。
- 维度 B (执行落点): 跑真实的 ``_evaluate_monitors`` / ``SinaUSProvider.get_daily``
  / registry 默认源解析, 不测私有 helper。

变异锚点 (见各用例 docstring): 默认源改回 yfinance / 删 403 marker /
港美门控改回 A 股口径, 对应用例必须红。
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl

from app.data_providers import registry as provider_registry
from app.data_providers.base import ProviderCapabilities
from app.markets import registry
from app.markets.profile import TradingSession

_BACKEND = Path(__file__).resolve().parents[3]


# ══════════════════════════════════════════════════════════════════════════
# 任务 A: 美股 provider 根治
# ══════════════════════════════════════════════════════════════════════════


def test_us_default_provider_is_sina():
    """A2: US 默认源必须是 sina (变异锚: 改回 yfinance → 本条红)。"""
    provider = provider_registry.get_default_provider("US")
    assert provider.name == "sina", (
        f"US 默认源必须是 sina (新浪主源), 实际 {provider.name!r} —— "
        "Yahoo 403 时空烧且静默返空, 默认源不能再依赖 yfinance"
    )
    # yfinance 仍保留注册 (兜底/实时), 只是不再是默认
    assert provider_registry._MARKET_PROVIDERS["US"]["yfinance"] is not None


def test_sina_us_provider_get_daily_passes_symbols_and_filters(monkeypatch):
    """A1: get_daily 逐标的调 fetch_us_daily_akshare 并按 [start,end] 过滤。"""
    from app.data_providers.sina_us_provider import SinaUSProvider
    from app.services import hk_data_adapter

    calls: list[str] = []

    def _fake_fetch(symbol: str) -> pl.DataFrame:
        calls.append(symbol)
        rows = [
            {"symbol": symbol, "date": datetime(2026, 9, 20), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100.0, "amount": 150.0},
            {"symbol": symbol, "date": datetime(2026, 9, 21), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "volume": 100.0, "amount": 150.0},
        ]
        return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Datetime("us")))

    monkeypatch.setattr(hk_data_adapter, "fetch_us_daily_akshare", _fake_fetch)

    provider = SinaUSProvider()
    df = provider.get_daily(
        ["AAPL.US", "MSFT.US"],
        start_time=datetime(2026, 9, 21),
        end_time=datetime(2026, 9, 21),
        asset_type="stock",
    )

    assert calls == ["AAPL.US", "MSFT.US"], "必须逐标的把 symbol 原样传给适配层"
    assert set(df["symbol"].to_list()) == {"AAPL.US", "MSFT.US"}
    assert df["date"].dt.date().unique().to_list() == [date(2026, 9, 21)], (
        "窗口外日期必须被过滤掉"
    )


def test_sina_us_provider_partial_failure_returns_rest(monkeypatch):
    """A1: 单标的失败 (适配层返回空 df) 不抛错, 其余标的正常返回。"""
    from app.data_providers.sina_us_provider import SinaUSProvider
    from app.services import hk_data_adapter

    def _fake_fetch(symbol: str) -> pl.DataFrame:
        if symbol == "BAD.US":
            return pl.DataFrame()  # 适配层约定: 失败返回空 df 不抛
        return pl.DataFrame([{
            "symbol": symbol, "date": datetime(2026, 9, 21),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            "volume": 100.0, "amount": 150.0,
        }]).with_columns(pl.col("date").cast(pl.Datetime("us")))

    monkeypatch.setattr(hk_data_adapter, "fetch_us_daily_akshare", _fake_fetch)
    df = SinaUSProvider().get_daily(
        ["BAD.US", "GOOD.US"], datetime(2026, 9, 1), datetime(2026, 9, 30), "stock",
    )
    assert df["symbol"].unique().to_list() == ["GOOD.US"]


def test_yf_rate_limit_markers_include_403():
    """A3: 403/Forbidden 必须并入熔断 marker (变异锚: 删掉 → 本条红)。"""
    from app.data_providers.yfinance_provider import _yf_is_rate_limited

    assert _yf_is_rate_limited(Exception("HTTP Error 403")), "403 必须触发熔断计数"
    assert _yf_is_rate_limited(Exception("Forbidden for url")), "Forbidden 必须触发熔断计数"
    assert _yf_is_rate_limited(Exception("429 Too Many Requests")), "原有 429 判定不得回归"
    assert not _yf_is_rate_limited(Exception("connection reset")), "非限流错误不得误判"


def test_yf_circuit_opens_on_repeated_403(monkeypatch):
    """A3 (行为级): 连续 403 达阈值后熔断打开, 后续请求本地快速失败。"""
    from app.data_providers import yfinance_provider as mod

    mod.yf_circuit_reset()

    def _fake_history(**kwargs):
        raise Exception("HTTP Error 403: Forbidden")

    fake_yf = SimpleNamespace(Ticker=lambda sym: SimpleNamespace(history=_fake_history))
    monkeypatch.setattr(mod, "_try_import_yf", lambda: fake_yf)

    provider = mod.YFinanceProvider()
    for _ in range(mod._YF_FAILURES_TO_OPEN):
        provider.get_daily(["AAPL.US"], None, None, "stock")

    assert mod.yf_circuit_blocked(), "连续 403 必须打开熔断 (此前只 warning 后空烧)"
    mod.yf_circuit_reset()


# ══════════════════════════════════════════════════════════════════════════
# 任务 B: 港美监控时段门控
# ══════════════════════════════════════════════════════════════════════════


class _PinnedProfile:
    """钉住 now()/today()/sessions 的市场档案替身 (哨兵时钟)。"""

    def __init__(self, market: str, now_dt: datetime) -> None:
        self.market = market
        self._now = now_dt
        # sessions 与真实 profile 同构: 给一个覆盖当前时刻的时段
        self.sessions = (TradingSession(start=time(9, 30), end=time(16, 0)),)
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return self._now

    def today(self) -> date:
        return self._now.date()


def _pin_market_clocks(monkeypatch, *, cn: datetime, hk: datetime, us: datetime) -> None:
    """三市场互异哨兵时钟 (注入 registry._PROFILES, 打穿 is_continuous_trading)。"""
    profiles = {
        "CN": _PinnedProfile("CN", cn),
        "HK": _PinnedProfile("HK", hk),
        "US": _PinnedProfile("US", us),
    }
    unpinned = set(registry._PROFILES) - set(profiles)
    assert not unpinned, f"以下市场未钉哨兵: {sorted(unpinned)}"
    for market, profile in profiles.items():
        monkeypatch.setitem(registry._PROFILES, market, profile)


class _GateEngine:
    """最小监控引擎桩: 记录 abnormal 评估是否到达、传入行数。"""

    name = "stub"
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        self.rule_count = 1
        self.abnormal_rows: list[list[dict]] = []

    def has_rule_type(self, kind: str) -> bool:
        return kind == "abnormal"

    def has_asset_rules(self, _: str) -> bool:
        return False

    def min_abnormal_closeness(self) -> float:
        return 0.0

    def evaluate_abnormal(self, rows: list[dict]) -> list[dict]:
        self.abnormal_rows.append(list(rows))
        return []

    def evaluate(self, *a: Any, **k: Any) -> list[dict]:
        return []

    def evaluate_sectors(self, *a: Any, **k: Any) -> list[dict]:
        return []

    def set_name_map(self, *a: Any, **k: Any) -> None:
        return None

    def consume_strategy_result_updates(self) -> bool:
        return False


def _make_service(tmp_path: Path, engine: _GateEngine) -> Any:
    """构造足够跑 _evaluate_monitors 的 QuoteService 替身 (最小 state)。"""
    from app.services.quote_service import QuoteService

    svc = QuoteService.__new__(QuoteService)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    svc._repo = repo
    svc._app_state = SimpleNamespace(
        repo=repo, monitor_engine=engine,
    )
    svc._abnormal_last_eval = 0.0
    svc._alerts = []
    svc._enrich_alerts_ext = lambda alerts: None
    svc.get_enriched_today = lambda: (pl.DataFrame(), None)
    svc.get_index_quotes = lambda: pl.DataFrame()
    svc._inject_intraday_signals = lambda df, eng, at: df
    svc._inject_sealed_vol = lambda df, d: df
    svc._format_extension_notifications = lambda events: events
    svc.notify_strategy_results_updated = lambda: None
    # 下发链 (港美独立入口也走同一条): 桩掉外发通道, 只验证是否到达
    svc._broadcast_alerts = lambda alerts: None
    svc._maybe_send_system_notifications = lambda alerts: None
    svc._maybe_send_webhook = lambda events, engine: None
    return svc


def _write_enriched(tmp_path: Path, symbol: str, days: list[date]) -> None:
    """造 enriched 分区 (港美门控只扫 symbol/date 两列)。"""
    path = tmp_path / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": [symbol] * len(days), "date": days}).write_parquet(path)


def _pin_hk_us_snapshots(monkeypatch, applied: list[str]) -> None:
    """把港美异动快照构建替换为记录市场名的桩 (空行 → 只验证是否到达)。"""
    from app.services import hk_us_abnormal

    def _fake_build(data_dir, market: str, **kwargs):
        applied.append(market)
        return {"rows": []}

    monkeypatch.setattr(hk_us_abnormal, "build_hk_us_abnormal_overview", _fake_build)
    # A 股异动快照: 记为 "CN"
    from app.services import abnormal_moves

    monkeypatch.setattr(
        abnormal_moves, "build_overview",
        lambda *a, **k: {"rows": []},
    )


MON = date(2026, 9, 21)  # 周一


def test_a_share_polling_no_longer_builds_hk_us_snapshots(monkeypatch, tmp_path):
    """B3 迁移: 港美异动段已从 A 股轮询摘出 —— 任何时钟下都不再由它构建。

    原两条时段断言 ("美东 10:00 → 构建 US"、"HK 盘中 → 只构建 HK") 随判据
    作废: 港美是日线收盘口径, 由独立 job 触发 (见下列 evaluate_hk_us_monitors
    用例)。本条守住"摘干净"这件事本身 —— 变异锚: 把港美段塞回
    _evaluate_monitors → 本条红。
    """
    # 场景 1: 美东 10:00 (美股盘中) —— 旧门控会放行 US
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 22, 0),
        hk=datetime(2026, 9, 21, 22, 0),
        us=datetime(2026, 9, 21, 10, 0),
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)
    svc = _make_service(tmp_path, _GateEngine())
    svc._evaluate_monitors(pl.DataFrame(), None)
    assert applied == [], f"A 股轮询不得再构建港美快照; 实际 {applied}"

    # 场景 2: HK 盘中 + US 闭市 —— 旧门控会放行 HK
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 10, 0),
        hk=datetime(2026, 9, 21, 10, 0),
        us=datetime(2026, 9, 21, 22, 0),
    )
    applied.clear()
    svc = _make_service(tmp_path, _GateEngine())
    svc._evaluate_monitors(pl.DataFrame(), None)
    assert applied == [], f"A 股轮询不得再构建港美快照; 实际 {applied}"


# ── 港美独立入口 (日线收盘口径) ────────────────────────────────────────────
# 时钟口径: 北京 08:35 (US job) = 美东前一日 20:35; 北京 18:35 (HK job)。
# 周一 08:35 北京 ⇒ 美东周日 20:35, profile.today() = 周日, 而刚同步写入的是
# **上周五**的会话 ⇒ 门控用"落后天数 ≤ 容差"而非日期相等 (等值判据会每 5 场丢 1 场)。
MON = date(2026, 9, 21)      # 周一
FRI = date(2026, 9, 18)      # 上周五 (本批同步写入的会话)
SUN = date(2026, 9, 20)      # 周日 (周一 08:35 北京对应的美东日历日)


def test_hk_us_monitor_allows_after_session_close(monkeypatch, tmp_path):
    """核心可分性 A: 美股**已收盘** + as_of 在容差内 → 新门控放行。

    同时断言 is_continuous_trading("US") is False —— 即**旧门控在此刻必然拒绝**,
    两条判据在此场景上正交可分 (不是同一个条件的两种写法)。
    """
    _write_enriched(tmp_path, "AAPL.US", [FRI])
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 8, 35),    # 北京 08:35 (A 股闭市)
        hk=datetime(2026, 9, 21, 8, 35),
        us=datetime(2026, 9, 20, 20, 35),   # 美东周日 20:35 → 非盘中
    )
    assert registry.is_continuous_trading("US") is False, "场景前提: 旧门控此刻会拒绝"
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    svc = _make_service(tmp_path, _GateEngine())
    result = svc.evaluate_hk_us_monitors("US")

    assert result["status"] == "ok", result
    assert applied == ["US"], f"同步后 (会话已收盘) 必须评估 US; 实际 {applied}"


def test_hk_us_monitor_rejects_intraday_session(monkeypatch, tmp_path):
    """核心可分性 B: 美股**盘中** (as_of 恒为 T-1) → 新门控必须拒绝。

    这条是防退回旧设计的关键: 旧门控在美股盘中会放行 (is_continuous_trading
    = True), 但 parquet 里根本没有当日会话, 评估等于拿 T-1 冒充当日收盘。
    """
    _write_enriched(tmp_path, "AAPL.US", [FRI])   # 盘中 parquet 仍是上周五
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 22, 0),
        hk=datetime(2026, 9, 21, 22, 0),
        us=datetime(2026, 9, 21, 10, 0),   # 美东周一 10:00 → 盘中
    )
    assert registry.is_continuous_trading("US") is True, "场景前提: 旧门控此刻会放行"
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    svc = _make_service(tmp_path, _GateEngine())
    result = svc.evaluate_hk_us_monitors("US")

    assert result["status"] == "skipped", result
    assert applied == [], f"美股盘中 (parquet=T-1) 不得评估; 实际 {applied}"
    assert "连续竞价时段" in (result["reason"] or ""), result


def test_hk_us_monitor_rejects_stale_snapshot(monkeypatch, tmp_path):
    """守卫生效性: 会话已收盘但快照落后超过容差 (US=3 天) → 拒绝, 不基于陈旧数据告警。"""
    _write_enriched(tmp_path, "AAPL.US", [date(2026, 9, 14)])   # 落后 6 天
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 8, 35),
        hk=datetime(2026, 9, 21, 8, 35),
        us=datetime(2026, 9, 20, 20, 35),
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    svc = _make_service(tmp_path, _GateEngine())
    result = svc.evaluate_hk_us_monitors("US")

    assert result["status"] == "skipped", result
    assert applied == [], f"陈旧快照不得评估; 实际 {applied}"
    assert "落后" in (result["reason"] or ""), result


def test_hk_us_monitor_hk_runs_after_close(monkeypatch, tmp_path):
    """HK job 时刻 (18:35, 已收盘 + as_of 为当日) → 放行; 另一市场不串味。"""
    _write_enriched(tmp_path, "00700.HK", [MON])
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 18, 35),
        hk=datetime(2026, 9, 21, 18, 35),   # 18:35 ∉ 9:30-16:00 → 已收盘
        us=datetime(2026, 9, 21, 6, 35),
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    svc = _make_service(tmp_path, _GateEngine())
    result = svc.evaluate_hk_us_monitors("HK")

    assert result["status"] == "ok", result
    assert applied == ["HK"], f"只评估请求的市场; 实际 {applied}"


def test_run_hk_us_monitor_job_dispatches_to_service(monkeypatch):
    """job → service 真实调用点: run_hk_us_monitor 必须把 market 透传给入口方法。"""
    from app.jobs import hk_us_monitor as job

    seen: list[str] = []

    class _FakeService:
        def evaluate_hk_us_monitors(self, market: str) -> dict:
            seen.append(market)
            return {"market": market, "status": "ok", "events": 0, "alerts": 0}

    monkeypatch.setattr(job, "_quote_service", lambda: _FakeService())
    result = job.run_hk_us_monitor("US")

    assert seen == ["US"], f"job 必须把 market 透传给 quote_service; 实际 {seen}"
    assert result["status"] == "ok"


def test_register_hk_us_monitor_jobs_uses_sync_aligned_cron():
    """调度注册: HK 18:35 / US 08:35 mon-fri Asia/Shanghai (对齐日线同步 + 缓冲)。"""
    from apscheduler.triggers.cron import CronTrigger

    from app.jobs import hk_us_monitor as job

    class _StubScheduler:
        def __init__(self) -> None:
            self.jobs: dict[str, dict] = {}

        def add_job(self, func, trigger, id, misfire_grace_time, replace_existing):
            self.jobs[id] = {"trigger": trigger, "func": func}

    scheduler = _StubScheduler()
    job.register_hk_us_monitor_jobs(scheduler)

    assert set(scheduler.jobs) == {job.HK_US_MONITOR_JOB_ID_HK, job.HK_US_MONITOR_JOB_ID_US}
    assert isinstance(scheduler.jobs[job.HK_US_MONITOR_JOB_ID_HK]["trigger"], CronTrigger)
    fields = {
        job_id: {field.name: str(field) for field in payload["trigger"].fields}
        for job_id, payload in scheduler.jobs.items()
    }
    assert fields[job.HK_US_MONITOR_JOB_ID_HK]["hour"] == "18"
    assert fields[job.HK_US_MONITOR_JOB_ID_HK]["minute"] == "35"
    assert fields[job.HK_US_MONITOR_JOB_ID_US]["hour"] == "8"
    assert fields[job.HK_US_MONITOR_JOB_ID_US]["minute"] == "35"
    # 与港美日线同步同一日历 + 同一时区 (Asia/Shanghai)
    assert str(scheduler.jobs[job.HK_US_MONITOR_JOB_ID_US]["trigger"].timezone) == "Asia/Shanghai"


def test_all_closed_nothing_evaluated(monkeypatch, tmp_path):
    """三市场全闭 (北京周日晚) → 早退, 港美段零构建。"""
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 20, 10, 0),   # 周日
        hk=datetime(2026, 9, 20, 10, 0),
        us=datetime(2026, 9, 20, 22, 0),   # 周日晚也非连续竞价窗口
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    engine = _GateEngine()
    svc = _make_service(tmp_path, engine)
    svc._evaluate_monitors(pl.DataFrame(), None)

    assert applied == [], f"全闭市时不得有任何市场被评估; 实际 {applied}"


def test_is_continuous_trading_registry_helper(monkeypatch):
    """B1: helper 按市场档案的 now()/sessions 判定, 工作日 + session 命中。"""
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 10, 0),   # 周一盘中
        hk=datetime(2026, 9, 21, 12, 30),  # 周一午休 (假设 session 9:30-16:00 之外? 本桩 session 覆盖)
        us=datetime(2026, 9, 21, 3, 0),    # 周一盘前
    )
    assert registry.is_continuous_trading("CN") is True   # 10:00 ∈ 9:30-16:00
    assert registry.is_continuous_trading("HK") is True   # 12:30 ∈ 9:30-16:00 (桩 session)
    assert registry.is_continuous_trading("US") is False  # 3:00 ∉ 9:30-16:00

    # 周末: 无论几点都 False
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 19, 10, 0),
        hk=datetime(2026, 9, 19, 10, 0),
        us=datetime(2026, 9, 19, 10, 0),
    )
    assert registry.is_continuous_trading("CN") is False
    assert registry.is_continuous_trading("HK") is False
    assert registry.is_continuous_trading("US") is False
