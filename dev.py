#!/usr/bin/env python3
"""Start the FastAPI and Vite development servers with hot reload enabled."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
ENV_FILE = ROOT / ".env"
LOG_DIR = ROOT / "logs" / "launcher"
LAUNCHER_LOG_RETENTION = 50


def read_dotenv_value(path: Path, name: str) -> str | None:
    """Read one launcher-owned key without executing the dotenv file."""
    if not path.is_file():
        return None

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port: {value}") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535: {port}")
    return port


def first_value(*values: str | None, default: str) -> str:
    return next((value for value in values if value not in (None, "")), default)


def backend_python() -> Path:
    candidates = (
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",
        BACKEND_DIR / ".venv" / "bin" / "python",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def check_port_available(host: str, port: int, label: str) -> None:
    """Reject an actively listening service, but tolerate stale Windows sockets."""
    targets = [host]
    if host == "0.0.0.0":
        targets = ["127.0.0.1"]
    elif host == "::":
        targets = ["::1"]

    family = socket.AF_INET6 if ":" in targets[0] else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        try:
            connected = sock.connect_ex((targets[0], port)) == 0
        except OSError:
            connected = False
    if connected:
        raise RuntimeError(f"{label} port {port} is already in use on {host}")


def make_launcher_log(log_dir: Path = LOG_DIR) -> Path:
    """Allocate a fresh timestamped launcher log; never overwrites an existing file.

    The filename pattern is ``dev-YYYYMMDD-HHMMSS.log`` with an optional ``-N``
    suffix to disambiguate launches that share the same wall-clock second.
    Uses an exclusive ``open(..., "x")`` so concurrent launches cannot
    accidentally reuse the same path.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = 0
    while True:
        name = f"dev-{stamp}.log" if suffix == 0 else f"dev-{stamp}-{suffix}.log"
        candidate = log_dir / name
        try:
            with open(candidate, "x", encoding="utf-8"):
                pass
            return candidate
        except FileExistsError:
            suffix += 1


def trim_launcher_logs(
    log_dir: Path = LOG_DIR,
    keep: int = LAUNCHER_LOG_RETENTION,
    current: Path | None = None,
) -> None:
    """Prune historical launcher logs so the directory never grows unbounded.

    The ``current`` log (the active launch) is always preserved; at most
    ``keep - 1`` older logs are kept.
    """
    if keep <= 0 or not log_dir.is_dir():
        return
    candidates: list[tuple[float, Path]] = []
    for path in log_dir.glob("dev-*.log"):
        if path == current:
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except FileNotFoundError:
            continue  # Another launcher may have just pruned this file.
    candidates.sort(key=lambda item: item[0], reverse=True)
    historical_keep = max(0, keep - (1 if current is not None else 0))
    for _, path in candidates[historical_keep:]:
        with suppress(OSError):
            path.unlink()


def parse_args() -> argparse.Namespace:
    dotenv_host = read_dotenv_value(ENV_FILE, "HOST")
    dotenv_port = read_dotenv_value(ENV_FILE, "PORT")
    default_host = first_value(os.getenv("HOST"), dotenv_host, default="0.0.0.0")
    default_backend_port = first_value(
        os.getenv("BACKEND_PORT"), os.getenv("PORT"), dotenv_port, default="3018"
    )
    default_frontend_port = first_value(os.getenv("FRONTEND_PORT"), default="3011")

    parser = argparse.ArgumentParser(
        description="Start FastAPI and Vite together with hot reload enabled."
    )
    parser.add_argument("--host", default=default_host, help="bind host (default: %(default)s)")
    parser.add_argument(
        "--backend-port",
        type=positive_port,
        default=positive_port(default_backend_port),
        help="FastAPI port (default: %(default)s)",
    )
    parser.add_argument(
        "--frontend-port",
        type=positive_port,
        default=positive_port(default_frontend_port),
        help="Vite port (default: %(default)s)",
    )
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help="disable backend reload (Vite HMR remains enabled)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate dependencies and ports without starting services",
    )
    return parser.parse_args()


