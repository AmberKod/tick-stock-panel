"""通用热点新闻流 — RSS 聚合，**不需要任何 API Key**。

为什么要有这条路:
    热点页原本只有"行情聚合出的题材热度"和 Anspire 联网检索, 两者都是**股票视角**。
    用户要的"热点新闻"不止股票 —— 国际/国内/科技/财经/能源都算。RSS 是唯一
    既免费又能覆盖这些维度的路子(Anspire 这类搜索源按次计费, 不适合做常驻流)。

源清单按本机实测可达组织(2026-09-19 复测, 见 deliverables/news-workspace-general):
    可用: BBC / Guardian / NPR / Al Jazeera / 中新网(国际·滚动·社会·财经) / 人民网 / 新浪(财经) /
          36氪 / IT之家 / 钛媒体 / cnBeta / TechCrunch / The Verge / Ars Technica /
          CNBC / Yahoo Finance / OilPrice / Mining.com / Investing.com / The Hacker News / Krebs
    不通(实测): DW / Reuters / FreeBuf(XML 坏) / CISA / 虎嗅(读超时) / 新浪国内(404) /
                央视(404) / 生意社 —— 不写进清单, 写进去只会让用户看到一堆"加载失败"。
    已剔除: 中新网能源(空频道, XML 里没有 item, 不是时间窗问题); 新浪新闻焦点(只返 1 条)。

**网络前提(重要)**: 本机靠 ``HTTPS_PROXY=127.0.0.1:7897`` 出海。实测**关掉代理后
BBC/Guardian/AlJazeera/TheHackerNews/Yahoo/Investing 全部连接超时**, 国内源不受影响。
所以: 代理挂了 → 境外源整片失败(前端按 fail-closed 显示 source_errors), 这不是代码 bug。
排查"新闻全空"时**先确认代理活着**, 别去改源清单 —— 2026-09-19 就因为代理抖动
(ECONNREFUSED 7897) 把一批好源误判成不可达, 白剔了一轮。

设计:
    - 不引新依赖: 用 stdlib ``xml.etree.ElementTree`` 同时解 RSS 2.0 与 Atom。
    - 并发抓取(8 线程) + 单源超时, 一个源挂了不影响其他源。
    - fail-closed: 抓不到的源进 ``source_errors`` 明示, 绝不返回空列表冒充"今天没新闻"。
    - 去重: 标题归一化(去空白/标点) + 链接双键, 同一条新闻多源重复只留最新。
    - TTL 缓存(默认 300s): 新闻流不需要秒级刷新, 别把源站当轮询目标。
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# 单源抓取超时 / 并发 / 缓存
FETCH_TIMEOUT_S = 10.0
MAX_WORKERS = 8
CACHE_TTL_S = 300.0
MAX_WORKERS_DEFAULT = MAX_WORKERS

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)


@dataclass(frozen=True)
class Feed:
    """单个 RSS 源。"""

    name: str
    url: str
    #: 语言提示, 仅用于前端展示标记, 不参与逻辑
    lang: str = "zh"
    #: 个别源(如 36氪)偶发慢, 单独放宽超时; 默认见 FETCH_TIMEOUT_S
    timeout: float = FETCH_TIMEOUT_S


@dataclass(frozen=True)
class Category:
    """一个新闻分类。"""

    key: str
    label: str
    feeds: tuple[Feed, ...] = ()
    #: rss = 本模块聚合; search = 走外部检索源(见 services/news_search.py)
    kind: str = "rss"
    note: str = ""


CATEGORIES: dict[str, Category] = {
    "world": Category(
        key="world",
        label="国际要闻",
        feeds=(
            Feed("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", lang="en"),
            Feed("Guardian World", "https://www.theguardian.com/world/rss", lang="en"),
            Feed("NPR World", "https://feeds.npr.org/1004/rss.xml", lang="en"),
            Feed("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", lang="en"),
            Feed("中新网·国际", "https://www.chinanews.com.cn/rss/world.xml"),
        ),
        note="境外源需走本机代理(HTTPS_PROXY); 代理不可用时这几源会整片失败",
    ),
    "cn": Category(
        key="cn",
        label="国内",
        feeds=(
            Feed("中新网·滚动", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
            Feed("中新网·社会", "https://www.chinanews.com.cn/rss/society.xml"),
            Feed("人民网·时政", "http://www.people.com.cn/rss/politics.xml"),
        ),
    ),
    "tech": Category(
        key="tech",
        label="科技·AI",
        feeds=(
            # 36氪偶发 8s 以上才响应, 单独放宽到 15s; 且它不给 pubDate, 排序靠后
            Feed("36氪", "https://www.36kr.com/feed", timeout=15.0),
            Feed("IT之家", "https://www.ithome.com/rss/"),
            Feed("钛媒体", "https://www.tmtpost.com/rss.xml"),
            Feed("cnBeta", "https://www.cnbeta.com.tw/backend.php"),
            Feed("TechCrunch", "https://techcrunch.com/feed/", lang="en"),
            Feed("The Verge", "https://www.theverge.com/rss/index.xml", lang="en"),
            Feed("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", lang="en"),
        ),
        note="新浪科技 RSS 仍挂着 2018 年的旧内容, 不接入; 虎嗅读超时, 不接入",
    ),
    "finance": Category(
        key="finance",
        label="财经·市场",
        feeds=(
            Feed("新浪财经", "https://rss.sina.com.cn/finance/rollnews.xml"),
            Feed("中新网·财经", "https://www.chinanews.com.cn/rss/finance.xml"),
            Feed("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                         "?partnerId=wrss01&id=100003114", lang="en"),
            Feed("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", lang="en"),
        ),
    ),
    "energy": Category(
        key="energy",
        label="能源·大宗",
        feeds=(
            Feed("OilPrice", "https://oilprice.com/rss/main", lang="en"),
            Feed("Mining.com", "https://www.mining.com/feed/", lang="en"),
            Feed("Investing·大宗", "https://www.investing.com/rss/commodities.rss", lang="en"),
        ),
        note="中新网能源 RSS 是空频道(无 item)已剔除; 生意社不可达",
    ),
    "security": Category(
        key="security",
        label="安全·故障",
        feeds=(
            Feed("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", lang="en"),
            Feed("Krebs on Security", "https://krebsonsecurity.com/feed/", lang="en"),
        ),
        note="FreeBuf RSS 的 XML 坏了、CISA 连接失败, 均未接入",
    ),
    "market": Category(
        key="market",
        label="股市·个股",
        kind="search",
        note="走 Anspire 检索(需配 Key): 题材 / 自选股 / 自由搜索 —— 股票视角那一维",
    ),
}

CATEGORY_ORDER = ("world", "cn", "tech", "finance", "energy", "security", "market")


@dataclass
class NewsEntry:
    title: str
    url: str
    source: str
    published_at: str | None  # ISO8601 (UTC) 或 None(源没给时间)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "summary": self.summary,
        }


@dataclass
class FeedResult:
    category: str
    label: str
    kind: str = "rss"
    entries: list[NewsEntry] = field(default_factory=list)
    source_errors: list[str] = field(default_factory=list)
    source_count: int = 0
    ok_source_count: int = 0
    fetched_at: str = ""
    elapsed_s: float = 0.0
    cached: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label": self.label,
            "kind": self.kind,
            "entries": [e.to_dict() for e in self.entries],
            "entry_count": len(self.entries),
            "source_errors": list(self.source_errors),
            "source_count": self.source_count,
            "ok_source_count": self.ok_source_count,
            "fetched_at": self.fetched_at,
            "elapsed_s": round(self.elapsed_s, 3),
            "cached": self.cached,
            "note": self.note,
            # 没抓到任何源时前端要能区分"没新闻"和"全挂了"
            "success": self.kind == "search" or self.ok_source_count > 0,
        }


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_time(raw: str | None) -> datetime | None:
    """RSS pubDate(RFC822) / Atom updated(RFC3339) → aware datetime(UTC)。"""
    if not raw:
        return None
    text = raw.strip()
    # RFC822: Tue, 16 Sep 2026 08:00:00 GMT
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    # RFC3339 / ISO: 2026-09-16T08:00:00Z 或 +08:00
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _clean(text: str | None, limit: int = 240) -> str:
    """去标签/折叠空白, 摘要截断。"""
    if not text:
        return ""
    s = re.sub(r"<[^>]+>", " ", text)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _norm_title(title: str) -> str:
    """标题归一化: 去标点空白小写, 用于多源去重。"""
    return _PUNCT_RE.sub("", title).lower()


def _first(node: ET.Element, *tags: str) -> ET.Element | None:
    for t in tags:
        found = node.find(t)
        if found is not None:
            return found
    return None


def parse_feed(xml_bytes: bytes, source: str) -> list[NewsEntry]:
    """解析 RSS 2.0 / Atom → 条目列表。解析失败返回空(由调用方记 error)。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"XML 解析失败: {exc}") from exc

    out: list[NewsEntry] = []

    # RSS 2.0: channel/item
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title and not link:
            continue
        raw_time = item.findtext("pubDate") or item.findtext("published") or item.findtext("date")
        dt = _parse_time(raw_time)
        summary = item.findtext("description") or item.findtext("summary") or ""
        out.append(NewsEntry(
            title=title or "(无标题)",
            url=link,
            source=source,
            published_at=dt.astimezone(UTC).isoformat() if dt else None,
            summary=_clean(summary),
        ))

    # Atom: entry
    for entry in root.iter(f"{_ATOM_NS}entry"):
        title = (entry.findtext(f"{_ATOM_NS}title") or "").strip()
        link_node = _first(entry, f"{_ATOM_NS}link")
        link = ""
        if link_node is not None:
            link = (link_node.get("href") or link_node.text or "").strip()
        if not title and not link:
            continue
        raw_time = entry.findtext(f"{_ATOM_NS}updated") or entry.findtext(f"{_ATOM_NS}published")
        dt = _parse_time(raw_time)
        summary = entry.findtext(f"{_ATOM_NS}summary") or entry.findtext(f"{_ATOM_NS}content") or ""
        out.append(NewsEntry(
            title=title or "(无标题)",
            url=link,
            source=source,
            published_at=dt.astimezone(UTC).isoformat() if dt else None,
            summary=_clean(summary),
        ))

    return out


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------


