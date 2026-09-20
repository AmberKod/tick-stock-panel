#!/usr/bin/env python3
"""从参考项目 (daily_stock_analysis) 的 stocks.index.json 抽取美股 universe。

参考项目内置的 ``apps/dsa-web/public/stocks.index.json`` 是一份由 Tushare
美股列表生成的压缩索引，包含约 23357 只美股 (market=US)。本脚本将其抽取为
本项目 ``data/instruments/us_universe.csv``，供 ``sync_us_instruments`` 的
严格同步链路消费，最终落盘 ``us_instruments.parquet``。

条目格式 (压缩数组):
    [canonical_code, display_code, name, pinyin_full, pinyin_abbr, aliases, market, asset_type, active, popularity]

过滤规则:
    - 仅保留 market=US、asset_type=stock、active=True 的条目
    - ticker 仅保留纯字母数字 (A-Z0-9)，排除 SPAC units/warrants/优先股等带 "." 或 "-" 的代码
    - 排除以 "^" 开头的指数类代码
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_INDEX = Path(r"E:\ai_codes\ai_personal_panel\参考项目\stock\daily_stock_analysis\apps\dsa-web\public\stocks.index.json")
OUTPUT_DIR = REPO_ROOT / "data" / "instruments"
OUTPUT_PATH = OUTPUT_DIR / "us_universe.csv"

# 纯字母数字 ticker（排除 AAC.U / BRK-WT / xxx.PR 等）
_TICKER_RE = re.compile(r"^[A-Z0-9]+$")


def main() -> int:
    if not REFERENCE_INDEX.exists():
        print(f"[错误] 参考项目索引不存在: {REFERENCE_INDEX}", file=sys.stderr)
        return 1

    with REFERENCE_INDEX.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    skipped_non_ticker = 0
    skipped_inactive = 0
    for item in raw:
        if not isinstance(item, list) or len(item) < 10:
            continue
        canonical, _display, name, _pf, _pa, _aliases, market, asset_type, active, _pop = item[:10]
        if str(market).upper() != "US":
            continue
        if str(asset_type).strip().lower() != "stock":
            continue
        if active is not True:
            skipped_inactive += 1
            continue
        ticker = str(canonical or "").strip().upper()
        if not _TICKER_RE.fullmatch(ticker):
            skipped_non_ticker += 1
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        name = str(name or ticker).strip() or ticker
        rows.append((ticker, name))

    rows.sort(key=lambda r: r[0])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["code", "name"])
        writer.writerows(rows)

    print("美股 universe 抽取完成:")
    print(f"  - 参考索引总条目: {len(raw)}")
    print(f"  - 有效美股 (market=US, stock, active): {len(rows)}")
    print(f"  - 过滤非纯字母数字 ticker (units/warrants/优先股等): {skipped_non_ticker}")
    print(f"  - 过滤非 active: {skipped_inactive}")
    print(f"  - 输出: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
