# 前端全局 market context + Regime 市场切换 + 强度梯队 UI (P1-B 前端)

**日期**：2026-09-14
**场景**：全流程交付（前端改造）
**Commit**：`7c4c343`
**前置**：`787fe78`（enriched 重算脚本）— 本批次纯前端，后端未动

---

## 📌 TL;DR

- 整体结论：🟢 **通过** —— 类型检查 0 错误，三市场能力已接到 UI
- 后端 `market` 参数（P0 治本产物）此前前端一律硬编码 A 股，本批次全部打通
- 类型检查顺带抓出 3 处连带破坏（QK 常量改函数后调用方未同步），已修
- 阻塞项：0（完整 `tsc -b` / `vite build` 需用户本机跑，沙箱限制）

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟢 Go |
| 严重度分布 | 🔴 0 / 🟠 0 / 🟡 1（港美数据仍停 09-03）/ 🟢 0 |
| 关键行动项 | 3 条 |
| 建议负责人 | Amber（本机 build 验证） |

---

## 1. 为什么做（上下文）

P0 治本 5 步（`603d0c9`→`f5fbfba`→`9c663f0`→`dcc84f0`→`6557762`）让后端 regime / strength_ladder
全面支持 `market` 参数，P1-A（`f6f4a9d`）产出三市场真实数据。但**前端一直没有市场概念**：

- `api.ts` 的 6 个 regime 调用全部无 market 参数
- `strength_ladder` API 前端**完全没接**（grep 零结果）
- 无全局 market context（grep `MarketContext|useMarket` 零结果）

本批次把这三块补齐。

---

## 2. 交付清单

### 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `frontend/src/lib/marketContext.tsx` | 145 | 全局市场上下文 |
| `frontend/src/components/StrengthLadderPanel.tsx` | 180 | 港美强度梯队面板 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `frontend/src/lib/api.ts` | +`MarketCode` 类型 / +`StrengthLadder` 类型族 / 6 个 regime 调用加 market / +`strengthLadder()` |
| `frontend/src/lib/queryKeys.ts` | regime* key 加 market 维度 / 修 `regimeHistory` 漏 start+end 的缓存 bug / +`strengthLadder` |
| `frontend/src/pages/Regime.tsx` | 市场切换器 / 取数透传 / 港美禁用情绪周期 / 嵌强度梯队 |
| `frontend/src/router.tsx` | Layout 外层挂 `MarketProvider` |
| `frontend/src/pages/Data.tsx` | 2 处 QK 调用补 `('cn')` |
| `frontend/src/pages/backtest/MiningWorkbench.tsx` | 1 处 QK 调用补 `('cn')` |

---

## 3. 关键设计决策

### 3.1 `hasLimitUp` 作为能力开关

港美无涨跌停制度，连板梯队 / 封板率 / 情绪周期 / 主线**全部不适用**。与其在各处
散落 `market === 'cn'` 判断，不如在 `MARKETS` 元信息里放一个 `hasLimitUp: boolean`，
消费方统一读 `useMarket().hasLimitUp`。

好处：将来若某市场增加涨跌停（或增加新市场），只改元信息表一处。

### 3.2 「禁用」而非「隐藏」情绪周期 tab

港美下情绪周期 tab 用 `disabled` + tooltip 说明「港美股无涨跌停/概念板块，情绪周期不适用」，
而不是直接不渲染。

理由：隐藏会让用户以为功能不存在；禁用保留了功能可见性，同时明确传达「为何不可用」。

### 3.3 Provider 挂载位置

`MarketProvider` 用 `useLocation`，必须在 Router context 内。挂在 `router.tsx` 的
`<Layout />` 外层——而不是 `main.tsx`——因为 `RouterProvider` 是 data router，
main.tsx 里还没有 Router context。

### 3.4 缓存键必须含 market

三市场共用同一套组件，`queryKey` 不含 market 会导致：切到港股后命中 A 股缓存、显示错误数据。
所有 regime* key 首元素后紧跟 market。

---

## 4. 类型检查抓出的连带破坏（本批次最有价值的发现）

把 `QK.regimeCoverage` / `QK.regimeLatest` 从**常量**改成**函数**后，
TS 在另外两个文件报了 3 处错误——这两个文件我本来不知道它们也用：

| 文件 | 行 | 问题 |
|------|------|------|
| `pages/Data.tsx` | 79 | `queryKey: QK.regimeCoverage`（函数未调用） |
| `pages/Data.tsx` | 275 | `invalidateQueries({ queryKey: QK.regimeCoverage })` |
| `pages/backtest/MiningWorkbench.tsx` | 306 | `queryKey: QK.regimeLatest, queryFn: api.regimeLatest` |

另外 `Regime.tsx` 的 `MainlineFilterPanel` 是独立子组件，我加的 `market` 变量不在其作用域内，
补了 `market: MarketCode` prop 从主组件传入。

**这类"改公共 API 连带破坏"如果没有类型检查，会一路带到运行时才炸。**

---

## 5. 沙箱类型检查方法（可复用）

pnpm 的 `node_modules/<pkg>` 是 junction，沙箱内不可遍历（WinError 448），
`npx tsc` 报 `Cannot find module`。绕过方法：

```bash
# 1. 走 .pnpm 真实路径
node node_modules/.pnpm/typescript@5.9.3/node_modules/typescript/bin/tsc -p tsconfig.check.json --noEmit

# 2. 临时 tsconfig 把类型包映射到 .pnpm 真实路径（注意要指向 @types 包，不是 JS 入口）
#    "react": ["./node_modules/.pnpm/@types+react@18.3.29/node_modules/@types/react"]
#    "react-dom/client": [".../@types/react-dom/client"]

# 3. 检查完删除临时 tsconfig
```

坑：第一次把 `react` 映射到 `.../react/index.js`（JS 入口）导致满屏 TS7016，
必须映射到 `@types` 包的 `.d.ts` 目录。

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 |
|---|------|--------|--------|
| 1 | 本机跑 `pnpm tsc -b` + `pnpm build` 验证完整构建 | Amber | P0 |
| 2 | 本机起服务，切换 A股/港股/美股 看 Regime 页与强度梯队渲染 | Amber | P0 |
| 3 | 决定何时补港美 H6 数据（当前停 09-03，切换后看到的是旧数据） | Amber | P1 |

---

## ⚠️ 已知局限

- 港美 regime 数据停在 **2026-09-03**（H6 层就没拉新数据，见 `787fe78` 排查），
  切换到港股/美股看到的是 09-03 的时序，不是今天的
- 强度梯队同理，数据是 09-01~09-03
- `vite build` / 完整 `tsc -b` 受沙箱 pnpm junction 限制，未能执行

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
