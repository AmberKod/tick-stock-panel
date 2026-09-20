"""市场态势 (market posture) 计算 — 三市场态势总览面板的后端判断层。

设计纪律 (来源: deliverables/panel-portfolio-2026-09-20.md §2 / §3.2 面板 1):

1. **可解释规则, 不加权打分**。本项目已有 hotspot scoring 与 regime 阈值两套
   未回测的加权打分, 本模块**绝不再引入第三套**。判定 = 一票否决 + 逐维度投票,
   没有任何权重求和。
2. **不可用不计入分母**。某维度对某市场不可用时标 ``unavailable`` 并从投票
   分母中剔除, **不得当作 0 或中性** —— 当 0 会让港股被永久误判成"防守",
   当中性会静默退化成一票。
3. **不可用维度按市场分别声明, 禁止"港美缺 X + Y"式一刀切**。把"可用"误判成
   "不可用"是同一类错误的镜像 (会让美股白白丢掉它本来有的 industry 维度)。
4. **阈值随本模块存放**。``tiers.yaml`` 是数据源套餐能力对照表, 业务代码不读;
   所有常量由 ``tests/test_market_posture.py`` 钉死。

只读约定: 本模块只走落盘数据的只读路径 (regime parquet / hotspot snapshot /
overview builder), **绝不触发任何同步或刷新** (不走 ``discover(refresh=True)``)。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 态势枚举 (四态, 不要可空) ──────────────────────────────────
POSTURE_ATTACK = "attack"
POSTURE_BALANCED = "balanced"
POSTURE_DEFEND = "defend"
POSTURE_UNKNOWN = "unknown"

POSTURE_LABELS: dict[str, str] = {
    POSTURE_ATTACK: "进攻",
    POSTURE_BALANCED: "均衡",
    POSTURE_DEFEND: "防守",
    POSTURE_UNKNOWN: "未知",
}

# ── 投票枚举 (unavailable 必须是一等公民) ──────────────────────
VOTE_ATTACK = "attack"
VOTE_NEUTRAL = "neutral"
VOTE_DEFEND = "defend"
VOTE_UNAVAILABLE = "unavailable"

VOTE_LABELS: dict[str, str] = {
    VOTE_ATTACK: "进攻",
    VOTE_NEUTRAL: "中性",
    VOTE_DEFEND: "防守",
    VOTE_UNAVAILABLE: "不可用",
}

# ── 投票维度 (固定 4 个) ──────────────────────────────────────
# mainline 不列为维度: A 股专属 + 依赖 _SCORE_WEIGHTS 加权, 撞 §2 纪律;
# concept_rank 不列为维度: 港美恒空 (hk_us_overview_builder 硬编码)。
# 两者都是"根本不列为维度", 而非"标 unavailable"。
DIM_REGIME = "regime"
DIM_BREADTH = "breadth"
DIM_HOTSPOTS = "hotspots"
DIM_INDUSTRY = "industry"

VOTING_DIMS: tuple[str, ...] = (DIM_REGIME, DIM_BREADTH, DIM_HOTSPOTS, DIM_INDUSTRY)

DIM_LABELS: dict[str, str] = {
    DIM_REGIME: "市场环境",
    DIM_BREADTH: "涨跌广度",
    DIM_HOTSPOTS: "热点阶段",
    DIM_INDUSTRY: "行业强弱",
}

ALLOWED_MARKETS: tuple[str, ...] = ("cn", "hk", "us")
MARKET_LABELS: dict[str, str] = {"cn": "A 股", "hk": "港股", "us": "美股"}

# ─────────────────────────────────────────────────────────────
# 不可用矩阵 (按市场分别声明)
# ─────────────────────────────────────────────────────────────
# - cn: 0 个
# - us: 0 个 —— ⚠️ industry 对美股**可用** (NASDAQ sector 聚合),
#       绝不可误判为不可用, 否则美股白白丢掉一个维度。
# - hk: 1 个 —— industry。⚠️ 这一条是**兜底解释**, 不是"港股没有行业数据":
#   2026-09-19 把 sync_hk_industries 改成东财批量分页后, hk_instruments.parquet 的
#   sector 覆盖已达 2810/2816 (99.8%, 31 类), **目前实际有数据、照常参与投票**。
#   所以下面这张表只在「运行时确无数据」时才生效(见 evaluate 里的 _has_runtime_data),
#   作用仅是给出人话原因, 而不是无条件把维度判死。
#   ⚠️ 别拿 hk_us_overview_builder._sector_rank 的 docstring「港股无则留空」当依据 ——
#   那句话写在 sector 还是空的时候, 已过时。
STATIC_UNAVAILABLE_DIMS: dict[str, frozenset[str]] = {
    "cn": frozenset(),
    "us": frozenset(),
    "hk": frozenset({DIM_INDUSTRY}),
}

STATIC_UNAVAILABLE_REASON: dict[str, dict[str, str]] = {
    "hk": {
        DIM_INDUSTRY: (
            "港股行业榜为空 (universe 无 sector 或当日无行业聚合结果)。"
            "注: 2026-09-19 后 sector 覆盖已达 99.8%, 出现此项通常意味着维表被冲掉"
        )
    },
}

# ─────────────────────────────────────────────────────────────
# 阈值常量 (§2.1: 随本模块存放, 不进 tiers.yaml; 由单测钉死)
# ─────────────────────────────────────────────────────────────

# 一票否决线: 上涨家数占比低于此值直接判防守, 不再投票。
# 单位与 overview.breadth.up_pct 一致 = 百分点 (0~100)。
UP_PCT_FLOOR = 35.0
# 广度投进攻的门槛 (百分点, 0~100)。
UP_PCT_ATTACK = 60.0

# 一票否决的 regime 状态 (弱/偏弱)。
VETO_REGIME_STATES: tuple[str, ...] = ("weak", "lean_weak")
# regime 投票定向: 强/偏强 → 进攻; 弱/偏弱 → 防守; 其余 (震荡) → 中性。
REGIME_ATTACK_STATES: tuple[str, ...] = ("strong", "lean_strong")
REGIME_DEFEND_STATES: tuple[str, ...] = ("weak", "lean_weak")

# 热点阶段定向 (models.HOTSPOT_STAGES 的子集)。
HOTSPOT_ATTACK_STAGES: tuple[str, ...] = ("加速主升", "确认扩散")
HOTSPOT_DEFEND_STAGES: tuple[str, ...] = ("降温退潮", "分歧放量")
# 判定所需占比: 定向阶段数 / 有效主题数 ≥ 此值才投该方向。
HOTSPOT_STAGE_RATIO = 0.5

# 行业强弱定向。单位与 industry_rank[].avg_pct 一致 = 小数 (0.01 = 1%)。
INDUSTRY_ATTACK_PCT = 0.01
INDUSTRY_DEFEND_PCT = -0.01

# 定案门槛: 某方向票数 ≥ 此值才定案, 否则均衡。
MIN_VOTES_FOR_VERDICT = 3

# 热点读取上限 (只读落盘快照, 不影响配额)。
HOTSPOT_TOP = 50


# ─────────────────────────────────────────────────────────────
# 输入快照
# ─────────────────────────────────────────────────────────────


@dataclass
class PostureInputs:
    """posture 计算所需的只读输入快照。

    所有字段默认 None = 该维度不可用。单测可直接构造本对象,
    不依赖 repo / 磁盘 / 网络。
    """

    market: str = "cn"
    regime: dict | None = None               # regime 最新行 (含 state/score/date)
    breadth: dict | None = None              # overview.breadth (含 up_pct/total)
    hotspot_stages: list[str] | None = None  # 各热点主题的 stage
    industry: dict | None = None             # industry_rank {"leading":[...], "lagging":[...]}
    as_of: str | None = None                 # 行情快照日
    regime_as_of: str | None = None          # regime 最新日
    hotspot_age_hours: float | None = None   # 热点快照龄 (小时)
    source_errors: list[str] = field(default_factory=list)


def _finite(value: Any) -> float | None:
    """转 float, 非有限值/不可转换返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct_text(value: float | None) -> str:
    """小数 → 百分比文案 (0.0123 → '+1.23%')。"""
    if value is None:
        return "无数据"
    return f"{value * 100:+.2f}%"


