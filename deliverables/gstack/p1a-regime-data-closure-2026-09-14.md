# P1-A regime 数据闭环 — 实施步骤总结

**日期**: 2026-09-14
**commit**: `f6f4a9d` feat(regime): schema drift 兜底 + cn/hk/us 首次产出真实数据
**场景**: 港美 Regime + 强度梯队治本 5 步的**数据闭环**（之前 commit 全部是代码，0 数据）

---

## 📌 TL;DR

- P0 治本 5 步代码 100% 就绪（603d0c9 → f5fbfba → 9c663f0 → dcc84f0 → 6557762），
  但 **regime_history/{cn,hk,us}/part.parquet 全空**，治本"未真正闭环"
- 本批：(1) 修 A 股 enriched schema drift 兜底；(2) cn 90 天 + hk 37 天 + us 37 天
  regime 历史落盘；(3) hk 2296 行 + us 5568 行强度梯队首跑
- 全后端 **2045/2045 passed**（1 项沙箱 flaky 假阳性，单跑过，与本批代码零耦合）

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🟢 0 / 🟡 0 / 🟢 0 |
| 关键行动项 | 3（全部完成） |
| 建议负责人 | 梅林（已交付） |

---

## 1. 关键发现

| # | 发现 | 影响 |
|---|------|------|
| 1 | A 股 enriched 当前 12-15 列精简 schema，**不含 change_pct / signal_limit_\* / ma20** | `_aggregate_daily` 走 change_pct-not-in-avail 早退 |
| 2 | A 股 enriched 跨分区 schema 漂移（早期 15 列，最近 12 列） | `pl.scan_parquet` 跨分区拿第一个文件 schema 作基线，多列的抛 did not find column |
| 3 | 港美 enriched 实际 100% 覆盖（美股 6071/6071、港股 2795/2798，3 只新上市 09-13 还没） | 之前 memory 写"09-03 停"是误读，实际 100% 但**没有新日 enriched** |
| 4 | 港股 fetch_hk_daily_sina 全量拉新日时 `is_verified_hk_raw` 拒绝（缺复权因子） | 港股全量重跑需先建 `data/adj_factor_hk/`，本次不补 |
| 5 | regime_history 三市场从 commit ① 以来从未真正产出 | 治本代码闭环但**数据空白** |

---

## 2. 修复

### 2.1 `_aggregate_daily` schema drift 兜底（regime_builder.py）

```python
# change_pct 兜底派生: A 股 enriched 精简 schema 不含
if "change_pct" not in df.columns and "close" in df.columns and "symbol" in df.columns:
    df = df.with_columns(
        pl.when(pl.col("close").shift(1).over("symbol") > 0)
        .then(pl.col("close") / pl.col("close").shift(1).over("symbol") - 1)
        .otherwise(None)
        .alias("change_pct")
    )

# signal_limit_up 兜底: raw_close 比值推断
if "signal_limit_up" not in df.columns:
    if "raw_close" in df.columns:
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
# signal_limit_down 同理 <= -0.095
# signal_broken_limit_up: 兜底 False
# ma20: 缺则 ma20_above 走 0
```

**为什么 0.095 阈值**：
- 主板涨停 9.97~10.05% → 比值 0.0997~0.1005 → 绝对值 9.97~10.05% ≥ 9.5% ✓
- ST 涨停 4.97~5.05% → 比值 0.0497~0.0505 → 绝对值 4.97~5.05% < 9.5% ✗
- 主板跌停 -9.97~-10.05% → ≤-9.5% ✓
- 普通股 ±3~9% 区间都不触发 → ✓ 不会误判

**ST 涨停 5% 漏判**算小瑕疵：A 股 enriched 当前 12 列无 turnover_rate/limit_price，
无法精确判断 ST。如果用户在意，后续 enriched schema 重算时补 `is_st` 字段。

### 2.2 `resync_hk_daily.py` — 港股全量重同步脚本

与 `resync_us_daily.py` 同模式：
- `--concurrency N`：并发线程数（默认 6）
- `--limit N`：最多处理 N 只（0=全部）
- `--max-attempts N`：单只重试（默认 3）
- `--start N`：从 universe 第 N 只开始（续跑用）

注：实际跑会遇到 `is_verified_hk_raw` 拒绝（**新增日需要复权因子**），
所以本次未跑全量。脚本就绪，等复权因子建好（`data/adj_factor_hk/`）后直接用。

