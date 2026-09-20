# 批次 1 运行时验收报告（2026-09-20）

> 批次 1（跨市场面板）此前**零运行时验证** —— 开发全程 3011/3018 未监听，只有 tsc + 单测。
> 本报告是服务起来后的第一次实跑记录。

## 0. 环境事实（先纠正两条会误导人的旧信息）

| 项 | 旧记忆 | 实际情况 |
|---|---|---|
| 后端端口 | 3018 | **3020**。`vite.config.ts:7` 用 `process.env.BACKEND_PORT \|\| '3018'`，dev.py 起服务时注入真实端口 |
| 健康检查 | `/api/health` | **`/health`**。`/api/health` 会落到静态文件兜底、返回 index.html，curl 看到 200 是假象 |
| 数据目录 | `backend/data/` | **项目根 `data/`**（`config.py:45`，非 frozen 时 `_PROJECT_ROOT / "data"`）。`backend/data/` 只是残留 |

探活必须加 `--noproxy '*'` —— 本机 `HTTPS_PROXY=127.0.0.1:7897` 会把 127.0.0.1 的请求也拦成 502。

## 1. 新端点实测

### `GET /api/overview/posture` ✅

HTTP 200，4.4 KB 真实数据，60s 缓存生效（首次与二次均 0.004s，命中缓存）。

| 市场 | posture | counted | unavailable_dims | 关键证据 |
|---|---|---|---|---|
| A 股 | **进攻** | 4 | 无 | regime 强势(80) / 广度 75.9% / 行业 半导体 +4.31% |
| 港股 | **均衡** | **3** | `hotspots` | regime 震荡(48) / 广度 55.3% / 行业 半导体 **+6.08%** |
| 美股 | **均衡** | **3** | `hotspots` | regime 震荡(51) / 广度 54.3% / 行业 Technology +0.85% |

**两条纪律在真实数据上得到印证：**

1. **不可用不计入分母** —— 港股/美股 hotspots 无快照 → `counted=3`（不是 4），判定 `balanced`。
   若把该维度当 0/防守，港美股会被永久误判成防守。这就是我们最担心的那类错误。
2. **港股 industry 不是缺口** —— 港美股 industry **都投了票**（半导体 +6.08% / Technology +0.85%），
   实测坐实了 09-20 对 PRD §1.4 与 `_sector_rank` docstring 的更正（sector 覆盖 2810/2816，是 09-19 批量分页修好的）。

参数校验：`markets=jp` → **400**（白名单生效）。

### `POST /api/news/batch-stock` ✅ 且撞上了 fail-closed 的靶心场景

```jsonc
{ "ok": true,                    // ← 恒为 true，即使 provider 全挂
  "requested": 2, "processed": 2, "error_count": 2, "elapsed_s": 0.0,
  "results": { "600519.SH": { "success": false, "error": "anspire: 未配置 Key", ... } } }
```

Anspire Key 未配时：`ok` 仍 true、`error_count === processed`、`elapsed 0.0s`（根本没打 provider）。
前端 `attributionHealth` 正是靠 `processed>0 && error_count===processed` 判 `all-fail` → Panel 走 `unavailable`。
**这不是"0 条命中"，是"查不到"** —— 两条路径在本次实跑中都被触发到。

## 2. 端到端回归（`/markets` 页消费的全部端点）

| 端点 | 结果 |
|---|---|
| `/api/overview/market` | 200 · 13.7 KB |
| `/api/hk/overview` | 200 · 8.7 KB |
| `/api/us/overview` | 200 · 11.7 KB |
| `/api/overview/posture` | 200 · 4.4 KB |
| `/api/regime/latest?market=hk` | 200 |
| `/api/regime/latest?market=us` | 200 |
| `/api/abnormal/hk-us/overview?market=HK` | 200 · 37 KB |
| `/api/abnormal/hk-us/overview?market=US` | 200 · 75 KB |
| `/api/alerts` | 200 · `{"alerts":[],"total":0}` |
| `/api/news/status` | 200 · `configured_any: false` |

## 3. 前端运行时编译冒烟 ✅

Vite dev server 按需转译 = 比 tsc 更接近实跑的校验（tsc 只做类型，Vite 会真编译）。
直接请求源模块，全部 200 且产物是编译后 JS（非报错页）：

