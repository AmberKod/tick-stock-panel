# 新闻源接入(Anspire)交付说明 — 2026-09-18

## 背景与选型

热点工作区此前只有本地行情聚合出的"热度"(概念涨跌幅 + 涨停家数),回答不了
"今天到底发生了什么"。需要补一路联网检索,把公告/事件/催化拉进热点页。

选型依据:参考项目 `参考项目/stock/daily_stock_analysis` 的 `src/search_service.py`
(4946 行)已实现 7 家 provider(Tavily / SerpAPI / Bocha / Anspire / MiniMax /
Brave / SearXNG)+ 多 Key 轮询 + 故障转移,可直接借鉴其端点与参数形态。

连通性实测(本机):

| 源 | 结果 | 备注 |
|---|---|---|
| Anspire | 307 / plugin 端点 401(测试 Key) | 可达,401 说明鉴权链路正常 |
| Tavily | 200 | 可达 |
| Bocha | 405 | 可达(方法不对) |
| Brave | 422 | 可达 |
| SerpAPI | 401 | 可达 |
| MiniMax | 301 | 可达 |
| 东方财富(对照) | 被墙 | 这也是必须接外部搜索源的原因 |

结论:**Anspire** 一 Key 兼大模型与联网检索、国内直连、对 A 股/港美股检索有优化,
本轮只落地 Anspire 一家,其余 provider 的类已经留好插槽(`PROVIDER_CLASSES`),
后续加一家只需新增子类 + 注册。

## 本轮范围(用户拍板)

- 搜索源:**Anspire**
- 交付范围:**搜索服务 + 热点页「新闻」tab**
- Key 存放:**设置页配置**(非环境变量、非 .env)

## 改动清单

### 后端

| 文件 | 改动 |
|---|---|
| `backend/app/secrets_store.py` | 升级:新增 `ENCRYPTED_FIELDS` 白名单(anspire/bocha/tavily/brave/serpapi),Windows 走 DPAPI(`CryptProtectData`/`CryptUnprotectData`,ctypes),非 Windows 退化为明文 0600;`load()` 解密、`save()/clear()` 统一经 `_write_all()`(唯一加密落点)。TickFlow / AI Key 保持明文不变,向后兼容 |
| `backend/app/services/news_search.py` | 新增:`NewsItem`/`SearchResponse`；`BaseSearchProvider`；`AnspireSearchProvider`；`PROVIDER_CLASSES`、`PROVIDER_FALLBACK_ORDER`；`search()` fail-closed；`_provider_cache` + `key_fingerprint` 实现多 Key 轮询；`stock_query()`/`concept_query()`；成功才写 TTL 缓存 |
| `backend/app/api/news.py` | 新增:`GET /api/news/search`、`/stock`、`/concept`、`/status`,`POST /api/news/cache/invalidate` |
| `backend/app/api/settings.py` | 新增:`GET/POST/DELETE /api/settings/search-key`,POST 走「先探后存」 |
| `backend/app/main.py` | 注册 news router |

### 前端

| 文件 | 改动 |
|---|---|
| `frontend/src/lib/api.ts` | `NewsItem`/`NewsResponse`/`NewsProviderStatus`/`NewsStatus` 类型;news/news 相关 4 个方法 + search-key 3 个方法 |
| `frontend/src/lib/queryKeys.ts` | `newsStatus`/`newsSearch`/`newsStock`/`newsConcept`/`searchKey` |
| `frontend/src/components/hotspot/NewsPanel.tsx` | 新增:新闻列表 + 内联 `SearchKeySetup`;未配 Key 时显示配置入口而非空列表 |
| `frontend/src/pages/Hotspots.tsx` | 新增「题材 / 新闻」视图切换,新闻视图挂 `NewsPanel` + 题材 chip |
| `frontend/src/pages/settings/Keys.tsx` | 新增 `SearchKeyConfig`(状态/掩码/多 Key 计数/校验并保存/清除) |
| `frontend/src/pages/settings/DataSources.tsx` | 数据源 tab 末尾挂载 `<SearchKeyConfig />` |

## 设计决策

1. **fail-closed,不伪装**:没 Key / 请求失败一律返回 `success=false` + 明确
   `error_message`,前端据此显示"去配置"。绝不给空列表让用户以为"今天没新闻"。
2. **先探后存**:设置页保存 Key 前,先用该 Key 真发一次搜索请求;失败则
   `ok=false` 且不落盘(与 tickflow-key 同策),避免"配了但一直失败查不出原因"。
   探测失败时清掉旧值,防止失效 Key 一直挡着。
3. **密钥加密**:搜索 Key 属敏感凭据,走系统级 DPAPI 加密存本机,磁盘上无明文;
   非 Windows 退化为 0600 明文(与既有 TickFlow Key 行为一致)。
   白名单机制:不在 `ENCRYPTED_FIELDS` 里的字段保持原行为,零迁移成本。
4. **多 Key 轮询**:`_provider_cache` 按 provider 名缓存实例,并用 `key_fingerprint`
   感知 Key 变化(Key 变了才重建实例)。否则每次请求新建 provider、`cycle`
   归零,轮询会退化成永远只用第一个 Key。
5. **只落一家**:`PROVIDER_FALLBACK_ORDER` 目前只有 `anspire`,保留结构不预留死代码。

## 验证

- `ruff check backend/`(CI 同口径):**All checks passed**
- 新增测试:`tests/test_news_search.py` 14 + `tests/test_secrets_store.py` 7 = **21 passed**
  覆盖:DPAPI 往返、磁盘无明文、`clear()` 不写回明文、白名单拦截未知 key、
  fail-closed(无 Key / 401 / 403 / 业务码非 0)、轮询、缓存(成功才缓存)、
  查询词构造。
- 全量:**2191 passed,1 failed**;唯一失败 `test_dev_launcher` 为环境性
  (worktree 无 `backend/.venv`),主仓通过。
- 前端 `tsc --noEmit`(sandbox 路径映射):**0 errors**

## 已知限制 / 后续

- 只接了 Anspire 一家。要加 Tavily/Brave 等:新增 `BaseSearchProvider` 子类 +
  注册进 `PROVIDER_CLASSES` / `PROVIDER_FALLBACK_ORDER` + 前端 provider 下拉即可。
- 新闻结果未做去重/排序优化(按源返回顺序),也未接本地缓存持久化(仅进程内 TTL)。
- 未做"新闻 → 题材归因"的自动关联(目前靠题材 chip 手动切)。
- 本分支尚未合入 develop,需与 `enriched-cols` / `data-freshness` / `hk-enriched`
  在**同一停服窗口**一起上(见停服窗口清单)。

## 上手步骤

1. 设置页 → 数据源 →「新闻搜索源 (Anspire)」粘贴 Key(多个用逗号分隔)→ 校验并保存。
2. 热点页右上角切到「新闻」tab,点题材 chip 或直接看当前题材的新闻。
3. 想确认后端状态:`GET /api/news/status`。
