from __future__ import annotations

import argparse
import importlib.util
import io
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_launcher", ROOT / "dev.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    return load_launcher()


def test_read_dotenv_value_handles_quotes_comments_and_exact_keys(
    launcher: ModuleType, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PORT_BACKUP=9999\n"
        "PORT=\"3018\" # backend port\n"
        "HOST='127.0.0.1'\n",
        encoding="utf-8",
    )

    assert launcher.read_dotenv_value(env_file, "PORT") == "3018"
    assert launcher.read_dotenv_value(env_file, "HOST") == "127.0.0.1"
    assert launcher.read_dotenv_value(env_file, "MISSING") is None


@pytest.mark.parametrize("value", ["0", "65536", "abc"])
def test_positive_port_rejects_invalid_values(launcher: ModuleType, value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        launcher.positive_port(value)


def test_check_port_available_rejects_listener(launcher: ModuleType) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        with pytest.raises(RuntimeError, match=f"port {port} is already in use"):
            launcher.check_port_available("127.0.0.1", port, "test")


def test_resolve_commands_enables_reload_and_frontend_proxy(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = SimpleNamespace(
        host="0.0.0.0",
        backend_port=8018,
        frontend_port=8011,
        no_reload=False,
    )
    monkeypatch.setenv("DEV_NODE", str(Path(launcher.sys.executable)))

    backend_command, frontend_command, frontend_env = launcher.resolve_commands(args)

    assert backend_command[:4] == [
        str(launcher.backend_python()),
        "-m",
        "uvicorn",
        "app.main:app",
    ]
    assert "--reload" in backend_command
    # 09-16 事故回归: reload 必须只盯 app/, 否则 backend/ 根目录的临时文件
    # 变动 (探针脚本创建/删除) 也会触发 reload 并停掉整棵服务树。
    reload_dir_at = backend_command.index("--reload-dir")
    assert backend_command[reload_dir_at + 1] == "app"
    assert backend_command[-4:] == ["--host", "0.0.0.0", "--port", "8018"]
    assert frontend_command[-5:] == ["--host", "0.0.0.0", "--port", "8011", "--strictPort"]
    assert frontend_env["BACKEND_HOST"] == "0.0.0.0"
    assert frontend_env["BACKEND_PORT"] == "8018"


class FaultyLog:
    """Inject a single file-operation failure without touching a real log."""

    def __init__(self, operation: str, *, enabled: bool = True) -> None:
        self.operation = operation
        self.enabled = enabled
        self.close_attempted = False
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        if self.enabled and self.operation == "write":
            raise OSError("injected log write failure")
        self.lines.append(text)
        return len(text)

    def flush(self) -> None:
        if self.enabled and self.operation == "flush":
            raise OSError("injected log flush failure")

    def close(self) -> None:
        self.close_attempted = True
        if self.enabled and self.operation == "close":
            raise OSError("injected log close failure")


def test_make_launcher_log_is_exclusive_for_same_second(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    stamp = "20260913-170600"
    monkeypatch.setattr(launcher, "time", SimpleNamespace(strftime=lambda _: stamp))
    existing = tmp_path / f"dev-{stamp}.log"
    existing.write_text("existing sentinel\n", encoding="utf-8")
    workers = 16
    barrier = threading.Barrier(workers)

    def allocate(index: int) -> tuple[Path, str]:
        barrier.wait(timeout=10)
        path = launcher.make_launcher_log(tmp_path)
        content = f"worker={index}\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)
        return path, content

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(allocate, range(workers)))

    assert len({path for path, _ in results}) == workers
    assert existing.read_text(encoding="utf-8") == "existing sentinel\n"
    assert all(path.read_text(encoding="utf-8") == content for path, content in results)


@pytest.mark.parametrize("operation", ["open", "write", "flush", "close"])
def test_stream_output_keeps_draining_when_log_fails(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], tmp_path: Path, operation: str,
) -> None:
    log = FaultyLog(operation)

    def open_log(*_args, **_kwargs):
        if operation == "open":
            raise PermissionError("injected log open failure")
        return log

    monkeypatch.setattr(launcher, "open", open_log, raising=False)
    stream = io.StringIO("first\nsecond 中文\n")
    launcher.stream_output("backend", stream, tmp_path / "unused.log")

    output = capsys.readouterr()
    assert output.out == "[backend ] first\n[backend ] second 中文\n"
    assert stream.closed
    if operation != "open":
        assert log.close_attempted


def test_stream_output_appends_and_mirrors_console(
    launcher: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    path = tmp_path / "output.log"
    path.write_text("existing\n", encoding="utf-8")
    stream = io.StringIO("first\nsecond 中文\n")

    launcher.stream_output("backend", stream, path)

    expected = "[backend ] first\n[backend ] second 中文\n"
    assert capsys.readouterr().out == expected
    assert path.read_text(encoding="utf-8") == "existing\n" + expected
    assert stream.closed


def prepare_fake_launcher(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    *, check: bool = False,
) -> tuple[Mock, Mock]:
    args = SimpleNamespace(
        host="127.0.0.1", backend_port=8018, frontend_port=8011,
        no_reload=False, check=check,
    )
    monkeypatch.setattr(launcher, "parse_args", lambda: args)
    monkeypatch.setattr(launcher, "resolve_commands", lambda _: (["fake-backend"], ["fake-frontend"], {}))
    monkeypatch.setattr(launcher, "check_port_available", lambda *_args: None)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(launcher, "LOG_DIR", tmp_path)
    real_make = launcher.make_launcher_log
    monkeypatch.setattr(launcher, "make_launcher_log", lambda *_args, **_kwargs: real_make(tmp_path))
    real_trim = launcher.trim_launcher_logs
    monkeypatch.setattr(
        launcher, "trim_launcher_logs",
        lambda *_args, **kwargs: real_trim(log_dir=tmp_path, keep=2, current=kwargs.get("current")),
    )
    start = Mock(side_effect=AssertionError("test must not start a real process"))
    stop = Mock(side_effect=AssertionError("test must not stop a real process"))
    monkeypatch.setattr(launcher, "start_process", start)
    monkeypatch.setattr(launcher, "stop_process_tree", stop)
    return start, stop


@pytest.mark.parametrize("operation", ["write", "flush", "close", "closed_console"])
def test_shutdown_closes_all_resources_when_log_fails(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str,
) -> None:
    start, stop = prepare_fake_launcher(launcher, monkeypatch, tmp_path)
    log_path = tmp_path / "dev-current.log"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(launcher, "make_launcher_log", lambda *_args, **_kwargs: log_path)
    log = FaultyLog("write" if operation == "closed_console" else operation, enabled=False)
    monkeypatch.setattr(launcher, "open", lambda *_args, **_kwargs: log, raising=False)
    backend, frontend = Mock(), Mock()
    backend_thread, frontend_thread = Mock(), Mock()

    def backend_exited() -> int:
        log.enabled = True
        if operation == "closed_console":
            monkeypatch.setattr(launcher, "print", Mock(side_effect=ValueError("I/O on closed stream")), raising=False)
        return 7

    backend.poll.side_effect = backend_exited
    start.side_effect = [(backend, backend_thread), (frontend, frontend_thread)]
    stop.side_effect = None

    assert launcher.main() == 7

    assert stop.call_args_list == [((frontend,),), ((backend,),)]
    backend_thread.join.assert_called_once()
    frontend_thread.join.assert_called_once()
    assert log.close_attempted


@pytest.mark.parametrize("failure", ["create", "open"])
def test_launcher_can_run_with_console_only_when_log_unavailable(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    start, stop = prepare_fake_launcher(launcher, monkeypatch, tmp_path)
    if failure == "create":
        monkeypatch.setattr(launcher, "make_launcher_log", Mock(side_effect=PermissionError("unwritable logs")))
    else:
        path = tmp_path / "dev-current.log"
        path.write_text("", encoding="utf-8")
        monkeypatch.setattr(launcher, "make_launcher_log", lambda *_args, **_kwargs: path)
        monkeypatch.setattr(launcher, "open", Mock(side_effect=PermissionError("cannot open log")), raising=False)
    backend, frontend = Mock(), Mock()
    backend.poll.return_value = 3
    threads = [Mock(), Mock()]
    start.side_effect = [(backend, threads[0]), (frontend, threads[1])]
    stop.side_effect = None

    assert launcher.main() == 3
    assert stop.call_args_list == [((frontend,),), ((backend,),)]
    assert all(thread.join.call_count == 1 for thread in threads)


@pytest.mark.parametrize("mode", ["check", "dependency_error"])
def test_early_return_trims_only_isolated_launcher_logs(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str,
) -> None:
    start, stop = prepare_fake_launcher(launcher, monkeypatch, tmp_path, check=mode == "check")
    for index in range(4):
        path = tmp_path / f"dev-old-{index}.log"
        path.write_text(f"old {index}", encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    unrelated = tmp_path / "unrelated.log"
    unrelated.write_text("preserve", encoding="utf-8")
    if mode == "dependency_error":
        monkeypatch.setattr(launcher, "resolve_commands", Mock(side_effect=RuntimeError("missing dependency")))

    assert launcher.main() == (0 if mode == "check" else 1)

    assert len(list(tmp_path.glob("dev-*.log"))) == 2
    assert unrelated.read_text(encoding="utf-8") == "preserve"
    start.assert_not_called()
    stop.assert_not_called()


def test_retention_failure_does_not_prevent_log_close(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    prepare_fake_launcher(launcher, monkeypatch, tmp_path, check=True)
    log = FaultyLog("none")
    monkeypatch.setattr(launcher, "make_launcher_log", lambda *_args, **_kwargs: tmp_path / "dev-current.log")
    monkeypatch.setattr(launcher, "open", lambda *_args, **_kwargs: log, raising=False)
    monkeypatch.setattr(launcher, "trim_launcher_logs", Mock(side_effect=OSError("log stat failed")))

    assert launcher.main() == 0
    assert log.close_attempted
    launcher.trim_launcher_logs.assert_called_once()
