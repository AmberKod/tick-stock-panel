"""热点评分算法及角色回填。

对齐参考项目 ``src/services/screening/hotspot.py:152-294`` 的核心公式:
- ``compute_board_heat_score`` - heat_score 由板块涨幅+排名派生
- ``classify_hotspot_stage`` - 五段生命周期分类
- ``score_constituent`` - 龙头股多维加权评分
- ``assign_role`` - 角色分配(核心龙头/助攻/补涨/后排/掉队)

设计差异:
- 不依赖 pandas:输入直接接受 dataclass / dict。
- 缺失字段容错:``safe_*`` 系列函数把 None/NaN/乱字符串等价为安全值。
- 评分、拆分和阶段分类无副作用; 角色 helper 按现有契约回填输入股票。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from app.services.hotspot.models import (
    HOTSPOT_ROLES,
    HOTSPOT_STAGES,
    HotspotStock,
)

# ---------------------------------------------------------------------------
# 通用 safe 工具
# ---------------------------------------------------------------------------

def safe_text(value: Any) -> str:
    """任何输入转干净字符串。NaN/None/全空白统一为空串。"""
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    text = str(value).strip()
    if text.casefold() in {"nan", "none", "null"}:
        return ""
    return text


def safe_float(value: Any, default: float = 0.0) -> float | None:
    """安全 float 解析,无法解析回退为 ``default``(默认 0.0)。

    唯一会返 None:``value is None``(区分"我给过你无法处理"与"没值")。
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


# ---------------------------------------------------------------------------
# 板块 heat score (本项目已批准的涨幅与排名权重)
# ---------------------------------------------------------------------------

# 设计意图:分数主要来自当日涨幅(0~10% → 0~70),辅以排名(1~80 → +30)。
# 涨跌幅按小数制打分(0.0000~0.10 → 0~70),与项目其它归一化口径一致。
_BOARD_BASE = 30.0
_CHANGE_MAX = 0.10  # 10% 视为满分(沪深主板涨停 10%)
_RANK_TOP = 80  # 排名 1~80 给予正向加成


def compute_board_heat_score(change_pct: float | None, rank: int | None) -> float:
    """板块 heat_score in [0, 100].

    change_pct 取小数制(0.0366 = 3.66%)。当 change_pct 缺失时,假设为 0。
    rank 取东方财富接口的板块排名(1 = 涨幅最高)。

    公式: 涨幅归一(-1.0~+1.5)*70 + 排名加成(0~30), 结果限制为 0~100。
    """
    change = safe_float(change_pct) or 0.0
    change_component = clamp(change / _CHANGE_MAX, -1.0, 1.5) * 70.0

    rank_component = 0.0
    if rank is not None and rank > 0:
        # 1 -> 30, 80 -> 0.375, >80 -> 0
        rank_component = clamp(1.0 - (rank - 1) / _RANK_TOP, 0.0, 1.0) * 30.0

    return clamp(_BOARD_BASE + change_component + rank_component - _BOARD_BASE, 0.0, 100.0)


# ---------------------------------------------------------------------------
# 阶段分类(与参考项目 classify_hotspot_stage 等价)
# ---------------------------------------------------------------------------

def classify_stage(
    *,
    state: str = "",
    trend_score: float | None = None,
    cooling_score: float | None = None,
    persistence_score: float | None = None,
    latest_score: float | None = None,
    observations: int | None = None,
) -> str:
    """将趋势三维度 + state/observations 投影到 5 段生命周期。

    与参考项目逻辑保持一致(先判 cooling,再判 persistence/trend,最后兜底)。
    """
    state_text = safe_text(state).lower()
    trend = safe_float(trend_score) or 0.0
    cooling = safe_float(cooling_score) or 0.0
    persistence = safe_float(persistence_score) or 0.0
    latest = safe_float(latest_score) or 0.0
    obs = int(safe_float(observations) or 0)

    if state_text in {"weakening", "cooling"} and (latest < 60 or trend <= -5):
        return "降温退潮"
    if cooling >= 8 and (latest < 60 or trend <= -5):
        return "降温退潮"
    if cooling >= 5:
        return "分歧放量"
    if latest >= 75 and trend >= 8 and persistence >= 50:
        return "加速主升"
    if state_text == "persistent_hot" or persistence >= 66.6667:
        return "确认扩散"
    if trend >= 5 and obs >= 2:
        return "确认扩散"
    return "初次异动"


# ---------------------------------------------------------------------------
# 成分股评分(与参考项目 score_hotspot_stock 等价)
# ---------------------------------------------------------------------------

def score_constituent(row: Mapping[str, Any] | HotspotStock) -> float:
    """成分股 hot_stock_score in [0, 100]。

    多维加权(满分 ~100):
      35 起步 + 涨跌幅(±18~32) + 成交额(0~18, log10 压缩)
      + 换手率(0~14) + 量比(0~12) ± 主力净流(±12/-8)
      + 涨停 +8 + 连续活跃(0~8) + 证据数(0~8)

    change_pct 和 turnover_rate 均为小数制 (0.05 = 5%)。
    仅明确提供的 is_limit_up 为真时加分, 不由涨幅推断涨停。
    """
    return round(clamp(sum(_constituent_score_components(row).values()), 0.0, 100.0), 4)


