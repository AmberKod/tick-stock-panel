# World Monitor 深度调研报告

> 调研对象：`E:\ai_codes\ai_personal_panel\参考项目\hotNews\worldmonitor-main`
> 调研人：许清楚（产品经理） · 日期：2026-09-19
> 许可证：AGPL-3.0-only（**只读不抄**，本报告的 URL / 概念均为事实信息复述）

---

## TL;DR（约 190 字）

World Monitor 不是"新闻聚合器"，而是一个**多源交叉验证 + 早期预警引擎**：它把 761 个上游主机的异构数据（结构化 API 332 + RSS/feed 461 + 运营状态 30）全部灌进 Redis 只读快照，前端只做渲染，请求时几乎不打上游。

它最打动人的"在新闻见报前标记汇聚点"**不是预测模型**，而是三条极朴素的规则：① 1°×1° 地理网格内 24 小时出现 3 类以上独立事件；② 30 分钟内 3 类以上信源报道同一事件；③ 预测市场先动、新闻后到。

对我们 tick-stock-panel 的真正价值**不在数据源**（地缘政治那一半完全不适合），而在**信号层**：实体注册表 + "异动是否有新闻解释" + 关键词三关加速检测 + 联邦免 Key 的 Google News 查询模板。建议 P0 只做两件小事，其余多为 P2/不做。

---

## A. 项目全景

### A.1 一句话定位（我的复述，非照抄）

> 一个把**彼此无关的公开数据流**按地理网格、实体身份、时间速度三种方式强行对齐，用"多个独立流同时异常"代替"单一权威信源"，从而在官方叙事形成前标出可疑区域的实时仪表板。

它解决的核心问题不是"信息不够"，而是"**信息太多但彼此不说话**"——USGS 的地震、OpenSky 的军机、ACLED 的抗议、GDELT 的新闻在各自的网站上是四条无关信息，在同一张图上同一个 1°×1° 格子里就是一条预警。

### A.2 核心概念（它自己定义的术语）

| 概念 | 定义 | 备注 |
|---|---|---|
| **CII（Country Instability Index）** | 国家不稳定指数 v8，覆盖 31 个 Tier-1 国家，0–100 分。`baselineRisk×0.40 + eventScore×0.60 + boosts`，eventScore 由 Unrest 25% / Conflict 30% / Security 20% / Information 25% 构成，再叠加 10 类 boosts 与 advisory floors，最后 clamp 到 0–100 | 见 C 节对"复合指数"的诚实评价 |
| **Geographic Convergence（地理汇聚点）** | 1°×1° 地理网格，24 小时窗口，**≥3 种不同事件类型**落在同一格 → 生成汇聚告警。`score = min(100, 类型数×25 + min(25, 事件总数×2))` | 用户所说的"汇聚点"主要指这个 |
| **Signal（信号）** | 14 种跨流检测结果，分新闻/市场/基础设施/地缘军事四族 | 详见 C.3 |
| **Feed Digest** | 服务端把一个变体的全部新闻分类**预聚合**成一次响应，客户端不发任何 feed 请求 | 我们已有 FastAPI 缓存，思路可直接借 |
| **Last-Good Digest** | 客户端本地留存的最近一份可用 digest，用于上游全挂时仍能渲染。用 **coverage 而非"响应是否 200"** 决定是否可入留存 | 很工程化、很值得学 |
| **Digest Coverage** | digest 自述的 completeness 词汇表：`complete / partial / stale / unavailable`，由「内容态」与「尝试态」两个身份共同决定 | 防止"响应成功但没数据"被当成健康 |
| **Publisher Family** | 同一新闻编辑部的多个 feed（BBC World / BBC ME / BBC Hindi）归为一个"家族"；所有"N 家独立来源"计数都按家族算，不按 feed 标签算 | 对"题材有几家报道"这类指标极关键 |
| **Catalog Provider / Logical Provider** | 清单里的一个可寻址条目；Logical Provider = 没有自家编辑域名、全靠聚合传输投递的出版方（按出版方名分组而非传输域名） | 归因口径问题 |
| **Source Tier 1–4** | 1=通讯社/官方，2=主流大报，3=垂类专业，4=聚合器/博客。另挂「宣传风险评级」和「国家关联标志」 | 与 `credibilityScore`（0–100 源可信度）区分于 `importanceScore`（新闻价值） |
| **Variant Host** | 同一代码库按 hostname 切出 6 个站点变体：world / tech / finance / commodity / happy / energy | 桌面端用 localStorage 而非 hostname |
| **Bootstrap Hydration** | 首屏两级并发水合（fast 3s / slow 5s 超时），大 payload 走 `ensureHydrated(key)` 面板渲染时才拉 | 见 D 节 |
| **Read Model / Seed** | Railway 常驻 seeder 写 Redis，Edge handler **只读** Redis，不在请求时打上游 | 抗流量放大的核心 |
| **Intelligence Gap** | 显式报告"我们现在看不到什么"（fresh / stale 2h / very_stale 6h / no_data / error / disabled），而不是把空数据渲染成"风平浪静" | 全项目最诚实的设计 |

### A.3 技术栈清单

| 层 | 技术 | 备注 |
|---|---|---|
| 前端 | **Vanilla TypeScript**（无框架）+ Vite | 不是 React/Vue，纯手工 DOM + 事件委托 |
| 地图 | globe.gl + Three.js（3D 球） ／ deck.gl + maplibre-gl（平面） | 双引擎共享一份 layer catalog；PMTiles 自托管底图；supercluster 聚合 |
| 桌面 | Tauri 2（Rust）+ Node.js sidecar | sidecar 做 fetch patch，把浏览器请求重定向到本地 |
| 后端 | Vercel Edge Functions（`api/`）+ `server/worldmonitor/**` 领域 handler | 用 **sebuf + Protocol Buffers** 定义 RPC 契约，生成 TS client / OpenAPI |
| 常驻服务 | **Railway**：AIS relay（WebSocket + 多组 seed 循环）、Macro seed bundle、Resilience seed bundle、Consumer prices（Playwright 容器） | 这是它真正的"后端"，Edge 几乎不计算 |
| 缓存 | Upstash Redis（REST）+ 4 层缓存（L1 内存 / L2 Redis / L3 CDN / L4 service worker） | `cachedFetchJson()` 做 cache-miss 合并，防惊群 |
| 用户/计费 | Convex Cloud（Dodo 支付、用户状态、API key、邮件广播、历史情报向量检索） | 自托管版无此后端 |
| 边缘 | Cloudflare Worker（CORS 预检）+ Cloudflare _zone 侧缓存规则（dashboard 管理，不在仓库里） | |
| AI | 服务端 Ollama / Groq / OpenRouter；浏览器端 Transformers.js（ONNX：MiniLM-L6 embedding / 情感 / 摘要 / NER） | Web Worker + IndexedDB 向量库，号称 Local AI 免 Key |
| 契约/生态 | MCP server（Streamable HTTP）+ 官方 CLI（npm）+ Python/Ruby/Go SDK + Agent Skills 清单 | |
| 部署 | Vercel / Docker（GHCR 多架构）/ Tauri / PWA / Nixpacks | |

