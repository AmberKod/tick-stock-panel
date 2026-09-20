#!/usr/bin/env python3
"""归一化港美日 K / enriched 分区中 volume 列的 dtype 为 Float64。

背景:
    akshare 新浪源写出的 volume 为 Float64, 而 yfinance 兜底 (hk_data_adapter.
    _fetch_us_daily_yfinance 历史版本) 写出的 volume 为 Int64。跨分区 scan_parquet
    时两者不一致会触发 polars.SchemaError (volume: Int64 != Float64), 导致
    /api/{hk,us}/overview 返回 500。

    fix_volume_dtype.py 扫描 ``kline_daily/symbol=*`` 与 ``kline_hk_us_enriched/
    symbol=*``, 将任何 volume 非 Float64 的分区读回后 cast 为 Float64 并原位覆盖。
    幂等、只读不删、不触发网络请求。

用法:
    python backend/scripts/fix_volume_dtype.py [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings as _settings

_TARGET_DIRS = (
    _settings.data_dir / "kline_daily",
    _settings.data_dir / "kline_hk_us_enriched",
)


def _find_polluted(root: Path) -> list[Path]:
    polluted: list[Path] = []
    for part in sorted(root.glob("symbol=*/part.parquet")):
        try:
            vt = pl.scan_parquet(str(part)).collect_schema().get("volume")
        except Exception:
            continue
        if vt is not None and vt != pl.Float64:
            polluted.append(part)
    return polluted


def main() -> int:
    parser = argparse.ArgumentParser(description="归一化港美日 K volume dtype 为 Float64")
    parser.add_argument("--dry-run", action="store_true", help="只扫描报告, 不写入")
    args = parser.parse_args()

    total = 0
    for root in _TARGET_DIRS:
        if not root.exists():
            continue
        polluted = _find_polluted(root)
        print(f"[{root.name}] 污染分区: {len(polluted)} 个")
        for part in polluted:
            if args.dry_run:
                print(f"  (dry-run) {part}")
                continue
            df = pl.read_parquet(part)
            if "volume" not in df.columns:
                continue
            df = df.with_columns(pl.col("volume").cast(pl.Float64))
            df.write_parquet(part)
            print(f"  已修复: {part.parent.name}")
            total += 1
    print(f"完成: 修复 {total} 个分区")
    return 0


if __name__ == "__main__":
    sys.exit(main())
