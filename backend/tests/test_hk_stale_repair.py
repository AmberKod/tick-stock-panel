"""港股 stale 补拉脚本 (scripts/repair_hk_stale.py) 的验收测试。

二维标准:
- 维度 A (值可分): 哨兵日期互异且远离 as_of, 断言值与"真实可达值"可分;
- 维度 B (真实调用点): 直接跑 ``repair_stale`` / ``compute_stale_plan``,
  provider 通过 patch 现役注册表注入 (不是绕过入口只测内部 helper)。

变异验证 (见文件末注释, 跑法与证据在交付报告里):
1. start 改成 ``latest`` (少补一天) → 参数断言红;
2. 去掉 dry_run 判定 → "dry-run 不写盘" 用例红;
3. 请求窗口退回纯增量 ``latest+1`` (无维护窗口) → 真实形态用例红, 复现线上
   "缺失 7374 天" 报错;
4. ``no_data`` 合并进 failures → "源无数据单独归类" 用例红;
5. stale 基准退回全市场 ``as_of`` (移动靶) → 移动靶/边界/容差/估算 6 条红;
6. 容差不生效 (基准 = 市场当天) → 容差与移动靶 2 条红;
7. 估算恒用经验值 (不吃实测) → "实测估算" 用例红;
8. 去掉 currency 证据链 (恒 None) → "8 列老分区发布成功" 红;
9. 证据链顺序反转 (HKEX 优先于旧分区) → "继承 CNY" 红;
10. repair_window_incomplete 并进 failures → "维护窗口不全单独归类" 红。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

_SENTINEL_CURRENCY = object()   # "不动 frame 的 currency" 与 None (腾讯熔断) 可分

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "repair_hk_stale.py"
_spec = importlib.util.spec_from_file_location("repair_hk_stale", _SCRIPT_PATH)
repair_hk_stale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_hk_stale)

# 哨兵: as_of = 2026-09-22, 两个 stale 档停在互异的更早日期
AS_OF = date(2026, 9, 22)
FRESH_DAY = AS_OF
STALE_DAY_A = date(2026, 9, 3)
STALE_DAY_B = date(2026, 9, 1)

# 移动靶场景哨兵 (2026-09-23 实证): 市场当天 09-23; 默认容差 7 天 → 基准 09-16
TODAY = date(2026, 9, 23)
STOP_DAY = date(2026, 9, 18)      # "服务停摆日" 那批: 不是源缺口
GAP_DAY = date(2026, 9, 3)        # 真缺口那批 (971@09-03)
DEFAULT_THRESHOLD = date(2026, 9, 16)


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


def _unverified_raw(symbol: str, days: list[date]) -> pl.DataFrame:
    """未核实口径的旧分区行: 缺 currency/volume_unit/price_schema_version 等身份列。

    这正是 2026-09-23 实跑那批标的的形态 —— ``is_verified_hk_raw`` 判 False,
    publish 的 legacy 合并守卫要求 incoming 覆盖这些日期。
    """
    count = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "date": days,
        "open": [1.0] * count,
        "high": [1.0] * count,
        "low": [1.0] * count,
        "close": [1.0] * count,
        "volume": [100.0] * count,
        "amount": [100.0] * count,
        "source": ["legacy"] * count,
        "observed_at": ["2026-09-03T00:00:00+00:00"] * count,
    })


def _eight_column_partition(symbol: str, days: list[date], *, currency: str | None = None) -> pl.DataFrame:
    """第三层守卫的真实形态: 旧仓两代口径并存里的"老 8 列"分区。

    只有 amount/close/date/high/low/open/symbol/volume —— 5 个身份声明列全缺
    (2026-09-24 实测 1425/1529 只 stale 标的是这种)。腾讯熔断时 provider 回的
    currency 也是 null, 两头都空 → publish 守卫"口径未核实"拒。
    """
    count = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * count,
        "date": days,
        "open": [1.0] * count,
        "high": [1.0] * count,
        "low": [1.0] * count,
        "close": [1.0] * count,
        "volume": [100.0] * count,
        "amount": [100.0] * count,
    })


def _null_currency_frame(symbol: str, days: list[date]) -> pl.DataFrame:
    """腾讯熔断时 provider 实际回的形态: 4 列声明在, currency 整列 null。

    对应 hk_daily_provider.py:260 (currency=None) + :709 (腾讯缺席时 lit(None))。
    """
    return _verified_raw(symbol, days).with_columns(
        pl.lit(None, dtype=pl.String).alias("currency"),
    )


def _write_hkex_instruments(root: Path, rows: list[tuple[str, str | None]]) -> None:
    """造 instruments/hk_instruments.parquet (sync_hk_lot_sizes 的落盘形态)。"""
    path = root / "instruments" / "hk_instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [symbol for symbol, _ in rows],
        "currency": [currency for _, currency in rows],
    }).write_parquet(path)


def _business_days(start: date, end: date) -> list[date]:
    """周一~周五的日期序列 (造"数千天历史"用, 不需要真实交易日历)。"""
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


class _FakeProvider:
    """现役 provider 的替身: 记录每次调用的入参, 返回本批要补的日线。

    ``synth=True`` 时按请求的 [start, end] 现场生成"源返回了整个窗口"的日线
    (真实形态: 新浪一次请求返回全历史再在内存过滤), 用于覆盖"旧分区有数千天
    历史、incoming 必须覆盖它们"的场景。

    ``currency=None`` (默认) 模拟腾讯熔断: item 不带 currency、frame 的
    currency 列全 null —— 第三层守卫的触发条件。
    """

    def __init__(self, frames: dict[str, pl.DataFrame] | None = None,
                 adjustments: dict[str, pl.DataFrame] | None = None,
                 raise_for: set[str] | None = None,
                 synth: bool = False, currency: str | None = _SENTINEL_CURRENCY) -> None:
        self.frames = frames or {}
        self.adjustments = adjustments or {}
        self.raise_for = raise_for or set()
        self.synth = synth
        self.currency = currency
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
        if symbol in self.frames:
            frame, factors = self.frames[symbol], self.adjustments.get(symbol, pl.DataFrame())
        elif self.synth and start_time is not None and end_time is not None:
            days = _business_days(start_time.date(), end_time.date())
            frame = _verified_raw(symbol, days).with_columns(
                # close 随日期递增, 便于断言"新行覆盖旧行"(旧分区 close 恒为 1.0)
                pl.Series("close", [10.0 + index for index in range(len(days))]),
            )
            factors = _factors(symbol, days)
        else:
            frame, factors = pl.DataFrame(), pl.DataFrame()
        if self.currency is _SENTINEL_CURRENCY:
            pass  # frame 原样返回 (调用方可能已把 currency 造好)
        elif self.currency is None:
            frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias("currency"))
        else:
            frame = frame.with_columns(pl.lit(self.currency, dtype=pl.String).alias("currency"))
        item_currency = None if self.currency is _SENTINEL_CURRENCY else self.currency
        item = {"symbol": symbol, "status": "ok", "coverage_complete": True,
                "currency": item_currency}
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

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=AS_OF)

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

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=AS_OF)

    assert plan["buckets"][0] == {"latest_date": STALE_DAY_A.isoformat(), "symbols": 4}
    assert plan["targets"][0]["latest_date"] == STALE_DAY_A.isoformat()
    assert plan["targets"][-1]["symbol"] == "00099.HK"


def test_repair_window_covers_gap(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """② 真实调用点: 无历史标的的窗口起点 = 最新日 +1 (缺口必须被覆盖)。"""
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
    # 维度 A: 无历史时起点就是 09-04 (= 09-03 + 1), 与"少补一天"的 09-03 可分
    assert call["start_time"] == datetime.combine(STALE_DAY_A + timedelta(days=1), time.min)
    assert call["end_time"] == datetime.combine(AS_OF, time.min)
    assert call["symbols"] == ["00002.HK"]
    assert seen and seen[0][0] == "00002.HK"


def test_repair_window_starts_at_existing_history(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """窗口起点前移到"现有历史最早日" (维护窗口), 且**仍覆盖** latest+1 缺口。"""
    symbol = "00002.HK"
    old_days = _business_days(date(2020, 1, 1), STALE_DAY_A)
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _verified_raw(symbol, old_days).with_columns(pl.lit(1.0).alias("close")).write_parquet(
        raw_dir / "part.parquet",
    )
    _write_enriched(tmp_path, symbol, [STALE_DAY_A])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    fake_provider.synth = True

    repair_hk_stale.repair_stale(tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0)

    call = fake_provider.calls[0]
    assert call["start_time"] == datetime.combine(old_days[0], time.min), (
        f"有历史时必须按维护窗口起点请求 (历史最早日 {old_days[0]}); 实际 {call['start_time']}"
    )
    assert call["start_time"] <= datetime.combine(STALE_DAY_A + timedelta(days=1), time.min), (
        "窗口起点不得晚于 latest+1, 否则缺口本身没被覆盖"
    )
    assert call["end_time"] == datetime.combine(AS_OF, time.min)


def test_legacy_symbol_publishes_and_keeps_history(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """真实形态 (实跑 50/50 全失败那类): 旧分区**未核实口径** + 数千天历史。

    修复前 (纯增量 latest+1) → merge 守卫判 missing 数千天 → raise;
    修复后 (维护窗口) → incoming 自带完整历史 → 放行, 且历史不丢。
    """
    symbol = "00050.HK"
    old_days = _business_days(date(1998, 6, 1), STALE_DAY_A)   # ≈7500 个交易日
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _unverified_raw(symbol, old_days).write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [STALE_DAY_A])
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    fake_provider.synth = True

    report = repair_hk_stale.repair_stale(tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0)

    assert report["succeeded"] == 1, (
        f"真实形态必须发布成功; failures={report['failures']}"
    )
    merged = pl.read_parquet(raw_dir / "part.parquet")
    # 历史没丢 (旧分区最早日仍在) + 已推进到市场当天
    assert merged["date"].min() == old_days[0], f"历史 earliest 行不得丢: {merged['date'].min()}"
    assert merged["date"].max() == AS_OF, f"必须推进到市场当天: {merged['date'].max()}"
    # 新行覆盖同日旧行: 旧分区 close 恒为 1.0, 源返回的是递增值
    sample = STALE_DAY_A
    assert merged.filter(pl.col("date") == sample)["close"][0] != 1.0, (
        "同日期必须以新数据为准 (旧行不得覆盖新行)"
    )


def test_no_data_symbols_classified_separately(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """源侧无数据 (停牌/退市/源缺) 单独归类, 不计入 failed —— 与发布失败是两类问题。"""
    _write_enriched(tmp_path, "00002.HK", [STALE_DAY_A])   # provider 返回空 (源无数据)
    _write_enriched(tmp_path, "00003.HK", [STALE_DAY_B])   # 正常补拉
    _write_enriched(tmp_path, "00001.HK", [FRESH_DAY])
    fake_provider.frames["00003.HK"] = _verified_raw("00003.HK", [STALE_DAY_B + timedelta(days=1)])
    fake_provider.adjustments["00003.HK"] = _factors("00003.HK", [STALE_DAY_B, STALE_DAY_B + timedelta(days=1)])

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=AS_OF, sleep_seconds=0.0,
        publisher=lambda root, symbol, frame, **kwargs: {"symbol": symbol, "status": "ok"},
    )

    assert report["no_data"] == 1 and report["no_data_symbols"] == ["00002.HK"]
    assert report["failed"] == 0, f"源无数据不得算作管道失败: {report['failures']}"
    assert report["succeeded"] == 1


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

    plan = repair_hk_stale.compute_stale_plan(tmp_path, "HK", limit=2, today=AS_OF)

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


def _stale_symbols(plan: dict[str, Any]) -> list[str]:
    return [entry["symbol"] for entry in plan["targets"]]


def test_stale_baseline_does_not_rebound_after_repair(tmp_path: Path) -> None:
    """移动靶回归: 补拉把 as_of 推高后, "停在服务停摆日"的标的不得反弹成 stale。

    实证 (2026-09-23): 补拉前 as_of=09-18 / stale=1611; 只补 2 只把 as_of 推到
    09-23 后, 旧基准 (latest < 全市场 as_of) 下 stale 变 2796 —— 多出的 1187 只
    全是"停在 09-18"的服务停摆标的, 不是源缺口。补得越多 stale 越多。
    """
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])   # 停摆日那批 (2 只代表 1187)
    _write_enriched(tmp_path, "00004.HK", [STOP_DAY])
    _write_enriched(tmp_path, "00002.HK", [GAP_DAY])    # 真缺口, 本次要补的

    before = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=TODAY)

    assert before["as_of"] == STOP_DAY.isoformat()
    assert before["stale_before"] == DEFAULT_THRESHOLD.isoformat(), (
        f"默认基准必须是 市场当天-7d = {DEFAULT_THRESHOLD}: {before['stale_before']}")
    assert before["basis"] == "market-today-7d"
    assert _stale_symbols(before) == ["00002.HK"], f"停摆标的不得算 stale: {before['targets']}"
    # 旧基准等价复现 (阈值 = as_of = 09-18): 此时两边一致, 说明差异来自基准而非数据
    old_before = repair_hk_stale.compute_stale_plan(
        tmp_path, "HK", today=TODAY, stale_before=STOP_DAY)
    assert old_before["stale"] == 1, "补拉前旧基准与新基准结论一致"

    # 补掉真缺口那只 → as_of 被推到市场当天
    _write_enriched(tmp_path, "00002.HK", [TODAY])
    after = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=TODAY)
    # 旧基准等价复现: 阈值 = 被推高后的 as_of (09-23), 严格小于才算 stale
    old_after = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=TODAY, stale_before=TODAY)

    assert after["as_of"] == TODAY.isoformat(), "as_of 已被补拉推高"
    assert after["stale"] == 0, f"新基准下补完就归零: {after['targets']}"
    assert old_after["stale"] == 2, (
        f"旧 (as_of) 基准下停摆标的会反弹成 stale: {old_after['targets']}")
    assert _stale_symbols(old_after) == ["00001.HK", "00004.HK"], "反弹的必须正是停摆那批"
    assert after["stale"] <= before["stale"], "stale 数只许下降, 不许随补拉反弹"


def test_stale_before_pins_the_boundary(tmp_path: Path) -> None:
    """--stale-before 就是阈值本身: 早于它算 stale, 等于它不算 (相邻日可分)。"""
    _write_enriched(tmp_path, "00002.HK", [date(2026, 9, 3)])   # < 09-04 → stale
    _write_enriched(tmp_path, "00003.HK", [date(2026, 9, 4)])   # = 09-04 → 不补
    _write_enriched(tmp_path, "00004.HK", [date(2026, 9, 10)])  # 远在阈值后 → 不补

    plan = repair_hk_stale.compute_stale_plan(
        tmp_path, "HK", today=TODAY, stale_before=date(2026, 9, 4))

    assert plan["basis"] == "stale-before"
    assert plan["stale_before"] == "2026-09-04"
    assert _stale_symbols(plan) == ["00002.HK"], f"边界必须严格左闭: {plan['targets']}"


def test_tolerance_days_shifts_baseline(tmp_path: Path) -> None:
    """容差决定基准: 同一只 09-18 的标的, 容差 1 天算 stale, 容差 7 天不算。"""
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])

    tight = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=TODAY, tolerance_days=1)
    loose = repair_hk_stale.compute_stale_plan(tmp_path, "HK", today=TODAY, tolerance_days=7)

    assert tight["stale_before"] == date(2026, 9, 22).isoformat()
    assert loose["stale_before"] == DEFAULT_THRESHOLD.isoformat()
    assert tight["stale"] == 1 and loose["stale"] == 0, (
        f"容差必须真的移动基准: tight={tight['stale']}, loose={loose['stale']}")


def test_dry_run_estimate_uses_measured_constant(tmp_path: Path, monkeypatch) -> None:
    """dry-run 没有实测样本 → 用经验值估算, 并标明是经验值 (不当 SLA)。"""
    monkeypatch.setattr(repair_hk_stale, "_MEASURED_SECONDS_PER_SYMBOL", 3.5)
    for index in range(3):
        _write_enriched(tmp_path, f"{index + 2:05d}.HK", [GAP_DAY])

    report = repair_hk_stale.repair_stale(tmp_path, "HK", dry_run=True, today=TODAY)

    assert report["seconds_per_symbol"] == 3.5, "必须读经验值常量, 不是写死的数字"
    assert report["seconds_per_symbol_measured"] is False
    assert report["estimate_all_seconds"] == round(3 * 3.5, 1)
    assert report["estimate_remaining_seconds"] == report["estimate_all_seconds"]


def test_real_run_estimate_uses_measured_elapsed(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """实跑后必须用**本次实测**均值估算 (不再是经验值)。"""
    for index in range(2):
        _write_enriched(tmp_path, f"{index + 2:05d}.HK", [GAP_DAY])
    for index in range(2):
        symbol = f"{index + 2:05d}.HK"
        days = [GAP_DAY + timedelta(days=offset) for offset in range(1, 4)]
        fake_provider.frames[symbol] = _verified_raw(symbol, days)
        fake_provider.adjustments[symbol] = _factors(symbol, [GAP_DAY, *days])

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        publisher=lambda root, symbol, frame, **kwargs: {"symbol": symbol, "status": "ok"},
    )

    assert report["seconds_per_symbol_measured"] is True
    assert report["seconds_per_symbol"] == round(
        report["elapsed_seconds"] / report["attempted"], 3), "必须用实测耗时, 不是经验值"
    assert report["estimate_all_seconds"] == round(2 * report["seconds_per_symbol"], 1)
    assert report["estimate_remaining_seconds"] == 0.0


def test_main_passes_stale_before_and_tolerance_days(tmp_path: Path, monkeypatch) -> None:
    """真实 CLI 调用点: --stale-before / --tolerance-days 必须透传到 repair_stale。"""
    captured: dict[str, Any] = {}
    real = repair_hk_stale.repair_stale

    def _spy(data_dir, market="HK", limit=None, **kwargs):
        captured.update(kwargs)
        kwargs["dry_run"] = True      # 只验参数透传, 不发请求不写盘
        return real(data_dir, market, limit, **kwargs)

    monkeypatch.setattr(repair_hk_stale, "repair_stale", _spy)
    code = repair_hk_stale.main([
        "--data-dir", str(tmp_path), "--yes", "--market", "HK",
        "--stale-before", "2026-09-04", "--tolerance-days", "3",
    ])

    assert code == 0
    assert captured["stale_before"] == date(2026, 9, 4)
    assert captured["tolerance_days"] == 3
    assert captured["dry_run"] is False, "--yes 必须落到非 dry-run"

    bad: dict[str, Any] = {}
    monkeypatch.setattr(repair_hk_stale, "repair_stale",
                        lambda *a, **kw: bad.update(kw) or {})
    assert repair_hk_stale.main(["--data-dir", str(tmp_path), "--stale-before", "09/04/2026"]) == 2
    assert bad == {}, "非法日期必须在入口就拒, 不得继续补拉"


def test_print_report_shows_baseline_and_estimate(tmp_path: Path, capsys) -> None:
    """人读报告必须给出基准与耗时估算 (用户要拿它决定放不放全量)。"""
    for index in range(3):
        _write_enriched(tmp_path, f"{index + 2:05d}.HK", [GAP_DAY])

    code = repair_hk_stale.main(["--data-dir", str(tmp_path), "--market", "HK"])
    text = capsys.readouterr().out

    assert code == 0
    assert "stale 基准" in text, f"报告没给判定基准: {text}"
    assert "单只耗时" in text and "全量预计" in text, f"报告没给耗时估算: {text}"
    assert "经验值" in text, "dry-run 必须标明耗时是经验值"

    assert repair_hk_stale.main(["--data-dir", str(tmp_path), "--market", "HK", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["basis"] == "market-today-7d"
    assert payload["estimate_all_seconds"] == round(3 * payload["seconds_per_symbol"], 1)


# ---------------------------------------------------------------- 第三层守卫
# 2026-09-24 全量实跑: 00050/00051 成功后从 00167.HK 起 100% 失败, 同一报错
# "港股原始日线的币种、量单位或价格口径未核实"。旧仓 raw 两代口径并存:
# 104 只带 5 列声明 / 1425 只只有老 8 列。8 列标的首拉时 provider 的 currency
# 全 null (腾讯熔断), publish 的继承旁路又要求旧分区已核实 → 拒。

_EIGHT_COL_DAYS = _business_days(date(2015, 1, 1), GAP_DAY)


def test_eight_column_partition_publishes_with_identity(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """真实形态: 老 8 列分区 + 腾讯熔断 (currency null) → 修复前守卫红, 修复后绿。

    修复路径: HKEX 证券清单补 currency, 4 列声明 provider 恒产;
    落盘分区必须带完整 5 列声明且值正确 (守卫一行没动, 数据配得上守卫了)。
    """
    symbol = "00167.HK"
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _eight_column_partition(symbol, _EIGHT_COL_DAYS).write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])     # as_of 存在但 < 基准
    _write_hkex_instruments(tmp_path, [("00167.HK", "HKD")])
    fake_provider.synth = True
    fake_provider.currency = None                          # 腾讯熔断: currency 全 null

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1),
    )

    assert report["succeeded"] == 1, f"8 列老分区必须发布成功: {report['failures']}"
    merged = pl.read_parquet(raw_dir / "part.parquet")
    # 落盘分区带完整 5 列声明且值正确 —— 这正是守卫要的"配得上守卫的数据"
    assert merged["currency"].unique().to_list() == ["HKD"]
    assert merged["volume_unit"].unique().to_list() == ["share"]
    assert merged["price_adjustment"].unique().to_list() == ["unadjusted"]
    assert merged["price_schema_version"].unique().to_list() == [1]
    assert merged["raw_price_verified"].unique().to_list() == [True]
    # 历史不丢 + 推进
    assert merged["date"].min() == _EIGHT_COL_DAYS[0]
    assert merged["date"].max() == TODAY


def test_currency_inherited_from_partition_beats_default(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """currency 继承: 旧分区 (人民柜台) CNY 必须继承 CNY, 不许默认 HKD。"""
    symbol = "80016.HK"
    old_days = _business_days(date(2018, 1, 1), GAP_DAY)
    partition = _eight_column_partition(symbol, old_days).with_columns(
        pl.lit("CNY", dtype=pl.String).alias("currency"),     # 只带 currency 一列的旧分区
    )
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    partition.write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])
    _write_hkex_instruments(tmp_path, [(symbol, "HKD")])       # HKEX 层与旧分区冲突 → 旧分区赢
    fake_provider.synth = True
    fake_provider.currency = None

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1),
    )

    assert report["succeeded"] == 1, report["failures"]
    merged = pl.read_parquet(raw_dir / "part.parquet")
    assert merged["currency"].unique().to_list() == ["CNY"], (
        "旧分区已核实的币种必须优先于 HKEX 清单, 更不许默认 HKD")


def test_quote_currency_wins_over_partition(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """证据链最强层: 本次腾讯报价 (USD) 优先于旧分区 (HKD)。"""
    symbol = "00770.HK"
    old_days = _business_days(date(2018, 1, 1), GAP_DAY)
    partition = _eight_column_partition(symbol, old_days).with_columns(
        pl.lit("HKD", dtype=pl.String).alias("currency"),
    )
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    partition.write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])
    fake_provider.synth = True
    fake_provider.currency = "USD"                            # 腾讯报价活着

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1),
    )

    assert report["succeeded"] == 1, report["failures"]
    merged = pl.read_parquet(raw_dir / "part.parquet")
    assert merged["currency"].unique().to_list() == ["USD"], "同次请求的报价币种必须最优先"


def test_no_currency_evidence_skips_without_defaulting(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """三层证据全空 → 单独归类跳过, 绝不默认 HKD 落盘 (有 24 CNY/1 USD 在清单里)。"""
    symbol = "00167.HK"
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _eight_column_partition(symbol, _EIGHT_COL_DAYS).write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])
    # 不造 instruments (HKEX 层缺失), 腾讯熔断, 旧分区无 currency
    fake_provider.synth = True
    fake_provider.currency = None

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1),
    )

    assert report["no_identity"] == 1 and report["no_identity_symbols"] == [symbol]
    assert report["failed"] == 0 and report["succeeded"] == 0, (
        f"证据不足是'跳过'不是'失败': {report['failures']}")
    merged = pl.read_parquet(raw_dir / "part.parquet")
    assert "currency" not in merged.columns, "不许给老分区凭空写默认币种"


def test_verified_partition_identity_untouched(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """已带 5 列声明的分区 (00050 型, 104 只): 不进证据链、行为不变。"""
    symbol = "00050.HK"
    old_days = _business_days(date(2018, 1, 1), GAP_DAY)
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    _verified_raw(symbol, old_days).write_parquet(raw_dir / "part.parquet")   # 已核实口径
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])
    fake_provider.synth = True
    fake_provider.currency = None    # 腾讯熔断 —— 但 frame 的 currency 已由 publish 旁路继承

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1),
    )

    # 已核实分区 currency null 由 publish 自带的继承旁路处理, 脚本证据链不干预
    assert report["no_identity"] == 0
    assert report["succeeded"] == 1, report["failures"]
    merged = pl.read_parquet(raw_dir / "part.parquet")
    assert merged["currency"].unique().to_list() == ["HKD"]


def test_identity_report_block_printed(tmp_path: Path, capsys) -> None:
    """报告必须单列'币种证据不足'块 (与失败/源无数据分开, 别让用户误判)。"""
    report = {
        "market": "HK", "scanned": 0, "as_of": None, "stale": 0, "buckets": [],
        "targets": [], "stale_before": "2026-09-16", "basis": "market-today-7d",
        "tolerance_days": 7, "market_today": "2026-09-23", "dry_run": False, "limit": 0,
        "attempted": 1, "succeeded": 0, "failed": 0, "skipped": 0,
        "no_data": 0, "no_data_symbols": [],
        "no_identity": 2, "no_identity_symbols": ["00167.HK", "80016.HK"],
        "repair_blocked": 1, "repair_blocked_symbols": ["00167.HK"],
        "failures": [], "elapsed_seconds": 1.0,
        "seconds_per_symbol": 8.2, "seconds_per_symbol_measured": True,
        "estimate_remaining_seconds": 0.0, "estimate_all_seconds": 16.4,
    }
    repair_hk_stale.print_report(report)
    text = capsys.readouterr().out
    assert "币种证据不足" in text and "00167.HK" in text, text
    assert "维护窗口不全" in text, f"第四层守卫归类必须单列: {text}"


def test_repair_window_incomplete_classified_separately(tmp_path: Path, fake_provider: _FakeProvider) -> None:
    """第四层守卫 (00167 型): 近期覆盖缺口 → publish partial + repair_window_incomplete。

    正确语义: **归类, 不修** —— 守卫保住原文件是对的 (源侧真没有近期数据,
    修管道修不出来); 归入 repair_blocked, 不计入 failed, 且原分区一个字节不动。
    """
    symbol = "00167.HK"
    old_days = _business_days(date(2015, 1, 1), GAP_DAY)
    raw_dir = tmp_path / "kline_daily" / f"symbol={symbol}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    partition = _eight_column_partition(symbol, old_days)     # 口径未核实 → repair=True
    partition.write_parquet(raw_dir / "part.parquet")
    _write_enriched(tmp_path, symbol, [GAP_DAY])
    _write_enriched(tmp_path, "00001.HK", [STOP_DAY])
    _write_hkex_instruments(tmp_path, [(symbol, "HKD")])
    fake_provider.synth = True
    fake_provider.currency = None
    before_bytes = (raw_dir / "part.parquet").read_bytes()

    def _blocked_publisher(root, sym, frame, **kwargs):
        # publish 对第四层守卫是 raise 而非返回 partial (:1041)
        raise ValueError("旧价格口径维护窗口尚未具备完整原始价与复权因子, 已保留原文件")

    report = repair_hk_stale.repair_stale(
        tmp_path, "HK", dry_run=False, today=TODAY, sleep_seconds=0.0,
        stale_before=GAP_DAY + timedelta(days=1), publisher=_blocked_publisher,
    )

    assert report["repair_blocked"] == 1 and report["repair_blocked_symbols"] == [symbol]
    assert report["failed"] == 0 and report["succeeded"] == 0, (
        f"维护窗口不全不是管道失败: {report['failures']}")
    assert (raw_dir / "part.parquet").read_bytes() == before_bytes, (
        "publish 保留原文件的语义必须在脚本侧兑现 (零写盘)")
