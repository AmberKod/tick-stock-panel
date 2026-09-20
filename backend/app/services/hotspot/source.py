"""热点数据源抽象 + 内置 Stub 实现。

设计意图:
- 路由层只依赖 ``HotspotSource`` 接口,不知道底层是 akshare / shy313 / 内存 stub。
- 本批交付 ``StubHotspotSource``:每次返回固定 fixture,便于端到端测试 & UI 渲染验证。
- 下一批新增 ``AkshareHotspotSource`` 与 1-2 个 fallback(项目惯例:多层降级)。
- 港股 / 美股统一返回 ``missing_mapping`` 失败,与 ``concept_heat`` 的 fail-closed 一致。

替换实现示例(下一批)::

    class AkshareHotspotSource(HotspotSource):
        def discover(self, market=..., top=...) -> HotspotResults:
            import akshare as ak
            df_concept = ak.stock_board_concept_name_em()
            ...

合约方法:
- ``discover(market, top)`` -> HotspotResults(含 leadership 自动填充)
- ``fetch_detail(topic, market, top_stocks)`` -> HotspotDetail
- ``supports(market)`` -> bool
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.services.hotspot.models import (
    QUALITY_PARTIAL,
    HotspotDetail,
    HotspotResults,
    HotspotStock,
    HotspotSummary,
    SourceError,
    utc_now_iso,
)
from app.services.hotspot.scoring import (
    _coerce_bool,
    assign_roles,
    classify_stage,
    compute_board_heat_score,
    safe_float,
    safe_text,
    score_constituent,
)

logger = logging.getLogger(__name__)


class HotspotSource:
    """抽象数据源。"""

    name: str = "abstract"

    def supports(self, market: str) -> bool:
        """当前 source 是否能服务指定 market。"""
        return False

    def discover(
        self,
        *,
        market: str = "cn",
        top: int = 20,
    ) -> HotspotResults:
        """获取主题列表(含成分股/leader)。

        内部调用 top=0 请求全部主题, 用于构建完整持久化快照;
        top>0 仅返回前 N 个主题。HTTP 参数限制由路由层负责。
        """
        raise NotImplementedError

    def fetch_detail(
        self,
        topic: str,
        *,
        market: str = "cn",
        top_stocks: int = 10,
    ) -> HotspotDetail | None:
        """获取单主题详情。返回 None 表示该 topic 不在 source 里。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Stub 实现 — 现在的内置数据源
# ---------------------------------------------------------------------------

_STUB_FALLBACK_ERROR = "source_unavailable: stub"


# 五组静态主题 fixture 用于 A 股热点流程验证
_STUB_TOPICS_A: list[dict[str, Any]] = [
    {
        "topic": "人工智能",
        "name": "人工智能",
        "source": "concept",
        "rank": 1,
        "change_pct": 0.0826,
        "sample_stock_count": 48,
    },
    {
        "topic": "新能源车",
        "name": "新能源车",
        "source": "concept",
        "rank": 2,
        "change_pct": 0.0512,
        "sample_stock_count": 35,
    },
    {
        "topic": "半导体",
        "name": "半导体",
        "source": "industry",
        "rank": 3,
        "change_pct": 0.0234,
        "sample_stock_count": 22,
    },
    {
        "topic": "白酒",
        "name": "白酒",
        "source": "industry",
        "rank": 4,
        "change_pct": -0.0128,
        "sample_stock_count": 18,
    },
    {
        "topic": "光伏",
        "name": "光伏",
        "source": "concept",
        "rank": 5,
        "change_pct": 0.0742,
        "sample_stock_count": 30,
    },
]

