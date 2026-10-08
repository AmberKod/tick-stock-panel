"""JobStore → Postgres 的单向写镜像(S4 切片①)。

定位: **镜像, 不是权威源**。
  - 权威源仍然是 `JobStore` 的内存 + `data/job_store/*.json`。
  - 本模块只负责「把 job 的当前快照投到 DB」, 供长历史查询与聚合使用。
  - **任何异常都在本模块内消化**: 记 WARN + 计入熔断, 绝不向上抛、绝不阻塞。
    DB 挂了, job 照跑、照常落 parquet、照常 succeed/fail。

两个必须显式声明而非静默的事(对应设计 §7.5):
  - `result` 被裁剪过 → `result_trimmed = true`
  - `result` 超限被整个丢弃 → `result = NULL` 且 `result_overflow = true`
不能让人拿到一个静默变小的 result 还以为完整。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from app.state import db

logger = logging.getLogger(__name__)

# ── 节流 ────────────────────────────────────────────────────────────────
# progress() 是逐标的回调: market_daily 单次 5897s, 无节流会产生上万次 UPSERT。
# 终态(create/start/succeed/fail)无条件写; progress 满足三者之一才写。
_THROTTLE_INTERVAL_S = 10.0
_PROGRESS_BUCKET = 5          # 跨过 5% 的整数档即写
_LAST_EMIT_MAX = 128

_last_emit: dict[str, tuple[float, str | None, int]] = {}

# ── result 裁剪 ─────────────────────────────────────────────────────────
# 实测单条 result 3.68 MB(2840 个 universe_symbols + 223 个 failed_symbols)。
# 大数组的**长度**已经抽到 rows_* 列, 明细没必要进库。
_DROP_KEYS = ("universe_symbols", "completed_symbols", "failures", "items", "symbols", "rows")
_FAILED_SYMBOLS_KEEP = 50     # 够定位问题
_RESULT_MAX_BYTES = 256 * 1024

# ── 测试注入点 ──────────────────────────────────────────────────────────
# 设了 sink 之后, 镜像写会调用它而不是真的执行 SQL —— 让「双写契约」可以在
# 完全离线的单元测试里验证(节流 / 裁剪 / 熔断 / fail-closed 全是纯逻辑)。
_MIRROR_SINK: Callable[[dict[str, Any]], None] | None = None


def set_mirror_sink(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _MIRROR_SINK
    _MIRROR_SINK = fn


# ── 写 ──────────────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO job_run (
    id, job_type, market, trigger_source, host, app_version,
    status, stage, progress, stage_pct, timeout_s,
    started_at, last_progress_at, finished_at, duration_s,
    rows_total, rows_done, rows_failed,
    error, error_kind, result, log, result_trimmed, result_overflow,
    updated_at
) VALUES (
    %(id)s, %(job_type)s, %(market)s, %(trigger_source)s, %(host)s, %(app_version)s,
    %(status)s, %(stage)s, %(progress)s, %(stage_pct)s, %(timeout_s)s,
    %(started_at)s, %(last_progress_at)s, %(finished_at)s, %(duration_s)s,
    %(rows_total)s, %(rows_done)s, %(rows_failed)s,
    %(error)s, %(error_kind)s, %(result)s, %(log)s, %(result_trimmed)s, %(result_overflow)s,
    now()
)
ON CONFLICT (id) DO UPDATE SET
    job_type         = EXCLUDED.job_type,
    market           = EXCLUDED.market,
    trigger_source   = EXCLUDED.trigger_source,
    host             = EXCLUDED.host,
    app_version      = EXCLUDED.app_version,
    status           = EXCLUDED.status,
    stage            = EXCLUDED.stage,
    progress         = EXCLUDED.progress,
    stage_pct        = EXCLUDED.stage_pct,
    timeout_s        = EXCLUDED.timeout_s,
    -- 时间列用 COALESCE: progress 的 upsert 不带 finished_at, 不能把已知值抹成 NULL
    started_at       = COALESCE(EXCLUDED.started_at, job_run.started_at),
    last_progress_at = COALESCE(EXCLUDED.last_progress_at, job_run.last_progress_at),
    finished_at      = COALESCE(EXCLUDED.finished_at, job_run.finished_at),
    duration_s       = COALESCE(EXCLUDED.duration_s, job_run.duration_s),
    rows_total       = EXCLUDED.rows_total,
    rows_done        = EXCLUDED.rows_done,
    rows_failed      = EXCLUDED.rows_failed,
    error            = EXCLUDED.error,
    error_kind       = EXCLUDED.error_kind,
    result           = EXCLUDED.result,
    log              = EXCLUDED.log,
    result_trimmed   = EXCLUDED.result_trimmed,
    result_overflow  = EXCLUDED.result_overflow,
    updated_at       = now()
"""