# ─────────────────────────────────────────────────────────────
# 逐维度投票
# ─────────────────────────────────────────────────────────────


def _state_label(state: str) -> str:
    from app.services.regime_builder import STATE_LABELS

    return STATE_LABELS.get(state, state or "未知")


def _regime_vote(row: dict | None) -> dict:
    """regime 维度投票: 强/偏强 → 进攻, 弱/偏弱 → 防守, 震荡 → 中性。"""
    if not row:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "无 regime 数据 (该维度不可用, 不计入分母)",
        }
    state = str(row.get("state") or "").strip()
    if not state:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "regime 缺 state 字段 (该维度不可用, 不计入分母)",
        }
    score = _finite(row.get("score"))
    score_text = f"{score:.0f}" if score is not None else "无"
    label = _state_label(state)
    if state in REGIME_ATTACK_STATES:
        return {"vote": VOTE_ATTACK, "detail": f"regime 状态「{label}」(score={score_text})"}
    if state in REGIME_DEFEND_STATES:
        return {"vote": VOTE_DEFEND, "detail": f"regime 状态「{label}」(score={score_text})"}
    return {"vote": VOTE_NEUTRAL, "detail": f"regime 状态「{label}」(score={score_text})"}


def _breadth_vote(breadth: dict | None) -> dict:
    """涨跌广度投票: up_pct ≥ 进攻线 → 进攻, < 否决线 → 防守, 否则中性。"""
    if not breadth:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "无广度数据 (该维度不可用, 不计入分母)",
        }
    up_pct = _finite(breadth.get("up_pct"))
    total = _finite(breadth.get("total"))
    if up_pct is None or not total:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "广度无有效样本 (该维度不可用, 不计入分母)",
        }
    detail = f"上涨家数占比 {up_pct:.1f}% (否决线 {UP_PCT_FLOOR:.0f}%, 进攻线 {UP_PCT_ATTACK:.0f}%)"
    if up_pct >= UP_PCT_ATTACK:
        return {"vote": VOTE_ATTACK, "detail": detail}
    if up_pct < UP_PCT_FLOOR:
        return {"vote": VOTE_DEFEND, "detail": detail}
    return {"vote": VOTE_NEUTRAL, "detail": detail}


