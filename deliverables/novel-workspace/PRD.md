# PRD — 小说工作区（Phase 4 落地）

| 项 | 内容 |
|---|---|
| 文档语言 | 中文 |
| 项目名 | `novel_workspace` |
| 宿主项目 | `E:/ai_codes/ai_personal_panel/tsp-fresh`（TickStock 多维工作台） |
| 技术栈 | 前端 React 18 + Vite + TS + Tailwind（dev 3011）；后端 FastAPI（`BACKEND_PORT\|\|3018`） |
| 交付位置 | `E:/ai_codes/ai_personal_panel/tsp-fresh/deliverables/novel-workspace/PRD.md` |
| 作者 | 许清楚（产品经理） |
| 版本 | v1.0（MVP 范围冻结） |

---

## 0. 原始需求复述

把 `router.tsx` 中已有路由 `/novel` → `<NovelWorkspace />` 从「诚实占位页」升级为**可用功能**（Phase 4 落地）。占位页已向用户公开承诺四件事，本 PRD 必须逐条满足或显式升级：

1. **书架与大纲**：每本书独立目录，大纲树 + 章节 Markdown 管理
2. **章节编辑器**：Markdown 编辑，天然可 git、可 diff
3. **AI 续写/润色**：走统一 AI 网关，按大纲上下文续写
4. **导出**：章节产物支持导出 Markdown / 文本
5. **synergy 承诺**：本地优先 —— 小说稿是纯 Markdown + JSON 大纲，数据始终在你自己的 `data/` 目录里，任何编辑器都能打开

---

## 1. 产品目标

**一句话定位**：一个**本地优先、人机协同**的长篇小说写作台 —— 稿件以明文 Markdown 存在你自己的 `data/` 目录，AI 只在人划定的大纲与上下文内产出**草稿**，经人工采纳后才成为正文，全程不留黑盒。

**三条设计原则**

| 原则 | 含义 | 落地约束 |
|---|---|---|
| **P1 本地优先** | 稿件、大纲、追踪态全部是 `data/novel/` 下的明文文件（`.md` + `.json`），无数据库、无云端、无 Blob。用户用记事本/VSCode 打开 `data/novel/books/<id>/正文/ch-001.md` 就能改，改完 UI 下一次刷新即读到 | 后端只做**文件读写 + 原子替换写**，不做缓存唯一权威；任何内存态都必须可从磁盘重建 |
| **P2 诚实不可用（fail-closed）** | 能力缺失或调用失败时，UI 必须**显式声明 unavailable 与具体原因**，禁止静默空结果、禁止 mock 假生成、禁止把失败包装成"暂无内容" | 所有 AI 相关端点返回显式 `{ok, available, reason, code}`，前端据此置灰按钮 + 展示原因文案 |
| **P3 人机协同而非全自动** | AI 产出永远是**草稿**，不直接覆盖正文；关键的结构化摄取（角色状态、伏笔、事实快照）由人点「采纳」触发；写后自检**只提醒不改写** | 草稿与正式物理分离存储；无「一键写完全书」入口 |
| **补充：AI 输出不做任何数值评分承诺** | 本域不产出"章节评分/质量分/推荐指数"等数字指标（该纪律在股票域为"新打分需回测"，本域等价表达为：不做无依据的量化质量断言） | AI 返回的只有文本与结构化事实字段；质量相关反馈一律用**规则命中清单**（如"疑似截断""AI 高频词 ≥N 次"）而非分数 |

---

## 2. 用户故事

| # | 用户故事 |
|---|---|
| US-1 | 作为**首次使用的作者**，我希望在工作区里一键新建一本书并自动生成标准目录（`book.json` + `正文/` + `state.json`），并且空态直接告诉我这些文件的绝对路径，以便我立刻知道稿子会落在磁盘的哪里、可以随时用手边编辑器打开 |
| US-2 | 作为**大纲驱动写作的作者**，我希望以「卷 → 章」两级树管理大纲，并为每章填写一句话细纲与本章节拍，以便后续 AI 续写有明确依据、而不是凭空发挥 |
| US-3 | 作为**专注写作的作者**，我希望在编辑器里直接写 Markdown 并自动落盘（无需点保存），能切换源码/预览视图，以便我像在本地编辑器一样写作且不怕丢改动 |
| US-4 | 作为**卡文的作者**，我希望选中当前章节后点「AI 续写」，系统按「本书滚动摘要 + 本章细纲 + 最近章节事实 + 未收伏笔」组装上下文走统一 AI 网关生成续写草稿，以便产出接得上前文、不丢设定 |
| US-5 | 作为**追求文风的作者**，我希望选中一段正文做「AI 润色」，得到一段新草稿与我原文**并排对照**，我逐段决定采纳，以便保留我的语感、不被 AI 整段替换 |
| US-6 | 作为**长期连载的作者**，我希望每章被采纳为正式后，系统把这章的出场角色、角色状态变化、埋/收伏笔自动写入追踪态，并重建「续写上下文卡 / 时间线 / 角色表」三份只读派生视图，以便写到第 50 章时 AI 还记得第 3 章埋的伏笔 |
| US-7 | 作为**担心生成中断的作者**，我希望 AI 生成过程按 Step 落 checkpoint（上下文组装 → 正文草稿 → 事实快照 → 摄取），中途失败或刷新页面后能看到"失败在第几步"并重试该步，以便不浪费已经生成的部分、也不重跑花钱的上游调用 |
| US-8 | 作为**要交稿/要备份的作者**，我希望把单章或整本书导出为 `.md` 或剥离标记的 `.txt`，以便直接发给编辑或做纯文本存档 |
| US-9 | 作为**没有配置 AI 的作者**，我希望进入工作区就明确看到"AI 网关未配置，续写/润色不可用（去设置页配置）"，但写稿、大纲、导出照常可用，以便我不被一个假装能用的按钮骗到（对应原则 P2） |