```
200  95557B  src/pages/Markets.tsx
200 186978B  src/pages/AbnormalMoves.tsx
200 280664B  src/pages/Monitor.tsx
200    909B  src/components/panel/index.ts
200   8234B  src/lib/refreshTiers.ts
```

`/markets` SPA 路由 → 200；前端 `/api` 代理到后端 → 通（`BACKEND_PORT` 注入正确）。

## 4. 数据现场（迁移已在生产执行）

```
根 data/hotspot/topics.parquet   → 已删除（迁移完成）
根 data/hotspot/cn/topics.parquet → 35165 B，2026-09-20 11:30 重建
根 data/hotspot/hk|us/            → 不存在
```

hk/us 确实没有热点快照，所以它们的"不可用"是**真实的**，不是误判。
（旧的不分片文件里只剩 A 股行 —— 正是那个覆盖 bug 的现场证据。）

## 5. ⚠️ 三项数据陈旧，会影响观感但不是代码问题

| 项 | 现状 | 影响 |
|---|---|---|
| posture `as_of` | **2026-09-18**（今 09-20） | 定案基于 2 天前数据 |
| 美股 regime `as_of` | **2026-09-11**（9 天前） | 美股"市场环境"这一票是 9 天前的判断 |
| `alerts` | 空数组 | 面板 3 告警汇聚**没有数据可汇聚**，显示空态（不是不可用，也不是故障） |

想看到饱满效果，需先跑一轮数据同步 + 配好 Anspire Key + 攒出告警。

## 6. 浏览器视觉验收（14:59–15:05 补做）✅

用 playwright-core + Chrome for Testing 153 headless 实渲染（配图 `batch1-markets-screenshot.png` / `batch1-abnormal-screenshot.png`）：

**`/markets` 跨市场总览 —— 通过**

- 三张态势卡视觉正确：A 股红「进攻」实线框、港美蓝「均衡」；票 chip 四态分色
- **「不可用维度 · 已不计入分母」橙色虚线块真实渲染且非常显眼**，附铁律文案
  「这些维度既不算进攻也不算防守，更不会按『中性/0』参与计票 —— 分母只数有效票」；
  港股卡「热点阶段·不可用」橙色虚线 chip 与中性实心灰 chip 视觉明确区分
- 三市场关键量全有数据：A股（上证 3911.87 +0.94%、涨 4138/跌 1151、2.07 万亿、情绪 88）、
  港股（恒生 +0.59%、71）、美股（道琼斯 +0.37%、68）；广度条、档位 chip（T0 实时 / T2 慢快照 / T3 日级）齐
- 页脚数据状态条诚实标注：美股「落后 9 天 · 补美股」
- **console 零错误、零页面异常、零失败请求**（仅 1 条 React Router v7 future flag 无害警告）

**`/abnormal` 异动归因 —— 门控行为正确**

- 监控开关未开时显示「监控未开启」引导卡，归因区块不出现 —— `enabled` 门控生效，
  没有偷发新闻请求。面板 2 的完整 UI 需开启监控后再看（配 Key 前应显示整块"不可用"）。

**验收过程排掉的两个环境坑（记录备查）**：
1. Chrome 沙箱在本机受限环境起不了 network service（`Network service crashed`）→ 必须 `--no-sandbox`
2. playwright 会读进程代理环境变量，回环请求被 `127.0.0.1:7897` 代理吃掉 → 必须 `--no-proxy-server`；
   另外 headless `--screenshot` 的裸模式在 `load` 事件即截图，SPA 那时 React 未挂载，会得到白图 ——
   必须用 CDP 等 `innerText` 增长后再截；`--virtual-time-budget` 与 SSE 长连接互斥（永不退出）

**一个前端小瑕疵（低，不阻塞）**：posture 首算期间（约 20–60s，三市场聚合）态势卡 footer
会显示「分母 0/4 · regime 口径 —」——loading 态不该渲染定案数字，建议 footer 在 loading 时隐藏。

## 结论

后端与前端的**运行时行为与视觉呈现**均已验证通过，三条硬纪律（不可用不入分母 / 无第三套加权打分 / fail-closed）
在真实数据、真实请求路径与真实渲染 DOM 上都成立。批次 1 = **已验收**。

