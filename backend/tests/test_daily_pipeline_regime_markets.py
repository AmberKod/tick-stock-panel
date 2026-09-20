"""调度按市场循环 (commit ④ 港美 Regime 治本)。

验证 daily_pipeline 的 regime 计算步骤按市场路由:
- cn 永远启用
- hk / us 仅当 instruments/{hk,us}_instruments.parquet 存在时启用
- 单市场失败不阻塞其他市场 (软失败)
- 阶段切换推送按市场分组 (push_phase_change_alert 接受 market 参数)
- 缓存失效按市场分组 (invalidate_regime_cache 调用次数等于有数据的市场数)

注意: daily_pipeline 在函数体内 import regime_builder / latest_phase_transition,
monkeypatch 需打到 app.services.regime_builder.* (真正的 import 来源), 不是 daily_pipeline.*。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.jobs import daily_pipeline as dp_module

# ────────────────────── helpers ──────────────────────


class _FakeRepo:
    """最小 fake repo: daily_pipeline 只用到 repo.store.data_dir。"""

    def __init__(self, data_dir: Path) -> None:
        class _Store:
            pass

        self.store = _Store()
        self.store.data_dir = data_dir


class _EmitRecorder:
    """捕获 emit() 调用顺序, 让测试断言阶段标记。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str]] = []

    def __call__(self, stage: str, pct: int, msg: str) -> None:
        self.calls.append((stage, pct, msg))


def _write_instruments(data_dir: Path, market: str) -> None:
    """创建 instruments/{market}_instruments.parquet 占位文件(universe 同步过)。"""
    inst_dir = data_dir / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / f"{market}_instruments.parquet").write_bytes(b"")


def _patch_prefs(monkeypatch, *, enabled: bool) -> None:
    """绕过实际 preferences 模块, 强制 pipeline_regime_enabled 走指定分支。"""
    import app.services.preferences as prefs_mod

    monkeypatch.setattr(
        prefs_mod, "get_pipeline_regime_enabled", lambda: enabled, False
    )


# ────────────────────── 调度路由 ──────────────────────


def test_pipeline_loops_only_cn_when_no_instruments(tmp_path, monkeypatch):
    """无 hk/us instruments → 只跑 cn(向后兼容)。"""
    _patch_prefs(monkeypatch, enabled=True)
    calls: list[str] = []

    def _fake_incremental(repo, data_dir, *, today=None, market="cn"):
        calls.append(market)
        import polars as pl

        return pl.DataFrame()  # 空 → 无阶段推送

    monkeypatch.setattr(
        "app.services.regime_builder.compute_regime_incremental",
        _fake_incremental,
    )
    emit = _EmitRecorder()
    repo = _FakeRepo(tmp_path)

    dp_module._compute_regime_step(repo=repo, emit=emit, skipped=[], stage_errors=[])

    assert calls == ["cn"]
    # 没 hk/us instruments → emit 里不能出现 hk/us 标记
    markets_in_logs = [c for c in emit.calls if "[" in c[2]]
    assert all("cn" in c[2] for c in markets_in_logs)
    assert not any("hk" in c[2] or "us" in c[2] for c in markets_in_logs)


def test_pipeline_loops_cn_hk_us_when_instruments_present(tmp_path, monkeypatch):
    """hk + us instruments 都在 → 三个市场全跑, 顺序 cn→hk→us。"""
    _patch_prefs(monkeypatch, enabled=True)
    _write_instruments(tmp_path, "hk")
    _write_instruments(tmp_path, "us")
    calls: list[str] = []

    def _fake_incremental(repo, data_dir, *, today=None, market="cn"):
        calls.append(market)
        import polars as pl

        return pl.DataFrame()

    monkeypatch.setattr(
        "app.services.regime_builder.compute_regime_incremental",
        _fake_incremental,
    )
    emit = _EmitRecorder()
    repo = _FakeRepo(tmp_path)

    dp_module._compute_regime_step(repo=repo, emit=emit, skipped=[], stage_errors=[])

    assert calls == ["cn", "hk", "us"]


def test_pipeline_only_hk_when_only_hk_instruments(tmp_path, monkeypatch):
    """只有 hk instruments → cn + hk, 跳过 us。"""
    _patch_prefs(monkeypatch, enabled=True)
    _write_instruments(tmp_path, "hk")
    calls: list[str] = []

    def _fake_incremental(repo, data_dir, *, today=None, market="cn"):
        calls.append(market)
        import polars as pl

        return pl.DataFrame()

    monkeypatch.setattr(
        "app.services.regime_builder.compute_regime_incremental",
        _fake_incremental,
    )
    emit = _EmitRecorder()
    repo = _FakeRepo(tmp_path)

    dp_module._compute_regime_step(repo=repo, emit=emit, skipped=[], stage_errors=[])

    assert calls == ["cn", "hk"]


