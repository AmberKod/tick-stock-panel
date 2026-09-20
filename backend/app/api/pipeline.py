"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.data import invalidate_storage_cache
from app.jobs import daily_pipeline
from app.services.pipeline_jobs import (
    JobCancelledError,
    job_store,
    release_run_slot,
    try_acquire_run_slot,
)

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
_financial_tasks: set[asyncio.Task[None]] = set()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    卡死判定按「进度停滞」而非总时长(慢带宽下长任务不会被误杀), 见 reap_stale。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities

    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    job_id, is_new = job_store.create()
    if not is_new:
        return {"job_id": job_id, "reused": True}

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(request.app.state, "quote_service", None)
        try:
            job_store.start(job_id)
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                if qs:
                    with qs.paused():
                        return daily_pipeline.run_now(repo, capset, on_progress=progress)
                return daily_pipeline.run_now(repo, capset, on_progress=progress)

            result = await loop.run_in_executor(_long_task_executor, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
            repo.refresh_cache()  # 刷新 Polars 缓存
        except JobCancelledError:
            # 已被 reap/手动取消终止: job 状态已由 terminate() 写为 failed,
            # 拉取线程在分块回调处自行退出, 这里无需(也无法)再写状态。
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as e:
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    # RUF006: 持有引用防 GC 中途回收
    _task = asyncio.create_task(task())
    _financial_tasks.add(_task)
    _task.add_done_callback(_financial_tasks.discard)
    return {"job_id": job_id, "reused": False}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端持续轮询, 进度停滞超阈值后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 running 的 job(协作式: 拉取线程在当前分块完成后自行退出)。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.terminate(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }


def _parse_market_daily_request(body: dict) -> tuple[str, list[str] | None, object, object, str, int | None]:
    from datetime import datetime, time

    market = str(body.get("market") or "").upper()
    if market not in {"HK", "US"}:
        raise HTTPException(status_code=400, detail="market 只支持 HK 或 US")
    symbols = body.get("symbols")
    if symbols is not None and (not isinstance(symbols, list) or not symbols):
        raise HTTPException(status_code=400, detail="symbols 必须是非空数组，或省略以使用全量标的池")
    if isinstance(symbols, list) and len(symbols) > 100000:
        raise HTTPException(status_code=400, detail="symbols 数量不能超过 100000")
    start_raw = str(body.get("start_date") or "")
    end_raw = str(body.get("end_date") or "")
    try:
        start_date = datetime.combine(datetime.fromisoformat(start_raw).date(), time.min)
        end_date = datetime.combine(datetime.fromisoformat(end_raw).date(), time.max)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start_date/end_date 必须是 YYYY-MM-DD") from exc
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    mode = str(body.get("mode") or "full")
    if mode not in {"full", "incremental"}:
        raise HTTPException(status_code=400, detail="mode 只支持 full 或 incremental")
    batch_size = body.get("batch_size")
    if batch_size is not None and (isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500):
        raise HTTPException(status_code=400, detail="batch_size 必须在 1 到 500 之间")
    normalized_symbols = None if symbols is None else [str(symbol) for symbol in symbols]
    return market, normalized_symbols, start_date, end_date, mode, batch_size


async def _start_market_daily_job(
    request: Request,
    *,
    market: str,
    symbols: list[str] | None,
    start_date,
    end_date,
    mode: str,
    batch_size: int | None,
    resume_checkpoint: dict | None = None,
) -> dict:
    from app.api.data import invalidate_storage_cache
    from app.services.market_daily_sync import run_market_daily_sync

    repo = request.app.state.repo
    capset = request.app.state.capabilities
    job_id, is_new = job_store.create(long_running=True)
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行，请稍后再试")
            return
        loop = asyncio.get_event_loop()

        def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False) -> None:
            job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

        try:
            job_store.start(job_id)
            result = await loop.run_in_executor(
                _long_task_executor,
                lambda: run_market_daily_sync(
                    repo=repo,
                    capset=capset,
                    job_id=job_id,
                    market=market,
                    symbols=symbols,
                    start_date=start_date,
                    end_date=end_date,
                    mode=mode,
                    resume_checkpoint=resume_checkpoint,
                    batch_size=batch_size,
                    on_progress=progress,
                    compute_indicators=True,
                ),
            )
            job_store.succeed(job_id, result)
            from app.api.market_data import refresh_market_state

            refresh_market_state(request, market)
        except JobCancelledError:
            logger.warning("market daily job %s cancelled", job_id)
        except Exception as exc:
            logger.exception("market daily job failed: %s", job_id)
            job_store.fail(job_id, str(exc))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    _task = asyncio.create_task(task())
    _financial_tasks.add(_task)
    _task.add_done_callback(_financial_tasks.discard)
    return {"status": "started", "job_id": job_id, "market": market, "mode": mode}


