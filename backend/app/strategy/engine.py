"""策略引擎 — 加载、执行、评分。

职责: 从文件系统加载策略 Python 模块，执行两阶段过滤(基础+策略)，
     通用评分排序。
不知道: AI、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.strategy.concept_heat import (
    HISTORICAL_REASON,
    ConceptMappingSnapshot,
    attach_concept_heat,
    concept_heat_availability,
    concept_heat_required,
    concept_market,
    concept_quote_date,
    load_concept_mapping,
)
from app.strategy.scoring import (
    SCORING_DIRECTION_LOW,
    effective_scoring,
    effective_scoring_directions,
    materialize_scoring_columns,
    scoring_dependencies,
    scoring_value_expr,
    scoring_warmup_bars,
)

logger = logging.getLogger(__name__)

# 引擎级默认基础过滤 — 策略未定义 BASIC_FILTER 时兜底
DEFAULT_BASIC_FILTER: dict = {
    "price_min": 3,
    "price_max": 300,
    "market_cap_min": 10e8,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 0.2e8,
    "amount_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "exclude_st": True,
    "exclude_new_days": 30,
    "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"],
}

# 叠加策略硬上限：子策略数量。控制信号计算成本与字段并集膨胀，避免 OOM。
MAX_COMPOSITE_CHILDREN = 8


BASIC_FILTER_NUMERIC_FIELDS: dict[str, tuple[str, ...]] = {
    "price": ("close",),
    "market_cap": ("close", "total_shares"),
    "float_cap": ("close", "float_shares"),
    "amount": ("amount",),
    "turnover": ("turnover_rate",),
    "change_pct": ("change_pct",),
    "pe_ttm": ("pe_ttm",),
    "pb": ("pb",),
}


def market_basic_filter(
    base: dict,
    asset_type: str,
    *,
    overrides: dict | None = None,
    explicit_keys: frozenset[str] | None = None,
) -> dict:
    """Resolve market defaults before applying explicit strategy/user choices.

    Older callers that did not record provenance treat CN-specific values in
    ``base`` as engine defaults. Explicit share-cap bounds remain enabled and
    require real share data; they are never silently disabled.
    """
    resolved = dict(base or {})
    market_defaults = resolved.pop("market_defaults", {})
    if asset_type in ("hk", "us"):
        defaults = {"boards": [], "exclude_st": False, "exclude_new_days": 0}
        defaults.update(dict.fromkeys((
            "market_cap_min", "market_cap_max", "float_cap_min", "float_cap_max",
        )))
        for key, value in defaults.items():
            if key not in (explicit_keys or frozenset()):
                resolved[key] = value
    if isinstance(market_defaults, dict) and isinstance(market_defaults.get(asset_type), dict):
        resolved.update(market_defaults[asset_type])
    if overrides:
        resolved.update(overrides)
    if asset_type in ("hk", "us") and resolved.get("enabled", True):
        unsupported = [
            key for key in ("boards", "exclude_st", "exclude_new_days") if resolved.get(key)
        ]
        if unsupported:
            raise ValueError(
                "港美股不支持所选 A 股基础规则: " + ", ".join(unsupported)
                + ";请关闭这些条件后重试"
            )
    return resolved


def basic_filter_dependencies(config: dict) -> set[str]:
    """Return the exact inputs required by enabled basic-filter conditions."""
    if not config or not config.get("enabled", True):
        return set()
    dependencies: set[str] = set()
    for prefix, names in BASIC_FILTER_NUMERIC_FIELDS.items():
        if any(config.get(f"{prefix}_{bound}") is not None for bound in ("min", "max")):
            dependencies.update(names)
    if config.get("exclude_st"):
        dependencies.add("name")
    if config.get("boards"):
        dependencies.add("symbol")
    return dependencies


def filter_dependencies(strategy: StrategyDef, params: dict) -> tuple[set[str], bool]:
    """Share expression-root dependency checks between current and historical runs."""
    declared = set(strategy.required_features)
    function = strategy.filter_history_fn or strategy.filter_fn
    constants = getattr(getattr(function, "__code__", None), "co_consts", ())
    if strategy.filter_history_fn:
        if "concept_heat" in constants:
            declared.add("concept_heat")
        return declared, bool(declared)
    if not strategy.filter_fn:
        return declared, True
    try:
        expression = strategy.filter_fn(pl.DataFrame(), params)
        return declared | (set(expression.meta.root_names()) if expression is not None else set()), True
    except Exception as exc:
        logger.debug("strategy filter dependency resolution failed: %s", exc)
        if "concept_heat" in constants:
            declared.add("concept_heat")
        return declared, bool(declared)


def strategy_feature_dependencies(strategy: StrategyDef, params: dict) -> set[str]:
    """Keep snapshot identities while resolving declared and dynamic inputs."""
    features = set(strategy.required_features) | filter_dependencies(strategy, params)[0]
    if strategy.meta.get("order_by"):
        features.add(str(strategy.meta["order_by"]))
    if strategy.matrix_strategy is not None:
        features.update(strategy.matrix_strategy.required_fields())
        parameter_fields = getattr(strategy.matrix_strategy, "required_fields_for_params", None)
        if callable(parameter_fields):
            features.update(parameter_fields(params))
    return features


def _validate_filter_data(df: pl.DataFrame, config: dict) -> None:
    """Reject unavailable enabled inputs while allowing explicitly reported row gaps."""
    if df.is_empty():
        return
    dependencies = basic_filter_dependencies(config)
    missing = sorted(dependencies - set(df.columns))
    if missing:
        raise ValueError("基础筛选不可计算,缺少字段: " + ", ".join(missing))
    numeric = dependencies - {"name", "symbol"}
    unavailable = [
        name for name in sorted(dependencies)
        if not df.select(
            (pl.col(name).cast(pl.Float64, strict=False).is_finite()
             if name in numeric else pl.col(name).is_not_null()).any()
        ).item()
    ]
    if unavailable:
        raise ValueError("基础筛选不可计算,字段没有有效数据: " + ", ".join(unavailable))


def _normalize_param_defs(params: Any) -> list[dict]:
    """把 META["params"] 归一化为标准 list[dict] (每项含 id/label/type/default).

    支持的输入格式:
    - list[dict] (标准): 保持, 补齐缺失的 id/label/type/default 字段
    - dict ({"lookback": 20} 或 {"lookback": {"default": 20, "type": "int"}}):
      按 key 作参数 id 转换
    - list[str] (["lookback", "threshold"]): 每项作 id, default=None
    - 其他类型 / 不可识别项: 丢弃并 warning 记录; 整体异常则返回空 list (降级而非崩溃)

    保证下游 {p["id"]: p["default"] for p in params} 永远不会因格式问题抛 TypeError.
    """
    if params is None:
        return []

    # dict 格式: {"lookback": 20} 或 {"lookback": {"default": 20, "type": "int"}}
    if isinstance(params, dict):
        items: list[dict] = []
        for key, val in params.items():
            if not isinstance(key, str) or not key:
                continue
            item = {"id": key, **val} if isinstance(val, dict) else {"id": key, "default": val}
            items.append(item)
        return [_normalize_param_item(item) for item in items]

    # 期望是 list/tuple, 其他类型直接降级
    if not isinstance(params, (list, tuple)):
        logger.warning("strategy params 非标准格式 (%s), 已降级为空 list", type(params).__name__)
        return []

    result: list[dict] = []
    for i, p in enumerate(params):
        if isinstance(p, str):
            result.append({"id": p, "default": None})
        elif isinstance(p, dict):
            item = _normalize_param_item(p)
            if item:  # 缺 id 等异常项 _normalize_param_item 返回空 dict, 丢弃
                result.append(item)
        else:
            logger.warning("strategy params[%d] 不可识别 (%s), 已丢弃", i, type(p).__name__)
    return result


def _normalize_param_item(item: dict) -> dict:
    """补齐单个参数定义的默认字段, 保证 id/label/type/default 都存在."""
    norm = dict(item)
    if "id" not in norm or not norm["id"]:
        logger.warning("strategy param 定义缺少 id, 已丢弃: %s", item)
        return {}
    norm.setdefault("label", str(norm["id"]))
    norm.setdefault("type", "float")
    norm.setdefault("default", None)
    return norm


def _parse_composite_children(raw: Any) -> CompositeSpec:
    """解析 META["children"] 为 CompositeSpec。

    每项形如 {"strategy_id": "xxx", "weight": 0.4}。仅做结构和权重校验:
    - 非空 list, 每项含合法 strategy_id 与非负 weight
    - 数量 <= MAX_COMPOSITE_CHILDREN (超出在加载期拒绝, 避免信号计算成本爆炸)
    子策略的存在性/非嵌套/asset_types 一致性由 _load_all 两阶段校验保证。
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError("composite strategy META['children'] must be a non-empty list")
    if len(raw) > MAX_COMPOSITE_CHILDREN:
        raise ValueError(
            f"composite strategy children count {len(raw)} exceeds limit {MAX_COMPOSITE_CHILDREN}"
        )
    children: list[CompositeChild] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"composite children[{i}] must be a dict")
        cid = item.get("strategy_id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"composite children[{i}] missing non-empty 'strategy_id'")
        if cid in seen:
            raise ValueError(f"composite children[{i}] duplicate strategy_id {cid!r}")
        seen.add(cid)
        weight = item.get("weight", 1.0)
        try:
            weight = float(weight)
        except (TypeError, ValueError) as e:
            raise ValueError(f"composite children[{i}] weight must be a number") from e
        if weight < 0:
            raise ValueError(f"composite children[{i}] weight must be >= 0")
        children.append(CompositeChild(strategy_id=cid, weight=weight))
    return CompositeSpec(children=tuple(children))


@dataclass
class StrategyDataContext:
    """一次策略调用所需的标准数据上下文。"""

    asset_type: str
    timeframe: str
    as_of: date
    current: pl.DataFrame | None = None
    history: pl.DataFrame | None = None
    market: Any | None = None
    cache_key: str | None = None
    data_generation: str | None = None
    is_historical: bool = False
    concept_snapshot: ConceptMappingSnapshot | None = None
    concept_heat_metadata: dict = field(default_factory=dict)
    concept_materialized_frames: tuple[Any, Any, Any] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class CompositeChild:
    """叠加策略的一个子策略引用。"""

    strategy_id: str
    weight: float


@dataclass(frozen=True)
class CompositeSpec:
    """叠加策略的子策略声明（无业务代码，仅引用与权重）。

    引用合法性与一致性由 _load_all 两阶段校验保证：子策略必须存在、
    非嵌套、asset_types 一致、数量 ≤ MAX_COMPOSITE_CHILDREN。
    """

    children: tuple[CompositeChild, ...]


@dataclass
class StrategyDef:
    """加载后的策略定义（只读数据 + filter 函数引用）"""
    meta: dict
    basic_filter: dict
    entry_signals: list[str]
    exit_signals: list[str]
    stop_loss: float | None
    trailing_stop: float | None
    trailing_take_profit_activate: float | None
    trailing_take_profit_drawdown: float | None
    max_hold_days: int | None
    filter_fn: Callable[[pl.DataFrame, dict], pl.Expr] | None
    filter_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None
    lookback_days: int
    source: str  # "builtin" | "custom" | "ai" | "composite"
    required_features: frozenset[str] = field(default_factory=frozenset)
    file_path: Path | None = None
    execution_backend: str = "polars_expr"
    matrix_strategy: Any | None = None
    composite: CompositeSpec | None = None  # 仅 backend=="composite" 时非空
    basic_filter_explicit_keys: frozenset[str] = frozenset()


@dataclass
class StrategyResult:
    """策略执行结果"""
    as_of: date
    strategy_id: str
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)
    entry_signal_hits: list[dict] = field(default_factory=list)
    exit_signal_hits: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    industry_mapping_version: str | None = None
    concept_heat_metadata: dict = field(default_factory=dict)


