"""港股深度数据 API —— 公告(港交所披露易) + 财报(东财公告原文解析)。

- GET /api/hk/announcements/{symbol}: 个股公告/公司行动(披露易, 全类别)
- GET /api/hk/financials/{symbol}:   财务比率(东财公告原文, 逐字段带溯源)
- GET /api/hk/forecasts/{symbol}:    券商盈利预测/评级/目标价(东财, 数十家券商)

项目惯例(与 astock_deep.py / us_deep.py 一致): 不可用是正常状态不是错误 →
失败也返回 HTTP 200 且 ``{available: false, reason: ...}``, 绝不返回占位值
伪装成功(fail-closed)。

⚠️ 与 astock_deep 相同的一条刻意设计: **「窗口内没发过公告」是真实答案**,
此时 ``available`` 仍为 ``true``、``rows`` 为空数组, 前端显示"窗口内无公告"。
把空 rows 当 no_data, 等于把"这家公司这段时间很安静"谎报成"取不到数"。

⚠️ 而**财报解析出 0 行是失败不是答案**: 那意味着该公司公告版式不在解析
规则里(实测 00011.HK 如此), 如实 ``available:false``, 不用别处数字顶上。

命名: 挂在 ``/api/hk`` 下与 hk.py 的行情端点同域; 与 ``hk_us_deep.py`` 的
``/api/hk/deep/*``(K线能力)不冲突。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.data_providers.hk_deep_provider import (
    SOURCE_EM_HK_ANN,
    SOURCE_HK_FORECAST,
    SOURCE_HKEX,
    HKNewsNotFoundError,
    analyst_forecasts,
    announcements,
    financials,
)
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hk", tags=["hk-deep"])


def _respond(symbol: str, call, *, required_key: str, source: str) -> dict:
    """统一响应: 成功过闸门 → available:true; 异常/被闸门拦 → available:false。

    required_key 用来在返回前确认 provider 没把结构改坏 —— 空数组是合法答案,
    缺键才是结构坏了。
    """
    try:
        payload = call()
    except HKNewsNotFoundError as exc:
        # 确定性无数据(退市/查不到 stockId), 原因要写给前端看, 不当成网络错误
        logger.info("hkex symbol not found (%s): %s", symbol, exc)
        return {"available": False, "reason": f"not_found: {exc}", "source": source}
    except Exception as exc:
        logger.warning("hk deep provider error (%s): %s", symbol, exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": source}

    if not is_real(payload):
        logger.error("hk deep blocked by source_gate: %s source=%r", symbol, payload.get("source"))
        return {"available": False,
                "reason": f"source_gate_rejected: {payload.get('source')!r}",
                "source": source}

    data = payload.get("data")
    if not isinstance(data, dict) or required_key not in data:
        return {"available": False, "reason": "bad_payload: provider 返回结构异常", "source": source}

    return {"available": True, "source": payload["source"], "symbol": data["symbol"], "data": data}


@router.get("/announcements/{symbol}")
def get_announcements(symbol: str, days: int = 180, limit: int = 50) -> dict:
    """个股公告 / 公司行动(港交所披露易)。days 1..730 默认 180; limit 1..200 默认 50。

    ⚠️ 披露易只认内部 stockId, **不认股票代码** —— provider 内部先查
    prefix.do 拿 stockId(缓存 24h)。查不到(退市, 如 00011)直接 available:false,
    不返回空列表冒充"这家公司没发过公告"。

    ⚠️ 取**全类别**(t1code=-2): 只筛财报会把回购日报、股东大会结果、
    月报表这些真正影响判断的公司行动漏掉。
    """
    return _respond(symbol, lambda: announcements(symbol, days=days, limit=limit),
                    required_key="rows", source=SOURCE_HKEX)


@router.get("/financials/{symbol}")
def get_financials(symbol: str, periods: int = 8) -> dict:
    """港股财务比率(东财公告原文解析)。periods 1..20, 默认 8。

    与 A股 ``/api/astock/financials``(akshare 80 指标)不是同一口径:
    这里只有 8 个字段但**每个都带原始财报行 + 公式 + 公告URL + content_hash**,
    可以逐条回溯到公告原文。港股没有 akshare 那种现成聚合表, 代价就是字段少。

    ⚠️ 单次抓取 2.3~14.8s(要翻公告列表再逐份取正文), 已做 6h 缓存;
       首次访问该标的会有一次明显等待, 属预期行为。
    """
    return _respond(symbol, lambda: financials(symbol, periods=periods),
                    required_key="rows", source=SOURCE_EM_HK_ANN)


@router.get("/forecasts/{symbol}")
def get_forecasts(symbol: str, limit: int = 60) -> dict:
    """券商盈利预测 / 评级 / 目标价(东财港股通口径)。limit 1..200, 默认 60。

    港股侧对标 A股「研报」位的数据 —— 数十家券商对同一财政年度的纯利、EPS、
    派息预测, 外加各自的**评级与目标价**, ``summary`` 里给目标价高/低/均值/
    中位数与评级分布(全部由真实券商观点聚合, 不合成不预测)。

    单位口径见返回体 ``note``: 纯利原始=百万港元(已换算亿港元), 每股原始=
    港仙(已换算港元), 目标价=港元; ``raw_*`` 保留未换算原值供核对。

    ⚠️ 空 rows **不是**成功: 该页对未覆盖的标的会返回 0 行, 这里如实
    available:false —— 与公告不同(公告的"窗口内安静"是真答案)。
    """
    if limit < 1 or limit > 200:
        return {"available": False, "reason": f"bad_limit: {limit} (1..200)",
                "source": SOURCE_HK_FORECAST}
    return _respond(symbol, lambda: analyst_forecasts(symbol, limit=limit),
                    required_key="rows", source=SOURCE_HK_FORECAST)
