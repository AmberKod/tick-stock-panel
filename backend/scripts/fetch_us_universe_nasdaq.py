#!/usr/bin/env python3
"""从 NASDAQ 官方 screener API 拉取美股主板 universe 并清洗落盘。

背景:
    早前用 Tushare 参考索引生成的美股 universe 混入大量粉单/OTC/权证/优先股
    (23221 只), 导致新浪日 K 源成功率仅 ~33%。本脚本改用 NASDAQ 官方 screener
    (仅含 NASDAQ/NYSE/AMEX 主板上市证券), 覆盖主流普通股 + ADR + MLP, 并附带
    sector/industry/market_cap/country 字段 (为看板「行业热度」维度铺路)。

清洗规则 (宁松勿紧, 保证零误杀大盘股):
    - 剔除 symbol 带 ``^`` 的优先股 (如 ABR^D)
    - 剔除 name 含 ``WARRANT`` 的权证
    - 剔除优先股 (name 含 PREFERRED/PREFERENCE) 但排除 ADR
    - 剔除 SPAC/权益单位 (name 含 UNIT(S)) 但排除 MLP 与 ADR
    - 剔除认股权 (name 含 RIGHT(S)) 但排除 ADR
    - symbol 斜杠 ``/WS`` ``/WT`` (权证) 剔除; 其余 ``/X`` 多类股转 ``.X``
      (新浪日 K 源仅认点格式, 如 BRK/A -> BRK.A)

用法:
    python backend/scripts/fetch_us_universe_nasdaq.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "data" / "instruments"
OUTPUT_PATH = OUTPUT_DIR / "us_universe.csv"

NASDAQ_API = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=100&offset=0&download=true"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}


def _fetch_nasdaq_rows() -> list[dict]:
    req = urllib.request.Request(NASDAQ_API, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    rows = data.get("data", {}).get("rows", [])
    if not rows:
        raise RuntimeError("NASDAQ screener 返回空 rows")
    return rows


def _clean(df: pl.DataFrame) -> pl.DataFrame:
    df = df.with_columns(pl.col("market_cap").cast(pl.Float64, strict=False))
    name = df["name"].str.to_uppercase()
    code = df["code"]

    is_caret = code.str.contains(r"\^")
    is_adr = name.str.contains(r"AMERICAN DEPOSITARY|AMERICAN DEPOSITORY| ADS\b|\bADR\b")
    is_mlp = name.str.contains(r"COMMON UNITS|LIMITED PARTN| L\.P\.|\bLP\b|HOLDING L\.P")
    is_pref = name.str.contains(r"PREFERRED|PREFERENCE")
    is_warrant = name.str.contains(r"WARRANT")
    is_unit = name.str.contains(r"\bUNITS?\b")
    is_right = name.str.contains(r"\bRIGHTS?\b")
    is_ws = code.str.contains(r"/W[SOT]\b")  # 权证/单位后缀 (WS/WT/WO)

    junk = (
        is_caret
        | is_warrant
        | is_ws
        | (is_pref & ~is_adr)
        | (is_unit & ~is_mlp & ~is_adr)
        | (is_right & ~is_adr)
    )
    clean = df.filter(~junk)
    # 多类股斜杠转点 (BRK/A -> BRK.A), 新浪日 K 源只认点格式
    clean = clean.with_columns(
        pl.col("code").str.replace("/", ".").alias("code"),
        pl.col("name").str.replace(r"\s+$", "").alias("name"),
    )
    return clean.sort("market_cap", descending=True, nulls_last=True)


def main() -> int:
    rows = _fetch_nasdaq_rows()
    df = pl.DataFrame(rows).select(["symbol", "name", "sector", "industry", "marketCap", "country"])
    df = df.rename({"symbol": "code", "marketCap": "market_cap"})
    raw_n = df.height

    clean = _clean(df)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    clean.write_csv(OUTPUT_PATH)
    print("NASDAQ 美股 universe 清洗完成:")
    print(f"  - 原始主板证券: {raw_n}")
    print(f"  - 保留普通股/ADR/MLP: {clean.height}")
    print(f"  - 剔除 (优先股/权证/单位/认股权): {raw_n - clean.height}")
    print(f"  - sector 覆盖: {clean['sector'].drop_nulls().len()}")
    print(f"  - 输出: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
