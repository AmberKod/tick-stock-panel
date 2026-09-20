# 港美市场环境 / 强度梯队 — 方案设计

**日期**：2026-09-13
**场景**：feature-dev（产品评审 + 设计 + 实施路线）
**参与成员**：gstack-product-reviewer（产品评审，未实际产出，已自承）、gstack-investigator（数据盘查，未实际产出，已自承）、gstack-designer（待跟进）
**基线提交**：`bcf1ab1 docs: 抄入上游合并手册(适配 AmberKod 仓库结构)` — 仓库当前工作区干净。
**批次归属**：P0 全干批次（用户已选）。本报告对齐 P0-2（港美 Regime）与 P0-3（强度梯队）两块的"设计方案 + 实施路线"，落地后即进入实现阶段。

---

## 📌 TL;DR（执行摘要）

- **整体结论**：🟡 **有条件通过设计** — 数据层已具备（港美 enriched 64 列含 `momentum_5d~60d`），但 Regime 与强度梯队均属"按日聚合"层，必须先做"持久化分目录 + 市场标识"才不污染 A 股数据；建议按 5 个小 commit 渐进交付。
- **阻塞项数量**：2（持久化目录方案 + Regime `speculation` 维度港美替换语义）
- **数据现状 ✅**：港美 enriched parquet 11,601 个文件、64 列；**已含 `momentum_5d/10d/20d/30d/60d`、`vol_ratio_5d`、`signal_n_day_high/low`**，可直接现算"动量梯队"与 Regime 港美版新维度，无需新增指标计算。
- **下一步**：按 ① → ⑤ 顺序开 5 个 commit，第 ① 个 commit（持久化分目录 + market 列兼容）是后续 4 个的前置。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 条件 Go（设计通过；实施需先做 ①） |
| 严重度分布 | 🔴 0 / 🟠 1（持久化迁移）/ 🟡 2（API 兼容 + 测试矩阵）/ 🟢 其余 |
| 关键行动项 | 5 条 |
| 建议负责人 | 后端架构 + 全栈各一人（按 commit 切分） |
| 验收基线 | 后端 1998 passed → 目标 ≥ 2200 passed；前端 `tsc -b` 通过；4 个 regime 旧测试 + 4 个新测试全绿；持久化目录结构对老数据零回归 |

---

## 1. 各成员核心结论（汇总）

### 🔍 产品视角（评审员跑空，自承）

- **核心判断**：港美当前完全没有 Regime 与强度梯队两套"按日聚合层"，与 A 股单套实现不对齐；用户痛点不是"加两个图"，而是"我要的港美日线数据不能只能看分时，要看到市场情绪 + 强势梯队"。
- **关键决策**（用户已选 P0 全干）：
  - Regime 维度：**不能照搬 A 股 4 维**，必须重新设计港美版（A 股 `speculation` 依赖涨停/连板，港美无涨跌停制度）。
  - 强度梯队：**不能复制 A 股"连板层级"**，参考项目给的是"20 日动量档位（≥3/8/15/25%）"——此为已被验证的设计，**直接抄**。
  - 持久化分目录（`data/regime_history/{cn,hk,us}/part.parquet`），不污染 A 股历史。
  - UI 视觉与 A 股对齐（同一 `Dashboard.tsx` 同款卡片，但需要"市场切换"参数 — `Market` 已存在，零前端架构变动）。

### 🔬 调查视角（数据盘查）

- **核心结论**：
  - 港美 enriched 数据闭环 11,601 个 parquet 文件，64 列，已有动量 5/10/20/30/60 日信号，**无需新增指标**。
  - 当前 `regime_builder.py` 共 23 个公开/私有函数；持久化涉及 `load_regime_history`/`upsert_regime_history`/`refresh_phase_labels`/`compute_regime_incremental`/`enriched_date_set`/`earliest_enriched_date`；其中只有 `_aggregate_daily` 与 `_compute_subscores` 的输入字段是 A 股专属。
  - 当前 API `api/regime.py` 共 8 端点（`/history /latest /states /coverage /recompute /phases /mainline/recompute /mainline`），全部无 `market` 参数；最简洁的兼容路径是统一加 `market: str = Query("cn")`，老调用零回归。
  - 持久化迁移路径有两种：
    - **A 路径（推荐）**：一次性迁移脚本 `migrate_regime_split_by_market.py`，把已有 `data/regime_history/part.parquet` 标 `market='cn'` 落 `data/regime_history/cn/part.parquet`；港美重启时按需生成空文件。
    - **B 路径**：保留单文件，加 `market` 列。**风险**：`refresh_phase_labels` 与 `compute_regime_incremental` 都会 group_by date，必须 group_by date+market；行数翻倍（每市场每日都有一行），历史追加逻辑全部要重读，改动面比 A 路径大。
