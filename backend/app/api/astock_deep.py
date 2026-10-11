"""A股深度数据 API — 东方财富 datacenter (九章融合 P2-2)。

- GET /api/astock/dragon/{code}: 龙虎榜(上榜记录 + 买卖席位 TOP5 + 机构/北向动向)
- GET /api/astock/research/{code}: 个股研报(东财 reportapi: 机构/评级/目标价/EPS 预测)
- GET /api/astock/announcements/{code}: 个股公告(巨潮 cninfo 全文检索)
- GET /api/astock/fundflow/{code}: 资金流向(新浪; 东财 push2/push2his 被墙)
- GET /api/astock/financials/{code}: 财务摘要(akshare 东财财务指标, 7 组 80 指标)
- GET /api/astock/quote/{code}: 实时行情(腾讯, 补齐成交额/换手/PE/PB/市值)
- GET /api/astock/minute/{code}: 当日分时(腾讯; 分时线/均价线/分时量)
- GET /api/astock/mkline/{code}: 分钟K(腾讯; 1/5/15/30/60 分)
- GET /api/astock/adjratio/{code}: 日K复权系数(腾讯; qfq/hfq/none) —— 只给系数,
  由调用方乘到自己的价格列上(复权是价格缩放, 不是换数据源)
- GET /api/astock/sectors/{code}: 所属板块/题材(东财 F10, 带入选理由)
- GET /api/astock/index/{code}: 指数日K(腾讯) —— 对比页基准指数(沪深300/上证指数…
  本地 index 库不全, 走腾讯真源)
- GET /api/astock/ticks/{code}: 成交明细(腾讯) —— **每 3 秒一行的成交快照**,
  不是逐笔(逐笔需 Level-2)。尾部 N 行 + 该窗口的买卖净额汇总

项目惯例(与 us_deep.py / us_financials.py 一致): 不可用是正常状态不是错误 →
失败也返回 HTTP 200 且 ``{available: false, reason: ...}``, 绝不返回占位值
伪装成功(fail-closed)。

⚠️ 一处与 us_deep 刻意不同: **「窗口内没上过榜」是真实答案, 不是数据缺失**。
此时 ``available`` 仍为 ``true``、``records`` 为空数组, 由前端显示"近期无
上榜记录"。若把空 records 当 no_data, 等于把"这只票干净"谎报成"取不到数"。
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter

from app.data_providers.jiuzhang_astock_provider import (
    SOURCE,
    SOURCE_AKSHARE,
    SOURCE_CNINFO,
    SOURCE_EM_F10,
    SOURCE_SINA,
    SOURCE_TENCENT,
    adjust_ratios,
    announcements,
    dragon_tiger,
    financials,
    fund_flow,
    index_daily,
    minute_bars,
    minute_kline,
    realtime_quote,
    research_reports,
    stock_sectors,
    tick_trades,
)
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/astock", tags=["astock-deep"])

_CODE_RE = re.compile(r"\d{6}")


def _norm_code(raw: str) -> str | None:
    """从 '002317' / 'sz002317' / '002317.SZ' 里抽出 6 位数字代码。"""
    m = _CODE_RE.search(raw or "")
    return m.group(0) if m else None


def _respond(code: str, call, *, required_key: str, source: str = SOURCE) -> dict:
    """统一响应: 成功过闸门 → available:true; 异常/被闸门拦 → available:false。

    required_key: 各端点的数据主键(records/reports/announcements), 用来在返回
    前确认 provider 没把结构改坏 —— 空数组是合法答案, 缺键才是结构坏了。
    """
    try:
        payload = call()
    except Exception as exc:
        logger.warning("astock deep provider error (%s): %s", code, exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": source}

    if not is_real(payload):
        # 防御性闸门: provider 恒带真实 source, 一旦被改坏在这里拦下
        logger.error("astock deep blocked by source_gate: %s source=%r", code, payload.get("source"))
        return {"available": False,
                "reason": f"source_gate_rejected: {payload.get('source')!r}",
                "source": source}

    data = payload.get("data")
    if not isinstance(data, dict) or required_key not in data:
        return {"available": False, "reason": "bad_payload: provider 返回结构异常", "source": source}

    return {"available": True, "source": payload["source"], "code": code, "data": data}


@router.get("/dragon/{code}")
def get_dragon(code: str, days: int = 30) -> dict:
    """龙虎榜。days 为回溯窗口(自然日), 默认 30。

    records 为空表示窗口内该票未上榜 —— 这是真实结论, available 仍为 true。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码", "source": SOURCE}
    if days < 1 or days > 180:
        return {"available": False, "reason": f"bad_days: {days} (1..180)", "source": SOURCE}
    return _respond(norm, lambda: dragon_tiger(norm, days=days), required_key="records")