### A.4 仓库规模感

| 指标 | 数值 |
|---|---|
| 代码 + 文档文件数 | **4,702** |
| 源码行数（ts/tsx/js/mjs/cjs/py/rs，含 generated） | **≈ 1,277,000** |
| md / mdx 文档行数 | **≈ 135,000** |
| `package.json` npm scripts | **192** |
| 运行时依赖 / 开发依赖 | 48 / 35 |
| 上游数据源主机 | **761**（= 748 provider：结构化 API 332 + feed 461 + 运营状态 30） |
| `server/worldmonitor/` 领域数 | **40**（market / news / intelligence / military / maritime …） |
| `api/` 子目录 | **48** |
| `src/components/` 文件 / Panel 子类 | 208 / **109** |
| 面板文档 `docs/panels/` | 32 篇 |
| 环境变量 | **270**（`.env.example` 达 49 KB） |
| 环境变量中真正"必须"才能启动的 | 仅 4 个（`RELAY_SHARED_SECRET` / `REDIS_PASSWORD` / `REDIS_TOKEN` / `WM_SESSION_SECRET`） |

> **体感判断**：这是一个**文档密度异常高**的项目（13.5 万行 md，几乎每个算法都有 methodology 页 + 反例 + "为什么错"）。代码量大但相当部分是 registry/清单。真正的"算法"体量不大——汇聚检测就是几十行加权求和。

---

## B. 数据源清单（**全报告最有价值的部分**）

### B.0 读法说明

- **免 Key** = 无需注册/无需 token 即可直接 HTTP 拿到数据（可能有匿名限流）。
- **半 Key** = 免费注册即得 token，或匿名可用但配额极低。
- **需 Key** = 必须凭证，且多为付费或强配额。
- 标注 **〔可直接抄 URL〕** 的，是我判断对 tick-stock-panel 有直接增强价值、且技术上我们 FastAPI 侧可直接复用的。
- 所有 URL 均为**事实信息**（一个公开的 endpoint 地址不构成可版权表达），但**任何解析/调度/打分代码必须自己重写**。

### B.1 【最重要】免 Key 的"万能查询层"：Google News RSS 模板

这是 World Monitor 用得最多、也是它能在不申请任何 Key 的情况下铺出 461 个 feed 的根本原因。

| 模板 | URL | Key | 粒度/频率 |
|---|---|---|---|
| 通用查询 | `https://news.google.com/rss/search?q=<URL-encoded query>&hl=en-US&gl=US&ceid=US:en` | **免** | 分钟级；靠 `when:Nd` 控制窗口 |
| 站点限定 | `?q=site:reuters.com+world+when:1d` | **免** | 同上 |
| 中文（locale 版） | `&hl=zh-CN&gl=CN&ceid=CN:zh-Hans` | **免** | 中日韩/小语种必须带 locale，否则结果质量显著下降 |
| 关键词 OR 组 | `?q=(CPI+OR+inflation+OR+GDP+OR+"economic data"+OR+"jobs report")+when:2d` | **免** | 同上 |

**它内部封装的两个函数**（概念，非代码）：
- `gn(q)` → 上述默认 en-US 模板
- `gnLocale(q, hl, gl, ceid)` → 带区域版本

**对我们的价值：P0。** 我们现有的 `backend/app/services/news_feed.py` 已经是 RSS 路线，但很可能只有固定 feed 列表，缺这一层"任意关键词/任意站点/任意时间窗"的按需查询能力。有了它，题材热度榜可以从"扫已有分类"升级为"按题材名动态生成查询"。

> 注意：Google News RSS 的**实际可解析性/稳定性需自行验证**（待确认项，见 F）。

### B.2 市场 / 财经（与我们重叠度最高）

| 名称 | 域名 / URL | Key | 粒度/频率 | 备注 |
|---|---|---|---|---|
| CNBC | `https://www.cnbc.com/id/100003114/device/rss/rss.html` | **免** | 分钟级 | 〔可直接抄 URL〕 |
| Yahoo Finance Top Stories | `https://finance.yahoo.com/rss/topstories` | **免** | 分钟级 | 〔可直接抄 URL〕 |
| Seeking Alpha Market Currents | `https://seekingalpha.com/market_currents.xml` | **免** | 分钟级 | 〔可直接抄 URL〕 |
| Fox Business | `https://moxie.foxbusiness.com/google-publisher/latest.xml` | **免** | 分钟级 | |
| Business Insider | `https://www.businessinsider.com/rss` | **免** | 分钟级 | |
| GlobeNewswire | `https://www.globenewswire.com/RssFeed/subjectcode/22/feedTitle/GlobeNewswire` | **免** | 分钟级 | 财讯通稿，适合做事件源 |
| Business Wire | `https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==` | **免** | 分钟级 | |
| SEC 新闻稿 | `https://www.sec.gov/news/pressreleases.rss` | **免** | 小时级 | 〔可直接抄 URL〕官方源，Tier 1 |
| 美联储 | `https://www.federalreserve.gov/feeds/press_all.xml` | **免** | 事件驱动 | 〔可直接抄 URL〕官方源 |
| 上证所沪港通/融资融券 | `https://query.sse.com.cn/commonSoaQuery.do`、`https://query.sse.com.cn/marketdata/tradedata/queryMargin.do` | **免** | 日级 | **〔重点，见下〕** |
| 深证所 | `https://www.szse.cn/api/report/ShowReport/data`、`/api/report/exchange/onepersistenthour/monthList` | **免** | 日级 | **〔重点，见下〕** |
| 上海航运交易所 CCFI | `https://en.sse.net.cn/indices/ccfinew.jsp` | **免** | 周级 | 集运指数 |
| CoinDesk / Cointelegraph / Decrypt / Chainwire / The Defiant / Bitcoin Magazine / CryptoSlate / Unchained | 各家 `/rss` 或 `/feed` | **免** | 分钟级 | 加密侧，我们若不做可忽略 |
| 沪深交易所公开披露元数据 | `sse.com.cn` / `szse.cn` | **免** | 事件驱动 | 作者明确记录 **HKEX 被其自动化访问条款禁止**（合规红线，值得注意） |
| **Finnhub** | finnhub.io | **半**（免费配额低） | 实时/日级 | 它的**首选**请求时报价源 |
| **Alpha Vantage** | alphavantage.co | **半**（免费强限流） | 日级为主 | 它的**批量 seeder 主力**；也做商品/FX 日线 |
| **FMP（Financial Modeling Prep）** | — | 需（商业） | — | **它被明确排除**：ToS §2.2.1–2.2.2 禁止商业与多用户再分发 |
| **Yahoo Finance** | — | 名义免 | 分钟级 | 它正在**退役** Yahoo（issue #3731 合规整改），仅保留极少数 `yahooOnly` residual |
| Fear & Greed Index | `api.alternative.me` | **免** | 日级 | |
| BTC 链上 | `mempool.space` | **免** | 分钟级 | 哈希率/出块 |
| CoinGecko / CoinPaprika | — | 半 | 分钟级 | |

