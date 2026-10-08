"""请求耗时中间件测试。

背景: uvicorn access log 不记处理耗时, 「同步期间 API 有没有变慢」没有数据
—— 只能证明吞吐没掉、零 5xx, 证明不了延迟没涨。中间件负责把每个请求的
处理耗时落日志 + 回写 X-Process-Time, 跑一段时间后可按固定格式聚合出
P50/P95, 用真实延迟数据决定要不要做专门优化。

重点验证的三件事:
  1. 响应头 X-Process-Time 存在且是合法毫秒数;
  2. 日志是固定 key=value 格式, 便于 grep / awk 聚合;
  3. 异常路径同样留下耗时记录 —— 否则最慢的那批请求会在统计里凭空消失,
     把 P95 系统性压低。
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.main import app as main_app
from app.main import process_time_middleware


@pytest.fixture()
def probe_app() -> FastAPI:
    """最小复现: 只挂耗时中间件 + 一个正常路由 + 一个抛异常路由。

    不直接打主 app 的业务路由, 免得测试被认证/数据依赖绑死; 中间件本身
    与主 app 是同一个函数对象, 行为一致。
    """
    probe = FastAPI()
    # 与 main.py 同序: 先注册 = 最外层, 认证/路由耗时一并计入
    probe.middleware("http")(process_time_middleware)

    @probe.get("/ok")
    def _ok() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @probe.get("/boom")
    def _boom() -> PlainTextResponse:
        raise RuntimeError("intentional")

    return probe


class TestProcessTimeHeader:
    def test_header_present_and_parseable(self, probe_app: FastAPI) -> None:
        with TestClient(probe_app, raise_server_exceptions=False) as client:
            resp = client.get("/ok")
        assert resp.status_code == 200
        raw = resp.headers.get("X-Process-Time")
        assert raw is not None, "缺少 X-Process-Time 响应头"
        # 必须是可 float() 解析的毫秒数, 且非负
        value = float(raw)
        assert value >= 0.0
        # 格式化为一位小数 (便于肉眼扫, 也避免超长浮点噪声)
        assert len(raw.split(".")[-1]) == 1

    def test_header_grows_with_slow_route(self, probe_app: FastAPI) -> None:
        """慢路由的耗时应显著大于快路由 —— 证明测的是真实处理耗时。"""
        import time

        @probe_app.get("/slow")
        def _slow() -> PlainTextResponse:
            time.sleep(0.15)
            return PlainTextResponse("slow")

        with TestClient(probe_app, raise_server_exceptions=False) as client:
            fast_ms = float(client.get("/ok").headers["X-Process-Time"])
            slow_ms = float(client.get("/slow").headers["X-Process-Time"])
        assert slow_ms >= 150.0
        assert slow_ms > fast_ms


class TestProcessTimeLog:
    def test_logs_fixed_key_value_format(self, probe_app: FastAPI, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="app.main"):
            with TestClient(probe_app, raise_server_exceptions=False) as client:
                client.get("/ok")
        line = next(
            (rec.getMessage() for rec in caplog.records if rec.getMessage().startswith("REQ ")),
            None,
        )
        assert line is not None, "没有落 REQ 日志"
        # 固定格式: REQ method=... path=... status=... dur_ms=...
        assert line.startswith("REQ method=GET path=/ok status=200 dur_ms=")
        dur = float(line.rsplit("dur_ms=", 1)[1])
        assert dur >= 0.0

    def test_exception_path_still_logged(self, probe_app: FastAPI, caplog) -> None:
        """异常路径必须留耗时证据, 否则 P95 会被系统性压低。"""
        with caplog.at_level(logging.INFO, logger="app.main"):
            with TestClient(probe_app, raise_server_exceptions=False) as client:
                client.get("/boom")
        line = next(
            (
                rec.getMessage()
                for rec in caplog.records
                if rec.getMessage().startswith("REQ ") and "path=/boom" in rec.getMessage()
            ),
            None,
        )
        assert line is not None, "异常请求没有落 REQ 日志"
        assert "status=exception" in line
        assert float(line.rsplit("dur_ms=", 1)[1]) >= 0.0


class TestMiddlewareWiredIntoMainApp:
    def test_registered_on_main_app(self) -> None:
        """主 app 必须真的挂上了 —— 否则前面测的只是孤儿函数。

        Starlette 把 @app.middleware("http") 存成 Middleware(cls=BaseHTTPMiddleware,
        kwargs={"dispatch": fn}), 所以按 kwargs 里的 dispatch 反查最可靠。
        """
        dispatch_names = set()
        for m in main_app.user_middleware:
            fn = getattr(m, "kwargs", {}).get("dispatch")
            if fn is not None:
                dispatch_names.add(getattr(fn, "__name__", None))
        assert "process_time_middleware" in dispatch_names

    def test_registered_outermost(self) -> None:
        """必须是最外层: 先注册 = 外层, 才能把认证中间件的耗时也计入。

        放在 CORSMiddleware 之后会退化成「只测路由耗时」, 系统性低估延迟,
        拿它做 P95 决策就会得出错误结论。
        """
        order = [
            getattr(getattr(m, "kwargs", {}).get("dispatch"), "__name__", None)
            or getattr(getattr(m, "cls", None), "__name__", None)
            for m in main_app.user_middleware
        ]
        assert order[0] == "process_time_middleware", f"中间件顺序不符预期: {order[:3]}"

    def test_health_carries_header(self) -> None:
        """/health 探活同时验证中间件在真实 app 上生效。"""
        with TestClient(main_app, raise_server_exceptions=False) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert float(resp.headers["X-Process-Time"]) >= 0.0