def _fetch_one(feed: Feed, timeout: float | None = None) -> tuple[list[NewsEntry], str | None]:
    """抓单个源。返回 (条目, 错误或 None)。"""
    try:
        resp = httpx.get(
            feed.url,
            timeout=timeout or feed.timeout or FETCH_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TickStockPanel/1.0)"},
        )
        resp.raise_for_status()
    except Exception as exc:  # 网络层任何异常都按源失败处理
        return [], f"{feed.name}: {type(exc).__name__}"
    try:
        return parse_feed(resp.content, feed.name), None
    except ValueError as exc:
        return [], f"{feed.name}: {exc}"


def _within(entry: NewsEntry, cutoff: datetime | None) -> bool:
    if cutoff is None or not entry.published_at:
        return True  # 源没给时间的不瞎猜, 保留
    try:
        return datetime.fromisoformat(entry.published_at) >= cutoff
    except ValueError:
        return True


def fetch_category(
    category: str,
    *,
    hours: int = 48,
    limit: int = 40,
    timeout: float | None = None,
    use_cache: bool = True,
) -> FeedResult:
    """抓一个分类的新闻流。

    ``timeout`` 为 None 时每个源用自己的超时(见 Feed.timeout), 便于给慢源单独放宽。
    """
    """抓一个分类的新闻流。

    fail-closed: 全挂了就在 ``source_errors`` 里写明原因, 不返回"看起来正常的空列表"。
    """
    cat = CATEGORIES.get(category)
    if cat is None:
        return FeedResult(
            category=category, label=category, kind="unknown",
            source_errors=[f"未知分类: {category}"],
        )
    if cat.kind != "rss":
        # 检索型分类(股市·个股)由 news_search 负责, 这里只回元信息
        return FeedResult(category=cat.key, label=cat.label, kind=cat.kind, note=cat.note)

    cached = _cache_get(category, hours) if use_cache else None
    if cached is not None:
        cached.cached = True
        return cached

    t0 = time.monotonic()
    cutoff = datetime.now(UTC) - timedelta(hours=hours) if hours > 0 else None

    entries: list[NewsEntry] = []
    errors: list[str] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(len(cat.feeds), 1))) as pool:
        futures = {pool.submit(_fetch_one, f, timeout): f for f in cat.feeds}
        for fut in futures:
            got, err = fut.result()
            if err:
                errors.append(err)
                continue
            ok += 1
            entries.extend(e for e in got if _within(e, cutoff))

    # 去重: 标题归一化 + 链接; 同一条留最新
    seen: dict[str, NewsEntry] = {}
    for e in entries:
        key = _norm_title(e.title) or e.url
        if not key:
            continue
        prev = seen.get(key)
        if prev is None or ((e.published_at or "") > (prev.published_at or "")):
            seen[key] = e
    deduped = list(seen.values())
    deduped.sort(key=lambda e: e.published_at or "", reverse=True)

    result = FeedResult(
        category=cat.key,
        label=cat.label,
        kind="rss",
        entries=deduped[: max(limit, 0)] if limit else deduped,
        source_errors=errors,
        source_count=len(cat.feeds),
        ok_source_count=ok,
        fetched_at=datetime.now(UTC).isoformat(),
        elapsed_s=time.monotonic() - t0,
        note=cat.note,
    )
    _cache_put(category, hours, result)
    return result


