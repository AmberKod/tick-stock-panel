"""市场状态标签的**定义体检** —— 不下结论, 只看标签贴得像不像真的牛熊。

为什么单独一个脚本: 状态定义选错了, 后面所有"市况效应"都是精致的垃圾。
这个脚本只读 parquet, 把几种候选定义的按年分布并排打出来, 人对着 A 股历史
(2015H1 牛 / 2015H2-2016 熊 / 2017 慢牛 / 2018 熊 / 2019-2020 牛 / 2022 熊 /
2024 反弹) 看哪个像。

跑法(容器内):
  uv run python /app/scripts/regime_audit.py
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data")
WIN = 60


def load(root: Path) -> pd.DataFrame:
    files = sorted(glob.glob(str(root / "kline_daily_enriched" / "date=*" / "*.parquet")))
    if not files:
        raise SystemExit(f"没有 enriched 分区: {root}")
    bars = (
        pl.scan_parquet(files)
        .select("symbol", "date", "open", "close")
        .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        .collect()
        .sort(["symbol", "date"])
    )
    df = bars.to_pandas()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df


def daily_series(df: pd.DataFrame) -> pd.DataFrame:
    """个股 open→open 日收益(标记在起始日 d) → 每日横截面的三个统计量。"""
    d = df.sort_values(["symbol", "date"], kind="mergesort").copy()
    nxt = d.groupby("symbol", sort=False)["open"].shift(-1)
    d["_r"] = nxt / d["open"] - 1.0
    g = d.groupby("date")["_r"]
    out = pd.DataFrame({
        # 等权均值: 最接近"指数/全市场组合"的口径
        "mean": g.mean(),
        # 中位数个股: 抗极端值, 但**中位数个股 ≠ 市场** —— A股中位数个股长期走平
        "median": g.median(),
        # 上涨占比(市场宽度): 不受极端收益拉动, 最贴近"普涨/普跌"的直觉
        "breadth": g.apply(lambda s: float((s > 0).mean())),
    })
    return out


def main() -> int:
    df = load(ROOT)
    daily = daily_series(df)
    # shift(1): d 日收盘时最新可得的一段是 (d-1 → d)
    shifted = daily.shift(1)
    cum_median = shifted["median"].rolling(WIN).sum()
    cum_mean = shifted["mean"].rolling(WIN).sum()
    breadth = shifted["breadth"].rolling(WIN).mean()

    tbl = pd.DataFrame({
        "cum_median": cum_median,
        "cum_mean": cum_mean,
        "breadth": breadth,
    }).dropna()
    tbl["year"] = pd.to_datetime(tbl.index).year

    print(f"\n==== 市场状态定义体检 (回看 {WIN} 个交易日, 只用**历史**) ====")
    print(f"{'年':<6}{'天数':>6}{'中位累计%':>11}{'等权累计%':>11}{'宽度%':>9}"
          f"{'中位牛%':>9}{'等权牛%':>9}{'宽度牛%':>9}{'宽度熊%':>9}")
    for y, g in tbl.groupby("year"):
        bull_med = float((g["cum_median"] >= 0.05).mean()) * 100
        bull_mean = float((g["cum_mean"] >= 0.05).mean()) * 100
        bull_br = float((g["breadth"] >= 0.55).mean()) * 100
        bear_br = float((g["breadth"] <= 0.45).mean()) * 100
        print(f"{y:<6}{len(g):>6}{g['cum_median'].mean() * 100:>11.2f}"
              f"{g['cum_mean'].mean() * 100:>11.2f}{g['breadth'].mean() * 100:>9.1f}"
              f"{bull_med:>9.0f}{bull_mean:>9.0f}{bull_br:>9.0f}{bear_br:>9.0f}")

    print("\n宽度(breadth)全样本分位:", {
        q: round(float(breadth.quantile(q)), 3) for q in (0.05, 0.25, 0.5, 0.75, 0.95)})
    print("等权累计全样本分位:", {
        q: round(float(cum_mean.quantile(q)), 3) for q in (0.05, 0.25, 0.5, 0.75, 0.95)})
    # 抽查几个历史锚点: 人对着日历看标签对不对
    print("\n历史锚点抽查(标签应只反映**之前** 60 日):")
    for anchor in ("2015-06-10", "2015-09-30", "2016-03-01", "2017-12-01",
                   "2018-10-15", "2019-04-15", "2020-07-10", "2021-12-01",
                   "2022-10-15", "2024-10-10", "2025-04-07"):
        i = tbl.index.searchsorted(anchor)
        if i >= len(tbl.index):
            continue
        dt = tbl.index[i]
        print(f"  {dt}  中位累计 {tbl['cum_median'].iloc[i] * 100:+6.2f}%  "
              f"等权累计 {tbl['cum_mean'].iloc[i] * 100:+6.2f}%  "
              f"宽度 {tbl['breadth'].iloc[i] * 100:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
