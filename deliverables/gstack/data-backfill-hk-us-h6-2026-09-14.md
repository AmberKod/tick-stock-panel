# 港美 H6 数据补数：根因诊断与执行方案

**日期**：2026-09-14
**场景**：数据闭环 / 补数方案
**参与成员**：主理人（数据排查 + 方案收口）

---

## 📌 TL;DR

- H6（港美日K）停在 09-03 的根因**不是数据源不可用**，而是**调度从未触发**：`market_daily_hk` 每天 18:00 / `market_daily_us` 每天 08:00，用户服务不常在线，misfire 2h 错过即跳过且不补跑。
- 今 21:22 实测：**港股新浪源 8/10 一次成功（数据到今天 09-14），美股 3/3 一次成功（到 09-11 上一交易日）**——源当前健康，可立即补数。
- 补数四步（用户侧挂机约 1~1.5 小时）：港股 H6 重拉 → 美股 H6 重拉 → enriched 重算 → 起服务跑一次 pipeline 追平 regime+梯队。
- 治本项（下批代码）：调度 catch-up / 腾讯日历 501 依赖修复，见第 5 节。

---

## 1. 根因诊断（三层叠加）

### 层 1：调度窗口与用户在线时段错配（主因）
- `daily_pipeline.start_scheduler` 注册：HK 每天 18:00、US 每天 08:00（北京时间），`misfire_grace_time=7200`。
- 启动日志仅 4 份（09-13 16:07 / 16:42 / 22:42 / 23:09），服务运行时段不覆盖 18:00 / 08:00 窗口。
- **JobStore 铁证**：09-04 18:05 之后再无任何港美任务记录（09-06 16:51、09-10 22:13、09-13 16:01 的 succeeded 均为 A 股盘后 pipeline）。

### 层 2：09-06 datetime bug 曾污染触发窗口
- JobStore 09-04 00:32、09-06 15:22~16:37 共 6 次 `can't compare datetime.datetime to datetime.date`（resolve_universe 阶段）——即当天修复的 `latest_daily_date` bug（commit `d9bca87`）。
- 期间即使调度触发也会失败。

### 层 3：框架依赖的腾讯日历 09-14 起 501
- `run_market_daily_sync` 框架依赖腾讯日历接口，**2026-09-14 起 501 不可用**（`resync_hk_daily.py` 注释记录）。
- 即使调度触发，框架路径当前也会失败——补数必须走 `resync_*.py` 绕开脚本。

### 修正一个历史误读
- 09-04 18:05 的 HK 任务实际是 **2798 全量、2703 成功 / 95 失败（missing_from_result，多为僵尸/停牌标的）**；此前读到的 "56/56" 只是最后一个 chunk 的进度日志。
- 09-05 起任务根本没跑，H6 自然停更。

---

## 2. 当前数据现状（实测）

| 维度 | 现状 |
|------|------|
| HK H6 分区 | 2795 个：2200 停 09-03（老 schema：`datetime[μs]`、无身份列）；仅 3 只到 09-11（新 schema：`Date`、`raw_price_verified=true`，手动重拉样本） |
| US H6 分区 | 8806 个：5912 停 09-03（含约 2700 个清洗前粉单残留分区，不阻塞，属后续清理） |
| 港股源实测 | **8/10 一次成功、数据到 09-14**；01810/01299 偶发 0 行（重试可救）；之前判「结构性失败」的 00388 一次通过 → **~30% 成功率是并发+风控偶发，非结构性** |
| 美股源实测 | 3/3 一次成功（AAPL/MSFT/NVDA），数据到 09-11（上一交易日，正确） |
| 港美 enriched | 停 09-03；港股重算受双重门槛（`adj_factor_hk/` 空 + `is_verified_hk_raw` 拒老 H6）——重拉带身份列的 H6 后即可过 |

---

## 3. 补数执行方案（四步，用户侧执行）

> 长任务遵循「用户自己启动」惯例；每步完成后下一步。全部命令在仓库根目录 `tick-stock-panel/` 下执行。
> Python 直连（绕过 uv trampoline 偶发问题）：
> `C:/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12.11-windows-x86_64-none/python.exe`
> （或正常用 `python`，backend venv 可用时皆可）

