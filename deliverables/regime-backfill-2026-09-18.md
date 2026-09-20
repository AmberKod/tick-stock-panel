# 港美 regime / 强度梯队停更治理（2026-09-18）

## 现象

服务验收时日 K 数据已到 09-17，但「市场环境（regime）」和「强度梯队」两个卡片
都停在 **09-03**，看起来像数据不更新。

## 根因

不是数据缺失，是**开关 + 两个设计缺陷**：

| 层 | 事实 |
|---|---|
| 开关 | `pipeline_regime_enabled` 默认 `False`，同时门禁 `_compute_regime_step` 与 `_compute_strength_ladder_step`。日志：`compute_regime/mainline skipped: user disabled` |
| 默认关闭的理由 | 港美 enriched 存的是**全历史**（港股最早 1998、美股最早 1972），增量缺口恒为数千天（hk 6960 / us 13757），一次批算上千天会拖垮盘后管道 |
| 真缺陷 1 | `compute_regime_incremental` 调 `run_regime_batch` 时**漏传 `market`**，后者默认 `"cn"` → 港美增量会用 A 股聚合结果算完写进 `regime_history/{hk,us}` |
| 真缺陷 2 | 增量补差**没有回溯窗口**（同类的 `strength_ladder` 有 30 天窗口） |

## 处理

### 1. 补算（已完成，看板已可见，无需重启也生效）

新增 `backend/scripts/backfill_regime_ladder.py`：按「regime 表最新日的次日」起补，
只补近 30 天，不做全量回填。

| 指标 | 补算前 | 补算后 |
|---|---|---|
| regime hk | 37 行 / 09-03 | **47 行 / 09-17** |
| ladder hk | — | **25,218 行 / 09-17** |
| regime us | 37 行 / 09-03 | **42 行 / 09-11** |
| ladder us | — | **68,877 行 / 09-11** |

美股停在 09-11 是因为美股 enriched 本身只到 09-11，不是补算遗漏。

### 2. 代码修复（`90a2271` → develop `3bfa215`）

- `compute_regime_incremental` 补传 `market=m`
- 新增 `max_backfill_days=30`（对齐 `strength_ladder`）：每天盘后新增缺口只有 1 天，
  永远落在窗口内 → **开关可以安全常开**
- 新增 2 个测试用例锁住「窗口裁剪」与「market 透传」

### 3. 顺带清掉 CI lint 欠账

batch-3 收尾只跑了 `ruff check app scripts`，而 **CI 跑的是 `ruff check backend/`
（含 tests/）** → 68 处漏网，本地"清零"是假象。按 CI 口径重新清零：
自动修 56 处 + 人工 12 处（zip strict / 解包拼接 / match 需 raw / `l`→`lst` /
死绑定 / 注入测试宽泛断言 noqa / `tests/**` 豁免 N801——测试桩 `class store`
必须与 `repo.store` 属性同名）。

### 4. 打开开关

`data/user_data/preferences.json` 写入 `"pipeline_regime_enabled": true`。
**顺序不能反**：先合入修复再开开关，否则旧代码的 market 串味 bug 会污染 hk/us 数据。

## 验证

- `ruff check .`（CI 口径，**主仓**）：All checks passed
- 主仓测试：regime + pipeline + launcher **52 passed**
- `preferences.get_pipeline_regime_enabled()` → `True`
- API 冒烟：`/api/regime/latest?market=hk` → 09-17；`/api/strength_ladder?market=hk` → 09-17

## 待办

- 重启服务（`python dev.py`）加载合入后的代码
- 美股 enriched 补到最新（当前只到 09-11，属既有数据缺口，与本轮无关）
