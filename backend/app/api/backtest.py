"""回测 API — 信号回测 + 因子回测 + 策略回测。"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.config import settings
from app.services.backtest import (
    BacktestConfig,
    BacktestService,
    VectorbtUnavailableError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

FACTOR_DEFAULT_DAYS = 180
STRATEGY_DEFAULT_DAYS = 365 * 3
BACKTEST_MAX_SERVER_DAYS = 186
FACTOR_MAX_SYMBOLS = 1000
BACKTEST_SERVER_GUARD_MESSAGE = (
    "当前服务器内存约 1.8GB，回测区间最多支持 6 个月；"
    "更长周期容易触发 OOM，建议在 8GB 以上内存环境或本机运行。"
)


def _get_engine(request: Request):
    """获取或创建 BacktestEngine (单例，PanelCache 跨请求生效)。"""
    from app.backtest.engine import BacktestEngine
    engine = getattr(request.app.state, "backtest_engine", None)
    if engine is None:
        engine = BacktestEngine(request.app.state.repo)
        request.app.state.backtest_engine = engine
    return engine


def _resolve_start(req: BaseModel, end: date, default_days: int) -> date:
    """未传 start 使用默认区间；显式传 null/空值表示全部历史。"""
    start = req.start
    if start is not None:
        return start
    if "start" in req.model_fields_set:
        return date(1900, 1, 1)
    return end - timedelta(days=default_days)


def _guard_server_backtest_range(start: date, end: date):
    if not settings.backtest_range_guard:
        return
    days = (end - start).days + 1
    if days > BACKTEST_MAX_SERVER_DAYS:
        raise HTTPException(status_code=400, detail=BACKTEST_SERVER_GUARD_MESSAGE)


# ================================================================
# 状态
# ================================================================

@router.get("/status")
def status():
    """前端可用此接口判断回测页是否要灰显。"""
    return {"available": True}


# ================================================================
# 信号回测 (现有接口，保持不变)
# ================================================================

class BacktestRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=1)
    start: date | None = None
    end: date | None = None
    entries: list[str] = []
    exits: list[str] = []
    stop_loss_pct: float | None = None
    max_hold_days: int | None = None
    fees_pct: float = 0.0002
    slippage_bps: float = 5
    matching: Literal["close_t", "open_t+1"] = "close_t"
    asset_type: str = "stock"


@router.post("/run")
def run(req: BacktestRequest, request: Request):
    """信号回测 — 现有接口，向后兼容。"""
    repo = request.app.state.repo
    svc = BacktestService(repo)
    end = req.end or date.today()
    start = req.start or (end - timedelta(days=365 * 3))

    cfg = BacktestConfig(
        symbols=req.symbols,
        start=start,
        end=end,
        entries=req.entries,
        exits=req.exits,
        stop_loss_pct=req.stop_loss_pct,
        max_hold_days=req.max_hold_days,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        matching=req.matching,
        asset_type=req.asset_type,
    )
    try:
        result = svc.run(cfg)
    except VectorbtUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return asdict(result)


# ================================================================
# 因子回测
# ================================================================

class FactorColumnsResponse(BaseModel):
    columns: list[dict]


@router.get("/factor/columns")
def factor_columns(
    request: Request,
    purpose: Literal["research", "scoring"] = "research",
    asset_type: Literal["stock", "etf", "hk", "us"] = "stock",
    context: Literal["current", "historical"] = "current",
    as_of: date | None = None,
):
    """Keep historical research factors separate from current scoring capability."""
    from app.backtest.factor import FACTOR_COLUMNS
    if purpose == "research":
        return {"columns": FACTOR_COLUMNS}
    from app.strategy.concept_heat import concept_scoring_column

    concept = concept_scoring_column(request.app.state.repo, asset_type=asset_type, context=context, as_of=as_of)
    return {"columns": [*FACTOR_COLUMNS, concept]}


class FactorBacktestRequest(BaseModel):
    factor_name: str = Field(..., min_length=1, max_length=64)
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    n_groups: int = Field(5, ge=2, le=10)
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@router.post("/factor/run")
def factor_run(req: FactorBacktestRequest, request: Request):
    """因子回测 — IC/IR 分析 + 分层回测。"""
    from app.backtest.factor import FACTOR_COLUMNS, FactorBacktestService, FactorConfig

    if req.factor_name not in {item["id"] for item in FACTOR_COLUMNS}:
        raise HTTPException(status_code=400, detail=f"不支持的因子: {req.factor_name}")

    engine = _get_engine(request)
    svc = FactorBacktestService(engine)

    end = req.end or date.today()
    start = _resolve_start(req, end, STRATEGY_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    symbols = req.symbols if req.symbols else None
    if symbols is not None and len(symbols) > FACTOR_MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"指定标的最多支持 {FACTOR_MAX_SYMBOLS} 只，请缩小标的范围。",
        )

    cfg = FactorConfig(
        factor_name=req.factor_name,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=req.n_groups,
        rebalance=req.rebalance,
        weight=req.weight,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        asset_type=req.asset_type,
    )
    result = svc.run(cfg)
    return asdict(result)


class FactorBatchRequest(BaseModel):
    factor_names: list[str] = Field(..., min_length=1, max_length=64)
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    n_groups: int = Field(5, ge=2, le=10)
    rebalance: Literal["daily", "weekly", "monthly"] = "monthly"
    weight: Literal["equal", "factor_weight"] = "equal"
    fees_pct: float = 0.0002
    slippage_bps: float = 5.0
    asset_type: str = "stock"


@router.post("/factor/batch")
def factor_batch(req: FactorBatchRequest, request: Request):
    """批量筛选因子, 同一批次只加载并计算一次数据面板。"""
    from app.backtest.factor import (
        FACTOR_COLUMNS,
        FactorBacktestService,
        FactorBatchConfig,
    )

    factor_names = list(dict.fromkeys(req.factor_names))
    allowed = {item["id"] for item in FACTOR_COLUMNS}
    invalid = [name for name in factor_names if name not in allowed]
    if invalid:
        raise HTTPException(status_code=400, detail=f"不支持的因子: {', '.join(invalid)}")

    end = req.end or date.today()
    start = _resolve_start(req, end, STRATEGY_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)
    symbols = req.symbols if req.symbols else None
    if symbols is not None and len(symbols) > FACTOR_MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"指定标的最多支持 {FACTOR_MAX_SYMBOLS} 只, 请缩小标的范围。",
        )

    svc = FactorBacktestService(_get_engine(request))
    result = svc.run_batch(FactorBatchConfig(
        factor_names=factor_names,
        symbols=symbols,
        start=start,
        end=end,
        n_groups=req.n_groups,
        rebalance=req.rebalance,
        weight=req.weight,
        fees_pct=req.fees_pct,
        slippage_bps=req.slippage_bps,
        asset_type=req.asset_type,
    ))
    return asdict(result)


# ================================================================
# 研究候选方案
# ================================================================

class CandidateCreateRequest(BaseModel):
    kind: Literal["factor", "strategy"]
    name: str = Field(..., min_length=1, max_length=80)
    source_id: str = Field(..., min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)
    data_as_of: date | None = None
    status: Literal["pending", "validated", "rejected"] = "pending"


class CandidateUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    status: Literal["pending", "validated", "rejected"] | None = None


def _candidate_store():
    from app.backtest.candidates import CandidateStore

    return CandidateStore(settings.data_dir)


def _raise_candidate_error(exc: Exception) -> None:
    from app.backtest.candidates import CandidateValidationError

    status_code = 400 if isinstance(exc, CandidateValidationError) else 500
    raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.get("/candidates")
def candidates_list():
    try:
        return {"items": _candidate_store().list()}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates")
def candidate_create(req: CandidateCreateRequest):
    try:
        return _candidate_store().create(
            kind=req.kind,
            name=req.name,
            source_id=req.source_id,
            config=req.config,
            metrics=req.metrics,
            data_as_of=req.data_as_of.isoformat() if req.data_as_of else None,
            status=req.status,
        )
    except Exception as exc:
        _raise_candidate_error(exc)


@router.patch("/candidates/{candidate_id}")
def candidate_update(candidate_id: str, req: CandidateUpdateRequest):
    if req.name is None and req.status is None:
        raise HTTPException(status_code=400, detail="至少提供一个需要更新的字段")
    try:
        return _candidate_store().update(candidate_id, name=req.name, status=req.status)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="候选方案不存在") from exc
    except Exception as exc:
        _raise_candidate_error(exc)


@router.delete("/candidates/{candidate_id}")
def candidate_delete(candidate_id: str):
    try:
        _candidate_store().delete(candidate_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="候选方案不存在") from exc
    except Exception as exc:
        _raise_candidate_error(exc)


# ================================================================
# 策略回测
# ================================================================

class StrategyBacktestRequest(BaseModel):
    strategy_id: str
    symbols: list[str] | None = None
    start: date | None = None
    end: date | None = None
    params: dict | None = None
    overrides: dict | None = None
    # matching 向后兼容; 显式传 entry_fill/exit_fill 时以二者为准。
    matching: Literal["close_t", "open_t+1"] = "open_t+1"
    entry_fill: Literal["close_t", "open_t+1"] | None = None
    exit_fill: Literal["close_t", "open_t+1", "signal_next_minute"] | None = None
    fees_pct: float | None = Field(None, ge=0, lt=1, allow_inf_nan=False)
    commission_pct: float | None = Field(None, ge=0, lt=1, allow_inf_nan=False)
    stamp_tax_pct: float | None = Field(None, ge=0, lt=1, allow_inf_nan=False)
    buy_stamp_tax_pct: float = Field(0.0, ge=0, lt=1, allow_inf_nan=False)
    slippage_bps: float = Field(5.0, ge=0, lt=10000, allow_inf_nan=False)
    max_positions: int = Field(10, ge=1)
    max_exposure_pct: float = Field(1.0, ge=0, le=1, allow_inf_nan=False)
    initial_capital: float = Field(1_000_000.0, gt=0, allow_inf_nan=False)
    position_sizing: Literal["equal", "score_weight"] = "equal"
    mode: Literal["position", "full"] = "position"
    holding_days: int = 5
    asset_type: Literal["stock", "etf", "hk", "us"] = "stock"
    minute_fill: bool = False
    regime_filter: dict | None = None

    @model_validator(mode="after")
    def validate_market_execution(self) -> StrategyBacktestRequest:
        from app.backtest.engine import validate_backtest_market

        if self.symbols is not None:
            self.symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in self.symbols if symbol.strip()))
        validate_backtest_market(
            self.asset_type, self.symbols, minute_fill=self.minute_fill,
            exit_fill=self.exit_fill or self.matching,
        )
        if self.asset_type in {"hk", "us"} and self.regime_filter:
            raise ValueError("港美股尚无可追溯的市场环境数据,无法启用市场环境过滤")
        if self.start and self.end and self.start > self.end:
            raise ValueError("回测起始日期不能晚于结束日期")
        if self.fees_pct is None:
            self.fees_pct = 0.0 if self.asset_type in {"hk", "us"} else 0.0002
        commission = self.commission_pct if self.commission_pct is not None else self.fees_pct
        if commission + max(self.stamp_tax_pct or 0, self.buy_stamp_tax_pct) + self.slippage_bps / 10000 >= 1:
            raise ValueError("单边费用与滑点之和必须小于成交金额")
        return self


@router.post("/strategy/run")
def strategy_run(req: StrategyBacktestRequest, request: Request):
    """策略回测 — 复用 StrategyDef 体系做全周期回测。"""
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task

    end = req.end or date.today()
    start = _resolve_start(req, end, FACTOR_DEFAULT_DAYS)
    _guard_server_backtest_range(start, end)

    cfg = StrategyBacktestConfig(
        strategy_id=req.strategy_id,
        symbols=req.symbols if req.symbols else None,
        start=start,
        end=end,
        params=req.params,
        overrides=req.overrides,
        matching=req.matching,
        entry_fill=req.entry_fill,
        exit_fill=req.exit_fill,
        fees_pct=req.fees_pct,
        commission_pct=req.commission_pct,
        stamp_tax_pct=req.stamp_tax_pct,
        buy_stamp_tax_pct=req.buy_stamp_tax_pct,
        slippage_bps=req.slippage_bps,
        max_positions=req.max_positions,
        max_exposure_pct=req.max_exposure_pct,
        initial_capital=req.initial_capital,
        position_sizing=req.position_sizing,
        mode=req.mode,
        holding_days=req.holding_days,
        asset_type=req.asset_type,
        minute_fill=req.minute_fill,
        regime_filter=req.regime_filter,
    )
    task = make_worker_task("backtest", settings.data_dir, cfg)
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    with shared_heavy_job_limiter.slot("normal"):
        return run_worker_task(task)


# ── SSE 流式回测 (实时进度 + 可取消 + 支持重连) ───────────────────

import hashlib
import time


class _BacktestJob:
    """单个回测任务的状态, 存模块级供重连使用。"""
    __slots__ = ("cancel_event", "done", "error", "finish_ts", "key", "progress", "request_key", "result")

    def __init__(self, key: str, request_key: str | None = None):
        self.key = key
        self.request_key = request_key or key
        self.cancel_event = threading.Event()
        self.progress: list[dict] = []   # 进度历史 (新连接可回放)
        self.result = None               # 完成后的结果
        self.error: str | None = None
        self.done = False
        self.finish_ts: float = 0.0


# 模块级任务表: key -> _BacktestJob
_running_jobs: dict[str, _BacktestJob] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 300  # 完成后保留 5 分钟


def _cleanup_stale_jobs():
    """清理过期任务 (完成超过 TTL 的)。全程持 _jobs_lock: 迭代+pop 与其他访问互斥。"""
    now = time.time()
    with _jobs_lock:
        stale = [k for k, j in _running_jobs.items() if j.done and now - j.finish_ts > _JOB_TTL]
        for k in stale:
            _running_jobs.pop(k, None)


def _finish_job(job: _BacktestJob, *, result=None, error: str | None = None) -> None:
    """Publish the terminal state and proactively drop the reconnect entry after TTL."""
    finished_at = time.time()
    with _jobs_lock:
        job.result = result
        job.error = error
        job.done = True
        job.finish_ts = finished_at

    def _expire() -> None:
        with _jobs_lock:
            current = _running_jobs.get(job.key)
            if current is job and current.done and current.finish_ts == finished_at:
                _running_jobs.pop(job.key, None)

    timer = threading.Timer(_JOB_TTL, _expire)
    timer.daemon = True
    timer.start()


def _make_job_key(
    strategy_id: str, symbols: str | None, start: str | None, end: str | None,
    matching: str, entry_fill: str | None, exit_fill: str | None,
    fees_pct: float | None, slippage_bps: float,
    max_positions: int, max_exposure_pct: float, initial_capital: float, position_sizing: str,
    params: str | None, overrides: str | None,
    mode: str = "position", holding_days: int = 5,
    commission_pct: float | None = None, stamp_tax_pct: float | None = None,
    asset_type: str = "stock",
    minute_fill: bool = False,
    regime_filter: str | None = None,
    buy_stamp_tax_pct: float = 0.0,
    data_generation: str | None = None,
) -> str:
    if fees_pct is None:
        fees_pct = 0.0 if asset_type in {"hk", "us"} else 0.0002
    raw = f"{strategy_id}|{symbols}|{start}|{end}|{matching}|{entry_fill}|{exit_fill}|{fees_pct}|{slippage_bps}|{max_positions}|{max_exposure_pct}|{initial_capital}|{position_sizing}|{params}|{overrides}|{mode}|{holding_days}|{commission_pct}|{stamp_tax_pct}|{asset_type}|{minute_fill}|{regime_filter}|{buy_stamp_tax_pct}"
    if data_generation is not None:
        raw += f"|generation:{data_generation}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


@router.get("/strategy/stream")
async def strategy_stream(
    request: Request,
    strategy_id: str,
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    matching: str = "open_t+1",
    entry_fill: str | None = None,
    exit_fill: str | None = None,
    fees_pct: float | None = None,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    params: str | None = None,
    overrides: str | None = None,
    mode: str = "position",
    holding_days: int = 5,
    asset_type: str = "stock",
    minute_fill: bool = False,
    regime_filter: str | None = None,
    buy_stamp_tax_pct: float = 0.0,
):
    """SSE 流式策略回测: 实时推送进度, 完成后推送结果, 支持重连 (刷新/切页后恢复)。

    - 相同参数的任务只启动一次, 多次连接订阅同一个任务
    - 断开连接不会取消任务 (除非显式调用 cancel)
    - 结果保留 5 分钟供重连

    事件类型:
      - progress: {day, total, date, equity}
      - done: {result} (完整回测结果)
      - error: {message}
    """
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task

    try:
        validated = StrategyBacktestRequest(
            strategy_id=strategy_id,
            symbols=[symbol.strip() for symbol in symbols.split(",") if symbol.strip()] if symbols else None,
            start=start or None, end=end or None, matching=matching, entry_fill=entry_fill, exit_fill=exit_fill,
            fees_pct=fees_pct, commission_pct=commission_pct, stamp_tax_pct=stamp_tax_pct,
            buy_stamp_tax_pct=buy_stamp_tax_pct, slippage_bps=slippage_bps, max_positions=max_positions,
            max_exposure_pct=max_exposure_pct, initial_capital=initial_capital, position_sizing=position_sizing,
            params=json.loads(params) if params else None, overrides=json.loads(overrides) if overrides else None,
            mode=mode, holding_days=holding_days, asset_type=asset_type, minute_fill=minute_fill,
            regime_filter=json.loads(regime_filter) if regime_filter else None,
        )
    except (ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.markets.registry import get_profile

    profile = get_profile(asset_type if asset_type in {"hk", "us"} else "CN")
    end_date = validated.end or profile.today()
    fees_pct = validated.fees_pct
    if validated.start:
        start_date = validated.start
    else:
        # 空 start = 全部历史: 用本地最早日K日期, 查不到再回退到默认窗口
        earliest = (
            date(1900, 1, 1) if asset_type in {"hk", "us"}
            else request.app.state.repo.earliest_daily_date()
        )
        start_date = earliest or (end_date - timedelta(days=FACTOR_DEFAULT_DAYS))

    # 服务端范围保护
    guard_violated = False
    if settings.backtest_range_guard:
        days = (end_date - start_date).days + 1
        if days > BACKTEST_MAX_SERVER_DAYS:
            guard_violated = True

    request_key = _make_job_key(
        strategy_id, symbols, start, end,
        matching, entry_fill, exit_fill,
        fees_pct, slippage_bps, max_positions, max_exposure_pct, initial_capital, position_sizing,
        params, overrides,
        mode, holding_days,
        commission_pct, stamp_tax_pct,
        asset_type=asset_type,
        minute_fill=minute_fill,
        regime_filter=regime_filter,
        buy_stamp_tax_pct=buy_stamp_tax_pct,
    )

    generation_loader = getattr(request.app.state.repo, "get_matrix_data_generation", None)
    try:
        generation = generation_loader(asset_type) if callable(generation_loader) else None
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=503, detail="市场数据正在更新或暂不可读取,请稍后重试") from exc
    job_key = (
        hashlib.md5(f"{request_key}|{generation}".encode()).hexdigest()[:12]
        if generation is not None else request_key
    )

    _cleanup_stale_jobs()

    # 获取或创建任务
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key, request_key=request_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():
        # 范围保护: 直接报错
        if guard_violated:
            yield f"event: error\ndata: {json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            return

        # 分钟K精确回测: Pro+ 门控 + 数据范围检查
        if minute_fill:
            capset = request.app.state.capabilities
            from app.tickflow.capabilities import Cap
            if not capset.has(Cap.KLINE_MINUTE_BATCH):
                yield f"event: error\ndata: {json.dumps({'message': '分钟K精确回测需要 Pro+ 权限 (kline.minute.batch)'}, ensure_ascii=False)}\n\n"
                return
            # 检查本地分钟K历史是否覆盖回测区间
            repo = request.app.state.repo
            earliest_minute = repo.earliest_minute_date() if hasattr(repo, "earliest_minute_date") else None
            if earliest_minute is not None and start_date < earliest_minute:
                msg = (f"本地分钟K历史最早到 {earliest_minute}, 无法覆盖回测起始日 {start_date}。"
                       f"请先用「扩展分钟K历史」功能拉取更多数据, 或缩小回测区间。")
                yield f"event: error\ndata: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
                return

        # 如果是新任务, 启动回测线程
        if is_new and not job.done:
            cfg = StrategyBacktestConfig(
                strategy_id=strategy_id,
                symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                start=start_date,
                end=end_date,
                params=json.loads(params) if params else None,
                overrides=json.loads(overrides) if overrides else None,
                matching=matching,
                entry_fill=entry_fill,
                exit_fill=exit_fill,
                fees_pct=fees_pct,
                commission_pct=commission_pct,
                stamp_tax_pct=stamp_tax_pct,
                buy_stamp_tax_pct=buy_stamp_tax_pct,
                slippage_bps=slippage_bps,
                max_positions=int(max_positions),
                max_exposure_pct=float(max_exposure_pct),
                initial_capital=float(initial_capital),
                position_sizing=position_sizing,
                mode=mode,
                holding_days=int(holding_days),
                asset_type=asset_type,
                minute_fill=minute_fill,
                regime_filter=json.loads(regime_filter) if regime_filter else None,
            )

            def _run_backtest():
                from app.services.heavy_job_limiter import (
                    HeavyJobCancelledError,
                    shared_heavy_job_limiter,
                )

                try:
                    with shared_heavy_job_limiter.slot(
                        "normal",
                        cancel_event=job.cancel_event,
                    ):
                        task = make_worker_task("backtest", settings.data_dir, cfg)
                        if asset_type == "hk" and generation is not None:
                            task["expected_data_generation"] = generation
                        result = run_worker_task(
                            task,
                            lambda d: job.progress.append(d),
                            job.cancel_event,
                        )
                    if asset_type == "hk" and callable(generation_loader) and generation_loader(asset_type) != generation:
                        raise RuntimeError("港股数据在回测任务执行期间已更新,请重新回测")
                    _finish_job(job, result=result)
                except HeavyJobCancelledError:
                    _finish_job(job, error="回测已取消")
                except Exception as e:
                    _finish_job(job, error=str(e))

            # 启动后台线程 (不阻塞事件循环)
            threading.Thread(target=_run_backtest, daemon=True).start()

        # 订阅进度: 用读指针读 job.progress 列表 (多连接互不干扰)
        cursor = 0
        tick = 0

        try:
            while True:
                # 已完成: 推送最终结果/错误并退出
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        r = job.result
                        error = r.get("error") if isinstance(r, dict) else getattr(r, "error", None)
                        if error == "cancelled":
                            yield f"event: error\ndata: {json.dumps({'message': '回测已取消'}, ensure_ascii=False)}\n\n"
                        elif error:
                            yield f"event: error\ndata: {json.dumps({'message': error}, ensure_ascii=False)}\n\n"
                        else:
                            payload = r if isinstance(r, dict) else asdict(r)
                            yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    return

                # 断开检测: 每 4 轮检查一次 (降低 GIL 抢占频率)
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break

                # 推送新进度 (从 cursor 开始读)
                prog_list = job.progress
                while cursor < len(prog_list):
                    msg = prog_list[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/strategy/cancel")
async def strategy_cancel(request: Request):
    """取消正在运行的回测任务 (前端传 query string, 后端算 job_key)。"""
    body = await request.json()
    qs = body.get("qs", "")
    # 解析 qs 得到参数
    from urllib.parse import parse_qs
    p = parse_qs(qs)
    def _get(key: str, default: str = "") -> str:
        return p.get(key, [default])[0]
    def _get_opt_float(key: str) -> float | None:
        # 可选成本参数: 缺省或空串 → None (与 stream 侧 float | None 口径一致, 保证 job_key 对齐)。
        v = _get(key)
        return float(v) if v else None
    job_key = _make_job_key(
        _get("strategy_id"),
        _get("symbols") or None,
        _get("start") or None,
        _get("end") or None,
        _get("matching", "open_t+1"),
        _get("entry_fill") or None,
        _get("exit_fill") or None,
        _get_opt_float("fees_pct"),
        float(_get("slippage_bps", "5")),
        int(_get("max_positions", "10")),
        float(_get("max_exposure_pct", "1")),
        float(_get("initial_capital", "1000000")),
        _get("position_sizing", "equal"),
        _get("params") or None,
        _get("overrides") or None,
        _get("mode", "position"),
        int(_get("holding_days", "5")),
        commission_pct=_get_opt_float("commission_pct"),
        stamp_tax_pct=_get_opt_float("stamp_tax_pct"),
        buy_stamp_tax_pct=_get_opt_float("buy_stamp_tax_pct") or 0.0,
        asset_type=_get("asset_type", "stock"),
        minute_fill=_get("minute_fill", "false").lower() in {"true", "1"},
        regime_filter=_get("regime_filter") or None,
    )
    # 持锁读任务表: 与 _cleanup_stale_jobs 的 pop、stream 的写入互斥
    with _jobs_lock:
        jobs = [job for job in _running_jobs.values() if job.request_key == job_key and not job.done]
    if jobs:
        for job in jobs:
            job.cancel_event.set()
        return {"ok": True, "cancelled_count": len(jobs)}
    return {"ok": False, "message": "任务不存在或已完成"}


# ══════════════════════════════════════════════════════════════
# 参数网格优化器 — 复用 _BacktestJob SSE 框架 (多组参数并行回测 + 排序)
# ══════════════════════════════════════════════════════════════

def _json_safe(obj):
    """递归把 nan/inf 置 None —— json.dumps(default=str) 处理不了它们, 会输出非法 JSON
    字面量 NaN/Infinity 让前端 JSON.parse 崩。优化器/WF 结果嵌套深 (逐组/逐折的
    sortino 等零波动场景可能算出 nan), 序列化前统一清洗。"""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# 透传给每组回测的 StrategyBacktestConfig 字段 (作为 backtest_kwargs)。
_OPT_BT_FIELDS = [
    "matching", "fees_pct", "commission_pct", "stamp_tax_pct", "slippage_bps",
    "max_positions", "max_exposure_pct", "initial_capital", "position_sizing",
    "mode", "holding_days",
]


def _make_opt_job_key(
    strategy_id,
    symbols,
    start,
    end,
    param_grid,
    objective,
    direction,
    bt_sig,
    params=None,
    overrides=None,
    matrix_cache_max_mb=512,
) -> str:
    raw = (
        f"OPT|{strategy_id}|{symbols}|{start}|{end}|{param_grid}|{objective}|"
        f"{direction}|{bt_sig}|{params}|{overrides}|cache={matrix_cache_max_mb}"
    )
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _opt_backtest_kwargs(
    matching, fees_pct, commission_pct, stamp_tax_pct, slippage_bps,
    max_positions, max_exposure_pct, initial_capital, position_sizing, mode, holding_days,
) -> dict:
    return {
        "matching": matching,
        "fees_pct": fees_pct,
        "commission_pct": commission_pct,
        "stamp_tax_pct": stamp_tax_pct,
        "slippage_bps": slippage_bps,
        "max_positions": int(max_positions),
        "max_exposure_pct": float(max_exposure_pct),
        "initial_capital": float(initial_capital),
        "position_sizing": position_sizing,
        "mode": mode,
        "holding_days": int(holding_days),
    }


@router.get("/optimize/stream")
async def optimize_stream(
    request: Request,
    strategy_id: str,
    param_grid: str,                 # JSON: {param_id: [values] | {min,max,step}}
    objective: str = "sortino",
    direction: str | None = None,
    max_workers: int = 4,
    matrix_cache_max_mb: int = 512,
    params: str | None = None,       # JSON: 未扫描参数固定为用户当前值 (base_params)
    overrides: str | None = None,    # JSON: 策略当前的 basic_filter/signals/风控等覆盖
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    matching: str = "open_t+1",
    fees_pct: float = 0.0002,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    mode: str = "position",
    holding_days: int = 5,
):
    """SSE 流式参数优化: 并行跑各参数组回测, 按 objective 排序。

    事件类型:
      - progress: {type: "optimizer_progress", done, total, best_score}
      - done: {result} (含 best_params / results 排名)
      - error: {message}
    """
    from app.backtest.optimizer import OptimizeConfig
    from app.backtest.worker import make_worker_task, run_worker_task

    end_date = date.fromisoformat(end) if end else date.today()
    if start:
        start_date = date.fromisoformat(start)
    else:
        earliest = request.app.state.repo.earliest_daily_date()
        start_date = earliest or (end_date - timedelta(days=FACTOR_DEFAULT_DAYS))

    guard_violated = False
    if settings.backtest_range_guard and (end_date - start_date).days + 1 > BACKTEST_MAX_SERVER_DAYS:
        guard_violated = True

    # 空串归一为 None, 与 cancel 侧 `_get("direction") or None` 口径一致, 避免 job_key 失配。
    direction = direction or None
    bt_kwargs = _opt_backtest_kwargs(
        matching, fees_pct, commission_pct, stamp_tax_pct, slippage_bps,
        max_positions, max_exposure_pct, initial_capital, position_sizing, mode, holding_days,
    )
    bt_sig = "|".join(f"{k}={bt_kwargs[k]}" for k in _OPT_BT_FIELDS)
    job_key = _make_opt_job_key(
        strategy_id,
        symbols,
        start,
        end,
        param_grid,
        objective,
        direction,
        bt_sig,
        params,
        overrides,
        matrix_cache_max_mb,
    )

    _cleanup_stale_jobs()
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():
        # 首个事件回吐 job_key, 前端存下供 cancel 直接引用 (消除两侧重算契约)。
        yield f"event: job\ndata: {json.dumps({'key': job_key}, ensure_ascii=False)}\n\n"

        if guard_violated:
            yield f"event: error\ndata: {json.dumps({'message': BACKTEST_SERVER_GUARD_MESSAGE}, ensure_ascii=False)}\n\n"
            return

        if is_new and not job.done:
            try:
                grid = json.loads(param_grid)
            except (json.JSONDecodeError, TypeError):
                grid = None
            # grid 必须是非空 dict; null/[]/"" 等合法 JSON 但结构错误也在此拦下,
            # 否则会跳过线程启动却不置 done -> event_generator 永久空转、job 挂死。
            if not isinstance(grid, dict) or not grid:
                _finish_job(job, error="param_grid 必须是非空的参数网格对象")
                grid = None

            if grid is not None:
                # 未扫描参数固定为用户当前值 (base_params); overrides 让策略的 basic_filter/
                # 信号/风控按用户当前配置参与, 保证优化的就是用户实际回测的策略。
                try:
                    base_params = json.loads(params) if params else {}
                except (json.JSONDecodeError, TypeError):
                    # 静默降级会让"用户配置丢失"变成无声 bug: 至少 warn 供诊断 (前端应传合法 JSON)。
                    logger.warning("optimize: params JSON 解析失败, 降级为空 params: %r", params)
                    base_params = {}
                try:
                    ov = json.loads(overrides) if overrides else None
                except (json.JSONDecodeError, TypeError):
                    logger.warning("optimize: overrides JSON 解析失败, 降级为 None: %r", overrides)
                    ov = None
                ocfg = OptimizeConfig(
                    strategy_id=strategy_id,
                    symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                    start=start_date,
                    end=end_date,
                    param_grid=grid,
                    objective=objective,
                    direction=direction,
                    max_workers=int(max_workers),
                    matrix_cache_max_mb=int(matrix_cache_max_mb),
                    base_params=base_params if isinstance(base_params, dict) else {},
                    overrides=ov if isinstance(ov, dict) else None,
                    backtest_kwargs=bt_kwargs,
                )

                def _run_opt():
                    from app.services.heavy_job_limiter import (
                        HeavyJobCancelledError,
                        shared_heavy_job_limiter,
                    )

                    try:
                        with shared_heavy_job_limiter.slot(
                            "normal",
                            cancel_event=job.cancel_event,
                        ):
                            task = make_worker_task("optimize", settings.data_dir, ocfg)
                            result = run_worker_task(
                                task,
                                lambda d: job.progress.append(d),
                                job.cancel_event,
                            )
                        _finish_job(job, result=result)
                    except HeavyJobCancelledError:
                        _finish_job(job, error="优化已取消")
                    except Exception as e:
                        _finish_job(job, error=str(e))

                threading.Thread(target=_run_opt, daemon=True).start()

        cursor = 0
        tick = 0
        try:
            while True:
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.cancel_event.is_set():
                        # 取消时优化器把每组记为 cancelled 并正常返回, 需在此分流为取消提示而非"完成"。
                        yield f"event: error\ndata: {json.dumps({'message': '优化已取消'}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        yield f"event: done\ndata: {json.dumps(_json_safe(job.result), ensure_ascii=False, default=str)}\n\n"
                    return
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break
                while cursor < len(job.progress):
                    msg = job.progress[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/optimize/cancel")
async def optimize_cancel(request: Request):
    """取消优化任务 — 前端传 stream 首事件回吐的 job_key, 后端直接查表。

    不再让 cancel 侧重算 job_key: 两侧重算必须逐字段一致的脆弱契约(PR3 C1 / direction
    空串失配都源于此)在此彻底消除。stream 首个 SSE 事件把后端算出的 key 回吐给前端,
    cancel 原样传回即可。
    """
    body = await request.json()
    job_key = body.get("job_key", "")
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}


# ══════════════════════════════════════════════════════════════
# Walk-forward 优化 — 每折训练区间优化 + 测试区间 OOS 验证 (复用优化器 + job_key 回吐)
# ══════════════════════════════════════════════════════════════

def _make_wf_job_key(
    strategy_id,
    symbols,
    start,
    end,
    param_grid,
    objective,
    direction,
    windows,
    bt_sig,
    params=None,
    overrides=None,
    matrix_cache_max_mb=512,
) -> str:
    raw = (
        f"WF|{strategy_id}|{symbols}|{start}|{end}|{param_grid}|{objective}|"
        f"{direction}|{windows}|{bt_sig}|{params}|{overrides}|cache={matrix_cache_max_mb}"
    )
    return hashlib.md5(raw.encode()).hexdigest()[:12]


@router.get("/walkforward/stream")
async def walkforward_stream(
    request: Request,
    strategy_id: str,
    param_grid: str,
    objective: str = "sortino",
    direction: str | None = None,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int = 63,
    max_workers: int = 4,
    matrix_cache_max_mb: int = 512,
    params: str | None = None,       # JSON: 未扫描参数固定为用户当前值 (base_params)
    overrides: str | None = None,    # JSON: 策略当前的 basic_filter/signals/风控等覆盖
    symbols: str | None = None,
    start: str | None = None,
    end: str | None = None,
    matching: str = "open_t+1",
    fees_pct: float = 0.0002,
    commission_pct: float | None = None,
    stamp_tax_pct: float | None = None,
    slippage_bps: float = 5.0,
    max_positions: int = 10,
    max_exposure_pct: float = 1.0,
    initial_capital: float = 1_000_000.0,
    position_sizing: str = "equal",
    mode: str = "position",
    holding_days: int = 5,
):
    """SSE 流式 walk-forward: 每折训练区间网格优化 -> 测试区间 OOS 回测。

    事件: job {key} / progress {type:walkforward_progress,done,total,fold} / done {result} / error {message}
    """
    from app.backtest.walkforward import WalkForwardConfig
    from app.backtest.worker import make_worker_task, run_worker_task

    direction = direction or None

    end_date = date.fromisoformat(end) if end else date.today()
    if start:
        start_date = date.fromisoformat(start)
    else:
        earliest = request.app.state.repo.earliest_daily_date()
        start_date = earliest or (end_date - timedelta(days=STRATEGY_DEFAULT_DAYS))

    bt_kwargs = _opt_backtest_kwargs(
        matching, fees_pct, commission_pct, stamp_tax_pct, slippage_bps,
        max_positions, max_exposure_pct, initial_capital, position_sizing, mode, holding_days,
    )
    bt_sig = "|".join(f"{k}={bt_kwargs[k]}" for k in _OPT_BT_FIELDS)
    windows = f"{train_days}/{test_days}/{step_days}"
    job_key = _make_wf_job_key(
        strategy_id,
        symbols,
        start,
        end,
        param_grid,
        objective,
        direction,
        windows,
        bt_sig,
        params,
        overrides,
        matrix_cache_max_mb,
    )

    # guard 作用于单折窗口 (每折训练/测试各是一次回测), 而非总区间 —— WF 总区间可长达数年,
    # 按总区间拦会误杀; 真正的 OOM 风险在单折窗口过大。
    wf_guard_violated = (
        settings.backtest_range_guard
        and max(int(train_days), int(test_days)) > BACKTEST_MAX_SERVER_DAYS
    )

    _cleanup_stale_jobs()
    with _jobs_lock:
        job = _running_jobs.get(job_key)
        if job is None:
            job = _BacktestJob(job_key)
            _running_jobs[job_key] = job
            is_new = True
        else:
            is_new = False

    async def event_generator():
        yield f"event: job\ndata: {json.dumps({'key': job_key}, ensure_ascii=False)}\n\n"

        if wf_guard_violated:
            msg = f"单折窗口最多 {BACKTEST_MAX_SERVER_DAYS} 天 (当前 train/test 更大), 请减小训练/测试窗口或在更大内存环境运行。"
            yield f"event: error\ndata: {json.dumps({'message': msg}, ensure_ascii=False)}\n\n"
            return

        if is_new and not job.done:
            try:
                grid = json.loads(param_grid)
            except (json.JSONDecodeError, TypeError):
                grid = None
            if not isinstance(grid, dict) or not grid:
                job.error = "param_grid 必须是非空的参数网格对象"
                job.done = True
                job.finish_ts = time.time()
                grid = None

            if grid is not None:
                try:
                    base_params = json.loads(params) if params else {}
                except (json.JSONDecodeError, TypeError):
                    # 静默降级会让"用户配置丢失"变成无声 bug: 至少 warn 供诊断 (前端应传合法 JSON)。
                    logger.warning("walkforward: params JSON 解析失败, 降级为空 params: %r", params)
                    base_params = {}
                try:
                    ov = json.loads(overrides) if overrides else None
                except (json.JSONDecodeError, TypeError):
                    logger.warning("walkforward: overrides JSON 解析失败, 降级为 None: %r", overrides)
                    ov = None
                wf_cfg = WalkForwardConfig(
                    strategy_id=strategy_id,
                    symbols=[s.strip() for s in symbols.split(",") if s.strip()] if symbols else None,
                    start=start_date,
                    end=end_date,
                    param_grid=grid,
                    objective=objective,
                    direction=direction,
                    train_days=int(train_days),
                    test_days=int(test_days),
                    step_days=int(step_days),
                    max_workers=int(max_workers),
                    base_params=base_params if isinstance(base_params, dict) else {},
                    overrides=ov if isinstance(ov, dict) else None,
                    backtest_kwargs=bt_kwargs,
                    matrix_cache_max_mb=int(matrix_cache_max_mb),
                )

                def _run_wf():
                    from app.services.heavy_job_limiter import (
                        HeavyJobCancelledError,
                        shared_heavy_job_limiter,
                    )

                    try:
                        with shared_heavy_job_limiter.slot(
                            "normal",
                            cancel_event=job.cancel_event,
                        ):
                            task = make_worker_task("walkforward", settings.data_dir, wf_cfg)
                            result = run_worker_task(
                                task,
                                lambda d: job.progress.append(d),
                                job.cancel_event,
                            )
                        _finish_job(job, result=result)
                    except HeavyJobCancelledError:
                        _finish_job(job, error="walk-forward 已取消")
                    except Exception as e:
                        _finish_job(job, error=str(e))

                threading.Thread(target=_run_wf, daemon=True).start()

        cursor = 0
        tick = 0
        try:
            while True:
                if job.done:
                    if job.error:
                        yield f"event: error\ndata: {json.dumps({'message': job.error}, ensure_ascii=False)}\n\n"
                    elif job.cancel_event.is_set():
                        yield f"event: error\ndata: {json.dumps({'message': 'walk-forward 已取消'}, ensure_ascii=False)}\n\n"
                    elif job.result is not None:
                        yield f"event: done\ndata: {json.dumps(_json_safe(job.result), ensure_ascii=False, default=str)}\n\n"
                    return
                tick += 1
                if tick % 4 == 0 and await request.is_disconnected():
                    break
                while cursor < len(job.progress):
                    msg = job.progress[cursor]
                    cursor += 1
                    yield f"event: progress\ndata: {json.dumps(msg, ensure_ascii=False, default=str)}\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/walkforward/cancel")
async def walkforward_cancel(request: Request):
    """取消 walk-forward 任务 — 传 stream 首事件回吐的 job_key。"""
    body = await request.json()
    job_key = body.get("job_key", "")
    job = _running_jobs.get(job_key)
    if job and not job.done:
        job.cancel_event.set()
        return {"ok": True}
    return {"ok": False, "message": "任务不存在或已完成"}
