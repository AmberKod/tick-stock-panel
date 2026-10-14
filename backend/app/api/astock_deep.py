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
    industry_peers,
    minute_bars,
    minute_kline,
    realtime_quote,
    research_reports,
    stock_sectors,
    tick_trades,
)
from app.data_providers.tdx_tick_provider import (
    SOURCE as SOURCE_TICK,
)
from app.data_providers.tdx_tick_provider import (
    day_summary as tick_day_summary,
)
from app.data_providers.tdx_tick_provider import (
    recent_trades as tick_recent_trades,
)
from app.data_providers.tdx_tick_provider import (
    trading_dates as tick_trading_dates,
)
from app.data_providers.tdx_xdxr_provider import (
    SOURCE as SOURCE_XDXR,
)
from app.data_providers.tdx_xdxr_provider import (
    adj_ratios as xdxr_adj_ratios,
)
from app.data_providers.tdx_xdxr_provider import (
    xdxr_events,
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

    🔴 **口径警告(2026-10-13 实测)**: 腾讯这套是**等额(减法)**前复权, 不是交易所标准的
       乘性缩放 —— 实测 sh600519: ``qfqday`` 与 ``day`` 之差在同一个复权段内恒为
       ``103.463`` 元(恰好等于该段之后几次每股分红之和), 而股价从 1380 → 1559 元时
       ``ratio = qfq/raw`` 在**段内漂移 2.49%**。同一复权段内 ratio 本应是常数,
       漂就意味着它不是一个纯粹的缩放因子, 乘出来的"前复权价"会随原价格波动失真。

       要标准的**乘性口径**(与前复权的收益率语义一致, 且与新浪 hfq.js 因子的相邻比
       逐条吻合到 0.000000%), 请改用 ``/api/astock/adjfactor/{code}``。
       本端点保留不改: 万一有外部依赖在用旧口径, 静默改人群会踩空。
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


@router.get("/xdxr/{code}")
def get_xdxr_events(code: str) -> dict:
    """A股**分红送配全事件**(通达信标准市 7709 ``GetXdxrInfoCmd``) —— 上面 adjratio 的输入源头。

    与腾讯 adjratio 的分工:
      - ``/adjratio`` 给的是腾讯自己复完的系数(黑盒), 回答不了"这天为什么跌这么多";
      - ``/xdxr`` 给的是**原始权益事件**(除权日、每股分红、送转比例、配股价/比例、
        前后总股本), 每一档系数都能追到具体是哪一次分红导致的, 也能自己复核。

    ⚠️ ``category`` 不是全都能拿去算复权 —— 只有 **1(除权除息)** 参与; 其余是股本变动
       (增发/回购注销/非流通股上市等)的记录, 展示用, 别当股价调整据。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_XDXR}
    return _respond(norm, lambda: xdxr_events(norm),
                    required_key="events", source=SOURCE_XDXR)


@router.get("/adjfactor/{code}")
def get_xdxr_adj_ratios(code: str, mode: str = "qfq", limit: int = 640) -> dict:
    """从**权益事件推导**的复权系数(qfq/hfq) —— 可 self-check 版。

    与 ``/adjratio`` 数值上几乎一致, 差别在于本端点的每一档都带 ``events_used``
    (哪天、多少分红、多少送转、当时的前收盘、算出来的 r), 并且把已知偏差写进
    ``deviation_note``: 存在大量回购注销的股票(格力/美的)会有约 0.1%~0.5% 的额外偏差,
    因为交易所实际除权扣的不是通达信给的名义每股分红。

    ``mode=none`` 不在此提供 —— 不复权就是乘 1, 不需要打上游。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_XDXR}
    mode = (mode or "").strip().lower()
    if mode not in ("qfq", "hfq"):
        return {"available": False, "reason": f"bad_mode: {mode!r} (qfq/hfq)",
                "source": SOURCE_XDXR}
    if limit < 60 or limit > 800:
        return {"available": False, "reason": f"bad_limit: {limit} (60..800)",
                "source": SOURCE_XDXR}
    return _respond(norm, lambda: xdxr_adj_ratios(norm, mode=mode, limit=limit),
                    required_key="ratios", source=SOURCE_XDXR)


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


