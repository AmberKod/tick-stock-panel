"""组合层约束 — 选股输出的行业集中度控制 (借鉴 AlphaSift portfolio_profile)。

与 basic_filter (行级过滤) 不同, 组合约束作用于**截面集合**:
按行业分桶, 限制候选集中同桶最多 N 只 (超出的从结果中剔除, 保留桶内 score 最高者),
可选对桶内后续标的施加集中度惩罚 (score x (1 - penaltyx超限数), 降分后重排)。
这是信号候选集合的约束,不读取持仓,也不保证实际持仓的行业集中度。

数据依赖: symbol → industry 映射。A股读 ext_data/ext_hy_ths (同花顺行业, level-1
取一级); 港股 hk_instruments.sector; 美股 us_instruments.sector。映射缺失的标的
归入 "__unknown__" 桶, 不受约束 (宁漏勿错)。

设计原则:
- 纯函数: apply_portfolio_constraints(rows, industry_map, config) → rows
- 输入输出均为 list[dict] (与 StrategyResult.rows 同构), 不依赖 polars
- config 全字段可选, None/空 config 直通
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import polars as pl

UNKNOWN_BUCKET = "__unknown__"

DEFAULT_MAX_SAME_INDUSTRY = 2
DEFAULT_CONCENTRATION_PENALTY = 0.0  # 默认只剔除不惩罚 (保守)

_lock = threading.Lock()
_industry_cache: dict[tuple[str, str, int], tuple[tuple, dict[str, str]]] = {}


def clear_industry_cache() -> None:
    """清空行业映射缓存 (测试隔离 / 行业数据重新同步后调用)。"""
    with _lock:
        _industry_cache.clear()


def _parse_industry(raw: str | None, level: int) -> str:
    """'医药生物-医疗器械-医疗耗材' 按 level 取级 (level=1 → 医药生物)。"""
    if not raw:
        return UNKNOWN_BUCKET
    parts = [p.strip() for p in str(raw).split("-") if p.strip()]
    if not parts:
        return UNKNOWN_BUCKET
    if level <= 1:
        return parts[0]
    return "-".join(parts[:min(level, len(parts))])


def _load_ths_industry(data_dir: Path, level: int) -> dict[str, str]:
    """A股: ext_data/ext_hy_ths/part.parquet → {symbol: 一级行业}。"""
    path = data_dir / "ext_data" / "ext_hy_ths" / "part.parquet"
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path)
    except Exception:
        return {}
    field = next(
        (c for c in ("所属同花顺行业", "行业", "industry") if c in df.columns),
        None,
    )
    if field is None or "symbol" not in df.columns:
        return {}
    return {
        str(sym): _parse_industry(raw, level)
        for sym, raw in zip(df["symbol"].to_list(), df[field].to_list(), strict=False)
    }


def _load_instrument_sector(data_dir: Path, fname: str) -> dict[str, str]:
    """港美: instruments parquet 的 sector 列 → {symbol: sector}。"""
    path = data_dir / "instruments" / fname
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path, columns=["symbol", "sector"])
    except Exception:
        return {}
    out: dict[str, str] = {}
    for sym, raw in zip(df["symbol"].to_list(), df["sector"].to_list(), strict=False):
        bucket = _parse_industry(raw, 1)
        if bucket != UNKNOWN_BUCKET:
            out[str(sym)] = bucket
    return out


def industry_mapping_signature(
    data_dir: Path | None, market: str = "cn", level: int = 1,
) -> tuple:
    """Identify the exact mapping snapshot used by derived strategy results."""
    normalized_dir = os.path.normcase(str(Path(data_dir).resolve())) if data_dir is not None else ""
    if data_dir is None:
        return normalized_dir, market, level, None, None
    if market == "cn":
        path = Path(data_dir) / "ext_data" / "ext_hy_ths" / "part.parquet"
    elif market in ("hk", "us"):
        path = Path(data_dir) / "instruments" / f"{market}_instruments.parquet"
    else:
        return normalized_dir, market, level, None, None
    try:
        stat = path.stat()
    except OSError:
        return normalized_dir, market, level, None, None
    return normalized_dir, market, level, stat.st_mtime_ns, stat.st_size


def industry_map_for_market(
    data_dir: Path | None, market: str = "cn", level: int = 1,
) -> dict[str, str]:
    """按目录、市场、级别与文件版本缓存 symbol → 行业桶映射。

    cn: 同花顺一级行业; hk/us: instruments.sector。混合市场 (跨市场自选池)
    时调用方可多次调用后 merge —— 本函数不做合并判断。
    """
    if data_dir is None:
        return {}
    signature = industry_mapping_signature(data_dir, market, level)
    key = signature[:3]
    with _lock:
        cached = _industry_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]

    if market == "cn":
        mapping = _load_ths_industry(data_dir, level)
    elif market == "hk":
        mapping = _load_instrument_sector(data_dir, "hk_instruments.parquet")
    elif market == "us":
        mapping = _load_instrument_sector(data_dir, "us_instruments.parquet")
    else:
        mapping = {}

    with _lock:
        _industry_cache[key] = signature, mapping
    return mapping


def industry_mapping_version(data_dir: Path | None, market: str = "cn") -> str:
    """Return a cache token without exposing local filesystem paths in API data."""
    signature = industry_mapping_signature(data_dir, market)
    return hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()


def infer_market_of_rows(rows: list[dict]) -> str:
    """从 rows 的 symbol 后缀推断主导市场 (cn/hk/us)。"""
    counts = Counter()
    for r in rows:
        sym = str(r.get("symbol") or "")
        if sym.endswith(".HK"):
            counts["hk"] += 1
        elif sym.endswith(".US"):
            counts["us"] += 1
        else:
            counts["cn"] += 1
    return counts.most_common(1)[0][0] if counts else "cn"


def normalize_portfolio_config(raw: Mapping[str, Any] | None) -> dict | None:
    """解析 portfolio 约束配置; 无有效字段返回 None (直通)。

    支持字段:
    - enabled: bool (默认 True, 显式 False 直通)
    - max_same_industry: int ≥1 (默认 2)
    - concentration_penalty: float 0~1 (默认 0 只剔除不惩罚)
    - industry_level: 1|2|3 (A股同花顺行业分级, 默认 1)
    """
    if not isinstance(raw, Mapping):
        return None
    if raw.get("enabled") is False:
        return None
    cfg: dict[str, Any] = {}
    max_same = raw.get("max_same_industry")
    penalty = raw.get("concentration_penalty")
    level = raw.get("industry_level")
    if max_same is not None or penalty is not None or level is not None or raw.get("enabled") is True:
        try:
            cfg["max_same_industry"] = max(1, int(max_same)) if max_same is not None else DEFAULT_MAX_SAME_INDUSTRY
        except (TypeError, ValueError):
            cfg["max_same_industry"] = DEFAULT_MAX_SAME_INDUSTRY
        try:
            p = float(penalty) if penalty is not None else DEFAULT_CONCENTRATION_PENALTY
            cfg["concentration_penalty"] = min(max(p, 0.0), 1.0)
        except (TypeError, ValueError):
            cfg["concentration_penalty"] = DEFAULT_CONCENTRATION_PENALTY
        try:
            lv = int(level) if level is not None else 1
            cfg["industry_level"] = lv if lv in (1, 2, 3) else 1
        except (TypeError, ValueError):
            cfg["industry_level"] = 1
        return cfg
    return None


def apply_portfolio_constraints(
    rows: list[dict],
    industry_map: Mapping[str, str],
    config: Mapping[str, Any] | None,
) -> list[dict]:
    """对已排序的选股结果施加行业集中度约束。

    rows 须已按 score 降序 (调用方排序)。同桶超出 max_same_industry 的标的:
    - penalty=0: 直接剔除 (保名额内最高分)
    - penalty>0: 保留但降分 score × (1 - penalty × 超限序号)
    未入桶标的 (industry_map 无 symbol) 归 __unknown__, 不受约束。

    config 为 None 时直通返回 (零开销)。
    """
    cfg = normalize_portfolio_config(config)
    if cfg is None or not rows:
        return rows
    max_same = cfg["max_same_industry"]
    penalty = cfg["concentration_penalty"]

    bucket_count: Counter[str] = Counter()
    bucket_excess: dict[str, int] = defaultdict(int)  # 惩罚保留的额外超限数 (不占名额)
    kept: list[dict] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        bucket = industry_map.get(symbol, UNKNOWN_BUCKET)
        if bucket == UNKNOWN_BUCKET:
            kept.append(row)
            continue
        taken = bucket_count[bucket]
        if taken < max_same:
            bucket_count[bucket] += 1
            kept.append(row)
        elif penalty > 0:
            # 惩罚保留: 不占桶名额, 每多留一只惩罚递增 (第1只超限 ×(1-p), 第2只 ×(1-2p)...)
            bucket_excess[bucket] += 1
            excess_index = bucket_excess[bucket]
            demoted = dict(row)
            score = row.get("score")
            if isinstance(score, (int, float)):
                demoted["score"] = float(score) * (1.0 - penalty * excess_index)
            demoted["portfolio_constrained"] = True
            kept.append(demoted)

    if penalty > 0:
        kept.sort(key=lambda r: r.get("score") if isinstance(r.get("score"), (int, float)) else 0, reverse=True)
    return kept