@router.get("/research/{code}")
def get_research(code: str, limit: int = 20) -> dict:
    """个股研报列表(东财 reportapi)。limit 1..100, 默认 20。

    reports 为空 = 该票近三年确实没有机构覆盖 —— 真实结论, available 仍为 true。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码", "source": SOURCE}
    if limit < 1 or limit > 100:
        return {"available": False, "reason": f"bad_limit: {limit} (1..100)", "source": SOURCE}
    return _respond(norm, lambda: research_reports(norm, limit=limit), required_key="reports")


@router.get("/announcements/{code}")
def get_announcements(code: str, limit: int = 20) -> dict:
    """个股公告列表(巨潮 cninfo)。limit 1..100, 默认 20。

    巨潮 orgId 不能由代码拼出, provider 内部走 topSearch 拿真 orgId(见 provider
    注释 —— 参考实现九章用 gssz0{code} 拼, 导致公告面板恒空)。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码", "source": SOURCE_CNINFO}
    if limit < 1 or limit > 100:
        return {"available": False, "reason": f"bad_limit: {limit} (1..100)", "source": SOURCE_CNINFO}
    return _respond(norm, lambda: announcements(norm, limit=limit),
                    required_key="announcements", source=SOURCE_CNINFO)


@router.get("/fundflow/{code}")
def get_fund_flow(code: str, days: int = 20) -> dict:
    """资金流向(新浪日线)。days 1..120, 默认 20。

    ⚠️ 九章走东财 push2, 我方被墙(连接级 RST)故换源; 新浪接口只提供主力净额
    (netamount)与超大单净额(r0_net), 没有大/中/小单分档 —— 缺的档位留空,
    不按其它数字推算。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码", "source": SOURCE_SINA}
    if days < 1 or days > 120:
        return {"available": False, "reason": f"bad_days: {days} (1..120)", "source": SOURCE_SINA}
    return _respond(norm, lambda: fund_flow(norm, days=days),
                    required_key="rows", source=SOURCE_SINA)


@router.get("/quote/{code}")
def get_quote(code: str) -> dict:
    """A股实时行情(腾讯 qt.gtimg.cn)。

    补齐行情条上东财 push2(被墙)拿不到的那一半: 成交额 / 换手率 / 振幅 /
    PE(TTM) / PB / 总市值 / 流通市值 / 涨停跌停。全部真源, 与九章同源。

    🔴 九章 ``tx_quote()`` 把腾讯 44/45 两个字段写反了(44 实为流通市值、
    45 实为总市值), 它的"总市值"显示的是流通市值。这里按实测口径修正。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: realtime_quote(norm),
                    required_key="price", source=SOURCE_TENCENT)


@router.get("/minute/{code}")
def get_minute(code: str) -> dict:
    """A股当日分时(腾讯 web.ifzq.gtimg.cn) —— 分时线 / 均价线 / 分时量。

    九章「分时」tab 的同源实现。与它的差别: 腾讯第 3/4 列是**全天累计**量额,
    这里按差分还原成每分钟量额(见 provider 注释), 均价线按 累计额/累计量 算。

    非交易日/尚未开盘时腾讯返回空数组 → 这里如实给 ``available:false``,
    不补零、不沿用上一交易日数据冒充"今天"。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: minute_bars(norm),
                    required_key="points", source=SOURCE_TENCENT)


@router.get("/mkline/{code}")
def get_minute_kline(code: str, period: str = "m5", limit: int = 320) -> dict:
    """A股分钟K(腾讯 mkline): 1分 / 5分 / 15分 / 30分 / 60分。

    本地 kline_minute 库对这些标的还是空的(补数要 Pro+ 权限), 但这个端点直连就有
    真数据 —— 不做"等库补齐再说"的空图。

    ⚠️ 行内量单位随标的不同(主板手 / 科创板股), 这里已统一成**股**;
       时间字段是该分钟区间的结束时刻, 前端直接用, 不要再切片成日期。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TENCENT}
    period = (period or "").strip().lower()
    if period not in ("m1", "m5", "m15", "m30", "m60"):
        return {"available": False,
                "reason": f"bad_period: {period!r} (m1/m5/m15/m30/m60)",
                "source": SOURCE_TENCENT}
    if limit < 20 or limit > 1000:
        return {"available": False, "reason": f"bad_limit: {limit} (20..1000)",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: minute_kline(norm, period=period, limit=limit),
                    required_key="rows", source=SOURCE_TENCENT)