def mirror(job: dict[str, Any], reason: str) -> bool:
    """把 job 的当前快照镜像进 DB。返回是否真的投递。

    reason: create / start / progress / succeed / fail。

    契约(不可违反):
      - 永不抛异常, 永不阻塞调用方。
      - 返回 False 只表示「这次没写」(节流 / 熔断 / 未启用), 调用方无需处理。
    """
    try:
        jid = job.get("id")
        if not jid:
            return False
        if reason == "progress" and not _should_emit(job):
            return False

        row = _build_row(job)

        if reason in ("succeed", "fail"):
            _last_emit.pop(jid, None)
        else:
            _last_emit[jid] = (
                time.monotonic(),
                job.get("stage"),
                int(job.get("progress") or 0) // _PROGRESS_BUCKET,
            )
            if len(_last_emit) > _LAST_EMIT_MAX:
                for k in list(_last_emit)[: len(_last_emit) - _LAST_EMIT_MAX]:
                    _last_emit.pop(k, None)

        sink = _MIRROR_SINK
        return db.submit(sink or _write_row, row)
    except Exception as e:      # noqa: BLE001 — 镜像是全兜旁路, 绝不影响主流程
        logger.warning("job_state mirror failed (reason=%s): %s", reason, e)
        return False


def _should_emit(job: dict[str, Any]) -> bool:
    """progress 节流: ≥10s / stage 变化 / 跨 5% 档, 三者满足其一。"""
    now = time.monotonic()
    stage = job.get("stage")
    bucket = int(job.get("progress") or 0) // _PROGRESS_BUCKET
    prev = _last_emit.get(job["id"])
    if prev is None:
        return True
    t0, s0, b0 = prev
    return (now - t0) >= _THROTTLE_INTERVAL_S or s0 != stage or b0 != bucket


def _write_row(row: dict[str, Any]) -> None:
    params = dict(row)
    params["result"] = _jsonb(row.get("result"))
    params["log"] = _jsonb(row.get("log"))
    with db.connection() as conn:
        conn.execute(_UPSERT_SQL, params)


def _jsonb(value: Any) -> Any:
    if value is None:
        return None
    from psycopg.types.json import Jsonb

    return Jsonb(value)


# ── 行构造 ──────────────────────────────────────────────────────────────

def _to_dt(ts: Any) -> Any:
    """把 JobStore 的 "2026-10-08T15:00:29Z" 解析成 tz-aware datetime。

    复用 pipeline_jobs._parse_utc(不在本模块重写, 保证与 JSON 侧解析一致)。
    延迟导入: pipeline_jobs 会 import 本模块, 模块级互导会成环。
    """
    if not ts:
        return None
    try:
        from app.services.pipeline_jobs import _parse_utc

        return _parse_utc(ts)
    except Exception:
        return None


def _app_version() -> str | None:
    try:
        from app import __version__

        return __version__
    except Exception:
        return None


def _build_row(job: dict[str, Any]) -> dict[str, Any]:
    job_type = job.get("job_type") or "pipeline"
    status = job.get("status") or "pending"
    result, trimmed, overflow = _trim_result(job.get("result"))
    total, done, failed = _extract_counts(job_type, job.get("result"))

    return {
        "id": job["id"],
        "job_type": job_type,
        "market": job.get("market"),
        "trigger_source": job.get("trigger_source") or "manual",
        "host": db.host_ident(),
        "app_version": _app_version(),
        "status": status,
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "stage_pct": job.get("stage_pct"),
        "timeout_s": job.get("timeout_s"),
        "started_at": _to_dt(job.get("started_at")),
        "last_progress_at": _to_dt(job.get("last_progress_at")),
        "finished_at": _to_dt(job.get("finished_at")),
        # duration_s 直接用 JobStore 算好的值, 不另算 —— 保证与 JSON 侧完全一致
        "duration_s": job.get("duration_s"),
        "rows_total": total,
        "rows_done": done,
        "rows_failed": failed,
        "error": job.get("error"),
        "error_kind": _error_kind(status, job.get("error")),
        "result": result,
        "log": job.get("log"),
        "result_trimmed": trimmed,
        "result_overflow": overflow,
    }


