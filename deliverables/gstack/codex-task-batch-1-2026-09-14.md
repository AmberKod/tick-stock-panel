# Codex 任务包 · 第 1 批（CI 工作流 + Ruff 基线清理）

> 本任务书面向外部协作者（Codex GPT-6），**自包含**，无需其他上下文。
> 仓库：`E:\ai_codes\ai_personal_panel\tick-stock-panel`
> 完成后**不要自行 merge**，提交到独立分支交回 review。

---

## 0. 仓库背景速览（必读）

- **项目**：A 股/港股/美股三市场分析终端。后端 FastAPI + Pydantic v2 + Polars + DuckDB + Parquet（无数据库无 ORM）；前端 React18 + Vite + TS + Tailwind + TanStack Query。
- **后端**：`backend/`，Python 3.12，依赖管理用 **uv**（`uv sync --frozen`），测试 pytest（基线 ~2057 passed）。
- **前端**：`frontend/`，pnpm，类型检查 `pnpm exec tsc -b`，构建 `pnpm build`。
- 本机环境为 Windows，但你应在仓库根目录用 Git Bash 或标准 shell 执行命令。

### 环境硬约束（本机踩过的坑，务必遵守）

1. **测试必须用隔离 basetemp**，否则触发沙箱 safe-delete 护栏：
   ```bash
   cd backend
   uv run pytest tests/ --basetemp=.pytest_tmp_codex -p no:cacheprovider
   ```
   每次 run 前若目录不存在先 `mkdir -p .pytest_tmp_codex`。
2. **禁止启动任何长驻服务**（dev.py / uvicorn / vite dev）：端口 3011/3018 归用户直管。
3. **禁止读写 `data/` 目录下任何文件**（真实行情数据，写坏不可再生）。
4. `docs/` 目录被 .gitignore 忽略（个别文件是 `-f` 强制跟踪的，git add 时注意）。
5. 提交信息用 conventional commits（feat/fix/chore/refactor/test），每个逻辑单元一个 commit。

---

## 任务 A：GitHub Actions CI 工作流

### 背景
仓库当前无 CI。同源兄弟项目（`E:\ai_codes\ai_personal_panel\参考项目\stock\tickflow-stock-panel-main`）有现成 workflow 可参考，但**只参考结构，不照抄内容**（它是 v0.1.88 老版本，我们已领先 65+ commits）。

### 范围
新增 `.github/workflows/ci.yml`（单文件，backend + frontend 两个 job）：

**backend job**（ubuntu-latest）：
- `uv sync --frozen --extra multi-market`
- `uv run ruff check backend/`
- `uv run pytest`（全部 tests/）
- 已知会有少量 Windows 特定/环境耦合测试在 Linux 失败（涉及端口监听、Windows 文件锁、node_modules junction）。处理方式：**用 `pytest.mark.skipif(sys.platform == "win32" 的反向)` 或 marker 精确排除，并在 PR 描述里列出排除清单及理由**。严禁为了让 CI 变绿而修改断言或删除测试。

**frontend job**（ubuntu-latest）：
- pnpm install
- `pnpm exec tsc -b`
- `pnpm build`

### 验收标准
1. push 到分支后 Actions 两个 job 全绿。
2. 被排除的测试逐条列出原因（平台差异/环境依赖），数量 ≤ 10。
3. workflow 里加 concurrency 取消机制（同分支重复 push 只跑最新）。

### 禁令
- 不改任何业务代码与测试逻辑（marker/skipif 的添加除外，且需逐条注释原因）。
- 不引入新依赖到 pyproject/package.json。

---

## 任务 B：Ruff 老文件基线清理

### 背景
全仓 `ruff check` 存在约 45 条历史告警，集中在几个巨型历史文件。目标：清零或收敛到明确白名单。

### 范围（只允许动这些后端文件）
- `backend/app/api/settings.py`
- `backend/app/api/kline.py`
- `backend/app/backtest/strategy.py`
- `backend/app/tickflow/repository.py`
- `backend/app/services/quote_service.py`
- 以及 `ruff check` 输出中**仅属于以上文件**的告警

### 明确禁区（这些文件有告警也不许动，主理人近期要改）
- `backend/app/jobs/daily_pipeline.py`
- `backend/app/services/strength_ladder.py`
- `backend/app/services/regime_builder.py`
- `backend/app/services/market_daily_sync.py`
- `backend/app/services/hk_data_adapter.py`
- 所有测试文件、前端文件

### 规则
1. 只做两类修复：`ruff check --fix` 自动修复；需手工的告警按最小改动修（如未使用 import 删除、f-string 无占位符降级为普通字符串）。
2. **禁止重构**：不改函数签名、不挪函数位置、不改逻辑、不做提取公共函数。
3. B008（FastAPI `Depends()` 在默认参数）属框架惯用法，**加 per-file-ignores 白名单**而不是改代码。
4. 每个文件一个 commit，message 形如 `chore(ruff): clean api/kline.py baseline (N/5)`。
5. 每改完一个文件跑一次该文件相关测试 + 全量 ruff，确认无新增告警。

### 验收标准
1. `uv run ruff check backend/` 输出为 0（或仅剩白名单注明的框架惯用法）。
2. 全量 pytest 基线不降（~2057 passed，沙箱已知 1-2 项 flaky 与本次无关）。
3. `git diff` 审查时无逻辑变更（纯 lint 修复）。

---

## 通用禁令（两任务共享）

- ❌ 禁止 `git push`、`git merge`、`git rebase`——只在你的 feature 分支上 commit。
- ❌ 禁止修改 `.gitignore`、`pyproject.toml` 的依赖段（`[tool.ruff]` 配置段允许按任务 B 规则改）。
- ❌ 禁止动 `data/**`、`logs/**`、`deliverables/**`。
- ❌ 遇到与本任务书冲突的信息，以任务书为准；仍不确定就在 commit message 或 PR 描述里标注「QUESTION:」留给 review 者决策，不要自行猜测大改。

## 交付物

1. 分支 `feature/codex-ci`（任务 A）与 `feature/codex-ruff-baseline`（任务 B），或合并为一个分支 `feature/codex-batch-1` 按任务分 commit。
2. 每个任务的总结：改动文件清单、测试结果数字、遗留问题（QUESTION 列表）。
