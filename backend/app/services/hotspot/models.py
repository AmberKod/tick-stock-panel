"""热点数据模型。

所有 dataclass 与参考项目 ``src/services/screening/hotspot.py`` 对齐字段名
和语义,同时适配本项目 polars 直读直写的扁平结构。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

# 五段生命周期(中文文案,与 MarketDash 已有阶段文本一致)。
HOTSPOT_STAGES: tuple[str, ...] = (
    "初次异动",
    "确认扩散",
    "加速主升",
    "分歧放量",
    "降温退潮",
)
# 角色类型。
HOTSPOT_ROLES: tuple[str, ...] = (
    "核心龙头",
    "助攻",
    "补涨",
    "后排",
    "掉队",
)
# 数据质量状态(对齐参考项目 enum)。
QUALITY_OK = "available"
QUALITY_PARTIAL = "partial"
QUALITY_STALE = "stale"
QUALITY_FAILED = "failed"
QUALITY_MISSING = "missing_mapping"


@dataclass
class SourceError:
    """数据源错误描述。

    与参考项目同名异构:参考项目用 ``list[str]``,本项目用结构化记录便于
    上层把多条同源错误去重 + 暴露给前端做诊断展示。
    """

    provider: str = ""
    method: str = ""
    message: str = ""

    def as_str(self) -> str:
        head = ".".join(part for part in (self.provider, self.method) if part)
        return f"{head}: {self.message}" if head else self.message


@dataclass
class HotspotSummary:
    """热点话题快照。"""

    topic: str
    name: str = ""
    source: str = ""  # concept | industry | akshare 等
    rank: int | None = None
    change_pct: float | None = None  # 小数制 0.0366 = 3.66%
    heat_score: float = 50.0  # 0-100
    trend_score: float | None = None  # -∞ ~ +∞
    persistence_score: float | None = None  # 0-100
    cooling_score: float | None = None  # 0-100
    observations: int = 0
    state: str = ""  # 自由字符串,供 stage 分类使用
    # 生命周期阶段。None = 未判定(趋势维度没有观测值),不是"初次异动" —
    # "初次异动"是观测结论,缺观测点时只能说未判定 —— 拿缺省值冒充它会被渲染成假结论。
    stage: str | None = None
    sample_stock_count: int = 0
    leaders: list[str] = field(default_factory=list)
    leader_stocks: list[HotspotStock] = field(default_factory=list)
    quality_status: str = QUALITY_PARTIAL
    missing_fields: list[str] = field(default_factory=list)
    canonical_topic: str = ""
    aliases: list[str] = field(default_factory=list)
    provider_used: str = ""
    fallback_used: bool = False
    source_errors: list[str] = field(default_factory=list)
    stale: bool = False
    stale_age_hours: float | None = None
    topic_date: str = ""  # YYYY-MM-DD 该快照所属交易日
    snapshot_at: str = ""  # UTC ISO 数据观察时间, 未知时留空
    snapshot_market: str = ""  # 与数据一起持久化; 空值兼容旧 CN 快照


@dataclass
class HotspotStock:
    """热点成分股。"""

    code: str
    name: str = ""
    change_pct: float | None = None  # 小数制 0.05 = 5%
    amount: float | None = None  # 成交额
    turnover_rate: float | None = None  # 小数制 0.04 = 4%
    volume_ratio: float | None = None
    net_inflow: float | None = None
    is_limit_up: bool = False  # False 表示没有已确认的涨停事实, 不加涨停分
    active_days: int = 0
    evidence_count: int = 0
    role: str = ""
    hot_stock_score: float = 0.0
    source: str = ""
    source_confidence: float | None = None
    fallback_used: bool = False


@dataclass
class HotspotDetail:
    """热点话题详情。"""

    summary: HotspotSummary
    stocks: list[HotspotStock] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    route: list[dict[str, Any]] = field(default_factory=list)
    stock_count: int = 0


@dataclass
class HotspotResults(list[HotspotSummary]):
    """列表型结果,附带 provider/degradation 元数据。

    与参考项目同名类一致:list[HotspotSummary] + 顶层错误/降级信息。
    """

    def __init__(
        self,
        items: list[HotspotSummary] | None = None,
        *,
        provider_used: str = "",
        fallback_used: bool = False,
        source_errors: list[str] | None = None,
        stale: bool = False,
        stale_age_hours: float | None = None,
        market: str = "cn",
        quality_status: str = "",
        sample_coverage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(items or [])
        self.provider_used = provider_used
        self.fallback_used = fallback_used
        self.source_errors = _dedupe_errors(source_errors or [])
        self.stale = stale
        self.stale_age_hours = stale_age_hours
        self.market = market
        # 本次结果的样本覆盖率 {"covered","universe","ratio","as_of","stale_symbols",...}
        # None = 分母不可得 (universe 读不到) 或该源不适用, 前端据此不显示。
        self.sample_coverage = sample_coverage
        if quality_status:
            self.quality_status = quality_status
        elif stale:
            self.quality_status = QUALITY_STALE
        elif not self:
            self.quality_status = QUALITY_FAILED
        elif fallback_used or any(item.quality_status != QUALITY_OK or item.missing_fields for item in self):
            self.quality_status = QUALITY_PARTIAL
        else:
            self.quality_status = QUALITY_OK

    @property
    def is_usable(self) -> bool:
        """是否有任何可用结果(即便降级也算可用,便于前端降级展示)。"""
        return len(self) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hotspots": [asdict(item) for item in self],
            "hotspot_count": len(self),
            "provider": self.provider_used or "stub",
            "provider_used": self.provider_used,
            "fallback_used": self.fallback_used,
            "source_errors": list(self.source_errors),
            "stale": self.stale,
            "stale_age_hours": self.stale_age_hours,
            "market": self.market,
            "quality_status": self.quality_status,
            "sample_coverage": dict(self.sample_coverage) if self.sample_coverage else None,
        }


# 类型别名方便引用。
HotspotStage = str
HotspotRole = str
QualityStatus = str


def utc_now_iso() -> str:
    """统一 UTC ISO 时间戳,便于 parquet 写入。"""
    return datetime.now(UTC).isoformat()


def _dedupe_errors(errors: list[str] | list[SourceError]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in errors:
        text = raw.as_str() if isinstance(raw, SourceError) else str(raw).strip()
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return items
