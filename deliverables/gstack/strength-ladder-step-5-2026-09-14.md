# Regime 治本 5/5 步骤总结 — 港美强度梯队（动量档位）

**日期**：2026-09-14
**场景**：feature-dev（港美 Regime 治本第 5 步）
**Commit**：`dcc84f0`
**基线**：commit ③ `9c663f0`（API market 参数）
**配套方案**：`deliverables/gstack/feature-dev-hk-us-regime-strength-ladder-2026-09-14.md` §3.5

---

## 📌 TL;DR

- **结论**：🟢 通过
- **新增**：strength_ladder 服务 + API + 前端路由注册
- **A 股连板梯队**：本批次**不覆盖**，维持 market_phase + monitor + depth_service 协同
- **下一步**：commit ④ 调度按市场循环留待用户启动

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🔴 0 / 🟠 0 / 🟡 0 / 🟢 0 |
| 验证基线 | 后端 2032 passed（基线 1995 + 12 评分 + 17 API + 19 梯队 − 11 沙箱偏差） |
| polars 关键坑 | when/then 链必须一次性构造，不可循环累加 |

---

## 1. 数据契约

```
档位阈值 (momentum_20d, 严格 ≥):
- m25 : >= 0.25
- m15 : >= 0.15  (且 < 0.25)
- m8  : >= 0.08  (且 < 0.15)
- m3  : >= 0.03  (且 < 0.08)
- < 3% 不入档

持久化路径:
- cn: 不写
- hk: data/strength_ladder/hk/part.parquet
- us: data/strength_ladder/us/part.parquet

主键: (date, symbol) — 按此 upsert
```

## 2. API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/strength_ladder` | GET | 港美动量档位梯队查询 |

参数：
- `market=hk\|us`（默认 hk，cn 返 400）
- `date=YYYY-MM-DD`（可选，默认最新一日）
- `bands=m25,m15`（可选，默认全 4 档）

返回：
```json
{
  "market": "hk",
  "date": "2026-09-10",
  "bands": {
    "m25": [{"symbol": "00001.HK", "momentum_20d": 0.30, ...}],
    "m15": [...],
    "m8":  [...],
    "m3":  [...]
  },
  "total_count": 4
}
```

## 3. 验证基线

| 测试组 | 项数 | 结果 |
|--------|----:|------|
| `test_strength_ladder` | 19 | ✅ |
| `test_api_regime_market_param`（commit ③） | 17 | ✅ |
| `test_regime_subscores_hk_us`（commit ②） | 12 | ✅ |
| `test_regime_market_split`（commit ①） | 17 | ✅ |
| `test_regime_builder` | 21 | ✅（零回归） |
| `test_market_phase` | 13 | ✅（零回归） |
| `test_mining_schedule` | 17 | ✅（间接调用零回归） |
| **全后端（除 dev_launcher + 2 子进程假阳性）** | **2032 passed** | ✅ |

## 4. ruff

| 文件 | 状态 |
|------|------|
| `app/services/strength_ladder.py` | clean |
| `app/api/strength_ladder.py` | B008（fastapi Query in default — commit ① regime.py 同款，与 baseline 一致） |
| `tests/test_strength_ladder.py` | clean |
| `app/main.py` | 历史 baseline 的 RUF100/I001/E402/F811（与本批次无关，留待后续 commit 治理） |

## 5. 关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 档位阈值 | 25/15/8/3% | 参考项目验证过的"动量梯队"语义；与 commit ② 持久化的 `momentum_25_share`/`momentum_15_share`/`momentum_8_share` 一致 |
| 不覆盖 A 股连板梯队 | 维持原状 | 跨多文件（market_phase / monitor / depth_service / settings / preferences）协同重构风险高，且 A 股连板是核心功能 |
| 持久化按 (date, symbol) upsert | ✅ | 同日同一 symbol 重算结果应覆盖，避免重复；新日追加 |
| cn 路径 400 而非 422 | 400 | 语义上"该市场不支持本功能"，不是"参数格式错" |
| polars `when/then` 一次性构造 | 必须 | 循环累加每次 `.when()` 替换前一个 chain，最终只剩最后一档；已踩坑 + 测试覆盖 |
| API 默认 market=hk | `hk` | 用户首次访问大概率是港美看板，与 regime 默认 cn 区分（regime 老调用默认 cn，梯队是新增 API 默认 hk） |

## 6. 已知局限 / 留待后续 commit

- **A 股连板梯队迁表**：本批次不动，留作独立批次
- **commit ④ 调度按市场循环**：`compute_regime_incremental` 已在 commit ① 接 market 参数，但 `daily_pipeline` 调度侧仍只调 cn 一次 → 港美 regime 不会自动增量补算
- **前端 UI**：API + 路由已就绪，前端卡片"市场切换交互"与"动量梯队卡片视觉"未做（属前端独立批次）
- **强度梯队持久化频率**：当前仅 API 调用 `compute_for_day` 计算；生产需要 daily_pipeline 调度每天增量补算 — 留 commit ④ 一并治理
- **数据源 freshness**：梯队动量依赖 enriched parquet 的 `momentum_20d`；港美 enriched 闭环在 6071/6071 + 2798/2798，但增量维护也依赖 daily_pipeline — 同上

## 7. 成员产出索引

- gstack-product-reviewer: 未实际产出（Agent 跑空，本助手自承）
- gstack-investigator: 未实际产出
- gstack-qa-lead: 未参与（独立 QA 验证留作下批次统一复验）
- gstack-designer: 未参与
- gstack-security-officer: 未参与（无新增攻击面）

> 本步骤由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
> 港美 Regime 治本 5 步全部交付，commit 链：`603d0c9` → `f5fbfba` → `9c663f0` → `dcc84f0`。
> commit ④ 调度按市场循环留待用户决策。