def _error_kind(status: str, error: Any) -> str | None:
    """失败原因分类。分不出就 unknown —— 不猜, 也不留空(空会被读成"没原因")。"""
    if status != "failed" or not error:
        return None
    text = str(error)
    # 先判 timeout: reap 的消息是「超时自动取消: 进度停滞 ...」, 含"取消"二字,
    # 但它的成因是超时, 归类成 cancelled 会丢掉真正的信号。
    if "超时" in text or "timeout" in text.lower():
        return "timeout"
    if "取消" in text or "cancelled" in text.lower():
        return "cancelled"
    if any(k in text.lower() for k in ("provider", "限流", "rate limit", "网络", "连接")):
        return "provider"
    return "unknown"


# ── 计数抽取(§4.3)────────────────────────────────────────────────────────
# 纪律: 抽不出来就写 NULL, **绝不写 0**。0 会被读成「确认处理了 0 条」, 那是假数据。

def _len_of(value: Any) -> int | None:
    return len(value) if isinstance(value, (list, tuple)) else None


def _int_of(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _extract_counts(job_type: str, result: Any) -> tuple[int | None, int | None, int | None]:
    """按 job_type 抽 (rows_total, rows_done, rows_failed)。未识别一律全 NULL。"""
    if not isinstance(result, dict):
        return None, None, None

    if job_type == "market_daily":
        total = _int_of(result.get("symbols_total"))
        if total is None:
            total = _len_of(result.get("universe_symbols"))
        done = _len_of(result.get("completed_symbols"))
        if done is None:
            done = _int_of(result.get("symbols_completed"))
        failed = _len_of(result.get("failed_symbols"))
        if failed is None:
            failed = _len_of(result.get("failures"))
        return total, done, failed

    if job_type == "pipeline":
        return None, _int_of(result.get("universe_size")), _int_of(result.get("lagging_symbols"))

    if job_type == "minute_sync":
        return None, _int_of(result.get("universe_size")), None

    if job_type == "instruments_sync":
        return None, _int_of(result.get("universe_sync_rows")), None

    return None, None, None


# ── result 裁剪(§7.5)────────────────────────────────────────────────────

def _trim_result(result: Any) -> tuple[Any, bool, bool]:
    """返回 (result, trimmed, overflow)。非 dict 原样返回(不裁剪, 但要过体积关)。"""
    if result is None:
        return None, False, False

    trimmed = False
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for k, v in result.items():
            if k in _DROP_KEYS and isinstance(v, (list, tuple)):
                trimmed = True
                continue
            if k == "failed_symbols" and isinstance(v, (list, tuple)) and len(v) > _FAILED_SYMBOLS_KEEP:
                out[k] = list(v)[: _FAILED_SYMBOLS_KEEP]
                trimmed = True
                continue
            out[k] = v
        result = out
    else:
        result = result

    if _byte_size(result) > _RESULT_MAX_BYTES:
        # 超限就整个丢弃并**显式声明**, 绝不静默给一个不完整的 result
        return None, trimmed, True
    return result, trimmed, False


def _byte_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return _RESULT_MAX_BYTES + 1     # 序列化不了就当超处理, 宁可显式声明


# ── 读(历史 / 聚合)────────────────────────────────────────────────────────

_HISTORY_COLUMNS = (
    "id", "job_type", "market", "trigger_source", "host", "status", "stage",
    "progress", "started_at", "finished_at", "duration_s",
    "rows_total", "rows_done", "rows_failed", "error", "error_kind",
    "result_trimmed", "result_overflow",
)


def fetch_history(
    limit: int = 50,
    status: str | None = None,
    job_type: str | None = None,
    days: int | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """读 DB 历史。返回 (ok, rows)。

    ok=False 只表示**没拿到**(开关关闭 / 熔断 / 查询失败), 调用方必须据此降级并
    带上 reason —— 绝不能把 ok=False 当成「查到 0 条」。
    """
    def _q(conn: Any) -> list[dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
        if status:
            conds.append("status = %(status)s")
            params["status"] = status
        if job_type:
            conds.append("job_type = %(job_type)s")
            params["job_type"] = job_type
        if days:
            conds.append("started_at >= now() - make_interval(days => %(days)s)")
            params["days"] = int(days)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        sql = (
            "SELECT " + ", ".join(_HISTORY_COLUMNS) +
            " FROM job_run" + where +
            " ORDER BY started_at DESC NULLS LAST LIMIT %(limit)s"
        )
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    ok, rows = db.fetch(_q)
    return ok, (rows or [])


def fetch_stats(days: int = 30) -> tuple[bool, dict[str, Any] | None]:
    """成功率 / 耗时 / 处理量聚合。返回 (ok, stats)。

    p95 在 Python 侧算: N 很小(日均几十条), 用 percentile_cont 反而绕。
    """

    def _q(conn: Any) -> dict[str, Any]:
        cur = conn.execute(
            "SELECT status, duration_s, rows_done FROM job_run"
            " WHERE started_at >= now() - make_interval(days => %(days)s)",
            {"days": int(days)},
        )
        rows = cur.fetchall()
        total = len(rows)
        succeeded = sum(1 for r in rows if r[0] == "succeeded")
        failed = sum(1 for r in rows if r[0] == "failed")
        durations = sorted(r[1] for r in rows if r[1] is not None)
        rows_done = sum(r[2] for r in rows if r[2] is not None)
        return {
            "days": int(days),
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            # 没有样本时是 None 而不是 0.0 —— 0 会被读成「成功率 0%」
            "success_rate": round(succeeded / total, 4) if total else None,
            "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
            "p95_duration_s": _p95(durations),
            "rows_done": rows_done if rows_done else None,
        }

    return db.fetch(_q)


def _p95(sorted_values: list[float]) -> float | None:
    if not sorted_values:
        return None
    idx = int(round(0.95 * (len(sorted_values) - 1)))
    return round(sorted_values[idx], 2)


def local_history(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """降级回退: 读本地 JSON(权威源, 但只有最近 50 条)。

    只支持 status 过滤 —— JSON 里没有 job_type / market 列, 不能假装有。
    """
    from app.services.pipeline_jobs import job_store

    items = job_store.list_recent(limit=max(1, min(int(limit), 500)))
    if status:
        items = [i for i in items if i.get("status") == status]
    return items


# ── 对外 payload(API 层直接用)─────────────────────────────────────────────

def history_payload(
    limit: int = 50,
    status: str | None = None,
    job_type: str | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """`/api/pipeline/jobs/history` 的响应体。

    DB 不可用时**降级回退本地 JSON**, 但必须打上
    `available=false / source="local_json" / reason=...` ——
    绝不返回无解释的空列表(那会被读成「最近没跑过」)。
    """
    health = db.db_status()
    if health["status"] == "disabled":
        return {
            "available": False,
            "source": "local_json",
            "reason": "postgres 镜像未启用 (TSP_POSTGRES_ENABLED=false)",
            "limit": limit,
            "items": local_history(limit, status),
        }

    ok, rows = fetch_history(limit=limit, status=status, job_type=job_type, days=days)
    if ok:
        return {
            "available": True,
            "source": "postgres",
            "reason": None,
            "limit": limit,
            "items": rows,
        }

    reason = health.get("reason") or "postgres 不可用"
    logger.warning("history 降级回退本地 JSON: %s", reason)
    return {
        "available": False,
        "source": "local_json",
        "reason": reason,
        "limit": limit,
        "items": local_history(limit, status),
    }


def stats_payload(days: int = 30) -> dict[str, Any]:
    """`/api/pipeline/jobs/stats` 的响应体。

    聚合**没有**本地降级: 本地 JSON 只有最近 50 条, 拿它算「30 天成功率」
    是假数据。不可用时显式返回 available=false + reason + 全 None。
    """
    health = db.db_status()
    if health["status"] == "disabled":
        return {"available": False, "source": "none", "reason": "postgres 镜像未启用 "
                "(TSP_POSTGRES_ENABLED=false)", "days": days, "stats": None}

    ok, stats = fetch_stats(days=days)
    if ok and stats is not None:
        return {"available": True, "source": "postgres", "reason": None, "days": days, "stats": stats}
    return {
        "available": False,
        "source": "none",
        "reason": health.get("reason") or "postgres 不可用",
        "days": days,
        "stats": None,
    }
