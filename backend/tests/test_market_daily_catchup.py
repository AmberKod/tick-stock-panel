"""market_daily 启动 catch-up 判定与触发逻辑测试。

背景: market_daily_hk/us 调度窗口 (18:00/08:00) 与服务在线时段错配时,
服务启动后由 run_market_daily_catchup 兜底补跑。核心判定:
调度窗口已过 + 抽样 H6 max(date) 落后超容差 (自然日, 覆盖周末节假日)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.jobs import daily_pipeline


def _write_h6_partition(
    root: Path, symbol: str, dates: list[object], date_dtype: object = pl.Date
) -> None:
    """写一个 H6 分区; date_dtype 允许 Datetime('us') 模拟老 schema 分区."""
    part = root / "kline_daily" / f"symbol={symbol}"
    part.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "date": pl.Series(dates).cast(date_dtype),
            "close": [1.0] * len(dates),
        }
    )
    frame.write_parquet(part / "part.parquet")


def _write_enriched_partition(
    root: Path, symbol: str, dates: list[object], date_dtype: object = pl.Date
) -> None:
    """写一个港美 enriched 分区 (kline_hk_us_enriched/symbol=*), 与 H6 分区同构。"""
    part = root / "kline_hk_us_enriched" / f"symbol={symbol}"
    part.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "symbol": [symbol] * len(dates),
            "date": pl.Series(dates).cast(date_dtype),
            "close": [1.0] * len(dates),
        }
    )
    frame.write_parquet(part / "part.parquet")


class TestH6LatestBySampling:
    def test_returns_mode_of_sampled_partitions(self, tmp_path: Path) -> None:
        today = date.today()
        for i in range(10):
            _write_h6_partition(tmp_path, f"{i:05d}.HK", [today - timedelta(days=5), today - timedelta(days=1)])
        latest = daily_pipeline._h6_latest_by_sampling(tmp_path, "HK")
        assert latest == today - timedelta(days=1)

    def test_mixed_schema_partitions_normalized(self, tmp_path: Path) -> None:
        """Date 与 Datetime('us') 分区共存 (老 schema 混存) 时统一归一为 date."""
        today = date.today()
        _write_h6_partition(tmp_path, "00001.HK", [today], date_dtype=pl.Date)
        _write_h6_partition(
            tmp_path, "00002.HK", [datetime(today.year, today.month, today.day)], date_dtype=pl.Datetime("us")
        )
        latest = daily_pipeline._h6_latest_by_sampling(tmp_path, "HK")
        assert latest == today

    def test_empty_dir_returns_none(self, tmp_path: Path) -> None:
        assert daily_pipeline._h6_latest_by_sampling(tmp_path, "HK") is None

    def test_us_suffix_ignored_for_hk(self, tmp_path: Path) -> None:
        today = date.today()
        _write_h6_partition(tmp_path, "AAPL.US", [today])
        assert daily_pipeline._h6_latest_by_sampling(tmp_path, "HK") is None
        assert daily_pipeline._h6_latest_by_sampling(tmp_path, "US") == today


class TestCatchupNeeded:
    def _mk(self, tmp_path: Path, latest: date) -> Path:
        _write_h6_partition(tmp_path, "00001.HK", [latest])
        return tmp_path

    def test_before_window_not_needed(self, tmp_path: Path) -> None:
        root = self._mk(tmp_path, date.today() - timedelta(days=30))
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=17)
        assert daily_pipeline._market_daily_catchup_needed(root, "HK", now) is False

    def test_stale_after_window_needed(self, tmp_path: Path) -> None:
        root = self._mk(tmp_path, date.today() - timedelta(days=10))
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(root, "HK", now) is True

    def test_fresh_after_window_not_needed(self, tmp_path: Path) -> None:
        root = self._mk(tmp_path, date.today() - timedelta(days=1))
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(root, "HK", now) is False

    def test_weekend_tolerance_not_needed(self, tmp_path: Path) -> None:
        """周日启动 + H6 停在上周五 (2 天): 容差内, 不触发."""
        root = self._mk(tmp_path, date.today() - timedelta(days=2))
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(root, "HK", now) is False

    def test_holiday_long_gap_needed(self, tmp_path: Path) -> None:
        """超长假期 (5 天) 后启动: 超容差, 触发 (框架 incremental 幂等)."""
        root = self._mk(tmp_path, date.today() - timedelta(days=5))
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(root, "HK", now) is True

    def test_us_window_and_tolerance(self, tmp_path: Path) -> None:
        _write_h6_partition(tmp_path, "AAPL.US", [date.today() - timedelta(days=4)])
        morning = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "US", morning) is True
        early = datetime.combine(date.today(), datetime.min.time()).replace(hour=8)
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "US", early) is False

    def test_no_partitions_not_needed(self, tmp_path: Path) -> None:
        now = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "HK", now) is False

    def test_partial_sync_after_window_needed(self, tmp_path: Path) -> None:
        """最新日只有零散标的到位 → 日期没落后也要补跑。

        真实场景: 港股 09-17 只有 72/2812 只同步到当天 (服务错开 18:30 窗口),
        按"众数最新日 = 09-16"算落后仅 2 天, 在容差内 → 旧判据漏判,
        该市场就此停在少数标的撑起来的日期上。
        """
        today = date.today()
        prev = today - timedelta(days=2)
        newest = today - timedelta(days=1)
        for i in range(10):
            _write_h6_partition(tmp_path, f"{i:05d}.HK", [prev])
        _write_h6_partition(tmp_path, "99999.HK", [newest])  # 只有 1 只到最新日
        now = datetime.combine(today, datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "HK", now) is True

    def test_complete_sync_not_needed(self, tmp_path: Path) -> None:
        """全部标的同步到同一天 (正常盘后同步后的状态) → 不触发。"""
        today = date.today()
        prev = today - timedelta(days=1)
        for i in range(10):
            _write_h6_partition(tmp_path, f"{i:05d}.HK", [prev])
        now = datetime.combine(today, datetime.min.time()).replace(hour=19)
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "HK", now) is False

    def test_enriched_partial_sync_needed(self, tmp_path: Path) -> None:
        """H6 停在 T-2 但 enriched 被零散标的推到 T-1 → 也要触发。

        09-18 实测: 港股 H6 最新停在 09-16 (压根没有 09-17), enriched 侧
        却有 72/2812 只被带到 09-17。只看 H6 会漏判。
        """
        today = date.today()
        prev = today - timedelta(days=2)
        newest = today - timedelta(days=1)
        for i in range(8):
            _write_h6_partition(tmp_path, f"{i:05d}.HK", [prev])
            _write_enriched_partition(tmp_path, f"{i:05d}.HK", [prev])
        _write_enriched_partition(tmp_path, "99999.HK", [newest])  # 只有 1 只到最新日

        now = datetime.combine(today, datetime.min.time()).replace(hour=19)
        # H6 侧众数 = prev, 落后 2 天在容差内, 单看 H6 不触发
        assert daily_pipeline._h6_latest_by_sampling(tmp_path, "HK") == prev
        assert daily_pipeline._market_daily_catchup_needed(tmp_path, "HK", now) is True


class TestRunCatchup:
    # 统一注入晚间时刻: HK(18:30) 与 US(08:30) 窗口均已过, 与真实时钟解耦
    _EVENING = datetime.combine(date.today(), datetime.min.time()).replace(hour=19)
    _MORNING = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)

    def _fake_repo(self, tmp_path: Path):
        class _Store:
            data_dir = tmp_path

        class _Repo:
            store = _Store()

        return _Repo()

    def test_triggers_only_stale_market(self, tmp_path: Path, monkeypatch) -> None:
        today = date.today()
        # HK 落后 10 天, US 新鲜
        _write_h6_partition(tmp_path, "00001.HK", [today - timedelta(days=10)])
        _write_h6_partition(tmp_path, "AAPL.US", [today - timedelta(days=1)])

        called: list[str] = []

        def _fake_scheduled(repo, capset, market):
            called.append(market)
            return {"status": "ok", "market": market}

        monkeypatch.setattr(daily_pipeline, "_run_market_daily_scheduled", _fake_scheduled)

        results = daily_pipeline.run_market_daily_catchup(
            self._fake_repo(tmp_path), None, now=self._EVENING
        )
        assert called == ["HK"]
        assert results == {"HK": {"status": "ok", "market": "HK"}}

    def test_scheduler_error_swallowed(self, tmp_path: Path, monkeypatch) -> None:
        today = date.today()
        _write_h6_partition(tmp_path, "00001.HK", [today - timedelta(days=10)])
        _write_h6_partition(tmp_path, "00002.US", [today - timedelta(days=10)])

        def _boom(repo, capset, market):
            raise RuntimeError("network down")

        monkeypatch.setattr(daily_pipeline, "_run_market_daily_scheduled", _boom)
        # 不抛异常, 静默返回 (不影响启动)
        results = daily_pipeline.run_market_daily_catchup(
            self._fake_repo(tmp_path), None, now=self._EVENING
        )
        assert isinstance(results, dict)

    def test_defers_us_when_provider_cooling(self, tmp_path: Path, monkeypatch) -> None:
        """yfinance 冷却期内不启动美股补跑。

        09-18 实测: 冷却期硬跑 = 6071 只逐个本地快速失败, job 9 秒空转、
        0 条数据, 下次启动又重复一遍。延后更合理。
        """
        today = date.today()
        _write_h6_partition(tmp_path, "AAPL.US", [today - timedelta(days=10)])
        monkeypatch.setattr(
            daily_pipeline, "_provider_cooling_down", lambda market: market.upper() == "US"
        )

        called: list[str] = []

        def _fake_scheduled(repo, capset, market):
            called.append(market)
            return {"status": "ok", "market": market}

        monkeypatch.setattr(daily_pipeline, "_run_market_daily_scheduled", _fake_scheduled)

        results = daily_pipeline.run_market_daily_catchup(
            self._fake_repo(tmp_path), None, now=self._EVENING
        )
        assert called == []  # 一次都没真跑
        assert results["US"]["status"] == "deferred"
        assert results["US"]["reason"] == "provider_cooling_down"

    def test_provider_cooling_down_reads_yfinance_circuit(self) -> None:
        """US 读 yfinance 熔断状态; HK 源不同, 不受美股熔断影响。"""
        from app.data_providers.yfinance_provider import (
            yf_circuit_open_for_test,
            yf_circuit_reset,
        )

        yf_circuit_reset()
        try:
            assert daily_pipeline._provider_cooling_down("US") is False
            yf_circuit_open_for_test(seconds=60)
            assert daily_pipeline._provider_cooling_down("US") is True
            assert daily_pipeline._provider_cooling_down("HK") is False
        finally:
            yf_circuit_reset()

    def test_all_fresh_no_call(self, tmp_path: Path, monkeypatch) -> None:
        today = date.today()
        _write_h6_partition(tmp_path, "00001.HK", [today - timedelta(days=1)])
        _write_h6_partition(tmp_path, "AAPL.US", [today - timedelta(days=1)])

        def _no_call(repo, capset, market):  # pragma: no cover - 不应被调到
            raise AssertionError("不应触发补跑")

        monkeypatch.setattr(daily_pipeline, "_run_market_daily_scheduled", _no_call)
        results = daily_pipeline.run_market_daily_catchup(
            self._fake_repo(tmp_path), None, now=self._EVENING
        )
        assert results == {}


class TestScheduledFullHistorySwitch:
    """full_history 参数映射: 默认 incremental/365d, 显式 full/1998-06-01。

    09-16 记档: run_hk_daily_catchup.py 曾硬编码 incremental+365d, docstring
    却声称与服务调度同构 —— 实际根本修不了 legacy 分区。修复后 scheduled
    增加 full_history 开关, 脚本 --full-history 透传。
    """

    @staticmethod
    def _stub_sync(monkeypatch, captured: dict):
        from app.services import market_daily_sync

        def _fake_sync(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "ok", "completed_symbols": [], "failed_symbols": []}

        monkeypatch.setattr(market_daily_sync, "run_market_daily_sync", _fake_sync)

    @staticmethod
    def _fake_repo(tmp_path: Path):
        from types import SimpleNamespace

        return SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    @staticmethod
    def _pin_markets(monkeypatch, span_days: int = 365) -> tuple[date, date]:
        """把 CN/HK/US 三个市场的"今天"钉成互异哨兵, 返回期望窗口 (start, end)。

        增量窗口已改为跨市场口径 (左端 = 最落后市场 - span_days, 右端 = 最靠前
        市场当天)。若继续用宿主机 date.today() 做期望值, 断言只在"本机日期恰好
        等于最落后市场当日"时成立 —— 换到 UTC 容器/美西主机就静默失效, 所以这里
        必须钉哨兵, 让期望值与本机时钟彻底解耦。
        """
        from app.markets import registry

        class _Pin:
            def __init__(self, pinned: date) -> None:
                self.pinned = pinned
                self.calls = 0

            def today(self) -> date:
                self.calls += 1
                return self.pinned

        anchor = date(2026, 3, 2)
        if anchor == date.today():
            anchor = anchor.replace(year=anchor.year + 1)
        pins = {
            "CN": anchor,
            "HK": anchor - timedelta(days=1),
            "US": anchor - timedelta(days=2),
        }
        for market, pinned in pins.items():
            monkeypatch.setitem(registry._PROFILES, market, _Pin(pinned))
        return min(pins.values()) - timedelta(days=span_days), max(pins.values())

    def test_default_is_incremental_365d(self, tmp_path: Path, monkeypatch) -> None:
        from app.tickflow.policy import CapabilitySet

        captured: dict = {}
        self._stub_sync(monkeypatch, captured)
        monkeypatch.setattr(
            "app.services.hk_data_adapter.sync_hk_instruments", lambda root, allow_demo=False: 10
        )
        expected_start, expected_end = self._pin_markets(monkeypatch)

        result = daily_pipeline._run_market_daily_scheduled(
            self._fake_repo(tmp_path), CapabilitySet(), "HK",
        )

        assert result["status"] == "ok"
        assert captured["mode"] == "incremental"
        assert captured["start_date"].date() == expected_start
        assert captured["end_date"].date() == expected_end

    def test_full_history_maps_full_mode_and_1998_window(self, tmp_path: Path, monkeypatch) -> None:
        from app.tickflow.policy import CapabilitySet

        captured: dict = {}
        self._stub_sync(monkeypatch, captured)
        monkeypatch.setattr(
            "app.services.hk_data_adapter.sync_hk_instruments", lambda root, allow_demo=False: 10
        )

        result = daily_pipeline._run_market_daily_scheduled(
            self._fake_repo(tmp_path), CapabilitySet(), "HK", full_history=True,
        )

        assert result["status"] == "ok"
        assert captured["mode"] == "full"
        assert captured["start_date"].date() == date(1998, 6, 1)
