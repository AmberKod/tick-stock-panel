#!/usr/bin/env python3
"""一次性把 instruments/{instruments,hk_instruments,us_instruments}.parquet 三个文件的 schema 拉齐。

## 背景
后端启动时 ``tickflow.repository._refresh_instruments`` 用
``scan_parquet_compat`` 把 ``data/instruments/*.parquet`` 三个文件一起
``collect()``，默认 polars 拒绝同列 dtype 不一致，会抛
``SchemaError: data type mismatch for column sector: incoming: String != target: Null``，
导致 ``_instruments_cache`` 为 None。

然后 ``backtest.matrix._resolve_matrix_storage_fields`` 拿不到包含
``total_shares/float_shares`` 列的 instruments 表，于是抛
``ValueError: matrix parquet fields unavailable: ['float_shares', 'total_shares']``。

三份文件的 schema 现状:

    A股  instruments.parquet       : 无 sector/industry, 有 total_shares/float_shares (Float64, 全 Null)
    港股  hk_instruments.parquet    : 有 sector/industry (Null dtype, 全 Null), 无 total_shares/float_shares
    美股  us_instruments.parquet    : 有 sector/industry (String, 198/6071 非 Null), 无 total_shares/float_shares

## 修复
统一三份文件的 schema 列集合 + dtype:
  - sector/industry: String (空值 -> None)
  - total_shares/float_shares: Float64 (空值 -> None)

执行后 ``_refresh_instruments`` 的 scan_parquet 不再有 dtype 冲突,
matrix 也拿得到含 float_shares/total_shares 列的 instruments 表。

## 用法
    python backend/scripts/migrate_instruments_schema.py            # 实际迁移
    python backend/scripts/migrate_instruments_schema.py --dry-run   # 仅打印计划

不会丢失现有数据; 迁移会原地写回原 parquet。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

import polars as pl

TARGET_FILES = [
    REPO_ROOT / "data" / "instruments" / "instruments.parquet",
    REPO_ROOT / "data" / "instruments" / "hk_instruments.parquet",
    REPO_ROOT / "data" / "instruments" / "us_instruments.parquet",
]

# 目标 schema (key=列名, value=目标 dtype)
# 列顺序: 先放所有数据文件都有的核心列, 然后是可选列, 这样写回后不同文件能拼起来。
SHARED_COLUMNS = [
    "symbol",
    "name",
    "code",
    "exchange",
    "market",
]
EXTRA_STRING_COLUMNS = ["sector", "industry", "asset_type", "source", "region", "type"]
EXTRA_FLOAT_COLUMNS = [
    "total_shares",
    "float_shares",
    "tick_size",
    "limit_up",
    "limit_down",
]
EXTRA_DATE_COLUMNS = ["listing_date", "as_of"]


def _ensure_column(df: pl.DataFrame, name: str, dtype: pl.DataType) -> pl.DataFrame:
    """确保列存在且 dtype 一致; 已有但 dtype 不一致则 cast。"""
    if name not in df.columns:
        return df.with_columns(pl.lit(None).cast(dtype).alias(name))
    if df.schema[name] != dtype:
        return df.with_columns(pl.col(name).cast(dtype))
    return df


def _unify_columns(df: pl.DataFrame) -> pl.DataFrame:
    """对一份 parquet 调齐: 补/cast 到目标 schema。"""
    # 关键列 (String, 不能改 dtype)
    for c in SHARED_COLUMNS:
        if c in df.columns and df.schema[c] != pl.Utf8:
            df = df.with_columns(pl.col(c).cast(pl.Utf8))

    # 字符串附加列 (缺失补 None, dtype 错则 cast)
    for c in EXTRA_STRING_COLUMNS:
        df = _ensure_column(df, c, pl.Utf8)

    # 浮点附加列 (缺失补 None, dtype 错则 cast, Int -> Float64 允许)
    for c in EXTRA_FLOAT_COLUMNS:
        df = _ensure_column(df, c, pl.Float64)

    # 日期附加列: 统一 dtype 到 Date (A股原本是 ISO 字符串如 "2004-03-05",
    # polars 可直接 cast 成 Date; 港美原本缺失则补 Null)
    for c in EXTRA_DATE_COLUMNS:
        if c not in df.columns:
            df = _ensure_column(df, c, pl.Date)
        elif df.schema[c] != pl.Date:
            df = df.with_columns(pl.col(c).cast(pl.Date))

    # 统一列顺序: 先 shared, 再 string 附加, 再 float 附加, 再 date 附加, 再其余
    ordered = SHARED_COLUMNS + EXTRA_STRING_COLUMNS + EXTRA_FLOAT_COLUMNS + EXTRA_DATE_COLUMNS
    rest = [c for c in df.columns if c not in ordered]
    final = [c for c in ordered if c in df.columns] + rest
    return df.select(final)


def _analyze_one(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    df = pl.read_parquet(path)
    issues = []
    for c in EXTRA_STRING_COLUMNS + EXTRA_FLOAT_COLUMNS:
        if c not in df.columns:
            issues.append(f"缺列 {c}")
        elif df.schema[c] != (pl.Utf8 if c in EXTRA_STRING_COLUMNS else pl.Float64):
            issues.append(f"{c} dtype={df.schema[c]} 应={pl.Utf8 if c in EXTRA_STRING_COLUMNS else pl.Float64}")
    return {
        "path": str(path),
        "exists": True,
        "rows": df.height,
        "issues": issues,
        "sector_dtype": str(df.schema.get("sector")) if "sector" in df.columns else "MISSING",
        "total_shares_dtype": str(df.schema.get("total_shares")) if "total_shares" in df.columns else "MISSING",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不实际写")
    args = parser.parse_args()

    print("=== 分析现状 ===")
    for p in TARGET_FILES:
        info = _analyze_one(p)
        if not info["exists"]:
            print(f"  - {info['path']}: 不存在")
            continue
        print(f"  - {info['path']}: {info['rows']} 行, sector={info['sector_dtype']}, total_shares={info['total_shares_dtype']}")
        for iss in info["issues"]:
            print(f"      ⚠️  {iss}")

    if args.dry_run:
        print("\n[dry-run] 不实际写文件")
        return 0

    print("\n=== 执行迁移 ===")
    for p in TARGET_FILES:
        if not p.exists():
            print(f"  - {p}: 跳过 (不存在)")
            continue
        df = pl.read_parquet(p)
        new = _unify_columns(df)
        # 备份原文件
        backup = p.with_suffix(p.suffix + ".schema-mig.bak")
        shutil.copyfile(p, backup)
        new.write_parquet(p)
        print(f"  ✅ {p.name}: 备份 → {backup.name}; rows={new.height}, cols={len(new.columns)}")

    print("\n=== 复验 scan_parquet 不再冲突 ===")
    try:
        lf = pl.scan_parquet(str(REPO_ROOT / "data" / "instruments" / "**" / "*.parquet"))
        df = lf.collect()
        print(f"  ✅ scan_parquet 成功: {df.height} 行, {len(df.columns)} 列")
    except Exception as e:
        print(f"  ❌ scan_parquet 仍然失败: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())