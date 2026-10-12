# P1：美股多日分时打通 + 分钟K 页签接长序列

日期：2026-10-12
范围：`tdx_ex_provider.multi_day_ticks` 重写、前端 `HKUSMultiDayChart`、`StockDailyKChart` 接 `/minuterange`

---

## 1. 美股多日分时 —— 从 fail-closed 到打通

### 之前为什么不给

上游 `TickCharts` 给美股的**不是「一天一块」**：

- 分块是「390 根一页」而不是按交易日切；
- `dates[]` 与内容**不对应**（day0 pre=336.640 而 day1 收 340.63）；
- 每块 minutes 累加到 1679+，mod 24 后钟点不可读。

要画就只能由我们给每块**指定日期** —— 那是编数据，所以一直 fail-closed。

### 现在的路子（港美股统一一条）

| 组成 | 来源 | 说明 |
|---|---|---|
| 分钟序列 | **0x240C** 按日循环 | 日期是我们传给上游的，天然自洽；港股 330 根/日、美股 390 根/日 |
| 昨收 | **日K**（`Period.DAILY`） | `prev_close = 该日在交易日历里前一个交易日的收盘`。是真源，不是推导 |
| 均价 | 0x240C 自带 `avg_price` | 比 m1 自算的权威 |

### 三重守卫（任一不满足即 fail-closed）

1. **日期在日历内** —— 0x240C 取到的每一天必须存在于日K 交易日历；
2. **末价 = 收盘** —— 当日最后一根 `price` 必须等于日K 当日 `close`（容差 0.5%）；
3. **相邻不漏** —— 相邻两天在日历里必须是相邻交易日（中间不漏天）。

### 实测（真机 TCP 直连 7727）

探针：`deliverables/tdx-ex-scan/probe_us_multiday_shape.py`

```
AAPL.US  日K 日历: 10-01 330.32 / 10-02 333.69 / 10-05 332.89 / 10-06 333.63
                   10-07 336.67 / 10-08 340.42 / 10-09 336.64 / 10-12 341.06
0x240C   日序列:   10-06(390) 10-07(390) 10-08(390) 10-09(390) 10-12(92, 进行中)

逐日核对(0x240C 末根 vs 日K close):
  OK 10-06  333.63 vs 333.63  0.000%
  OK 10-07  336.67 vs 336.67  0.000%
  OK 10-08  340.42 vs 340.42  0.000%
  OK 10-09  336.64 vs 336.64  0.000%
  OK 10-12  341.06 vs 341.06  0.000%

昨收链(日K 真源):  10-12 prev=336.64 (= 10-09 收盘 ✓)
```

港股 00700 同样 5/5 全过（10-12 prev=424.8 = 10-09 收盘 424.8 ✓）。

**10 组逐日核对全部 0.000% 偏差** —— 这是敢把「昨收由日K 给出」写成真源、而不是推导值的依据。

### 前端

`HKMultiDayChart` → **`HKUSMultiDayChart`**（改名，因为它现在服务两个市场）：

- 市场由后缀判定，分别打 `/api/hk|us/deep/multiday`
- 美股会话跨北京时间零点（21:30 → 次日 04:00），检测到就显示橙色提示，说明「时间轴走到 00:00 之后属于同一会话，不是日期错乱」
- 最新一天线宽加粗
- 配色扩到 10 条（天数上限同步提到 10）

---

## 2. 分钟K 页签接长序列

`StockDailyKChart` 新增 `deepMinuteDays` prop：

| 档位 | 端点 | 数据性质 |
|---|---|---|
| `1 日` | `/minute`（0x122E） | **交易所原生蜡烛**，但上游单次上限 700 根 |
| `3 / 5 / 10 日` | `/minuterange`（0x240C 按日循环） | 突破 700 根；开/高/低由 m1 聚合 |

新增 `MinuteDepthTabs`（1/3/5/10 日），只在分钟档打开时出现，挂在 `MinutePeriodTabs` 旁边。

**来源必须写在图上** —— 选 >1 日时图下方显示：

> 共 1170 根 / 3 个交易日（时间为北京时间）—— 开/高/低由上游 1 分钟价格聚合：收盘价与上游一致（实测 0.000%），开高低平均偏差 <0.05%、最大 <0.45%。

m1 档则写「上游只给该分钟价格（开高低为同一值），不是交易所原生蜡烛」。

不标就等于让用户把聚合值当交易所原生 OHLC 用。

---

## 3. 改动文件