def _hotspots_vote(stages: list[str] | None) -> dict:
    """热点阶段投票: 加速/扩散占比 ≥ 阈值 → 进攻, 降温/分歧占比 ≥ 阈值 → 防守。"""
    if not stages:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "无热点快照 (该维度不可用, 不计入分母)",
        }
    total = len(stages)
    attack_n = sum(1 for s in stages if s in HOTSPOT_ATTACK_STAGES)
    defend_n = sum(1 for s in stages if s in HOTSPOT_DEFEND_STAGES)
    detail = (
        f"{total} 个主题中 加速/扩散 {attack_n} 个、降温/分歧 {defend_n} 个"
        f" (定向门槛 {HOTSPOT_STAGE_RATIO:.0%})"
    )
    if attack_n / total >= HOTSPOT_STAGE_RATIO:
        return {"vote": VOTE_ATTACK, "detail": detail}
    if defend_n / total >= HOTSPOT_STAGE_RATIO:
        return {"vote": VOTE_DEFEND, "detail": detail}
    return {"vote": VOTE_NEUTRAL, "detail": detail}


def _industry_vote(industry: dict | None) -> dict:
    """行业强弱投票: 领涨行业平均涨幅 ≥ +1% → 进攻, 领跌 ≤ -1% → 防守。"""
    leading = list((industry or {}).get("leading") or [])
    lagging = list((industry or {}).get("lagging") or [])
    if not leading and not lagging:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "无行业榜数据 (该维度不可用, 不计入分母)",
        }
    top = _finite((leading[0] or {}).get("avg_pct")) if leading else None
    bottom = _finite((lagging[0] or {}).get("avg_pct")) if lagging else None
    top_name = str((leading[0] or {}).get("name") or "") if leading else ""
    bottom_name = str((lagging[0] or {}).get("name") or "") if lagging else ""

    if top is not None and top >= INDUSTRY_ATTACK_PCT:
        return {
            "vote": VOTE_ATTACK,
            "detail": f"领涨行业「{top_name}」平均涨幅 {_pct_text(top)}",
        }
    if bottom is not None and bottom <= INDUSTRY_DEFEND_PCT:
        return {
            "vote": VOTE_DEFEND,
            "detail": f"领跌行业「{bottom_name}」平均涨幅 {_pct_text(bottom)}",
        }
    if top is None and bottom is None:
        return {
            "vote": VOTE_UNAVAILABLE,
            "detail": "行业榜无有效涨幅 (该维度不可用, 不计入分母)",
        }
    return {
        "vote": VOTE_NEUTRAL,
        "detail": f"领涨「{top_name}」{_pct_text(top)} / 领跌「{bottom_name}」{_pct_text(bottom)}",
    }


