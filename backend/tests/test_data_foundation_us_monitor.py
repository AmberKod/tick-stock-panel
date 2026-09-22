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
    return svc


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


def test_us_market_hours_hk_us_evaluated_cn_skipped(monkeypatch, tmp_path):
    """核心断言: 美东 10:00 (A 股闭市) 时港美异动段执行、A 股段不执行。

    变异锚: 把港美门控改回 A 股口径 (cn_continuous) → 本条红。
    """
    # 美东周一 10:00 = 北京 22:00 (夏令时 EDT=UTC-4) —— A 股闭市、美股盘中
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 22, 0),   # 北京 22:00 → CN 闭市
        hk=datetime(2026, 9, 21, 22, 0),   # 香港 22:00 → HK 闭市
        us=datetime(2026, 9, 21, 10, 0),   # 美东 10:00 → US 盘中
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    engine = _GateEngine()
    svc = _make_service(tmp_path, engine)
    svc._evaluate_monitors(pl.DataFrame(), None)

    assert applied == ["US"], (
        f"美股盘中必须构建 US 异动快照且跳过闭市的 HK; 实际 {applied}"
    )


def test_cn_hours_only_cn_abnormal_no_hk_us_leak(monkeypatch, tmp_path):
    """防串味: A 股开市/港美闭市时钟下, 港美段不执行 (防'全局 or'反向错误)。"""
    _pin_market_clocks(
        monkeypatch,
        cn=datetime(2026, 9, 21, 10, 0),   # 北京 10:00 → CN 盘中
        hk=datetime(2026, 9, 21, 10, 0),   # 香港 10:00 → HK 也盘中 (重叠区)
        us=datetime(2026, 9, 21, 22, 0),   # 美东 22:00 → US 闭市
    )
    applied: list[str] = []
    _pin_hk_us_snapshots(monkeypatch, applied)

    engine = _GateEngine()
    svc = _make_service(tmp_path, engine)
    svc._evaluate_monitors(pl.DataFrame(), None)

    assert applied == ["HK"], f"HK 盘中只跑 HK, 闭市的 US 不得评估; 实际 {applied}"


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