---

## 3. MVP 边界

### 3.1 占位页四条承诺的满足方式

| 承诺 | 是否满足 | MVP 满足方式 |
|---|---|---|
| ① 书架与大纲（独立目录 + 大纲树 + 章节 Markdown） | ✅ 满足 | `data/novel/books/<book_id>/` 独立目录；书架列表 + 卷/章两级大纲树（左栏）；章节正文存 `正文/ch-NNN.md` |
| ② 章节编辑器（Markdown，可 git 可 diff） | ✅ 满足 | 纯 `<textarea>` 语义的 Markdown 编辑器 + 防抖自动保存 + 只读预览；文件即数据，天然 diff |
| ③ AI 续写/润色（统一网关，按大纲上下文续写） | ✅ 满足 | 复用 `app/services/ai_provider.py` 的 `generate_ai_text / stream_ai_text`，**不新建 LLM 客户端**；上下文由派生「续写上下文卡」组装 |
| ④ 导出 Markdown / 文本 | ✅ 满足 | 单章 + 全书（按大纲顺序拼接）导出 `.md` / `.txt` |
| ⑤ synergy：本地优先 | ✅ 满足 | 全部产物明文落在 `data/novel/`；`views/` 为**可删可重建**的派生只读视图 |

### 3.2 调研机制取舍（8 条 → MVP / P1 / P2）

| # | 机制 | MVP | 说明 |
|---|---|---|---|
| 1 | 四层目录信息架构 | ✅ **进 MVP（三层 + 设定行）** | MVP 落地 `book.json`（含 `setting_summary` 摘要行）/ `正文/` / `state.json` / `views/`；**完整的设定层卡片编辑降级到 P1-3** |
| 2 | 唯一权威 JSON + 派生只读视图 | ✅ **进 MVP** | `book.json`+`state.json` 是权威，`views/*.md` 是派生（随时可删、`POST /views/rebuild` 重建） |
| 3 | 滚动状态三件套（全局摘要 + 角色状态 + 伏笔） | ✅ **进 MVP** | 每章采纳为正式时回写 `state.json`；三件套是 AI 上下文的地基，不能延后 |
| 4 | 章节事实快照 | ✅ **进 MVP** | AI 生成结构化 `{chars, state_changes, planted, resolved, relations}`，**人工确认后**才并入 `state.json` |
| 5 | Step 级 checkpoint 与断点恢复 | ✅ **进 MVP（轻量版）** | 见 Q1：默认「异步 job + 轮询」，降级方案「同步 + 可重试失败步」，验收口径不变 |
| 6 | 草稿 / 正式两态 | ✅ **进 MVP** | 草稿不进追踪态，人工点「采纳」才摄取 —— 这是原则 P3 的物理保障，不可裁剪 |
| 7 | 上下文分层 + 相关章节推荐 | ⚠️ **进 MVP 的轻量分层，四维推荐降级 P1-1** | MVP 上下文 = 滚动摘要 + 本章细纲/节拍 + 最近 2 章事实快照 + 全部 open 伏笔 + 角色状态；**按伏笔/出场/状态变化/关系的四维打分推荐降级** |
| 8 | 写前门禁 + 写后自检 | ✅ **进 MVP（轻量版）** | 门禁：无细纲 → 续写按钮禁用并说明原因（后端二次校验）；自检：**纯本地正则规则**（疑似截断 / 工程词泄漏 / 高频 AI 味词 / 重复句），只列清单不自动改 |

**机制落#P2 或 Out of Scope 的**：四维推荐(P1-1)、流式输出(P1-2)、设定卡编辑(P1-3)、版本 diff(P1-4)、一致性矛盾检查(P1-5)、导入外部稿件(P1-6)、Docx 导出(P1-7)。

### 3.3 MVP 源文件清单（建议量级：后端 4 + 前端 5 + 测试 3）

| 端 | 文件 | 职责 |
|---|---|---|
| 后端 | `app/services/novel_store.py` | 文件系统事实层：书架 CRUD、大纲树读写、章节 Markdown 读写（**原子替换写**）、state.json 读写、派生视图重建、导出拼装 |
| 后端 | `app/services/novel_ai.py` | 复用 `ai_provider`；上下文卡组装（轻量分层）、续写/润色草稿生成、章节事实快照结构化输出、**自研提示词**（红线见 §6）、写后自检规则集 |
| 后端 | `app/services/novel_jobs.py` | Step 级 checkpoint：job 状态机、每步结果落 `checkpoints/<job_id>.json`、`resume` 从失败步继续 |
| 后端 | `app/api/novel.py` | 薄路由层（`APIRouter(prefix="/api/novel")`），参数校验 + 调 service + 显式 unavailable 语义 |
| 前端 | `src/pages/workspaces/NovelWorkspace.tsx`（新增，从 `workspaces/index.tsx` 的占位实现切换为三栏容器） | 三栏布局 + 状态编排 + 移动端单栏降级 |
| 前端 | `src/components/novel/BookshelfOutlinePanel.tsx` | 左栏：书架 + 卷/章大纲树 + 设定摘要行 |
| 前端 | `src/components/novel/ChapterListPanel.tsx` | 中栏：章节列表 + 状态标签（草稿/正式/AI 草稿待采纳）+ 导出入口 |
| 前端 | `src/components/novel/ChapterEditor.tsx` | 右栏：Markdown 编辑（源码/预览双 tab）+ 自动保存 + 字数统计 |
| 前端 | `src/components/novel/AiDraftPanel.tsx` | AI 面板：续写/润色、上下对照、写后自检清单、采纳、checkpoint 状态与重试 |
| 前端 | `src/lib/novelApi.ts` | API 客户端（复用 `src/lib/api.ts` 的错误/toast 约定） |
| 测试 | `tests/test_novel_store.py` | 目录/文件读写、原子写、大纲排序、派生视图重建幂等、导出拼装 |
| 测试 | `tests/test_novel_ai.py` | 上下文组装、AI unavailable 时的 fail-closed、事实快照 JSON 校验失败不落盘、自检规则命中 |
| 测试 | `tests/test_api_novel.py` | 路由契约、`/status` 语义、拒绝非法 book_id/chapter_id（路径穿越防护） |