# ─────────────────────────────────────────────────────────────
# 计票与定案
# ─────────────────────────────────────────────────────────────


def _tally(votes: dict[str, dict]) -> dict[str, int]:
    """统计有效票。

    ⚠️ 唯一入口: ``unavailable`` **不计入分母**, 既不当 0 也不当中性。
    任何"把不可用折算成一票"的实现都会让港股被永久误判成防守。
    """
    attack = 0
    neutral = 0
    defend = 0
    counted = 0
    for item in votes.values():
        vote = item["vote"]
        if vote == VOTE_UNAVAILABLE:
            continue
        counted += 1
        if vote == VOTE_ATTACK:
            attack += 1
        elif vote == VOTE_DEFEND:
            defend += 1
        else:
            neutral += 1
    return {"attack": attack, "neutral": neutral, "defend": defend, "counted": counted}


def _verdict(tally: dict[str, int]) -> str:
    """按票数定案: ≥3 进攻 → 进攻; ≥3 防守 → 防守; 无有效票 → 未知; 否则均衡。"""
    if tally["counted"] == 0:
        return POSTURE_UNKNOWN
    if tally["attack"] >= MIN_VOTES_FOR_VERDICT:
        return POSTURE_ATTACK
    if tally["defend"] >= MIN_VOTES_FOR_VERDICT:
        return POSTURE_DEFEND
    return POSTURE_BALANCED


def _has_runtime_data(inputs: PostureInputs, dim: str) -> bool:
    """该维度是否有运行时数据(用于区分"结构性缺口"与"数据暂时缺失")。"""
    if dim == DIM_REGIME:
        return bool(inputs.regime)
    if dim == DIM_BREADTH:
        return bool(inputs.breadth) and bool(_finite((inputs.breadth or {}).get("total")))
    if dim == DIM_HOTSPOTS:
        return bool(inputs.hotspot_stages)
    if dim == DIM_INDUSTRY:
        industry = inputs.industry or {}
        return bool(industry.get("leading") or industry.get("lagging"))
    return False


