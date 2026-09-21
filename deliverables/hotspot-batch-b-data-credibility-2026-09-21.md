# 热点工作区 · 批次 B：让数据可信

**日期**：2026-09-21
**提交**：`055c724`（stage 未判定）、`a141193`（覆盖率标注）、`8f1cfe1`（QA 守卫测试）、`1f9c5fc`（阶段列隐藏）、`05bb806`（批次 A 报告补入）
**状态**：✅ 已推送 origin/develop（`948a393..05bb806`）· ✅ ruff 全绿 · ✅ 2347 passed / 2 failed（2 条为既有环境故障）
**QA 路由结论**：**NoOne（无源码 Bug）**

---

## 一、最重要的发现：问题比原计划记的更广

原计划写的是「港美 `stage` 恒为初次异动」。逐行核实后发现 —— **这是 A股/港股/美股全市场的问题**。

`trend_score` / `persistence_score` / `cooling_score` 三个输入**在全仓库从未被任何源真正计算过**：

| 位置 | 传值 | 性质 |
|---|---|---|
| `akshare_source.py:299` | `0` | 伪造 |
| `source.py:304` | `0` | 伪造 |
| `hk_us_source.py:658` | `None` | 语义正确 |
| `cn_concept_source.py:205` | 不传 | 默认 None |

而 `scoring.py:111` 用 `safe_float(x) or 0.0` 把 `None` 兜成 `0` → 所有分支不成立 → 落到 `return "初次异动"`。

**本质**：把「没有趋势数据」渲染成了「数据判定它处于初次异动阶段」。同时违反纪律①（不可用不计入分母）和纪律③（fail-closed）。

## 二、B1：stage 未判定 → 不再冒充「初次异动」

- 新增 `observed_float()`：与 `safe_float` 严格区分，`None`/NaN/不可解析一律保留 `None`
- `classify_stage` 三维度**全 None** → fail-closed 返回 `None`
- **保留 None vs 0 的语义差别**：显式传真实 `0` 仍按原逻辑判定 —— 「观测到趋势为 0」是结论，不是缺失
- `HotspotSummary.stage` 类型 `str` → `str | None`
- 两处伪造（`akshare_source.py:299`、`source.py:304`）改为传 `None`

### 实现者自己发现、我规格里漏掉的一处
`storage.py` 的写盘侧 `_summary_to_dict` 与读回侧 `_row_to_summary` 都有 `or "初次异动"` ——
**即使 `classify_stage` 修好了，快照落盘再读回又会把 null 变回假徽标**。
> 教训：**改字段语义必须 grep 序列化/反序列化两侧**，不能只看计算处。

## 三、B2：港股 42% 覆盖率显式标注（幸存者偏差）

`_load_latest_rows` 按「全市场 `max(date)`」过滤，停在更早日期的标的被**静默丢弃**，不报错也不标 missing。

- 返回值从裸 tuple 改 `LatestRows` dataclass（不破坏调用方）+ 新增 `_build_coverage`
- 分母取 `instruments/{market}_instruments.parquet`，**读不到就返回 None** —— 不可用不计入分母，不拿命中数冒充分母
- `stale_symbols` 只统计 universe 内标的（universe 外脏数据是另一个问题，不混进口径）
- 一路冒到 `HotspotResults.sample_coverage` → API payload → 前端
- **不动 `quality_status` 判定口径**（改降级逻辑会牵动前端已有展示，另开一批）

### 实测数字（QA 用「直扫 parquet、不经被测代码」独立复算，完全一致）

| 市场 | covered / universe | ratio | stale 总数 | stale 分布峰值档 |
|---|---|---|---|---|
| 港股 | 1187 / 2816 | **42.15%** | 1611 | 973 只停在 2026-09-03（次档 158@09-02、77@09-01）|
| 美股 | 5915 / 6071 | 97.43% | 156 | 78@09-17、19@09-16、15@09-15 |

前端提示文案（实测输出）：
> 样本覆盖 1187/2816（42.1%）:港股本期只统计了 2026-09-18 当日有更新的标的;另有 1611 只未更新到该日、未计入本期(最多的一档 973 只停在 2026-09-03)。这是部分样本的热度榜,不代表全市场强弱。

覆盖率 ≥ 0.95 时静默（美股不刷存在感）；`ratio=0.9499` 显示、`0.95` 不显示。

