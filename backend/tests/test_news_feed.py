"""通用新闻流(RSS 聚合)测试 — 全部打桩, 不碰网络。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import news_feed as nf

RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>测试源</title>
<item>
  <title>央行宣布降准</title>
  <link>https://example.com/a</link>
  <pubDate>Sat, 19 Sep 2026 10:00:00 GMT</pubDate>
  <description>&lt;p&gt;释放长期资金&lt;/p&gt; 约五千亿</description>
</item>
<item>
  <title>油价大涨</title>
  <link>https://example.com/b</link>
  <pubDate>Sat, 19 Sep 2026 09:00:00 GMT</pubDate>
  <description>OPEC 减产</description>
</item>
</channel></rss>
""".encode()

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom 源</title>
<entry>
  <title>Atom 新闻</title>
  <link href="https://example.com/atom"/>
  <updated>2026-09-19T09:30:00Z</updated>
  <summary>摘要内容</summary>
</entry>
</feed>
""".encode()


@pytest.fixture(autouse=True)
def _clean_cache():
    nf.invalidate_cache()
    yield
    nf.invalidate_cache()


# ── 解析 ────────────────────────────────────────────────────────────


def test_parse_rss_items():
    entries = nf.parse_feed(RSS_XML, "测试源")
    assert len(entries) == 2
    first = entries[0]
    assert first.title == "央行宣布降准"
    assert first.url == "https://example.com/a"
    assert first.source == "测试源"
    assert first.published_at == "2026-09-19T10:00:00+00:00"
    # 摘要要去标签
    assert "<p>" not in first.summary


def test_parse_atom_entries():
    entries = nf.parse_feed(ATOM_XML, "Atom源")
    assert len(entries) == 1
    assert entries[0].title == "Atom 新闻"
    assert entries[0].url == "https://example.com/atom"
    assert entries[0].published_at == "2026-09-19T09:30:00+00:00"


def test_parse_invalid_xml_raises_value_error():
    with pytest.raises(ValueError):
        nf.parse_feed(b"not xml at all", "坏源")


def test_parse_time_handles_rfc822_and_iso():
    assert nf._parse_time("Sat, 19 Sep 2026 10:00:00 GMT") == datetime(
        2026, 9, 19, 10, 0, tzinfo=UTC
    )
    assert nf._parse_time("2026-09-19T10:00:00Z") == datetime(
        2026, 9, 19, 10, 0, tzinfo=UTC
    )
    assert nf._parse_time("2026-09-19T18:00:00+08:00") == datetime(
        2026, 9, 19, 10, 0, tzinfo=UTC
    )
    assert nf._parse_time(None) is None
    assert nf._parse_time("乱码") is None


def test_norm_title_strips_punctuation():
    assert nf._norm_title("央行 宣布，降准！") == nf._norm_title("央行宣布降准")


# ── 去重 / 时间窗 ───────────────────────────────────────────────────


def test_duplicate_titles_are_merged(monkeypatch):
    """同一条新闻多源重复 → 只留一条, 且留时间更新的那个。"""
    def fake(feed, timeout=None):
        if feed.name == "A":
            return [nf.NewsEntry("同一条新闻", "https://a/1", "A", "2026-09-19T10:00:00+00:00")], None
        return [nf.NewsEntry("同一条新闻", "https://b/1", "B", "2026-09-19T12:00:00+00:00")], None

    monkeypatch.setattr(nf, "_fetch_one", fake)
    # 借 world 分类的壳(4 个源), 全部返回同一条
    r = nf.fetch_category("world", hours=48, limit=10)
    assert len(r.entries) == 1
    assert r.entries[0].source == "B"  # 时间更新的胜出


def test_entries_outside_time_window_are_dropped(monkeypatch):
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    fresh = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    def fake(feed, timeout=None):
        return [
            nf.NewsEntry("老新闻", "https://old", feed.name, old),
            nf.NewsEntry("新新闻", "https://new", feed.name, fresh),
        ], None

    monkeypatch.setattr(nf, "_fetch_one", fake)
    r = nf.fetch_category("cn", hours=48, limit=50)
    titles = {e.title for e in r.entries}
    assert "新新闻" in titles
    assert "老新闻" not in titles


def test_entry_without_published_at_is_kept(monkeypatch):
    """源不给时间(如 36氪)时不瞎猜, 保留而不是丢掉。"""
    def fake(feed, timeout=None):
        return [nf.NewsEntry("无时间条目", "https://x", feed.name, None)], None

    monkeypatch.setattr(nf, "_fetch_one", fake)
    r = nf.fetch_category("tech", hours=48, limit=10)
    assert any(e.title == "无时间条目" for e in r.entries)


# ── fail-closed ────────────────────────────────────────────────────


def test_all_sources_failed_marks_not_success(monkeypatch):
    def fake(feed, timeout=None):
        return [], f"{feed.name}: ConnectError"

    monkeypatch.setattr(nf, "_fetch_one", fake)
    r = nf.fetch_category("world", hours=48)
    n = len(nf.CATEGORIES["world"].feeds)  # 别硬编码, 源清单会变
    assert r.entries == []
    assert r.ok_source_count == 0
    assert r.source_count == n
    assert len(r.source_errors) == n
    assert r.to_dict()["success"] is False  # 前端据此显示"源不可用"而不是"没新闻"


def test_unknown_category_returns_error():
    r = nf.fetch_category("not-a-category")
    assert r.source_errors and "未知分类" in r.source_errors[0]


def test_search_kind_category_has_no_rss_sources():
    """股市·个股 走检索源, 本模块不该去抓 RSS。"""
    r = nf.fetch_category("market")
    assert r.kind == "search"
    assert r.entries == []
    assert r.source_count == 0  # 该分类没有 RSS 源


# ── 缓存 / 分类清单 ─────────────────────────────────────────────────


def test_cache_reuses_result(monkeypatch):
    calls = {"n": 0}

    def fake(feed, timeout=None):
        calls["n"] += 1
        return [nf.NewsEntry(f"{feed.name}-{calls['n']}", "https://x", feed.name, None)], None

    monkeypatch.setattr(nf, "_fetch_one", fake)
    nf.fetch_category("cn", hours=48)
    first = calls["n"]
    second = nf.fetch_category("cn", hours=48)
    assert calls["n"] == first  # 没再抓
    assert second.cached is True
    # refresh 强制重抓
    nf.fetch_category("cn", hours=48, use_cache=False)
    assert calls["n"] > first


def test_invalidate_cache_clears():
    nf._CACHE[("cn", 48)] = (nf.time.monotonic(), nf.FeedResult(category="cn", label="国内"))
    nf.invalidate_cache()
    assert nf._CACHE == {}


def test_categories_payload_matches_order():
    payload = nf.categories_payload()
    keys = [c["key"] for c in payload]
    assert keys == list(nf.CATEGORY_ORDER)
    assert len(keys) == 7
    market = next(c for c in payload if c["key"] == "market")
    assert market["kind"] == "search"
    world = next(c for c in payload if c["key"] == "world")
    assert world["source_count"] >= 3


def test_every_rss_category_has_feeds():
    """每个 RSS 分类至少要有一个源 —— 空源分类等于给用户一个永远空白的 tab。"""
    for key, cat in nf.CATEGORIES.items():
        if cat.kind == "rss":
            assert cat.feeds, f"{key} 没有任何源"


def test_energy_avoids_dead_channel():
    """中新网能源 RSS 是空频道(无 item), 别再接回来。"""
    urls = " ".join(f.url for f in nf.CATEGORIES["energy"].feeds)
    assert "chinanews.com.cn/rss/energy.xml" not in urls