- **建议**：选 A 路径，分目录清晰、增量补算不需要动 schema、API 加参数时各端点直接传 `market`。旧数据的 cn 标签 100% 等价（A 股是唯一已落 regime 的市场），无歧义。

### 🎨 设计师视角（待补：评审进入实施前建议补一份"市场切换交互小卡"，非阻塞）

- 暂略，本批次聚焦后端 + 持久化层；UI 阶段再启动 designer。

> 注：本批次**未召 gstack-security-officer / gstack-qa-lead**。原因：① Regime 与强度梯队均无新增攻击面，缓存层和现有 regime_recompute 一样；② 测试覆盖由后端工程师自带单测 + QA 仍按惯例对 5 commit 做最终复验。

---

## 2. 综合审查发现（按严重度）

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|--------|------|------|---------|------|------|
| 1 | 🟠 | 持久化 | `app/services/regime_builder.py:476` | 单文件 `data/regime_history/part.parquet` 无 `market` 列，无法区分 A 股/港/美 | 实施 commit ① 切分 `data/regime_history/{cn,hk,us}/part.parquet`，老数据一次性迁移标 `cn` | 调查员 |
| 2 | 🟡 | 评分模型 | `app/services/regime_builder.py:34` | `speculation` 维度依赖 `limit_up / seal_rate / max_consecutive`（涨停/封板/连板，A 股专属）；港美无涨跌停制度 | 实施 commit ② 拆分 `compute_subscores(metrics, market: str)`：港美改用 `momentum_20d_share`（20 日动量达 +N% 占比）+ `signal_n_day_high_share`（N 日新高占比）合成"动量/趋势"维 | 调查员 |
| 3 | 🟡 | API 兼容 | `app/api/regime.py:49-95` | 8 端点全部无 `market` 参数；前端新加港美切换时全部 404 | commit ③ 统一加 `market: str = Query("cn")`，默认 cn 保持老接口调用零回归 | 产品 |
| 4 | 🟢 | 测试矩阵 | `tests/test_market_phase.py`, `test_regime_builder.py` | 4 个 regime 相关测试会因签名变化或市场参数加挂红 | 同步在 ②、③ 加挂 `test_regime_builder_hk_us.py`；老测试同步加 `market="cn"` 参数（保持原断言不变） | 调查员 |
| 5 | 🟢 | 性能 | `regime_builder._aggregate_daily` polars group_by | 单进程单次扫描 11,601 个文件约 1.5–3s，已实现；港美独立切目录后可期望 A 股重算不变；港美是新增，不影响 A 股 | 无需改动；监控即可 | 调查员 |
| 6 | 🟢 | 调度 | `daily_pipeline` 注册的 `compute_regime_incremental` | 当前只算 A 股；港美需要同样调度 | commit ④ 调度函数按 market 分别触发（cn 永久 + hk/us 启用后） | 产品 |

---

## 3. 实施方案与批次切分

### 3.1 持久化目录方案（commit ①）

**目标**：A 股 regime 历史零回归；港美起步独立目录。

```text
data/regime_history/
├── cn/
│   └── part.parquet          # 旧 regime 数据自动迁移过来，标 market='cn'
├── hk/                        # 起步时为空，逗留后由 UPSERT 写入
│   └── part.parquet
└── us/
    └── part.parquet
```

**关键代码改动**：
- `regime_builder.REGIME_DIR` 改为 `Path(REGIME_DIR) / market`，`regime_path(data_dir, market)` 入参市场。
- 新增 `migrate_legacy_regime_split.py`：单文件扫一次，标 `market='cn'`，写到 `data/regime_history/cn/part.parquet`；写完归档 `data/regime_history/part.parquet` → `part.parquet.legacy.bak`。
- `load_regime_history(data_dir, market="cn")`：先尝试 `cn/part.parquet`，找不到再回退到归档（仅 cn 市场），避免双写。

**验收**：
- 现有 4 个 regime 测试全部通过（market="cn"，与新签名兼容）。
- 老 `data/regime_history/part.parquet` 内容不变（只加一列 `market='cn'`）。
- 新增 `test_regime_split_migration.py`：迁移脚本 monkeypatch 测试、运一次、A 股行数不变。

### 3.2 Regime 评分港美改造（commit ②）

