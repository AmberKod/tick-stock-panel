# P2-1：多日分钟蜡烛改走 0x240D（真实开高低收）

日期：2026-10-13 · 分支 `develop` · 全部结论来自真机 TCP 实测（`116.205.135.205:7727`）

---

## 1. 为什么要换

上一轮（P1）的 `minute_range` 走 **0x240C 按日循环**，有一个硬折衷：
0x240C 每根只给「该分钟的一个价格」，**没有开/高/低**。于是

- m1 档只能标 `single_price`（四价同值）；
- N 分钟档只能由**单点价格的极值**去推 OHLC —— 实测相对交易所原生 MIN_5
  有平均 `<0.05%` / 最大 `<0.45%` 的偏差。

vendored `easy_tdx` 里本来就有 `GetExHistoryInstrumentBarsRangeCmd`（**0x240D**，区间K线），
之前没启用。这轮先做事实核对，再决定要不要切。

---

## 2. 实测结论（决定性的五条）

探针：`probe_240d_range.py` / `probe_240d_detail.py`（同目录）

| # | 结论 | 证据 |
|---|---|---|
| 1 | **每根都有真实 OHLC** | HK 00700 330/330 根、US AAPL 390/390 根的开高低收不全相等（少数相等是那一分钟确实没波动） |
| 2 | **时间戳与 `goods_kline` MIN_1 完全一致** | 0x240D 是 `09:31~16:00`(HK) / `21:31~04:00`(US)；而 0x240C 早一格（`09:30~15:59`）。两源同分钟 close 偏差 **0.000%**、volume 差 **0.0** |
| 3 | **单次上限 1500 根，且从 `start_date` 正向累计** | 请求 `20261001~20261013` 返回 `10-02~10-08`；请求跨 21 个月也只回 1500 根 |
| 4 | **未来/非交易日回空包，不是全 0 填充包** | 明日 `20261014` → `n=0`；今日 HK `20261013`（盘中）→ 277 根真数据 |
| 5 | **`hour` 已由上游 mod 24** | 美股会话是 `21,22,23,0,1,2,3,4`（不是 0x240C 的 21~27） |

外挂一条：从真实 m1 OHLC 聚合出的 MIN_5，与上游**原生** MIN_5
**逐根 0.000% 一致**（HK 66 根 / US 78 根，开高低收全项）——命令 aggregate
规则本来就与交易所一致，之前有偏差是因为输入是单点价格而不是真 OHLC。

---

## 3. 落地（`backend/app/data_providers/tdx_ex_provider.py`）

**拆成两条路（关键决策）**：

```
_minute_days_240c(symbol, days)   → 0x240C 按日循环，每根带 avg_price
                                     └─ 多日分时 / 当日分时专用（要均价线）

_minute_days_240d(market, code, days) → 0x240D 分片取，每根带真实 OHLC
                                     └─ minute_range（蜡烛图）专用
```

为什么不全切：**0x240D 不给均价**。多日分时的均线是上游直给的 `avg_price`，
不能拿成交量加权"算"一个冒充同名。所以 0x240C 那条路原样保留，
`multi_day_ticks` 改调 `_minute_days_240c`。

**0x240D 这条路的四个要点**：

1. **交易日历不猜**：先 `_daily_closes()` 拿日K 日历，取最后 `days` 个交易日。
   （0x240D 虽然不回填脏数据，但"哪天算交易日"仍得靠日K —— 港美股假期猜必错。）
2. **倒序分片**：`chunk = 1500 // 每日根数上界`（HK 340 → 4 天/片，US 400 → 3 天/片），
   因为上限是正向累计的。最后一片的 `end_date` 必须是**最新**交易日。
3. **按会话切天**（`_split_range_days`）：不能按 `datetime` 字符串排序 ——
   `hour` 已 mod 24，凌晨段会被甩到开盘段前面。改用会话内单调钟
   （`hour < start_hour → +24`），HK 是 9→16、US 是 21→28 一路递增，回落即换日。
   日期标签取每根自带的 `year/month/day`（真源）。