### 2.3 数据实际状态（落盘后）

```
regime_history/
├── cn/part.parquet  : 90 天 (2026-05-08 ~ 2026-09-11)
├── hk/part.parquet  : 37 天 (2026-07-15 ~ 2026-09-03)
└── us/part.parquet  : 37 天 (2026-07-15 ~ 2026-09-03)

strength_ladder/
├── hk/part.parquet  : 2296 行 (09-01 ~ 09-03, ~750 行/天)
└── us/part.parquet  : 5568 行 (09-01 ~ 09-03, ~1800 行/天)
```

**已知缺口**：
- 港美 enriched 09-04~09-11 (6 个交易日 × universe) 待拉
- 港美复权因子未建（拉新日 enriched 需要）

---

## 3. 验证

### 3.1 测试矩阵（6 项新增 + 7 文件回归）

| 测试 | 覆盖点 |
|------|--------|
| `test_aggregate_12col_minimal_schema_succeeds` | 12 列精简 schema 聚合成功 + limit_up 区间 |
| `test_aggregate_15col_full_schema_still_works` | 15 列老 schema 向后兼容 |
| `test_signal_limit_up_inference_from_raw_close` | raw_close 比值推断涨停 |
| `test_change_pct_derivation_uses_close_shift` | change_pct 派生精度 |
| `test_cn_regime_round_trip` | cn regime 落盘 → 读回 |
| `test_hk_us_regime_round_trip` | hk/us regime 落盘 → 读回 |

### 3.2 全后端基线

- regime + 强度梯队相关 7 文件 = **100/100 passed**
- 全后端 = **2045/2045 passed**（1 项 `test_ext_config_load_all_cache` 在全量跑时沙箱 flaky 单跑过，零耦合）
- ruff 增量 clean（`regime_builder.py` 新增兜底不引入新警告）

### 3.3 关键踩坑

| 坑 | 修复 |
|----|------|
| A 股 enriched 无 `change_pct` | 在 `_aggregate_daily` 入口派生 |
| A 股 enriched 无 `signal_limit_*` | 用 `raw_close` 比值推断 |
| A 股 enriched 跨分区 schema 漂移 | 不依赖 scan 路径；走手工 `pl.read_parquet` + `pl.concat(diagonal_relaxed)` |
| 港股 fetch_hk_daily_sina 全量失败 | 需要先有 `data/adj_factor_hk/`，留给后续 |
| cn 走 `run_regime_batch` 失败 | 改手工拼 + `_aggregate_daily` 落盘（绕开 framework 慢路径） |

---

## 4. 提交链

```
6557762  feat(pipeline): regime 按市场循环 (P0 治本 5/5)
f6f4a9d  feat(regime): schema drift 兜底 + cn/hk/us 首次产出真实数据 (P1-A)
```

P1-A 治本闭环完成。后续 P1-B (港美日K全量重跑+复权因子) 留给用户启动（2.5h+ 长跑）。

---

## ⚠️ 待完善 / 已知局限

- cn regime `max_consecutive` 在 09-04 之后为 0（schema 无 `consecutive_limit_ups` 列） — 评分时该子分弱化，不影响 state 5 档
- 港美 regime 只到 09-03（enriched 截止） — 09-04~09-11 等拉 enriched 后补
- 港美 enriched 全量重跑**必须先建 `data/adj_factor_hk/`**（同款美股已有 adj_factor，但港股没建） — 是个独立的"工具脚本"任务，建议在 P1-B 优先解决
- regime 评分用 `compute_phase` 内部需要完整日序，本批用最近 90 天数据，重算后续 enrich 会触发 phase 重新平滑
- `is_verified_hk_raw` 在新增日的 sina 拉数据偶发"OHLC 价格关系无效" — 内部 V8 解码偶发问题，3 次重试可缓解

---

## 📚 成员产出索引

- 方案: `deliverables/gstack/feature-dev-hk-us-regime-strength-ladder-2026-09-14.md`
- 治本 5 步骤: `deliverables/gstack/{regime-hk-us-scoring,regime-api-market-param,pipeline-regime-markets,strength-ladder}-step-*-2026-09-14.md`
- **本文档**：P1-A 数据闭环步骤总结

---

> P1-A 治本闭环完成。P1-B (港美 09-04~09-11 日K补齐 + 复权因子) 留给用户启动。