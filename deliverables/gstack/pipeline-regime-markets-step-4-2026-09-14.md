# commit ④ 调度按市场循环 — 实施步骤总结

**日期**: 2026-09-14
**commit**: `6557762` feat(pipeline): regime 按市场循环
**场景**: 港美 Regime + 强度梯队治本最后一步（5/5）

---

## 📌 TL;DR

- daily_pipeline.run_now 的 regime 计算步骤改为按市场循环：cn 永远跑，hk/us 视 universe 是否同步决定是否启用
- 单市场失败软失败，不影响其他市场继续
- 阶段切换推送也按市场路由（cn 默认；hk/us 消息带 `[HK]`/`[US]` 标记）
- 把内联的 regime/mainline 循环抽到独立函数（`_compute_regime_step` / `_compute_mainline_step`），
  便于测试 + 降低 run_now 复杂度
- 全后端 **2040 passed /0 failed**（基线 2032 + 8 项新增）

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🟢 0 / 🟡 0 / 🟢 0 |
| 关键行动项 | 3（已全部完成） |
| 建议负责人 | 梅林（已交付） |

---

## 1. 设计要点

### 1.1 市场路由规则

```python
enabled_markets: list[str] = ["cn"]    # 永久
for mkt, fname in (("hk", "hk_instruments.parquet"),
                    ("us", "us_instruments.parquet")):
    if (data_dir / "instruments" / fname).exists():
        enabled_markets.append(mkt)
```

判定逻辑：是否存在对应 `instruments/{market}_instruments.parquet`——
存在说明 universe 同步过，regime 才有数据可聚合。

### 1.2 软失败隔离

每个市场包独立 try/except：
- 抛出 → `stage_errors.append(f"compute_regime[{mkt}]: {e}")` + `skipped.append(f"regime[{mkt}]")`
- 不影响后续市场
- 管道末尾 `if stage_errors: raise PipelineStageError(...)` 仍然如实反映部分失败

### 1.3 阶段切换推送按市场分组

`_push_phase_change_alert(data_dir, market="cn")`：
- cn 默认无标记：`情绪周期阶段切换: 主升 → 高潮 (2026-09-10)`
- hk/us 加市场前缀：`情绪周期阶段切换: [HK] 主升 → 退潮 (2026-09-10)` / `[US] ...`
- severity：切入 `ebb`（退潮）/ `ice`（冰点）触发 warn；其余 info
- 监控中心 toast + 中心列表可按 `[HK]`/`[US]` 标签过滤

### 1.4 代码治理

抽函数降低 `run_now` 复杂度：
- `_compute_regime_step(*, repo, emit, skipped, stage_errors) -> int`：跨所有启用市场的累计天数
- `_compute_mainline_step(*, repo, emit, skipped, stage_errors) -> int`：mainline 行数

两个函数都用 keyword-only 参数，monkeypatch 友好。

---

## 2. 验证

### 2.1 测试矩阵（8 项新增）

| 测试 | 覆盖点 |
|------|--------|
| `test_pipeline_loops_only_cn_when_no_instruments` | 默认 cn-only |
| `test_pipeline_loops_cn_hk_us_when_instruments_present` | cn→hk→us 顺序循环 |
| `test_pipeline_only_hk_when_only_hk_instruments` | hk-only（跳过 us） |
| `test_pipeline_per_market_soft_failure` | hk 抛异常 → us 仍跑；hk 进 errors + skipped |
| `test_pipeline_logs_per_market` | 每个市场独立日志行 `compute_regime[market]` |
| `test_push_phase_change_alert_default_market_is_cn` | 默认 market='cn' |
| `test_push_phase_change_alert_passes_market` | market='hk' 透传到 `latest_phase_transition` + warn severity |
| `test_push_phase_change_alert_hk_message_has_market_tag` | hk 推送消息含 `[HK]` 标记 |

### 2.2 踩坑记录（重要）

| 坑 | 修复 |
|----|------|
| monkeypatch 打到 `dp_module.regime_builder` 失败 | 改打到 `app.services.regime_builder.*`（真正 import 来源） |
| `push_alerts` 是 `QuoteService` 实例方法，不是模块函数 | mock `_get_app_state()` 返回 fake app_state |
| 函数 keyword-only 参数 `*` 用位置传参失败 | 测试改用 kw=`=` |
| 第一次 edit 删了 run_now 末尾的 `result = {...}` + `return result` | 重新加回来 + 抽 mainline 时**只**改 mainline 部分 |
| `regime_days` 未初始化被引用 | 在 regime 分支前加 `regime_days = 0`，让 `_compute_regime_step` 返回 |
| ruff F821 一堆（universe / new_daily_days 等） | 发现是 mainline 抽函数时把整个 result dict 块误包进 `_compute_mainline_step` 函数体（重复块），删掉重复即可 |
| ruff I001 三个 in-function import 块 | 排序：stdlib → first-party → 自身 |

### 2.3 性能/正确性

- 三个市场顺序循环，单市场 IO < 1 秒（增量时），整体开销可控
- `_compute_regime_step` 仍以 `_push_phase_change_alert` 软失败包裹 → 推送异常不影响数据计算
- `regime_days` 累加跨市场 → result.regime_days 字段反映**所有**市场新算的天数（基线只统计 cn）

---

## 3. 提交链（一图看治本 5 步全貌）

```
bcf1ab1  docs: 抄入上游合并手册 (P0-1 准备)
603d0c9  feat(regime): 持久化分目录 + market 参数兼容 (治本 1/5)
f5fbfba  feat(regime): 港美评分改造 momentum+new_high (治本 2/5)
9c663f0  feat(regime): API 8 端点加 market 参数 (治本 3/5)
dcc84f0  feat(strength_ladder): 港美动量档位梯队 API+服务 (治本 4/5: 强度梯队)
6557762  feat(pipeline): regime 按市场循环 (治本 5/5: 调度闭环)
```

---

## ⚠️ 待完善 / 已知局限

- daily_pipeline.py 仍有 45 项历史 ruff 警告（中文标点、unused noqa、import 排序）—— 与本 commit 无关，留待后续治理
- 强度梯队的 daily_pipeline 自动补算（与 regime 同步）未在本批次实现——目前 strength_ladder 数据是 manual 触发
- regime_days 字段是总市场累加，前端展示时需分市场 drill-down 才清晰
- hk 校准值（动量阈值、subscore 权重）仍是经验数，等真实港美分位数回归后替换

---

## 📚 成员产出索引

- 方案: `deliverables/gstack/feature-dev-hk-us-regime-strength-ladder-2026-09-14.md`
- commit ② 步骤: `deliverables/gstack/regime-hk-us-scoring-step-2-2026-09-14.md`
- commit ③ 步骤: `deliverables/gstack/regime-api-market-param-step-3-2026-09-14.md`
- commit ④ 步骤:（本文档）
- commit ⑤ 步骤: `deliverables/gstack/strength-ladder-step-5-2026-09-14.md`

---

> 港美 Regime + 强度梯队治本批次 5 步全部交付，后续可继续推进 P1/P2 优先级。