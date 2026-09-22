"""市场环境(regime)计算 — 纯函数模块。

职责: 从已算好的 enriched 数据(含信号列)按日聚合环境指标, 用规则引擎分类离散状态,
持久化为时序表。不重算指标(不走 compute_indicators), 不依赖 quote/depth service。

性能设计:
- run_regime_batch 用 polars group_by("date").agg(...) 一次聚合多日, 非逐日循环。
- 数据走 repo.get_enriched_range(内存缓存, 已含信号列); 缓存不覆盖时走 scan_parquet 慢路径。

与 market_overview_builder 的区别:
- overview 面向单日详情(实时总览), 重算指标。
- regime 面向多日聚合统计(时序分析), 只聚合不重算。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.markets.cn import CN_PROFILE

logger = logging.getLogger(__name__)

# ───────────────────────── 状态分类阈值(可调) ─────────────────────────
# 评分模型对齐看板情绪分(market_overview_builder): 采用 _score(low,high) 归一化
# (比多点插值简洁、不易设错), 4 个轻量维度(赚钱/投机/抗跌/趋势), 阈值与看板统一。
# 设计取舍: 不复制看板的"量能/主线"维度 — 它们依赖 vol_ratio_5d/概念主线等重列,
# 全量回填补算会爆内存; 这两维对历史择时影响小, 且用户可在看板单独查看。

WEIGHTS = {
    "profit": 0.35,        # 赚钱(涨家数/均涨幅/中位涨幅/强弱差) — 最反映赚钱难度
    "speculation": 0.25,   # 投机(涨停数/封板率/连板高度)
    "resilience": 0.20,    # 抗跌(跌家数/大跌股占比) — 识别弱势的关键, 原模型缺失
    "trend": 0.20,         # 趋势(指数涨幅/MA20上方占比)
}

# 离散状态阈值(与看板情绪分统一)
STATE_STRONG = 70       # >= 强势
STATE_LEAN_STRONG = 55  # 55-70 偏强
STATE_RANGE = 45        # 45-55 震荡
STATE_LEAN_WEAK = 30    # 30-45 偏弱
# < 30 弱势

STATE_LABELS = {
    "strong": "强势",
    "lean_strong": "偏强",
    "range": "震荡",
    "lean_weak": "偏弱",
    "weak": "弱势",
}


def _score(value: float, low: float, high: float) -> float:
    """归一化: 把 value 在 [low, high] 区间线性映射到 [0, 100], 钳制边界。

    与看板 market_overview_builder._score 同款。low/high 用 A 股真实分位数校准。
    """
    if high <= low:
        return 50.0
    return float(max(0, min(100, round((value - low) / (high - low) * 100))))


def _compute_subscores(metrics: dict, market: str | None = "cn") -> dict:
    """计算 4 个子维度分 + 综合分(未取整)。供 classify_state 和持久化复用。

    返回 {profit, speculation, resilience, trend, score(float, 0-100)}。
    子维度分也是 0-100, 供趋势图展示"综合分由什么驱动"。

    按市场分流投机维度:
    - cn: 沿用 A 股 4 维(涨停数/封板率/连板高度)— 历史已校准 p15/p85。
    - hk/us: 无涨跌停制度, 改用"动量"+"新高占比"合成投机维度。
      momentum_20d_pct: 20 日动量档位(>=25%/15%/8%/3% 占比, 等权均涨幅打分)。
      new_high_share: 当日收盘创 N 日新高标的占比(>=0.30 强势)。
      两者独立打分后按 0.6/0.4 合成。trend/profit/resilience 共用同一套 _score(low, high),
      仅校准值按市场校准(港美校准沿用 A 股 p15/p85 数据, 跨市场可比但偏宽松,
      后续用真实港美分位数回归再迭代)。

    metrics 期望字段(由 _aggregate_daily 聚合):
      cn: up_pct, down_pct, avg_pct, median_pct, strong_up_pct, strong_down_pct,
          strong_diff_pct, limit_up, seal_rate(0-1), max_consecutive,
          index_pct(小数), above_ma20_pct(0-1)
      hk/us: up_pct, down_pct, avg_pct, median_pct, strong_up_pct, strong_down_pct,
             strong_diff_pct, momentum_20d_pct(均涨幅小数), new_high_share(0-1),
             momentum_25_share, momentum_15_share, momentum_8_share,
             index_pct(小数), above_ma20_pct(0-1)
    """
    m = _normalize_market(market)

    # 赚钱维度: 三市场公用同一组 _score(low, high), 仅校准值沿用 A 股 2022-2026
    # 真实 p15/p85, 跨市场可比但偏宽松(港美走强日偏高估); 后续用真实港美分位数回归。
    profit = (
        _score(metrics.get("up_pct", 50), 21, 75) * 0.45      # 涨家数占比 p15/p85
        + _score(metrics.get("avg_pct", 0) * 100, -1.2, 1.3) * 0.25  # 均涨幅
        + _score(metrics.get("median_pct", 0) * 100, -1.2, 1.3) * 0.20  # 中位涨幅
        + _score(metrics.get("strong_diff_pct", 0), -13, 14) * 0.10  # 强弱差
    )

    # 投机维度: 按市场分流
    if m == "cn":
        speculation = (
            _score(metrics.get("limit_up", 0), 35, 97) * 0.30     # 涨停数 p15/p85
            + _score((metrics.get("seal_rate", 0.5) or 0.5) * 100, 57, 75) * 0.40  # 封板率
            + _score(metrics.get("max_consecutive", 0), 4, 9) * 0.30  # 连板高度
        )
    else:
        # hk/us: 用"20 日动量档位"+"N 日新高占比"合成"动量/趋势"维度。
        # 校准值先用经验值, 等真实港美分位数回归后替换。
        momentum = (
            _score(metrics.get("momentum_20d_pct", 0) * 100, -2, 8) * 0.40      # 均涨幅 % 化
            + _score(metrics.get("momentum_25_share", 0) * 100, 1, 12) * 0.20  # >=25% 占比
            + _score(metrics.get("momentum_15_share", 0) * 100, 5, 20) * 0.20  # >=15% 占比
            + _score(metrics.get("momentum_8_share", 0) * 100, 10, 35) * 0.20  # >=8% 占比
        )
        new_high = _score(metrics.get("new_high_share", 0) * 100, 2, 25)
        speculation = momentum * 0.6 + new_high * 0.4

    # 抗跌维度(关键: 大跌日 strong_down_pct 飙升 → 子分低 → 总分进 weak)
    # 只用 strong_down_pct(大跌股≤-3%占比), 不用 down_pct — 后者与 profit 的 up_pct
    # 是同一信息的正反面, 叠加会放大两极化、挤压震荡区间。
    resilience = 100 - _score(metrics.get("strong_down_pct", 0), 2, 18)

    # 趋势维度
    trend = (
        _score(metrics.get("index_pct", 0) * 100, -2.5, 2.5) * 0.50  # 指数涨幅(对称)
        + _score((metrics.get("above_ma20_pct", 0.5) or 0.5) * 100, 22, 76) * 0.50  # MA20上方
    )

    score = (
        profit * WEIGHTS["profit"]
        + speculation * WEIGHTS["speculation"]
        + resilience * WEIGHTS["resilience"]
        + trend * WEIGHTS["trend"]
    )
    return {
        "profit": profit, "speculation": speculation,
        "resilience": resilience, "trend": trend,
        "score": max(0, min(100, score)),
    }


def classify_state(metrics: dict, market: str | None = "cn") -> tuple[str, int]:
    """规则引擎: 4 维指标 → 离散状态 + 综合分(0-100)。

    对齐看板情绪分的轻量维度(去掉量能/主线以控制内存):
    - 赚钱 profit: 涨家数占比 + 均涨幅 + 中位涨幅 + 强弱差
    - 投机 speculation: 涨停数 + 封板率 + 连板高度
    - 抗跌 resilience: 跌家数占比 + 大跌股占比(大跌日此项暴跌 → 总分进 weak)
    - 趋势 trend: 指数涨幅 + MA20 上方占比

    metrics 期望字段(由 _aggregate_daily 聚合):
      up_pct, down_pct, avg_pct, median_pct, strong_up_pct, strong_down_pct,
      strong_diff_pct, limit_up, seal_rate(0-1), max_consecutive,
      index_pct(小数), above_ma20_pct(0-1)
    """
    sub = _compute_subscores(metrics, market)
    score = max(0, min(100, round(sub["score"])))

    if score >= STATE_STRONG:
        state = "strong"
    elif score >= STATE_LEAN_STRONG:
        state = "lean_strong"
    elif score >= STATE_RANGE:
        state = "range"
    elif score >= STATE_LEAN_WEAK:
        state = "lean_weak"
    else:
        state = "weak"
    return state, score


# ───────────────────────── 批量聚合 ─────────────────────────

def _aggregate_daily(df: pl.DataFrame, index_pct_map: dict | None = None, market: str | None = "cn") -> pl.DataFrame:
    """对多日多 symbol 的 enriched DataFrame 按 date 聚合环境指标。

    纯 polars 聚合, 不重算指标(假设 df 已含 signal_*/change_pct/ma20 等列)。
    index_pct_map: {date: 指数涨幅} 可选, 由调用方从指数数据预先算好。
    梯队指标(首板/N板宽度/晋级率)由 market_phase 提供; phase 列不在此算
    (需要完整日序做平滑), 由 refresh_phase_labels 在 upsert 后统一重标。

    市场分流:
    - cn: 聚合涨停/封板/连板等 A 股专属信号 → metrics 走 cn 评分。
    - hk/us: 聚合 20 日动量档位 + N 日新高占比 → metrics 走 hk/us 评分。
      港美无涨跌停制度, 聚合时跳过 signal_limit_* / consecutive_limit_ups;
      聚合 momentum_20d / signal_n_day_high 列(来自 kline_hk_us_enriched)。
    """
    m = _normalize_market(market)
    from app.services.market_phase import (
        finalize_ladder_row,
        ladder_daily_aggs,
        ladder_promo_aggs,
        with_prev_consecutive,
    )

    needed = ["date", "change_pct", "amount", "signal_limit_up",
              "signal_limit_down", "signal_broken_limit_up",
              "consecutive_limit_ups", "close", "ma20"]
    # 港美补充动量/新高聚合列(港美 enriched schema 已含这些; A 股 schema 无 → 静默跳过)。
    if m != "cn":
        for c in ("momentum_20d", "signal_n_day_high"):
            if c not in needed and c in df.columns:
                needed.append(c)

    # change_pct 兜底派生: A 股 enriched 当前 15 列基础 schema 不含 change_pct
    # (compute_enriched 走精简 schema 输出)。此处用前复权价 (close) per symbol shift 派生,
    # 与涨跌幅榜口径一致; prev_close<=0 时 None(避免退市/断层标的产生 8000% 伪涨跌幅)。
    if "change_pct" not in df.columns and "close" in df.columns and "symbol" in df.columns:
        df = df.with_columns(
            pl.when(pl.col("close").shift(1).over("symbol") > 0)
            .then(pl.col("close") / pl.col("close").shift(1).over("symbol") - 1)
            .otherwise(None)
            .alias("change_pct")
        )

    # signal_limit_up/down 兜底: A 股 enriched 不同日期 schema 不一致(早期 15 列, 最近 12 列),
    # 且 compute_enriched 精简输出不含布尔涨跌停信号。
    # 用 raw_close / raw_close.shift(1) 比值推断:
    #   主板涨停 9.97~10.05、ST 涨停 4.97~5.05 → |change| >= 0.095 视为涨跌停。
    #   区分涨跌方向: 比值>1.0 涨; 比值<1.0 跌。精度受小数四舍五入限制, 容差 0.005 避免误差。
    if "signal_limit_up" not in df.columns:
        if "raw_close" in df.columns and "symbol" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("raw_close").shift(1).over("symbol") > 0)
                .then(
                    (pl.col("raw_close") / pl.col("raw_close").shift(1).over("symbol") - 1).abs() >= 0.095
                )
                .otherwise(None)
                .alias("signal_limit_up")
            )
        else:
            df = df.with_columns(pl.lit(False).alias("signal_limit_up"))
    if "signal_limit_down" not in df.columns:
        if "raw_close" in df.columns and "symbol" in df.columns:
            df = df.with_columns(
                pl.when(pl.col("raw_close").shift(1).over("symbol") > 0)
                .then(
                    (pl.col("raw_close") / pl.col("raw_close").shift(1).over("symbol") - 1) <= -0.095
                )
                .otherwise(None)
                .alias("signal_limit_down")
            )
        else:
            df = df.with_columns(pl.lit(False).alias("signal_limit_down"))
    # signal_broken_limit_up: 无原始价对应字段时给 False (regime 评分弱化该项)
    if "signal_broken_limit_up" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("signal_broken_limit_up"))

    avail = [c for c in needed if c in df.columns]
    if "date" not in avail or "change_pct" not in avail:
        return pl.DataFrame()

    if "consecutive_limit_ups" in avail and "symbol" in df.columns:
        df = with_prev_consecutive(df)

    # 基础聚合 — 全部用 group_by 一次性向量化算出, 避免逐日 filter 扫全表(OOM/超时元凶)。
    has_ma20 = "close" in avail and "ma20" in avail
    has_momentum = m != "cn" and "momentum_20d" in avail
    has_new_high = m != "cn" and "signal_n_day_high" in avail
    grouped = df.group_by("date").agg(
        *[
            pl.col("change_pct").gt(0).sum().alias("up_count")
            if "change_pct" in avail else pl.lit(0).alias("up_count"),
            pl.col("change_pct").lt(0).sum().alias("down_count")
            if "change_pct" in avail else pl.lit(0).alias("down_count"),
            pl.len().alias("total_count"),
        ],
        # 新增: 涨跌幅分布(赚钱/抗跌维度所需) — 全部向量化, 一次算出
        *(
            [
                pl.col("change_pct").mean().alias("avg_pct"),
                pl.col("change_pct").median().alias("median_pct"),
                pl.col("change_pct").ge(0.03).sum().alias("strong_up_count"),
                pl.col("change_pct").le(-0.03).sum().alias("strong_down_count"),
            ]
            if "change_pct" in avail else [
                pl.lit(0).alias("avg_pct"), pl.lit(0).alias("median_pct"),
                pl.lit(0).alias("strong_up_count"), pl.lit(0).alias("strong_down_count"),
            ]
        ),
        *(
            [pl.col("signal_limit_up").cast(pl.Boolean).sum().alias("limit_up")]
            if "signal_limit_up" in avail else [pl.lit(0).alias("limit_up")]
        ),
        *(
            [pl.col("signal_limit_down").cast(pl.Boolean).sum().alias("limit_down")]
            if "signal_limit_down" in avail else [pl.lit(0).alias("limit_down")]
        ),
        *(
            [pl.col("signal_broken_limit_up").cast(pl.Boolean).sum().alias("broken_limit")]
            if "signal_broken_limit_up" in avail else [pl.lit(0).alias("broken_limit")]
        ),
        *(
            [pl.col("consecutive_limit_ups").max().alias("max_consecutive")]
            if "consecutive_limit_ups" in avail else [pl.lit(0).alias("max_consecutive")]
        ),
        *(
            [pl.col("amount").sum().alias("total_amount")]
            if "amount" in avail else [pl.lit(0).alias("total_amount")]
        ),
        *(
            [pl.col("amount").mean().alias("avg_amount")]
            if "amount" in avail else [pl.lit(0).alias("avg_amount")]
        ),
        # MA20 上方占比: 向量化一次算出 (避免逐日 filter 扫全表)。
        # 仅统计 ma20 有效(非空且>0)的行中, close>ma20 的占比。
        *(
            [
                pl.when(pl.col("ma20").is_not_null() & (pl.col("ma20") > 0) & (pl.col("close") > pl.col("ma20")))
                  .then(1).otherwise(None).sum().alias("_above_cnt"),
                pl.when(pl.col("ma20").is_not_null() & (pl.col("ma20") > 0))
                  .then(1).otherwise(None).sum().alias("_valid_cnt"),
            ]
            if has_ma20 else []
        ),
        # 港美动量聚合: 20 日动量均值 + 各档位占比(m25≥0.25/m15≥0.15/m8≥0.08/m3≥0.03)。
        # 列名与 A 股 schema 不冲突, 仅 hk/us 走这条路径。
        *(
            [
                pl.col("momentum_20d").mean().alias("_m20d_mean"),
                pl.col("momentum_20d").ge(0.25).sum().alias("_m25_cnt"),
                pl.col("momentum_20d").ge(0.15).sum().alias("_m15_cnt"),
                pl.col("momentum_20d").ge(0.08).sum().alias("_m8_cnt"),
                pl.col("momentum_20d").ge(0.03).sum().alias("_m3_cnt"),
            ]
            if has_momentum else []
        ),
        # 港美新高聚合: N 日新高占比。
        *(
            [
                pl.col("signal_n_day_high").cast(pl.Boolean).sum().alias("_nh_cnt"),
            ]
            if has_new_high else []
        ),
        # 梯队指标(阶段判定所需): 首板/N板宽度/非空档位数; 晋级率需 _prev_consec
        # — 港美无连板梯队, 跳过 ladder_* 聚合, 但 group_by 仍按 A 股路径走 — 安全:
        #   if 条件自然为 False, ladder_* 不会聚合, metrics dict 里 ladder_xxx=None
        #   → finalize_ladder_row 安全返回空 dict。
        *(
            ladder_daily_aggs()
            if "consecutive_limit_ups" in avail else []
        ),
        *(
            ladder_promo_aggs()
            if "consecutive_limit_ups" in avail and "_prev_consec" in df.columns else []
        ),
    ).sort("date")

    # 转成 dict 列表做分类(规则引擎需逐日算, 但只扫 grouped 行数=天数, 不再回扫全表)
    index_pct_map = index_pct_map or {}
    rows = []
    for r in grouped.iter_rows(named=True):
        up = r.get("up_count", 0) or 0
        down = r.get("down_count", 0) or 0
        total = r.get("total_count", 0) or 0
        limit_up = r.get("limit_up", 0) or 0
        broken = r.get("broken_limit", 0) or 0
        # MA20 上方占比: 来自向量化聚合 (None→0)
        valid_cnt = r.get("_valid_cnt") or 0
        above_cnt = r.get("_above_cnt") or 0
        ma20_above = (above_cnt / valid_cnt) if valid_cnt > 0 else 0.0
        # 涨跌幅分布(占比, 0-100)
        up_pct = (up / total * 100) if total > 0 else 0.0
        down_pct = (down / total * 100) if total > 0 else 0.0
        strong_up_pct = ((r.get("strong_up_count", 0) or 0) / total * 100) if total > 0 else 0.0
        strong_down_pct = ((r.get("strong_down_count", 0) or 0) / total * 100) if total > 0 else 0.0
        avg_pct = r.get("avg_pct", 0.0) or 0.0
        median_pct = r.get("median_pct", 0.0) or 0.0
        # 港美动量派生指标: 由聚合出的 _m20d_mean / _m25_cnt / _m15_cnt / _m8_cnt / _m3_cnt
        # 与 _nh_cnt 派生为 metrics 字段, 供 _compute_subscores 港美路径使用。
        m20d_mean = r.get("_m20d_mean", 0.0) or 0.0
        m25_cnt = r.get("_m25_cnt", 0) or 0
        m15_cnt = r.get("_m15_cnt", 0) or 0
        m8_cnt = r.get("_m8_cnt", 0) or 0
        nh_cnt = r.get("_nh_cnt", 0) or 0
        metrics = {
            "limit_up": limit_up,
            "limit_down": r.get("limit_down", 0) or 0,
            "broken_limit": broken,
            "max_consecutive": r.get("max_consecutive", 0) or 0,
            "seal_rate": (limit_up / (limit_up + broken)) if (limit_up + broken) > 0 else 0.5,
            "up_count": up,
            "down_count": down,
            "up_ratio": (up / down) if down > 0 else (float(up) if up > 0 else 1.0),
            "index_pct": index_pct_map.get(r["date"], 0.0),
            "above_ma20_pct": ma20_above,
            "total_amount": r.get("total_amount", 0) or 0,
            "avg_turnover": r.get("avg_amount", 0) or 0,
            # 新模型所需(对齐看板)
            "up_pct": up_pct,
            "down_pct": down_pct,
            "avg_pct": avg_pct,
            "median_pct": median_pct,
            "strong_up_pct": strong_up_pct,
            "strong_down_pct": strong_down_pct,
            "strong_diff_pct": strong_up_pct - strong_down_pct,
            # 港美动量/新高派生指标 — cn 路径下为 0, 不影响评分(走 cn 分支时 metrics.get 默认值兜底)。
            "momentum_20d_pct": m20d_mean,
            "momentum_25_share": (m25_cnt / total) if total > 0 else 0.0,
            "momentum_15_share": (m15_cnt / total) if total > 0 else 0.0,
            "momentum_8_share": (m8_cnt / total) if total > 0 else 0.0,
            "new_high_share": (nh_cnt / total) if total > 0 else 0.0,
        }
        state, score = classify_state(metrics, market)
        # 4 个子维度分(供趋势图展示"综合分由什么驱动" + 未来策略按子维度过滤)
        sub = _compute_subscores(metrics, market)
        rows.append({
            "date": r["date"],
            "state": state,
            "score": score,
            "limit_up": limit_up,
            "limit_down": metrics["limit_down"],
            "broken_limit": broken,
            "max_consecutive": metrics["max_consecutive"],
            "seal_rate": round(metrics["seal_rate"], 4),
            "up_count": up,
            "down_count": down,
            "up_ratio": round(metrics["up_ratio"], 4),
            "index_pct": round(metrics["index_pct"], 4),
            "above_ma20_pct": round(ma20_above, 4),
            "total_amount": metrics["total_amount"],
            "avg_turnover": metrics["avg_turnover"],
            # 新增列(供未来策略按强势股占比等过滤)
            "avg_pct": round(avg_pct, 4),
            "median_pct": round(median_pct, 4),
            "strong_up_pct": round(strong_up_pct, 4),
            "strong_down_pct": round(strong_down_pct, 4),
            # 港美动量/新高持久化列(cn 路径写 0): 供未来策略按动量档位过滤。
            "momentum_20d_pct": round(metrics.get("momentum_20d_pct", 0.0), 4),
            "momentum_25_share": round(metrics.get("momentum_25_share", 0.0), 4),
            "momentum_15_share": round(metrics.get("momentum_15_share", 0.0), 4),
            "momentum_8_share": round(metrics.get("momentum_8_share", 0.0), 4),
            "new_high_share": round(metrics.get("new_high_share", 0.0), 4),
            # 4 个子维度分(0-100, 综合分加权来源): 赚钱/投机/抗跌/趋势
            "profit_score": round(sub["profit"]),
            "speculation_score": round(sub["speculation"]),
            "resilience_score": round(sub["resilience"]),
            "trend_score": round(sub["trend"]),
            # 梯队指标(阶段判定所需); phase 由 refresh_phase_labels 统一重标
            **finalize_ladder_row(r),
        })
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# 全量回填分批参数(控制内存峰值) —— 实际值从用户偏好读取(preferences.get_regime_*),
# 这里的常量仅作 fallback(偏好读取失败时)和文档说明:
# - batch_days: 每批目标交易日数。越小内存越省、批次越多越慢; ma20 需 20 交易日。
# - warmup_days: 每批前缀预热天数(日历日), 必须 > ma20 的 20 交易日(≈28 日历日)。
_REGIME_BATCH_DAYS_DEFAULT = 60
_REGIME_WARMUP_DAYS_DEFAULT = 40


def _compute_batch(repo, enriched_dir, instruments, historical_shares,
                   batch_start: date, batch_end: date, warmup_days: int) -> pl.DataFrame:
    """单批: 读 [batch_start-warmup, batch_end] → 算指标 → 截断回 [batch_start, batch_end]。

    warmup 前缀保证每批边界的滚动窗口指标(ma20)正确, 不依赖相邻批次。
    返回目标区间(不含 warmup)的含指标列 DataFrame。
    """
    from datetime import timedelta

    from app.indicators.pipeline import compute_indicators, compute_limit_signals
    warmup_start = batch_start - timedelta(days=warmup_days)
    df = pl.scan_parquet(enriched_dir / "**" / "*.parquet").filter(
        (pl.col("date") >= warmup_start) & (pl.col("date") <= batch_end)
    ).collect()
    if df.is_empty():
        return pl.DataFrame()
    # 新评分模型只需 change_pct(赚钱/抗跌维) + ma20(趋势维); 不再需要 vol_ratio_5d
    # (compute_limit_signals 文档虽提及但函数体未实际使用, 已验证可安全省去 → 省内存)
    df = compute_indicators(df, needed={"change_pct", "ma20"})
    if instruments is not None and not instruments.is_empty():
        df = compute_limit_signals(
            df, instruments,
            needed={"signal_limit_up", "signal_limit_down", "signal_broken_limit_up"},
            historical_shares=historical_shares,
        )
    # 晋级率需要昨日连板数: 在裁掉 warmup 之前先按 symbol 平移,
    # 保证每批首日的 _prev_consec 来自 warmup 的最后一个交易日而非 null。
    from app.services.market_phase import with_prev_consecutive
    df = with_prev_consecutive(df)
    # 丢弃 warmup 行, 只留目标区间
    return df.filter((pl.col("date") >= batch_start) & (pl.col("date") <= batch_end))


def _scan_enriched_fallback(repo, start: date, end: date) -> pl.DataFrame | None:
    """缓存不覆盖时的慢路径: scan enriched parquet + 重算所需指标列。

    仅在 regime 首次全量回填或缓存未预热时触发。返回含信号列的多日 DataFrame。

    内存控制(关键, 两层优化):
    1. needed 白名单: regime 只需 change_pct/ma20/涨跌停信号等少数列, 不用 compute_all
       算 72 列全套指标(那会让全量峰值达 6.8GB)。
    2. 分批: 范围超过 batch_days 个交易日时按批切片, 每批带 warmup 前缀算完后 concat。
       batch_days / warmup_days 由用户偏好控制(数据页「市场环境」卡片设置),
       实测默认值(60/40)全量(515万行)峰值约 1.9GB, 4GB 内存机器可稳跑。
    必须传入 instruments(涨跌停价表), 否则 compute_limit_signals 会跳过涨跌停信号。
    """
    try:
        from app.services import preferences
        batch_days = preferences.get_regime_batch_days()
        warmup_days = preferences.get_regime_warmup_days()
    except Exception:
        batch_days = _REGIME_BATCH_DAYS_DEFAULT
        warmup_days = _REGIME_WARMUP_DAYS_DEFAULT

    try:
        enriched_dir = repo.store.data_dir / "kline_daily_enriched"
        if not enriched_dir.exists():
            return None
        instruments = repo.get_instruments()
        historical_shares = repo.get_historical_shares()

        # 收集目标区间内所有交易日, 决定是否分批
        target_dates = sorted(d for d in enriched_date_set(repo)
                              if start <= d <= end)
        if not target_dates:
            return None

        # 小范围: 单次算(无分批开销)
        if len(target_dates) <= batch_days:
            df = _compute_batch(repo, enriched_dir, instruments, historical_shares,
                                target_dates[0], target_dates[-1], warmup_days)
            return df if not df.is_empty() else None

        # 大范围: 按交易日分批, 逐批算 + concat
        batches = [
            (target_dates[i], target_dates[min(i + batch_days - 1, len(target_dates) - 1)])
            for i in range(0, len(target_dates), batch_days)
        ]
        logger.info("regime fallback: %d 天分 %d 批 (每批≤%d天 + %d天warmup)",
                    len(target_dates), len(batches), batch_days, warmup_days)
        parts: list[pl.DataFrame] = []
        for bs, be in batches:
            df = _compute_batch(repo, enriched_dir, instruments, historical_shares, bs, be, warmup_days)
            if not df.is_empty():
                parts.append(df)
        if not parts:
            return None
        return pl.concat(parts, how="vertical_relaxed")
    except Exception as e:
        logger.warning("regime scan_enriched_fallback failed: %s", e)
        return None


def _load_index_pct(repo, start: date, end: date, symbol: str = CN_PROFILE.benchmark_symbol) -> dict:
    """读取主力指数日K, 算每日涨幅 → {date: pct}。指数数量少, 单次读取可接受。"""
    try:
        df = repo.get_index_daily(symbol, start, end, columns=["date", "change_pct"])
        if df.is_empty() or "change_pct" not in df.columns:
            return {}
        return {r["date"]: float(r["change_pct"] or 0) for r in df.iter_rows(named=True)}
    except Exception as e:
        logger.warning("regime load_index_pct failed: %s", e)
        return {}


# 各市场基准指数(港股恒指 / 美股标普 500) — regime batch 用于 index_pct 注入。
# 港美本地无 index_daily 接口(Yahoo 实时拉取被 sandbox 屏蔽), 故先尝试走 repo,
# 失败兜底为 0(意味着 trend 子分被钳到 50 中位, 不爆炸)。
_HK_BENCHMARK = "^HSI"          # Hang Seng Index
_US_BENCHMARK = "^GSPC"         # S&P 500
_MARKET_BENCHMARKS: dict[str, str] = {
    "cn": CN_PROFILE.benchmark_symbol,
    "hk": _HK_BENCHMARK,
    "us": _US_BENCHMARK,
}


def _scan_hk_us_enriched_for_regime(repo, start: date, end: date, market: str) -> pl.DataFrame | None:
    """港美 regime 批算的 enriched 扫描函数。

    输入数据: kline_hk_us_enriched/symbol=*.{HK,US}/part.parquet(per-symbol 全历史单文件)。
    输出: 与 A 股 enriched 一致的 {date, change_pct, amount, signal_limit_up: False,
           close, ma20, momentum_20d, signal_n_day_high} 多日 DataFrame(限 [start, end])。
    港美无涨跌停/连板信号, 这里显式置 None/False 让 _aggregate_daily 的 if 条件不触发聚合。
    """
    m = _normalize_market(market)
    suffix = f".{m.upper()}"
    enriched_dir = repo.store.data_dir / "kline_hk_us_enriched"
    if not enriched_dir.exists():
        return None
    try:
        # 与 hk_us_overview_builder 一致: 跨分区 schema 不同(integer vs float), 需要 cast_options。
        # 按市场后缀过滤(symbol.endswith('.HK' / '.US')) — 同一目录两个市场分区共存。
        df = pl.scan_parquet(
            str(enriched_dir / "symbol=*" / "part.parquet"),
            cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
        ).filter(
            (pl.col("symbol").str.ends_with(suffix))
            & (pl.col("date") >= start)
            & (pl.col("date") <= end)
        ).collect()
    except Exception as e:
        logger.warning("regime scan_hk_us_enriched failed: %s", e)
        return None
    if df.is_empty():
        return None

    # 港美无涨跌停/连板信号, 显式补 False / 0 列, 让 _aggregate_daily 的
    # `if "signal_limit_up" in avail` 条件分支按"信号缺失"路径走 — 不会聚合 limit_up。
    if "signal_limit_up" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("signal_limit_up"))
    if "signal_limit_down" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("signal_limit_down"))
    if "signal_broken_limit_up" not in df.columns:
        df = df.with_columns(pl.lit(False).alias("signal_broken_limit_up"))
    if "consecutive_limit_ups" not in df.columns:
        df = df.with_columns(pl.lit(0).alias("consecutive_limit_ups"))
    return df


def run_regime_batch(repo, start: date, end: date, market: str | None = "cn") -> pl.DataFrame:
    """批算 [start, end] 的环境时序。

    性能: 优先 repo.get_enriched_range(内存缓存); 缓存不覆盖走 scan_parquet 慢路径。
    按 date group_by 聚合, 不逐日重算。返回完整时序 DataFrame(可能为空)。

    市场分流:
    - cn: 走原 A 股路径(repo.get_enriched_range + _scan_enriched_fallback + ST 过滤)。
    - hk/us: 走 _scan_hk_us_enriched_for_regime(per-symbol 分区全目录扫描), 不做 ST 过滤。
    """
    if start > end:
        return pl.DataFrame()
    m = _normalize_market(market)

    # 指数涨幅(主力指数) — 各市场独立基准
    bench = _MARKET_BENCHMARKS[m]
    index_pct_map = _load_index_pct(repo, start, end, symbol=bench)

    if m == "cn":
        # A 股: 走原路径(内存缓存 → scan fallback → ST 过滤 → 聚合)
        df = repo.get_enriched_range(start, end)
        if df is None or df.is_empty():
            logger.info("regime batch[cn]: enriched cache miss [%s~%s], fallback to scan", start, end)
            df = _scan_enriched_fallback(repo, start, end)
        if df is None or df.is_empty():
            logger.info("regime batch[cn]: no enriched data for [%s~%s]", start, end)
            return pl.DataFrame()
        try:
            from app.services import preferences as _prefs_st
            exclude_st = _prefs_st.get_sentiment_exclude_st()
        except Exception:
            exclude_st = True
        if exclude_st:
            from app.services.market_mainline import load_risk_warning_symbols
            st_syms = load_risk_warning_symbols(repo.store.data_dir)
            if st_syms and "symbol" in df.columns:
                df = df.filter(
                    ~pl.col("symbol").str.to_uppercase().is_in(sorted(st_syms))
                )
    else:
        # hk/us: 走 per-symbol 全目录扫描; 无 ST 制度, 跳过风险警示过滤
        df = _scan_hk_us_enriched_for_regime(repo, start, end, m)
        if df is None or df.is_empty():
            logger.info("regime batch[%s]: no enriched data for [%s~%s]", m, start, end)
            return pl.DataFrame()

    return _aggregate_daily(df, index_pct_map, market=market)


# ───────────────────────── 持久化(upsert) ─────────────────────────

REGIME_DIR = "regime_history"

# 市场 → regime 子目录(持久化切分后, 各市场独立的 part.parquet)。
# 历史(commit 前)只有 cn 有数据, 一直写到 data/regime_history/part.parquet;
# 新切分下 cn 写到 cn/part.parquet, hk/us 独立子目录; 老单文件路径仍被 load_regime_history
# 作为 "cn 兜底" 识别(向下兼容, 无需 data 迁移)。
_MARKET_SUB_DIRS: dict[str, str] = {"cn": "cn", "hk": "hk", "us": "us"}


def _normalize_market(market: str | None) -> str:
    """规范化市场标识, 未知值回退 cn。"""
    if market is None:
        return "cn"
    m = str(market).strip().lower()
    if m in _MARKET_SUB_DIRS:
        return m
    if m == "a" or m == "cn_a" or m == "a股":
        return "cn"
    if m == "hkex" or m == "港股":
        return "hk"
    if m == "us_market" or m == "美股":
        return "us"
    return "cn"


def regime_path(data_dir: Path, market: str | None = "cn") -> Path:
    """市场专用的 regime 历史文件路径。

    - 新切分: data_dir/regime_history/{cn,hk,us}/part.parquet
    - 老单文件(仅 cn, 迁移前默认位置): data_dir/regime_history/part.parquet 仍可读,
      但写新数据永远走新切分路径。cn 既有数据会在首次 upsert 时被"读到 → 切走"到新目录。
    """
    m = _normalize_market(market)
    return data_dir / REGIME_DIR / _MARKET_SUB_DIRS[m] / "part.parquet"


def _legacy_regime_path(data_dir: Path) -> Path:
    """迁移前老单文件路径(仅 cn 市场兜底读)。"""
    return data_dir / REGIME_DIR / "part.parquet"


def load_regime_history(data_dir: Path, market: str | None = "cn") -> pl.DataFrame:
    """读取指定市场的 regime 时序; 不存在返回空 DataFrame。

    读取优先级:
    1. 新切分路径 data_dir/regime_history/{market}/part.parquet
    2. 仅 cn 市场兜底: 老单文件 data_dir/regime_history/part.parquet
    """
    m = _normalize_market(market)
    new_p = data_dir / REGIME_DIR / _MARKET_SUB_DIRS[m] / "part.parquet"
    if new_p.exists():
        try:
            return pl.read_parquet(new_p)
        except Exception as e:
            logger.warning("load_regime_history failed (new path %s): %s", new_p, e)
            return pl.DataFrame()
    if m == "cn":
        legacy = _legacy_regime_path(data_dir)
        if legacy.exists():
            try:
                return pl.read_parquet(legacy)
            except Exception as e:
                logger.warning("load_regime_history failed (legacy %s): %s", legacy, e)
    return pl.DataFrame()


def refresh_phase_labels(data_dir: Path, market: str | None = "cn") -> int:
    """对指定市场的 regime 时序重标情绪周期阶段。

    阶段判定需要完整日序(EMA 平滑 + 持续性确认), 不能在单批内完成,
    因此每次 upsert 后调用本函数整体重标并写回。行数为天数(千级), 开销可忽略。
    返回标注的天数; 阶段列缺失所需指标(旧 schema 未重算)时返回 0。
    """
    from app.services.market_phase import classify_phase_series

    df = load_regime_history(data_dir, market=market)
    required = {"date", "max_consecutive", "first_board", "ge2_count", "promo_rate", "seal_rate"}
    if df.is_empty() or not required.issubset(df.columns):
        return 0
    try:
        labeled = classify_phase_series(df)
    except Exception as e:
        logger.warning("refresh_phase_labels failed: %s", e)
        return 0
    target = regime_path(data_dir, market=market)
    target.parent.mkdir(parents=True, exist_ok=True)
    labeled.write_parquet(target)
    return labeled.height


def upsert_regime_history(data_dir: Path, new_rows: pl.DataFrame, market: str | None = "cn") -> None:
    """按 date 覆盖(upsert): 重算的天覆盖旧行, 新天追加。
    按市场持久化到 {market}/part.parquet, 老单文件路径不再写(读时仍兜底)。

    读旧 → anti-join 掉 new_rows 的天 → concat new_rows → 排序 → 写回。
    schema 兼容: 旧 parquet 可能缺新列(评分模型迭代新增的 avg_pct 等),
    concat 前给旧数据补缺失列(null), 让旧 parquet 首次重写时自动迁移到新 schema。
    """
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = regime_path(data_dir, market=market)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_dates = set(new_rows["date"].to_list())
    old = load_regime_history(data_dir, market=market)
    if old.is_empty():
        combined = new_rows
    else:
        kept = old.filter(~pl.col("date").is_in(list(new_dates)))
        # schema 对齐: 以 new_rows 的列名+顺序为权威, 旧数据补缺失列(null),
        # 并按相同列顺序 select, 确保 concat 不报 "schema names/lengths differ"。
        # 这样旧 parquet 首次重写时自动迁移到新 schema(新列在旧日期为 null)。
        target_cols = new_rows.columns
        keep_exprs = []
        for c in target_cols:
            if c in kept.columns:
                keep_exprs.append(pl.col(c))
            else:
                keep_exprs.append(pl.lit(None).alias(c))
        kept = kept.select(keep_exprs)
        new_rows = new_rows.select(target_cols)
        combined = pl.concat([kept, new_rows], how="vertical_relaxed")
    combined = combined.sort("date").unique(subset=["date"], keep="last")
    combined.write_parquet(p)


def get_regime_coverage(data_dir: Path, market: str | None = "cn") -> dict:
    """返回 regime 时序的覆盖元信息(供数据画像/API)。"""
    df = load_regime_history(data_dir, market=market)
    if df.is_empty():
        return {"rows": 0, "earliest_date": None, "latest_date": None}
    return {
        "rows": df.height,
        "earliest_date": str(df["date"].min()),
        "latest_date": str(df["date"].max()),
    }


def detect_stale_dates(data_dir: Path, repo, market: str | None = "cn") -> list[date]:
    """检测 regime 已有但需要重算的天(enriched 被覆写)。

    用 mtime 比对: enriched 分区 parquet 的 mtime > regime parquet 的 mtime
    → 该日 enriched 更新过, regime 需重算。

    - cn: 用 kline_daily_enriched(per-date 分区) — 历史上一直是 per-date 路径。
    - hk/us: 用 kline_hk_us_enriched(per-symbol 分区) — 港美无 per-date,
      用全目录最新 mtime 与 regime 最新 mtime 比较, 只要有任一 symbol parquet 被覆写过,
      则该市场全量重算(简单可靠, 不会漏; 港美数据闭环后增量为 0, 开销可控)。
    """
    regime_p = regime_path(data_dir, market=market)
    if not regime_p.exists():
        # 老 cn 数据在 legacy 路径时也认
        if _normalize_market(market) == "cn":
            regime_p = _legacy_regime_path(data_dir)
            if not regime_p.exists():
                return []
        else:
            return []
    try:
        regime_mtime = regime_p.stat().st_mtime
    except OSError:
        return []
    m = _normalize_market(market)
    if m == "cn":
        enriched_dir = repo.store.data_dir / "kline_daily_enriched"
        if not enriched_dir.exists():
            return []
        stale: list[date] = []
        existing = load_regime_history(data_dir, market=market)
        if existing.is_empty():
            return []
        existing_dates = set(existing["date"].to_list())
        for part in enriched_dir.glob("date=*/part.parquet"):
            try:
                ds = part.parent.name.replace("date=", "")
                d = date.fromisoformat(ds)
            except (ValueError, OSError):
                continue
            if d not in existing_dates:
                continue
            try:
                if part.stat().st_mtime > regime_mtime:
                    stale.append(d)
            except OSError:
                continue
        return sorted(stale)
    else:
        # hk/us: per-symbol 分区, 全目录整体比较
        enriched_dir = repo.store.data_dir / "kline_hk_us_enriched"
        if not enriched_dir.exists():
            return []
        try:
            latest_mtime = max(
                (p.stat().st_mtime for p in enriched_dir.glob("symbol=*/*.parquet")),
                default=0.0,
            )
        except OSError:
            return []
        if latest_mtime <= regime_mtime:
            return []
        # 整个市场标记为全量重算, 由 compute_regime_incremental 跑批算并 upsert 全量覆盖。
        existing = load_regime_history(data_dir, market=market)
        if existing.is_empty():
            return []
        return sorted(d for d in existing["date"].to_list())


def latest_phase_transition(data_dir: Path, market: str | None = "cn") -> tuple[str, str, str] | None:
    """读取 regime 时序末两日, 返回最近一次阶段切换 (prev, new, 日期str)。

    末两日阶段相同(或数据不足/无阶段列)返回 None。供盘后管道推送阶段切换通知。
    """
    hist = load_regime_history(data_dir, market=market)
    if hist.is_empty() or "phase" not in hist.columns:
        return None
    tail = hist.select(["date", "phase"]).sort("date").tail(2)
    if tail.height < 2:
        return None
    prev_phase, cur_phase = tail["phase"].to_list()
    if not prev_phase or not cur_phase or prev_phase == cur_phase:
        return None
    return prev_phase, cur_phase, str(tail["date"][-1])


def compute_regime_incremental(
    repo,
    data_dir: Path,
    *,
    today: date | None = None,
    market: str | None = "cn",
    max_backfill_days: int = 30,
) -> pl.DataFrame:
    """增量计算 regime(供 daily_pipeline / 启动补算调用)。

    双检测: 1) 缺口(enriched 有但 regime 没有) 2) stale(enriched 被覆写)。
    自动补齐所有需要的日。返回本次新算的 DataFrame。
    market 决定扫哪个 enriched 目录(cn = kline_daily_enriched, hk/us = kline_hk_us_enriched),
    并写入对应市场的 regime_history 子目录。

    max_backfill_days: 最多回溯多少个交易日(默认 30, 与 strength_ladder 对齐)。
    enriched 存的是全历史(港美最早到 1970s), 首次运行时缺口可达数千天;
    不设窗口的话盘后管道要一次批算上千天, 会拖垮日管道 —— 这正是
    pipeline_regime_enabled 默认关闭的原因。只补最近 N 天即可满足看板需求:
    每天盘后新增的缺口只有 1 天, 永远落在窗口内。设 0/负数 = 不限制(慎用)。
    """
    # 市场时钟·B类: 这里的 date.today() 只是 `today` 参数的**缺省兜底** ——
    # 盘后管道会显式传入市场感知的 today, 缺省路径只在手工调用时走; 改成市场
    # 日期会让缺省值与调用方传入值口径不一致, 且本函数已有 market 参数可分流。
    today = today or date.today()
    m = _normalize_market(market)
    existing = load_regime_history(data_dir, market=m)

    # 缺口: enriched 有哪些天, regime 缺哪些
    enriched_dates = enriched_date_set(repo, market=m)
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)

    # stale: enriched 覆写过
    stale = detect_stale_dates(data_dir, repo, market=m)

    to_compute = sorted(set(missing) | set(stale))
    if not to_compute:
        logger.debug("regime incremental: nothing to compute (market=%s)", m)
        return pl.DataFrame()

    if max_backfill_days > 0 and len(to_compute) > max_backfill_days:
        skipped_days = len(to_compute) - max_backfill_days
        to_compute = to_compute[-max_backfill_days:]
        logger.info(
            "regime incremental[%s]: %d 天缺口, 只补最近 %d 天(跳过 %d 天历史)",
            m, len(to_compute) + skipped_days, len(to_compute), skipped_days,
        )

    logger.info(
        "regime incremental[%s]: compute %d days (missing=%d, stale=%d)",
        m, len(to_compute), len(missing), len(stale),
    )
    new_rows = run_regime_batch(repo, start=to_compute[0], end=to_compute[-1], market=m)
    if not new_rows.is_empty():
        upsert_regime_history(data_dir, new_rows, market=m)
        refresh_phase_labels(data_dir, market=m)
    return new_rows


def _as_date(value) -> date:
    """把 enriched 的日期值统一成 datetime.date。

    坑: datetime 是 date 的子类, `isinstance(dt, date)` 恒为 True, 所以
    "isinstance(d, date) 就用原值" 的写法会让港美 enriched 的 Datetime('us')
    原样溜过去。后续一旦与 date.today() 比较就炸
    (TypeError: can't compare datetime.datetime to datetime.date)。
    这里先判 datetime 再判 date, 从源头归一化, regime 与 strength_ladder 都受益。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return date.fromisoformat(str(value)[:10])


def enriched_date_set(repo, market: str | None = "cn") -> set[date]:
    """扫描 enriched 分区目录, 返回该市场所有已有日期集合。

    - cn: kline_daily_enriched/date=*/part.parquet(per-date)
    - hk/us: kline_hk_us_enriched/symbol=*.{HK,US}/part.parquet(per-symbol),
      取所有 parquet 的 distinct date
    """
    m = _normalize_market(market)
    dates: set[date] = set()
    if m == "cn":
        enriched_dir = repo.store.data_dir / "kline_daily_enriched"
        if not enriched_dir.exists():
            return dates
        for part in enriched_dir.glob("date=*/part.parquet"):
            try:
                ds = part.parent.name.replace("date=", "")
                dates.add(date.fromisoformat(ds))
            except ValueError:
                continue
        return dates
    else:
        # hk/us: per-symbol 分区, 选后缀一致 symbol 的 parquet, 拼 distinct date
        suffix = ".HK" if m == "hk" else ".US"
        enriched_dir = repo.store.data_dir / "kline_hk_us_enriched"
        if not enriched_dir.exists():
            return dates
        seen_paths: set[str] = set()
        for part in enriched_dir.glob(f"symbol=*{suffix}/part.parquet"):
            if str(part) in seen_paths:
                continue
            seen_paths.add(str(part))
            try:
                df = pl.read_parquet(part, columns=["date"])
            except Exception:
                continue
            for d in df["date"].to_list():
                try:
                    dates.add(_as_date(d))
                except (TypeError, ValueError):
                    continue
        return dates


def earliest_enriched_date(repo, market: str | None = "cn") -> date | None:
    """返回 enriched 最早日期(供全量重算定起点)。无数据返回 None。"""
    dates = enriched_date_set(repo, market=market)
    return min(dates) if dates else None