def resolve_commands(args: argparse.Namespace) -> tuple[list[str], list[str], dict[str, str]]:
    python = backend_python()
    if not python.is_file():
        raise RuntimeError(
            f"backend virtual environment not found: {python}\n"
            "Run 'uv sync --frozen' in the backend directory first."
        )

    vite = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite.is_file():
        raise RuntimeError(
            f"frontend dependencies not found: {vite}\n"
            "Run 'pnpm install' in the frontend directory first."
        )

    node_candidates = [
        os.environ.get("DEV_NODE"),
        shutil.which("node"),
    ]
    node = next(
        (candidate for candidate in node_candidates if candidate and Path(candidate).is_file()),
        None,
    )
    if not node:
        raise RuntimeError("node was not found; install Node.js or set DEV_NODE")

    backend_command = [
        str(python),
        "-m",
        "uvicorn",
        "app.main:app",
    ]
    if ENV_FILE.is_file():
        backend_command.extend(["--env-file", str(ENV_FILE)])
    if not args.no_reload:
        backend_command.append("--reload")
        # 只监视 app/ 包。uvicorn 默认递归监视整个 cwd (backend/), 09-16 实证:
        # 在 backend/ 根目录创建/删除临时探针脚本也会触发 reload, 启动器把
        # 整棵服务树停掉且不拉回 (09-15 17:52 与 09-16 09:50 两次同型事故)。
        backend_command.extend(["--reload-dir", "app"])
    backend_command.extend(["--host", args.host, "--port", str(args.backend_port)])

    frontend_command = [
        node,
        str(vite),
        "--host",
        args.host,
        "--port",
        str(args.frontend_port),
        "--strictPort",
    ]
    frontend_env = os.environ.copy()
    frontend_env["BACKEND_HOST"] = args.host
    frontend_env["BACKEND_PORT"] = str(args.backend_port)
    return backend_command, frontend_command, frontend_env


def display_host(host: str) -> str:
    return "localhost" if host in {"0.0.0.0", "::"} else host


def stream_output(label: str, stream: TextIO, log_path: Path | None = None) -> None:
    """Mirror a child process's stdout to the console and an append-only log file.

    Each line is line-buffered and flushed immediately so both the user and any
    crash investigator (humans or Agents) see the same content at the same time.
    """
    fh = open_launcher_log(log_path)
    try:
        for line in iter(stream.readline, ""):
            line = line.rstrip()
            if not write_line(fh, f"[{label:<8}] {line}"):
                close_launcher_log(fh)
                fh = None
    finally:
        close_launcher_log(fh)
        with suppress(OSError):
            stream.close()


def process_options() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def start_process(
    label: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
) -> tuple[subprocess.Popen[str], threading.Thread]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **process_options(),
    )
    assert process.stdout is not None
    thread = threading.Thread(
        target=stream_output,
        args=(label, process.stdout, log_path),
        name=f"{label}-log",
        daemon=True,
    )
    thread.start()
    return process, thread


def report_log_error(error: Exception) -> None:
    """Logging is optional; even an unavailable stderr must not stop cleanup."""
    with suppress(OSError, ValueError):
        print(f"[dev     ] WARNING: launcher log unavailable: {error}", file=sys.stderr, flush=True)


def open_launcher_log(log_path: Path | None) -> TextIO | None:
    """Return an optional handle; the caller must use close_launcher_log in finally."""
    if log_path is None:
        return None
    try:
        return open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        report_log_error(exc)
        return None


def write_launcher_log(log_fh: TextIO | None, text: str) -> bool:
    """Write and flush without making file I/O part of the service lifecycle."""
    if log_fh is None:
        return True
    try:
        log_fh.write(text)
        log_fh.flush()
        return True
    except (OSError, ValueError) as exc:
        report_log_error(exc)
        return False


def close_launcher_log(log_fh: TextIO | None) -> None:
    """Attempt close even after a failed flush, without masking the exit status."""
    if log_fh is not None:
        try:
            log_fh.close()
        except (OSError, ValueError) as exc:
            report_log_error(exc)


def write_line(log_fh: TextIO | None, line: str, *, file: TextIO | None = None) -> bool:
    """Print a line and mirror it to the log; return whether the mirror succeeded."""
    with suppress(OSError, ValueError):
        print(line, file=file, flush=True)
    return write_launcher_log(log_fh, line + "\n")