def evaluate(inputs: PostureInputs) -> dict:
    """纯函数: 输入快照 → 态势判定结果。

    Args:
        inputs: 各维度的只读输入快照。

    Returns:
        ``{market, market_label, posture, posture_label, votes, unavailable_dims,
        evidence, veto, tally, as_of, freshness, source_errors}``
    """
    market = inputs.market
    static = STATIC_UNAVAILABLE_DIMS.get(market, frozenset())

    votes: dict[str, dict] = {}
    for dim in VOTING_DIMS:
        if dim in static and not _has_runtime_data(inputs, dim):
            reason = STATIC_UNAVAILABLE_REASON.get(market, {}).get(dim, "该维度对该市场不可用")
            votes[dim] = {"vote": VOTE_UNAVAILABLE, "detail": f"{reason} (不计入分母)"}
            continue
        if dim == DIM_REGIME:
            votes[dim] = _regime_vote(inputs.regime)
        elif dim == DIM_BREADTH:
            votes[dim] = _breadth_vote(inputs.breadth)
        elif dim == DIM_HOTSPOTS:
            votes[dim] = _hotspots_vote(inputs.hotspot_stages)
        else:
            votes[dim] = _industry_vote(inputs.industry)

    # ① 一票否决 (只在相关维度可用时生效; 不可用的维度不能否决)
    veto: dict | None = None
    regime_state = str((inputs.regime or {}).get("state") or "").strip()
    if regime_state and regime_state in VETO_REGIME_STATES:
        veto = {
            "dim": DIM_REGIME,
            "reason": (
                f"regime 状态「{_state_label(regime_state)}」属偏弱/弱势, "
                "一票否决 → 直接判防守, 不再投票"
            ),
        }
    if veto is None:
        up_pct = _finite((inputs.breadth or {}).get("up_pct"))
        total = _finite((inputs.breadth or {}).get("total"))
        if up_pct is not None and total and up_pct < UP_PCT_FLOOR:
            veto = {
                "dim": DIM_BREADTH,
                "reason": (
                    f"上涨家数占比 {up_pct:.1f}% < 否决线 {UP_PCT_FLOOR:.0f}%, "
                    "一票否决 → 直接判防守, 不再投票"
                ),
            }

    tally = _tally(votes)
    if veto is not None:
        posture = POSTURE_DEFEND
    else:
        posture = _verdict(tally)

    # evidence: 每个维度一句人话理由 + 否决说明
    evidence: list[dict] = []
    for dim in VOTING_DIMS:
        item = votes[dim]
        vote = item["vote"]
        label = DIM_LABELS.get(dim, dim)
        if vote == VOTE_UNAVAILABLE:
            text = f"{label}: {item['detail']}"
        else:
            text = f"{label}: {item['detail']} → 投「{VOTE_LABELS.get(vote, vote)}」"
        evidence.append({"dim": dim, "text": text})
    if veto is not None:
        evidence.append({"dim": veto["dim"], "text": veto["reason"]})
        evidence.append({
            "dim": "_verdict",
            "text": f"一票否决生效, 最终判定「{POSTURE_LABELS[posture]}」",
        })
    else:
        evidence.append({
            "dim": "_verdict",
            "text": (
                f"有效票 {tally['counted']} 张 (进攻 {tally['attack']} / 中性 "
                f"{tally['neutral']} / 防守 {tally['defend']}), "
                f"定案门槛 {MIN_VOTES_FOR_VERDICT} 票 → 判定「{POSTURE_LABELS[posture]}」"
            ),
        })

    unavailable_dims = [dim for dim in VOTING_DIMS if votes[dim]["vote"] == VOTE_UNAVAILABLE]

    return {
        "market": market,
        "market_label": MARKET_LABELS.get(market, market),
        "posture": posture,
        "posture_label": POSTURE_LABELS.get(posture, posture),
        "votes": [
            {
                "dim": dim,
                "label": DIM_LABELS.get(dim, dim),
                "vote": votes[dim]["vote"],
                "vote_label": VOTE_LABELS.get(votes[dim]["vote"], votes[dim]["vote"]),
                "detail": votes[dim]["detail"],
            }
            for dim in VOTING_DIMS
        ],
        "unavailable_dims": unavailable_dims,
        "evidence": evidence,
        "veto": veto,
        "tally": tally,
        "as_of": inputs.as_of,
        "freshness": {
            "regime_as_of": inputs.regime_as_of,
            "hotspot_age_hours": inputs.hotspot_age_hours,
        },
        "source_errors": list(inputs.source_errors),
    }


# ─────────────────────────────────────────────────────────────
# 只读采集 (绝不触发同步/刷新)
# ─────────────────────────────────────────────────────────────


def _resolve_data_dir(repo: Any = None, data_dir: Any = None) -> Path:
    """取数据根目录: 显式传入 > repo.store.data_dir > settings.data_dir。

    统一转 Path: 下游 regime/hotspot/overview 的读取都用 ``/`` 拼路径,
    传 str 会抛 TypeError 并被降级成"不可用"(静默丢维度, 极难排查)。
    """
    if data_dir is not None:
        return Path(data_dir)
    store = getattr(repo, "store", None)
    if store is not None and getattr(store, "data_dir", None) is not None:
        return Path(store.data_dir)
    from app.config import settings

    return Path(settings.data_dir)


def _read_regime_row(data_dir: Any, market: str) -> dict | None:
    """读 regime 最新一行 (只读 parquet, 不重算)。"""
    from app.services.regime_builder import load_regime_history

    try:
        df = load_regime_history(data_dir, market=market)
    except Exception as e:
        # 只读路径失败按"不可用"处理, 不能带崩整屏
        logger.warning("posture: regime 读取失败 (%s): %s", market, e)
        return None
    if df is None or df.is_empty():
        return None
    try:
        row = df.sort("date", descending=True).head(1).to_dicts()[0]
    except Exception as e:
        logger.warning("posture: regime 取最新行失败 (%s): %s", market, e)
        return None
    if row.get("date") is not None:
        row["date"] = str(row["date"])
    return row


