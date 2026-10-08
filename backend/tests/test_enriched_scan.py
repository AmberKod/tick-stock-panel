"""app/services/enriched_scan 测试。

存在理由: 这段扫描原先只有 scripts/repair_hk_stale 一份实现, 而 backend/scripts
被 .dockerignore 排除 → 容器内 import 不到 → daily_pipeline 每次启动打印
"enriched 扫描不可用" 并退化成只看 H6 单侧判新鲜度, 补跑判据失真。
搬到 app/ 后必须锁住: ① 镜像里真的能 import; ② 行为与原实现一致。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.services import enriched_scan


def _write(root: Path, symbol: str, dates: list[object], dtype: object = pl.Date) -> None:
    part = root / "kline_hk_us_enriched" / f"symbol={symbol}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [symbol] * len(dates),
            "date": pl.Series(dates).cast(dtype),
            "close": [1.0] * len(dates),
        }
    ).write_parquet(part / "part.parquet")


class TestScanLatestDates:
    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        frame = enriched_scan.scan_latest_dates(tmp_path, "HK")
        assert frame.is_empty()
        assert frame.schema["latest_date"] == pl.Date

    def test_returns_max_date_per_symbol(self, tmp_path: Path) -> None:
        _write(tmp_path, "00001.HK", [date(2026, 9, 1), date(2026, 9, 20)])
        _write(tmp_path, "00002.HK", [date(2026, 9, 18)])
        frame = enriched_scan.scan_latest_dates(tmp_path, "HK")
        assert frame.height == 2
        rows = {r["symbol"]: r["latest_date"] for r in frame.to_dicts()}
        assert rows == {"00001.HK": date(2026, 9, 20), "00002.HK": date(2026, 9, 18)}

    def test_market_suffix_filters(self, tmp_path: Path) -> None:
        """混市场目录: 只返回目标市场, 不做静默兜底。"""
        _write(tmp_path, "00001.HK", [date(2026, 9, 20)])
        _write(tmp_path, "AAPL.US", [date(2026, 9, 21)])
        hk = enriched_scan.scan_latest_dates(tmp_path, "HK")
        assert hk.get_column("symbol").to_list() == ["00001.HK"]
        us = enriched_scan.scan_latest_dates(tmp_path, "US")
        assert us.get_column("symbol").to_list() == ["AAPL.US"]

    def test_mixed_schema_is_unavailable_not_crash(self, tmp_path: Path) -> None:
        """Date 与 Datetime('us') 分区混存 → 按「不可用」返回空表, 不炸、不半算。

        注意: 函数里对 date 的 cast 发生在 collect() **之后**; 跨分区 schema
        不一致时 scan 本身就失败, 于是走 except 返回空表。也就是说文档里
        "legacy Datetime 统一 cast" 只在各分区类型可统一提升时成立 —— 混存
        场景实际落 fail-closed 的「不可用不计入」, 而不是归一后返回。
        这里如实锁定该行为: 宁可少算一侧, 也不拿半份分布去判新鲜度。
        """
        _write(tmp_path, "00001.HK", [date(2026, 9, 20)], dtype=pl.Date)
        _write(
            tmp_path,
            "00002.HK",
            [datetime(2026, 9, 19, 15, 0)],
            dtype=pl.Datetime("us"),
        )
        frame = enriched_scan.scan_latest_dates(tmp_path, "HK")
        assert frame.is_empty()

    def test_unsupported_market_raises(self, tmp_path: Path) -> None:
        """不支持的市场必须显式报错, 不能静默返回空表冒充"没有 stale"。

        注意 root 不存在时会提前 return empty, 所以必须先造出目录才能走到
        市场校验 —— 这也顺带说明: 目录缺失优先按"不可用"处理。
        """
        (tmp_path / "kline_hk_us_enriched").mkdir(parents=True, exist_ok=True)
        try:
            enriched_scan.scan_latest_dates(tmp_path, "JP")
        except ValueError:
            return
        raise AssertionError("不支持的市场应抛 ValueError")


class TestImportableFromAppPackage:
    def test_daily_pipeline_uses_app_module(self) -> None:
        """回归守卫: daily_pipeline 不得再 import scripts.* (镜像内不存在)。"""
        import inspect

        from app.jobs import daily_pipeline

        source = inspect.getsource(daily_pipeline)
        assert "from scripts." not in source
        assert "app.services.enriched_scan" in source