# 同一 fixture 的成分股(节选),用于 detail 渲染。
_STUB_CONSTITUENTS_A: dict[str, list[dict[str, Any]]] = {
    "人工智能": [
        {"code": "300474.SZ", "name": "景嘉微", "change_pct": 0.198, "amount": 12.4e8,
         "turnover_rate": 0.085, "volume_ratio": 3.2, "net_inflow": 1.2e8, "is_limit_up": True,
         "active_days": 4, "evidence_count": 6},
        {"code": "603019.SH", "name": "中科曙光", "change_pct": 0.074, "amount": 9.1e8,
         "turnover_rate": 0.041, "volume_ratio": 2.5, "net_inflow": 6.4e7,
         "active_days": 3, "evidence_count": 3},
        {"code": "002230.SZ", "name": "科大讯飞", "change_pct": 0.061, "amount": 7.3e8,
         "turnover_rate": 0.038, "volume_ratio": 1.8, "net_inflow": 4.2e7,
         "active_days": 2, "evidence_count": 2},
        {"code": "688256.SH", "name": "寒武纪", "change_pct": 0.085, "amount": 6.0e8,
         "turnover_rate": 0.052, "volume_ratio": 2.1, "net_inflow": 8.5e7,
         "active_days": 2, "evidence_count": 4},
    ],
    "新能源车": [
        {"code": "300750.SZ", "name": "宁德时代", "change_pct": 0.062, "amount": 2.5e9,
         "turnover_rate": 0.014, "volume_ratio": 1.6, "net_inflow": 1.1e8,
         "active_days": 1, "evidence_count": 2},
        {"code": "002594.SZ", "name": "比亚迪", "change_pct": 0.054, "amount": 1.8e9,
         "turnover_rate": 0.013, "volume_ratio": 1.3, "net_inflow": 6.4e7,
         "active_days": 1, "evidence_count": 1},
        {"code": "300014.SZ", "name": "亿纬锂能", "change_pct": 0.045, "amount": 6.4e8,
         "turnover_rate": 0.022, "volume_ratio": 1.4, "net_inflow": 3.1e7,
         "active_days": 1, "evidence_count": 1},
    ],
    "半导体": [
        {"code": "688981.SH", "name": "中芯国际", "change_pct": 0.029, "amount": 1.2e9,
         "turnover_rate": 0.012, "volume_ratio": 1.1, "net_inflow": 5.4e7,
         "active_days": 1, "evidence_count": 1},
        {"code": "002371.SZ", "name": "北方华创", "change_pct": 0.038, "amount": 8.7e8,
         "turnover_rate": 0.018, "volume_ratio": 1.2, "net_inflow": 4.2e7,
         "active_days": 1, "evidence_count": 1},
        {"code": "688012.SH", "name": "中微公司", "change_pct": 0.015, "amount": 4.8e8,
         "turnover_rate": 0.014, "volume_ratio": 1.0, "net_inflow": 1.4e7,
         "active_days": 1, "evidence_count": 1},
    ],
    "白酒": [
        {"code": "600519.SH", "name": "贵州茅台", "change_pct": -0.018, "amount": 1.0e9,
         "turnover_rate": 0.006, "volume_ratio": 0.8, "net_inflow": -2.4e7,
         "active_days": 0, "evidence_count": 0},
        {"code": "000858.SZ", "name": "五粮液", "change_pct": -0.024, "amount": 5.4e8,
         "turnover_rate": 0.007, "volume_ratio": 0.7, "net_inflow": -1.8e7,
         "active_days": 0, "evidence_count": 0},
    ],
    "光伏": [
        {"code": "601012.SH", "name": "隆基绿能", "change_pct": 0.082, "amount": 9.0e8,
         "turnover_rate": 0.022, "volume_ratio": 1.8, "net_inflow": 7.2e7,
         "active_days": 1, "evidence_count": 2},
        {"code": "002459.SZ", "name": "晶澳科技", "change_pct": 0.087, "amount": 6.4e8,
         "turnover_rate": 0.024, "volume_ratio": 1.9, "net_inflow": 5.6e7,
         "active_days": 1, "evidence_count": 2},
        {"code": "300763.SZ", "name": "锦浪科技", "change_pct": 0.066, "amount": 4.2e8,
         "turnover_rate": 0.029, "volume_ratio": 1.4, "net_inflow": 3.1e7,
         "active_days": 1, "evidence_count": 2},
    ],
}


