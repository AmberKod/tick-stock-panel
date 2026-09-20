# Codex 任务书：热点工作区改造（batch-hotspot）

**日期**：2026-09-15
**分支/工作区**：`E:\ai_codes\ai_personal_panel\tick-stock-panel-codex-hotspot`（分支 `feature/codex-hotspot`，基于 `develop` 的 `f9bc6df`）
**主理人工作区**：`E:\ai_codes\ai_personal_panel\tick-stock-panel`（分支 `develop`）—— **禁止改动**
**设计文档（必读）**：
- `E:\ai_codes\ai_personal_panel\tick-stock-panel\deliverables\gstack\hotspot-workspace-design-2026-09-15.md`（综合方案）
- `E:\ai_codes\ai_personal_panel\tick-stock-panel\deliverables\gstack\hotspot-workspace-layout-design.md`（布局设计，含实现一致性要点第 5 节）
- mockup：`deliverables\gstack\mockups\hotspot-workspace-master-detail.html`（仅任务 D 用）

---

## 0. 环境

| 项 | 值 |
|---|---|
| Python | `<worktree>\backend\.venv\Scripts\python.exe` |
| 跑测试 | `CODEBUDDY_SAFE_DELETE_ENABLED=0 backend/.venv/Scripts/python.exe -m pytest tests/<file> -q --basetemp=.pytest_tmp`（在工作区根目录执行，目录写 `backend/tests/...` 时 cwd 为 `backend`） |
| ruff | `backend/.venv/Scripts/python.exe -m ruff check <你改动的文件>` |
| 前端 | **沙箱内跑不了** `tsc`/`vite`（pnpm 的 node_modules junction 在沙箱不可遍历）。只写代码，类型靠人工对齐；`pnpm tsc -b` 与 `vite build` 由用户在本机验收 |

---

## 1. 背景（自包含，无对话上下文也能开工）

项目是 A股/港股/美股三市场量化终端（FastAPI + React）。「热点工作区」= 后端 `backend/app/services/hotspot/`，前端 `frontend/src/pages/Hotspots.tsx`。

**已查明两个「假功能」**（这是本次改造的起因）：

1. **生产数据从未产出**：`data/hotspot/` 目录不存在。调度窗口是工作日 09:05-15:35 每 30 分钟（`backend/app/jobs/hotspot_sync.py`），但服务只在晚间启动，窗口从未命中 —— 与 market_daily 同一个病根。
2. **五段生命周期是常数**：`backend/app/services/hotspot/akshare_source.py:297-300` 把 `trend_score/persistence_score/cooling_score/observations` 硬编码为 0；`hk_us_source.py:468-473` 首日恒 None、`observations=1`。结果 `scoring.classify_stage()` 对所有主题永远输出「初次异动」。
   但 `storage.append_history()` 每次同步都在往 `history/topics.jsonl` 追加观测点（含 heat/stage/leaders），**基础设施齐全，只是没有任何代码读回来计算 trend/persistence/cooling** —— 即「引擎没接、数据在攒」。激活它**不需要任何新数据源**。

**产品定位（已定，不要偏离）**：不做新闻流，做「盘中主线雷达 + 尾盘决策的板块层输入」。用户是活跃交易者，固定节奏 14:30 筛选 / 14:40 复盘 / 14:50-14:57 执行。

**死区处置决策（已定）**：
- `timeline` → **激活**，语义改为「热度轨迹」（从 history 观测点读，不是事件时间线）
- `route`（产业链传导）→ **删除前端死代码**（无数据源支撑，违背 fail-closed）
- `news_search` / `include_search` → **删除**（不做真新闻源）

---

## 2. 任务清单（按序执行，逐个提交）

### 任务 A：第 0 批 —— 数据链路验证（P0）
目标：证明热点同步在真实环境能产出数据。

1. 确认 akshare 已安装且东财板块接口可用：`stock_board_concept_name_em` / `stock_board_industry_name_em`（概念列表）、`stock_board_concept_cons_em`（成分股）
2. 在盘中时段（09:30-15:00 之间）执行一次手动同步，验证 `data/hotspot/topics.parquet` + `constituents/` + `history/topics.jsonl` + `job_state.json` 全部落盘
3. 若接口不通或依赖缺失 → **不要改业务逻辑去绕**，提交一份诊断报告（哪个接口、什么错误、是否有替代源），交给主理人决策
4. 顺带确认 `settings.data_dir` 指向（应为项目 `data/`，见 `backend/app/config.py` 的 `_user_data_root()`）

验收：同步后 `data/hotspot/job_state.json` 的 `last_status` 为 ok/非 failed，且 `topics.parquet` 行数 > 0。

### 任务 B：生命周期引擎激活（P0，核心）
目标：让 `stage` 列不再是常数，趋势/持续性/降温真实反映历史观测。