### 🔴 口径纠正（我记错了很久）
**2798 和 2816 是两个不同的量，不能混用**：
- **2816** = `hk_instruments.parquet` 的 universe 总数（行数 = unique symbol = 2816，全以 `.HK` 结尾）→ **覆盖率分母用这个**
- **2798** = enriched 里有过历史行的 HK 标的数
- 差 18 只 = universe 里从未产生过 enriched 行的标的

另外 **973 只是 stale 分布的峰值档，不是 stale 总数（总数 1611）**。

## 四、阶段列处理（用户拍板：整列隐藏）

三个市场的源都不传趋势维度 → 修完后 stage 列**每一行都显示灰色「未判定」**，信息量为零、纯视觉噪音。

`1f9c5fc` 在 `HotspotList.tsx`：
- 两个 grid 模板提为常量 `GRID_WITH_STAGE`(9 列) / `GRID_WITHOUT_STAGE`(8 列)，避免只改一处造成表头/数据行错位
- `items.length > 0 && items.every(item => !item.stage)` → 隐藏整列（**表头与单元格同由 `showStageColumn` 控制、共用 `gridCols`**）
- 空列表不隐藏（避免 loading 态列数跳变）；**部分有值时保留整列**，null 的行仍显示「未判定」
- `HotspotDetailDrawer.tsx` 的守卫不动 —— 详情是单条上下文，「未判定」有信息量

**为什么没有"真去算趋势维度"**：`hotspot/history/topics.jsonl` 里 `market='cn'` 只有 **1 个交易日**（384 行），凑不出 ≥2 个观测点；另有 1536 行老数据无 market 字段（按铁律不倒推成 cn）。等 history 攒够后可另议口径 + 回测。

## 五、验证（QA 独立执行，非仅跑实现者跑过的）

| 项 | 证据 |
|---|---|
| null 语义四层一致 | docstring / `models.stage: str\|None` / `api.summary_to_dict` / `api.ts` + `HotspotList.tsx` —— 正反都测（含 NaN、`"N/A"` 不可解析） |
| 显式 0 仍是观测值 | `classify_stage(trend=0, cooling=0, persistence=0, latest=10, obs=0)` → `"初次异动"` ✅ |
| 快照读写回不复活假徽标 | 写→读→再写→再读，以及端到端 `discover_hotspots` 落盘回退读回，均保持 `None` |
| 覆盖率数字 | 另写脚本直扫 parquet 复算，与实现逐项对账一致 |
| 阈值行为 | 后端 CN 源 `sample_coverage is None`（三级断言）；前端 `buildCoverageNotice` 源文提取后在 node 里代入真实数字执行 |
| **变异检验** | 新增 27 条测试放到修复前基线跑 → **14 failed / 10 passed**，证明能抓住 `or "初次异动"` / `or 0.0` 兜底复活，**不是恒真断言** |
| 2 条失败测试与本批无关 | 用 `git worktree add` 在基线 `2a7bb2b` 上复跑（不动主工作树），同样红，堆栈不碰 hotspot 代码 |

## 六、遗留（有意不做）

- `history/topics.jsonl` 里仍存着修复前写入的 `"stage": "初次异动"` 字符串（timeline 恒空，暂无可见影响）—— 历史数据债
- `stage=None` 未加进 `missing_fields`（加了会把 `quality_status` 推导成 partial，本批不改降级口径）
- 美股冬令时 cron（22:30-05:00 北京）未覆盖
- GET 每次无条件 append history 的重复行
- 1536 行无 market 的老 history 需显式迁移脚本

## 七、环境故障（非本批引入，但会挡路）

| 问题 | 现状 | 绕法 |
|---|---|---|
| `backend/.venv/Scripts/python.exe` | `uv trampoline failed to spawn Python child process` | `PYTHONPATH=".venv/Lib/site-packages" "C:/Users/Administrator/AppData/Roaming/uv/python/cpython-3.12.11-windows-x86_64-none/python.exe" -m pytest ...` |
| `npx tsc -b` | 17562 条 `Cannot find module 'react'`，依赖类型解析整体坏掉 | 无。**前端类型检查暂时不能当门禁**。跑 tsc 会改写 `frontend/vite.config.d.ts`，需 `git checkout` 还原 |
| git 推 GitHub | 环境 `HTTPS_PROXY=3413`（WorkBuddy 代理）对 GitHub CONNECT 502 | `git -c https.proxy=http://127.0.0.1:7897 -c http.proxy=http://127.0.0.1:7897 push origin develop` |
| `npm run lint` | 仓库无 eslint 配置 | 无 |