#### ★ 沪深港通北向成交额 + 融资余额（我认为这是 B 节里对我们价值最高的一条）

- 端点：`query.sse.com.cn`（沪）、`www.szse.cn/api/report/...`（深）
- Key：**免**
- 粒度：日级（北向**成交额** gross turnover + 两融余额）
- 重要事实：**2024-08-16 起，沪深交易所停止公布北向资金净买入（buy/sell split）**，只剩总成交额。作者没有用 turnover 冒充 flow，而是在 payload 里显式声明 net 不可得。
- 作者明确**排除**了 BaoStock / AKShare（Python 库，其 seeder fleet 是 Node ESM）和东方财富（未公开的私有 JSON，无条款）。这两条排除理由本身就很有价值——**如果我们抓东财，条款风险是真实的**。
- 对我们的契合度：**高**。我们是 A 股/港股终端，"北向成交额 + 两融余额"是标准的资金面日频指标，且免 Key。

### B.3 宏观 / 央行 / 统计（免 Key 占多数）

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| FRED（圣路易斯联储） | `fred.stlouisfed.org` | **半**（免费注册得 key） | 日/月/季，视序列 |
| ECB Data Portal | `data-api.ecb.europa.eu` | **免** | 日级（汇率、收益率曲线、ESTR/EURIBOR、金融压力指数） |
| Eurostat | `ec.europa.eu/eurostat` | **免** | 月/季（HICP、GDP、失业） |
| BIS Statistics | `stats.bis.org` | **免** | 季/月（政策利率、REER、credit-to-GDP） |
| World Bank Open Data | `api.worldbank.org` / `data.worldbank.org` | **免** | 年为主 |
| 世界银行 WDI（经 UNESCO UIS 镜像） | `api.worldbank.org` | **免** | 年 |
| ILOSTAT SDMX | `ilo.org` | **免** | 年 |
| UN WPP 2024 | `population.un.org` | **免** | 年（CC BY 3.0 IGO） |
| 俄罗斯央行 | `cbr.ru` | **免** | 日（54 种货币兑 RUB + 关键利率） |
| UN Comtrade | `comtradeapi.un.org` | **半**（匿名可用，key 提配额） | 月/年 |
| IMF PortWatch | `portwatch.imf.org` | **免** | 日（咽喉要道通行量） |
| U.S. BLS | `bls.gov/developers` | **半** | 月 |
| USA Spending | `api.usaspending.gov` | **免** | 日 |
| U.S. EIA | `eia.gov/opendata` | **需**（免费注册） | 周/月 |
| 中国国家统计局 NBS | `stats.gov.cn` | **免** | 月/季 |
| 国家外汇管理局 SAFE | `safe.gov.cn` | **免** | 月 |
| ChinaMoney / CFETS | `chinamoney.com.cn` | **免** | 日（LPR 发布日核验） |
| 人民银行 / 工信部 / 商务部 / 发改委 / 网信办 / 市场监管总局 | `pbc.gov.cn` `miit.gov.cn` `mofcom.gov.cn` `ndrc.gov.cn` `cac.gov.cn` `samr.gov.cn` | **免** | 事件驱动（政策文件） |

### B.4 地缘政治 / 冲突

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| ACLED | `acleddata.com` | **需**（OAuth email+password 或 token） | 30 天窗口，10 分钟 TTL |
| UCDP（乌普萨拉冲突数据） | `ucdp.uu.se` | **半**（`UCDP_ACCESS_TOKEN`，注册可得） | 年 + 月度候选版（滞后约 1 月）；**年度版滞后约 7 个月** |
| GDELT | `gdeltproject.org` | **免** | 7 天地理事件流；事件提及数 ≥5 才收，≥30 标 `validated` |
| UN OCHA HAPI | — | **免** | 月（难民/ asylum/ IDP） |
| 美/澳/英旅行警示 + 使馆告警 | state.gov / dfat / fcdo / `ae.usembassy.gov` | **免**（RSS/Atom） | 小时级 seed |
| OREF 以色列防空警报 | `oref.org.il` | **免但需绕 WAF** | 5 分钟轮询；**作者用 curl 而非 Node fetch（JA3 指纹被拦）+ 以色列出口住宅代理** |
| LiveUAMap | `liveuamap.com` | — | 事件驱动（伊朗战区） |
| 台湾 MND / 日本统合幕僚监部 | `mnd.gov.tw` / `mod.go.jp/js` | **免** | 日（官方主张，作为 publisher claim 存，非客观轨迹） |
| SIPRI 军贸 | `armstransfers.sipri.org` | **免但受限** | 年；**仅发布派生份额，不镜像全库**（SIPRI 商业使用需授权） |
| **Polymarket** | Gamma API | **免（浏览器直连）/ 服务端需绕 JA3** | 5 分钟缓存 | 见下方"4 层抓取策略" |