> **依赖说明**：MVP **不新增任何第三方依赖**。Markdown 预览用自研极简渲染（`src/components/novel/MarkdownLite.tsx`，支持标题/粗斜体/引用/列表/段落/分割线即可），理由见 Q2。

---

## 4. 需求池

### P0（Must have — 本次必须交付，缺任一条则 Phase 4 不成立）

| ID | 需求名 | 描述 | 验收标准（可测） | 端 |
|---|---|---|---|---|
| P0-1 | 数据层与目录结构 | `data/novel/books/<book_id>/` 落盘；原子替换写（先写 `.tmp` 再 `os.replace`） | ① 建书后磁盘出现 `book.json` + `正文/` + `state.json` + `views/`；② 并发写同一章节 100 次后文件不损坏、内容等于最后一次写入；③ 手工编辑 md 后重新 GET 章节能读到新内容 | 后端 |
| P0-2 | 书架 CRUD | 新建/重命名/删除书籍；书籍列表返回书名、章节数、字数、`updated_at` | ① 增删改后列表与磁盘一致；② 删除书籍连带删除整个目录；③ `book_id` 只允许 `[a-z0-9-]{1,64}`，含 `..` 或 `/` 的请求返回 422 | 前后端 |
| P0-3 | 大纲树（卷/章两级） | 树结构增删改 + 上移/下移排序；节点类型 `volume`/`chapter`；章节节点带 `status` | ① 新增卷/章、改名、删除、排序后 `book.json.outline.nodes` 与 UI 一致；② 删除卷时其子章节一并删除或明确升格（默认：一并删除并二次确认） | 前后端 |
| P0-4 | 章节 Markdown 读写 | 每章对应 `正文/ch-NNN-<slug>.md`；正文与序号从大纲树解析 | ① 打开章节返回 Markdown 原文 + 字数（不含空白）② PUT 正文后文件落盘且 `updated_at` 变更；③ Markdown 原文不做任何转码/重排，字节级保留用户换行习惯 | 前后端 |
| P0-5 | 章节编辑器 | `<textarea>` 编辑 + 源码/预览双 tab + 800ms 防抖自动保存 + 保存状态指示（已保存/保存中/失败） | ① 输入后 ≤1s 内落盘；② 断网/后端 500 时**明示"未保存"**且保留编辑器内容不清空；③ 字数实时更新；④ 预览 tab 启用 `MarkdownLite`，不支持的语法原样显示（诚实） | 前端 |
| P0-6 | 草稿 / 正式两态 | AI 产出写入 `drafts/<draft_id>.md`，**永不覆盖**正文；点「采纳」才 `_adopt` 写入正文并标记 `status=published` | ① AI 生成后正文字节不变；② 采纳后正文 = 草稿全文、`status` 变 `published`；③ 草稿可丢弃；④ **草稿阶段绝不影响 `state.json`**（单元测试断言） | 前后端 |
| P0-7 | AI 能力状态显式化 | `GET /api/novel/status` 返回 `{configured, provider, model, available, reason, code}`，复用 `ai_provider.ai_configured()/current_ai_provider()/current_ai_model()` | ① 未配 Key：按钮禁用 + 文案「AI 网关未配置，续写/润色不可用 · 去设置页配置」；② provider=codex_cli 且 CLI 不可用：文案「Codex CLI 不可用」，不静默放行；③ **任何情况下不返回 mock 生成结果** | 前后端 |
| P0-8 | AI 续写 | 按「滚动摘要 + 本章细纲/节拍 + 最近 2 章事实快照 + open 伏笔 + 角色状态」组装上下文，调 `ai_provider` 生成续写草稿 | ① 在无 AI 环境（pytest monkeypatch `ai_configured=False`）下返回 503 + `code=ai_unavailable`，不抛 500；② 有 AI 时（stub provider）返回的 prompt 中**必须包含**本章细纲文本与至少 1 条 open 伏笔文本（断言，证明上下文真的被用上）；③ 输入超窗时返回明确报错而非静默截断 | 前后端 |
| P0-9 | AI 润色 | 选中正文片段 → 生成改写草稿 → 与原文**左右/上下对照**展示 → 用户决定采纳 | ① 空选区时按钮禁用并提示「请先选中要润色的文本」；② 润色结果以并排 diff 视图展示，原文始终可见；③ 采纳支持"仅替换选中区间" | 前端 + 后端 |
| P0-10 | 章节事实快照 + 滚动状态回写 | 采纳时把 AI 生成的快照 `{chars, state_changes, planted, resolved, relations}` 并入 `state.json`，并更新 `rolling.summary` / `characters` / `foreshadow` | ① 采纳后 `state.json.chapters` 新增一条且 `rolling.summary` 变化；② AI 返回**非法 JSON** → **不写入** `state.json`，返回显式错误并保留草稿供重试或手工填写；②b **口径澄清（主理人裁定，与 ARCHITECTURE §3.4 对齐）**：合法 JSON 但**缺字段**时按字段默认值补齐并判成功 —— 与「手工补录允许只传部分字段」同源（`ChapterFact` 各字段均有默认值）；③ 支持用户**手工补录**章节事实（不强依赖 AI）；④ **硬保证不变**：无论 ② 还是 ②b，快照都**只在 `/adopt` 时**写入 `state.json`，草稿阶段绝不污染追踪态 | 后端 + 前端 |
| P0-11 | 派生只读视图 | `views/context-card.md`（续写上下文卡）、`views/timeline.md`、`views/characters.md`，全部从权威 JSON 生成 | ① 删除 `views/` 后调用 rebuild 可完整重建；② **同一份权威 JSON 连续 rebuild 两次输出字节一致**（幂等）；③ UI 标注「派生视图 · 请勿手工编辑」 | 后端 |
| P0-12 | 写前门禁 + 写后自检 | 门禁：本章 `beat`/`summary` 为空 → 续写禁用 + 后端二次校验返回 `code=missing_beat`；自检：纯本地规则（疑似截断 以句子不完整结尾、工程词泄漏 `json/API/null/undefined`、AI 高频词计数、连续重复句） | ① 无细纲时前端禁用 + 后端拒绝（双重）；② 自检命中项以**清单形式**展示，附命中位置；③ 自检**永不自动改写**正文；④ 规则函数有单测覆盖正负样例 | 前后端 |
| P0-13 | Step 级 checkpoint 与断点恢复 | 每个 AI 任务拆 4 步 `context → draft_text → fact_snapshot → ingest`，每步完成即落 `checkpoints/<job_id>.json`；失败记录失败步与原因 | ① 人为在第 2 步注入异常后，`GET /jobs/{id}` 返回 `failed_step=draft_text` 且前一步产物仍在文件中；② 「重试」从第 2 步开始，不重跑第 1 步（断言 step1 产物未被改写） | 后端 + 前端 |
| P0-14 | 导出 Markdown / 文本 | 单章、整本（按大纲顺序拼接，卷名作为 `#` 分隔）导出 `.md` 与 `.txt`（txt 剥离 `# > ** -` 等行内标记） | ① 单章导出字节等于磁盘 md 原文；② 全书 md 导出章节顺序与大纲树一致；③ txt 导出中不含 `#`、`**`、`` ` `` 残留 | 前后端 |
| P0-15 | 工程纪律与回归 | 不改 `WorkspaceShell.tsx` 公共契约；不破坏其它工作区；新增后端单测；`tsc --noEmit` 通过；`ruff` 通过（口径含 tests）；pytest 全量（基线 2423）无新增失败 | ① 上述四道检查命令各自退出码 0；② 抽查股票/港股/热点页在主流程无报错（冒烟） | 前后端 |

### P1（Should have — 本次不做，下一迭代）

| ID | 需求 | 一句话描述 | 端 |
|---|---|---|---|
| P1-1 | 四维相关章节推荐 | 按「伏笔相关 / 角色出场 / 状态变化 / 关系变化」打分选 Top-K 章节进上下文，替代 MVP 的"最近 2 章" | 后端 |
| P1-2 | 流式输出 | 前端 SSE 承接 `stream_ai_text`，逐段显示草稿 + 可中途停止（保留已生成部分） | 前后端 |
| P1-3 | 设定层结构化编辑 | 人物卡 / 世界观卡 / 势力卡，作为可引用的一等实体进 UI | 前后端 |
| P1-4 | 章节版本与 Diff | 每次采纳生成 `versions/ch-NNN-vN.md`，支持两版本并排 diff（呼应"可 git 可 diff"） | 后端 + 前端 |
| P1-5 | 一致性检查 | 规则化检测：时间线矛盾、伏笔长期未收、角色状态与出场冲突（提醒不阻断） | 后端 |
| P1-6 | 导入既有稿件 | 导入本地 Markdown 文件夹/散文件，按文件名猜章节顺序并生成大纲树 | 后端 + 前端 |
| P1-7 | 导出增强 | 带 YAML front matter、Docx（需评估依赖）、按卷分文件 zip | 后端 |
| P1-8 | 示例书籍一键生成 | 空书架时提供「创建示例书（1 卷 3 章 + 已填追踪态）」，用于演示一致性机制 | 后端 + 前端 |

### P2（Nice to have）

| ID | 需求 | 一句话描述 |
|---|---|---|
| P2-1 | 封面资产引用 | 从图片工作区资产库引用封面（跨域联动，待图片域落地） |
| P2-2 | 字段级生成校验（卡片 Schema） | 事实快照按 Schema 校验 + 字段级定位错误提示（源自 NovelForge 概念） |
| P2-3 | 写作统计与目标 | 日更字数、连更天数、目标进度条（本地统计，不联网） |
| P2-4 | 移动端/键盘优化 | <768px 单栏分段控件、编辑器快捷键（加粗/标题） |

### Out of Scope（明确不做）

- 向量库 / RAG / Embedding 检索
- 知识图谱可视化
- 全自动无人干预整本生成
- 多智能体编排框架（LangGraph / AutoGen 类）
- 扫榜、市场雷达、封面生成、衍生工坊
- 引入重型第三方依赖（前端/后端均克制）
- 多人协作 / 实时协同编辑（本地单机定位）

---

## 5. 信息架构与数据设计

### 5.1 目录布局

```
data/novel/                          # 与 settings.data_dir 对齐；data/** 已被 .gitignore 忽略
└── books/
    └── <book_id>/                   # 例：book-2026-001（正则 ^[a-z0-9-]{1,64}$）
        ├── book.json                # 【权威】元数据 + 大纲树（唯一结构化权威的一部分）
        ├── state.json               # 【权威】追踪态：滚动摘要 / 角色状态 / 伏笔 / 章节事实快照
        ├── 正文/                    # 【权威】每章一个 Markdown 明文文件
        │   ├── ch-001-yinzi.md
        │   └── ch-002-yuye.md
        ├── drafts/                  # 草稿区：AI 产出，未采纳 = 不污染任何权威数据
        │   └── d-<timestamp>-continue.md
        ├── checkpoints/             # Step 级断点：崩溃/刷新后可恢复
        │   └── <job_id>.json
        └── views/                   # 【派生只读】可删除、可重建（rebuild 幂等）
            ├── context-card.md      # 续写上下文卡
            ├── timeline.md          # 时间线
            └── characters.md        # 角色表
