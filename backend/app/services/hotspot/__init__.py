"""热点工作区核心模块。

设计要点(与参考项目的对齐/差异):

- 数据模型 (HotspotSummary / HotspotStock / HotspotDetail) 对齐参考项目
  ``src/services/screening/hotspot.py``,但用 polars/parquet 而非 pandas/JSON。
- 评分(heat_score / trend / persistence / cooling / stage)参考项目方案,
  但所有数值计算在 ``scoring.py`` 内独立实现,避免紧耦合。
- 持久化遵循项目惯例:``backend/data/hotspot/{<market>/topics.parquet,
  <market>/constituents/<topic>.parquet, history/*.jsonl}``。topics 与 constituents
  都按市场分片(cn/hk/us),避免跨市场互相整文件覆盖。``data/`` 整体不入库。
- Source 抽象(``source.py``)让 A 股 akshare 接入可以后续替换具体实现,
  路由层不感知。今天内置 ``StubSource``,下一批替换为 ``AkshareSource``。
- 港美市场缺概念/板块数据源,统一返回 ``missing_mapping``,对齐
  ``app/strategy/concept_heat.py`` 的 fail-closed 风格。
"""
from __future__ import annotations

from app.services.hotspot.akshare_source import AkshareHotspotSource, decorate_a_share_code
from app.services.hotspot.models import (
    HotspotDetail,
    HotspotResults,
    HotspotRole,
    HotspotStage,
    HotspotStock,
    HotspotSummary,
    QualityStatus,
    SourceError,
)
from app.services.hotspot.scoring import (
    assign_role,
    clamp,
    classify_stage,
    hot_stock_score_breakdown,
    safe_float,
    safe_text,
    score_constituent,
)
from app.services.hotspot.service import (
    HotspotService,
    discover_hotspots,
    get_hotspot_detail,
)
from app.services.hotspot.source import (
    HotspotSource,
    StubHotspotSource,
    select_source,
)
from app.services.hotspot.storage import (
    HotspotStorage,
    append_history_row,
    load_history_jsonl,
)

__all__ = [
    "AkshareHotspotSource",
    "HotspotDetail",
    "HotspotResults",
    "HotspotRole",
    "HotspotService",
    "HotspotSource",
    "HotspotStage",
    "HotspotStock",
    "HotspotStorage",
    "HotspotSummary",
    "QualityStatus",
    "SourceError",
    "StubHotspotSource",
    "append_history_row",
    "assign_role",
    "clamp",
    "classify_stage",
    "decorate_a_share_code",
    "discover_hotspots",
    "get_hotspot_detail",
    "hot_stock_score_breakdown",
    "load_history_jsonl",
    "safe_float",
    "safe_text",
    "score_constituent",
    "select_source",
]