@router.get("/ticktrades/{code}")
def get_tdx_tick_trades(code: str, date: str | None = None, tail: int = 200) -> dict:
    """A股**真逐笔成交**(通达信标准市 ``GetHistoryTransactionDataCmd`` 0x0fb5)。

    与上面的 ``/ticks``(腾讯 3 秒快照)的根本区别:
        - ``/ticks`` 每 3 秒一行、一行是该窗口所有成交的**合计**;
        - 这里一行的就是一笔成交, 粒度到单笔。

    ``date`` 省略时取最近交易日(由腾讯日K日历决定, 不会挑到非交易日上)。
    ``tail`` 最近多少笔(默认 200, 上限 800 —— 协议单次上限, 超过要翻页走 ``/tickdaysummary``)。

    协议两个坑已经在 provider 里处理并把结论写在模块 docstring:
    上游会夹带 ``vol=0`` 的行情标记行(已剔除并计数)、15:00 之后还有几笔
    恒等于收盘价的盘后成交(单标 ``after_hours``, 不计入盘中量)。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TICK}
    if tail < 1 or tail > 800:
        return {"available": False, "reason": f"bad_tail: {tail} (1..800)",
                "source": SOURCE_TICK}
    return _respond(norm, lambda: tick_recent_trades(norm, date, tail),
                    required_key="trades", source=SOURCE_TICK)


@router.get("/tickdaysummary/{code}")
def get_tdx_tick_day_summary(code: str, date: str | None = None) -> dict:
    """整天逐笔 + **成交量**交叉校验 —— 比 ``/ticktrades`` 慢(要翻多页)。

    这里的 ``cross_check`` 是最硬的一条: 盘中(≤15:00)逐笔量合计必须等于腾讯
    日K成交量、末笔价必须等于收盘价 —— 实测 600519 / 300750 双双 0.000000%。
    UI 首屏别用它, 给"数据可信度"这类自查入口用。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TICK}
    return _respond(norm, lambda: tick_day_summary(norm, date),
                    required_key="trades", source=SOURCE_TICK)


@router.get("/tickdates/{code}")
def get_tdx_tick_dates(code: str, limit: int = 10) -> dict:
    """最近若干个交易日(升序) —— 逐笔面板的日期下拉。"""
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_TICK}
    dates = tick_trading_dates(norm, max(1, min(limit, 30)))
    if not dates:
        return {"available": False, "reason": "cross_source_unavailable: 腾讯日K取不到",
                "source": SOURCE_TICK}
    return {"available": True, "source": SOURCE_TICK, "code": norm,
            "data": {"code": norm, "dates": dates, "count": len(dates),
                     "latest": dates[-1] if dates else None}}


@router.get("/peers/{code}")
def get_industry_peers(code: str, limit: int = 16) -> dict:
    """同行公司对比 —— 东财 F10「行业对比」, 个股页「同行业对比」面板。

    ⚠️ 名单口径(面板里也写同一句话): 这是**分三块各取其指标行业前列**的公司
    再加本公司, **不是该行业全部成分股**。三块入选标准不同, 所以并非每家每列
    都有数 —— 缺的返回 None, 前端显示 "—", 不插值不补零。

    ⚠️ 同行而不"同三级行业": 逐核过 EM2016 后发现东财有时会把**产业链相关但
    三级行业不同**的公司算进来(例宁德时代同行里的天赐材料/先导智能)。返回体
    带了每家的 ``industry`` 与 ``same_industry``, 由前端标注, 不替东财否认。
    """
    norm = _norm_code(code)
    if not norm:
        return {"available": False, "reason": f"bad_code: {code!r} 不是 A股代码",
                "source": SOURCE_EM_F10}
    if limit < 1 or limit > 30:
        return {"available": False, "reason": f"bad_limit: {limit} (1..30)",
                "source": SOURCE_EM_F10}
    return _respond(norm, lambda: industry_peers(norm, limit=limit),
                    required_key="peers", source=SOURCE_EM_F10)