> Polymarket 的 4 层抓取链很值得记一笔：① bootstrap 水合 → ② sebuf RPC 读 Redis → ③ **浏览器直连**（浏览器 TLS 指纹能过 Cloudflare）→ ④ Tauri Rust `reqwest`（TLS 指纹与 Node 不同）。它甚至提到 Vercel edge 有时能过。这说明**服务端抓现代 API 的普遍困境是 TLS 指纹而非 Key**。

### B.5 网络信号 / 网络基础设施（免 Key 为主）

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| Cloudflare Radar | `radar.cloudflare.com` | **需**（`CLOUDFLARE_API_TOKEN`） | 5 分钟（互联网中断/流量异常） |
| Feodo Tracker（abuse.ch） | — | **免** | 实时（C2 服务器） |
| URLhaus（abuse.ch） | — | **半**（`URLHAUS_AUTH_KEY`） | 实时（恶意软件 URL） |
| C2IntelFeeds | — | **免** | 实时 |
| AlienVault OTX | — | **半**（`OTX_API_KEY`） | 实时 |
| AbuseIPDB | `abuseipdb.com` | **需** | 实时 |
| Ransomware.live | — | **免** | 实时 |
| IP 地理富化 | ipinfo.io → freeipapi.com 兜底 | **免** | 24h 缓存，16 并发 / 250 IP 上限 |
| Submarine Cable Map | `submarinecablemap.com` | **免** | 静态（86 条海缆） |

### B.6 海运 / 航空

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| AISStream | `aisstream.io` | **需**（`AISSTREAM_API_KEY`） | 实时 WebSocket；**陆地接收，欧洲/大西洋覆盖好，中东/亚洲/远洋差** |
| adsb.lol | `api.adsb.lol` | **免**（ODbL） | 实时 ADS-B（军机主力） |
| airplanes.live | `api.airplanes.live` | **免**（非商业） | 点查询兜底 |
| adsb.fi | `opendata.adsb.fi` | **免**（个人/非商业） | 点查询兜底 |
| Wingbits | `wingbits.com` | **需** | 实时 ADS-B |
| OpenSky Network | `opensky-network.org` | **半**（client id/secret，匿名强限流） | 实时；作者已**在军机 seeder 中禁用** |
| FAA ASWS | `nasstatus.faa.gov` | **免**（XML） | 实时（14 个美国枢纽机场） |
| ICAO NOTAM | `icao.int` | **需**（`ICAO_API_KEY`） | 实时（46 个 MENA 机场） |
| AviationStack | `aviationstack.com` | **需** | 56 个国际机场；**有月度预算与请求预算护栏** |
| gpsjam.org | `gpsjam.org` | **免** | 日/实时（H3 res-4 网格 GPS 干扰） |
| CelesTrak | `celestrak.org` | **免** | 日（卫星 TLE，客户端 SGP4 推算） |

### B.7 能源 / 商品 / 环境（免 Key 为主）

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| GIE AGSI+ | `agsi.gie.eu` | **半**（`GIE_API_KEY`） | 日（欧盟天然气库存） |
| USGS 地震 | `earthquake.usgs.gov` | **免** | **5 分钟**（M4.5+） |
| NASA FIRMS | `firms.modaps.eosdis.nasa.gov` | **半**（`NASA_FIRMS_API_KEY`） | 近实时（VIIRS 火点） |
| NASA EONET | `eonet.gsfc.nasa.gov` | **免** | 实时（13 类自然灾害） |
| GDACS | `gdacs.org` | **免** | 实时 |
| Open-Meteo（ERA5） | `open-meteo.com` | **免** | 日（气候异常基线） |
| US NWS | `api.weather.gov/alerts/active` | **免** | 实时 |
| 加拿大 ECCC | `api.weather.gc.ca` | **免** | 实时 |
| WMO SWIC | `severeweather.wmo.int/json/wmo_all.json` | **免** | 实时（CAP） |
| 香港天文台 | `data.weather.gov.hk` | **免** | 实时 |
| 日本气象厅 | `jma.go.jp` | **免** | 实时（西北太平洋台风） |
| 印度 IMD | `api.imd.gov.in` | **需**（三重凭证 + 短时 JWT） | 实时 |
| USGS 矿产摘要 MCS | `sciencebase.gov` DOI `10.5066/P1WKQ63T` | **免**（美国公有领域） | 年 |
| BGS 世界矿产生产 | `bgs.ac.uk` | **免（需署名）** | 年 |
| 上海黄金交易所 | `en.sge.com.cn/data_BenchmarkPrice` | **免** | 日（SHAU/SHAG AM/PM 基准） |

### B.8 媒体 / OSINT

| 名称 | 域名 | Key | 粒度 |
|---|---|---|---|
| Telegram OSINT | MTProto（GramJS，`data/telegram-channels.json`） | **需**（`TELEGRAM_API_ID/HASH`，另有 session） | **60 秒轮询**，单频道 15s 超时，整轮 3 分钟硬超时 |
| X 新闻账号 | `api.x.com` 官方 API | **需**（`X_BEARER_TOKEN`） | 5–15 分钟；**明确禁止爬取**；默认 `metadata_only` 存储 |
| YouTube 直播摄像头 |  curated live stream ID | **免** | 实时（重新校验 liveness） |
| 全球主流 RSS | 见 `docs/data-sources.mdx` 的 per-variant 表（full 变体 15 个分类、数百个 feed） | **免** | 分钟级 |

### B.9 免 Key 可达汇总（我们最关心的）

**完全免 Key 且对我们的股票终端有直接价值的：**

1. **Google News RSS 查询模板**（B.1）— 最高价值
2. **财经 RSS 直连**：CNBC / Yahoo Finance / Seeking Alpha / Business Wire / GlobeNewswire / BI / Fox Business
3. **官方监管 RSS**：SEC `pressreleases.rss`、Fed `press_all.xml`
4. **A 股官方**：SSE `query.sse.com.cn`、SZSE `www.szse.cn/api/report/...`（北向成交额 + 两融余额，日频）
5. **CCFI**：`en.sse.net.cn/indices/ccfinew.jsp`
6. **宏观**：ECB Data Portal、Eurostat、BIS、World Bank、UN Comtrade（匿名）、CBR、USA Spending、NBS/SAFE/CFETS（中国官方）
7. **情绪指标**：alternative.me Fear & Greed、mempool.space
8. **Polymarket Gamma API**（浏览器直连免 Key，服务端需绕 TLS 指纹）
9. **地理/灾害类**：USGS、GDACS、NASA EONET、Open-Meteo、NWS、ECCC、WMO SWIC、HKO、JMA（**与我们无关，仅列完整性**）