class StubHotspotSource(HotspotSource):
    """内置 fixture 数据源。

    当前为默认实现。所有数据都是静态的,但真实走完整评分链路(与下一批
    AkshareHotspotSource 共用相同处理逻辑),用以打通路由 + 渲染全链路。
    """

    name = "stub"

    def supports(self, market: str) -> bool:
        return market in {"cn"}

    def discover(
        self,
        *,
        market: str = "cn",
        top: int = 20,
    ) -> HotspotResults:
        """返回预设主题; top=0 返回全部主题, 正数返回前 N 个。"""
        if market != "cn":
            err = SourceError(provider=self.name, method="discover",
                              message=f"market '{market}' unsupported by stub source")
            return HotspotResults(
                [],
                provider_used=self.name,
                source_errors=[err.as_str()],
                market=market,
            )

        source_errors: list[str] = []
        summaries: list[HotspotSummary] = []
        cap = max(int(top), 0)
        used = _STUB_TOPICS_A[:cap] if cap else _STUB_TOPICS_A
        for raw in used:
            summary = self._build_summary(raw)
            constituents = _STUB_CONSTITUENTS_A.get(summary.topic, [])
            stocks = self._coerce_stocks(constituents, source=f"{self.name}.constituents")
            if not stocks:
                _mark_missing(summary, ["stocks", "leader_stocks"])
            summary.leader_stocks = stocks[:3] if stocks else []
            summary.leaders = [s.name or s.code for s in summary.leader_stocks if (s.name or s.code)]
            summary.sample_stock_count = max(summary.sample_stock_count or 0, len(stocks))
            summaries.append(summary)

        if not summaries:
            source_errors.append(_STUB_FALLBACK_ERROR)

        # 本批保留 fixture 的预设列表顺序。
        return HotspotResults(
            summaries,
            provider_used=self.name,
            source_errors=source_errors,
            market=market,
        )

    def fetch_detail(
        self,
        topic: str,
        *,
        market: str = "cn",
        top_stocks: int = 10,
    ) -> HotspotDetail | None:
        if market != "cn":
            return None
        canonical = _match_stub_topic(topic)
        if canonical is None:
            return None
        raw_match = next((t for t in _STUB_TOPICS_A if t["topic"] == canonical), None)
        if raw_match is None:
            return None
        summary = self._build_summary(raw_match)
        constituents = _STUB_CONSTITUENTS_A.get(canonical, [])
        stocks = self._coerce_stocks(constituents, source=f"{self.name}.constituents")
        if stocks:
            stocks = assign_roles(stocks)
        summary.leader_stocks = [s for s in stocks if s.role == "核心龙头"][:3]
        if not summary.leader_stocks:
            summary.leader_stocks = stocks[:3]
        summary.leaders = [s.name or s.code for s in summary.leader_stocks if (s.name or s.code)]
        summary.sample_stock_count = len(stocks)
        if not stocks:
            _mark_missing(summary, ["stocks", "leader_stocks"])

        return HotspotDetail(
            summary=summary,
            stocks=stocks[:max(int(top_stocks), 0)],
            timeline=[],
            route=[],
            stock_count=len(stocks),
        )

    # ---- 内部 ----

    @staticmethod
    def _build_summary(raw: dict[str, Any]) -> HotspotSummary:
        change = safe_float(raw.get("change_pct"))
        rank = int(safe_float(raw.get("rank")) or 0) if raw.get("rank") is not None else None
        heat = compute_board_heat_score(change, rank)
        summary = HotspotSummary(
            topic=safe_text(raw["topic"]),
            name=safe_text(raw.get("name")) or safe_text(raw["topic"]),
            source=safe_text(raw.get("source")) or "concept",
            rank=rank,
            change_pct=change,
            heat_score=round(heat, 4),
            trend_score=None,
            persistence_score=None,
            cooling_score=None,
            observations=0,
            state="",
            stage=classify_stage(latest_score=heat, observations=0, persistence_score=0, trend_score=0, cooling_score=0),
            sample_stock_count=int(safe_float(raw.get("sample_stock_count")) or 0),
            canonical_topic=safe_text(raw["topic"]),
            aliases=[safe_text(raw["topic"])],
            quality_status="available",
            missing_fields=[],
            provider_used=StubHotspotSource.name,
            topic_date=utc_now_iso()[:10],
        )
        return summary

    @staticmethod
    def _coerce_stocks(raw_rows: Sequence[dict[str, Any]], *, source: str) -> list[HotspotStock]:
        stocks: list[HotspotStock] = []
        for row in raw_rows:
            stock = HotspotStock(
                code=safe_text(row.get("code")),
                name=safe_text(row.get("name")),
                change_pct=safe_float(row.get("change_pct")),
                amount=safe_float(row.get("amount")),
                turnover_rate=safe_float(row.get("turnover_rate")),
                volume_ratio=safe_float(row.get("volume_ratio")),
                net_inflow=safe_float(row.get("net_inflow")),
                # An unknown limit status earns no bonus; a price change cannot
                # determine the market/date-specific exchange limit.
                is_limit_up=_coerce_bool(row.get("is_limit_up")),
                active_days=int(safe_float(row.get("active_days")) or 0),
                evidence_count=int(safe_float(row.get("evidence_count")) or 0),
                source=source,
                source_confidence=1.0,
                fallback_used=False,
            )
            stock.hot_stock_score = score_constituent(stock)
            stocks.append(stock)
        return stocks