```

**中文目录名（`正文/`）的取舍**：可读性强，符合"任何编辑器都能打开"的直觉；但 Windows 下需注意编码（Python 一律 `encoding='utf-8'`）。**默认建议采用中文目录名**，理由：用户对 `data/novel/books/xxx/正文/` 的自解释性远胜 `text/`；若实现中出现编码问题，退化为英文目录（`chapters/`）不影响其它设计。

### 5.2 `book.json`（草案 · 字段级）

```jsonc
{
  "version": 3,                        // 乐观锁：写前比对，冲突返回 409 而非静默覆盖
  "id": "book-2026-001",
  "title": "星海黎明",
  "author": "",
  "genre": "科幻",
  "pov": "第三人称限知",
  "tense": "过去时",
  "setting_summary": "星海历 302 年，人类依托跃迁航道扩张；主角陆昭为星港工程师……",  // 设定层 MVP 形态：单行摘要
  "created_at": "2026-10-01T10:00:00+08:00",
  "updated_at": "2026-10-02T22:10:00+08:00",
  "outline": {
    "nodes": [
      {
        "id": "v1", "type": "volume", "title": "第一卷 破晓", "order": 1,
        "children": [
          {
            "id": "ch-001", "type": "chapter", "title": "引子", "order": 1,
            "status": "published",              // draft | ai_draft | published
            "word_target": 2000,
            "summary": "冷开场：陆昭在废弃船坞发现黑匣子",
            "beat": "埋下黑匣子来源的伏笔，确立主角技术直觉",   // 写前门禁的判据
            "file": "正文/ch-001-yinzi.md",
            "word_count": 1842
          }
        ]
      }
    ]
  }
}
```

### 5.3 `state.json`（草案 · 字段级）

```jsonc
{
  "book_id": "book-2026-001",
  "updated_at": "2026-10-02T22:10:00+08:00",
  "rolling": {
    "summary": "前情提要：陆昭捡到黑匣子后被卷入星港走私案……",  // 全局滚动摘要（人工可改）
    "updated_chapter": "ch-002"
  },
  "characters": {
    "陆昭": { "status": "轻伤住院", "location": "星港医院", "last_seen_chapter": "ch-002",
              "traits": ["技术直觉强", "不善言辞"] },
    "白露": { "status": "行踪不明", "location": "旧城区", "last_seen_chapter": "ch-002", "traits": [] }
  },
  "foreshadow": [
    { "id": "f-001", "text": "黑匣子的真实来源", "planted_chapter": "ch-001",
      "status": "open", "resolved_chapter": null }        // open | resolved
  ],
  "chapters": [                                            // 章节事实快照（每章一条）
    {
      "id": "ch-002", "title": "雨夜",
      "chars": ["陆昭", "白露"],
      "state_changes": ["陆昭从健康→轻伤住院"],
      "planted": ["f-002"], "resolved": [],
      "relations": [{ "from": "陆昭", "to": "白露", "delta": "信任开始动摇" }],
      "source": "ai",                                      // ai | manual
      "adopted_at": "2026-10-02T22:10:00+08:00"
    }
  ]
}
```

### 5.4 `checkpoints/<job_id>.json`（草案）

```jsonc
{
  "job_id": "job-20261002-221000-ch002",
  "book_id": "book-2026-001", "chapter_id": "ch-002",
  "mode": "continue",                                   // continue | polish
  "created_at": "...", "updated_at": "...",
  "steps": [
    { "name": "context",         "status": "done",   "at": "..." },
    { "name": "draft_text",      "status": "done",   "at": "..." },
    { "name": "fact_snapshot",   "status": "failed", "at": "...", "error": "AI 返回非法 JSON: Expecting value" },
    { "name": "ingest",          "status": "pending", "at": null }
  ],
  "artifacts": { "context_card_md": "…", "draft_text": "…", "fact_json": null },
  "status": "failed",            // running | failed | done
  "failed_step": "fact_snapshot"
}
```

> **幂等约定**：`resume` 时 `status=done` 的 step **跳过重跑**（P0-13 验收点），仅从 `failed`/`pending` 的第一 step 继续。

### 5.5 API 草案（薄路由层，最终以前端实际消费为准）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/novel/status` | AI 网关可用性 + provider/model 显式状态（fail-closed 入口） |
| GET/POST | `/api/novel/books` | 书架列表 / 建书 |
| PATCH/DELETE | `/api/novel/books/{book_id}` | 改名 / 删除 |
| GET/PUT | `/api/novel/books/{book_id}/outline` | 大纲树读 / 全量保存（带 `version` 乐观锁） |
| GET | `/api/novel/books/{book_id}/chapters` | 章节列表（含字数、状态） |
| GET/PUT | `/api/novel/books/{book_id}/chapters/{chapter_id}` | 读正文 / 写正文（自动保存） |
| GET | `/api/novel/books/{book_id}/state` | 追踪态（滚动摘要/角色/伏笔/事实快照） |
| GET | `/api/novel/books/{book_id}/views/{name}` | 派生只读视图（`context-card`/`timeline`/`characters`） |
| POST | `/api/novel/books/{book_id}/views/rebuild` | 由权威 JSON 重建全部派生视图（幂等） |
| POST | `/api/novel/books/{book_id}/chapters/{chapter_id}/ai/draft` | 启动 AI 任务（continue/polish），返回 `job_id` |
| GET | `/api/novel/jobs/{job_id}` | 轮询进度 / 失败步 / 产物 |
| POST | `/api/novel/jobs/{job_id}/resume` | 从失败步重试 |
| POST | `/api/novel/books/{book_id}/chapters/{chapter_id}/adopt` | 草稿转正式 + 摄取事实快照 + 重建视图 |
| POST | `/api/novel/books/{book_id}/lint` | 写后自检（纯本地规则，返回命中清单） |
| GET | `/api/novel/books/{book_id}/export` | `?format=md\|txt&scope=chapter\|book&chapter_id=` |

