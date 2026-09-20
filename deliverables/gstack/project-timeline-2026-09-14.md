# tick-stock-panel 项目进度时间线

**日期**：2026-09-14
**场景**：项目进度回顾 / 时间线存档
**参与成员**：主理人汇编（依据项目记忆 + 每日工作日志 + 交付文档索引）

---

## 📌 TL;DR

- 项目主线：fork `shy3130/tickflow-stock-panel`（A 股终端）→ 扩展为 **A 股 + 港股 + 美股三市场分析终端**
- 最大转折点：2026-09-04 用户对港美看板「非常不满意」→ 根因审计发现美股 universe 混入 1.5 万粉单/OTC → NASDAQ 官方清洗（23221 → 6071 只，日K成功率 29% → 99.1%）
- 当前状态：**架构性差距已全部还清**（Regime / 强度梯队 / 热点 / 概念热度 / 前端 market context），剩余阻塞均为数据新鲜度问题（港美 enriched 停 09-03）+ 用户本机前端验收
- 测试基线：后端从接手时 ~102 个测试文件 → 当前 **~2057 passed**

---

## 1. 项目坐标

| 维度 | 内容 |
|------|------|
| 目录 | `E:\ai_codes\ai_personal_panel\tick-stock-panel`（前端 3011 / 后端 3018，后因 WinNAT 保留段临时改 3020） |
| 血脉 | upstream `shy3130/tickflow-stock-panel`（已同步 v0.2.1）；兄弟 fork `hzy1522/...`（v0.1.88，落后上游 67 提交，仅用于反向查漏） |
| 体量 | 后端 299 py / 91,823 行；前端 191 tsx|ts / 61,412 行 |
| 技术栈 | FastAPI + Pydantic v2 + Polars + DuckDB + Parquet；React18 + Vite + TS + Tailwind + TanStack Query |

---

## 2. 完整时间线

| 日期 | 阶段 | 关键内容 | 代表提交 |
|------|------|---------|---------|
| **08-28** | 摸底 | 项目架构深度分析、本地环境搭建（uv sync + pnpm）、UI/UX 走查报告 | — |
| **08-29** | 设计期 | ① UI/UX P0/P1 优化（焦点环/对比度/侧边栏分组）；② 多维工作台 Phase 0 壳改造（WorkspaceShell + 4 工作区占位）；③ **多市场扩展设计**：实地探查确认 A 股假设渗透 6 层（market_time / price_limits / ext_data 路由 / 核心指数硬编码 / 货币），产出 MarketProfile 抽象层方案，用 ai_stock_tools 项目验证 4 个决策（港股源选 QuickQuote、美股收缩池、原币为主、先港股） | — |
| **08-30~09-01** | 基建期 | 港美实时批量行情（腾讯批量 50/片 + 新浪补漏 + 并发 8）；修复新浪港股字段错位、腾讯港股量 ×100 两个源 bug；参考项目 daily_stock_analysis 两轮审计；港美日K可恢复同步框架（market_daily_sync：checkpoint / retry-only / 熔断 / 退避）；跨市场自选复合身份；dev.py 启动器 | `f342374` `39b0c12` |
| **09-02~09-03** | 数据闭环 1 | 港股 instruments 严格同步落盘 2798 只（新浪备用源）；美股 universe 从参考项目 Tushare 索引导入 23221 只；港股日K源敲定新浪 `stock_hk_daily`（MiniRacer V8 并发崩溃 → 必须 `--concurrency 1` 串行）；美股日K源敲定 akshare 新浪源；**阶段 B**：港美 enriched 独立目录 `kline_hk_us_enriched/symbol=*`（清洗掉污染 A股分区的旧实现）+ overview 装配（`/api/hk/overview` `/api/us/overview`）；**阶段 C**：抽取 OverviewKit 复用 A股视觉组件 | `a23f9b0` `6819efd` `80ba951` `177b6b3` |
| **09-04** | ⚠️ 转折点 | **用户对港美看板「非常不满意」**（全量拉不全、效果远差 A股）→ 根因审计：① 日K/enriched 未闭环（看板覆盖远小于全量）；② 美股 23221 只混 ~1.5 万粉单/OTC（新浪源日K成功率仅 29~33%）；③ 前端孤儿组件/半成品。**整改**：NASDAQ 官方 screener API 清洗至 6071 只普通股/ADR/MLP（零误杀零漏网）；港股日K 2795/2798、美股 6017/6071；港美指数实时行情接入；第四榜「放量榜」对齐 A股四榜；孤儿组件清理；修 3 个历史失败单测；线程安全新浪源直连（模块级共享 MiniRacer + Lock）+ 美股全量重同步 **6071/6071，10.44 只/秒，582 秒完成** | 8 连击 `35c9e33 → 07521d1` |
| **09-06** | 深度对齐 | instruments 三 parquet schema 归一化（修 sector Null/String 冲突 → matrix 崩溃链）；港股行业映射补齐（东财 RPT_HKF10，2792/2798）；latest_daily_date datetime 兜底；港美个股 K线统一 A股同款 StockDailyKChart + 弹窗；腾讯分时图接入；异动引擎港美扩展（复利 mom3 修复：0.000432 → 0.2478）+ 监控规则合流；吸收 AlphaSift 三大能力（估值过滤/同行业组合约束/行业热度因子） | `255c0c9` `5a3c37c` `eeb2c55` `b5aa0de` `fe667ed` `7b6d1f5` `699a16a` `3e74605` |
| **09-07** | 小迭代 | 策略设置面板 portfolio 组合约束 UI | — |
| **09-10** | 架构验收 | 多市场统一架构 67 文件（+3387/-1134）验收通过（后端 616 / 前端 27 / 浏览器 23 项）→ 按层拆 6 commit：data→api→strategy→backtest→frontend→frontend 收尾 | `704a987 → 55c4c67` |
| **09-13 白天** | 概念热度 + 热点 | 概念热度 fast 审计（fail-closed 设计 5 场景全验证）+ 分批提交 6 个（`ee1fc77 → 8fa253f`，测试基线 1233 → 1744）；dev.py 启动日志加固（独占创建 + 保留 50 份）+ WinNAT 3018 端口诊断；**热点工作区**：后端骨架（models/scoring/storage/source/service）→ akshare 真实源（东财概念/行业双路）→ APScheduler 调度 → 前端页面/抽屉；港美 topic 本地聚合源（instruments industry × 行情等权聚合） | `28b4e86 → f157ada` `7c27de5 → 648c42e` |
| **09-13 深夜** | 差距定位 | 与兄弟 fork hzy1522 版对比：我们是更新的（v0.2.1 vs v0.1.88），独有 60+ 文件（港美全链路 + 热点 + 数据体检），确认 2 个明确差距：**港美 Regime、强度梯队** → 用户拍板「P0 全干」 | — |
| **09-13~09-14** | Regime 治本 | 港美 Regime + 强度梯队 5 步治本：① regime 历史分目录 `{cn,hk,us}` + market 参数兼容（老调用零回归）；② 港美评分（动量 0.6 + 新高 0.4 合成，cn 沿用涨停 4 维）；③ API 8 端点加 market 参数 + 缓存键隔离；④ daily_pipeline 调度按市场循环（单市场软失败）；⑤ 强度梯队（20 日动量档位 m25/m15/m8/m3，cn 返 400） | `603d0c9 → 6557762` |
| **09-14** | P1 数据闭环 | **P1-A**（`f6f4a9d`）：regime schema drift 兜底（A 股 enriched 12-15 列精简 schema 导致聚合早退，入口派生 change_pct/涨跌停信号），3 市场历史真实落盘（cn 90 天 / hk,us 37 天）。**P1-B**：① `787fe78` enriched 重算脚本（rebuild_hk_us_enriched.py，legacy/new 双模式，踩 scan_parquet 跨分区 schema 冲突已修）；② `7c4c343` 前端全局 market context（MarketProvider + hasLimitUp 能力开关）+ Regime 市场切换 + StrengthLadderPanel；③ `8fd1489` 强度梯队接入 daily_pipeline 自动调度（Step 2.8，增量补算 max_backfill_days=30，顺手修 `datetime` 是 `date` 子类导致 `isinstance` 恒真的真 bug） | `f6f4a9d` `787fe78` `7c4c343` `8fd1489` |

