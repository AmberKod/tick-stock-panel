"""港美股「深度 K 线」端点 —— 同一份逻辑挂两个市场。

为什么一个文件里两个 router: 港美股这些能力的**差异只在数据源支撑与否**
(详见 provider 模块头的实测表), 端点和响应契约完全一致。写两份必然漂移 ——
与前端 KlineChartTools 抽共用层是同一个道理。

- /api/hk/deep/*  →  港股
- /api/us/deep/*  →  美股(与既有 OpenBB 的 us_deep 同前缀不同路径, 无冲突)

端点:
- GET  /capabilities                能力矩阵: 哪些可用、不可用的原因
- GET  /period/{symbol}?period=week 周期K(week/month/quarter/year)
- GET  /index/{code}?limit=120      指数日K(HSI/HSCEI/DJI/IXIC/INX)
- GET  /minute/{symbol}             分钟K —— 恒不可用, 带原因
- GET  /adjratio/{symbol}           复权系数 —— 恒不可用, 带原因

契约: 成功 HTTP 200 + available:true; 拿不到**也是 HTTP 200** + available:false
+ reason(fail-closed, 不抛 5xx)。derived_from 非空表示该串是推导值, 前端须标口径。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.data_providers import hk_us_deep_provider as prov
from app.data_providers.hk_us_deep_provider import CapabilityUnavailableError
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router_hk = APIRouter(prefix="/api/hk/deep", tags=["hk-deep"])
router_us = APIRouter(prefix="/api/us/deep", tags=["us-deep"])

_REASONS = {
    "no_symbol": lambda s: f"symbol 需带 .HK/.US 后缀: {s!r}",
}


def _respond(call, *, required_key: str, source_hint: str) -> dict:
    """统一响应: 成功 → available:true; 取数失败 / 无真源 → available:false + reason。"""
    try:
        payload = call()
    except CapabilityUnavailableError as exc:
        # 不是故障, 是"该能力没有真源" —— UI 应置灰并展示原因, 而不是重试
        return {"available": False, "reason": f"capability_unavailable: {exc}", "source": None}
    except Exception as exc:
        logger.warning("hk/us deep provider error: %s", exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": source_hint}

    if not is_real(payload):
        logger.error("hk/us deep blocked by source_gate: source=%r", payload.get("source"))
        return {"available": False,
                "reason": f"source_gate_rejected: {payload.get('source')!r}",
                "source": source_hint}
    if required_key not in payload:
        return {"available": False, "reason": f"bad_payload: 缺 {required_key}", "source": source_hint}

    return {"available": True, **payload}


def _attach(router: APIRouter, market: str) -> None:
    source_hint = "tencent" if market == "HK" else "yfinance"

    @router.get("/capabilities")
    def _capabilities() -> dict:
        """能力矩阵 —— 元数据, 不走 source_gate(它不是行情数据)。"""
        return prov.capabilities(market)

    @router.get("/period/{symbol}")
    def _period(symbol: str, period: str = "week", limit: int = 120) -> dict:
        """周期 K。week/month/quarter/year, limit 10..1000。"""
        if limit < 10 or limit > 1000:
            return {"available": False, "reason": f"bad_limit: {limit} (10..1000)", "source": source_hint}
        return _respond(lambda: prov.period_bars(symbol, period=period, limit=limit),
                        required_key="rows", source_hint=source_hint)

    @router.get("/index/{code}")
    def _index(code: str, limit: int = 120) -> dict:
        """指数日 K。code ∈ {HSI, HSCEI, DJI, IXIC, INX}。"""
        if limit < 10 or limit > 1000:
            return {"available": False, "reason": f"bad_limit: {limit} (10..1000)", "source": source_hint}
        return _respond(lambda: prov.index_bars(code, limit=limit),
                        required_key="rows", source_hint=source_hint)

    @router.get("/minute/{symbol}")
    def _minute(symbol: str, period: str = "m30", limit: int = 320) -> dict:
        """分钟 K —— 港美股均无真源, 恒返回 available:false + 原因。"""
        return _respond(lambda: prov.minute_bars(symbol, period=period, limit=limit),
                        required_key="rows", source_hint=source_hint)

    @router.get("/adjratio/{symbol}")
    def _adjratio(symbol: str, mode: str = "qfq", limit: int = 640) -> dict:
        """复权系数 —— 港美股均无真源, 恒返回 available:false + 原因。"""
        return _respond(lambda: prov.adjust_ratios(symbol, mode=mode, limit=limit),
                        required_key="ratios", source_hint=source_hint)


_attach(router_hk, "HK")
_attach(router_us, "US")