### 步骤 1：港股 H6 全量重拉（约 25~40 分钟，并发 3 稳妥）
```bash
python backend/scripts/resync_hk_daily.py --concurrency 3 --max-attempts 4
```
- 内部：新浪源全量拉取 → 写 H6（新 schema 带身份列）→ 尝试 enriched（港股会被 factor 门槛跳过，**预期行为**，第 3 步统一算）。
- 并发说明：今实测串行健康（8/10 一次成功）；历史 30% 成功率出现在并发 4+风控时段，故降并发 + 提重试。
- 结束后留意输出的失败清单，若失败 >100 只可再跑一遍（retry 天然幂等，已是最新的会被 merge 跳过）。

### 步骤 2：美股 H6 全量重拉（约 10 分钟）
```bash
python backend/scripts/resync_us_daily.py
```
- 6071 只，上次全量重跑 6071/6071 全成功（10.44 只/秒）。

### 步骤 3：港美 enriched 重算（约 8 分钟）
```bash
python backend/scripts/rebuild_hk_us_enriched.py --target legacy
```
- **必须 `--target legacy`**：存量 enriched 是 64 列 / `Datetime(us)`，新 schema 76 列 / `Date` 混存会炸整目录 scan（P1-B 已踩过并修复过一次）。
- 港股约 2 分钟 + 美股约 6 分钟。

### 步骤 4：起服务追平 regime + 强度梯队（几分钟）
```bash
python dev.py
```
- 服务起来后手动触发一次盘后管道（前端「数据」页或 `POST /api/pipeline/run`）。
- Step 2.7（regime 增量）+ Step 2.8（强度梯队增量，max_backfill_days=30）会自动把 09-04~09-14 的缺口补齐。

### 验收口径
| 项 | 期望 |
|----|------|
| HK H6 | ≥2700 只 max_date ≥ 2026-09-14 |
| US H6 | ≥6000 只 max_date ≥ 2026-09-11 |
| enriched | 港美 max(date) ≥ 09-11（对应各自最新交易日） |
| `/api/hk/overview` `/api/us/overview` | 快照日期 = 各自最新交易日 |
| regime / 强度梯队 | 前端 Regime 页切 hk/us，历史覆盖到最新交易日 |

---

## 4. 已知边界（不阻塞，如实记录）

- 港股约 95 只 `missing_from_result`（停牌/僵尸/退市），重拉仍失败的属 universe 清理范畴（约 5% 停在 2023/2024）。
- 美股 JONE 真实缺失（yfinance `Period 'max' invalid`，已知）。
- 美股 H6 目录 8806 分区 vs universe 6071：约 2700 个清洗前粉单残留分区，不影响 enriched/rebuild（按 instruments 6071 只算），建议后续清盘。

---

## 5. 治本项（下一批代码，待用户拍板优先级）

| # | 事项 | 说明 | 级别 |
|---|------|------|------|
| 1 | **调度 catch-up**：服务启动时检测错过窗口并补跑 market_daily | 根治「用户不在线 = 数据停更」——用户使用模式（白天/深夜起服务）与 18:00/08:00 窗口天然错配 | P0 |
| 2 | 腾讯日历 501 修复 / 替换交易日历源 | `run_market_daily_sync` 框架路径当前不可用；补数靠 resync 脚本绕开 | P0 |
| 3 | H6 老分区 schema 归一（datetime[μs] → Date + 身份列） | 本次重拉会自然覆盖大部分；剩余失败标的可跑一次批量 cast | P1 |
| 4 | 美股粉单残留分区清理 + 港股僵尸标的 universe 治理 | 数据卫生 | P2 |

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 |
|---|------|--------|--------|
| 1 | 按第 3 节四步执行补数 | Amber | P0 |
| 2 | 每步贴回控制台尾部输出，我来核对验收口径 | Amber → Gu | P0 |
| 3 | 治本项 1+2（调度 catch-up + 日历源修复）排进下批 | Gu | P1 |

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
