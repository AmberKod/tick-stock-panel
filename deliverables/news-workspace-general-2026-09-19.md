# 新闻工作区：从"只跟股票相关"扩展为通用热点（2026-09-19）

## 起因

用户指出「热点新闻区不能只是跟股票相关的」，并提示参考项目里新加了 `参考项目/hotNews/worldmonitor-main`
（World Monitor —— 实时全球情报面板：RSS 聚合 + AI 摘要 + 地图 + CII 指数）。

**许可证先行**：World Monitor 是 **AGPLv3**，本仓是 **MIT**。AGPL 有传染性，
**代码一律不复制**，只借鉴其信息架构（分类维度 + 多源聚合 + 源可信度标注的思路），
源清单自己按本机实测重新组织（源 URL 属事实信息）。

## 借鉴了什么

World Monitor 的新闻层本质是一层 **RSS 聚合 + 去重 + 分类面板**，
AI 摘要与地图不属于本轮范围。对我们最有价值的一点：**RSS 不需要 API Key**，
比现在只靠 Anspire 检索（按次计费、且必须配 Key 才有内容）更适合做常驻新闻流。

## 实测：本机可达的源（2026-09-19 两次实测）

### 第一版（不靠谱，已被推翻）

第一次探测得出「TechCrunch / The Verge / Ars / CNBC / Yahoo / OilPrice / Mining /
TheHackerNews / Krebs / FreeBuf / CISA / cnBeta 全部不可达」，据此剔了一大批源。

**这个结论是错的。** 根因是本机 `HTTPS_PROXY=http://127.0.0.1:7897` 当时在抖动
（同会话里出现过 `connect ECONNREFUSED 127.0.0.1:7897`）—— 代理挂了，所有境外请求
直接失败，被误读成"源不可达"。**代理挂 ≠ 源挂**，排查时别急着改源清单。

### 第二版（代理健康时复测，现行清单依据）

| 分类 | 可用（条数） | 实测不可用 |
|---|---|---|
| 国际 | BBC 29、Guardian 45、NPR 10、**Al Jazeera 25**、中新网·国际 30 | DW（ConnectError）、Reuters（HTTP 状态错） |
| 国内 | 中新网·滚动 30、**中新网·社会 30**、**人民网·时政 100** | 新浪国内 404、央视 404 |
| 科技 | 36氪 30、IT之家 60、钛媒体 15、**cnBeta 150**、**TechCrunch 20**、**The Verge 10**、**Ars Technica 20** | 虎嗅（读超时） |
| 财经 | 新浪财经 40、中新网·财经 30、**CNBC 30**、**Yahoo Finance 50** | — |
| 能源 | **OilPrice 15 / 20**、**Mining.com 36**、**Investing·大宗 10** | 中新网能源（**空频道**，见下） |
| 安全 | **The Hacker News 50**、**Krebs 10** | FreeBuf（XML 坏）、CISA（ConnectError） |

关键更正：
- **中新网能源 RSS 是空频道** —— XML 里 `<channel>` 下压根没有 `<item>`，
  不是"更新频率低"，也不是被 48h 时间窗滤掉（去掉时间窗解析仍为 0 条）。已剔除。
- **新浪新闻焦点（ddt.xml）只返 1 条**，等于废源，已换成中新网·社会 + 人民网·时政。
- **新浪科技** RSS 仍挂着 **2018 年**的旧内容 —— 继续不接入（会误导）。
- 「安全·故障」从"无任何可用源"变成 **The Hacker News + Krebs 两个可用源** → 补上这一类。

### 网络前提（运维须知）

本机靠 `HTTPS_PROXY=127.0.0.1:7897` 出海。**关掉代理后**实测：
BBC / Guardian / Al Jazeera / The Hacker News / Yahoo / Investing **全部连接超时**，
国内源（中新网、新浪、36氪、IT之家、钛媒体、cnBeta）不受影响。

所以：**代理挂了 → 境外源整片失败**（前端按 fail-closed 显示 `source_errors`，
不会假装"今天没新闻"），这是网络事实，不是 bug。已写进 `news_feed.py` 模块 docstring。

## 分类设计

