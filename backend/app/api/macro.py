"""中国宏观 8 指标 API (akshare)。

- GET /api/cn/macro: CPI / PPI / PMI / GDP / LPR / M2 / 社融 / 10年国债收益率

项目惯例(与 astock_deep.py 一致): 不可用是正常状态不是错误 → 失败/未开启/
缓存未热一律 **HTTP 200** + ``{available: false, reason: ...}``, 绝不返回占位值
伪装成功(fail-closed)。

⚠️ 与"取不到就编一个数"的差别就在这里: 前端拿到的每个 ``value`` 要么来自
akshare 的真实最新行, 要么是 ``None``(配 ``status: "unavailable"``), 没有
第三种可能。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.config import settings
from app.data_providers.cn_macro_provider import SOURCE, macro_brief
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cn", tags=["cn-macro"])

_DISCLAIMER = "以上基于公开宏观数据整理，仅供学习与跟踪参考，不构成投资建议。"


@router.get("/macro")
def get_macro() -> dict:
    """中国宏观 8 指标 + 小白解读。

    ``cn_macro_enabled=false``(默认)时直接短路 —— 本端点依赖 akshare
    (``BACKEND_EXTRAS=multi-market``), 未确认依赖就位前不上线。

    首次访问返回 ``reason: "warming"``: 8 个接口串行冷启动要 10~25s,
    后端不让它阻塞首屏, 后台预热完的下一个请求即为真数据。
    """
    if not settings.cn_macro_enabled:
        return {
            "available": False,
            "source": SOURCE,
            "reason": "cn_macro_disabled: 需设置 CN_MACRO_ENABLED=true 且安装 akshare",
            "live_count": 0,
            "total": 0,
            "items": [],
            "disclaimer": _DISCLAIMER,
        }

    try:
        payload = macro_brief()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("cn macro provider error: %s", exc)
        return {
            "available": False,
            "source": SOURCE,
            "reason": f"provider_error: {exc}",
            "live_count": 0,
            "total": 0,
            "items": [],
            "disclaimer": _DISCLAIMER,
        }

    if not is_real(payload):
        # 防御性闸门: provider 恒带真实 source, 一旦被改坏在这里拦下
        logger.error("cn macro blocked by source_gate: source=%r", payload.get("source"))
        return {
            "available": False,
            "source": SOURCE,
            "reason": f"source_gate_rejected: {payload.get('source')!r}",
            "live_count": 0,
            "total": 0,
            "items": [],
            "disclaimer": _DISCLAIMER,
        }

    out = dict(payload)
    out["disclaimer"] = _DISCLAIMER
    return out
