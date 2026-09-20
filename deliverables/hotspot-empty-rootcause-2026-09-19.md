# 热点工作区空白 — 根因与修复 — 2026-09-19

## 现象

用户反馈「热点工作区还是没有内容」。服务当时后端(3018)未启动,但即便起来也分市场表现不一,
所以逐个市场端到端验了一遍(绕过面板密码守卫,用本机身份直连 API)。

## 三个市场各自的根因

| 市场 | 现象 | 根因 | 状态 |
|---|---|---|---|
| A 股 `cn` | 0.1s / 20 条题材 / 09-18 数据 | 无。**之前空白纯粹因为后端没起** | ✅ 本来就是好的 |
| 港股 `hk` | 2.6s 返回 **0 条**，`quality=failed` | `hk_instruments.parquet` 的 `industry` 覆盖只有 **10/2816**，热门源按行业聚合 → 无成分股 | ✅ 已修 |
| 美股 `us` | **请求挂死 22 分钟以上** | 盘中判定为交易时段后，对 **5653 只**行业成分股逐只走 **yfinance**（每只多次 HTTP） | ✅ 已修 |

### 港股：行业映射缺失

`scripts/sync_hk_industries.py` 原本是**逐只 filter** 拉东财
`RPT_HKF10_INFO_ORGPROFILE`（`(SECUCODE="00700.HK")`），2816 只 × 8 并发。
进度文件显示 2798 只"拉过"，但落盘只有 10 只有行业 —— 要么接口那时不可用，要么被后续维表同步冲掉。

实测（2026-09-19）该接口**不带 filter + 分页**完全可用：
`pageSize=500` 时返回 500 条，全库 6925 条 / **14 页**。

修复：脚本新增批量模式（**默认**），14 次请求拿全市场；落盘逻辑抽成 `_write_industries()`
并保留原值兜底。逐只模式保留为 `--per-symbol` 补漏。

```
批量拉取完成 3710 只, 耗时 1.7s
已落盘 hk_instruments.parquet (2816 行), sector 覆盖 10 → 2810 (31 类)
```

修复后港股热点：2.6s / 20 条 / 半导体 +6.08% / 09-18，`quality=stale`（周末 as_of≠今日，符合预期）。

### 美股：实时行情规模失控

原路径 `create_market_realtime_provider("us")` → `YFinanceProvider.get_realtime()`，
对 5653 只成分股逐只请求。faulthandler 抓到的栈：

```
curl_cffi perform ← yfinance data.py:_make_request
yfinance scrapers/quote.py:previous_close
yfinance_provider.py:218 get_realtime
hk_us_source.py:362 _fetch_us_realtime
```

同一个窗口 PDE 永不返回 → 前端拿不到任何 JSON → 页面空白（不是"空列表"，是根本没响应）。

修复：改走**腾讯批量** `qt.gtimg.cn/q=usAAPL,usMSFT,...`（实测 200 只 / 0.11s / 覆盖率 100%），
每批 200 只，全市场约 30 次请求，并加**硬超时闸门**`REALTIME_DEADLINE_S=25s`：
超时返回空 dict → 上层回落 enriched 日K，"宁可标 stale 也不挂死"。

单位口径（差点埋雷）：腾讯 `change_pct` 是**百分数**（-0.71 = -0.71%），
enriched 日K 是**小数**（0.0266 = +2.66%）。新增 `_pct_from_quote_row()`：
优先 `last_price/prev_close` 现算，没有 prev_close 才按百分数 /100。

修复后美股热点：**3.0s**（原 22+ 分钟）/ 20 条 / `provider=hkus_industry:realtime` / 09-18 盘中。

港股实时路同时加了分块 deadline（第一块放行、之后每块查闸门），防同类问题 —— 港股当日闭市未能实测。

## 改动清单

| 文件 | 改动 |
|---|---|
| `backend/scripts/sync_hk_industries.py` | 新增 `fetch_all_industries()` 批量分页模式（默认）+ `_write_industries()` 抽公共落盘；`--per-symbol` 保留逐只兜底 |
| `backend/app/services/hotspot/hk_us_source.py` | `_fetch_us_realtime` 换腾讯批量 + deadline + 单位换算；`_fetch_hk_realtime` 加分块 deadline；新增 `_pct_from_quote_row()` |
| `backend/tests/services/hotspot/test_hk_us_source.py` | +4 回归用例（批量而非逐只、百分数→小数、超时回落、单位换算优先级）|

数据侧：`data/instruments/hk_instruments.parquet` sector/industry 覆盖 10 → 2810；
改前已备份为 `hk_instruments.parquet.bak-20260919`。

## 验证

- `ruff check backend/`：**All checks passed**
- 热点模块测试：**46 passed**（含 4 条新增回归）
- 全量 **2222 passed / 1 failed**；唯一失败
  `test_hk_us_matrix.py::test_symbol_partition_disk_cache_reuses_and_invalidates_on_append_and_rewrite`
  单独跑该文件 29 passed —— 是**全量并发下 mtime 粒度**导致的抖动，
  与本次改动无关（该用例纯 `tmp_path` 隔离，不碰热点代码）。
- 端到端（本机身份绕过密码守卫）：

| 市场 | 耗时 | 条数 | provider | 数据日期 |
|---|---|---|---|---|
| cn | 0.1s | 20 | cn_local_concept | 2026-09-18 |
| hk | 2.6s | 20 | hkus_industry:daily | 2026-09-18 |
| us | 3.0s | 20 | hkus_industry:realtime | 2026-09-18 盘中 |

## 遗留 / 后续

- **matrix 缓存签名**（`backtest/matrix.py:1587-1607`）用 `(size, mtime_ns)` 判失效。
  同尺寸 + 同一时间戳 tick 内的改写可能判不出来。本机全量跑偶发抖一次，
  建议后续加内容摘要或写入版本号加固 —— 与本轮无关，单独排期。
- 港股实时路径（腾讯/新浪 `fetch_quotes_batch_sync`）当日闭市未实测，下个交易日盘中应验一次。
- 港美 enriched 仍停在旧日期（US 快照 as_of=2026-09-11），需要跑港美 enriched 补数。
