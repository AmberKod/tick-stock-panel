"""Postgres 连接池 + 健康状态 + 熔断器(S4 切片①)。

职责边界:
  - 只做「连接 + 投递 + 健康可见」三件事, 不含任何业务 SQL(DDL 在 schema.py,
    行的构造与裁剪在 job_state.py)。
  - **所有写都在单写线程里串行执行**, 事件循环与 worker 线程共用一条投递路径,
    既避免阻塞事件循环, 也避免池争用。

⚠️ 硬约束: 本模块**禁止直接读取进程环境变量**取配置, 一律走 app.config.settings。
    原因见 app/config.py 里 Postgres 字段的注释 —— 线上容器的进程 env 是容器
    创建时 env_file 固化下来的快照, 实测已经过期(POSTGRES_PORT=5432 且无
    POSTGRES_HOST), 而 settings 读的是实时 bind mount 的 /app/.env。
    有静态测试断言本文件源码不含那两个取值标识符, 别踩。
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal

logger = logging.getLogger(__name__)

DbStatus = Literal["disabled", "ok", "unavailable"]

# ── 熔断参数 ────────────────────────────────────────────────────────────
# 连续失败这么多次后开窗; 窗口内的写请求直接丢弃(不建连、不等待)。
_BREAKER_THRESHOLD = 3
_BREAKER_WINDOW_S = 60.0

_lock = threading.Lock()

_status: DbStatus = "ok"
_reason: str = ""
_since: float = 0.0          # 进入当前非 ok 状态的时间戳(time.time())
_open_until: float = 0.0     # 熔断窗口结束时间戳
_dropped_writes: int = 0
_failures: int = 0           # 连续失败计数(成功即清零)

_pool: Any = None            # psycopg_pool.ConnectionPool | None
_writer: ThreadPoolExecutor | None = None
_closed = False

_HOST_CACHE: str = ""


def host_ident() -> str:
    """产生本进程/容器的标识, 写进 job_run.host。

    用 socket.gethostname() 而不是环境变量: 容器里主机名就是容器 id 前缀,
    天然区分实例, 且不受 §0.1 的 env 固化问题影响。
    """
    global _HOST_CACHE
    if not _HOST_CACHE:
        try:
            _HOST_CACHE = socket.gethostname()
        except Exception:
            _HOST_CACHE = "unknown"
    return _HOST_CACHE


# ── 投递 ────────────────────────────────────────────────────────────────

def _writer_pool() -> ThreadPoolExecutor:
    """单写线程。max_workers=1 顺带把所有写串行化(池 max_size=2 足够)。"""
    global _writer
    if _writer is None:
        _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
    return _writer


def submit(fn: Callable[..., None], *args: Any, count_drop: bool = True) -> bool:
    """把一次写投递到单写线程。返回是否真的投递。

    熔断窗口内 / 开关关闭时返回 False 且**不**调用 fn(调用方据此判定丢弃)。
    调用方无需捕获异常 —— fn 里的异常在写线程内被吞掉并计入熔断。
    """
    if not _acquire(count_drop=count_drop):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _writer_pool().submit(_guarded(fn), *args)
    else:
        loop.run_in_executor(_writer_pool(), _guarded(fn), *args)
    return True


def _guarded(fn: Callable[..., None]) -> Callable[..., None]:
    def _wrap(*args: Any) -> None:
        try:
            fn(*args)
        except Exception as e:      # noqa: BLE001 — 兜一切, 绝不让异常逃出写线程
            _record_failure(e)
        else:
            _record_success()

    return _wrap


def drain(timeout: float = 5.0) -> bool:
    """等待已投递的写全部完成。仅用于测试与关闭路径。"""
    if _status == "disabled" or _writer is None:
        return True
    done = threading.Event()
    try:
        _writer_pool().submit(lambda: done.set())
    except Exception:
        return False
    return done.wait(timeout)


# ── 熔断 ────────────────────────────────────────────────────────────────

def _acquire(count_drop: bool) -> bool:
    """是否允许发起一次 DB 操作。熔断窗口内直接丢弃。"""
    global _dropped_writes
    if _status == "disabled":
        return False
    if _open_until and time.monotonic() < _open_until:
        if count_drop:
            with _lock:
                _dropped_writes += 1
        return False
    return True


def _record_failure(exc: BaseException) -> None:
    global _status, _reason, _since, _open_until, _failures
    with _lock:
        _failures += 1
        if _status != "unavailable":
            _status = "unavailable"
            _reason = f"{type(exc).__name__}: {exc}"
            _since = time.time()
        else:
            _reason = f"{type(exc).__name__}: {exc}"
        if _failures >= _BREAKER_THRESHOLD:
            _open_until = time.monotonic() + _BREAKER_WINDOW_S
        dropped = _dropped_writes
        failures = _failures
        opened = _open_until
    logger.warning(
        "db write failed (%d consecutive, breaker=%s, dropped_writes=%d): %s",
        failures, "open" if opened > time.monotonic() else "closed", dropped, exc,
    )


def _record_success() -> None:
    global _status, _reason, _since, _open_until, _failures
    with _lock:
        if _status != "ok" or _failures:
            if _status != "ok":
                logger.info("db recovered: 写成功, 状态复位为 ok")
            _status = "ok"
            _reason = ""
            _since = 0.0
        _open_until = 0.0
        _failures = 0


def db_status() -> dict[str, Any]:
    """对外暴露的健康状态块。挂在 /api/pipeline/jobs 的 history_db 里。"""
    with _lock:
        return {
            "status": _status,
            "reason": _reason or None,
            "since": _since or None,
            "dropped_writes": _dropped_writes,
            "breaker_open": bool(_open_until and time.monotonic() < _open_until),
            "host": host_ident(),
        }


# ── 连接 ────────────────────────────────────────────────────────────────

def _conninfo() -> str:
    from app.config import settings

    # ⚠️ 必须走 make_conninfo, 不能手工拼 "k=v k=v" 串:
    #    含空格的值会被 libpq 按空白切成多个 key —— `options=-c statement_timeout=5000`
    #    会被解析成 options='-c' + 未知键 'statement_timeout', 连接直接失败
    #    (真连 postgres-18.6 才暴露, 单元测试里的假写入器发现不了)。
    #    make_conninfo 负责加引号转义, 密码里有空格/特殊字符也不会炸。
    from psycopg.conninfo import make_conninfo

    params: dict[str, Any] = {
        "host": settings.postgres_host,
        "port": int(settings.postgres_port),
        "user": settings.postgres_user,
        "dbname": settings.postgres_db,
        # connect_timeout 是硬要求: host.docker.internal 可能先解析到不可路由的
        # IPv6, 没有超时会把写线程挂死。
        "connect_timeout": int(settings.postgres_connect_timeout_s),
        # 语句级兜底: 单条 UPSERT 卡住时不拖死写线程(单位毫秒)。
        "options": "-c statement_timeout=5000",
    }
    if settings.postgres_password:
        params["password"] = settings.postgres_password
    return make_conninfo(**params)


def _ensure_pool() -> Any:
    """惰性建池。失败抛异常, 由 init()/调用方转成 unavailable。"""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            _conninfo(),
            min_size=0,      # 空闲时不占连接
            max_size=2,      # 单写线程 + 偶尔的读
            timeout=3.0,
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[Any]:
    """取一条连接。调用方必须自己 try/except。"""
    pool = _ensure_pool()
    with pool.connection() as conn:
        yield conn


def fetch(fn: Callable[[Any], Any]) -> tuple[bool, Any]:
    """同步执行一次读。返回 (ok, value)。

    ok=False 表示没拿到(开关关闭 / 熔断中 / 查询异常), 调用方必须据此降级
    并带上 reason —— **绝不能把 False 当成「查到空结果」**。
    """
    if not _acquire(count_drop=False):
        return False, None
    try:
        with connection() as conn:
            return True, fn(conn)
    except Exception as e:      # noqa: BLE001
        _record_failure(e)
        return False, None


# ── 生命周期 ────────────────────────────────────────────────────────────

def preflight() -> tuple[bool, str]:
    """启动预检: 解析全部地址 + TCP 连一次, 把结果打进日志。

    目的不是「保证能连上」, 而是**让失败可见**: host.docker.internal 解析到
    IPv6 还是 IPv4、哪个地址通, 全写在日志里, 排障时不用进容器猜。
    """
    from app.config import settings

    host = settings.postgres_host
    try:
        port = int(settings.postgres_port)
    except Exception:
        return False, f"POSTGRES_PORT 非法: {settings.postgres_port!r}"

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"getaddrinfo({host!r}) 失败: {e}"

    addrs = sorted({ai[4][0] for ai in infos})
    tcp_ok = False
    last_err = ""
    for addr in addrs:
        try:
            sock = socket.create_connection(
                (addr, port), timeout=float(settings.postgres_connect_timeout_s)
            )
        except Exception as e:
            last_err = str(e)
            continue
        sock.close()
        tcp_ok = True
        break

    detail = f"host={host}:{port} addrs={addrs} tcp={'ok' if tcp_ok else 'FAIL'}"
    if not tcp_ok and last_err:
        detail += f" err={last_err}"
    return tcp_ok, detail


def init() -> bool:
    """启动挂钩: 建池 + 预检 + 建表 + reconcile。

    **永不抛异常**: 任何一步失败都只是把状态置 unavailable 并记录 WARN,
    启动照常完成 —— DB 挂了不能让服务起不来(本项目铁律)。
    """
    global _status, _reason, _since

    from app.config import settings

    if not settings.tsp_postgres_enabled:
        with _lock:
            _status = "disabled"
            _reason = "TSP_POSTGRES_ENABLED=false"
            _since = time.time()
        logger.warning("db=disabled (TSP_POSTGRES_ENABLED=false): job 状态镜像未启用, "
                       "其余功能不受影响")
        return False

    ok, detail = preflight()
    logger.info("db preflight: %s", detail)
    if not ok:
        with _lock:
            _status = "unavailable"
            _reason = f"preflight 失败: {detail}"
            _since = time.time()
        logger.warning("db=unavailable reason=preflight_failed detail=%s (服务继续启动, "
                       "job 照常执行, 仅长历史不可查)", detail)
        return False

    try:
        with connection() as conn:
            from app.state import schema

            schema.ensure_schema(conn)
            interrupted = schema.reconcile_interrupted(conn)
            if interrupted:
                logger.warning("db reconcile: 上次进程遗留的 %d 条 running/pending 已标记为 "
                               "interrupted(进程重启, 不可能还活着)", interrupted)
    except Exception as e:      # noqa: BLE001
        with _lock:
            _status = "unavailable"
            _reason = f"{type(e).__name__}: {e}"
            _since = time.time()
        logger.warning("db=unavailable reason=init_failed: %s (服务继续启动)", e)
        return False

    _record_success()
    logger.info("db=ok: job 状态镜像已启用 (%s)", detail)
    return True


def close() -> None:
    """关闭钩子。幂等, 永不抛异常。"""
    global _pool, _writer, _closed
    if _closed:
        return
    _closed = True
    try:
        if _writer is not None:
            # ⚠️ 不能传 timeout=: ThreadPoolExecutor.shutdown() 的 timeout 参数是
            #    Python 3.13 才加的, 本项目 venv 是 3.12 —— 传了直接 TypeError,
            #    关闭路径的异常会被下面的 except 吞成一条 WARNING, 极难发现。
            #    按设计 §6.2 用 wait=False: 关闭不等待在途写, 反正 DB 是旁路镜像。
            _writer.shutdown(wait=False)
    except Exception as e:      # noqa: BLE001
        logger.warning("db writer shutdown failed: %s", e)
    try:
        if _pool is not None:
            _pool.close(timeout=2.0)
    except Exception as e:      # noqa: BLE001
        logger.warning("db pool close failed: %s", e)
    _pool = None
    _writer = None


def reset_for_tests() -> None:
    """把模块级状态复位到「未初始化」。仅测试用。"""
    global _status, _reason, _since, _open_until, _dropped_writes, _failures, _pool, _writer, _closed
    if _writer is not None:
        _writer.shutdown(wait=False)
    with _lock:
        _status = "ok"
        _reason = ""
        _since = 0.0
        _open_until = 0.0
        _dropped_writes = 0
        _failures = 0
    _pool = None
    _writer = None
    _closed = False