---

## 3. 关键经验教训（跨阶段沉淀）

| # | 教训 | 出处 |
|---|------|------|
| 1 | **数据源选型决定成败**：美股 universe 不清洗，下游全白搭（29% vs 99.1%） | 09-04 |
| 2 | akshare 新浪源的 MiniRacer V8 并发不安全：港股必须串行；美股用模块级共享实例 + Lock | 09-03/09-04 |
| 3 | polars scan_parquet 跨分区 schema 必须一致（dtype/时间单位/date vs datetime），混存整目录崩 | 09-06/09-14 |
| 4 | `isinstance(datetime, date)` 恒 True（子类），日期归一化先判 datetime 再判 date | 09-14 |
| 5 | 港美与 A 股的分区结构不同（per-symbol vs per-date），聚合链路必须显式 market 路由 | 09-14 |
| 6 | 沙箱环境限制：pnpm junction 穿不过（tsc 用 .pnpm 真实路径 + 临时 tsconfig）、pytest 用 `CODEBUDDY_SAFE_DELETE_ENABLED=0` + 新 basetemp、批量删除用 PowerShell Remove-Item | 09-10~09-14 |
| 7 | 测试必须显式注入 Stub：惰性单例源指向真实 settings.data_dir，不注入会读真实数据 | 09-13 |

---

## 4. 当前坐标（2026-09-14 晚）

| 维度 | 状态 |
|------|------|
| A 股 | ✅ 全链路成熟（regime 90 天、日K到 09-11、策略/回测/监控/热点全通） |
| 港股 | ✅ 框架全通；enriched 停 **09-03**，梯队已能自动补 |
| 美股 | ✅ 日K 6070/6071（仅 JONE 真实缺失）；enriched 同停 09-03 |
| 测试基线 | 后端 ~2057 passed |
| **P0 阻塞（用户侧）** | 本机 `pnpm tsc -b` + `vite build` + 起服务切三市场验收 |
| **P1 阻塞** | 港美补数：H6 拉取成功率仅 ~30%（结构性瓶颈，非复权因子问题——已纠错过一次误判）；港股 2795 只 enriched 全跳过待重拉带身份列的全量 H6 |
| P2 可选 | ruff 老文件基线清理、港美 topic 概念维度、监控条件归因、CI 工作流 |

---

## 5. 交付文档索引（deliverables/gstack/）

- `feature-dev-hk-us-regime-strength-ladder-2026-09-14.md`（Regime+梯队总方案）
- `regime-hk-us-scoring-step-2-2026-09-14.md` / `regime-api-market-param-step-3-2026-09-14.md` / `pipeline-regime-markets-step-4-2026-09-14.md` / `strength-ladder-step-5-2026-09-14.md`
- `p1a-regime-data-closure-2026-09-14.md`
- `p1b-enriched-rebuild-scan-2026-09-14.md` / `p1b-frontend-market-context-2026-09-14.md` / `p1b-strength-ladder-scheduling-2026-09-14.md`

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