def assign_role(stock: HotspotStock, *, top_score: float) -> str:
    """根据 hot_stock_score + change_pct 给单个成分股分配角色。

    ``top_score`` 是同 topic 内排名最高的 hot_stock_score,用于「top+相邻分数」
    判定核心龙头。

    当 hot_stock_score <= 0 时会回填输入股票的评分, 返回角色但不写 role 字段。
    """
    if stock.hot_stock_score <= 0:
        stock.hot_stock_score = score_constituent(stock)
    change = stock.change_pct or 0.0
    # change_pct 是小数制 (0.05 = 5%) — 项目惯例, 与 extent_data / overview 一致
    # 阈值 5%/3%/0% 折算小数 0.05/0.03/0.00
    if (
        stock.hot_stock_score >= 70
        and stock.hot_stock_score >= max(68.0, top_score - 8.0)
        and change >= 0.05
    ):
        return "核心龙头"
    if stock.hot_stock_score >= 62.0 and change >= 0.03:
        return "助攻"
    if stock.hot_stock_score >= 48.0 and change >= 0.0:
        return "补涨"
    if stock.hot_stock_score >= 38.0:
        return "后排"
    return "掉队"


# ---------------------------------------------------------------------------
# 批处理 helper
# ---------------------------------------------------------------------------

def assign_roles(stocks: list[HotspotStock]) -> list[HotspotStock]:
    """回填输入股票的缺省评分和角色, 返回按分数排序的新列表。

    返回列表复用原 HotspotStock 对象; 输入列表的顺序保持不变。
    """
    if not stocks:
        return []
    # 计算 / 兜底评分(输入可能 score=0)
    for stock in stocks:
        if stock.hot_stock_score <= 0:
            stock.hot_stock_score = score_constituent(stock)
    sorted_stocks = sorted(
        stocks,
        key=lambda s: (
            s.hot_stock_score,
            s.change_pct if s.change_pct is not None else -999.0,
            s.amount if s.amount is not None else -1.0,
            s.code,
        ),
        reverse=True,
    )
    top_score = sorted_stocks[0].hot_stock_score
    for idx, stock in enumerate(sorted_stocks):
        change = stock.change_pct or 0.0
        # change_pct 是小数制 — 阈值 5%/3% 折算 0.05/0.03
        if (idx == 0 and stock.hot_stock_score >= 70) or (
            idx <= 2
            and stock.hot_stock_score >= max(68.0, top_score - 8.0)
            and change >= 0.05
        ):
            role = "核心龙头"
        else:
            role = assign_role(stock, top_score=top_score)
        stock.role = role
    return sorted_stocks


def hot_stock_score_breakdown(row: Mapping[str, Any] | HotspotStock) -> dict[str, float]:
    """诊断助手:把 score_constituent 的各项贡献拆出来便于日志/测试展示。"""
    return {name: round(value, 4) for name, value in _constituent_score_components(row).items()}


def _constituent_score_components(row: Mapping[str, Any] | HotspotStock) -> dict[str, float]:
    """Compute unrounded contributions for both total scoring and diagnostics."""
    values = asdict(row) if isinstance(row, HotspotStock) else row
    change = safe_float(values.get("change_pct")) or 0.0
    amount = safe_float(values.get("amount")) or 0.0
    turnover = safe_float(values.get("turnover_rate")) or 0.0
    volume_ratio = safe_float(values.get("volume_ratio")) or 0.0
    net_inflow = safe_float(values.get("net_inflow")) or 0.0
    is_limit_up = _coerce_bool(values.get("is_limit_up"))
    active_days = int(safe_float(values.get("active_days")) or 0)
    evidence_count = int(safe_float(values.get("evidence_count")) or 0)

    breakdown = {
        "base": 35.0,
        # The reference coefficients use percentage points; our inputs are ratios.
        "change": clamp(change * 100.0 * 2.7, -18.0, 32.0),
        "amount": 0.0,
        "net_inflow": 0.0,
        "turnover": clamp(turnover * 100.0 * 1.1, 0.0, 14.0),
        "volume_ratio": clamp(volume_ratio * 3.0, 0.0, 12.0),
        "limit_up": 8.0 if is_limit_up else 0.0,
        "active_days": clamp(active_days * 2.5, 0.0, 8.0),
        "evidence": clamp(evidence_count * 2.0, 0.0, 8.0),
    }
    if amount > 0:
        breakdown["amount"] = clamp((math.log10(amount) - 6.0) / 4.0 * 18.0, 0.0, 18.0)
    if net_inflow > 0:
        breakdown["net_inflow"] = clamp((math.log10(net_inflow) - 5.0) / 4.0 * 12.0, 0.0, 12.0)
    elif net_inflow < 0:
        breakdown["net_inflow"] = -clamp((math.log10(abs(net_inflow)) - 5.0) / 4.0 * 8.0, 0.0, 8.0)
    return breakdown


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = safe_text(value).lower()
    return text in {"1", "true", "yes", "y", "是", "涨停", "limit_up"}


# 对外暴露类便于 IDE / 测试。
__all__ = [
    "HOTSPOT_ROLES",
    "HOTSPOT_STAGES",
    "_coerce_bool",
    "assign_role",
    "assign_roles",
    "clamp",
    "classify_stage",
    "compute_board_heat_score",
    "hot_stock_score_breakdown",
    "safe_float",
    "safe_text",
    "score_constituent",
]