---

## C. 信息架构

### C.1 面板 / 视图布局

- **主体**：一张全屏地图（平面 deck.gl / 3D globe.gl 可切换）+ 右侧/下方的**可拖拽面板网格**。
- **Panel 体系**：109 个 `Panel` 子类，统一继承基类；`setContent(html)` 150ms 防抖渲染；**事件委托**在稳定的 `this.content` 上；支持行列 span 调整，布局持久化到 localStorage。
- **地图图层分组**（`src/config/map-layer-definitions.ts` 统一定义 renderer 支持 / 是否 premium / 变体过滤 / i18n key）：

| 组 | 图层 |
|---|---|
| 地缘政治 | 冲突区、热点、制裁、抗议 |
| 军事战略 | 军事基地、核设施、伽马辐照源、APT 组织、发射场、关键矿产 |
| 基础设施 | 海缆(86)、管道(88)、互联网中断、AI 数据中心(313) |
| 交通 | AIS 船舶、机场延误 |
| 自然事件 | 地震+EONET、天气警报 |
| 叠加/标注 | 昼夜线、经济、国界、水道、贸易路线(19)、火点(FIRMS) |
| 摄像头 | 实时直播网格 |

- **6 个站点变体**由 hostname 决定，控制：默认面板集、地图图层、刷新间隔、主题、文案。
- **智能面板**（把原始流变成判断）：AI 战略态势、战略风险总览、CII、基础设施级联、国家简报、航空情报、气候异常、难民追踪、海湾经济体、WTO 贸易政策、央行/BIS/IMF、市场自选。
- 其他视图：搜索（Cmd+K）、快照系统、数据导出、自定义监控、活动追踪（自动"已读"）、移动端首屏欢迎。

### C.2 "把独立数据源关联到单一作战图景"具体怎么做

**三种关联机制，缺一不可：**

| 机制 | 实现 | 例子 |
|---|---|---|
| **① 地理关联** | 统一降维到空间单元：1°×1° 网格（汇聚检测）、200km 半径（区域汇聚）、H3 res-4 六边形（GPS 干扰）、0.1° 网格去重（抗议/灾害，约 10km） | 地震 + 军机 + 抗议落同格 → 汇聚告警 |
| **② 实体关联** | **Entity Registry** 知识库：38 家公司 / 3 个指数 / 5 个板块 / 6 种商品 / 3 个 crypto / 11 个国家。每个实体含 `id / name / type / aliases / keywords / sector / related`。匹配按置信度分级：别名 95%、关键词 70%、关联实体 60% | `AVGO` → broadcom → ["Broadcom","AVGO","AI chips","semiconductors","VMware","nvidia","intel","amd"] → 扫全部新闻簇 |
| **③ 时间/速度关联** | 维护 rolling snapshot（话题词频 / 价格变动 / 预测市场概率），每次刷新与上一快照比对，加速度超阈值即告警；配去重防告警疲劳 | 话题提及率翻倍且 ≥6 源/小时 → Velocity Spike |

**再加一层"国家聚合"**：所有信号按国家码分组，产出 `{country, totalCount, highSeverityCount, signalTypes:Set, signals[]}`，形成统一国家视图。

### C.3 "在新闻见报前标记汇聚点"到底是什么机制（重点）

**先说结论——它没有预测模型，也没有 LLM 预言。它是 9 条可解释的规则，核心思想是"多个独立流同时异常 = 可疑"。**

#### 机制一：地理汇聚（Geographic Convergence）

```
网格：1° × 1°
窗口：24 小时
事件类型（4 类）：抗议(ACLED/GDELT) / 军机(OpenSky ADS-B) / 军舰(AIS) / 地震(USGS)
触发：同一格内 ≥3 种不同类型
评分：type_score = 类型数 × 25      （4 类满分 100）
      count_boost = min(25, 事件总数 × 2)
      score = min(100, type_score + count_boost)
分级：4 类 = Critical；3 类且高分(90-100) = Critical；3 类且 81-89 = High
```

#### 机制二：14 种信号（这才是"在新闻见报前"的主力）

| 族 | 信号 | 触发条件 | 含义 |
|---|---|---|---|
| 新闻源 | **◉ Convergence** | 30 分钟内 **≥3 类信源**报道同一事件 | 多渠道互证 |
| 新闻源 | **△ Triangulation** | 通讯社 + 政府 + 情报媒体**三方对齐** | "权威三角" |
| 新闻源 | **🔥 Velocity Spike** | 话题提及率翻倍 且 ≥6 源/小时 | 故事正在加速 |
| 新闻源 | **📊 Keyword Spike** | 词频显著高于基线（**三关**：独立提及数下限 + 基线倍数 + 由 >1 家媒体承载，防止单一高产源刷出） | 关键词正在破圈 |
| 市场 | **🔮 Prediction Leading** | 预测市场移动 ≥5% 但新闻覆盖低 | 市场已定价、新闻未到 —— **最接近"见报前"的一条** |
| 市场 | **📰 News Leads Markets** | 新闻高速但价格没动 | 可能错杀/未定价 |
| 市场 | **✓ Market Move Explained** | 价格动 ≥2% 且实体检索到相关新闻 | 有明确催化剂 |
| 市场 | **📊 Silent Divergence** | 价格动 ≥2% 但穷尽搜索后无相关新闻 | 无解释的异动 |
| 市场 | **📈 Sector Cascade** | 多个相关板块同向 | 级联反应 |
| 基建 | **🛢 Flow Drop** / **🔁 Flow-Price Divergence** | 管道中断关键词 / 中断新闻但油价未动 | 供给约束未定价 |
| 地缘 | **🌍 Geographic Convergence** | 见机制一 | |
| 地缘 | **🔺 Hotspot Escalation** | 4 分量加权：新闻 35% + CII 25% + 地理汇聚 25% + 军事 15%，再与静态基线 `0.30/0.70` 混合，映射到 1–5 分 | 多流互证的热点升级 |
| 地缘 | **✈ Military Surge** | 战区内运输/战斗机活动达基线 2× | 部署或危机响应 |

