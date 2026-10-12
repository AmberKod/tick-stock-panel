# P0 落地：港股逐笔成交 + 分钟序列扩容（2026-10-12）

接的是上一份《deepread》里定下的 P0 两项。所有结论都先实测再写代码。

---

## 一、动手前先钉死的三个事实

不确认这三条就会画错图，所以先跑探针（脚本在 `deliverables/tdx-ex-scan/`）。

| # | 问题 | 实测结论 | 对实现的影响 |
|---|---|---|---|
| 1 | `goods_kline` MIN_1 的时间戳口径 | US AAPL 700 根：`10-08` 309 根、`10-09` **390 根**（含 04:00 凌晨段）、`10-12` 1 根 → 确认是**会话日期 + 北京时间钟点**，凌晨段不翻页 | 0x240C 的 `hour` 要到 27，必须 `% 24` 输出，才能与现有 `minute_bars` / `intraday` 同口径 |
| 2 | 0x240C 的 `volume` 是不是累计值 | **不是**。港股首根 859434、次根 171997、末根 2180500（美股单调但首末异大）→ 是**每分钟成交量**，首末两根含集合竞价 | 不拿它推均价；均价直接用上游 `avg_price`（更权威） |
| 3 | 0x240C 有没有 OHLC | **没有**，只有 `price` + `avg_price` + `volume` | m1 档不能伪造蜡烛；更长周期才能聚合，且必须先量化偏差（见下） |

**聚合可信度实测**（`probe_m1_agg_vs_upstream.py`，同一天 HK 00700 / US AAPL 各与上游 MIN_5 逐根对比）：

| 字段 | 平均偏差 | 最大偏差 |
|---|---|---|
| close | **0.000%** | 0.000% |
| high | 0.021% ~ 0.037% | ≤ 0.205% |
| low | 0.023% ~ 0.041% | ≤ 0.289% |
| open | 0.033% ~ 0.061% | ≤ 0.434% |

→ 可以聚合，但**必须标注**：close 是真值，开/高/低是推导值，偏差写进 payload。

---

## 二、做了什么

### 后端（`tdx_ex_provider.py`）

**1. `transactions(symbol, date=None, limit)` —— 逐笔成交**

| 市场 | 协议 | 价格 | 方向字段 |
|---|---|---|---|
| 港股 | 当日 **0x23FC** / 历史 **0x2406** | 上游整数 `÷1000` = 港元 | `nature` **原样下发不映射**（未实测，不猜"0=买"） |
| 美股 | **0x122F** | 已是浮点 | `bs_flag`（0买/1卖/2中性/5盘后，协议注释口径） |

- `trade_count`：港股 ex 协议没有这个字段 → 给 **`None`**，不给 0 冒充（0 会被读成"0 笔成交"）。
- 上游按时间倒序返回 → 下发前统一升序。
- 空结果 = `TdxExUnavailableError`（fail-closed），不返回空表冒充"今日无成交"。
- 日期解析不宽容：只认 `YYYY-MM-DD` / `YYYYMMDD` / `date`，其余 `bad_date`（猜格式会把 9 月读成 10 月）。

**2. `minute_range(symbol, days, period)` —— 分钟序列按日循环**

走 `GetExHistoryMinuteTimeDataCmd`（**0x240C**）= 单日一包，按 `date` 循环拼接，**不再受 700 根限制**。

- 非交易日上游给空包 → 跳过（不是故障）。
- `period=m1`：`ohlc="single_price"`，四价同值 = 该分钟价格，payload 明说"上游无真实开高低"。
- `period=m5/m15/m30/m60`：由 m1 确定性聚合，`ohlc="aggregated_from_m1"`、`derived=["open","high","low"]`、note 里写明实测偏差。
- 聚合 bar 的时间戳取**组内最后一根**（不 `+step`，否则美股收盘那组会造出 04:00 之后的时刻）；比上游原生 N 分钟 K 的区间结束时刻早 1 分钟，已写进 docstring。
- **透出上游 `avg_price`**（当日累计均价），比我们 m1 自己合成的均价权威。

### 端点（`api/hk_us_deep.py`）

- `GET /api/{hk,us}/deep/transactions/{symbol}?date=&limit=`（limit 1..1800）
- `GET /api/{hk,us}/deep/minuterange/{symbol}?days=&period=`（days 1..20）
- `capabilities` 新增 `transactions` / `minute_range` 两项（含协议与口径说明）

### 前端

- 新增 `HKUSTransactionsPanel.tsx`：逐笔明细表（时间/成交价/成交量/笔数/方向），30s 轮询；**港股不标方向**（后端明确未实测），美股按 `bs_flag` 标买卖色。
- 挂在整页 `StockDetail` 的非 A 股区块（板块面板、多日分时之后）。

### 测试

- `test_tdx_ex.py`：**96 passed**（原 68，+28）——协议分叉、价格除法、排序、日期校验、空结果 fail-closed、m1 不造 OHLC、聚合声明、hour%24、非交易日跳过、均价透出。
- `test_hk_us_deep.py`：新增 8 个端点测试全过（含 bad_limit / bad_date / 节点全挂 → HTTP200 + reason）。

---

## 三、真机验证（生产代码直连，不是参考项目）

```
--- HK 逐笔 当日 ---   0x23FC/0x2406  today  price_unit=HKD(上游整数/1000)
  {"time":"15:59:00","price":425.2,"volume":400,"trade_count":null,"nature":0}
--- HK 逐笔 历史 2026-10-09 ---   424.2 港元 ✓（10-09 收盘 424.80）
--- US 逐笔 ---        0x122F  22:04 341.89 vol 8005 bs_flag 0 / 22:05 bs_flag 1 ✓

00700.HK m1  days=3  bar_count=990   ohlc=single_price  首 09:30 422.4 avg 422.2505
AAPL.US  m1  days=3  bar_count=1170  首 21:30 → 末 03:59 ✓（跨零点口径正确）
00700.HK m5  days=3  bar_count=198   ohlc=aggregated_from_m1  首根 open 422.4 high 424.6 low 422.4 close 423.4 ✓
```

港股 990 根 / 美股 1170 根，均**超过旧的 700 根天花板**。

---

## 四、已知限制（都写进了 payload / note，前端照着展示）

1. **0x240C 没有真实开高低**：m1 档是"分钟价格线"不是蜡烛；更长周期是 m1 聚合（close 真值，开高低偏差 <0.45%）。要原生蜡烛仍走 `/minute`（700 根上限）。
2. **volume 是每分钟成交量**（首末两根含集合竞价明显偏大），不是累计值 —— 不要拿它做累计口径的图。
3. **港股逐笔无买卖方向**：`nature` 语义未实测，前端不标色。
4. 逐笔单次一页（≤1800）不翻页：上游没有 `total` 字段，翻页终止条件不严谨，不冒险。

## 五、下一步

- 美股多日分时：数据基座（`minute_range` 按日循环、日期自洽）已经好了，可以直接替换现在 `/multiday` 对美股的 fail-closed 分支。
- 分钟 K 页签接 `minuterange`（长区间走聚合蜡烛并标注，短区间走原生蜡烛）。