> **降级路径（见 Q1）**：若异步 job 实现成本超预期，`/ai/draft` 可改为同步返回 `{ok, steps, artifacts, failed_step}`，前端直接展示失败步并提供重试；**checkpoint 文件与验收标准不变**。

---

## 6. 约束（工程与合规）

| 约束 | 说明 |
|---|---|
| **许可证红线** | AGPL/GPL 项目（QMAI、AI_NovelGenerator、NovelForge、ReNovel-AI）**只借鉴概念与信息架构，不搬运代码、不复用提示词原文**。可安全参考实现的仅 `oh-story-claudecode`（MIT）与 `ainovel-cli`（Apache-2.0）。所有提示词**自研**，且需在文件头注明"自研，未复制第三方提示词"。新文件头部 license 与宿主项目一致 |
| **统一 AI 通道** | 必须复用 `app/services/ai_provider.py`（`generate_ai_text` / `stream_ai_text` / `ai_configured` / `current_ai_provider` / `current_ai_model`），**禁止新建独立 LLM 客户端**、禁止绕过 `secrets_store` 自读 Key |
| **本地优先** | 全部数据落在 `settings.data_dir/novel/`，明文 `.md` / `.json`；`.tmp` + `os.replace` 原子写；读取每次走磁盘（进程内只允许短 TTL 派生缓存，权威数据不缓存） |
| **诚实不可用** | 端点返回显式 `{ok, available, reason, code}`；前端对 unavailable 状态使用统一文案组件，**禁止空列表冒充成功、禁止 mock 生成**；写文件失败必须回传错误而非吞异常 |
| **不做数值评分承诺** | 不产出章节评分/质量分/推荐指数；质量反馈一律用规则命中清单表达 |
| **依赖克制** | MVP 零新增第三方依赖。任何新增依赖需在变更前说明理由 + 体积/许可证评估，并同步更新 `pyproject.toml` / `package.json` 与该 PRD |
| **路径安全** | `book_id` / `chapter_id` 必须正则校验并做目录穿越防护（`Path(root) / name` 后校验 `.resolve()` 前缀） |
| **壳契约零侵入** | 不修改 `WorkspaceShell.tsx` 的 `WORKSPACES` 与 props 契约；小说 UI 只作为 `/novel` 路由子树挂载。若有需求也只允许**纯增量**改动，不得影响其它工作区 |
| **测试与 lint** | 新增功能必带 pytest 单测；`ruff` 口径含 tests；前端 `tsc --noEmit` 必须通过；pytest 基线 2423 条不得新增失败 |
| **UI 语言与主题** | 全中文；浅色（light）优先，深色变量已存在需同步支持。遵循 token：`bg-base/bg-surface/bg-elevated`、`border-border`、`text-foreground/text-secondary/text-muted`、`rounded-card/rounded-btn`、`accent`；小说域强调色沿用 `#22c55e` 仅用于图标/状态点，**不用于大面积背景** |