def _read_hotspot_stages(data_dir: Any, market: str) -> tuple[list[str], float | None]:
    """读热点快照的 stage 列表 (只读 storage, 不走 discover 的刷新路径)。"""
    from app.services.hotspot.storage import read_topics

    try:
        topics = read_topics(data_dir, market=market)
    except Exception as e:
        logger.warning("posture: 热点快照读取失败 (%s): %s", market, e)
        return [], None
    stages = [str(t.stage or "") for t in topics if str(t.stage or "")]
    return stages[:HOTSPOT_TOP], _hotspot_age_hours(data_dir, market)


def _hotspot_age_hours(data_dir: Any, market: str) -> float | None:
    """热点快照龄(小时), 取自 job_state 的按市场成功时间。"""
    from app.services.hotspot.storage import read_job_state

    try:
        state = read_job_state(data_dir)
        raw = (state.get("last_success_at") or {}).get(market)
        if not raw:
            return None
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        return round((datetime.now() - ts).total_seconds() / 3600.0, 2)
    except Exception:
        # 新鲜度是辅助信息, 失败不影响判定
        return None


def _read_overview(market: str, repo: Any, data_dir: Any, quote_service: Any, depth_service: Any,
                   as_of: date | None) -> dict:
    """复用既有装配器取广度与行业榜 (不重写聚合逻辑)。"""
    if market == "cn":
        from app.services.market_overview_builder import build_market_overview

        if repo is None:
            return {}
        return build_market_overview(
            repo=repo,
            quote_service=quote_service,
            depth_service=depth_service,
            as_of=as_of,
        )
    from app.services.hk_us_overview_builder import build_hk_us_overview

    return build_hk_us_overview(market.upper(), data_dir, as_of)


def collect_inputs(
    market: str,
    *,
    repo: Any = None,
    data_dir: Any = None,
    quote_service: Any = None,
    depth_service: Any = None,
    as_of: date | None = None,
) -> PostureInputs:
    """只读采集一个市场的 4 个投票维度输入。

    任何一路读取失败都按"该维度不可用"处理 (fail-closed 到 unavailable,
    而不是当 0), 绝不让整屏 500。
    """
    market = market.lower()
    resolved_dir = _resolve_data_dir(repo, data_dir)
    source_errors: list[str] = []

    regime_row = _read_regime_row(resolved_dir, market)
    if regime_row is None:
        source_errors.append(f"regime: 无 {market} 数据")

    stages, age_hours = _read_hotspot_stages(resolved_dir, market)
    if not stages:
        source_errors.append(f"hotspots: 无 {market} 快照")

    breadth: dict | None = None
    industry: dict | None = None
    overview_as_of: str | None = None
    try:
        overview = _read_overview(market, repo, resolved_dir, quote_service, depth_service, as_of)
        breadth = overview.get("breadth")
        industry = overview.get("industry_rank")
        raw_as_of = overview.get("as_of")
        overview_as_of = str(raw_as_of) if raw_as_of else None
    except Exception as e:
        logger.warning("posture: overview 装配失败 (%s): %s", market, e)
        source_errors.append(f"overview: {e}")

    if not breadth or not _finite(breadth.get("up_pct")):
        source_errors.append(f"breadth: 无 {market} 有效样本")

    return PostureInputs(
        market=market,
        regime=regime_row,
        breadth=breadth,
        hotspot_stages=stages or None,
        industry=industry,
        as_of=overview_as_of,
        regime_as_of=str(regime_row.get("date")) if regime_row and regime_row.get("date") else None,
        hotspot_age_hours=age_hours,
        source_errors=source_errors,
    )


def compute_market_posture(
    market: str,
    *,
    repo: Any = None,
    data_dir: Any = None,
    quote_service: Any = None,
    depth_service: Any = None,
    as_of: date | None = None,
) -> dict:
    """计算单市场态势 (采集 + 判定)。

    Args:
        market: "cn" | "hk" | "us"。
        repo: KlineRepository (A 股 overview 需要)。
        data_dir: 数据根目录, 缺省取 repo.store.data_dir。
        quote_service / depth_service: 可选, 透传给 A 股 overview 装配器。
        as_of: 指定日期, None 取最新。

    Returns:
        evaluate() 的判定结果 dict。
    """
    inputs = collect_inputs(
        market,
        repo=repo,
        data_dir=data_dir,
        quote_service=quote_service,
        depth_service=depth_service,
        as_of=as_of,
    )
    return evaluate(inputs)