@dataclass
class _RealtimeMatrixEntry:
    fingerprint: tuple[Any, ...]
    buffer: Any


class StrategyEngine:
    """策略引擎 — 策略加载 + 执行 + 评分"""

    _module_load_lock = threading.RLock()

    def __init__(
        self,
        strategy_dirs: list[Path] | None = None,
        *,
        override_loader: Callable[[str], dict] | None = None,
        data_dir: Path | None = None,
    ):
        self._strategies: dict[str, StrategyDef] = {}
        self._load_errors: list[dict] = []  # 加载失败的策略 [{file, error}]
        self._strategy_dirs = strategy_dirs or []
        self._realtime_matrices: dict[str, _RealtimeMatrixEntry] = {}
        self._realtime_matrix_lock = threading.RLock()
        # 可选的 override 加载器: 叠加策略执行时用它查子策略的用户覆盖配置,
        # 保证 composite 内跑子策略与单独跑子策略使用同一口径(CONTRIBUTING §5.1)。
        # None 时(测试/无 data_dir) 子策略用默认参数, 不报错。
        self._override_loader = override_loader
        # data_dir 用于组合层约束的行业映射加载 (ext_hy_ths / instruments.sector);
        # None 时 (测试) portfolio 约束优雅降级为直通。
        self._data_dir = data_dir
        self._load_all(retain_previous_on_error=False)

    # ================================================================
    # 加载
    # ================================================================

    def _load_all(self, *, retain_previous_on_error: bool) -> bool:
        candidates: dict[str, StrategyDef] = {}
        candidate_paths: dict[str, Path] = {}
        errors: list[dict] = []
        duplicate_ids: set[str] = set()
        for d in self._strategy_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                try:
                    s = self._load_file(f)
                    strategy_id = str(s.meta["id"])
                    if strategy_id in duplicate_ids:
                        errors.append({
                            "file": str(f),
                            "error": f"duplicate strategy id {strategy_id!r}",
                        })
                        continue
                    if strategy_id in candidates:
                        previous_path = candidate_paths.pop(strategy_id)
                        candidates.pop(strategy_id)
                        duplicate_ids.add(strategy_id)
                        message = f"duplicate strategy id {strategy_id!r}"
                        errors.extend([
                            {"file": str(previous_path), "error": message},
                            {"file": str(f), "error": message},
                        ])
                        continue
                    candidates[strategy_id] = s
                    candidate_paths[strategy_id] = f
                except Exception as e:
                    logger.warning("load strategy %s failed: %s", f.name, e)
                    errors.append({"file": str(f), "error": str(e)})

        # 第二阶段: 校验 composite 引用合法性。
        # _load_file 是 staticmethod, 加载单个文件时无法判断 child 是否存在;
        # 此处已拿到全部 candidates, 可做引用、嵌套、asset_types 与数量校验。
        # 孤儿 composite (引用不合法) 被移出 candidates 并记入 errors,
        # 但不触发整体 reload 失败 —— 不波及其他正常策略(插件隔离原则)。
        for sid in list(candidates):
            strategy = candidates[sid]
            if strategy.execution_backend != "composite" or strategy.composite is None:
                continue
            error = self._validate_composite_references(sid, strategy, candidates)
            if error is not None:
                errors.append({
                    "file": str(strategy.file_path) if strategy.file_path else sid,
                    "error": error,
                })
                candidates.pop(sid, None)
                candidate_paths.pop(sid, None)

        self._load_errors = errors
        if errors and retain_previous_on_error:
            return False

        self._strategies = candidates
        for strategy_id, strategy in candidates.items():
            logger.debug("loaded strategy: %s (%s)", strategy_id, strategy.source)
        return not errors

    def load_errors(self) -> list[dict]:
        """返回最近一次 _load_all 中加载失败的策略 [{file, error}]。"""
        return list(self._load_errors)

    @staticmethod
    def _validate_composite_references(
        sid: str,
        strategy: StrategyDef,
        candidates: dict[str, StrategyDef],
    ) -> str | None:
        """校验 composite 策略的引用合法性。返回错误描述或 None。

        规则(首版硬约束):
        - 每个 child 必须已加载(candidates 中存在)
        - 禁止 composite 嵌套 composite(子策略必须是叶子)
        - child 的 asset_types / timeframes 必须覆盖父 composite 声明的范围
        - 数量 <= MAX_COMPOSITE_CHILDREN
        任一不满足返回错误描述, 由 _load_all 移除该孤儿策略(不波及无辜)。
        """
        assert strategy.composite is not None
        children = strategy.composite.children
        if len(children) > MAX_COMPOSITE_CHILDREN:
            return (
                f"composite strategy {sid} children count {len(children)} "
                f"exceeds limit {MAX_COMPOSITE_CHILDREN}"
            )
        parent_assets = list(strategy.meta.get("asset_types", ["stock"]))
        parent_timeframes = list(strategy.meta.get("timeframes", ["1d"]))
        for child in children:
            child_def = candidates.get(child.strategy_id)
            if child_def is None:
                return f"composite strategy {sid} 引用的子策略 {child.strategy_id!r} 不存在"
            if child_def.execution_backend == "composite":
                return (
                    f"composite strategy {sid} 引用的子策略 {child.strategy_id!r} "
                    f"也是叠加策略; 首版禁止嵌套叠加"
                )
            child_assets = list(child_def.meta.get("asset_types", ["stock"]))
            if not set(parent_assets).issubset(set(child_assets)):
                return (
                    f"composite strategy {sid} 的 asset_types {parent_assets} "
                    f"未被子策略 {child.strategy_id!r} 完全支持(支持 {child_assets})"
                )
            child_timeframes = list(child_def.meta.get("timeframes", ["1d"]))
            if not set(parent_timeframes).issubset(set(child_timeframes)):
                return (
                    f"composite strategy {sid} 的 timeframes {parent_timeframes} "
                    f"未被子策略 {child.strategy_id!r} 完全支持(支持 {child_timeframes})"
                )
        return None

    @staticmethod
    def _load_file(path: Path) -> StrategyDef:
        """从 Python 文件加载策略定义"""
        # 纵深防御: 执行前再跑一次 AST 安全校验, 防止策略文件被直接篡改
        # 绕过 API 校验后, 在 exec_module 时执行恶意代码。
        dependency_paths = [
            candidate
            for candidate in path.parent.glob("_*.py")
            if candidate != path
        ]
        dependency_names = frozenset(candidate.stem for candidate in dependency_paths)
        try:
            code = path.read_text(encoding="utf-8")
            from app.strategy.ai_generator import AIStrategyGenerator
            AIStrategyGenerator._validate_safety(
                code,
                extra_allowed_import_modules=dependency_names,
            )
            for dependency_path in dependency_paths:
                AIStrategyGenerator._validate_safety(
                    dependency_path.read_text(encoding="utf-8"),
                    extra_allowed_import_modules=frozenset({
                        "collections.abc",
                        "types",
                        "typing",
                    }),
                    extra_allowed_calls=frozenset({"vars"}),
                )
        except ValueError:
            raise
        except Exception:
            # 文件读不到/语法错等: 不阻断, 让下方 exec_module 抛原样错误
            pass

        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        with StrategyEngine._module_load_lock:
            previous_module = sys.modules.get(spec.name)
            sys.modules[spec.name] = mod
            inserted_path = str(path.parent)
            sys.path.insert(0, inserted_path)
            try:
                for dependency_name in dependency_names:
                    sys.modules.pop(dependency_name, None)
                spec.loader.exec_module(mod)
            except Exception:
                if previous_module is None:
                    sys.modules.pop(spec.name, None)
                else:
                    sys.modules[spec.name] = previous_module
                raise
            finally:
                with contextlib.suppress(ValueError):
                    sys.path.remove(inserted_path)

        meta = dict(getattr(mod, "META", {}) or {})
        meta.setdefault("id", path.stem)
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("tags", [])
        meta.setdefault("params", [])
        meta.setdefault("scoring", {})
        meta.setdefault("order_by", "score")
        meta.setdefault("descending", True)
        meta.setdefault("limit", 100)

        source = "custom"
        normalized_path = str(path).replace("\\", "/")
        if "/builtin/" in normalized_path:
            source = "builtin"
        elif "/ai/" in normalized_path:
            source = "ai"
        elif "/composite/" in normalized_path:
            source = "composite"

        if source == "builtin" and "asset_types" not in meta:
            raise ValueError("builtin strategy META must declare asset_types")
        meta.setdefault("asset_types", ["stock"])
        meta.setdefault("timeframes", ["1d"])
        for field_name in ("asset_types", "timeframes"):
            values = meta.get(field_name)
            if (
                not isinstance(values, (list, tuple))
                or not values
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError(f"META[{field_name!r}] must be a non-empty string list")
            meta[field_name] = list(dict.fromkeys(values))

        # 归一化 params 为标准 list[dict]: custom/AI 策略的 META["params"] 可能是
        # dict / list[str] 等非标准格式 (LLM 偶发漂移 / 用户手改), 不归一化的话会在
        # _strategy_detail() 的 {p["id"]: p["default"] for p in params} 处抛 TypeError,
        # 导致整个 /api/strategies 列表 500. 降级为空 list 而非崩溃, 策略仍可见可用.
        meta["params"] = _normalize_param_defs(meta.get("params"))

        # 合并默认基础过滤
        bf = {**DEFAULT_BASIC_FILTER}
        strat_bf = getattr(mod, "BASIC_FILTER", None)
        if strat_bf:
            bf.update(strat_bf)
        # meta 里的 basic_filter 也合并（优先级最高）
        meta_bf = meta.get("basic_filter")
        if meta_bf:
            bf.update(meta_bf)

        filter_fn = getattr(mod, "filter", None)
        filter_history_fn = getattr(mod, "filter_history", None)
        execution_backend = str(
            getattr(
                mod,
                "EXECUTION_BACKEND",
                meta.get(
                    "execution_backend",
                    "python_history_legacy" if filter_history_fn else "polars_expr",
                ),
            )
        )
        valid_backends = {"polars_expr", "matrix_native", "python_history_legacy", "composite"}
        if execution_backend not in valid_backends:
            raise ValueError(
                f"unsupported execution backend {execution_backend!r}; "
                f"expected one of {sorted(valid_backends)}"
            )

        matrix_strategy = getattr(mod, "MATRIX_STRATEGY", None)
        composite_spec: CompositeSpec | None = None
        if execution_backend == "matrix_native":
            from app.backtest.matrix import MatrixStrategy

            if matrix_strategy is None:
                raise ValueError("matrix_native strategy must declare MATRIX_STRATEGY")
            if not isinstance(matrix_strategy, MatrixStrategy):
                raise TypeError("MATRIX_STRATEGY must implement MatrixStrategy")
            if filter_fn is not None or filter_history_fn is not None:
                raise ValueError("matrix_native strategy must not declare filter or filter_history")
        elif execution_backend == "polars_expr":
            if filter_fn is None or filter_history_fn is not None:
                raise ValueError("polars_expr strategy must declare only filter")
        elif execution_backend == "composite":
            # 叠加策略是声明式的: 不含业务代码, 仅通过 META["children"] 引用其他策略。
            # 引用合法性(子策略存在/非嵌套/asset_types 一致/数量上限)延后到
            # _load_all 两阶段校验 —— 因为此时注册表尚未加载完, 无法判断 child 是否存在。
            if (
                filter_fn is not None
                or filter_history_fn is not None
                or matrix_strategy is not None
            ):
                raise ValueError(
                    "composite strategy must not declare filter, filter_history or MATRIX_STRATEGY"
                )
            composite_spec = _parse_composite_children(meta.get("children"))
        elif filter_history_fn is None or filter_fn is not None:
            raise ValueError("python_history_legacy strategy must declare only filter_history")

        return StrategyDef(
            meta=meta,
            basic_filter=bf,
            basic_filter_explicit_keys=frozenset(strat_bf or {}) | frozenset(meta_bf or {}),
            entry_signals=getattr(mod, "ENTRY_SIGNALS", []),
            exit_signals=getattr(mod, "EXIT_SIGNALS", []),
            stop_loss=getattr(mod, "STOP_LOSS", None),
            trailing_stop=getattr(mod, "TRAILING_STOP", None),
            trailing_take_profit_activate=getattr(mod, "TRAILING_TAKE_PROFIT_ACTIVATE", None),
            trailing_take_profit_drawdown=getattr(mod, "TRAILING_TAKE_PROFIT_DRAWDOWN", None),
            max_hold_days=getattr(mod, "MAX_HOLD_DAYS", None),
            filter_fn=filter_fn,
            filter_history_fn=filter_history_fn,
            required_features=frozenset(meta.get("required_features", []) or [])
            | frozenset(getattr(mod, "REQUIRED_FEATURES", []) or []),
            lookback_days=int(getattr(mod, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1),
            source=source,
            file_path=path,
            execution_backend=execution_backend,
            matrix_strategy=matrix_strategy,
            composite=composite_spec,
        )

    def reload(self) -> None:
        """原子热重载；任一策略失败时保留上一版注册表。"""
        if not self._load_all(retain_previous_on_error=True):
            details = "; ".join(
                f"{item['file']}: {item['error']}" for item in self._load_errors
            )
            raise ValueError(f"strategy reload failed: {details}")
        with self._realtime_matrix_lock:
            self._realtime_matrices.clear()

    # ================================================================
    # 查询
    # ================================================================

    def list_strategies(self, *, include_research: bool = False) -> list[dict]:
        """Return public strategy metadata unless research templates are requested."""
        result = []
        for s in self._strategies.values():
            if s.meta.get("research_only") and not include_research:
                continue
            result.append({
                **s.meta,
                "source": s.source,
                "execution_backend": s.execution_backend,
            })
        return result

    def strategy_definitions(self) -> tuple[StrategyDef, ...]:
        """Return the immutable registry snapshot for framework dependency planning."""
        return tuple(self._strategies.values())

    def get(self, strategy_id: str) -> StrategyDef:
        s = self._strategies.get(strategy_id)
        if not s:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return s

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    def unregister(self, strategy_id: str) -> bool:
        """从运行时注册表移除单个策略, 不重新加载其他策略文件。"""
        if strategy_id not in self._strategies:
            return False
        strategies = dict(self._strategies)
        strategies.pop(strategy_id)
        self._strategies = strategies
        with self._realtime_matrix_lock:
            self._realtime_matrices.clear()
        return True

    def find_dependents(self, strategy_id: str) -> list[str]:
        """返回引用了 strategy_id 作为子策略的所有 composite 策略 id。

        供删除校验使用: 删除被引用的子策略会令 composite 加载失败,
        删除前应阻止(fail-closed)或提示用户先解除引用。策略数量通常很小,
        线性遍历注册表即可, 无需维护反向索引。
        """
        dependents: list[str] = []
        for sid, strategy in self._strategies.items():
            if strategy.execution_backend != "composite" or strategy.composite is None:
                continue
            if any(c.strategy_id == strategy_id for c in strategy.composite.children):
                dependents.append(sid)
        return dependents

    @staticmethod
    def validate_context(strategy: StrategyDef, context: StrategyDataContext) -> None:
        asset_types = strategy.meta.get("asset_types", ["stock"])
        if context.asset_type not in asset_types:
            raise ValueError(
                f"strategy {strategy.meta['id']} does not support asset_type "
                f"{context.asset_type!r}; supported={asset_types}"
            )
        timeframes = strategy.meta.get("timeframes", ["1d"])
        if context.timeframe not in timeframes:
            raise ValueError(
                f"strategy {strategy.meta['id']} does not support timeframe "
                f"{context.timeframe!r}; supported={timeframes}"
            )

    @staticmethod
    def resolve_params(
        strategy: StrategyDef,
        params: dict | None = None,
        overrides: dict | None = None,
    ) -> dict:
        """Resolve one parameter source of truth for every strategy consumer."""
        resolved = {
            item["id"]: item.get("default")
            for item in strategy.meta.get("params", [])
            if isinstance(item, dict) and item.get("id")
        }
        saved = (overrides or {}).get("params")
        if isinstance(saved, dict):
            resolved.update(saved)
        if params:
            resolved.update(params)
        return resolved

    @staticmethod
    def _result_limit(strategy: StrategyDef, overrides: dict | None) -> int | None:
        if overrides and "display_limit" in overrides:
            value = overrides.get("display_limit")
            if value in (None, 0):
                return None
            return max(0, int(value))
        value = strategy.meta.get("limit", 100)
        if value in (None, 0):
            return None
        return max(0, int(value))

    def required_history_bars(
        self,
        strategy_ids: list[str],
        *,
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
    ) -> int:
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        required = 1
        for strategy_id in strategy_ids:
            strategy = self.get(strategy_id)
            overrides = overrides_map.get(strategy_id) or {}
            scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
            required = max(required, scoring_warmup_bars(scoring))
            if strategy.execution_backend == "matrix_native":
                params = self.resolve_params(
                    strategy,
                    params_map.get(strategy_id),
                    overrides,
                )
                required = max(
                    required,
                    int(strategy.matrix_strategy.required_warmup_bars(params)) + 1,
                )
            elif strategy.execution_backend == "composite":
                # composite 预热 = 各子策略预热的 max。
                # 子策略已通过加载期校验(非嵌套叶子), 这里展开一层即可。
                if strategy.composite is None:
                    continue
                child_ids = [c.strategy_id for c in strategy.composite.children]
                required = max(
                    required,
                    self.required_history_bars(child_ids, params_map=params_map),
                )
            elif strategy.filter_history_fn:
                # lookback_days 优先取自解析后的参数(默认值/保存覆盖/本次调用),
                # 静态 LOOKBACK_DAYS 兜底。策略可能只把窗口声明为参数
                # (如 AI 生成策略), 此时 strategy.lookback_days 回退到 1,
                # 不解析参数会低估历史需求 → build_strategy_context 跳过加载 → 运行时报错。
                params = self.resolve_params(
                    strategy,
                    params_map.get(strategy_id),
                    overrides_map.get(strategy_id),
                )
                lookback = int(strategy.lookback_days)
                param_lookback = params.get("lookback_days")
                if isinstance(param_lookback, (int, float)) and param_lookback > 0:
                    lookback = max(lookback, int(param_lookback))
                required = max(required, lookback)
        return required

    def prepare_realtime_matrix(
        self,
        context: StrategyDataContext,
        strategy_ids: list[str],
        *,
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
    ):
        """Build once, then update only the latest live bar for matrix strategies."""
        from app.backtest.matrix import RealtimeMarketDataMatrix

        current = context.current
        if current is None:
            raise ValueError("realtime matrix context requires current data")
        if current.is_empty() or not strategy_ids:
            return None
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        field_columns: set[str] = set()
        max_warmup = 1
        matrix_ids: list[str] = []
        for strategy_id in strategy_ids:
            strategy = self.get(strategy_id)
            self.validate_context(strategy, context)
            if strategy.execution_backend != "matrix_native":
                continue
            params = self.resolve_params(
                strategy,
                params_map.get(strategy_id),
                overrides_map.get(strategy_id),
            )
            matrix_ids.append(strategy_id)
            max_warmup = max(
                max_warmup,
                int(strategy.matrix_strategy.required_warmup_bars(params)) + 1,
                scoring_warmup_bars(
                    effective_scoring(
                        strategy.meta.get("scoring"),
                        overrides_map.get(strategy_id),
                    )
                ),
            )
            field_columns.update(
                self._matrix_field_columns(
                    strategy,
                    overrides_map.get(strategy_id),
                    params,
                    context.asset_type,
                )
            )
        if not matrix_ids:
            return None

        timestamp_col = "datetime" if "datetime" in current.columns else "date"
        if timestamp_col not in current.columns:
            raise ValueError("realtime matrix current data requires date or datetime")
        latest_value = current[timestamp_col].max()
        as_of = latest_value.date() if hasattr(latest_value, "date") else latest_value
        if not isinstance(as_of, date):
            raise ValueError("realtime matrix timestamp cannot be converted to date")
        symbols = tuple(current["symbol"].cast(pl.Utf8).unique().sort().to_list())
        fingerprint = (
            tuple(sorted(field_columns)),
            max_warmup,
            symbols,
        )

        with self._realtime_matrix_lock:
            cache_key = context.cache_key or f"{context.asset_type}:{context.timeframe}"
            entry = self._realtime_matrices.get(cache_key)
            if entry is not None and entry.fingerprint == fingerprint:
                try:
                    entry.buffer.update(current)
                    return entry.buffer.snapshot()
                except ValueError as exc:
                    logger.info("realtime matrix %s invalidated: %s", cache_key, exc)

            history = context.history
            if history is None:
                raise ValueError("matrix strategy realtime context requires history data")
            if history is None or history.is_empty():
                raise ValueError("matrix strategy realtime history is empty")
            if timestamp_col in history.columns:
                history = history.filter(pl.col(timestamp_col) != latest_value)
            elif "date" in history.columns:
                history = history.filter(pl.col("date") != as_of)
            panel = pl.concat([history, current], how="diagonal_relaxed")
            previous_builds = entry.buffer.build_count if entry is not None else 0
            buffer = RealtimeMarketDataMatrix(
                panel,
                field_columns=field_columns,
                build_count=previous_builds + 1,
            )
            self._realtime_matrices[cache_key] = _RealtimeMatrixEntry(
                fingerprint=fingerprint,
                buffer=buffer,
            )
            return buffer.snapshot()

    def realtime_matrix_stats(self, cache_key: str) -> dict[str, int]:
        with self._realtime_matrix_lock:
            entry = self._realtime_matrices.get(cache_key)
            if entry is None:
                return {"generation": 0, "build_count": 0, "update_count": 0}
            return {
                "generation": int(entry.buffer.generation),
                "build_count": int(entry.buffer.build_count),
                "update_count": int(entry.buffer.update_count),
            }

    # ================================================================
    # 执行
    # ================================================================

    def run(
        self,
        strategy_id: str,
        context: StrategyDataContext,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
    ) -> StrategyResult:
        """执行策略: 基础过滤 → 策略过滤 → 评分排序

        Args:
            strategy_id:        策略 ID
            context:            调用级行情、资产和周期上下文
            pool:               限定股票池
            params:             本次执行显式传入的策略参数
            overrides:          用户覆盖配置 (params/basic_filter/scoring/stop_loss 等)
        """
        t0 = time.perf_counter()

        s = self.get(strategy_id)
        self.validate_context(s, context)
        self._assert_hk_context_generation(context)
        as_of = context.as_of
        overrides = overrides or {}
        params = self.resolve_params(s, params, overrides)
        entry_signals = self._effective_signals(overrides, "entry_signals", s.entry_signals)
        exit_signals = self._effective_signals(overrides, "exit_signals", s.exit_signals)
        scoring = effective_scoring(s.meta.get("scoring"), overrides)
        basic_filter = market_basic_filter(
            s.basic_filter, context.asset_type,
            overrides=overrides.get("basic_filter"),
            explicit_keys=s.basic_filter_explicit_keys,
        )
        self._validate_historical_inputs(s, context, basic_filter, scoring, overrides, params)
        if context.asset_type == "hk":
            from app.backtest.fundamentals import HK_FINANCIAL_NAMES

            financial_names = (basic_filter_dependencies(basic_filter) | scoring_dependencies(scoring)
                               | strategy_feature_dependencies(s, params)) & HK_FINANCIAL_NAMES
            if s.meta.get("order_by") in HK_FINANCIAL_NAMES:
                financial_names.add(s.meta["order_by"])
            context = self._with_hk_financials(context, financial_names)
        concept_fingerprint = self.concept_config_fingerprint(strategy_id, params, overrides)
        if concept_fingerprint:
            if context.is_historical:
                raise ValueError(HISTORICAL_REASON)
            snapshot = context.concept_snapshot or load_concept_mapping(self._data_dir, concept_market(context.asset_type))
            context = self._with_concept_heat(context, snapshot)
        concept_metadata = {
            **context.concept_heat_metadata, "config_fingerprint": concept_fingerprint,
        } if concept_fingerprint else {}
        industry_version = self._industry_version(s, context, scoring, overrides)
        context = self._with_industry_heat(context, scoring)

        if s.execution_backend == "matrix_native":
            result = self._run_matrix_strategy(
                strategy_id,
                s,
                as_of,
                pool=pool,
                params=params,
                overrides=overrides,
                context=context,
                started_at=t0,
            )
            result.industry_mapping_version = industry_version
            result.concept_heat_metadata = concept_metadata
            self._assert_hk_context_generation(context)
            return result

        if s.execution_backend == "composite":
            result = self._run_composite_strategy(
                strategy_id,
                s,
                context,
                pool=pool,
                params=params,
                overrides=overrides,
                started_at=t0,
            )
            if concept_metadata:
                result.concept_heat_metadata = {**concept_metadata, **result.concept_heat_metadata}
                result.concept_heat_metadata["config_fingerprint"] = concept_fingerprint
            self._assert_hk_context_generation(context)
            return result

        scoring_directions = effective_scoring_directions(overrides)
        current, history = self._materialize_scoring_frames(
            context.current,
            context.history,
            scoring,
        )

        signal_df = current if current is not None else history
        if signal_df is None:
            signal_df = pl.DataFrame()
        if not signal_df.is_empty() and "date" in signal_df.columns:
            signal_df = signal_df.filter(pl.col("date") == as_of)
        if pool and not signal_df.is_empty():
            signal_df = signal_df.filter(pl.col("symbol").is_in(pool))
        exit_signal_hits = self._collect_signal_hits(signal_df, exit_signals)

        # 普通策略只读目标日期；历史策略读取调用方注入的历史窗口。
        if s.filter_history_fn:
            if history is None:
                raise ValueError(f"strategy {strategy_id} requires history data")
            df = history
            if df.is_empty():
                return StrategyResult(
                    as_of=as_of,
                    strategy_id=strategy_id,
                    exit_signal_hits=exit_signal_hits,
                    concept_heat_metadata=concept_metadata,
                )
            # 自定义信号前置校验: REQUIRED_FEATURES 引用的 csg_ 列未注入时,
            # 给出明确指引, 而不是让策略代码抛 polars 缺列错 (500)。
            # 盘中单日路径不在此校验 (该路径对带偏移信号本就优雅降级)。
            missing_csg = [
                name for name in s.required_features
                if name.startswith("csg_") and name not in df.columns
            ]
            if missing_csg:
                raise ValueError(
                    "策略引用了未定义的自定义信号: "
                    + ", ".join(sorted(missing_csg))
                    + " — 请先在「自定义信号」管理中创建对应信号后再运行"
                )
            df = s.filter_history_fn(df, params)
            if "date" in df.columns:
                df = df.filter(pl.col("date") == as_of)
        else:
            if current is None:
                raise ValueError(f"strategy {strategy_id} requires current data")
            df = current

        if df.is_empty():
            return StrategyResult(
                as_of=as_of,
                strategy_id=strategy_id,
                exit_signal_hits=exit_signal_hits,
                concept_heat_metadata=concept_metadata,
            )

        # 基础过滤: 策略默认 basic_filter 兜底, 用户 override 优先覆盖。
        # 这样策略文件里写的 exclude_st/price_min 等默认值即使前端没保存也能生效。
        bf = basic_filter
        warnings = self._missing_input_warnings(df, bf, {})

        # Stage 1: 基础过滤（enabled 默认开启; 显式 enabled=false 才跳过）
        if bf and bf.get("enabled", True):
            df = self._apply_basic_filter(df, bf)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # Stage 2: 策略过滤
        if s.filter_fn:
            expr = s.filter_fn(df, params)
            df = df.filter(expr)

        # Stage 3: 热度已经基于完整输入截面计算,候选过滤不能改变其定义。
        warnings.extend(self._missing_input_warnings(df, {}, scoring))
        df = self._apply_scoring(df, scoring, scoring_directions)
        if any(weight for weight in scoring.values()) and "score" in df.columns:
            df = df.filter(pl.col("score").is_finite())
        order_value = None
        order_by = s.meta.get("order_by")
        if not df.is_empty() and "score" not in df.columns and order_by and order_by != "score":
            df = materialize_scoring_columns(df, [order_by])
            order_value = scoring_value_expr(df.columns, order_by)
            if order_value is None:
                raise ValueError(f"策略排序不可计算,缺少字段或依赖: {order_by}")
            order_value = order_value.cast(pl.Float64, strict=False)
            if not df.select(order_value.is_finite().any()).item():
                raise ValueError(f"策略排序不可计算,字段没有有效数据: {order_by}")
            warnings.extend(self._missing_input_warnings(df, {}, {order_by: 1}))
            df = df.filter(order_value.is_finite())
        entry_signal_hits = self._collect_signal_hits(df, entry_signals)
        if not entry_signals and (s.filter_history_fn or s.filter_fn):
            entry_signal_hits = [
                {"symbol": str(symbol), "signals": []}
                for symbol in df["symbol"].cast(pl.Utf8).unique().to_list()
            ]

        # 排序 + 限制
        limit = self._result_limit(s, overrides)
        order_desc = s.meta.get("descending", True)
        if "score" in df.columns:
            df = df.sort(["score", "symbol"], descending=[order_desc, False], nulls_last=True)
        elif order_value is not None:
            df = df.sort([order_value, "symbol"], descending=[order_desc, False], nulls_last=True)

        # 组合层约束 (借鉴 AlphaSift portfolio_profile): 排序后、截断前,
        # 对截面施加同行业最多 N 只 + 可选集中度惩罚。仅显式配置时生效,
        # 默认 (无 portfolio 配置) 行为与历史完全一致。
        rows = df.to_dicts()
        portfolio_cfg = overrides.get("portfolio", s.meta.get("portfolio"))
        rows = self._constrain_candidates(rows, portfolio_cfg, context.asset_type, warnings)
        if limit is not None:
            rows = rows[:limit]

        # 输出 (rows 已在组合约束段物化为 dict 列表)
        rows = _sanitize(rows)
        elapsed = (time.perf_counter() - t0) * 1000

        scores: dict[str, float] = {}
        for r in rows:
            score = r.get("score")
            if isinstance(score, (int, float)):
                scores[r["symbol"]] = float(score)

        self._assert_hk_context_generation(context)
        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
            scores=scores,
            entry_signal_hits=entry_signal_hits,
            exit_signal_hits=exit_signal_hits,
            warnings=warnings,
            industry_mapping_version=industry_version,
            concept_heat_metadata=concept_metadata,
        )

    def concept_config_fingerprint(
        self, strategy_id: str, params: dict | None = None, overrides: dict | None = None,
    ) -> str | None:
        """Identify effective configuration, including dependent composite children.

        This does not read concept data. Cache readers use it to reject delayed
        results from runs started before a configuration save.
        """
        strategy = self.get(strategy_id)
        overrides = overrides or {}
        params = self.resolve_params(strategy, params, overrides)
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        required = concept_heat_required(scoring, strategy_feature_dependencies(strategy, params))
        children_config: list[dict] = []
        if strategy.composite is not None:
            for child in self._concept_children(strategy, overrides):
                child_override = self._concept_child_override(child.strategy_id, overrides)
                fingerprint = self.concept_config_fingerprint(child.strategy_id, overrides=child_override)
                required = required or fingerprint is not None
                children_config.append({
                    "id": child.strategy_id, "weight": child.weight,
                    "meta": self.get(child.strategy_id).meta, "overrides": child_override,
                    "concept_fingerprint": fingerprint,
                })
        if not required:
            return None
        payload = {"id": strategy_id, "meta": strategy.meta, "params": params,
                   "scoring": scoring, "overrides": overrides, "children": children_config}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]

    @staticmethod
    def _concept_children(strategy: StrategyDef, overrides: dict) -> tuple[CompositeChild, ...]:
        override_children = overrides.get("children")
        if isinstance(override_children, list) and override_children:
            return _parse_composite_children(override_children).children
        return strategy.composite.children if strategy.composite is not None else ()

    def _concept_child_override(self, strategy_id: str, overrides: dict) -> dict:
        try:
            loaded = self._override_loader(strategy_id) if self._override_loader is not None else {}
        except Exception as exc:
            # Match the existing composite fallback for unavailable saved settings.
            logger.debug("composite override lookup failed: %s", exc)
            loaded = {}
        child_override = dict(loaded) if isinstance(loaded, dict) else {}
        if overrides.get("basic_filter"):
            child_override["basic_filter"] = overrides["basic_filter"]
        return child_override

    def _with_concept_heat(
        self, context: StrategyDataContext, snapshot: ConceptMappingSnapshot,
    ) -> StrategyDataContext:
        """Attach the full current slice; historical values remain unknown."""
        from app.markets import get_profile

        market_name = concept_market(context.asset_type)
        today = get_profile(market_name if market_name != "etf" else "cn").today()
        current = context.current
        if current is None and context.history is not None:
            panel = context.history
            if "datetime" in panel.columns:
                current = panel.filter(pl.col("datetime").cast(pl.Date) == context.as_of)
                if not current.is_empty():
                    current = current.filter(pl.col("datetime") == current["datetime"].max())
            elif "date" in panel.columns:
                current = panel.filter(pl.col("date") == context.as_of)
        quote_date, mixed_dates = concept_quote_date(current)
        metadata = concept_heat_availability(
            snapshot, market=market_name, as_of=context.as_of, quote_date=quote_date,
            current_market_date=today, historical=context.is_historical,
        )
        if mixed_dates and metadata["reason_code"] == "missing_quote_date":
            metadata.update(reason="行情包含多个交易日期,概念热度需要同一日期的完整截面", reason_code="mixed_quote_dates")
        if metadata["status"] == "unavailable":
            raise ValueError(metadata["reason"])
        if current is None or "change_pct" not in current.columns:
            raise ValueError("概念热度不可计算:缺少当日涨幅 change_pct")
        if "symbol" not in current.columns:
            raise ValueError("概念热度不可计算:缺少完整标的代码")
        prepared = context.concept_materialized_frames
        reuse_frames = (
            context.concept_snapshot is snapshot and prepared is not None
            and prepared[0] is current and prepared[1] is context.history
            and context.concept_heat_metadata.get("market") == market_name
            and context.concept_heat_metadata.get("quote_date") == str(quote_date)
            and context.concept_heat_metadata.get("current_market_date") == str(today)
        )
        if reuse_frames:
            metadata = context.concept_heat_metadata
            if prepared[2] is context.market:
                return context
        else:
            counts: dict[str, int] = {}
            current = attach_concept_heat(current, snapshot, market=market_name, diagnostics=counts)
            valid_count = counts.get("computable_symbols", 0)
            if not valid_count:
                raise ValueError("概念热度不可计算:没有至少 3 个独立有效成员的概念")
            metadata.update(counts, status="partial" if valid_count < counts["input_symbols"] else "available")
        history = context.history
        if not reuse_frames and history is not None and not history.is_empty():
            history = history.drop("concept_heat", strict=False)
            if ("datetime" in current.columns) != ("datetime" in history.columns):
                raise ValueError("概念热度不可计算:当前截面与历史窗口的行情时间精度不一致")
            keys = [name for name in ("symbol", "date", "datetime") if name in current.columns and name in history.columns]
            if not ({"date", "datetime"} & set(keys)):
                raise ValueError("概念热度不可计算:历史窗口缺少可对齐的行情时点")
            values = current.select([*keys, "concept_heat"]).unique(subset=keys, keep="last")
            history = history.join(values, on=keys, how="left", maintain_order="left")
        matrix = context.market
        if matrix is not None:
            heat = np.full(matrix.shape, np.nan, dtype=np.float32)
            time_key = "datetime" if "datetime" in current.columns else "date"
            values = {
                (str(row[time_key])[:19].replace("T", " "), str(row["symbol"]).strip().upper()): row["concept_heat"]
                for row in current.select("symbol", time_key, "concept_heat").iter_rows(named=True)
            }
            target_ids = [i for i, label in enumerate(matrix.timestamp_labels) if label[:10] == str(quote_date)]
            if not target_ids:
                raise ValueError("概念热度不可计算:矩阵缺少当前行情日期")
            if time_key == "date" and any(len(matrix.timestamp_labels[i]) > 10 for i in target_ids):
                raise ValueError("概念热度不可计算:分钟矩阵需要对应的行情时间")
            for time_id in target_ids:
                label = matrix.timestamp_labels[time_id][:19].replace("T", " ")
                for asset_id, symbol in enumerate(matrix.symbols):
                    value = values.get((label, symbol.strip().upper()))
                    if value is not None:
                        heat[time_id, asset_id] = float(value)
            if not np.isfinite(heat[target_ids[-1]]).any():
                raise ValueError("概念热度不可计算:矩阵目标时点与行情时间不一致")
            matrix = replace(matrix, fields={**matrix.fields, "concept_heat": heat})
        return replace(context, current=current, history=history, market=matrix,
                       concept_snapshot=snapshot, concept_heat_metadata=metadata,
                       concept_materialized_frames=(current, history, matrix))

    def _industry_version(
        self, strategy: StrategyDef, context: StrategyDataContext,
        scoring: Mapping[str, Any], overrides: dict,
    ) -> str | None:
        from app.strategy.industry_heat import scoring_uses_industry_heat
        from app.strategy.portfolio_constraints import (
            industry_mapping_version,
            normalize_portfolio_config,
        )

        portfolio = overrides.get("portfolio", strategy.meta.get("portfolio"))
        if not scoring_uses_industry_heat(scoring) and not normalize_portfolio_config(portfolio):
            return None
        market = context.asset_type if context.asset_type in ("hk", "us") else "cn"
        return industry_mapping_version(self._data_dir, market)

    def _constrain_candidates(
        self, rows: list[dict], portfolio: dict | None, asset_type: str, warnings: list[str],
    ) -> list[dict]:
        from app.strategy.portfolio_constraints import (
            UNKNOWN_BUCKET,
            apply_portfolio_constraints,
            industry_map_for_market,
            normalize_portfolio_config,
        )

        config = normalize_portfolio_config(portfolio)
        if not config or not rows:
            return rows
        market = asset_type if asset_type in ("hk", "us") else "cn"
        mapping = industry_map_for_market(self._data_dir, market, config["industry_level"])
        if not mapping:
            raise ValueError("候选集合行业约束不可计算,缺少行业映射")
        unknown = sum(mapping.get(str(row.get("symbol")), UNKNOWN_BUCKET) == UNKNOWN_BUCKET for row in rows)
        if unknown:
            warnings.append(f"{unknown} 个标的行业未知,已保留,未应用候选集合行业约束")
        return apply_portfolio_constraints(rows, mapping, config)

    @staticmethod
    def _validate_historical_inputs(
        strategy: StrategyDef,
        context: StrategyDataContext,
        basic_filter: dict,
        scoring: Mapping[str, Any],
        overrides: dict,
        params: dict | None = None,
    ) -> None:
        """Current valuation/sector snapshots cannot describe past screening dates."""
        if not context.is_historical:
            return
        from app.strategy.portfolio_constraints import normalize_portfolio_config

        features = basic_filter_dependencies(basic_filter) | scoring_dependencies(scoring)
        features.update(strategy_feature_dependencies(strategy, StrategyEngine.resolve_params(strategy, params, overrides)))
        if "concept_heat" in features:
            raise ValueError(HISTORICAL_REASON)
        unavailable = features & ({"industry_heat"} if context.asset_type == "hk" else {"pe_ttm", "pb", "industry_heat"})
        portfolio = overrides.get("portfolio", strategy.meta.get("portfolio"))
        if normalize_portfolio_config(portfolio):
            unavailable.add("portfolio")
        if unavailable:
            raise ValueError(
                "历史筛选不可计算,缺少目标日期可追溯的估值或行业数据: "
                + ", ".join(sorted(unavailable))
            )

    def _assert_hk_context_generation(self, context: StrategyDataContext) -> None:
        if context.asset_type != "hk" or context.data_generation is None or self._data_dir is None:
            return
        from app.enriched_generation import EnrichedGenerationUnavailableError
        from app.services.market_data_status import market_data_generation

        if market_data_generation(self._data_dir, "HK") != context.data_generation:
            raise EnrichedGenerationUnavailableError("港股筛选上下文的数据版本已过期,请重新加载")

    def _with_hk_financials(self, context: StrategyDataContext, names: set[str]) -> StrategyDataContext:
        """Materialize requested historical ratios for every strategy backend."""
        if not names:
            return context
        from app.backtest.fundamentals import (
            attach_hk_financial_fields,
            build_fundamental_matrices,
            load_fundamental_snapshot,
            require_hk_financial_coverage,
        )

        snapshot = load_fundamental_snapshot(self._data_dir, market="HK", names=names)
        current = attach_hk_financial_fields(context.current, snapshot, names) if context.current is not None else None
        history = attach_hk_financial_fields(context.history, snapshot, names) if context.history is not None else None
        target = current if current is not None else history
        if target is not None and "date" in target.columns:
            target = target.filter(pl.col("date") == context.as_of)
        if target is not None:
            require_hk_financial_coverage(target, names, context=str(context.as_of))
        market = context.market
        if market is not None:
            extra = build_fundamental_matrices(
                market, snapshot, names,
                price_metadata=context.history if context.history is not None else context.current,
            )
            target_times = [index for index, label in enumerate(market.timestamp_labels) if label[:10] == str(context.as_of)]
            if target_times:
                require_hk_financial_coverage(
                    pl.DataFrame({name: values[target_times].reshape(-1) for name, values in extra.items()}),
                    names, context=str(context.as_of),
                )
            market = replace(market, fields={**dict(market.fields), **extra})
        return replace(context, current=current, history=history, market=market)

    def _with_industry_heat(
        self, context: StrategyDataContext, scoring: Mapping[str, Any],
    ) -> StrategyDataContext:
        """Attach heat for the current market/date before pool or strategy filtering."""
        from app.strategy.industry_heat import attach_industry_heat, scoring_uses_industry_heat

        if not scoring_uses_industry_heat(scoring):
            return context
        current = context.current
        if current is None and context.history is not None:
            current = self._matrix_target_frame(context.history, context.as_of)
        if current is None or current.is_empty():
            return context
        if "industry_heat" in current.columns:
            current = current.drop("industry_heat")
        market_name = context.asset_type if context.asset_type in ("hk", "us") else "cn"
        current = attach_industry_heat(current, self._data_dir, market=market_name)
        history = context.history
        if history is not None and not history.is_empty():
            if "industry_heat" in history.columns:
                history = history.drop("industry_heat")
            keys = [name for name in ("symbol", "date", "datetime") if name in current.columns and name in history.columns]
            values = current.select([*keys, "industry_heat"]).unique(subset=keys, keep="last")
            history = history.join(values, on=keys, how="left")
        matrix = context.market
        if matrix is not None:
            heat = np.full(matrix.shape, np.nan, dtype=np.float32)
            by_symbol = dict(zip(current["symbol"].to_list(), current["industry_heat"].to_list(), strict=True))
            target_times = [i for i, label in enumerate(matrix.timestamp_labels) if label[:10] == str(context.as_of)]
            if target_times:
                for asset_id, symbol in enumerate(matrix.symbols):
                    value = by_symbol.get(symbol)
                    if value is not None:
                        heat[target_times[-1], asset_id] = float(value)
            matrix = replace(matrix, fields={**matrix.fields, "industry_heat": heat})
        return replace(context, current=current, history=history, market=matrix)

    @staticmethod
    def _missing_input_warnings(
        df: pl.DataFrame, basic_filter: dict, scoring: Mapping[str, Any],
    ) -> list[str]:
        if df.is_empty():
            return []
        warnings: list[str] = []
        for name in sorted(basic_filter_dependencies(basic_filter) - {"symbol", "name"}):
            if name not in df.columns:
                continue
            count = df.select((~pl.col(name).cast(pl.Float64, strict=False).is_finite().fill_null(False)).sum()).item()
            if count:
                warnings.append(f"{count} 个标的缺少有效 {name},已从基础筛选候选中排除")
        for name, weight in scoring.items():
            if not weight:
                continue
            value = scoring_value_expr(df.columns, str(name))
            if value is None:
                continue
            count = df.select((~value.cast(pl.Float64, strict=False).is_finite().fill_null(False)).sum()).item()
            if count:
                warnings.append(f"{count} 个标的缺少有效 {name},无法评分,已从策略候选中排除")
        return warnings

    @staticmethod
    def _effective_signals(overrides: dict, key: str, default: list[str]) -> list[str]:
        value = overrides.get(key)
        if isinstance(value, list):
            return [str(signal) for signal in value if signal]
        return list(default or [])

    @staticmethod
    def _collect_signal_hits(df: pl.DataFrame, signals: list[str]) -> list[dict]:
        if df.is_empty() or not signals or "symbol" not in df.columns:
            return []
        resolved = [
            signal if signal.startswith(("signal_", "csg_")) else f"signal_{signal}"
            for signal in signals
        ]
        available = [
            (signal, column)
            for signal, column in zip(signals, resolved, strict=True)
            if column in df.columns
        ]
        if not available:
            return []
        hit_df = df.filter(pl.any_horizontal(pl.col(column).fill_null(False) for _, column in available))
        return [
            {
                "symbol": str(row["symbol"]),
                "signals": [signal for signal, column in available if row.get(column)],
            }
            for row in hit_df.iter_rows(named=True)
        ]

    def run_all(
        self,
        context: StrategyDataContext,
        params_map: dict | None = None,
        overrides_map: dict | None = None,
        *,
        strategy_ids: list[str] | None = None,
    ) -> dict[str, StrategyResult]:
        """批量执行策略；当前数据、历史和矩阵均来自同一个调用上下文。"""
        if context.current is None:
            raise ValueError("strategy run_all context requires current data")
        df = context.current
        params_map = params_map or {}
        overrides_map = overrides_map or {}
        selected_ids = list(self._strategies) if strategy_ids is None else strategy_ids
        selected = [(sid, self.get(sid)) for sid in selected_ids]
        for _, strategy in selected:
            self.validate_context(strategy, context)

        concept_ids = [sid for sid, _ in selected if self.concept_config_fingerprint(
            sid, params_map.get(sid), overrides_map.get(sid),
        )]
        if concept_ids:
            if context.is_historical:
                raise ValueError(HISTORICAL_REASON)
            snapshot = context.concept_snapshot or load_concept_mapping(self._data_dir, concept_market(context.asset_type))
            context = self._with_concept_heat(context, snapshot)
            df = context.current

        history_strats = [
            (sid, strategy)
            for sid, strategy in selected
            if strategy.filter_history_fn or strategy.execution_backend == "matrix_native"
        ]
        shared_history = context.history
        if history_strats and shared_history is None:
            raise ValueError("selected strategies require history data")

        shared_matrix = context.market
        matrix_strats = [
            (sid, strategy)
            for sid, strategy in selected
            if strategy.execution_backend == "matrix_native"
        ]
        if (
            shared_matrix is None
            and matrix_strats
            and shared_history is not None
            and not shared_history.is_empty()
        ):
            from app.backtest.matrix import build_market_data_matrix

            field_columns: set[str] = set()
            for sid, strategy in matrix_strats:
                field_columns.update(
                    self._matrix_field_columns(
                        strategy,
                        overrides_map.get(sid),
                        params_map.get(sid),
                        context.asset_type,
                    )
                )
            shared_matrix = build_market_data_matrix(
                shared_history,
                field_columns=field_columns,
            )

        if concept_ids and shared_matrix is not None:
            context = self._with_concept_heat(replace(context, market=shared_matrix), context.concept_snapshot)
            shared_matrix = context.market

        results: dict[str, StrategyResult] = {}

        for sid, _ in selected:
            results[sid] = self.run(
                sid,
                replace(
                    context,
                    current=df,
                    history=shared_history,
                    market=shared_matrix,
                ),
                params=params_map.get(sid),
                overrides=overrides_map.get(sid),
            )

        return results

    @staticmethod
    def _matrix_field_columns(
        strategy: StrategyDef,
        overrides: dict | None = None,
        params: dict | None = None,
        asset_type: str = "stock",
    ) -> set[str]:
        fields = set(strategy.matrix_strategy.required_fields())
        # 参数评分字段 (如挖掘策略的因子组合) 需展开为实际数据依赖,
        # 与 backtest._resolve_matrix_native 保持同一语义, 否则虚拟因子
        # (limit_up_count_* -> consecutive_limit_ups) 在矩阵里缺字段。
        parameter_fields = getattr(
            strategy.matrix_strategy,
            "required_fields_for_params",
            None,
        )
        if callable(parameter_fields):
            fields.update(
                scoring_dependencies(
                    {str(name): 1.0 for name in parameter_fields(params or {})}
                )
            )
        basic_filter = market_basic_filter(
            strategy.basic_filter,
            asset_type,
            overrides=(overrides or {}).get("basic_filter"),
            explicit_keys=getattr(strategy, "basic_filter_explicit_keys", frozenset()),
        )
        fields.update(basic_filter_dependencies(basic_filter) - {"symbol", "name"})
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        fields.update(scoring_dependencies(scoring))
        order_by = strategy.meta.get("order_by")
        if order_by and order_by != "score":
            fields.add(str(order_by))
        return fields

    def _run_matrix_strategy(
        self,
        strategy_id: str,
        strategy: StrategyDef,
        as_of: date,
        *,
        pool: list[str] | None,
        params: dict,
        overrides: dict,
        context: StrategyDataContext,
        started_at: float,
    ) -> StrategyResult:
        from app.backtest.matrix import (
            MatrixPipelineConfig,
            MatrixStrategyPipeline,
            build_market_data_matrix,
        )

        source_panel = context.history
        market = context.market
        if market is None:
            if source_panel is None:
                raise ValueError(f"matrix strategy {strategy_id} requires history data")
            if source_panel is None or source_panel.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            market = build_market_data_matrix(
                source_panel,
                field_columns=self._matrix_field_columns(
                    strategy, overrides, params, context.asset_type,
                ),
            )

        if source_panel is None or source_panel.is_empty():
            source_panel = context.current
        if source_panel is None or source_panel.is_empty():
            return StrategyResult(as_of=as_of, strategy_id=strategy_id)

        basic_filter = market_basic_filter(
            strategy.basic_filter,
            context.asset_type,
            overrides=overrides.get("basic_filter"),
            explicit_keys=strategy.basic_filter_explicit_keys,
        )
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        asset_mask = None
        if pool:
            pool_set = set(pool)
            asset_mask = np.fromiter(
                (symbol in pool_set for symbol in market.symbols),
                dtype=bool,
                count=len(market.symbols),
            )

        target_frame = self._matrix_target_frame(source_panel, as_of)
        _validate_filter_data(target_frame, basic_filter)
        warnings = self._missing_input_warnings(target_frame, basic_filter, {})
        target_ids = [
            time_id
            for time_id, label in enumerate(market.timestamp_labels)
            if label[:10] == str(as_of)
        ]
        if not target_ids:
            return StrategyResult(as_of=as_of, strategy_id=strategy_id)
        target_time = target_ids[-1]
        entry_time_mask = np.zeros(market.shape[0], dtype=bool)
        entry_time_mask[target_time] = True
        diagnostics: dict = {}
        signals = MatrixStrategyPipeline().run(
            strategy.matrix_strategy,
            market,
            params,
            MatrixPipelineConfig(
                basic_filter=basic_filter,
                scoring=scoring,
                scoring_directions=effective_scoring_directions(overrides),
                order_by=strategy.meta.get("order_by"),
                descending=bool(strategy.meta.get("descending", True)),
                asset_mask=asset_mask,
                diagnostics=diagnostics,
                entry_time_mask=entry_time_mask,
            ),
        )
        unscored = int(diagnostics["unscored_by_date"][target_time])
        for name, counts in diagnostics.get("score_missing_by_date", {}).items():
            count = int(counts[target_time])
            if count:
                warnings.append(f"{count} 个标的缺少有效 {name},无法评分,已从策略候选中排除")
        entry_active = signals.entry[target_time]
        exit_active = signals.exit[target_time]
        if asset_mask is not None:
            entry_active = entry_active & asset_mask
            exit_active = exit_active & asset_mask
        entry_signal_hits = self._matrix_signal_hits(
            entry_active,
            signals.entry_signal_code[target_time],
            signals.entry_signal_ids,
            market.symbols,
        )
        exit_signal_hits = self._matrix_signal_hits(
            exit_active,
            signals.exit_signal_code[target_time],
            signals.exit_signal_ids,
            market.symbols,
        )
        selected_assets = np.flatnonzero(entry_active != 0)
        if selected_assets.size == 0:
            if unscored:
                raise ValueError("策略不可计算,候选标的缺少有效评分数据: " + ", ".join(scoring))
            return StrategyResult(
                as_of=as_of,
                strategy_id=strategy_id,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                entry_signal_hits=entry_signal_hits,
                exit_signal_hits=exit_signal_hits,
                warnings=warnings,
            )

        selected_symbols = [market.symbols[int(asset_id)] for asset_id in selected_assets]
        score_frame = target_frame.filter(pl.col("symbol").is_in(selected_symbols))
        warnings.extend(self._missing_input_warnings(score_frame, {}, scoring))
        if any(weight for weight in scoring.values()) and not np.isfinite(
            signals.score[target_time, selected_assets]
        ).any():
            raise ValueError("策略不可计算,候选标的缺少有效评分数据: " + ", ".join(scoring))
        row_by_symbol = {
            str(row["symbol"]): row
            for row in target_frame.iter_rows(named=True)
        }
        ranked: list[tuple[float, dict]] = []
        for asset_id in selected_assets:
            symbol = market.symbols[int(asset_id)]
            row = row_by_symbol.get(symbol)
            if row is None:
                continue
            score = float(signals.score[target_time, int(asset_id)])
            ranked.append((score, {**row, "score": score}))
        descending = bool(strategy.meta.get("descending", True))
        weighted = any(weight for weight in scoring.values())
        # order_by values are already direction-adjusted by build_matrix_score.
        rank_descending = descending if weighted or strategy.meta.get("order_by") in (None, "score") else True
        ranked.sort(key=lambda item: (
            not np.isfinite(item[0]),
            (-item[0] if rank_descending else item[0]) if np.isfinite(item[0]) else 0.0,
            str(item[1]["symbol"]),
        ))
        # 组合层约束 (与 run() 主路径同语义): 排序后、截断前
        portfolio_cfg = overrides.get("portfolio", strategy.meta.get("portfolio"))
        pending = self._constrain_candidates(
            [row for _, row in ranked], portfolio_cfg, context.asset_type, warnings,
        )
        ranked = [(float(row.get("score") or 0.0), row) for row in pending]
        limit = self._result_limit(strategy, overrides)
        selected_rows = ranked if limit is None else ranked[:limit]
        rows = _sanitize([row for _, row in selected_rows])
        scores = {
            str(row["symbol"]): float(row["score"])
            for row in rows if isinstance(row.get("score"), (int, float))
        }
        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            scores=scores,
            entry_signal_hits=entry_signal_hits,
            exit_signal_hits=exit_signal_hits,
            warnings=warnings,
        )

    def _run_composite_strategy(
        self,
        strategy_id: str,
        strategy: StrategyDef,
        context: StrategyDataContext,
        *,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
        started_at: float,
    ) -> StrategyResult:
        """叠加策略选股: 调度各子策略(共享 context)→ 合并结果。

        复用 run_all 共享 current/history/market, 避免各子策略重复加载数据。
        子策略必须已在加载期通过两阶段引用校验(存在/非嵌套/asset_types 一致)。
        """
        from app.strategy import composite as composite_mod

        assert strategy.composite is not None
        overrides = overrides or {}

        # 权重: override.children 优先(META 固化值的轻量覆盖), 否则用 META 声明。
        override_children = overrides.get("children")
        if isinstance(override_children, list) and override_children:
            spec = _parse_composite_children(override_children)
            children = spec.children
        else:
            children = strategy.composite.children

        child_ids = [c.strategy_id for c in children]
        child_weights = [c.weight for c in children]
        merge_mode = str(params.get("merge_mode") or "union")
        min_confirm = int(params.get("min_confirm") or 0)

        # 子策略 override: 先加载各自保存的用户配置(参数/评分/信号等),
        # 再叠加 composite 统一的 basic_filter(计划 §3.3, 保证候选池一致)。
        # 这样 composite 内跑子策略与单独跑子策略使用同一口径。
        shared_basic_filter = overrides.get("basic_filter")
        overrides_map: dict[str, dict] = {}
        for cid in child_ids:
            child_override: dict = {}
            if self._override_loader is not None:
                try:
                    loaded = self._override_loader(cid)
                    if isinstance(loaded, dict):
                        child_override = dict(loaded)
                except Exception:
                    pass
            if shared_basic_filter:
                child_override["basic_filter"] = shared_basic_filter
            overrides_map[cid] = child_override

        # 共享 context 跑所有子策略。run_all 内部对 matrix_native 子策略会
        # 合并 field_columns 构建超集矩阵, 一次加载。
        child_results = self.run_all(
            context,
            params_map={},
            overrides_map=overrides_map,
            strategy_ids=child_ids,
        )
        ordered_results = [child_results[cid] for cid in child_ids]

        merged = composite_mod.merge_results(
            ordered_results,
            child_weights,
            merge_mode,
            min_confirm,
            as_of=context.as_of,
            strategy_id=strategy_id,
        )

        # 构造展示行: 按 symbol 从各子结果取首个命中的行(含 name/价格等展示字段),
        # 融合 score。子策略间 schema 可能不同, 保留首个命中子的字段即可。
        row_by_symbol: dict[str, dict] = {}
        for res in ordered_results:
            for row in res.rows:
                sym = str(row.get("symbol"))
                if sym and sym not in row_by_symbol and sym in merged.scores:
                    row_by_symbol[sym] = row

        order_desc = bool(strategy.meta.get("descending", True))
        ranked_symbols = sorted(
            merged.scores.keys(),
            key=lambda s: merged.scores[s],
            reverse=order_desc,
        )
        limit = self._result_limit(strategy, overrides)
        if limit is not None:
            ranked_symbols = ranked_symbols[:limit]

        rows = _sanitize([
            {**row_by_symbol[sym], "score": merged.scores[sym]}
            for sym in ranked_symbols
            if sym in row_by_symbol
        ])
        scores = {str(row["symbol"]): float(row.get("score") or 0.0) for row in rows}
        concept_children = {result.strategy_id: result.concept_heat_metadata
                            for result in ordered_results if result.concept_heat_metadata}
        concept_metadata: dict = {}
        if concept_children:
            identities = {
                tuple(metadata.get(name) for name in ("market", "quote_date", "current_market_date", "mapping_version"))
                for metadata in concept_children.values()
            }
            if len(identities) != 1:
                raise ValueError("叠加策略概念热度来源或行情时点不一致,请重新计算")
            concept_metadata = {**next(iter(concept_children.values())), "children": concept_children}

        return StrategyResult(
            as_of=context.as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            scores=scores,
            warnings=list(dict.fromkeys(warning for result in ordered_results for warning in result.warnings)),
            concept_heat_metadata=concept_metadata,
        )

    @staticmethod
    def _matrix_signal_hits(
        active: np.ndarray,
        codes: np.ndarray,
        signal_ids: tuple[str, ...],
        symbols: tuple[str, ...],
    ) -> list[dict]:
        hits = []
        for asset_id in np.flatnonzero(active != 0):
            code = int(codes[int(asset_id)])
            signals = [signal_ids[code]] if 0 <= code < len(signal_ids) else []
            hits.append({"symbol": symbols[int(asset_id)], "signals": signals})
        return hits

    @staticmethod
    def _matrix_target_frame(panel: pl.DataFrame, as_of: date) -> pl.DataFrame:
        if "datetime" in panel.columns:
            target = panel.filter(pl.col("datetime").cast(pl.Date) == as_of)
            if target.is_empty():
                return target
            latest = target["datetime"].max()
            target = target.filter(pl.col("datetime") == latest)
        elif "date" in panel.columns:
            target = panel.filter(pl.col("date") == as_of)
        else:
            return panel.head(0)
        return target.unique(subset=["symbol"], keep="last")

    # ================================================================
    # 内部: 基础过滤
    # ================================================================

    @staticmethod
    def _basic_filter_expr(df: pl.DataFrame, bf: dict) -> pl.Expr | None:
        """构建基础过滤表达式。回测可复用为买入候选 mask，不删除行情行。"""
        if not bf or not bf.get("enabled", True):
            return None
        _validate_filter_data(df, bf)
        exprs: list[pl.Expr] = []
        for prefix, names in BASIC_FILTER_NUMERIC_FIELDS.items():
            minimum, maximum = bf.get(f"{prefix}_min"), bf.get(f"{prefix}_max")
            if minimum is None and maximum is None:
                continue
            value = pl.col(names[0]).cast(pl.Float64, strict=False)
            for name in names[1:]:
                value = value * pl.col(name).cast(pl.Float64, strict=False)
            exprs.append(value.is_finite())
            if minimum is not None:
                exprs.append(value >= float(minimum))
            if maximum is not None:
                exprs.append(value <= float(maximum))
        if bf.get("exclude_st"):
            exprs.append(~pl.col("name").str.contains("(?i)ST|\\*ST|退"))
        # 板块过滤
        boards = bf.get("boards")
        if boards and isinstance(boards, list) and len(boards) > 0:
            board_exprs: list[pl.Expr] = []
            for b in boards:
                if b == "沪主板":
                    board_exprs.append(pl.col("symbol").str.starts_with("60"))
                elif b == "深主板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("00")
                        | pl.col("symbol").str.starts_with("001")
                    )
                elif b == "创业板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("300")
                        | pl.col("symbol").str.starts_with("301")
                    )
                elif b == "科创板":
                    board_exprs.append(pl.col("symbol").str.starts_with("688"))
                elif b == "北交所":
                    board_exprs.append(pl.col("symbol").str.contains(r"\.BJ$"))
            if board_exprs:
                exprs.append(pl.any_horizontal(board_exprs))
        if exprs:
            return pl.all_horizontal(exprs)
        return None

    @staticmethod
    def _apply_basic_filter(df: pl.DataFrame, bf: dict) -> pl.DataFrame:
        """Stage 1: 基础参数过滤"""
        expr = StrategyEngine._basic_filter_expr(df, bf)
        if expr is not None:
            return df.filter(expr)
        return df

    # ================================================================
    # 内部: 评分
    # ================================================================

    @staticmethod
    def _apply_scoring(
        df: pl.DataFrame,
        weights: dict,
        directions: Mapping[str, str] | None = None,
    ) -> pl.DataFrame:
        """通用评分: min-max 归一化 → 加权求和 → 0~100 分"""
        if not weights or df.is_empty():
            return df

        missing = [str(col) for col, weight in weights.items() if weight and scoring_value_expr(df.columns, str(col)) is None]
        if missing:
            raise ValueError("策略评分不可计算,缺少字段或依赖: " + ", ".join(sorted(missing)))
        executable = [
            (str(col), value, weight)
            for col, weight in weights.items()
            if weight and (value := scoring_value_expr(df.columns, str(col))) is not None
        ]
        total_weight = sum(weight for _, _, weight in executable)
        if total_weight <= 0:
            return df

        unavailable = [
            name for name, value, _ in executable
            if not df.select(value.cast(pl.Float64, strict=False).is_finite().any()).item()
        ]
        if unavailable:
            raise ValueError("策略评分不可计算,字段没有有效数据: " + ", ".join(unavailable))
        score_parts: list[pl.Expr] = []
        for name, value, weight in executable:
            value = pl.when(value.cast(pl.Float64, strict=False).is_finite()).then(value).otherwise(None)
            w = weight / total_weight
            col_min = value.min()
            col_range = value.max() - col_min
            normalized = pl.when(value.is_not_null()).then(
                pl.when(col_range > 0).then((value - col_min) / col_range).otherwise(0.5)
            ).otherwise(None)
            if (directions or {}).get(name) == SCORING_DIRECTION_LOW:
                normalized = 1.0 - normalized
            score_parts.append(normalized * w)

        if not score_parts:
            return df

        score_expr = score_parts[0]
        for part in score_parts[1:]:
            score_expr = score_expr + part
        return df.with_columns((score_expr * 100).alias("score"))

    @staticmethod
    def _materialize_scoring_frames(
        current: pl.DataFrame | None,
        history: pl.DataFrame | None,
        scoring: Mapping[str, Any],
    ) -> tuple[pl.DataFrame | None, pl.DataFrame | None]:
        names = [str(name) for name, weight in scoring.items() if weight]
        if not names:
            return current, history
        if history is None or history.is_empty():
            return (
                materialize_scoring_columns(current, names) if current is not None else None,
                history,
            )

        scored_history = materialize_scoring_columns(history, names)
        if current is None or current.is_empty():
            return current, scored_history
        join_keys = [key for key in ("symbol", "date", "datetime") if key in current.columns and key in scored_history.columns]
        added = [name for name in names if name not in current.columns and name in scored_history.columns]
        if not join_keys or not added:
            return materialize_scoring_columns(current, names), scored_history
        values = scored_history.select([*join_keys, *added]).unique(subset=join_keys, keep="last")
        return current.join(values, on=join_keys, how="left"), scored_history


def _sanitize(rows: list[dict]) -> list[dict]:
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                r[k] = None
    return rows