| key | 名称 | 通路 | 源数 |
|---|---|---|---|
| world | 国际要闻 | RSS | 5 |
| cn | 国内 | RSS | 3 |
| tech | 科技·AI | RSS | 7 |
| finance | 财经·市场 | RSS | 4 |
| energy | 能源·大宗 | RSS | 3 |
| security | 安全·故障 | RSS | 2 |
| market | 股市·个股 | Anspire 检索（需 Key） | — |

**股票视角降为其中一类**，其余六类是通用热点，且**不需要任何 Key**。

## 实现

| 文件 | 说明 |
|---|---|
| `backend/app/services/news_feed.py` | 新增。分类源目录（Feed 可单独设 timeout，36氪偶发慢给 15s）；8 线程并发抓取；stdlib `xml.etree` 同时解 RSS 2.0 与 Atom（**不引新依赖**，feedparser 未安装）；RFC822/RFC3339 时间解析；标题归一化 + 链接去重（留最新）；时间窗过滤；TTL 300s 缓存；fail-closed（全源挂 → `success=false` + `source_errors`，不返回空列表冒充"没新闻"） |
| `backend/app/api/news.py` | 新增 `GET /api/news/categories`、`GET /api/news/feeds?category=&hours=&limit=&refresh=`；`/cache/invalidate` 一并清 RSS 缓存 |
| `frontend/src/pages/News.tsx` | 重写：7 分类 tab + 时间窗（24/48/72h）+ 刷新；RSS 类渲染新闻卡片流（来源徽标/相对时间/摘要/外链）；`market` 类渲染股票视角（题材 chip / 自选股 / 自由搜索 → NewsPanel） |
| `frontend/src/lib/api.ts`、`queryKeys.ts` | `NewsFeedEntry/NewsCategory/NewsFeedResult` 类型 + `newsFeeds/newsCategories` 方法 + 两个 queryKey |

## 验证

- `ruff check backend/`（CI 同口径）：**All checks passed**
- `tests/test_news_feed.py`：**16 passed**（RSS/Atom 解析、去标签、时间解析三种格式、
  多源去重留最新、时间窗丢弃旧条目、无 pubDate 条目保留、全源失败 → success=false、
  未知分类、缓存复用与失效、7 类清单、每个 RSS 分类至少 1 个源、energy 不含空频道源）
- 端到端（真抓，代理健康时）::

    [world   ] success=True  entries=40 ok_src=5/5  elapsed=2.3s
    [cn      ] success=True  entries=40 ok_src=3/3  elapsed=1.8s
    [tech    ] success=True  entries=40 ok_src=7/7  elapsed=10.7s   ← 36氪慢, 已单独放宽至 15s
    [finance ] success=True  entries=40 ok_src=4/4  elapsed=2.4s
    [energy  ] success=True  entries=38 ok_src=3/3  elapsed=3.4s   ← 修前 0 条
    [security] success=True  entries=20 ok_src=2/2  elapsed=2.5s   ← 新增分类
    [market  ] success=True  entries= 0 ok_src=0/0                 ← 需配 Anspire Key

- `tsc`（沙箱临时 tsconfig）：改动行 **0 error**；残留 TS7006 是库类型在沙箱内退化为
  `any` 的环境性产物（未改动的 `NewsPanel.tsx` 同样出现），完整 `tsc -b` / `vite build`
  需用户在非受限环境执行。

## 环境备注（沙箱专属，不影响本机）

本轮沙箱里 `backend/.venv/Scripts/python.exe` 起不来：
`uv trampoline failed to spawn Python child process` / WinError 448「不受信任的装入点」
—— uv 管理的 python 目录含 junction，沙箱穿不过去（与 pnpm `node_modules` junction 同一类问题）。
绕法：`C:/Users/.../uv/python/cpython-3.12.11.../python.exe` + `PYTHONPATH=<venv>/Lib/site-packages`。
**用户本机不受影响。**

## 后续

- AI 摘要：World Monitor 那层用在我们的 AI provider 上即可，但依赖 Key 且耗时，本轮没做。
- 源可信度标注（World Monitor 有 state-affiliated / propaganda risk 分级）：需要人工维护映射表，暂不做。
- 源清单应定期复查（RSS 源会失效，如新浪科技就是活着的僵尸源）。
  **复查前先确认代理活着** —— 否则会把好源误判成死源（本轮已踩过一次）。
- 「股市·个股」那一类需要配置 Anspire Key 才有内容：设置页 → 数据源 → 新闻搜索源。
