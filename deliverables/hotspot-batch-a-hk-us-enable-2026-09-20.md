# 热点工作区 · 批次 A：港美热点启用

**日期**：2026-09-20
**提交**：`2a7bb2b`（本地，未推送）
**状态**：✅ 代码完成 · ✅ 端到端验证通过 · ✅ 293 passed / 1 xfailed · ✅ ruff 全绿 · ✅ 工作区干净

---

## 一、根因：港美热点为什么一直是空的

源一直是通的 —— `source.py:420-432` 早已把 `hk` / `us` 路由到 `HkUsIndustryHotspotSource`。
真正卡住的是四件事，缺一不可：

| # | 卡点 | 后果 |
|---|---|---|
| ① | 缓存无 TTL | 源是模块级单例，行情/维表只在进程启动时加载一次，之后永远命中首轮快照 |
| ② | 港美无 cron 入口 | `hotspot_sync.py` 只有 cn 一个 job，港美**从来没有被定时触发过** |
| ③ | cn cron 用错源 | 曾对 cn 强制 `AkshareHotspotSource`（依赖 `push2.eastmoney.com`，本机不可达），工作日每半小时刷一次把 `job_state` 刷成 empty，前端显示"无数据" |
| ④ | history 无 market 字段 | 港美一旦落盘，同名 topic 在同一 jsonl 里无法区分，历史必须清洗 |

## 二、改动清单

**1. 缓存 TTL** — `backend/app/services/hotspot/hk_us_source.py`
- `QUOTE_TTL_S = 180`：须远小于 cron 间隔 30min（否则定时永远命中旧缓存），又大于一次 `discover` 耗时。
- `INSTRUMENTS_TTL_S = 3600`：维表是日级变量。
- `_store_quotes()` 收敛为 quotes / mode / as_of 的**唯一写入点**，杜绝"新行情 + 旧 as_of"错配；`_begin_round()` 保证同一轮取数口径一致。
- `clock_fn` / `quote_ttl_s` / `instruments_ttl_s` 可注入 → 测试不用真等 180 秒。

**2. 港美定时** — `backend/app/jobs/hotspot_sync.py`
- 单条 cn job 换成 `_SYNC_JOBS` 四元组表：

| job | 时间（北京） | 星期 | 说明 |
|---|---|---|---|
| `cn` | 9-15 | mon-fri | A 股盘中 |
| `hk` | 9-15 | mon-fri | 港股盘中 |
| `us` | 21-23 | mon-fri | 美股前半场 |
| `us_late` | 0-3 | **tue-sat** | 美股后半场（跨日历日，故拆两个） |

> ⚠️ **cron `day_of_week` 是按日历日判的，不是交易日。** 写成单段 `21-23,0-3` + `mon-fri` 会让周五夜盘后半段（周六 00:05~03:35）整段断档 7 个触发点，而周一凌晨美股休市反而空跑 8 次。`tue-sat` 恰好覆盖周一~周五夜盘的后半段且排除休市日。
> 🟡 冬令时（22:30-05:00 北京）尚未覆盖。

**3. cron 换源** — 移除 `run_hotspot_sync` 里的 akshare 硬编码，改走 `select_source` 默认（`CnConceptHotspotSource`）；akshare 保留为可显式注入的备源。

**4. history 补 market** — `storage.py` / `service.py`
- `append_history` / `append_constituents_history` 的 `market` 参数改为**必填**（缺参数直接报错，不留默认值）。
- `load_history_jsonl` 增加可选 `market` 过滤。
- 两处写入点（`service.py:130` 成分股、`:212` 三市场共用出口）全部显式传 market。

## 三、验证结果（QA 端到端，非仅单测）

| 项 | 结果 |
|---|---|
| 港美首次落盘 | **HK 29 topic / US 110 topic**（港股 31 行业里 29 个过 `MIN_MEMBERS=10`） |
| TTL 真生效 | T0 loader=1（真实取数）→ T+30s 命中缓存 0.32s → T+200s 越过 180s 重新真实取数 |
| 存量老数据零伪造 | 1536 行无 market 的老记录，按 `market='cn'` 过滤命中 **0 行** |
| 测试 | 293 passed / 1 xfailed |
| lint | `ruff check backend/` 全绿 |

> 验证时服务并未在线（3020 / 3011 均无监听、机器上无 python 进程），QA 改用**进程内直调** `discover_hotspots()`，走的是同一条代码路径。
> 教训：**环境事实要先探测，再写进验证 spec**，不要拿"我以为服务在跑"当前提。

## 四、明确记录的技术债（主动选择，非遗漏）

- **存量 1536 行 history 无 market 字段**（`source` 值是 `'concept'`，不是 `cn_local_concept`）。
  按 market 过滤一律不匹配，**刻意不按 source 倒推成 cn** —— 那是伪造。
  代价：这批观测点在按市场口径下**永久缺席**。要复用必须写显式的迁移脚本，不可自动推断。
- 港股热点样本覆盖率仅 42.4%（enriched 到 09-18 的 1187/2798，973 只停在 09-03），
  `_load_latest_rows` 按"全市场 max date"过滤会把它们**静默丢弃**，不报错也不标 missing → 幸存者偏差。
- 港美 `stage` 恒为「初次异动」，`trend` / `persistence` / `cooling` 三列永远显示 `—`。
- GET 每次请求都无条件 append history，已存在重复行（09-18 的 768 行 = 384×2）。

## 五、下一步候选

| 批次 | 内容 | 价值 |
|---|---|---|
| **B** | 让数据可信：港股 42% 覆盖显式标注 / 港美 stage 不再恒"初次异动" | 高 —— 直接消掉幸存者偏差与假徽标 |
| **C** | 清理：7 处过期注释、history append 去重、1536 行迁移脚本 | 中 —— 卫生问题 |
| — | 美股冬令时 cron 覆盖 | 低 —— 到 11 月才生效 |

## 六、推送状态

远端 `origin/develop` = `948a393`，本地 HEAD = `2a7bb2b`，**未推送 3 个提交**：

```
8891ed1 fix(panel): footer 与 children 同状态门控, 非 ok 态不再输出假定案数字
5c1716c test: footer 门控修复的 5 场景实渲染验证截图
2a7bb2b feat(hotspot): 启用港美热点 —— 缓存 TTL + 港美定时 + cron 换源 + history 补 market
```

（之前记的"领先 5 个"是错的 —— 本地 `origin/develop` 一度只读 packed-refs 旧值。以 `git ls-remote` 为准。）