置信度区间 **60–95%**，按模式强度给分。

#### 机制三：诚实性兜底（我认为这才是它真正的护城河）

- **Intelligence Gap**：单例 freshness tracker 监控 40 个数据源，状态分 `fresh(<15min) / stale(2h) / very_stale(6h) / no_data / error / disabled`，**显式报告"我们现在看不到什么"**。
- **Publisher Family**：任何"N 家独立来源"都按**编辑部**计数，不按 feed 标签计数。防止同一媒体的 5 个区域版被当成 5 家独立来源。
- **Brief Grounding**：国别简报只能引用它"脚下的"新闻集合；若这些新闻来自少于发布门槛的 Publisher Family 数，就**不生成简报**——宁可留白，不出版"一家媒体合成的周报"。
- **不可用 ≠ 0**：`HKEX 因自动化访问条款被明确禁止`；`JODI 的 China 契约被显式阻塞直到验证其确实发布 CN 行`；`北向净流入停发后不拿成交额冒充净流入`；`省略的分类是 unknown 不是 0`。这一条贯穿全项目。

> **我的判断**：所谓"在新闻见报前标记汇聚点"，80% 是**规则化的多流互证**，20% 是**把不确定性显式化**。它不预测，它只是**在你还没注意到的时候，把"几个互不相干的异常刚好撞在一起"这件事摆到你面前**。这个定位比"预测"诚实得多，也便宜得多。

---

## D. 对 tick-stock-panel 的可借鉴性分级表

> 诚实原则：我们是**三市场股票分析终端**，不是地缘政治情报系统。下表明确标注"不适合"。

| # | 可借鉴点 | 难度 | 需要什么 | 与我们现有能力契合度 | 建议 |
|---|---|---|---|---|---|
| 1 | **Google News RSS 查询模板**（`site:` / 关键词 OR / `when:Nd` / `hl-gl-ceid` locale） | **低** | 无新依赖、无 Key | **极高** — 我们已有 `news_feed.py`（7 分类 RSS），缺的正是"按需生成任意查询"这一层 | **P0** |
| 2 | **财经 RSS URL 直接补进 finance/market 分类**（CNBC / Yahoo Finance / Seeking Alpha / Business Wire / GlobeNewswire / SEC / Fed） | **低** | 无新依赖、全免 Key | **极高** — 直接扩充现有 7 分类中 finance、market 两类的源密度 | **P0** |
| 3 | **实体注册表 + "异动是否有新闻解释"**（ticker→别名→关键词→关联实体；≥2% 异动后扫新闻，有=Explained，无=Silent Divergence） | **中** | 需新建一张实体表（A/港/美股约几百条即可起步）；无外部 Key | **极高** — 这是全报告里**最像一个股票终端该有的东西**；我们有行情 + 有新闻流，就差把两者用实体对齐 | **P0** |
| 4 | **题材热度加速检测（Keyword Spike 三关）**：独立提及数下限 + 基线倍数 + 由 >1 家媒体承载 | **中** | 无新依赖；需一个滚动词频快照存储 | **高** — 我们有 `/hotspots` 题材热度榜，现在大概率只有"量"没有"加速度"，加上这层才是真"热度" | **P0** |
| 5 | **服务端分类级摘要聚合 + 空结果短 TTL**（一次聚合 N 个 feed；单 feed 8s 超时；健康 3600s / 空结果 300s；digest 900s） | **低** | 我们已有 FastAPI + 缓存；只是把策略补齐 | **高** — 直接提升新闻流稳定性与首屏速度 | **P1** |
| 6 | **Source Tier 1–4 + 可信度标签**（通讯社/官方→主流→垂类→聚合器；另挂宣传风险） | **低** | 无新依赖，纯元数据标注 | **中高** — 我们有 7 分类但源无分级；给新闻打可信度标签是低成本高感知 | **P1** |
| 7 | **Publisher Family 归一**（同一编辑部多个 feed 算 1 家，用于"几家报道"计数） | **中** | 需一张 publisher→family 映射表 | **中高** — 直接决定题材热度榜的"几家媒体报道"是否可信。不做这条，第 4 条会失真 | **P1** |
| 8 | **沪深港通北向成交额 + 两融余额**（SSE/SZSE 免 Key 端点） | **中** | 新采集器；需处理交易所反爬；日频即可 | **高** — A 股/港股终端的标准资金面指标，我们目前大概率没有 | **P1** |
| 9 | **情报缺口显式化 + 健康度分级**（fresh/stale/very_stale/no_data/error/disabled；"看不到就说看不到"，不把空数据渲染成风平浪静） | **低** | 无新依赖 | **中高** — 团队已有 `data-freshness-and-local-hotspot` / `hotspot-empty-rootcause` 报告，说明这就是我们的痛点 | **P1** |
| 10 | **七信号复合雷达（Market Radar）**：流动性(JPY ROC) / 资金结构(BTC vs QQQ) / 宏观 Regime(QQQ vs XLP 20d ROC) / 技术趋势 / 哈希率 / 挖矿成本 / 恐惧贪婪，≥57% 看多才 BUY | **中** | 需要 BTC/链上数据 + 美指；**部分信号对我们无意义（哈希率/挖矿成本）** | **中** — 我们有 Regime 判断，可借"多信号投票 + 未知信号不计入分母"的框架，但信号本身要换成 A/港/美股适用的（如北向、两融、涨跌家数、波动率、AH 溢价） | **P1（借框架，换信号）** |
| 11 | **Last-Good Digest + Coverage 准入**（用 coverage 而非 200 决定是否可留存；用 CAS 防并发写坏） | **中** | 无新依赖 | **中** — 我们的新闻流若已有缓存，加这一层可显著减少"上游抖动→前端空白" | **P2** |
| 12 | **Bootstrap 两级水合**（fast/slow 双超时并发 + 按需 `ensureHydrated`） | **低** | 前端已有 TanStack Query，需改造首屏预热 | **中** — 收益有限，我们规模没到 | **P2** |
| 13 | **SmartPollLoop**（指数退避 ≤4× / 视口内才刷新 / 标签页隐藏暂停 / 恢复时错峰 150ms） | **低** | 无新依赖 | **中** — 我们前端已有 TanStack Query 的 refetch 机制，边际收益小 | **P2** |
| 14 | **复合指数方法论（CII 式）**：静态基线 ×0.4 + 动态事件分 ×0.6 + boosts + floors + clamp | **中** | 概念可借，无需新依赖 | **中** — 我们有"强度梯队"，但要注意 CII 的权重是**拍的**（见下方风险提示） | **P2** |
| 15 | **面板化可拖拽 UI**（109 个 Panel 子类、span 调整、localStorage 持久化） | **中** | 需新建 React 组件体系（dnd + grid） | **中低** — 我们已有固定布局，重构成本高，用户没提这个诉求 | **P2** |
| 16 | **MCP server / CLI / SDK 工具化**（它把每个能力都暴露成 MCP tool + `npx worldmonitor`） | **中** | 需实现 MCP 协议 | **中** — 若 Amber 想让 AI Agent 调用我们的终端，这是加分项；但偏离主线 | **P2** |
| 17 | **本地 LLM / 浏览器端 ONNX**（MiniLM embedding + 聚类 + 摘要 + NER） | **高** | 需引入 transformers.js 或 Python 侧模型 | **低** — 我们后端是 Python，做 embedding 更合适在服务端；且聚类对股票终端价值不明 | **不做** |
| 18 | **变体系统**（同一代码库 6 个 hostname 站点） | **高** | 需主题/配置分层 | **低** — 我们只有一个产品 | **不适合** |
| 19 | **Tauri 桌面端 + Node sidecar + fetch patch** | **高** | Rust 工具链、打包、签名 | **低** — 我们是本地 Web 终端 | **不做** |
| 20 | **双地图引擎（deck.gl + globe.gl）/ 地图图层体系** | **高** | WebGL 栈、瓦片、大量地理数据 | **极低** — 股票终端没有地理维度 | **不适合** |
| 21 | **地缘政治 / 军事 / 海运 AIS / 航空 ADS-B / GPS 干扰 / 网络威胁 IOC 全套** | — | — | **零** — 与我们的三市场股票分析完全无关 | **不适合** |
| 22 | **Convex 计费/用户态/向量记忆 / Dodo 支付 / Clerk 鉴权** | — | — | **零** — 我们是本地单人终端 | **不适合** |
| 23 | **Railway 常驻 seeder 舰队 + Vercel Edge + Upstash Redis 四层缓存** | — | — | **零** — 我们的部署模型完全不同（本地 FastAPI + Polars/Parquet 数据湖） | **不适合（架构不借鉴，只借"seed 与请求分离"的原则）** |

