# P1-B ② 强度梯队接入 daily_pipeline 增量调度

**日期**：2026-09-14
**场景**：功能开发（补齐港美 Regime 批次唯一的功能缺口）
**提交**：`8fd1489`
**参与成员**：主理人（编排 + 实现调度）／排障手视角（根因定位与回归锁定）

---

## 📌 TL;DR

- 强度梯队此前是**手动触发**的唯一遗留项，现已与 regime 同开关、同管道自动补算。
- 顺带修掉一个真实踩到的日期类型 bug（`datetime` 是 `date` 子类导致 `isinstance` 漏判）。
- 真实数据端到端验证通过：港股 ladder 2296 → 4904 行，3 个交易日 32.9s。
- 相关测试 145 passed（12 新增 + 133 存量），ruff 新代码零告警。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 功能缺口 | ✅ 已补齐（regime 自动 / 梯队手动 → 两者都自动） |
| 严重度分布 | 🔴 0 / 🟠 1（日期类型 bug，已修）/ 🟡 0 / 🟢 0 |
| 新增代码 | 3 个文件修改 + 1 个测试文件（+457 行） |
| 新增测试 | 12 项 |
| 真实数据验证 | ✅ 港股 2296 → 4904 行，覆盖 08-27~09-03 |
| 性能 | ≈11s/交易日（2795 只标的），单次最多补 30 天 |

---

## 1. 问题背景

港美 Regime + 强度梯队治本批次（5 个 commit）与 P1-A 数据闭环跑完后，功能上留了一个不一致：

| 能力 | 计算 | 调度 |
|------|------|------|
| Regime（港美） | 后端服务 | ✅ `daily_pipeline` 自动，按市场循环 |
| 强度梯队（港美） | 后端服务 | ❌ 只能手动调 `compute_strength_ladder_for_day` |

结果就是：regime 每天自动出数，梯队要靠人想起来才跑，前端 `StrengthLadderPanel` 拿到的是过期快照。这一批要做的就是把缺口补平。

---

## 2. 实现方案

### 2.1 调度层 — `app/jobs/daily_pipeline.py`

新增 `_compute_strength_ladder_step()`，紧随 `_compute_mainline_step()` 之后：

- **市场启用判定复用 regime 规则**：检查 `instruments/{hk,us}_instruments.parquet` 是否存在。这样 regime 和梯队的市场生命周期天然一致，不会出现"regime 算了 hk 但梯队没算"的裂缝。
- **按市场循环 + 软失败隔离**：单市场抛异常只写 `stage_errors` + `skipped`，不影响另一个市场，也不阻断主管道。
- **cn 永不进入**：A 股走的是连板梯队（market_phase / monitor / depth_service 协同），动量档位是港美专属替代方案。
- 在 `run_now` 插入 **Step 2.8**（mainline 之后、refresh_views 之前），受 `prefs.pipeline_regime_enabled` 同一开关控制。
- 结果字典新增 `strength_ladder_rows`，与 `regime_days` / `mainline_rows` 并列，前端管道日志可直接读。

### 2.2 服务层 — `app/services/strength_ladder.py`

新增 `compute_strength_ladder_incremental(repo, data_dir, *, today, market, max_backfill_days=30)`：

```
缺口 = enriched 已有交易日  −  ladder 已落盘日期
```

逐日 `compute_strength_ladder_for_day` + `upsert_strength_ladder`。

**为什么必须有 `max_backfill_days`**：

regime 是向量化聚合，一次批算多天很便宜；梯队是**逐日扫全市场 symbol 文件**，单日成本 O(标的数)。首次跑如果不设限，几百天 × 数千只标的会直接拖死日管道。设 30 天上限，超限只补最近 30 天并在日志里写明跳过了多少天历史——想补全量可以显式传 `max_backfill_days=0`（不限，慎用）。

单日的 `compute` 和 `upsert` 各自独立 try，坏一天不影响其它天。

### 2.3 Bug 修复 — `app/services/regime_builder.py`

真实数据验证时炸了：

```
TypeError: can't compare datetime.datetime to datetime.date
```

**根因**：`datetime` 是 `date` 的**子类**，所以 `isinstance(dt, date)` **恒为 True**。原写法

```python
d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
```

会把 `Datetime` 类型的值原样放行（以为是 date 了），后续 `d <= date.today()` 就类型不匹配。港股 enriched 的老分区 `date` 列正是 `Datetime('us')`，真实数据一跑就踩。

**修法**：新增 `_as_date()`，**先判 `datetime` 再判 `date`**，并让 `enriched_date_set` 改用它。因为 regime 和 strength_ladder 共用这个日期扫描，一处修好两条链路；strength_ladder 侧再加一层二次归一化做防御。

---

## 3. 测试覆盖（12 项，`tests/test_strength_ladder_pipeline_step.py`）

| 分组 | 用例 |
|------|------|
| 日期归一化 | `test_as_date_normalizes_datetime`、`test_incremental_survives_datetime_dates` |
| 增量逻辑 | `test_incremental_rejects_cn`、`test_incremental_no_gap_returns_zero`、`test_incremental_fills_gap`、`test_incremental_respects_backfill_limit`、`test_incremental_soft_fails_single_day` |
| 调度 step | `test_step_skips_when_no_universe`、`test_step_only_enabled_markets`、`test_step_both_markets`、`test_step_soft_failure_isolated`、`test_step_never_enables_cn` |

其中 `test_incremental_survives_datetime_dates` 和 `test_as_date_normalizes_datetime` 是**回归锁**，专门防止上面的 `isinstance` 坑再回来。

---

## 4. 验证结果

| 项 | 结果 |
|------|------|
| 真实数据（港股） | ladder 2296 → **4904 行**，覆盖扩到 08-27~09-03 |
| 耗时 | 3 个交易日 32.9s（≈11s/天 × 2795 只） |
| 专项测试 | 12/12 passed |
| 相关测试（strength_ladder / regime / pipeline） | **145 passed** |
| ruff（新代码） | 零告警 |

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 |
|---|------|--------|--------|
| 1 | 本机跑 `pnpm tsc -b` + `vite build`，起服务切三市场看效果（① 用户侧验收） | Amber | P0 |
| 2 | 决定港美数据补数时机（现停 09-03，H6 拉取成功率 ~30% + 港股复权因子缺失） | Amber | P1 |
| 3 | 补数后跑一次完整 `daily_pipeline`，确认 Step 2.8 在真实调度下出数 | 待定 | P1 |

---

## ⚠️ 已知局限

- **首次运行不会补全量历史**：只补最近 30 个交易日。更早的历史需要显式调用并放宽 `max_backfill_days`。
- **梯队性能与标的数线性相关**：美股 6071 只时单日约 25s，日管道 Step 2.8 会多花 ~1 分钟。若日后觉得慢，可考虑改成向量化批算（与 regime 同构）。
- **港股 enriched 仍停在 09-03**：09-04 之后缺 `adj_factor_hk/`，本调度只能在 enriched 已有的日期范围内补算，不会凭空造数。

---

> 关键决策请由工程负责人复核。