async def _start_hk_financial_job(request: Request, *, symbols: list[str] | None) -> dict:
    from app.api.market_data import refresh_market_state
    from app.services.financial_sync import sync_hk_financial_history
    from app.services.market_data_status import market_data_generation

    repo = request.app.state.repo
    capset = getattr(request.app.state, "capabilities", None)
    job_store.reap_stale()
    job_id, is_new = job_store.create(long_running=True)
    if not is_new:
        return {"status": "reused", "job_id": job_id}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行, 请稍后再试")
            return
        def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False) -> None:
            job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)
        try:
            job_store.start(job_id)
            result = await asyncio.get_running_loop().run_in_executor(
                _long_task_executor,
                lambda: sync_hk_financial_history(repo.store.data_dir, capset, symbols=symbols,
                                                  on_progress=progress, job_id=job_id),
            )
            result["data_generation"] = market_data_generation(repo.store.data_dir, "HK")
            job_store.succeed(job_id, result)
            refresh_market_state(request, "HK")
        except JobCancelledError:
            logger.warning("HK financial job %s cancelled", job_id)
        except Exception:
            logger.exception("HK financial job failed: %s", job_id)
            job_store.fail(job_id, "港股历史财务同步失败, 已保留现有历史资料")
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    background_task = asyncio.create_task(task())
    _financial_tasks.add(background_task)
    background_task.add_done_callback(_financial_tasks.discard)
    return {"status": "started", "job_id": job_id, "market": "HK", "operation": "financial_sync"}


@router.post("/market-daily/run")
async def run_market_daily(request: Request) -> dict:
    """启动港股/美股可恢复日 K 同步任务。"""
    body = await request.json()
    market, symbols, start_date, end_date, mode, batch_size = _parse_market_daily_request(body)
    return await _start_market_daily_job(
        request,
        market=market,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        mode=mode,
        batch_size=batch_size,
    )


@router.post("/market-daily/retry")
async def retry_market_daily(request: Request) -> dict:
    """仅重试既有 checkpoint 中的失败标的。"""
    from datetime import datetime, time

    from app.services.market_daily_sync import checkpoint_path, read_checkpoint

    body = await request.json()
    source_job_id = str(body.get("job_id") or "").strip()
    if not source_job_id:
        raise HTTPException(status_code=400, detail="job_id 必填")
    source = read_checkpoint(checkpoint_path(request.app.state.repo.store.data_dir, source_job_id))
    if not source:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    market = str(source.get("market") or "").upper()
    if market not in {"HK", "US"}:
        raise HTTPException(status_code=400, detail="checkpoint market 不支持")
    try:
        start_date = datetime.combine(datetime.fromisoformat(str(body.get("start_date") or source["coverage_start"])).date(), time.min)
        end_date = datetime.combine(datetime.fromisoformat(str(body.get("end_date") or source["coverage_end"])).date(), time.max)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="checkpoint 日期无效") from exc
    symbols = [str(symbol) for symbol in source.get("universe_symbols", [])]
    if not symbols:
        raise HTTPException(status_code=400, detail="checkpoint 缺少 universe_symbols，无法恢复")
    batch_size = body.get("batch_size")
    if batch_size is not None and (isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500):
        raise HTTPException(status_code=400, detail="batch_size 必须在 1 到 500 之间")
    return await _start_market_daily_job(
        request,
        market=market,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        mode="retry_only",
        batch_size=batch_size,
        resume_checkpoint=source,
    )