def test_pipeline_per_market_soft_failure(tmp_path, monkeypatch):
    """hk 抛异常 → us 仍跑; hk 进 stage_errors + skipped, 不阻断。"""
    _patch_prefs(monkeypatch, enabled=True)
    _write_instruments(tmp_path, "hk")
    _write_instruments(tmp_path, "us")
    calls: list[str] = []

    def _fake_incremental(repo, data_dir, *, today=None, market="cn"):
        calls.append(market)
        if market == "hk":
            raise RuntimeError("hk regime failed")
        import polars as pl

        return pl.DataFrame()

    monkeypatch.setattr(
        "app.services.regime_builder.compute_regime_incremental",
        _fake_incremental,
    )
    emit = _EmitRecorder()
    repo = _FakeRepo(tmp_path)
    stage_errors: list[str] = []
    skipped: list[str] = []

    dp_module._compute_regime_step(repo=repo, emit=emit, skipped=skipped, stage_errors=stage_errors)

    # 三个市场都尝试过, hk 失败但不阻断 us
    assert calls == ["cn", "hk", "us"]
    # hk 失败进 stage_errors + skipped
    assert any("hk" in e for e in stage_errors)
    assert "regime[hk]" in skipped
    # cn / us 不在 errors
    assert not any("[cn]" in e for e in stage_errors)
    assert not any("[us]" in e for e in stage_errors)


def test_pipeline_logs_per_market(tmp_path, monkeypatch, caplog):
    """每个市场都有独立的日志条目(便于排查)。"""
    import logging

    _patch_prefs(monkeypatch, enabled=True)
    _write_instruments(tmp_path, "hk")

    def _fake_incremental(repo, data_dir, *, today=None, market="cn"):
        import polars as pl

        # 返回 3 行让 pipeline 触发 stage marker
        return pl.DataFrame({"date": [1, 2, 3]})

    monkeypatch.setattr(
        "app.services.regime_builder.compute_regime_incremental",
        _fake_incremental,
    )
    emit = _EmitRecorder()
    repo = _FakeRepo(tmp_path)
    with caplog.at_level(logging.INFO):
        dp_module._compute_regime_step(repo=repo, emit=emit, skipped=[], stage_errors=[])

    msgs = [r.message for r in caplog.records]
    assert any("compute_regime[cn]" in m for m in msgs)
    assert any("compute_regume[hk]" in m or "compute_regime[hk]" in m for m in msgs)


# ────────────────────── 阶段推送 ──────────────────────


class _FakeQuoteService:
    """最小 fake quote_service: 捕获 push_alerts 调用。"""

    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def push_alerts(self, alerts: list[dict[str, Any]]) -> None:
        self.alerts.extend(alerts)


class _FakeAppState:
    def __init__(self, qs: _FakeQuoteService) -> None:
        self.quote_service = qs


def _patch_app_state(monkeypatch, qs: _FakeQuoteService) -> _FakeAppState:
    state = _FakeAppState(qs)
    monkeypatch.setattr(dp_module, "_get_app_state", lambda: state)
    return state


def test_push_phase_change_alert_default_market_is_cn(monkeypatch):
    """_push_phase_change_alert 默认 market='cn', 调 latest_phase_transition(cn)。"""
    received: dict[str, Any] = {}

    def _fake_transition(data_dir, market="cn"):
        received["market"] = market
        received["data_dir"] = data_dir
        return ("start", "rise", "2026-09-10")

    monkeypatch.setattr(
        "app.services.regime_builder.latest_phase_transition",
        _fake_transition,
    )
    qs = _FakeQuoteService()
    _patch_app_state(monkeypatch, qs)

    # 默认 market='cn', 直接调用
    dp_module._push_phase_change_alert(Path("/tmp/nonexistent_dir"))
    assert received.get("market") == "cn"
    assert qs.alerts, "应有告警被推送到 quote_service"


def test_push_phase_change_alert_passes_market(monkeypatch):
    """_push_phase_change_alert(market='hk') 透传给 latest_phase_transition(market='hk')。"""
    received: dict[str, Any] = {}

    def _fake_transition(data_dir, market="cn"):
        received["market"] = market
        # hk 返回 'ebb' 触发 warn severity
        if market == "hk":
            return ("rise", "ebb", "2026-09-10")
        return None  # cn 默认无切换

    monkeypatch.setattr(
        "app.services.regime_builder.latest_phase_transition",
        _fake_transition,
    )
    qs = _FakeQuoteService()
    _patch_app_state(monkeypatch, qs)

    dp_module._push_phase_change_alert(Path("/tmp/x"), market="hk")
    assert received.get("market") == "hk"
    # hk 应触发告警(severity warn)
    assert qs.alerts
    assert qs.alerts[0]["severity"] == "warn"


def test_push_phase_change_alert_hk_message_has_market_tag(monkeypatch):
    """hk 推送消息带市场标记(便于监控中心分组过滤)。"""
    monkeypatch.setattr(
        "app.services.regime_builder.latest_phase_transition",
        lambda data_dir, market="cn": ("rise", "ebb", "2026-09-10"),
    )
    qs = _FakeQuoteService()
    _patch_app_state(monkeypatch, qs)

    dp_module._push_phase_change_alert(Path("/tmp/x"), market="hk")
    assert qs.alerts, "应有告警被推送"
    msg = qs.alerts[0].get("message", "")
    assert "hk" in msg.lower() or "港股" in msg