@router.get("/adjratio/{code}")
def get_adjust_ratios(code: str, mode: str = "qfq", limit: int = 640) -> dict:
    """A股日K复权系数(腾讯 fqkline): qfq 前复权 / hfq 后复权 / none 不复权。

    返回的是**系数**而不是整串复权K —— 本地 enriched 里还挂着指标/涨跌停信号/
    成交额/换手/ext 列, 整串换掉会一起丢。调用方把 ratio 乘到自己那串 K 的价格列上即可。

    ⚠️ 背景: 本地 ``adj_factor`` 表是空的(TickFlow ex_factors 对本账号返回 0 行),
       所以 enriched 里存的实际是**不复权价** —— 想看前复权必须从这里拿系数。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TENCENT}
    mode = (mode or "").strip().lower()
    if mode not in ("qfq", "hfq", "none"):
        return {"available": False, "reason": f"bad_mode: {mode!r} (qfq/hfq/none)",
                "source": SOURCE_TENCENT}
    if limit < 60 or limit > 640:
        return {"available": False, "reason": f"bad_limit: {limit} (60..640)",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: adjust_ratios(norm, mode=mode, limit=limit),
                    required_key="ratios", source=SOURCE_TENCENT)


@router.get("/sectors/{code}")
def get_sectors(code: str, limit: int = 24) -> dict:
    """个股所属板块 / 题材。

    行业走东财 F10 公司资料, 题材走 F10 核心题材 —— 每个题材都带东财给的
    ``reason``(入选理由), 前端 hover 即可看到"为什么归进这个板块"。

    ⚠️ 与九章不同: 九章在真源拿不到时会退回硬编码的 ``_DEMO_SECTORS_MAP``,
    我方两个报表都空就直接 ``available:false`` —— 宁可显示"取不到", 不编板块名。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_EM_F10}
    return _respond(norm, lambda: stock_sectors(norm, limit=limit),
                    required_key="boards", source=SOURCE_EM_F10)


@router.get("/financials/{code}")
def get_financials(code: str, periods: int = 8) -> dict:
    """财务摘要(akshare → 东财 datacenter 财务分析指标)。periods 1..12, 默认 8。

    返回 7 组共 80 个指标 × 最近 N 期, values 按报告期从新到旧。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_AKSHARE}
    if periods < 1 or periods > 12:
        return {"available": False, "reason": f"bad_periods: {periods} (1..12)",
                "source": SOURCE_AKSHARE}
    return _respond(norm, lambda: financials(norm, periods=periods),
                    required_key="indicators", source=SOURCE_AKSHARE)


@router.get("/index/{code}")
def get_index_daily(code: str, limit: int = 320) -> dict:
    """指数日K(腾讯) —— 多股对比页的「基准指数」用。

    本地 ``kline_index`` 库实测只有 399006(创业板指), 沪深300 / 上证指数都没有,
    所以基准走腾讯真源(与个股日K、分钟K 同源, source=live-tencent)。

    ⚠️ 代码规则与个股不同: 沪市指数是 **0 开头但属于 sh**(000300 沪深300、
    000001 上证指数), provider 里 ``_tx_index_symbol`` 单独处理 —— 按个股规则
    拼成 sz000300 会拿到空数据, 那是"返回成功但没东西"的假成功。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是指数代码",
                "source": SOURCE_TENCENT}
    if limit < 20 or limit > 1000:
        return {"available": False, "reason": f"bad_limit: {limit} (20..1000)",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: index_daily(norm, limit=limit),
                    required_key="rows", source=SOURCE_TENCENT)


@router.get("/ticks/{code}")
def get_tick_trades(code: str, tail: int = 200) -> dict:
    """成交明细(腾讯) —— 个股页「成交明细」面板。

    ⚠️ 粒度必须说清: 这是**每 3 秒一行的成交快照**("分笔"), 不是**逐笔**。
    免费公开源只到这个粒度, 逐笔要 Level-2(付费)。UI 上写着"逐笔"就是误导,
    面板标题与来源说明都由后端一并返回, 前端照抄, 不另编口径。

    ⚠️ 买卖方向(B/S/M)是行情商按成交价与当时盘口的比较判定的,
    **不是交易所披露字段**; M = 无法判断, 汇总时单独计一列, 不硬塞进买或卖。

    ``tail`` = 取最近多少行(默认 200, 上限 3000)。接口只能从开盘往后翻页,
    所以后端先对数级探到最后一页再回溯 —— 取最近 200 行约需十几次请求。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TENCENT}
    if tail < 1 or tail > 3000:
        return {"available": False, "reason": f"bad_tail: {tail} (1..3000)",
                "source": SOURCE_TENCENT}
    return _respond(norm, lambda: tick_trades(norm, tail=tail),
                    required_key="rows", source=SOURCE_TENCENT)