### D.1 两个必须写的风险提示

1. **CII / Market Radar / Hotspot Escalation 的权重是拍脑袋的。** 例如 Hotspot Escalation 的 `0.35/0.25/0.25/0.15`、Market Radar 的"≥57% 看多"、CII 的 `0.40/0.60`，全项目文档里**没有任何回测或拟合依据**，只有"这样看起来合理"。我们若要借，必须自己用历史数据验证，或直接标注为启发式。**不要因为"World Monitor 这么做了"就认为它是对的。**
2. **它的市场数据合规立场比我们严格。** 它因为 ToS 明确**排除了 FMP**、正在**退役 Yahoo**、**禁止抓 HKEX**。我们若从 Yahoo/东财取数，同样的条款风险是真实存在的。这条值得单独做一次数据源合规复核。

### D.2 一句话总结优先级

> **P0 只做三件事**：① Google News 查询模板；② 补财经 RSS URL；③ 实体注册表 + 异动-新闻对齐。
> 这三件都不需要新 Key、不需要新基础设施、不需要碰 AGPL 代码，且直接落在我们已有的 `news_feed.py` + `/hotspots` + 行情数据之上。
> **其余多为 P2/不做**——它最有视觉冲击力的部分（地图、地缘、军机、海缆）对我们 100% 无用。

---

## E. 许可证红线复核

### E.1 确认 AGPLv3

- 根 `LICENSE` 首行：**`GNU AFFERO GENERAL PUBLIC LICENSE` / `Version 3, 19 November 2007`** → **确认 AGPL-3.0**。
- README 明确：**AGPL-3.0-only**，且"Commercial use / SaaS"一栏写的是"Yes, under AGPL-3.0-only when you comply with AGPL obligations"。
- 版权：Copyright (C) 2024-2026 Elie Habib。

### E.2 子目录许可证（重要发现，之前未注意）

| 路径 | 许可证 | 能不能用 |
|---|---|---|
| 根 `LICENSE`（`src/` `server/` `api/` `convex/` `scripts/` `shared/` `data/` 等） | **AGPL-3.0-only** | ❌ **绝对不能复制任何代码** |
| `cli/LICENSE` | **MIT** | ⚠️ 名义 MIT，但它只是个 API 客户端外壳（依赖 `worldmonitor.app` 云端服务），**对我们无价值**，且混用有风险 → 不碰 |
| `sdk/go/LICENSE`、`sdk/python/LICENSE`、`sdk/ruby/LICENSE` | **MIT** | ⚠️ 同上，是云端 API 的零依赖客户端 → 不碰 |

> **结论**：不要因为"cli 和 sdk 是 MIT"就以为可以抄。它们和我们的需求无关（我们不需要调 World Monitor 的云端 API），且引用一个 AGPL 仓库里的 MIT 子目录在法律上虽可行，但**收益为零、风险非零**。一律不碰最安全。

### E.3 能拿 / 不能碰 清单

#### ✅ 可以拿（事实信息 / 思想，不受著作权保护或属于事实）

| 类别 | 具体 | 依据 |
|---|---|---|
| **数据源 URL / 域名 / API endpoint 地址** | 全部 B 节的 URL 清单 | 地址是事实，不是表达 |
| **数据源的能力描述** | "USGS 地震 5 分钟更新、M4.5+"、"GDELT 提及数 ≥5" | 事实 |
| **公开算法公式** | 汇聚评分 `types×25 + min(25,count×2)`、CII 权重、Hotspot 分量权重 | 文档已公开；但**实现代码要自己写** |
| **信息架构与分类法** | 14 种信号类型、Source Tier 1–4、Publisher Family、Intelligence Gap 状态词汇 | 思想/方法论 |
| **工程实践原则** | seed 与请求分离、空结果短 TTL、coverage 而非 200、不可用≠0 | 思想 |
| **合规判断** | "FMP ToS 禁止再分发"、"HKEX 禁止自动化访问"、"SIPRI 不镜像全库" | 事实 + 对我们是**有价值的红灯** |