---

## 7. UI 设计稿描述

### 7.1 总体说明

- 挂在 `WorkspaceShell` 的 `<Outlet />` 内（左侧 56px Rail 已在壳里，**小说区不再占用导航宽度**）。
- 桌面 ≥1280px：**三栏**（书架+大纲 / 章节列表 / 编辑器）；1024–1280px：三栏收窄，左栏可折叠为图标条；<768px：**单栏 + 顶部分段控件**（书架 / 章节 / 编辑）。
- 容器 `min-h-0 flex-1 overflow-hidden`，每栏内部独立滚动 —— 不出现整页滚动条。
- 域色 `#22c55e` 只用于： Rail 图标激活态（壳已处理）、章节状态点、AI 面板标题图标。文字语义色一律用 `text-foreground / text-secondary / text-muted`。

### 7.2 桌面三栏 ASCII 线框

```
┌──┬─────────────────────────────┬──────────────────────────┬────────────────────────────────────────────┐
│  │ ① 书架 / 大纲树  w-60       │ ② 章节列表  w-72         │ ③ 章节编辑器  flex-1                        │
│R │ bg-surface                  │ bg-base                  │ bg-surface                                 │
│a │ border-r border-border      │ border-r border-border   │                                            │
│i │                             │                          │                                            │
│l │ ┌ 书籍 ─────────── [+ 新建]┐│┌ 第1卷 破晓 ─── 3章/5.9k┐│┌ 02 雨夜 · 正式 ──── 💾已保存 [⋯] ────────┐│
│  │ │ ▾ 星海黎明  3章/5,921字 │││ ┌ 01 引子 ────────────┐│││ 节点：第一卷 破晓 › 02 雨夜                 ││
│5 │ │ ▸ 未名之书  0章         │││ │ ● 正式   1,842字    ││││ 本节节拍：陆昭受伤并与白露关系转冷         ││
│6 │ └─────────────────────────┘││ │ v3 · 10-02 22:10    ││││ 大纲细纲：雨夜追击中黑匣子被夺走           ││
│p │ ┌ 大纲 ────────── [+卷][+章]││ └──────────────────────┘│├─── [源码] [预览] ─────────── 1,842 字 ───┤│
│x │ │ ▾ ▸ 第一卷 破晓   3章    │││ ┌ 02 雨夜 ────────────┐│││ # 雨夜                                     ││
│  │ │   ├ ● 01 引子     正式  │││ │ ● 正式   2,051字    ││││                                            ││
│  │ │   ├ ● 02 雨夜     正式  │││ │ v1 · 10-02 23:41    ││││ 雨下了整夜，星港的霓虹在水面碎成一片……     ││
│  │ │   └ ◐ 03 密会     AI草稿│││ └──────────────────────┘│││                                            ││
│  │ │ ▾ ▸ 第二卷 潮涌   1章    │││ ┌ 03 密会 ────────────┐│││                                            ││
│  │ │   └ ○ 04 潮汐     仅大纲│││ │ ◐ AI草稿待采纳       ││││                                            ││
│  │ └─────────────────────────┘││ │ 1,120字 · 12分钟前   ││││                                            ││
│  │ ┌ 设定摘要 ───────────────┐││ └──────────────────────┘│├────────────────────────────────────────────┤│
│  │ │ 星海历 302 年 · 第三人称│││ ─ 导出 ────────────────│││ ▸ AI 面板                              [▾]││
│  │ │ 主角：陆昭（星港工程师）│││ 单章 [.md] [.txt]      │││ ① 上下文卡 ②续写 ③润色选中 ④写后自检     ││
│  │ │ 2 角色 · 3 伏笔(1 未收) │││ 全书 [.md] [.txt]      │││ [续写本章]                                 ││
│  │ └─────────────────────────┘││ [查看续写上下文卡]      │││ ⚠ AI 网关未配置 → 按钮禁用 + 原因文案     ││
│  │                             ││                          ││└────────────────────────────────────────────┘│
└──┴─────────────────────────────┴──────────────────────────┴────────────────────────────────────────────┘
 状态点图例： ○ 仅大纲   ◐ AI草稿待采纳   ● 正式
```