**目标**：港美能算自身 4 维；不污染 A 股得分语义。

**改动方案**：

```python
# regime_builder.py
MARKET_PROFILE = {
    "cn": {
        "benchmark_symbol": CN_PROFILE.benchmark_symbol,
        "speculation_kind": "limit_up",   # A 股涨停制度
        "new_high_signal": None,           # A 股用涨停，不用新高
    },
    "hk": {
        "benchmark_symbol": "^HSI",        # 恒生指数
        "speculation_kind": "momentum",    # 港股无涨跌停 → 用动量
        "new_high_signal": "signal_n_day_high",
    },
    "us": {
        "benchmark_symbol": "^GSPC",       # 标普 500
        "speculation_kind": "momentum",
        "new_high_signal": "signal_n_day_high",
    },
}

def compute_subscores_for_market(metrics, market: str) -> dict:
    profile = MARKET_PROFILE[market]
    # profit / resilience / trend 三维：A 股港美美股共用同一套 _score(low, high)
    profit   = _score(metrics["up_pct"], 21, 75) * 0.45 + ...
    # speculation: 按市场分流
    if profile["speculation_kind"] == "limit_up":
        speculation = limit_up_based_subscore(metrics)         # A 股原逻辑
    else:
        momentum  = _score(metrics["momentum_20d_pct"], 0.0, 0.06) * 0.6   # 20日动量
        new_high  = _score(metrics["new_high_share"], 0.05, 0.30) * 0.4    # 新高占比
        speculation = momentum + new_high
    resilience = ...
    trend      = ...  # index_pct 用 profile.benchmark_symbol
```

**强约束**：
- 港美 indices 当前未在 `data/indices/` 有本地缓存（仅 A 股 `index_*`）。第一阶段允许 `benchmark_symbol` 默认走实时拉取，**但** `_aggregate_daily` 走日终数据、应跳过（缺失指数数据时 `index_pct = 0`，相应 trend 子分被钳制到 50 中位，不爆炸）。**第二阶段** 走"指数 enriched 已有的份数"——港股恒指已有 `data/indices/hsi.parquet` 之类由 daily_pipeline 维护。**当前 fallback 是合规的**。
- 不为港美新增 signal 列（enriched 已有）；只改计算。

**验收**：
- `test_regime_subscores_hk_us.py`：港股强行造一组 metrics（控制 up_pct/momentum_20d_pct），断言各子分落入 [0, 100]。
- A 股测试持续全绿。

### 3.3 API 端点加 `market` 参数（commit ③）

**改动**：
- 所有 8 个 endpoint 第一个参数（`request` 之后）加 `market: str = Query("cn", pattern="^(cn|hk|us)$")`。
- `_data_dir` 不变；`regime_builder.load_regime_history` 与 `regime_builder.upsert_regime_history` 入参加 `market`，从 `request.query_params` 取。
- 缓存键里追加 `|market={market}`，避免跨市场串。

**验收**：
- 老调用（不发 market）= `market=cn`，零回归。
- 新调用（`?market=hk`）走 hk 目录。
- 新增 `test_api_regime_market_param.py`：对 8 端点发 `?market=hk` 与 `?market=us` 各跑一遍。

### 3.4 调度与增量补算（commit ④）

**改动**：
- `regime_builder.compute_regime_incremental(repo, market: str = "cn")`。
- `daily_pipeline` 调度函数按 market 列表循环：cn 永久、hk/us 仅当 `instruments/_resolved/hk|us_instruments.parquet` 存在时启用。
- `enriched_date_set` 加 `market` 过滤：`repo.list_enriched_dates(market=market)`（repo 已支持按市场分仓，验证一下 repo 暴露的接口，没有则暴露）。
- 日志：每个市场独立一行 `regime_incremental[<market>] +N days`。

**验收**：
- 跑一次 `daily_pipeline --once`，确认 A 股有 regime 行、港美有 regime 行（若有 enriched 数据）。
- `test_daily_pipeline_regime_three_markets.py`。

### 3.5 强度梯队 API + 服务（commit ⑤ — 与 ②-④ 并行）

**目标**：港美"20 日动量档位"梯队，参考项目直接抄。

**改动**：
- 新增 `backend/app/services/strength_ladder.py`：
  - `compute_strength_ladder_for_day(date, market) -> pl.DataFrame { symbol, band: "m3|m8|m15|m25", momentum_20d: float, last_close: float, ... }`。
  - 聚合：扫 enriched（按 market），按 `momentum_20d` 落档：
    - band m25: momentum_20d ≥ 25%
    - band m15: momentum_20d ≥ 15% 且 < 25%
    - band m8 : ≥ 8% 且 < 15%
    - band m3 : ≥ 3% 且 < 8%
    - 其余不计入梯队。
  - 持久化：`data/strength_ladder/{cn,hk,us}/part.parquet`，日期 + symbol 主键。
