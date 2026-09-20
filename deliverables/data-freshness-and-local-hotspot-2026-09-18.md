# 数据新鲜度状态栏 + 热点本地数据源

日期: 2026-09-18
分支: `data-freshness`（未合入 develop，等停服窗口）
回应诉求: ①"数据不够实时，应该提示正在拉取哪个市场的哪一天；缺失时让用户选范围拉取"
          ②"热点工作区到现在没看到成品"

---

## 一、数据新鲜度状态栏

### 后端 `app/services/data_freshness.py` + `GET /api/data/freshness`

回答三层：**现在有什么数据 / 在拉什么 / 缺哪一段**。

| 字段 | 含义 |
|---|---|
| `latest_date` / `raw_latest_date` | enriched（看板消费）与原始日K的最新日期 |
| `stale_days` / `tolerance_days` | 落后天数与市场容差（CN 3 / HK 4 / US 3） |
| `coverage_ratio` / `coverage_units` | 最新日完成度 + 单位（A股"交易日"、港美"标的"） |
| `history_days` / `history_insufficient` | 历史深度是否够（CN 120 / 港美 60 天） |
| `status` | `ok` / `stale` / `shallow` / `behind_raw` / `partial` / `empty` |
| `gap` | 建议补拉区间（`from` / `to` / `missing_days`），保证 `from <= to` |
| `active_job` | 当前同步任务：市场 + 后端 message + 百分比（API 层从 job_store 注入） |

**六种状态的判定顺序**（前者优先）：

- `stale` — 日期落后超过容差
- `shallow` — 日期不落后但历史太薄 → 缺的是"更早的全量"，建议补一年
- `behind_raw` — 原始数据已到位但 enriched 没算 → 不是"没拉到"，是"没算"
- `partial` — 日期最新但只有部分标的到位（只有少数标的撑起来的日期）
- `empty` / `ok`

**分区布局差异**：A 股 per-date（单位是交易日，目录名直接读）；港美 per-symbol，
全量扫 2.3w 个 parquet 不现实 → 抽样 30 个取众数，与
`daily_pipeline._h6_latest_distribution` 同口径。结果按
`(data_dir, markets, sample)` 复合 key 做 TTL 缓存 —— 单槽缓存会让不同 data_dir 互相串味。

**真实数据实测输出**：

```
A股   ok       latest=2026-09-17 raw=2026-09-17 stale=1d  cov=1.0    units=267交易日
港股  partial  latest=2026-09-18 raw=2026-09-18 stale=0d  cov=0.467  units=2798标的
美股  stale    latest=2026-09-11 raw=2026-09-11 stale=7d  cov=0.933  units=6071标的
                                                 gap=2026-09-12 ~ 2026-09-18
```

### 前端 `components/DataFreshnessBar.tsx`

`sticky bottom-0`（保留文档流位置：长页面滚动时始终贴底，且不遮挡内容），
挂在 Layout 的 `Outlet` 之后，全页面可见。三块：

1. **同步中** — 市场 + 后端 job message + 百分比；有任务时轮询自动提到 3s（空闲 30s）
2. **各市场 chip** — 最新日期 + 状态 + 覆盖度，hover 显示原始日期与覆盖单位
3. **缺口 CTA** — 非 ok 市场显示"补 X"按钮 → `BackfillDialog`

`BackfillDialog`：港美股可选起始/结束日（建议区间 / 近一年 / 近三年快捷）+ 增量|全量重拉
模式，调 `/api/pipeline/market-daily/run`；A 股没有 market-daily 端点，走
`/api/pipeline/run` 盘后管道（含 enriched 重算）。

---

## 二、热点本地数据源（A 股）

### 根因

A 股热点走 akshare 东财板块接口，依赖 `push2.eastmoney.com`；代理环境不可达，
实测 28.7s 超时后返回空列表 → 热点页长期空白。

**参考项目本来就是本地算**（同花顺概念/行业成分 × 本地行情），零 akshare。
而且港美热点源 `HkUsIndustryHotspotSource` 已经是这条路线 —— A 股只是没跟上。

### 新增 `app/services/hotspot/cn_concept_source.py`

```
topic   = ext_gn_ths.所属概念（或 ext_hy_ths.所属行业，分号分隔多值）
成分股  = 该维度下有本地行情的标的
涨跌幅  = 成分股前复权收盘的等权平均涨幅（与东财板块口径一致）
成交额  = 成分股当日 amount 之和
涨停    = 按板块阈值（主板 10% / 创业板·科创板 20% / 北交所 30%，留 0.2% 容差）
热度    = compute_board_heat_score(涨幅, 排名) + 连板加成
```

**关键：不依赖连板列**，所以即使 enriched 缺 `consecutive_limit_ups` 也能出榜
（缺时显式写进 `missing_fields`，不静默当 0）。概念热点不阻塞在 enriched 重算上。

与 `market_mainline` 的分工：主线按**涨停梯队**打分（需要连板时序），
热点按**当日涨幅 + 成交额**聚合。两者用同一份 ext 概念成分，同样的归属漂移限制。

`select_source("cn")` 已改为返回本地概念源（惰性单例）；akshare 源保留在代码里，
需要时由 `app.state.hotspot_cn_source` 注入 override。

### 真实数据实测（2026-09-17 收盘）

```
provider=cn_local_concept  quality=partial  count=10
  1. 转基因       +4.04%  heat=58.3  members=21
  2. 玉米         +3.74%  heat=55.8  members=34
  3. 粮食         +2.80%  heat=48.8  members=49
  4. 大豆         +2.16%  heat=44.0  members=14
  5. CRO          +2.09%  heat=43.1  members=77

detail(转基因, 21 只):
  920087.BJ 秋乐种业  +15.94%  limitup=False  ← 北交所阈值 30%，正确
  600354.SH 敦煌种业  +10.05%  limitup=True
  600371.SH 万向德农   +9.99%  limitup=True
```

`quality=partial` 是因为 enriched 当前缺连板列 —— 重算完 15 列后会自动转 `available`。

---

## 三、验证

| 项 | 结果 |
|---|---|
| 后端 lint（CI 口径 `ruff check backend/`） | All checks passed |
| 数据新鲜度单测 | 11 passed |
| 概念热点源单测 | 20 passed |
| 热点相关既有测试 | 209 passed（含改写的 `test_select_source_default_cn_*`） |
| 前端类型检查 `tsc --noEmit` | 0 error |
| 真实数据端到端 | 三个市场新鲜度正确；A 股热点不再超时空列表 |

---

## 四、合入步骤（停服窗口）

与 `enriched-cols`（enriched 15 列修复）、`hk-enriched`（港股 catch-up 覆盖）
同一窗口一起上：

1. 停服
2. `git merge enriched-cols` → `git merge hk-enriched` → `git merge data-freshness`
3. 隔离 9 个 12 列分区到 `data/_quarantine/enriched-12col-20260918/`
4. 重启 → `POST /api/pipeline/run` 重算 → 验证 15 列 + `/mainline/recompute` 不再 500
5. 验证底部状态栏显示三市场新鲜度、热点页出数据

---

## 五、已知限制

- 港股 `partial` 是真实状态（同步任务进行中），不是 bug；任务跑完自动转 `ok`
- A 股补数据走盘后管道，不支持自选时间范围（A 股日K拉取没有范围端点）
- 概念成分为当前快照（`ext_gn_ths` 本地自 2026-07 起留存，无历史版本），
  存在归属漂移，与主线同源同限制
- 热点成分股 `turnover_rate` / `volume_ratio` 当前为 None（依赖 enriched 15 列重算）