4. **聚合规则**（`_aggregate_m1`）：open 取组内首根的 `open`、high 取 `max(high)`、
   low 取 `min(low)`、close 取末根 `close`；时间戳取组内**末根**（实测与原生对齐：
   09:35 / 04:00），不做 `+step`（美股收盘那组会越过 04:00 造出不存在的时刻）。

**payload 变化**：

| 字段 | 旧 (0x240C) | 新 (0x240D) |
|---|---|---|
| `protocol` | `0x240C` | `0x240D` |
| `ohlc` | `single_price` / `aggregated_from_m1` | `native` / `aggregated_from_m1` |
| `derived` | `["open","high","low"]` | `["open","high","low","close"]` |
| `has_avg` | `true` | **`false`**（0x240D 不给均价，如实标注） |

---

## 4. 线上实拉（2026-10-13 15:40 前后）

```
00700.HK m1 days=5   protocol=0x240D ohlc=native bar_count=1611
  10-07 330根 09:31~16:00  真实OHLC=327/330
  10-08 330根              真实OHLC=330/330
  10-09 330根              真实OHLC=329/330
  10-12 330根              真实OHLC=330/330
  10-13 291根 09:31~15:21  真实OHLC=290/291   ← 当日盘中，实时
AAPL.US m1 days=5    bar_count=1950  每天 390 根 21:31~04:00（跨零点保序正确）
```

> HK 1611 / US 1950 均超过单层 1500 —— 说明分片 + 归并真的生效了。

交集比对（`minute_range(days=1)` vs `/minute` goods_kline MIN_1）：

```
00700.HK: 291 根交集   close 平均偏差 0.000000%   volume 差 0.0
AAPL.US : 390 根交集   close 平均偏差 0.000000%   volume 差 0.0
```

即：**1 日档与多日档的 x 轴首次完全对齐**（旧实现差 1 分钟）。

---

## 5. 过程中自己引入的 bug（都已修，测试覆盖）

1. **日期 key 不同型**：`merged` 用 `"2026-10-08"` 做 key、查找却用 `"20261008"` →
   全部 miss → "最近 N 个交易日均无分钟数据"。改为全程 `yyyymmdd`。
2. **market 大小写**：`market` 是 `"HK"`（大写），而 `_MINUTE_BARS_PER_DAY` /
   `_SESSION_START_HOUR` 用小写 key → 静默走 fallback：港股按美股的每日根数上界
   分片（多发一次请求），会话起始小时错成 21（虽然巧合下单调性仍成立，但纯属侥幸）。
   改为 `market.lower()`。

---

## 6. 测试与验证

- `tests/test_tdx_ex.py`：**103 → 112 passed**
  - 删 6 个只描述 0x240C 语义的旧断言（这些语义现在归 `_minute_days_240c`，已改测它）；
  - 新增 15 个：真 OHLC 下发、`has_avg=false` 且 `avg=None`、倒序分片（10 天→3 片，
    最后一片 end = 最新交易日）、美股跨零点保序、多会话切天、真 OHLC 聚合
    （`open` 取首根 open 而不是首根 close）、空包跳过、全 0 日丢弃、越界日期丢弃、
    日历缺失 fail-closed、全/ММ node down、参数越界、缓存键含 days。
- `tests/test_hk_us_deep.py` 端点层：新增 `_RngRec` / `_patch_minuterange` 桩，
  `test_api_minuterange_m1_flags_single_price` → `test_api_minuterange_m1_is_native_ohlc`，
  capabilities 断言改判 `0x240D`。**21 passed**。
- `ruff` 干净；`tsc --noEmit` + `pnpm build` 通过。

前端改动：`StockDailyKChart.tsx`（标注文案 + 类型注释）、`KPeriodTabs.tsx`（注释）、
`scripts/hkus_minute_depth_verify.mjs`（断言改判新文案，并新增"不含均价线"一条）。