### 7.3 各区域内容与交互

**① 书架 / 大纲树（左栏 `w-60`，可折叠）**
- 顶部：`书籍` 标题 + `[+ 新建]`；列表项显示书名 + `N章/字数`；当前书高亮（`bg-elevated` + 左侧 2px `#22c55e` 指示条）。
- 大纲区：树形（卷 → 章），卷可展开/收起，章节点右侧状态点（`○/◐/●`，`title` 属性给中文说明）。
- 每章 hover 出 `[⋯]`：重命名 / 删除 / 上移下移 / 编辑细纲。
- 底部「设定摘要」卡：`setting_summary` 单行 + `N 角色 · M 伏笔(K 未收)` 的只读统计（点开跳 `views/characters.md` 只读抽屉）。
- 空态：无书时显示「还没有书 — [新建第一本书]」，并明示数据将写在 `data/novel/books/`（兑现 synergy 承诺）。

**② 章节列表（中栏 `w-72`）**
- 按当前选中卷过滤（默认「全部」），每条是一张卡：章序号 + 标题 + 状态点 + 字数 + `版本号 · 更新时间`。
- 有 AI 草稿的章节在卡片上额外显示一条「AI草稿待采纳 · N字 · X分钟前」的次级条，点击进入编辑器并自动展开 AI 面板定位到草稿。
- 底部「导出」区：单章 `.md/.txt`、全书 `.md/.txt`；「查看续写上下文卡」打开只读抽屉显示 `views/context-card.md`，并标注「派生视图 · 请勿手工编辑 · [重新生成]」。

**③ 章节编辑器（右栏 `flex-1`）**
- 头部：章序号 + 标题 + 状态点 + 💾保存状态徽标（`已保存/保存中/未保存（原因）`）+ `[⋯]` 菜单（导出、查看历史 P1、删除）。
- 头部第二行：**本节节拍 / 大纲细纲** 只读展示 —— 让"按大纲写作"在视觉上成立；细纲为空时显示黄条提示「本章细纲为空，AI 续写已禁用（填写细纲后启用）」（P0-12 门禁的可视化）。
- 主体：`[源码] [预览]` 双 tab + 右上角字数。源码 tab 为等宽字体 `<textarea>`（`font-mono`，行高宽松）；预览 tab 用 `MarkdownLite` 只读渲染，**不支持的语法原样呈现**（诚实）。
- 底部 AI 面板（可折叠，默认展开）：
  - 四个区块：① 上下文卡（折叠显示本次将喂给 AI 的上下文摘要，可「复制」）② 续写 ③ 润色选中 ④ 写后自检。
  - AI 不可用时：所有按钮禁用 + 统一 unavailable 文案条（见 P0-7）。
  - 生成中：进度按 Step 显示（`① 组装上下文 ✓ → ② 生成正文 … → ③ 事实快照 → ④ 摄取`），背后就是 checkpoint 状态机。
  - 完成后：**左右并排**原文 ↔ 草稿，支持"采纳"（针对润色是"仅替换选中区间"）。

**移动端（<768px）**：顶部三段式分段控件「书架 / 章节 / 编辑」，同一时刻只渲染一栏，全宽 + `pb-14` 避让壳的 Tab Bar。编辑器全屏时自动折叠AI 面板。

### 7.4 关键状态文案（诚实不可用，禁止空状态冒充成功）