def _run_launcher(args: argparse.Namespace, log_path: Path | None, log_fh: TextIO | None) -> int:
    try:
        backend_command, frontend_command, frontend_env = resolve_commands(args)
        check_port_available(args.host, args.backend_port, "backend")
        check_port_available(args.host, args.frontend_port, "frontend")
    except RuntimeError as exc:
        write_line(log_fh, f"[dev     ] ERROR: {exc}", file=sys.stderr)
        return 1

    host = display_host(args.host)
    banner_lines = [
        f"[dev     ] backend:  http://{host}:{args.backend_port}",
        f"[dev     ] frontend: http://{host}:{args.frontend_port}",
        f"[dev     ] backend reload: {'off' if args.no_reload else 'on'}",
        f"[dev     ] launcher log: {log_path if log_path is not None else 'unavailable (console only)'}",
    ]
    for line in banner_lines:
        write_line(log_fh, line)

    if args.check:
        write_line(log_fh, "[dev     ] checks passed")
        return 0
    write_line(log_fh, "[dev     ] Ctrl+C stops both services")

    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    processes: list[subprocess.Popen[str]] = []
    threads: list[threading.Thread] = []
    exit_code = 0
    try:
        backend, backend_thread = start_process(
            "backend", backend_command, BACKEND_DIR, log_path=log_path
        )
        processes.append(backend)
        threads.append(backend_thread)
        frontend, frontend_thread = start_process(
            "frontend", frontend_command, FRONTEND_DIR, frontend_env, log_path=log_path
        )
        processes.append(frontend)
        threads.append(frontend_thread)

        while not stopping.is_set():
            for label, process in zip(("backend", "frontend"), processes, strict=True):
                code = process.poll()
                if code is not None:
                    line = f"[dev     ] {label} exited with code {code}; stopping both"
                    write_line(log_fh, line)
                    exit_code = code or 1
                    stopping.set()
                    break
            stopping.wait(0.2)
    except KeyboardInterrupt:
        write_line(log_fh, "[dev     ] KeyboardInterrupt -> stopping")
        stopping.set()
    except BaseException as exc:  # noqa: BLE001
        # 09-16/09-17 两次事故: watchfiles reload 后整树退出, 但日志无
        # "exited with code" 与 KeyboardInterrupt 记录 —— 监控循环外有异常路径
        # 直接落入 finally。此处强制取证: 类型+repr+traceback 全部落日志。
        import traceback
        write_line(log_fh, f"[dev     ] UNEXPECTED {type(exc).__name__}: {exc!r}")
        for tb_line in traceback.format_exc().splitlines():
            write_line(log_fh, f"[dev     ] {tb_line}")
        exit_code = 1
        stopping.set()
    finally:
        write_line(log_fh, "[dev     ] stopping services...")
        for label, process in zip(("backend", "frontend"), processes, strict=True):
            # 取证: finally 时两个子进程的存活状态 (reload 停树事故定位关键)
            with suppress(Exception):
                write_line(log_fh, f"[dev     ] {label} alive={process.poll() is None} rc={process.poll()}")
        for process in reversed(processes):
            stop_process_tree(process)
        deadline = time.monotonic() + 2
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        write_launcher_log(
            log_fh,
            f"# dev launcher stopped @ {time.strftime('%Y-%m-%dT%H:%M:%S')} exit_code={exit_code}\n"
        )
        write_line(None, "[dev     ] stopped")

    return exit_code


def main() -> int:
    args = parse_args()
    log_path = None
    log_fh = None
    try:
        try:
            log_path = make_launcher_log(LOG_DIR)
        except OSError as exc:
            report_log_error(exc)
        log_fh = open_launcher_log(log_path)
        write_launcher_log(
            log_fh,
            f"# tick-stock-panel dev launcher @ {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
            f"# argv={sys.argv!r} host={args.host} "
            f"backend_port={args.backend_port} frontend_port={args.frontend_port} "
            f"reload={not args.no_reload} check={args.check}\n\n",
        )
        return _run_launcher(args, log_path, log_fh)
    finally:
        close_launcher_log(log_fh)
        if log_path is not None:
            try:
                trim_launcher_logs(log_dir=log_path.parent, keep=LAUNCHER_LOG_RETENTION, current=log_path)
            except OSError as exc:
                report_log_error(exc)
            if log_fh is not None:
                write_line(None, f"[dev     ] logs saved to: {log_path}")


if __name__ == "__main__":
    raise SystemExit(main())
