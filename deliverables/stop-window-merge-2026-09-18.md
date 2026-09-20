# 停服窗口合并记录 — 2026-09-18

## 窗口条件

| 检查项 | 结果 |
|---|---|
| 后端 3018 | **已停**（无响应），不会触发 uvicorn reload 停树 |
| 前端 3011 | 在跑（vite HMR，合并后自动热更，无停服风险） |
| 港股同步任务 `bbf502b168` | **succeeded**（09-18 23:46），无在跑任务 |

## 已执行

### 1. 串行合并预演（一次性 worktree `.worktrees/merge-test`）

单分支 `git merge-tree` 全部 exit 0，但 `data-freshness` 与 `news-source` 都改了
`frontend/src/lib/api.ts` / `queryKeys.ts`，两两干净 ≠ 串行干净，所以在临时 worktree
里完整跑了一遍:

```
enriched-cols: OK → hk-enriched: OK → data-freshness: OK → news-source: OK
```

预演结果：`ruff check backend/` All checks passed；路由注册正常；
全量 **2218 passed / 1 failed**（`test_dev_launcher` 为 worktree 无 `.venv` 的环境性失败）。

### 2. 主仓合并（develop）

| 顺序 | 分支 | 内容 | merge commit |
|---|---|---|---|
| 1 | `enriched-cols` (`146484c`) | A 股 enriched 15 列 fail-loud + instruments 精确读取 | `a2cdaf3` |
| 2 | `hk-enriched` (`aa343a8`) | 港股 enriched 相关 | `6b511ee` |
| 3 | `data-freshness` (`f3793d2`) | 数据新鲜度状态栏 + A 股热点本地概念源 | `bd11364` |
| 4 | `news-source` (`a85842a`) | Anspire 搜索服务 + 热点页新闻 tab + 设置页 Key | `8fddd85` |

### 3. 12 列退化分区隔离

扫描 `data/kline_daily_enriched/`：267 个分区 → **258 个 15 列、9 个 12 列**
（2026-09-07 ~ 09-17；09-05/06 是周末无分区）。

9 个退化分区整目录 move 到 `data/_quarantine/enriched-12col-20260918/`，附
`MANIFEST.json`（原因 / 修复 commit / 回滚方式）。剩余分区最新为 **09-04**，全部 15 列。

回滚：把隔离区里的 `date=*` 移回 `data/kline_daily_enriched/` 即可（仅应急，正常应重算覆盖）。

### 4. 合并后主干校验

- `ruff check backend/`：**All checks passed**
- 路由：`/api/news/{search,stock,concept,status}` + `/api/news/cache/invalidate`
  + `/api/data/freshness` + `/api/data/freshness/invalidate` 全部注册
- 临时 worktree 已清理（`.worktrees/merge-test` 已 remove）

## ⬜ 你还需要做的（最后一步）

1. 启动后端（3018）。
2. `POST /api/pipeline/run` —— 重算 09-07 之后的 A 股 enriched 分区。
3. 验收：
   - 新分区列数 = 15（含 `turnover_rate` / `consecutive_limit_ups` / `consecutive_limit_downs`）
   - `POST /api/mainline/recompute` 不再 500
   - 页面底部出现数据新鲜度状态栏（A 股 / 港股 / 美股三片 + 补数据入口）
   - 热点页右上角可切到「新闻」tab；未配 Key 时显示"去配置"入口
   - 设置页 → 数据源 → 底部「新闻搜索源 (Anspire)」粘贴 Key → 校验并保存 → 回热点页看新闻

## 备注

- 新闻源只接了 Anspire 一家；`PROVIDER_CLASSES` / `PROVIDER_FALLBACK_ORDER` 已留插槽，
  加一家 = 新增 provider 子类 + 注册 + 前端 provider 选项。
- 港美 enriched 仍停在 09-03（`adj_factor_hk/` 目录已存在，可继续推进补数）。
