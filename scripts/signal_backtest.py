"""K 线标注的全市场回测 —— 给每个标注填一张证据卡 (容器内执行)。

产出 ``data/signal_stats.json``, 正是后端 ``GET /api/kline/signal-stats`` 读的契约,
所以**不用改任何前后端代码**, 跑完即生效。

方法纪律(改这里之前先读 deliverables/jiuzhang-signal-probe/20261015-箭头徽章与证据卡设计.md)
----------------------------------------------------------------------------------------
1. **统计的是前端实际画出来的那个标注**。指标口径逐条复刻 frontend/src/lib/klinePeriod.ts
   的 attachIndicators + klineSignals.ts 的 buildSignalMarkers(sma/ema/wilder/ddof=0/
   前 20 根窗口/交叉判定)。口径不一致的证据等于装饰品。
2. **PIT**: 信号只用第 i 根及之前的数据判定, 不读未来。
3. **撮合 open_t+1**: 信号第 i 根收盘才确认, 成交在第 i+1 根开盘, 持有 N 根后在
   open[t+1+N] 卖出。不用九章那套 same-bar fill(实测虚增 18.2%)。
4. **相对基准**: 超额 = 信号收益 - 同日全市场买同样持有期的横截面均值, 剔掉市场 beta。
5. **方向对齐**: buy 取正收益、sell 取负收益合成 aligned return; 成交量中立信号不翻转。
6. **扣费**: 双边 30bp(印花+佣金+滑点), 长腿短腿各扣一次。
7. **训练/测试分割**: 2015-2022 训练, 2023-2026 测试。两者同向才算 split_consistent。
   这是防"改规则改到好看为止"的闸门 —— 优化的是证据表里的数字, 不是回头改规则。
8. **多重检验**: 9 信号 × 3 持有期 = 27 次同时检定, BH-FDR 控制假发现率
   (不校正的话 α=0.05 下约 1.4 个纯 noise 信号会被误认为显著)。
9. **检验方法**: 按**事件日聚类** bootstrap(2000 次, 固定种子)。同一天几百个 simultanous
   信号不是独立样本, 独立同分布假设会让 p 值假性显著。

已知限制(写进 note, 不藏着)
--------------------------
- **幸存者偏差**: 标的池是当前 instruments, 历史上退市的股票不在样本里, 收益偏乐观。
- Warmup 前 70 根指标未收敛, 不参与统计。

用法
----
    # 抽样试跑(验证 + 计时)
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/signal_backtest.py --symbols 200
    # 全市场写盘
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/signal_backtest.py --out /app/data/signal_stats.json
    # 导出某几只票的信号日期, 用于跟 TS 实现逐条对照
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/signal_backtest.py --symbols 12 --dump Marks.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("signal_backtest")

WARMUP = 70                      # 指标收敛前丢掉
HOLDS = (5, 10, 20)              # 持有期(交易日)
PERM = 2000                      # bootstrap 次数
COST = 0.003                     # 双边成本 30bp
TRAIN_UNTIL = "2022-12-31"       # 训练/测试切分点
SEED = 20261015

# key -> (方向语义) buy=看涨标记 / sell=看跌标记 / neutral=无方向
SIGNAL_DIR = {
    "volume": "neutral",
    "newHigh": "buy",
    "newLow": "sell",
    "macd": "both",
    "maCross": "both",
    "maGolden60": "both",
    "ma20Cross": "both",
    "rsi": "both",
    "boll": "both",
}


# ──────────────────────────────────────────────────────────────────────
# 指标 —— 逐条对齐 frontend/src/lib/klinePeriod.ts attachIndicators
# ──────────────────────────────────────────────────────────────────────
def attach_indicators(g: pd.DataFrame) -> pd.DataFrame:
    """输入按日期升序的单票 DataFrame(open/close/volume), 补齐指标列。

    与 TS 版的对应关系:
      sma(values,n)  —— rolling(n).mean(), 前 n-1 个 NaN
      ema(values,n)  —— ewm(alpha=2/(n+1), adjust=False), y0 = x0
      wilder(v,n)    —— ewm(alpha=1/n, adjust=False),    y0 = x0
      BOLL σ         —— rolling(20).std(ddof=0)   # TS 里是 sqrt(sq/20) 总体标准差
      RSI14          —— 100 - 100/(1 + au/ad), au/ad 为涨跌的 Wilder 平滑
    """
    close = g["close"].astype(float)
    vol = g["volume"].astype(float).fillna(0.0)

    out = g.copy()
    out["ma5"] = close.rolling(5).mean()
    out["ma20"] = close.rolling(20).mean()
    out["ma60"] = close.rolling(60).mean()

    sd20 = close.rolling(20).std(ddof=0)
    out["boll_upper"] = out["ma20"] + 2 * sd20
    out["boll_lower"] = out["ma20"] - 2 * sd20

    ema12 = close.ewm(alpha=2 / 13, adjust=False).mean()
    ema26 = close.ewm(alpha=2 / 27, adjust=False).mean()
    out["macd_dif"] = ema12 - ema26
    out["macd_dea"] = out["macd_dif"].ewm(alpha=2 / 10, adjust=False).mean()

    chg = close.diff()
    up = chg.clip(lower=0).fillna(0.0)      # TS: up[0] 固定为 0
    dn = (-chg).clip(lower=0).fillna(0.0)
    au = up.ewm(alpha=1 / 14, adjust=False).mean()
    ad = dn.ewm(alpha=1 / 14, adjust=False).mean()
    out["rsi_14"] = 100 - 100 / (1 + au / ad.replace(0.0, np.nan))

    # 成交量基准: 前 20 根**不含当前根**([i-20, i))
    out["vol_base20"] = vol.rolling(20).mean().shift(1)
    # 60 根极值窗口: closes[i-60 .. i-1], 窗口不足 20 根不标
    out["hi60"] = close.rolling(60, min_periods=20).max().shift(1)
    out["lo60"] = close.rolling(60, min_periods=20).min().shift(1)
    return out


def crossed(cur, ref, prev_cur, prev_ref):
    """与 TS crossed() 同语义: 缺任一值返回 None(不猜)。"""
    if pd.isna(cur) or pd.isna(ref) or pd.isna(prev_cur) or pd.isna(prev_ref):
        return None
    if prev_cur <= prev_ref and cur > ref:
        return "up"
    if prev_cur >= prev_ref and cur < ref:
        return "down"
    return None


def events_for_symbol(g: pd.DataFrame, dump_sym: str | None = None) -> list[dict]:
    """单票 → [{date, key, dir}]。第 i 根只读第 i 与 i-1 根(PIT)。"""
    d = attach_indicators(g)
    n = len(d)
    close = d["close"].to_numpy(dtype=float)
    vol = d["volume"].fillna(0.0).to_numpy(dtype=float)
    dates = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d").tolist()

    prev_close = np.r_[np.nan, close[:-1]]
    ma5 = d["ma5"].to_numpy(dtype=float)
    ma20 = d["ma20"].to_numpy(dtype=float)
    ma60 = d["ma60"].to_numpy(dtype=float)
    dif = d["macd_dif"].to_numpy(dtype=float)
    dea = d["macd_dea"].to_numpy(dtype=float)
    rsi = d["rsi_14"].to_numpy(dtype=float)
    bu = d["boll_upper"].to_numpy(dtype=float)
    bl = d["boll_lower"].to_numpy(dtype=float)
    vb = d["vol_base20"].to_numpy(dtype=float)
    hi60 = d["hi60"].to_numpy(dtype=float)
    lo60 = d["lo60"].to_numpy(dtype=float)
    pma5 = np.r_[np.nan, ma5[:-1]]
    pma20 = np.r_[np.nan, ma20[:-1]]
    pma60 = np.r_[np.nan, ma60[:-1]]
    pdif = np.r_[np.nan, dif[:-1]]
    pdea = np.r_[np.nan, dea[:-1]]

    out: list[dict] = []
    for i in range(WARMUP, n):
        dt = dates[i]
        # 量能
        if np.isfinite(vb[i]) and vb[i] > 0 and vol[i] >= vb[i] * 2:
            out.append({"date": dt, "key": "volume", "dir": "neutral"})
        # 极值(newHigh / newLow 互斥, 与 TS 的 if/else if 一致)
        if np.isfinite(hi60[i]) and close[i] > hi60[i]:
            out.append({"date": dt, "key": "newHigh", "dir": "buy"})
        elif np.isfinite(lo60[i]) and close[i] < lo60[i]:
            out.append({"date": dt, "key": "newLow", "dir": "sell"})
        # MACD
        c = crossed(dif[i], dea[i], pdif[i], pdea[i])
        if c == "up":
            out.append({"date": dt, "key": "macd", "dir": "buy"})
        elif c == "down":
            out.append({"date": dt, "key": "macd", "dir": "sell"})
        # MA5/20
        c = crossed(ma5[i], ma20[i], pma5[i], pma20[i])
        if c == "up":
            out.append({"date": dt, "key": "maCross", "dir": "buy"})
        elif c == "down":
            out.append({"date": dt, "key": "maCross", "dir": "sell"})
        # MA20/60
        c = crossed(ma20[i], ma60[i], pma20[i], pma60[i])
        if c == "up":
            out.append({"date": dt, "key": "maGolden60", "dir": "buy"})
        elif c == "down":
            out.append({"date": dt, "key": "maGolden60", "dir": "sell"})
        # 收盘穿 MA20
        c = crossed(close[i], ma20[i], prev_close[i], pma20[i])
        if c == "up":
            out.append({"date": dt, "key": "ma20Cross", "dir": "buy"})
        elif c == "down":
            out.append({"date": dt, "key": "ma20Cross", "dir": "sell"})
        # RSI
        if np.isfinite(rsi[i]):
            if rsi[i] < 30:
                out.append({"date": dt, "key": "rsi", "dir": "buy"})
            elif rsi[i] > 70:
                out.append({"date": dt, "key": "rsi", "dir": "sell"})
        # 布林
        if np.isfinite(bu[i]) and close[i] > bu[i]:
            out.append({"date": dt, "key": "boll", "dir": "buy"})
        elif np.isfinite(bl[i]) and close[i] < bl[i]:
            out.append({"date": dt, "key": "boll", "dir": "sell"})
    return out


# ──────────────────────────────────────────────────────────────────────
# 收益 / 基准
# ──────────────────────────────────────────────────────────────────────
def forward_returns(df: pd.DataFrame) -> pd.DataFrame:
    """按 symbol 组算出 open[t+1] 入场、持有 N 根后在 open[t+1+N] 出场的分批收益。"""
    df = df.sort_values(["symbol", "date"], kind="mergesort")
    g = df.groupby("symbol", sort=False)
    out = df[["symbol", "date"]].copy()
    o_next = g["open"].shift(-1)          # 明日开盘 = 成交价
    for n in HOLDS:
        out[f"fwd{n}"] = (g["open"].shift(-(1 + n)) / o_next - 1.0).to_numpy()
    out["_valid"] = o_next.notna().to_numpy()
    return out


# ──────────────────────────────────────────────────────────────────────
# 统计
# ──────────────────────────────────────────────────────────────────────
def cluster_bootstrap_p(dates: np.ndarray, vals: np.ndarray, rng, perm: int = PERM) -> float:
    """按**事件日聚类**的重采样: 同一天的信号高度相关, 不能当独立样本。

    返回双尾 p 值。
    """
    uniq, inv = np.unique(dates, return_inverse=True)
    cluster_sum = np.bincount(inv, weights=vals)
    cluster_cnt = np.bincount(inv)
    n_cluster = len(uniq)
    if n_cluster < 5:
        return 1.0
    obs = vals.mean()
    hits = 0
    for _ in range(perm):
        idx = rng.integers(0, n_cluster, n_cluster)
        boot = cluster_sum[idx].sum() / cluster_cnt[idx].sum()
        if boot >= 0:
            hits += 1
    p = 2.0 * min(hits, perm - hits) / perm
    return float(min(1.0, p))


def _leg(sub: pd.DataFrame, n: int) -> float | None:
    """单条腿(buy 腿 / sell 腿)在该持有期上的 aligned 超额(%)；腿不存在返回 None。"""
    if sub.empty:
        return None
    v = sub[f"aligned{n}"].dropna()
    if len(v) < 100:
        return None
    return round(float(v.mean()) * 100, 3)


def _leg_stats(sub: pd.DataFrame, rng, perm: int) -> dict | None:
    """一条腿的完整统计。样本不足(单向信号那条不存在的腿)返回 None。

    注意这里 n<200 就判 None —— 一条腿只有几十个样本时 bootstrap 毫无意义，
    宁可写"该腿无结论"也不能拿噪声当证据。
    """
    if sub is None or len(sub) < 200:
        return None
    excess, p, train, test = {}, {}, {}, {}
    for n in HOLDS:
        col = f"aligned{n}"
        v = sub[["date", col]].dropna()
        vals = v[col].to_numpy(dtype=float)
        if len(vals) < 200:
            return None
        excess[str(n)] = round(float(vals.mean()) * 100, 3)
        p[str(n)] = cluster_bootstrap_p(v["date"].to_numpy(), vals, rng, perm)
        tr = sub[sub["is_train"]][col].dropna()
        te = sub[~sub["is_train"]][col].dropna()
        # 训练/测试任一段样本太少, 分段一致性无从谈起 —— 如实留空而不是猜
        train[str(n)] = round(float(tr.mean()) * 100, 3) if len(tr) >= 100 else None
        test[str(n)] = round(float(te.mean()) * 100, 3) if len(te) >= 100 else None
    return {"n": int(len(sub)), "excess": excess, "p": p,
            "train_excess": train, "test_excess": test}


def _split_ok(leg: dict | None) -> bool:
    """训练/测试两段的符号是否一致(任一段缺样本则无从判断, 返回 False)。"""
    if not leg:
        return False
    for n in HOLDS:
        tr, te = leg["train_excess"][str(n)], leg["test_excess"][str(n)]
        if tr is None or te is None:
            return False
        if (tr >= 0) != (te >= 0):
            return False
    return True


def _fmt(leg: dict | None) -> str:
    """一条腿的三周期超额, 给人看。"""
    if not leg:
        return "—"
    return "/".join(f"{leg['excess'][str(n)]:+.2f}%" for n in HOLDS)


def _verdict(leg: dict | None) -> tuple[str, str, float]:
    """一条腿 → (status, note_detail, mean_excess)。三道闸依次拦:
    ① BH 校正后至少一期显著 ② 训练/测试同向 ③ 经济上够本(≥2×成本)。
    """
    if not leg:
        return "untested", "", 0.0
    mean = float(np.mean(list(leg["excess"].values())))
    sig = [str(n) for n in HOLDS if leg["q"][str(n)] < 0.05]
    if not sig:
        return "weak", "三周期多重校正后均不显著 —— 与随机买卖无差异", mean
    if not _split_ok(leg):
        return "weak", "统计显著但训练/测试段方向不一致, 视为不稳健", mean
    # 经济显著性门槛: 平均超额得够得上"再翻一倍手续费"(2×30bp=60bp)才配给结论。
    # 否则百万级样本里 pb 级的差异也会被 bootstrap 判显著 —— 那是统计显著, 不是能赚钱。
    if abs(mean) < 0.6:
        return "weak", (f"统计显著但三周期均值仅 {mean:+.2f}%, "
                        f"扣费后不足 1 倍交易成本的 2 倍, 没有实操意义"), mean
    return ("significant" if mean > 0 else "rejected"), "", mean


def bh_fdr(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg 校正后的 q 值(升序返回原顺序)。"""
    m = len(pvals)
    order = np.argsort(pvals)
    ranked = np.array(pvals, dtype=float)[order]
    q = ranked * m / (np.arange(m) + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(q, 1.0)
    return out.tolist()


def main() -> int:
    ap = argparse.ArgumentParser(description="K 线标注全市场回测")
    ap.add_argument("--symbols", type=int, default=0, help="只取前 N 只标的(试跑)")
    ap.add_argument("--data-dir", default="/app/data")
    ap.add_argument("--out", default="", help="写 signal_stats.json 的路径; 不传则只打印")
    ap.add_argument("--dump", default="", help="导出逐票信号明细到 json(与 TS 实现对拍用)")
    ap.add_argument("--perm", type=int, default=PERM)
    ap.add_argument("--placebo", type=int, default=20000,
                    help="安慰剂对照样本数(随机事件), 0=不做")
    args = ap.parse_args()

    root = Path(args.data_dir)
    t0 = time.time()

    logger.info("读取 enriched …")
    import glob

    files = sorted(glob.glob(str(root / "kline_daily_enriched" / "date=*" / "*.parquet")))
    if not files:
        logger.error("没有 enriched 分区: %s", root)
        return 2
    import polars as pl

    lf = pl.scan_parquet(files).select("symbol", "date", "open", "close", "volume")
    bars = lf.filter(pl.col("close").is_not_null() & (pl.col("close") > 0)).collect()
    bars = bars.sort(["symbol", "date"])
    logger.info("bars: %d 行, %d 只, %.1fs", bars.height, bars["symbol"].n_unique(), time.time() - t0)

    # 剔除名称里带 . / 非 A 股? enriched 的 date= 分区只有 A 股, 但顺手兜一层。
    syms = sorted(bars["symbol"].unique().to_list())
    if args.symbols:
        syms = syms[: args.symbols]
        bars = bars.filter(pl.col("symbol").is_in(syms))

    df = bars.to_pandas()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    # ── 1. 信号 ──
    logger.info("生成标注(逐票, 可能几分钟)…")
    ev_rows: list[dict] = []
    t1 = time.time()
    for sym, g in df.groupby("symbol", sort=True):
        if len(g) <= WARMUP + max(HOLDS) + 2:
            continue
        for ev in events_for_symbol(g):
            ev["symbol"] = sym
            ev_rows.append(ev)
    events = pd.DataFrame(ev_rows)
    logger.info("信号事件 %d 条, %.1fs", len(events), time.time() - t1)
    if args.dump:
        payload: dict = defaultdict(list)
        for r in ev_rows:
            payload[r["symbol"]].append({"date": r["date"], "key": r["key"], "dir": r["dir"]})
        Path(args.dump).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info("已导出信号明细 %s", args.dump)

    # ── 2. 收益 + 基准 ──
    logger.info("计算 forward return 与当日全市场基准…")
    fr = forward_returns(df)
    merged = events.merge(fr, on=["symbol", "date"], how="left")
    merged = merged[merged["_valid"] & merged[f"fwd{min(HOLDS)}"].notna()]

    base = (
        fr[fr["_valid"]]
        .groupby("date")[[f"fwd{n}" for n in HOLDS]]
        .mean()
        .rename(columns={f"fwd{n}": f"base{n}" for n in HOLDS})
    )
    merged = merged.merge(base, on="date", how="left")

    for n in HOLDS:
        excess = merged[f"fwd{n}"] - merged[f"base{n}"]
        # 方向对齐 + 扣费: buy 顺着赚、sell 反向赚(做空), 成交一次 round-trip 30bp
        merged[f"aligned{n}"] = np.where(
            merged["dir"] == "sell", -excess, excess) - COST
    merged["is_train"] = merged["date"] <= TRAIN_UNTIL

    # ── 3. 逐信号统计 ──
    logger.info("bootstrap 检定 (%d 次)…", args.perm)
    rng = np.random.default_rng(SEED)
    rows: list[dict] = []
    for key, direction in SIGNAL_DIR.items():
        sub = merged[merged["key"] == key]
        if len(sub) < 200:
            logger.warning("%s 样本不足(%d), 标记 untested", key, len(sub))
            continue
        excess_map: dict[str, float] = {}
        p_map: dict[str, float] = {}
        for n in HOLDS:
            col = f"aligned{n}"
            v = sub[["date", col]].dropna()
            vals = v[col].to_numpy(dtype=float)
            excess_map[str(n)] = round(float(vals.mean()) * 100, 3)
            p_map[str(n)] = cluster_bootstrap_p(v["date"].to_numpy(), vals, rng, args.perm)
        # 双向信号里"金叉做多"和"死叉做空"可能一条灵一条哑, 合并成一个数会互相掩盖,
        # 所以统计下沉到腿。拆腿是**结构性**的(buy/sell 本就是两条独立规则),
        # 不是看完测试段再挑 —— 符号不参与拆不拆的决策。
        long_sub = sub[sub["dir"] != "sell"]     # buy + neutral 归看涨侧
        short_sub = sub[sub["dir"] == "sell"]
        rows.append({
            "key": key, "direction": direction, "n": len(sub),
            "excess": excess_map, "p": p_map,
            "legs": {
                "long": _leg_stats(long_sub, rng, args.perm),
                "short": _leg_stats(short_sub, rng, args.perm),
            },
            "train_excess": {
                str(n): round(float(sub[sub["is_train"]][f"aligned{n}"].mean()) * 100, 3)
                for n in HOLDS
            },
            "test_excess": {
                str(n): round(float(sub[~sub["is_train"]][f"aligned{n}"].mean()) * 100, 3)
                for n in HOLDS
            },
        })

    # ── 3.5 安慰剂对照 ──
    # 随机挑一批 (symbol, date) 伪事件、随机方向, 走完全相同的对齐/扣费流程。
    # 期望 ≈ -成本(-30bp): 若 placebo 也给出明显更负的超额, 说明是**管道本身**有偏
    # (基准错 / 对齐错 / 收益算错), 而不是信号真的有害 —— 这个检查不过就别下结论。
    if args.placebo:
        pool = (
            merged[["symbol", "date"] + [f"aligned{n}" for n in HOLDS]]
            .drop_duplicates(subset=["symbol", "date"])
            .dropna(subset=[f"aligned{n}" for n in HOLDS])   # 末尾没有未来收益的行丢掉
        )
        n_pl = min(int(args.placebo), len(pool))
        pick = pool.sample(n=n_pl, random_state=SEED)
        flip = rng.choice([1.0, -1.0], size=n_pl)
        logger.info("安慰剂对照 n=%d (随机标的/日期/方向)", n_pl)
        print(f"\n---- 安慰剂对照 (随机事件 x {n_pl}, 期望 ≈ {-COST * 100:.2f}% 只亏手续费) ----")
        for n in HOLDS:
            v = pick[f"aligned{n}"].to_numpy(dtype=float) + COST   # 先把成本加回来
            print(f"  {n:>2}日: 随机方向均值 {(float(np.mean(v * flip)) - COST) * 100:+.3f}%")
        print()

    # ── 4. BH-FDR(按**实际检定次数**) ──
    # 拆分到腿之后检定数从 27 变成"存在的腿 × 3"(双向信号 6 次)。校正次数不跟着加
    # 就是欠校正 —— 等于偷偷放宽了显著性门槛。这里把所有腿的 p 摊平一起校正。
    flat: list[tuple[int, str, int]] = []
    flat_p: list[float] = []
    for i, r in enumerate(rows):
        for leg_name in ("long", "short"):
            lg = r["legs"][leg_name]
            if not lg:
                continue
            for n in HOLDS:
                flat.append((i, leg_name, n))
                flat_p.append(lg["p"][str(n)])
    qs = bh_fdr(flat_p)
    for (i, leg_name, n), q in zip(flat, qs, strict=True):
        rows[i]["legs"][leg_name].setdefault("q", {})[str(n)] = round(q, 6)
    logger.info("BH-FDR 校正 %d 次检定(拆腿后, 不是 27)", len(flat_p))
    # 合并口径的 q 也留着(展示整体用), 但主结论不再基于它
    merged_q = bh_fdr([r["p"][str(n)] for r in rows for n in HOLDS])
    for i, r in enumerate(rows):
        r["q"] = {str(n): merged_q[i * len(HOLDS) + j] for j, n in enumerate(HOLDS)}

    alpha = 0.05
    signals: dict[str, dict] = {}
    for r in rows:
        key = r["key"]
        direction = r["direction"]
        lg, sh = r["legs"]["long"], r["legs"]["short"]
        v_long, v_short = _verdict(lg), _verdict(sh)
        # 主结论取"站得住的那条腿": 两条都给出结论时取幅度大的那条。
        # 都不站得住 → weak, 并把两腿各自的结论写进 note 说明是哪一步没过。
        cands = [(-abs(v[2]), name, v) for name, v, leg in
                 (("long", v_long, lg), ("short", v_short, sh))
                 if leg and v[0] in ("significant", "rejected")]
        if cands:
            cands.sort()
            _, best_leg, (status, note_detail, mean_all) = cands[0]
            both = len(cands) == 2
        else:
            # 两条腿都没站住 —— 也别默认甩给看涨侧, 取**幅度大的那条**展示:
            # 它离门槛最近、信息量最大。(只比 |mean|, 不比符号, 所以不是挑好的看)
            pick = sorted((-abs(v[2]), name, v) for name, v, leg in
                          (("long", v_long, lg), ("short", v_short, sh)) if leg)
            if pick:
                _, best_leg, (status, note_detail, mean_all) = pick[0]
            else:
                best_leg, status, note_detail, mean_all = "long", "weak", "", 0.0
            both = False
        ref_leg = lg if best_leg == "long" else sh
        mean_leg = float(np.mean(list(ref_leg["excess"].values()))) if ref_leg else 0.0
        sig_holds = ([str(n) for n in HOLDS if ref_leg["q"][str(n)] < alpha]
                     if ref_leg else [])
        # 分段一致性看**取用的那条腿**自己; 单向信号只有一条腿, 不能因为"没有第二条
        # 腿"就被判不一致。
        consistent = _split_ok(ref_leg)

        # ── 措辞: 结论指向哪条腿, 就说哪条腿; A 股没有融券, 做空腿 = 减仓/回避 ──
        leg_word = "看涨侧(做多)" if best_leg == "long" else "看跌侧(做空/减仓回避)"
        if status == "weak":
            note = note_detail
            if both or (lg and sh and v_long[0] != "untested" and v_short[0] != "untested"):
                note = (f"看涨侧 {_fmt(lg)} / 看跌侧 {_fmt(sh)} —— 两条腿分别验证, "
                        f"均不成立({note_detail})")
            elif direction == "neutral":
                note = f"放量后跑输 {mean_leg:+.2f}% —— 把它当『要涨』的暗示是错的"
        elif status == "rejected":
            note = (f"{leg_word}跟随后跑输 {mean_leg:+.2f}% —— 是警示标记, 不是买卖机会"
                    if direction != "neutral"
                    else f"放量后跑输 {mean_leg:+.2f}% —— 把它当『要涨』的暗示是错的")
        else:
            note = f"{leg_word} {mean_leg:+.2f}%"

        extra = []
        if direction == "neutral":
            extra.append("中性标记, 不带方向")
        elif direction == "both" and lg and sh:
            other = sh if best_leg == "long" else lg
            # two legs both failed 时别说"取自更显著的一侧" —— 没有一侧显著,
            # 只是取幅度大的那条展示而已。措辞必须和状态自洽。
            extra.append(
                f"双向信号两侧分别统计, 展示幅度较大的一侧({leg_word})"
                if status == "weak"
                else f"双向信号另一侧 {_fmt(other)}"
            )
        # 把**测试段**的幅度直接写出来: 训练/测试只查同向不够, 幅度衰减一半同样不算数,
        # 而看的人有权知道 out-of-sample 到底剩多少。
        if ref_leg:
            te = [ref_leg["test_excess"][str(n)] for n in HOLDS]
            if all(v is not None for v in te):
                extra.append(f"测试段(2023年后) {np.mean(te):+.2f}%")
        extra.append("样本为当前在市股票, 未剔除幸存者偏差, 收益偏乐观")
        signals[key] = {
            "status": status,
            "note": ("；".join([note] + extra) if note else "；".join(extra)),
            "samples": int(r["n"]),
            "best_leg": best_leg,
            "direction": direction,
            "excess": {str(n): r["excess"][str(n)] for n in HOLDS},
            "p_value": {str(n): r["p"][str(n)] for n in HOLDS},
            "q_value": {str(n): r["q"][str(n)] for n in HOLDS},
            "train_excess": {str(n): r["train_excess"][str(n)] for n in HOLDS},
            "test_excess": {str(n): r["test_excess"][str(n)] for n in HOLDS},
            "split_consistent": bool(consistent),
            "primary_holds": sig_holds,
            # 双腿分解: 每条腿各自的样本/超额/校正后 q/训练-测试分解
            "legs": {name: leg for name, leg in (("long", lg), ("short", sh)) if leg},
        }

    sample = {
        "tickers": int(df["symbol"].nunique()),
        "bars": int(bars.height),
        "from": str(df["date"].min()),
        "to": str(df["date"].max()),
        "cost_bp": int(COST * 1e4),
        "split": TRAIN_UNTIL,
        "method": "cluster-bootstrap by date, BH-FDR over 27 tests, open_t+1 fill, "
                  "excess vs same-day cross-sectional mean",
    }
    payload_out = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sample": sample,
        "signals": signals,
    }

    print("\n================ 回测结果 ================")
    print(f"样本: {sample['tickers']} 只 / {sample['bars']} 行 / "
          f"{sample['from']} ~ {sample['to']} / 成本 {sample['cost_bp']}bp")
    print(f"{'signal':<12}{'status':<13}{'取用腿':<8}{'n':>9}"
          f"{'5日%':>9}{'10日%':>9}{'20日%':>9}{'q':>9}{'分段':>6}")
    for key, s in signals.items():
        leg = s["legs"][s["best_leg"]]
        ex = leg["excess"]
        qmin = min(leg["q"].values())
        print(f"{key:<12}{s['status']:<13}{('做多' if s['best_leg'] == 'long' else '做空'):<8}"
              f"{leg['n']:>9}{ex['5']:>9.2f}{ex['10']:>9.2f}{ex['20']:>9.2f}{qmin:>9.3f}"
              f"{'✓' if s['split_consistent'] else '✗':>6}")
    print("\n分方向拆解 (做多腿 = buy/中性标记后持有; 做空腿 = sell 标记后做空 —— "
          "A股无融券, 实操含义是持仓者减仓/回避)")
    print(f"{'signal':<12}{'腿':<6}{'n':>10}{'5日%':>9}{'10日%':>9}{'20日%':>9}{'最低q':>9}")
    for key, s in signals.items():
        for leg_name in ("long", "short"):
            leg = s["legs"].get(leg_name)
            if not leg:
                continue
            mark = "*" if s["best_leg"] == leg_name else " "
            print(f"{key:<12}{('做多' if leg_name == 'long' else '做空') + mark:<6}"
                  f"{leg['n']:>10}"
                  + "".join(f"{leg['excess'][str(n)]:>9.2f}" for n in HOLDS)
                  + f"{min(leg['q'].values()):>9.3f}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("已写 %s (%.1fs)", args.out, time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
