# P0 · 港美股日线补跑「判据失效」双层缺陷

> 2026-09-24 · 主仓 `tsp-fresh` · develop `b7ba400` · 修复 commits `51ebc56`（第一层）+ 第二轮进行中
> 关键词：fail-open、判据与存储错配、fail-closed、变异验证

---

## 1. 起因：一句看起来很健康的日志

服务启动后打印：

```
market_daily catchup: 港美 H6 均为最新, 无需补跑
```

真相是**完全没数据**，而且 cron 永远不会去补。

---

## 2. 第一层：`latest is None` 被当成「数据最新」（已修，`51ebc56`）

`backend/app/jobs/daily_pipeline.py`

```python
# _market_daily_catchup_needed，原 :1380
latest = _h6_latest_by_sampling(data_dir, market)
if latest is None:
    return False          # ← 无数据 == 无需补跑

# 调用点 run_market_daily_catchup，原 :1420-1437
for market in ("HK", "US"):
    if not _market_daily_catchup_needed(...):
        continue
if not results:
    logger.info("港美 H6 均为最新, 无需补跑")   # ← 两种情况打同一句
```

**「目录下无数据」与「数据真最新」走同一路径、打印相同日志** —— 违反项目「不可用不伪装 / fail-closed」纪律。

### 修复要点

| 位置 | 改动 |
|---|---|
| `:1377-1381` | 新增 `_CATCHUP_REASON_{BEFORE_WINDOW,NO_DATA,FRESH,STALE,PARTIAL_SYNC}` |
| `:1385-1395` | 新增 `MarketCatchupDecision(needed, reason, latest)` frozen dataclass，替代裸 bool |
| `:1398-1432` | 新增 `_market_daily_catchup_decision()` 三态判定 |
| `:1435-1440` | `_market_daily_catchup_needed()` 保留裸 bool 契约，内部委托 decision |
| `:1477,1480-1497` | no_data 独立分支：WARNING + `{"status":"skipped","reason":"no_data"}` |
| `:1515-1527` | 收尾日志：仅当所有市场 reason 均为 `fresh` 才允许打印「均为最新」；否则如实打印 `无需补跑 (HK=no_data, ...)` |
| `:1584-1593` | A 股同类问题一并修（周末/窗口未过时把「无数据」藏起来） |

### 为什么不直接对 no_data 触发补跑（实测依据，非猜测）

1. **判据永远不会转绿** → 会退化成「每次启动全量重刷」。港美日 K 实际落盘在 `kline_hk_us_enriched`，而 H6 判据读 `kline_daily`，本部署后者 HK/US 实测 0 分区、前者 8869 分区。
2. **universe 缺失时补跑是报错不是空转**：`load_market_universe` 抛 `UniverseUnavailableError`；`sync_*_instruments(allow_demo=False)` 抛 `RuntimeError("拒绝写入 demo 快照")`。

⇒ 按「不可用显式声明并剔除出分母」处理：WARNING 声明 + `skipped/no_data` 记录 + 绝不写「均为最新」。

### A 股分支核查结论

`daily_pipeline.py:1561-1563` A 股对无数据是 **`return True`（fail-closed）**，与港美 `return False` 相反 —— **不存在同类伪装**。既有测试 `tests/test_daily_pipeline_cn_catchup.py:85` 已锁定该行为。

### 变异验证（改回 `return False` 必须转红）

Mutation-1 → `3 failed, 3 passed`，红态中复现缺陷本体：

```
INFO app.jobs.daily_pipeline:daily_pipeline.py:1521 market_daily catchup: 港美 H6 均为最新, 无需补跑
```

Mutation-2（保留 decision，改回裸 `continue`）→ 2 个用例转红，证明 `results` 记录那一半同样被覆盖。

---

## 3. 第二层：新鲜度判据只看单侧（进行中）

**这个更致命 —— 修完第一层，补跑依然永不触发。**

```
_h6_latest_by_sampling  → 读 kline_daily/symbol=*.{HK,US}
新仓 kline_daily HK/US 分区数 = 0        ← 恒空
新仓 kline_hk_us_enriched 分区数 = 8869   ← 数据在 enriched
```

而 `_market_partial_sync_pending` 的 docstring 原文：

> 判据: H6 与 enriched **两侧任一**满足…**两侧都看**是因为它们的覆盖会不一致, 只看一侧会漏判

**设计意图是双路，实现只落地了单路。**

### 修法（第二轮任务）

复用 `backend/scripts/repair_hk_stale.py::scan_latest_dates()`（已生产验证：处理跨分区 schema 不一、遵循「不可用不计入分母」），把 `latest` 取值扩为 **H6 ∪ enriched 双路取新**。

### 预期收益

改完后 HK 众数 latest = `2026-09-18`（enriched 侧实测众数；30 个抽样里仅 8 只到 09-24），staleness = 6 天 > HK 容差 4 ⇒ 判定 stale/partial_sync ⇒ **真实触发 `_run_market_daily_scheduled`**。

即：昨天标注的 **189 只残差，有可能由 cron 自动补掉**，不必再手动跑 `repair_hk_stale.py`。

---

## 4. 附带：数据目录错配（已处理）

服务 `data_dir = tsp-fresh/data`，但港股数据原先只在旧仓 `tick-stock-panel/data`（5.9GB / 8869 分区）——**分析对象与服务所见不是同一份**。

迁移（robocopy）已完成：

| 文件 | 状态 |
|---|---|
| `kline_hk_us_enriched` | ✅ 已迁 |
| `instruments/hk_instruments.parquet` | ✅ 已迁 |
| `instruments/us_instruments.parquet` | ⚠️ 第二轮命令漏拷，补拷中 |

---

## 5. 教训

1. **同一句日志承载两种语义 = 伪装**。凡是"跳过"路径，必须区分「没有」和「有但最新」。
2. **改 Tokens前先看设计注释是否与实现一致** —— 本项目里 docstring 写「两侧都看」而实现只读一侧，注释说得对、代码没跟上。这类错配比纯 Bug 更隐蔽。
3. **`latest is None` 的处置必须看「补跑能否让条件转绿」**。判据在结构上转不了绿时硬跑，只会变成每次启动的全量重刷。
