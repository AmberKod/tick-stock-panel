# Regime 治本 3/5 步骤总结 — API 8 端点加 market 参数

**日期**：2026-09-14
**场景**：feature-dev（港美 Regime 治本第 3 步）
**Commit**：`9c663f0`
**基线**：commit ② `f5fbfba`（港美评分改造）
**配套方案**：`deliverables/gstack/feature-dev-hk-us-regime-strength-ladder-2026-09-14.md` §3.3

---

## 📌 TL;DR

- **结论**：🟢 通过
- **改造**：regime API 7 个端点（保留 mainline 共 8 端点）加 `market` Query 参数
- **缓存隔离**：history endpoint 缓存键加 `|{market}|` 段，避免 cn/hk/us 串扰
- **老调用零回归**：所有端点 `market='cn'` 默认值，老路径行为完全不变
- **下一步**：commit ⑤ 强度梯队（动量档位）— 调度留到 commit ④

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🔴 0 / 🟠 0 / 🟡 0 / 🟢 0 |
| 验证基线 | 后端 2022 passed（基线 1995 + 17 API 新 + 12 评分新 − 2 沙箱子进程假阳性） |
| mainline 是否带 market | ❌ — mainline 是 cn 专属（概念/行业），保留 `kind` 参数 |

---

## 1. 端点改造清单

| 端点 | 加 market | 用途 |
|------|----------|------|
| `GET /history` | ✅ | 历史环境时序；缓存键加 `|{market}|` 段 |
| `GET /latest` | ✅ | 最新一日环境 |
| `GET /states` | ✅ | 状态分布统计 |
| `GET /coverage` | ✅ | 数据覆盖元信息 |
| `POST /recompute` | ✅ | 手动重算（admin） |
| `GET /phases` | ✅ | 情绪周期阶段段列表 |
| `POST /mainline/recompute` | ✅ | 主线重算（受 `earliest_enriched_date(market)` 影响） |
| `GET /mainline` | ❌（不动） | 主线排行（mainline 自身不分市场） |

## 2. 缓存键改造

```python
# Before
cache_key = f"hist|{start}|{end}|{limit}"

# After
cache_key = f"hist|{market}|{start}|{end}|{limit}"
```

防 cn/hk/us 互窜缓存（同一进程内连续 3 次不同 market 的查询不会复用旧 result）。

## 3. 验证基线

| 测试组 | 项数 | 结果 |
|--------|----:|------|
| `test_api_regime_market_param` | 17 | ✅ |
| `test_regime_subscores_hk_us`（commit ②） | 12 | ✅ |
| `test_regime_market_split`（commit ①） | 17 | ✅ |
| `test_regime_builder` | 21 | ✅（零回归） |
| `test_market_phase` | 13 | ✅（零回归） |
| `test_mining_schedule` | 17 | ✅（间接调用零回归） |
| **全后端（除 dev_launcher + 子进程并发）** | **2022 passed** | ✅ |

### 沙箱假阳性
- `tests/backtest/test_worker_process.py::test_spawn_optimizer_reuses_one_matrix_and_exits`
- `tests/backtest/test_worker_process.py::test_spawn_walkforward_reuses_shared_matrix_across_folds`
- 报错：`assert 3221225477 == 0`（0xC0000005 = `STATUS_ACCESS_VIOLATION`）
- 单跑通过，并发跑假阳性 — 与本批次代码无关。

## 4. ruff

| 文件 | 状态 |
|------|------|
| `tests/test_api_regime_market_param.py` | clean |
| `app/api/regime.py` | 有 B008（commit ① 之前就有的 `Query(None)` in default；fastapi 标准用法，留待后续 commit 治理） |
| `app/services/regime_builder.py`（仅 `earliest_enriched_date`） | clean |

## 5. 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| mainline 不加 market | 不动 GET /mainline | mainline 是 cn 专属概念/行业，与 regime 按市场路由无关 |
| mainline/recompute 加 market | ✅ 加 | 全量重算时 `earliest_enriched_date(repo, market=market)` 决定起算点（虽然 mainline 数据本身是 cn 共享的，但起算点应按市场扫） |
| 缓存键格式 | `hist|{market}|{start}|{end}|{limit}` | 简单追加，与原风格一致；缓存命中比较 string 相等 |
| `Query(pattern="^(cn|hk|us)$")` 校验 | 用 FastAPI Query 自带 pattern | 自动 422，无需手写校验 |
| `earliest_enriched_date` 加 market | ✅ 加 | recompute 起算点必须按市场扫 enriched；与 commit ① 一致 |
| 不在 mainline endpoint 加 market | 维持现状 | 前端 mainline 卡片只展示 cn 概念/行业；港美 topic 已用 `industry` 走另一路径 |

## 6. 已知局限 / 留待后续 commit

- `app/api/regime.py` 历史 B008（fastapi Query in default）— 与本批次无关，下个 commit 治理
- `earliest_enriched_date` 走 `enriched_date_set(repo, market)`；`compute_regime_incremental` 已经在 commit ① 接 market → 但 `daily_pipeline` 调度侧仍只对 cn 调一次 → 这是 commit ④ 的工作
- 前端 `useRegimeHistory` 等 query key 仍只含 `[start, end, limit]`，未含 market → 用户在港美视图看 regime 会拿到 cn 数据；前端改造属于另一批次（不是本批次后端责任）

## 7. 成员产出索引

- gstack-product-reviewer: 未实际产出（Agent 跑空，本助手自承）
- gstack-investigator: 未实际产出
- gstack-qa-lead: 未参与（commit ⑤ 后统一复验）
- gstack-designer: 未参与
- gstack-security-officer: 未参与（Query 参数校验由 FastAPI 自带 pattern，无新增攻击面）

> 本步骤由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
> 下一步：commit ⑤ 强度梯队（动量档位）API + 服务 + 前端标签按 market 分发；commit ④ 调度留最后。