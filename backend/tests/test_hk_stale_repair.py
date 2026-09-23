"""港股 stale 补拉脚本 (scripts/repair_hk_stale.py) 的验收测试。

二维标准:
- 维度 A (值可分): 哨兵日期互异且远离 as_of, 断言值与"真实可达值"可分;
- 维度 B (真实调用点): 直接跑 ``repair_stale`` / ``compute_stale_plan``,
  provider 通过 patch 现役注册表注入 (不是绕过入口只测内部 helper)。

变异验证 (见文件末注释, 跑法与证据在交付报告里):
1. start 改成 ``latest`` (少补一天) → 参数断言红;
2. 去掉 dry_run 判定 → "dry-run 不写盘" 用例红。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repair_hk_stale.py"
_spec = importlib.util.spec_from_file_location("repair_hk_stale", _SCRIPT_PATH)
repair_hk_stale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_hk_stale)

# 哨兵: as_of = 2026-09-22, 两个 stale 档停在互异的更早日期
AS_OF = date(2026, 9, 22)
FRESH_DAY = AS_OF
STALE_DAY_A = date(2026, 9, 3)
STALE_DAY_B = date(2026, 9, 1)


def _write_enriched(root: Path, symbol: str, days: list[date]) -> None:
    """造 enriched 分区 (只含 symbol/date, 脚本扫描只取这两列)。"""
    path = root / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": [symbol] * len(days), "date": days}).write_parquet(path)


def _verified_raw(symbol: str, days: list[date]) -> pl.DataFrame:
    """造一条"已核实口径"的港股原始日线 (publish 侧闸门要求的身份列)。"""
    count = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "date": days,
        "open": [10.0 + index * 0.01 for index in range(count)],
        "high": [10.5 + index * 0.01 for index in range(count)],
        "low": [9.5 + index * 0.01 for index in range(count)],
        "close": [10.2 + index * 0.01 for index in range(count)],
        "volume": [1000.0 + index for index in range(count)],
        "amount": [10000.0 + index * 10 for index in range(count)],
        "source": ["sina_hk_daily"] * count,
        "currency": ["HKD"] * count,
        "volume_unit": ["share"] * count,
        "price_adjustment": ["unadjusted"] * count,
        "price_schema_version": [1] * count,
        "raw_price_verified": [True] * count,
        "observed_at": ["2026-09-22T00:00:00+00:00"] * count,
    })


def _factors(symbol: str, days: list[date]) -> pl.DataFrame:
    """造复权因子快照: coverage_end 必须 >= raw 的最新日期, 否则 HK 分支拒绝重算。"""
    count = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "trade_date": days,
        "ex_factor": [1.0] * count,
        "cumulative_factor": [1.0] * count,
        "source": ["sina_hk_qfq"] * count,
        "version": ["v-test"] * count,
        "coverage_end": [max(days)] * count,
        "observed_at": ["2026-09-22T00:00:00+00:00"] * count,
    })


class _FakeProvider:
    """现役 provider 的替身: 记录每次调用的入参, 返回本批要补的日线。"""

    def __init__(self, frames: dict[str, pl.DataFrame] | None = None,
                 adjustments: dict[str, pl.DataFrame] | None = None,
                 raise_for: set[str] | None = None) -> None:
        self.frames = frames or {}
        self.adjustments = adjustments or {}
        self.raise_for = raise_for or set()
        self.calls: list[dict[str, Any]] = []

    def get_daily_with_report(self, symbols, start_time=None, end_time=None,
                              asset_type="stock", verification_archives=None):
        from app.data_providers.hk_daily_provider import DailyFetchResult

        symbol = symbols[0]
        self.calls.append({
            "symbols": list(symbols),
            "start_time": start_time,
            "end_time": end_time,
            "asset_type": asset_type,
        })
        if symbol in self.raise_for:
            raise RuntimeError(f"源不可用: {symbol}")
        frame = self.frames.get(symbol, pl.DataFrame())
        factors = self.adjustments.get(symbol, pl.DataFrame())
        item = {"symbol": symbol, "status": "ok", "coverage_complete": True}
        return DailyFetchResult(frame, (item,), factors, ())


@pytest.fixture
def fake_provider(monkeypatch) -> _FakeProvider:
    """把现役注册表换成替身 provider (patch 目标是注册表, 入口链路不变)。"""
    from app.data_providers import registry as provider_registry

    provider = _FakeProvider()
    monkeypatch.setattr(provider_registry, "get_default_provider",
                        lambda market, dataset=None: provider)
    return provider


def test_stale_plan_excludes_fresh_and_other_markets(tmp_path: Path) -> None:
    """① stale 清单: fresh 的被排除、stale 的全在、另一市场的标的不得混入。"""
    _write_enriched(tmp_path, "00001.HK", [date(2026, 9, 1), FRESH_DAY])
    _write_enriched(tmp_path, "00002.HK", [STALE_DAY_A])
    _write_enriched(tmp_path, "00003.HK", [STALE_DAY_B])
    _write_enriched(tmp_path, "AAPL.US", [date(2026, 9, 1)])  # 美股停在更早日, 但市场不同

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK")

    assert plan["as_of"] == AS_OF.isoformat()
    assert plan["scanned"] == 3, f"美股分区不得计入港股扫描: {plan['scanned']}"
    assert sorted(entry["symbol"] for entry in plan["targets"]) == ["00002.HK", "00003.HK"]
    assert plan["stale"] == 2


def test_stale_plan_prioritises_largest_bucket(tmp_path: Path) -> None:
    """峰值档优先: 停在同一天的标的越多, 排得越前 (973@09-03 应先补)。"""
    for index in range(4):
        _write_enriched(tmp_path, f"{index + 10:05d}.HK", [STALE_DAY_A])   # 4 只停在 09-03
    _write_enriched(tmp_path, "00099.HK", [STALE_DAY_B])                    # 1 只停在 09-01
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK")

    assert plan["buckets"][0] == {"latest_date": STALE_DAY_A.isoformat(), "symbols": 4}
    assert plan["targets"][0]["latest_date"] == STALE_DAY_A.isoformat()
    assert plan["targets"][-1]["symbol"] == "00099.HK"


def test_repair_start_is_latest_plus_one(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """② 真实调用点: 补拉区间必须从"该标的自身最新日 +1"起, 到市场当天止。"""
    _write_enriched(tmp_path, "00002.HK", [STALE_DAY_A])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    new_days = [STALE_DAY_A + timedelta(days=offset) for offset in range(1, 4)]
    fake_provider.frames["00002.HK"] = _verified_raw("00002.HK", new_days)
    fake_provider.adjustments["00002.HK"] = _factors("00002.HK", [STALE_DAY_A, *new_days])
    seen: list[tuple[str, pl.DataFrame]] = []

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0,
        publisher=lambda root, symbol, frame, **kwargs: (seen.append((symbol, frame)),
                                                         {"symbol": symbol, "status": "ok"})[1],
    )

    assert report["attempted"] == 1 and report["succeeded"] == 1, report["failures"]
    assert len(fake_provider.calls) == 1
    call = fake_provider.calls[0]
    # 维度 A: 期望 09-04 (= 09-03 + 1) 起, 与"少补一天"的 09-03 可分
    assert call["start_time"] == datetime.combine(STALE_DAY_A + timedelta(days=1), time.min)
    assert call["end_time"] == datetime.combine(AS_OF, time.min)
    assert call["symbols"] == ["00002.HK"]
    assert seen and seen[0][0] == "00002.HK"


def test_dry_run_does_not_fetch_or_write(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """③ dry-run: 不发请求、不写盘 (enriched 与 raw 目录快照逐字节不变)。"""
    _write_enriched(tmp_path, "00002.HK", [STALE_DAY_A])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*.parquet"))}

    report = repair_hk_stale.repair_stale(tmp_path, "HK", dry_run=True, today=AS_OF)

    assert report["stale"] == 1 and report["attempted"] == 0
    assert fake_provider.calls == [], "dry-run 不得调用现役数据源"
    assert not (tmp_path / "kline_daily").exists(), "dry-run 不得写 raw 分区"
    after = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*.parquet"))}
    assert after == before, "dry-run 不得改动任何既有文件"


def test_single_failure_does_not_stop_batch(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """④ 单只失败记录进 failures, 整批继续 (不中断)。"""
    _write_enriched(tmp_path, "00002.HK", [STALE_DAY_A])
    _write_enriched(tmp_path, "00003.HK", [STALE_DAY_B])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    fake_provider.raise_for = {"00003.HK"}
    fake_provider.frames["00002.HK"] = _verified_raw("00002.HK", [STALE_DAY_A + timedelta(days=1)])
    published: list[str] = []

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0,
        publisher=lambda root, symbol, frame, **kwargs: (
            published.append(symbol), {"symbol": symbol, "status": "ok"})[1],
    )

    assert report["attempted"] == 2, "失败不得吞掉后续标的"
    assert report["succeeded"] == 1 and report["failed"] == 1
    assert published == ["00002.HK"]
    assert [f["symbol"] for f in report["failures"]] == ["00003.HK"]


def test_limit_caps_targets(tmp_path: Path) -> None:
    """--limit 只截断本次计划, 不改变 stale 总量统计。"""
    for index in range(2, 7):  # 00002~00006 停在 09-03
        _write_enriched(tmp_path, f"{index:05d}.HK", [STALE_DAY_A])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK", limit=2)

    assert plan["stale"] == 5 and len(plan["targets"]) == 2


def test_default_daily_provider_exposes_report_api() -> None:
    """现役注册表默认日线源必须带逐标的报告接口 (补拉要拿本次复权因子)。"""
    from app.data_providers.hk_daily_provider import HKDailyProvider
    from app.data_providers.registry import get_default_provider

    provider = get_default_provider("HK", dataset="daily")

    assert isinstance(provider, HKDailyProvider)
    assert callable(provider.get_daily_with_report)


def test_real_publish_advances_raw_partition(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """真实落库路径: 走现役 publish_hk_daily_snapshot, raw 分区必须推进到新日期。

    这条不注入 publisher, 用来证明默认落库链路 (raw + 因子 + enriched 原子发布)
    真的能跑通, 而不只是被测到调用参数。
    """
    symbol = "00002.HK"
    old_days = [date(2026, 8, 1) + timedelta(days=offset) for offset in range(30)]  # 到 08-30
    old_days = [day for day in old_days if day <= STALE_DAY_A]
    _write_enriched(tmp_path, symbol, old_days)          # enriched 停在 09-03 (stale)
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])   # 提供 as_of
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _verified_raw(symbol, old_days).write_parquet(raw_dir / "part.parquet")

    new_days = [STALE_DAY_A + timedelta(days=offset) for offset in range(1, 6)]
    fake_provider.frames[symbol] = _verified_raw(symbol, new_days)
    fake_provider.adjustments[symbol] = _factors(symbol, old_days + new_days)

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0,
    )

    assert report["attempted"] == 1, report["failures"]
    merged = pl.read_parquet(raw_dir / "part.parquet")
    assert merged["date"].max() == new_days[-1], (
        f"raw 分区未推进到补拉日期: max={merged['date'].max()}, failures={report['failures']}"
    )