#### ❌ 绝对不能碰

| 类别 | 具体 | 理由 |
|---|---|---|
| **任何 `.ts` / `.tsx` / `.js` / `.mjs` / `.cjs` / `.py` / `.rs` 实现代码** | `server/worldmonitor/news/v1/_feeds.ts`、`list-feed-digest.ts`、`shared/analysis-*.ts`、`src/components/*.ts`、`scripts/seed-*.mjs` 等 | AGPL 传染性；**复制即触发我们整个仓库必须 AGPL 开源** |
| **配置文件** | `vite.config.ts`(81KB)、`vercel.json`(57KB)、`middleware.ts`、`biome.json`、Dockerfile 系列、`docker-compose.yml` | 同上 |
| **提示词工程** | `summarize-article.ts`、`brief-llm-core.js`、`docs/methodology/news-digest-and-briefing.mdx` 里的 prompt 与 LLM 编排 | 同上；且这是作者核心资产 |
| **数据文件** | `data/telegram-channels.json`、`data/x-accounts.json`、`shared/stocks.json`、`shared/commodities.json`、1,480 条希伯来语→英语地名映射 | 有独创性编排 + AGPL；**即使只是"参考结构"也不该照搬** |
| **文档正文** | `CONCEPTS.md`(153KB)、`ARCHITECTURE.md`、`docs/**.mdx` | 文档本身受著作权保护；**可以读、可以总结、不可复制段落** |
| **i18n 文案 / 分类名 / 面板名** | 若我们有对应的 UI 文案 | 表达性内容 |

### E.4 若想要但只能用代码实现的能力 → **必须自己重写**

以下能力我们若要做，**不允许参考其实现，只能依据本报告描述的需求自行设计**：

| 能力 | 重写边界 |
|---|---|
| 新闻摘要聚合与缓存 | 按 B.1/B.2 的 URL + D#5 的策略描述自己写，不读 `list-feed-digest.ts` |
| 实体注册表与异动-新闻对齐 | 按 C.2 的字段描述（`id/name/type/aliases/keywords/sector/related` + 95/70/60 置信度）自己建表，不读 `src/config/entity-registry*` |
| 关键词加速检测 | 按 C.3 的三关描述自己写，不读 `shared/analysis-*.ts` |
| 沪深港通 / 两融采集 | 按 B.2 的端点 URL 自己写请求与解析，**不读 `scripts/china-stock-connect/adapters.mjs`** |
| Source Tier / Publisher Family | 按 C.3 的规则自己建映射表，不读 `shared/source-provenance.ts` |
| 复合指数（强度梯队升级） | 按 C.3/D#14 的公式思路自己定权重并回测，**不读 `scripts/scorecard/**` 与 `cii-risk-scores` 实现** |

> ⚠️ **执行纪律建议**：后续若有人去实现 D 节的 P0/P1 项，**实现阶段不应再打开 worldmonitor 仓库的代码目录**。本报告已把所有需要的事实（URL、公式、阈值、策略）抽出来了；再回去看代码会直接产生 AGPL 污染风险。只可回查 `docs/*.mdx`（读总结可以，复制不行）。

---

## F. 待确认项（我没有验证，明确标注）

| # | 待确认 | 为什么 |
|---|---|---|
| 1 | **Google News RSS 在国内部网络环境下是否可达、解析是否稳定** | 我们部署环境是本地；Google 域名可达性未验证。这是 D#1（P0）的**前置条件**，必须先做一次连通性 + 解析冒烟 |
| 2 | SSE/SZSE 端点（`query.sse.com.cn`、`www.szse.cn/api/report/...`）的**反爬强度与字段稳定性** | 作者用了"configured proxy"直达，说明直连可能不稳定；具体限制未验证 |
| 3 | 各 RSS 源的**实际更新频率与条数** | 文档标称"分钟级"未经我实测 |
| 4 | `docs/data-sources.mdx` 中 feed 清单与代码 `_feeds.ts` 是否 100% 同步 | 文档自称由代码 URL 字面量生成（`source-attribution.mdx`），但 feed 表是人工维护的，可能存在漂移 |
| 5 | SEC / Fed / ECB / Eurostat / BIS 等官方源的**速率限制条款** | 未逐个核对 ToS |
| 6 | `data/telegram-channels.json` 与 `data/x-accounts.json` 里具体有哪些频道/账号 | **我故意没读**（对我们无用且属 AGPL 数据文件） |
| 7 | World Monitor 自身**没有任何回测证明其权重有效** | 我在 `docs/methodology/**` 里未找到拟合/回测依据。这是判断而非事实，请以"未找到"理解 |

---

## 附：本次调研的阅读路径（便于复核）

1. `README.md` → `ARCHITECTURE.md`（§1–§6）→ `CONCEPTS.md`（§News Story Tracking / Source Catalog / Prediction Markets）
2. `docs/data-sources.mdx`（797 行，**全报告主料**）→ `docs/source-attribution.mdx`（761 主机统计）
3. `docs/geographic-convergence.mdx`（64 行，汇聚机制）→ `docs/signal-intelligence.mdx`（14 信号 + 实体注册表）→ `docs/hotspots.mdx`（热点升级）
4. `docs/country-instability-index.mdx` + `docs/finance-data.mdx`（市场数据合规立场 + Market Radar）
5. `cli/README.md` + `cli/LICENSE`（发现子目录 MIT）
6. `SELF_HOSTING.md`（部署复杂度）→ `.env.example`（270 变量，判定 Key 需求）
7. `server/worldmonitor/news/v1/_feeds.ts`（**仅抽取 URL 字面量，未复制代码**）→ `scripts/china-stock-connect/adapters.mjs` 头部注释（仅读取设计意图与排除理由）
8. 规模统计：`find` + `wc -l`；面板数来自 `ARCHITECTURE.md` §3 自述（109 个 Panel 子类）
