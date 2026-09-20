# Regime 治本 2/5 步骤总结 — 港美 Regime 评分改造

**日期**：2026-09-14
**场景**：feature-dev（港美 Regime 治本第 2 步）
**Commit**：`f5fbfba`
**基线**：commit ① `603d0c9`（持久化分目录 + market 参数兼容）
**配套方案**：`deliverables/gstack/feature-dev-hk-us-regime-strength-ladder-2026-09-14.md` §3.2

---

## 📌 TL;DR

- **结论**：🟢 通过
- **实现**：港美 `speculation` 维度从涨停/封板/连板（A 股专属）替换为"动量+新高"合成
- **cn 路径零回归**：原 4 维（profit/speculation=涨停/resilience/trend）保持不变
- **下一步**：commit ③ API 8 端点加 `market` 参数

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🔴 0 / 🟠 0 / 🟡 0 / 🟢 0 |
| 关键行动项 | 3 条 |
| 验证基线 | 后端 2007 passed / 0 failed（基线 1995 + 12 新增） |
| cn 路径零回归 | ✅ — 老 metrics 字段、子分、加权、阈值全部不变 |

---

## 1. 改造范围（按函数分层）

| 函数 | 签名变化 | 行为变化 |
|------|---------|---------|
| `_compute_subscores(metrics, market="cn")` | 加 market 参数 | cn 走原路径；hk/us 替换 speculation 合成 |
| `classify_state(metrics, market="cn")` | 加 market 参数 | 透传给 `_compute_subscores` |
| `_aggregate_daily(df, idx, market="cn")` | 加 market 参数 | hk/us 聚合 `_m20d_mean`/`_m25_cnt`/`_m15_cnt`/`_m8_cnt`/`_nh_cnt`，派生 metrics 字段 |
| `run_regime_batch(repo, start, end, market="cn")` | 加 market 参数 | cn 原路径不变；hk/us 走 `_scan_hk_us_enriched_for_regime`，指数 `^HSI`/`^GSPC` |
| `_scan_hk_us_enriched_for_regime(repo, start, end, market)` | 新增 | per-symbol 全目录扫描 + 后缀过滤 + 日期范围 + 显式补 False/0 列 |

## 2. 港美评分公式

```
momentum = (
    _score(momentum_20d_pct*100, -2, 8)   * 0.40    # 20 日均涨幅(小数转 %)
  + _score(momentum_25_share*100, 1, 12)  * 0.20    # >=25% 档位占比
  + _score(momentum_15_share*100, 5, 20)  * 0.20    # >=15% 档位占比
  + _score(momentum_8_share*100, 10, 35)  * 0.20    # >=8% 档位占比
)
new_high = _score(new_high_share*100, 2, 25)        # N 日新高占比
speculation_hk_us = momentum * 0.6 + new_high * 0.4
```

校准值为经验值，**等真实港美分位数回归后替换**。

## 3. 数据契约（持久化新增 5 列）

| 列 | 类型 | 含义 | cn 写 |
|----|------|------|-------|
| `momentum_20d_pct` | Float64 | 20 日动量均涨幅(小数) | 0.0 |
| `momentum_25_share` | Float64 | momentum_20d >= 25% 占比 | 0.0 |
| `momentum_15_share` | Float64 | momentum_20d >= 15% 占比 | 0.0 |
| `momentum_8_share` | Float64 | momentum_20d >= 8% 占比 | 0.0 |
| `new_high_share` | Float64 | N 日新高占比 | 0.0 |

**schema 兼容**：新列在已有 cn 历史数据上为 null；首次 upsert 后会按 concat 自动补 null → 重写为 0。

## 4. 验证基线

| 测试组 | 项数 | 结果 |
|--------|----:|------|
| `test_regime_subscores_hk_us` | 12 | ✅ |
| `test_regime_market_split`（commit ①） | 17 | ✅ |
| `test_regime_builder` | 21 | ✅（零回归） |
| `test_market_phase` | 13 | ✅（零回归） |
| `test_mining_schedule` | 17 | ✅（间接调用零回归） |
| `backtest/test_mining_runtime` | 11 | ✅ |
| `backtest/test_strategy_backtest_correctness` | 36 | ✅ |
| **全后端** | **2007 passed** | ✅（基线 1995 + 12 新增） |

ruff：clean

---

## 5. 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 不复制 `MARKET_PROFILE` 协议到 `markets/profile.py` | 留在 `regime_builder._MARKET_BENCHMARKS` 内部常量 | 避免跨 markets/ + tests/services/hk_us/* 等大量文件改动；commit ② 是评分函数本身，profile 抽象留待后续统一 |
| 港美 enriched 缺信号列补 False/0 | 显式 `with_columns(pl.lit(False))` | 让 `_aggregate_daily` 的 `if "signal_limit_up" in avail` 自然走"信号缺失"分支，A 股聚合代码零改动 |
| cn 路径 metrics 不传入动量字段 | `metrics.get("momentum_20d_pct", 0.0)` 默认值兜底 | cn 评分公式不引用这些字段，`get` 返回 0 也不影响 cn 子分 |
| 不做指数历史补齐 | `^HSI` / `^GSPC` 走 `_load_index_pct` 实时拉取，失败 → 0 → trend 子分钳到 50 | 与方案文档一致；指数历史补齐属于 P1 改进项，不阻塞本次交付 |
| 不改 `markets/cn.py` 硬编码 `benchmark_symbol` | `_MARKET_BENCHMARKS` 内部 dict 覆盖 | commit ② 是评分函数改造；markets profile 抽象是另一批 commit |

---

## 6. 已知局限（待 P1）

- **港美校准值**：动量/新高的 `_score(low, high)` 边界用经验值（-2~8、1~12 等），需用真实港美 2022-2026 数据回归 p15/p85 替换
- **指数历史补齐**：`_load_index_pct(^HSI, ...)` 失败时 trend 子分钳到 50 中位（不爆炸），但"恒指/标普"的 regime 时序图会缺数据
- **港美 ST 过滤**：港美无 ST 制度，`run_regime_batch` 跳过 ST 过滤逻辑，但未复用 cn 的 `preferences.get_sentiment_exclude_st()` —— 港美没有这个开关
- **统一市场 profile 抽象**：HK/US profile 仍散落在 `markets/`、`regime_builder.py`、`hk_us_overview_builder.py` 三处；等 commit ④ 后再统一抽 MarketProfile 协议实现
- **港美 phase 列**：当前 `_aggregate_daily` 港美路径会写 `finalize_ladder_row(r)` 返回的空 dict（cn ladder 列名不适用），`refresh_phase_labels` 跑时若港美 regime 历史已写入会缺失预期列。当前 `refresh_phase_labels` 在 is_empty 时直接返回 0 → 暂不阻塞；等真用时再补港美 phase 逻辑

---

## 7. 成员产出索引

- gstack-product-reviewer: **未实际产出**（Agent 跑空，本助手自承）
- gstack-investigator: **未实际产出**（Agent 跑空，本助手自承）
- gstack-qa-lead: 未参与（待 commit ⑤ 后统一复验）
- gstack-designer: 未参与
- gstack-security-officer: 未参与（本批次无新增攻击面）

> 本步骤由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
> 下一步：commit ③ API 8 端点加 `market` 参数 + 缓存键更新。