**后端**
- `app/data_providers/tdx_ex_provider.py`
  - 新增 `_daily_closes()`（日K 交易日历，昨收真源 + 日期基准）
  - 重写 `multi_day_ticks()`（0x240C 按日循环 + 三重守卫），去掉 `TickChartsCmd` import
  - 新增常量 `MAX_MULTIDAY_DAYS=10` / `_MULTIDAY_CALENDAR_PADDING=10` / `_MULTIDAY_CLOSE_TOLERANCE=0.005`
- `app/data_providers/hk_us_deep_provider.py`：capabilities 的 `multiday` 改为两市场均可用；删除 `multiday_unavailable_reason()`
- `app/api/hk_us_deep.py`：`/multiday` 文档与 days 上限改为 `1..10`

**前端**
- `components/HKMultiDayChart.tsx` → `components/HKUSMultiDayChart.tsx`（改名 + 支持美股 + 跨零点提示）
- `components/StockDailyKChart.tsx`：`deepMinuteDays` prop、`/minuterange` 分支、days flatten、聚合来源标注
- `components/KPeriodTabs.tsx`：新增 `MinuteDepthTabs` / `MINUTE_DEPTHS`
- `pages/StockDetail.tsx`、`pages/HKStockAnalysis.tsx`、`pages/USStockAnalysis.tsx`：挂载

**测试**
- `tests/test_tdx_ex.py`：删掉基于 `TickCharts` 的 6 个旧桩/测试，新增 10 个（含三个守卫各一个、美股可用性）
- `tests/test_hk_us_deep.py`：新增 5 个端点测试（HK/US 均可用、bad_days、上游故障、capabilities）

---

## 4. 上线后真机抓到的 bug：0x240C 会给「未来的日期」发全 0 填充包

部署完打 `/api/us/deep/multiday/AAPL.US` 被守卫①拦了：

```
available False
reason: multiday: 2026-10-13 不在日K 交易日历里 —— 分钟与日线口径不一致, 拒绝下发
```

探针 `probe_us_session_date_label.py`（北京 10-13 00:19）给出真相：

```
AAPL.US
  20261008: 390 根  首 21:30 337.34  末 27:59 340.42
  20261009: 390 根  首 21:30 332.7   末 27:59 336.64
  20261012: 157 根  首 21:30 341.14  末 24:06 340.1     ← 会话进行中
  20261013: 390 根  首 21:30 0.0     末 27:59 0.0       ← 全 0 填充包!
  日K 日历: ... 20261009 336.64 / 20261012 340.1351
00700.HK
  20261012: 330 根 ... 425.8
  20261013: 330 根  首 09:30 0.0  末 15:59 0.0           ← 同样全 0
```

两个结论：

1. **上游对「还没发生的日期」不回空包，而是回一整包 `price=0` 的填充数据**。原来只判「非空」→ 把次日当成交易日，图上多一条 0 元线，多日分时还被守卫①整体拦掉。修法：额外要求**包内至少一根 `price > 0`**。
2. **日期口径其实是对齐的**：0x240C 用「会话开始时的北京日期」给美股会话打标签（21:30 开盘那天），与日K 的美股交易日标签一致 —— 差的只是那个全 0 包。

这个 bug 只在「今天还没开盘」的窗口里出现（午夜后到开盘前），所以白天测不出来。

## 5. 踩到的坑

1. **桩的键类型**：`GetExHistoryMinuteTimeDataCmd.date` 是 **int**（20261012），而我在桩里用字符串做键 → 全部 miss，报「最近 N 个交易日均无分钟数据」。桩必须 `str(cmd.date)`。
2. **守卫测不出来**：helper 里分钟与日K 共用一份 `close`，`close_override` 一改两边都改 → 守卫②永远不触发。必须**分开记** `true_close`（分钟侧）和 `daily_close`（日K 侧），override 只改后者。
3. **`lst[-0:]` 等于整串**（Python 语义）→ helper 传 `n_days=0` 会把 40 天全当有数据，`n_days <= 0` 必须显式短路。
4. **并发跑 pytest 抢 `data/.mining_process.lock`** → `MiningProcessLockError`（假失败）。一次只跑一个。

---

## 6. 已知限制

- 美股当天仍在交易时，最后一天只有部分分钟（实测 10-12 取到 92 根），图上那条线会短一截；日K close 也是实时值，守卫按实时值比对通过。
- 多日分时上限 10 日（图可读性 + 每天一次上游请求）。
- 分钟档 >1 日时开/高/低是聚合值，偏差已量化并显示在图上；要交易所原生蜡烛仍选 `1 日`。
