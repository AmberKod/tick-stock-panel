# A 股 enriched 静默退化 12 列 — 根因分析与修复

日期: 2026-09-18
现象: `/api/regime/mainline/recompute` 返回 500，`ColumnNotFoundError: consecutive_limit_ups`
影响面: 主线、热点、连板梯队、选股(换手率/连板因子) 全部受影响

---

## 一、现象

A 股 `data/kline_daily_enriched/` 共 267 个日期分区:

| 列数 | 分区数 | 日期范围 |
|---|---|---|
| 15 列 (正常) | 258 | 2025-08-28 ~ 2026-09-04 |
| 12 列 (退化) | 9 | 2026-09-07 ~ 2026-09-17 |

缺失的三列: `turnover_rate`、`consecutive_limit_ups`、`consecutive_limit_downs`。

---

## 二、根因链（已逐环验证）

```
① pipeline.py 用 glob 一次性扫三份维表
   inst_glob = data/instruments/**/*.parquet
        ↓
② 三份维表 schema 不一致 → polars 跨文件扫描失败
   A股 instruments.parquet   : region=String   (14 列)
   港股 hk_instruments.parquet: region=Null    (33 列)
   美股 us_instruments.parquet: 无 region 列    (16 列)
        ↓
③ except 只 warning → instruments = 空 DataFrame
        ↓
④ compute_all 的守卫为假 → 跳过 compute_limit_signals
   if instruments is not None and not instruments.is_empty():
        ↓
⑤ 不产出 turnover_rate / consecutive_limit_ups / consecutive_limit_downs
        ↓
⑥ _select_storage_cols 用 `if c in df.columns` 静默裁剪 → 15 列写成了 12 列
        ↓
⑦ market_mainline.py:167 select consecutive_limit_ups → ColumnNotFoundError → 500
```

### 日志实证（与分区 mtime 精确对应）

| 分区 mtime | 日志 |
|---|---|
| 2026-09-15 17:38:15 | `17:38:14 instruments 读取失败: data type mismatch for column region: incoming: String != target: Null` |
| 2026-09-17 22:23:07 | `22:23:07 instruments 读取失败: data type mismatch for column region: incoming: String != target: Null` |

**结论：不是历史遗留，是当前代码每次写入都在产出 12 列。** 不修代码只重算，明天照样退化。

### 触发时间点

港美股维表落盘到 `data/instruments/`（与 A 股维表同目录）之后，A 股管道才开始失败。
A 股 09-04 分区（09-06 落盘）仍是 15 列，09-07 起（09-10 首次批量落盘）全部退化。

---

## 三、修复

### 1. 治本 — 维表按市场精确读取

`app/indicators/pipeline.py`

- 新增 `_load_instruments(data_dir)`：固定读 `instruments/instruments.parquet`（A 股）；
  仅在它缺失时回退逐文件扫描（`diagonal_relaxed` 合并 + 按 `market` 过滤掉港美）。
- `run_pipeline` 改用该函数，删掉全局 `inst_glob`。
- 维表不可用/为空时 `logger.error`（原来只有 warning，容易被淹没）。

### 2. 防复发 — 存储列裁剪 fail-loud

`_select_storage_cols` 原来是 `[c for c in ENRICHED_STORAGE_COLS if c in df.columns]`，
缺列静默裁剪。改为：非空 DataFrame 缺任意存储列 → `raise ValueError`。

这是真正让 bug 藏了 11 天的原因：**数据坏在写入时，暴露在查询时。**
现在坏数据根本写不进去，且报错信息直接指向维表加载失败。

### 3. 回归测试

`backend/tests/test_enriched_storage_cols.py`（6 例）

- 目录下同时存在港/美维表时仍能拿到完整 A 股维表
- **记录旧实现为何失效**：直接 glob 扫两份维表必然抛 schema 冲突
- A 股维表缺失时回退扫描不因单文件损坏全盘失败
- 存储列裁剪：15 列保序通过 / 缺列 raise / 空分区不阻断

顺带修补 `tests/test_data_integrity.py::test_pipeline_self_heals_snapshot_day` 的 fixture
（原来没有维表，靠"静默裁剪"才通过 —— 补最小 A 股维表）。

---

## 四、验证结果

修复后在真实数据上复算 2026-09-17：

```
instruments: 5567 rows, 14 cols   ← 维表恢复（原来 0 行）
storage cols: 15 ['symbol','date','open','high','low','close','volume','amount',
                 'raw_close','raw_high','raw_low','turnover_rate',
                 'consecutive_limit_ups','consecutive_limit_downs','quote_ts']
MISSING: none
turnover_rate non-null: 11106 / 11110
consecutive_limit_ups > 0: 100 只
```

---

## 五、待执行：重算 9 个退化分区

代码修好后，已落盘的 9 个 12 列分区仍需重算（需要停服重启后执行）：

1. 隔离（不直接删，便于回滚）
   `data/kline_daily_enriched/date=2026-09-07 … 09-17` → `data/_quarantine/enriched-12col-20260918/`
2. 触发 `POST /api/pipeline/run`
   daily_pipeline 发现 daily 有而 enriched 缺 → 走 forward incremental 重算这 9 天
   （连板数从 enriched 最近 60 天历史前缀递推）
3. 验证：这 9 个分区回到 15 列，且 `consecutive_limit_ups > 0` 计数合理
4. 确认 `/api/regime/mainline/recompute` 不再 500

前置条件：服务需停 → 合入 `enriched-cols` 分支 → 重启。
当前有港股 catch-up 任务在跑，需等它结束后再停服。

---

## 六、观察项（本次未改）

- `daily_pipeline.py:818` / `extend_history.py:78` 用 duckdb
  `read_parquet('{d}/instruments/**/*.parquet', union_by_name=true)` 建视图。
  duckdb 的 `union_by_name` 对类型差异容忍度高于 polars，且已有 try/except 降级，
  未观察到失败，暂不改动。若后续出现 instruments 视图异常，这里是第二个排查点。
- 港/美股维表与 A 股同目录存放是设计上的隐患。若要根治，建议按市场分子目录
  （`instruments/cn/`、`instruments/hk/`、`instruments/us/`），但涉及多处路径改动，
  本次用"精确读文件"低成本规避。
