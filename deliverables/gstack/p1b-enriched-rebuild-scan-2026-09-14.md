# 港美补数链路排查 + enriched 重算脚本 (P1-B 第 1 段)

**日期**：2026-09-14
**场景**：调试复盘 / 数据闭环
**参与**：直接执行（多轮探索 + 实测验证）
**Commit**：`787fe78`

---

## 📌 TL;DR

- 整体结论：🟡 **有条件通过** —— 工具就绪，但补数前置依赖未满足，需用户决策
- 上一轮「港美补数卡在 adj_factor_hk」的判断**已推翻**，真根因是 H6 层没拉新数据
- 中途踩到 `scan_parquet` schema 冲突并成功止损
- 阻塞项：2 个（港股复权因子 / H6 拉取成功率）

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 条件 Go（工具已就绪，全量补数需用户决断时机） |
| 严重度分布 | 🟠 1（schema 冲突，已修） / 🟡 2（港股复权因子 / 拉取成功率） / 🟢 0 |
| 关键行动项 | 3 条 |
| 建议负责人 | Amber（长跑任务本机启动） |

---

## 1. 排查结论（推翻上一轮判断）

### 🧊 上一轮错误判断 vs 实测

上一轮 memory 写的是：

> 港美 enriched 停在 09-03，09-04~09-11 需先建 adj_factor_hk 才能拉（结构阻塞）

实测复核后**结论不成立**：

| 层 | 上一轮认为 | 实测 |
|---|---|---|
| 新浪源 `fetch_hk_daily_sina('00700')` | 不可用 | ✅ 5485 行 / 最新 09-14 / `is_verified_hk_raw=True` |
| `data/adj_factor` | 港美补数前置 | 空目录 0 parquet；A 股也没用它照样算到 09-11 |
| H6 `kline_daily/00700.HK` | 缺 09-04~09-11 | ✅ 已有 5468 行 / 最新 09-11 |

上一轮 fetch 返回 0 行是**偶发风控**，不是结构性阻塞。

### 🔥 真根因：H6 层才停在 09-03

抽样统计（每 ~150 只取 1，共 60 只）：

```
.US H6 抽样 60 只 → 09-03: 41 只, 09-02: 2 只, 2023/2024 僵尸: 3 只
.HK H6 抽样 60 只 → 09-03: 45 只, 09-11: 1 只, 僵尸: 2 只
```

`AAPL.US` / `00700.HK` 到 09-11 只是**少数手动重拉过的样本**，不具代表性。

**结论**：单补 enriched 无用，必须先补 H6。完整链路是两步：

```
① 拉 H6 (网络)  →  resync_hk_daily.py
② 算 enriched (本地)  →  rebuild_hk_us_enriched.py  ← 本批次产物
```

---

## 2. 综合发现

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 处置 |
|---|--------|------|------|---------|------|------|
| 1 | 🟠 | 数据 | `kline_hk_us_enriched/` | 新 76 列 vs 存量 64 列混存 → `scan_parquet` SchemaError，连带 regime / strength_ladder / overview 全线崩 | 加 `--target legacy` 降级 | ✅ 已修 + 止损 |
| 2 | 🟡 | 数据 | 港股 2795 只 | `_hk_factors_for_window` 要 `adj_factor_hk/`，目录空且 akshare 无港股复权因子接口 | 需重拉带身份列全量 H6 | ⏸ 待决策 |
| 3 | 🟡 | 数据 | H6 拉取 | 并发 4 成功率仅 30%（10 只成 3 只）；串行重试能救回部分（00939 第 3 次成功），但 00388/00001 重试 3 次仍 0 行 | 降并发 + 加大重试 + 接受缺口 | ⏸ 待决策 |

### 发现 1 详情（本批次唯一引入的回归，已修）

新 `compute_enriched` 输出 **76 列 / `date=Date`**，存量 **64 列 / `date=Datetime('us')`**。
二者混存会让整个 `kline_hk_us_enriched` 目录 `scan_parquet` 直接失败：

```
SchemaError: data type mismatch for column date: incoming: Date != target: Datetime('μs')
```

好消息：新旧是**超集关系**（新 76 ⊃ 旧 64，缺失列为空），且 `momentum_*` 新旧都有，
所以降级无损。多出的 12 列全是价格身份元数据（`adjustment_*` / `price_*` / `source` / `observed_at`）。

脚本加 `--target legacy`（默认）：cast `date→Datetime('us')` + `select` 旧 64 列。
已手工把误写入的 50 只降级回去，`scan_parquet` 恢复正常 ✅

---

## 3. 本批次产物

### `backend/scripts/rebuild_hk_us_enriched.py`（238 行）

两步链路的**第 2 步**：

- 输入 `data/kline_daily/symbol=*.{US,HK}/part.parquet`（H6）
- 输出 `data/kline_hk_us_enriched/symbol=XXX/part.parquet`
- **速率**：并发 6 实测 **21-23 只/秒**（纯本地 polars，不打网络）
  - 美股 8806 只 ≈ **6 分钟**
  - 港股 2795 只 ≈ **2 分钟**
- 幂等：单只独立写盘，中断重跑不丢已完成部分
- `--dry-run` 看规模 / `--limit` / `--start` 断点续跑 / `--target legacy|new`

命令示例：

```bash
python backend/scripts/rebuild_hk_us_enriched.py --dry-run --market all
python backend/scripts/rebuild_hk_us_enriched.py --market us --limit 50
python backend/scripts/rebuild_hk_us_enriched.py --market us --concurrency 8
```

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 决策：是否现在投入 1-3 小时做港美 H6 全量拉取（含约 30% 失败率） | Amber | P0 | 本轮 |
| 2 | 决策：港股「部分标的（00388/00001 类）拿不到」能否接受数据缺口 | Amber | P0 | 本轮 |
| 3 | 若不补数，转前端全局 market context（不依赖新数据，后端 API 已就绪） | Gu | P1 | 下一批 |

---

## ⚠️ 待完善 / 已知局限

- **僵尸标的**：抽样里发现停在 2023/2024 的代码（约 5%），应是退市/停牌，需要 universe 侧清理而非补数解决
- **港股复权**：即便 `_hk_factors_for_window` 用恒等因子（ex_factor=1.0）绕过，`is_verified_hk_raw` 仍会拒绝 H6 老数据——这是双重门槛，港股必须重拉带身份列的全量 H6
- **本次未实测** `--target new` 全量路径（需港美同时重算，风险高，留给明确决策后执行）

---

## 📚 附：关键命令记录

```bash
# 速率实测（港股拉取）
# 并发 4: 10 只 6.4s → 1.56 只/秒，成功 3/10
# 串行重试: 00939 第 3 次成功 / 01211 第 1 次成功 / 00388、00001 三次均 0 行

# scan 健康检查
python -c "import polars as pl; lf=pl.scan_parquet('data/kline_hk_us_enriched/**/*.parquet'); \
  print(lf.select(['symbol','date','close']).collect().height)"
```

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
