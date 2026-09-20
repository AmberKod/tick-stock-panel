# 港股 catch-up 覆盖度判据修复（2026-09-18）

## 现象

重启服务验证 regime 时发现：港美股 enriched 每日标的数

| 日期 | 港股标的数 |
|---|---|
| 09-14 | 1209 |
| 09-15 | 1208 |
| 09-16 | 1205 |
| **09-17** | **72** |

regime hk 的 09-17 行因此只有 `up=37 / down=27`（前一日 645/448），
样本严重不足，却照样作为「市场环境」上板。

## 根因：两层叠加

1. **调度窗口错配**：`pipeline@15:30` 跑，但服务是 23:43 / 20:35 才启动 →
   盘后管道当时不在线。
2. **兜底判据只看日期不看覆盖度**：
   `_market_daily_catchup_needed` 用抽样众数取「最新日期」，按
   `今天 - 最新日期 > 容差`（HK 4 天）判定。72/2812 太少撑不起众数，
   众数停在 09-16 → 落后 2 天 → **判定"不落后"，不补跑**。

对照：美股落后 7 天 > 容差 3 → 正常触发补跑。差距就在覆盖度这一维。

## 修复（`feature/hk-catchup-coverage`，提交 `09c47a2`）

- 抽出 `_h6_latest_distribution()`：返回抽样分区 `max(date)` 的分布
  （`_h6_latest_by_sampling` 复用它取众数，行为不变）
- 新增 `_h6_partial_sync_pending()`：**最新日期的分区数 < 众数分区数 × 50%**
  → 判定「部分同步未完成」，补跑有必要
- `_market_daily_catchup_needed()` 在日期判据之外追加覆盖度判据
- 新增 2 个测试：部分同步触发 / 完整同步不触发

验证：`ruff check .` All checks passed；worktree 全量 **2168 passed**
（唯一失败 `test_dev_launcher` 是 worktree 缺 `.venv` 的环境差异）。

## 附带修复：冷却期不再空转（提交 `d08f7df`）

```
20:36:03  market_daily catchup: US H6 数据落后, 启动补跑
20:36:06  yfinance rate-limited A     (熔断计数 1/5)
20:36:09  yfinance 熔断打开: 限流连续命中, 冷却 10 分钟
20:36:12  job 结束: succeeded=0 / failed=6071
```

**澄清**：job 层的 `succeeded` 只表示「job 没崩」，同步结果
`result.status` 其实是 `failed`、6071 只全 `provider_error`——标记本身没错。
真正的问题是**明知 provider 在冷却还照样启动补跑**，白烧一次 job 槽，
下次启动又重复（判据仍判定落后）。

修法：

- `yfinance_provider` 暴露 `yf_circuit_blocked()` / `yf_circuit_remaining_seconds()`
  / `yf_circuit_open_for_test()`，让调度层能读到熔断状态
- `daily_pipeline._provider_cooling_down(market)`：US 查 yfinance 熔断，
  查询失败按「未冷却」处理（不改变原有行为）
- `run_market_daily_catchup`：冷却期记 `deferred` 并跳过，不占 job 槽空转；
  判据不变，下次启动仍会重试
- 新增 2 个测试：冷却期延后不触发 / `_provider_cooling_down` 读熔断状态

验证：`ruff check .` All checks passed；全量 **2169 passed**
（`test_worker_process` 全量偶发失败为 flaky，单跑 11 passed）。

## 待办

- [ ] 停服务 → 合入 `09c47a2` + `d08f7df` → 重启
      - 港股应自动触发补跑（覆盖度判据），验证 09-17/18 补齐
      - 美股若仍在 yfinance 冷却期，应记 `deferred` 而非空转
- [ ] 港股长期缺口：每日覆盖从 1913 只降到 ~1208 只（09-04 起），既有 P0 遗留