1. 新增从 `history/topics.jsonl` 读取某主题最近 N 个交易日观测点的能力（按 `topic` + `canonical_topic` 匹配，注意别名合并）
2. 用观测点计算：`trend_score`（热度变化斜率/加速度）、`persistence_score`（连续出现次数 + 跨交易日存活）、`cooling_score`（降温幅度）、`observations`（历史观测次数）
3. 替换 `akshare_source.py:297-300` 与 `hk_us_source.py:468-473` 的硬编码
4. 首日/新主题仍应合理降级（历史不足 → observations=1、trend/cooling=0，stage=初次异动），不得因缺历史而报错或直接崩
5. **港美与 A 股两条路径都要接**

验收：
- 新增测试覆盖：无历史（首日）、1-2 个观测点、连续 5+ 日（应判定为持续/扩散而非初次异动）、降温场景
- 现有 `backend/tests/test_hotspots_api.py`、`test_hotspots_main_api.py`、`test_hotspot_sync.py` 全过

### 任务 C：前端市场切换接全局 context（P0，0.5h）
现状：`frontend/src/pages/Hotspots.tsx:42` 用局部 `useState<Market>('cn')` + 局部 MarketTab，没接全局 `frontend/src/lib/marketContext.tsx`（全站只有 `pages/Regime.tsx:144` 接了，是全站唯一未收敛的页面）。
改造：换用 `useMarket()`，页头样式对齐 Regime（pill 按钮组），去掉局部 state。

### 任务 D：布局变体 B 主从工作台（**待用户拍板，先不要开工**）
用户尚未在「变体 A 保守增强 / 变体 B 主从工作台」之间做决定，且详情区是否保留新闻模块占位也未定。
等主理人通知后再开工。开工前先读 `hotspot-workspace-layout-design.md` 全文（尤其第 5 节 6 条实现一致性要点）和 mockup HTML。

---

## 3. 文件白名单 / 禁区

**可改**：
- `backend/app/services/hotspot/**`
- `backend/app/jobs/hotspot_sync.py`
- `backend/app/api/hotspots.py`
- `backend/tests/test_hotspots*.py`、`backend/tests/test_hotspot_sync.py`、新增 `backend/tests/test_hotspot_lifecycle.py` 等
- `frontend/src/pages/Hotspots.tsx`、`frontend/src/components/hotspot/**`、`frontend/src/lib/api.ts`（仅热点相关段落）、`frontend/src/lib/queryKeys.ts`（同上）

**禁区（主理人正在这些区域工作，改动会被覆盖或冲突）**：
- `backend/app/jobs/daily_pipeline.py`、`backend/app/main.py`（除注册路由外）
- `backend/app/services/hk_data_adapter.py`、`backend/app/tickflow/**`
- `backend/app/data_providers/**`
- `data/**`（不要手动编辑或批量删除任何数据文件）
- 前端 `pages/Regime.tsx` 及其他页面、`Layout.tsx`
- 不要新增依赖、不要改 CI、不要动 `pyproject.toml`

---

## 4. 项目铁律（都是踩过的坑，务必遵守）

1. **测试必须显式注入 Stub**：`select_source("hk"/"us")` 与 `AkshareHotspotSource` 惰性单例默认指向真实 `settings.data_dir`，不注入就会读写真实数据目录。
2. **`polars.is_empty` 是方法不是属性**：判空用 `df.height == 0`，`getattr(df, "is_empty", False)` 恒真。
3. **`datetime` 是 `date` 的子类**：日期归一化必须先判 `datetime` 再判 `date`，否则 `d <= date.today()` 抛 TypeError。
4. **跨分区 `scan_parquet` schema 必须一致**：Int64/Float64、ms/μs datetime、Null/String、date=Date vs Datetime 任一不同都会让整目录扫描失败。
5. **ruff 只对自己的增量负责**：老文件有历史债（`hk_data_adapter.py` 等），不要顺手 `--fix` 整个文件，只保证你新增的行干净。
6. **不要加 `# noqa: BLE001` 之类的死注释**：项目没启用该规则，会触发 RUF100。
7. **不启动服务、不跑长时网络任务**：数据同步验证（任务 A）如超过数分钟，产出诊断报告即可，不要挂机等待；不要启动 uvicorn 或后台进程。
8. **提交规范**：`<type>(<scope>): <主题>`，正文用中文说明「背景 / 修改 / 测试」，每个任务一个 commit，分批留痕。

---

## 5. 交付时回报

用 SendMessage 或通过用户转交主理人，包含：
1. 每个任务的 commit hash 与一句话说明
2. 任务 A 的实测结论（接口是否可用、是否产出数据、job_state 状态快照）
3. 测试结果（新增用例数 + 相关文件通过数）
4. 遇到的意外与未决问题（不要自己拍板扩大范围，交给主理人）