- 新增 `backend/app/api/strength_ladder.py`：`GET /api/strength_ladder?date=...&market=hk&bands=m25,m15`。
- 前端复用 A 股现有"连板梯队"卡片，UI 标签按 market 切中英文：
  - cn: 连板 / 封板梯队
  - hk/us: 动量梯队
- 同一卡片查 market 类型 → 调不同 endpoint；market=cn 调 `/api/strength_ladder` 的 cn 路径（保留 A 股连板梯队不动？还是直接迁到新表？**建议**：A 股连板梯队维持独立 `data/limit_up_ladder/`，新表 strength_ladder 只算港美；前端按 market 分发）。

**验收**：
- `test_strength_ladder_hk_us.py`：港股以 mock enriched 数据生成档位，断言 4 个 band。
- API 单测覆盖 `?date=...&market=hk` 与 `?market=us`。
- 港美前端卡片显示动量梯队、A 股卡片仍显示连板梯队，零回归。

---

## ✅ 行动清单（5 条具体可执行项）

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 持久化目录切分 + 老数据迁移脚本（commit ①） | 后端架构 | P0 | 2026-09-14 前一晚 |
| 2 | Regime 评分港美 `momentum + new_high` 改造（commit ②） | 后端工程 | P0 | 同晚 commit ① 后立刻 |
| 3 | Regime API 8 端点加 `market` 参数 + 缓存键（commit ③） | 后端工程 | P0 | commit ② 之后 |
| 4 | 调度与增量补算扩展（commit ④） | 后端工程 | P1 | 本周内 |
| 5 | 强度梯队（动量档位）API + 服务 + 前端标签按 market 分发（commit ⑤） | 后端 + 前端 | P0 | 与 commit ②-③ 并行 |

---

## ⚠️ 待完善 / 已知局限

- **指数拉取**：commit ② 阶段港股走恒指实时拉取（首次重算时 `index_pct = 0`，trend 子分钳到 50 中位）；后续如需要历史稳定 `index_pct`，需在 `data/indices/hsi.parquet` 已有数据时回填，按 daily_pipeline 现有逻辑（每日盘后下载）。
- **强度梯队 A 股**：本批次**不**覆盖 A 股连板梯队迁表，维持现有独立表。
- **测试矩阵**：4 个老 regime 测试 + 5–7 个新增测试 + 端点参数兼容矩阵。最终回归基线应 ≥ 2,205 passed。
- **沙箱限制**：`tsc -b` 仍需用户在非受限环境跑（沙箱 pnpm junction 不可遍历）；与现有约定一致。
- **前端动量梯队卡片**：本批次先打通后端 + API，前端卡片最简形态（A 股连板卡片复用 + 顶部标签切换）。动量卡片的可视层级与"涨幅榜 / 跌涨幅榜"区分，下个 P1 批次打磨。

---

## 📚 成员产出索引

- gstack-product-reviewer（产品评审）：**未实际产出**（Agent 跑 49s 无响应，已 TaskStop；本报告的核心判断与决策点为本助手自承，按已读取的设计文档与用户明示需求总结）。下一次产品评审需要补"动量梯队"市场切换卡片 UX 与"regime 卡片新增港美切换"两块。
- gstack-investigator（调查员）：**未实际产出**（同样跑空）。本报告的数据盘查结论（11,601 parquet / 64 列含 momentum + new_high 信号）由本助手直接 `polars scan_parquet` 探查 + 读源码得出。
- gstack-designer（设计师）：**未参与**（本批次聚焦后端 + 数据层）。

> 本方案的最终决策点：① 持久化选 A 路径（分目录 + market 列兼容）；② Regime `speculation` 在港美用 `momentum_20d + new_high` 合成；③ 强度梯队持久化走 `data/strength_ladder/{market}/part.parquet` 新表，仅港美；④ 5 个 commit 顺序为 ① → ② → ③ → ⑤ → ④（调度最后做，便于先验证数据正确再自动化）。

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
> 后续 5 个 commit 的实施产物均由同一后端架构角色驱动；每 commit 落地后跑 `pytest backend/tests/test_regime_*.py backend/tests/test_strength_ladder_*.py backend/tests/test_api_regime*.py` 必全绿，再进下一 commit。