def categories_payload() -> list[dict[str, Any]]:
    """分类清单(给前端渲染 tab)。"""
    out: list[dict[str, Any]] = []
    for key in CATEGORY_ORDER:
        cat = CATEGORIES.get(key)
        if cat is None:
            continue
        out.append({
            "key": cat.key,
            "label": cat.label,
            "kind": cat.kind,
            "source_count": len(cat.feeds),
            "sources": [f.name for f in cat.feeds],
            "note": cat.note,
        })
    return out


# ---------------------------------------------------------------------------
# 缓存 (进程内 TTL)
# ---------------------------------------------------------------------------

_CACHE: dict[tuple[str, int], tuple[float, FeedResult]] = {}


def _cache_get(category: str, hours: int) -> FeedResult | None:
    hit = _CACHE.get((category, hours))
    if hit is None:
        return None
    ts, result = hit
    if time.monotonic() - ts > CACHE_TTL_S:
        _CACHE.pop((category, hours), None)
        return None
    return result


def _cache_put(category: str, hours: int, result: FeedResult) -> None:
    # 只在抓到东西或明确全失败时都缓存: 全失败也缓存, 避免每个请求都去撞一遍源站
    _CACHE[(category, hours)] = (time.monotonic(), result)


def invalidate_cache() -> None:
    _CACHE.clear()