| 场景 | 展示文案 |
|---|---|
| 未配置 AI Key | 「AI 网关未配置 — 续写/润色不可用。前往「设置 · AI」配置后自动解锁。写稿、大纲、导出不受影响。」 |
| provider=codex_cli 但 CLI 不可用 | 「Codex CLI 未就绪（未检测到可用命令）— 续写/润色不可用。」 |
| AI 调用失败 | 「本次生成失败：<模型/网关返回的简明错误>。已完成的步骤：<…>；[重试该步骤]」（并保留已生成内容） |
| 事实快照解析失败 | 「AI 返回的事实快照无法解析，未写入追踪态 — 正文不受影响。[重试] [手工补录]」 |
| 写后自检命中 | 「自检提醒 3 项：结尾疑似截断（末段）；出现工程词 "null"（第 42 行）；"不由得" 出现 4 次。 — 仅提醒，未改写你的文字。」 |
| 文件写入失败 | 「保存失败：<原因>。你的改动仍在编辑器中，未落盘 — [重试保存] [复制全文备份]」 |

---

## 8. 待确认问题（含默认建议）

| # | 问题 | 选项 | **我的默认建议** |
|---|---|---|---|
| **Q1** | AI 任务编排方式：异步 job + 轮询 `GET /jobs/{id}`（可跨刷新恢复、支持长文本），还是同步请求 + 失败步重试（实现更简单、无需任务基础设施）？ | A. 异步 job + 轮询<br>B. 同步 + 断点重试 | **默认 A**（长文本常超 30s，浏览器刷新会丢），**若实现成本超预期降级 B，checkpoint 文件结构与 P0-13 验收标准保持不变** |
| **Q2** | Markdown 预览是否需要引入 `react-markdown`（+`remark-gfm`）新依赖？ | A. 不引入，自研极简渲染 `MarkdownLite`<br>B. 引入，支持 GFM（表格/脚注/删除线） | **默认 A**：MVP 编辑器应以源码为尊，预览只是辅助；引入新依赖需评估体积与许可证。若确需 GFM 表格，再走依赖评审 |
| **Q3** | 「设定层」是否进 MVP UI？ | A. 仅 `setting_summary` 单行摘要（进 UI 只读展示）<br>B. 完整人物卡/世界观卡结构化编辑 | **默认 A**：保住 MVP 体量；结构化设定卡降到 P1-3，但**数据结构预留**（`state.json.characters` 已存在） |
| **Q4** | 写前门禁的强度：本章无细纲时是**硬禁用**续写，还是软提示放行？ | A. 硬禁用（前后端双重）<br>B. 软提醒仍可生成 | **默认 A**：这是本次调研中成本最低、收益最高的一致性机制；但**提供"跳过本次"的显式入口**（用户知情下可绕过，不偷偷放款） |
| **Q5** | 导出范围： | A. 单章 + 全书（md/txt）<br>B. 仅单章 | **默认 A**：全书拼接只是按大纲顺序读文件拼字符串，成本极低，而"导出备份"是用户最高频的安心来源 |
| **Q6** | `data/**` 已被 `.gitignore` 忽略 —— 小说稿**默认不入库**。是否需要提供「把某本书加入版本控制」的引导？ | A. 不提供，仅如实告知路径<br>B. 提供 `git init/追踪` 指引 | **默认 A**：与现有数据纪律一致（"git pull 物理上无法影响用户本地 data"）。仅在 UI 空态**如实展示绝对路径**，让用户自行决定是否用外部工具纳管 |

---

## 附一：参考来源与借鉴边界（留档，便于将来分发/上线时自证独立实现）

宿主项目 `tsp-fresh` 自身为 **MIT**。本节记录小说工作区在设计中参考了哪些开源项目、以及借鉴到什么层次。

| 层次 | 说明 | 本次是否涉及 |
|---|---|---|
| 阅读源码学习方法与机制 | 不受许可证约束（版权保护"表达"，不保护"思想/方法/架构"） | ✅ 涉及（7 个项目） |
| 借鉴概念与信息架构（用自己的语言重新实现） | 不受许可证约束 | ✅ 涉及（四层目录、唯一权威+派生视图、滚动状态三件套、Step 级 checkpoint、草稿/正式两态） |
| 结构/命名/组织高度雷同的"翻译式重写" | 灰色地带，可能被认定为衍生作品 | ❌ 未涉及 |
| 复制源码、复制提示词原文、复制模板/配置原文 | 明确不可 | ❌ 未涉及 |

- 参考项目位于 `参考项目/xiao_shuo/`：`oh-story-claudecode`（MIT）、`ainovel-cli`（Apache-2.0）为可安全参考实现的对象；`QMAI`、`ReNovel-AI`（GPL-3.0）与 `AI_NovelGenerator`、`NovelForge`（AGPL-3.0）及一个非标准双许可项目**仅限概念借鉴**。
- 全部提示词自研，文件头注明「自研，未复制第三方提示词」（见 `backend/app/services/novel_ai.py`）。
- 用途定位：个人自用/学习。AGPL/GPL 的源码开放义务触发于**分发**或**对外提供网络服务**，纯自用不触发；但若将来要分发或上线为服务，请以本节为界自查，避免混入任何 AGPL/GPL 来源的代码或文本表达。

## 附：验收门禁 Checklist（交付前逐条打勾）

- [ ] 占位页四条承诺 + synergy 全部可演示
- [ ] `data/novel/books/<id>/` 下文件可被记事本/VSCode 直接打开编辑并被 UI 读到
- [ ] 未配置 AI 时：无假生成、无空结果冒充成功，文案明确
- [ ] AI 草稿不采纳则 `state.json` 字节不变（单测断言）
- [ ] `views/` 删除后可幂等重建
- [ ] 第 2 步注入失败后 resume 不重跑第 1 步（单测断言）
- [ ] 路径穿越 payload（`../`, `/abs`）被拒
- [ ] `pytest` 全量无新增失败；`ruff` 通过；`tsc --noEmit` 通过
- [ ] 股票/港股/热点工作区冒烟无回归