def _mark_missing(summary: HotspotSummary, fields: list[str]) -> None:
    seen = set(summary.missing_fields or [])
    for f in fields:
        if f not in seen:
            summary.missing_fields.append(f)
            seen.add(f)
    summary.quality_status = QUALITY_PARTIAL


def _match_stub_topic(topic: str) -> str | None:
    """topic 别名匹配(fixture 阶段只做精确匹配)。"""
    text = safe_text(topic)
    for t in _STUB_TOPICS_A:
        if t["topic"] == text:
            return t["topic"]
    return None


# ---------------------------------------------------------------------------
# Source 选择器
# ---------------------------------------------------------------------------

_DEFAULT_CN_SOURCE = StubHotspotSource()
_DEFAULT_AKSHARE_SOURCE: object | None = None
_DEFAULT_CN_CONCEPT_SOURCE: object | None = None
_DEFAULT_HK_SOURCE: object | None = None
_DEFAULT_US_SOURCE: object | None = None


def _default_akshare_source():
    """A 股默认源: akshare 东财概念/行业板块 (惰性单例, 复用板块缓存)。

    fail-closed: akshare 不可用或拉取失败时返回空结果 + source_errors,
    由 service 层走缓存回退; **绝不**降级到 stub fixture 伪装真实数据。
    """
    global _DEFAULT_AKSHARE_SOURCE
    if _DEFAULT_AKSHARE_SOURCE is None:
        from app.services.hotspot.akshare_source import AkshareHotspotSource

        _DEFAULT_AKSHARE_SOURCE = AkshareHotspotSource()
    return _DEFAULT_AKSHARE_SOURCE


def _default_hk_us_source(market: str):
    """港美默认源: instruments 行业分类 x 行情聚合 (惰性单例, 按市场缓存)。

    akshare 无港美板块接口, 故港美走本地聚合路线; 盘中实时 / 日K快照的双路
    选择由 source 内部按交易时段决定。
    """
    global _DEFAULT_HK_SOURCE, _DEFAULT_US_SOURCE
    from app.config import settings
    from app.services.hotspot.hk_us_source import HkUsIndustryHotspotSource

    if market == "hk":
        if _DEFAULT_HK_SOURCE is None:
            _DEFAULT_HK_SOURCE = HkUsIndustryHotspotSource(settings.data_dir)
        return _DEFAULT_HK_SOURCE
    if _DEFAULT_US_SOURCE is None:
        _DEFAULT_US_SOURCE = HkUsIndustryHotspotSource(settings.data_dir)
    return _DEFAULT_US_SOURCE


def _default_cn_source():
    """A 股默认源: 同花顺概念成分 x 本地 enriched 聚合 (惰性单例)。

    2026-09-18 起替换 akshare 东财板块源: 后者依赖 push2.eastmoney.com,
    在代理环境不可达 (实测 28.7s 超时后空列表), 热点页长期空白。
    参考项目本来就是本地算, 且与港美本地行业源同口径。
    akshare 源仍在代码里, 需要时由 app.state.hotspot_cn_source 注入 override。
    """
    global _DEFAULT_CN_CONCEPT_SOURCE
    from app.config import settings
    from app.services.hotspot.cn_concept_source import CnConceptHotspotSource

    if _DEFAULT_CN_CONCEPT_SOURCE is None:
        _DEFAULT_CN_CONCEPT_SOURCE = CnConceptHotspotSource(settings.data_dir)
    return _DEFAULT_CN_CONCEPT_SOURCE


def select_source(market: str, *, override: HotspotSource | None = None) -> HotspotSource:
    """按 market 选择 source,override 优先级最高(用于测试)。

    A 股 → 本地同花顺概念 x 行情聚合; 港股 / 美股 → 本地 instruments 行业聚合。
    三个市场都不再依赖外部接口。stub 仅作测试注入用, 不作为生产默认源。
    """
    if override is not None:
        return override
    if market == "cn":
        return _default_cn_source()
    if market in {"hk", "us"}:
        return _default_hk_us_source(market)
    return _DEFAULT_CN_SOURCE
