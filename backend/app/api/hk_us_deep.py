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
- GET  /minute/{symbol}             分钟K —— 通达信扩展市场(MAC-EX 7727), 上限 700 根
- GET  /intraday/{symbol}           当日分时 —— 由上述 m1 合成(会话/昨收/均价)
- GET  /snapshot/{symbol}           实时快照 —— MAC-EX: 昨收/均价/换手率/内外盘
- GET  /orderbook/{symbol}          五档盘口 —— MAC-EX: 真五档(港股无档时显式标注)
- GET  /boards/{symbol}             所属板块 —— MAC-EX: 行业/概念归类 + 板块涨跌
- GET  /board/{code}/members        板块成分股 —— 涨跌/量比/市值/PE/股息率
- GET  /boardlist?kind=US_HY        板块概念榜 —— 板块指数 + 领涨股
- GET  /multiday/{symbol}?days=5     多日分时 —— 仅港股(美股日期不自洽, 不给)
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
        """分钟 K —— 通达信扩展市场(MAC-EX 7727), m1/m5/m15/m30/m60。

        上游单次上限 700 根, 超出截断并在 payload 里标注 —— 前端须据此说明
        "只有近日日内深度"。
        """
        return _respond(lambda: prov.minute_bars(symbol, period=period, limit=limit),
                        required_key="rows", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/intraday/{symbol}")
    def _intraday(symbol: str, limit: int = 700) -> dict:
        """当日分时 —— 由 1 分钟 K 合成(最近一个会话)。

        会话未必是"今天"(美股跨零点、且上游可能滞后), 所以响应里带 session_date /
        as_of / session_complete —— 前端照着标, 不要把昨天的曲线说成今日分时。
        """
        if limit < 1 or limit > 700:
            return {"available": False, "reason": f"bad_limit: {limit} (1..700)",
                    "source": prov.SOURCE_TDX_EX}
        return _respond(lambda: prov.intraday(symbol, limit=limit),
                        required_key="points", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/snapshot/{symbol}")
    def _snapshot(symbol: str) -> dict:
        """实时快照(MAC-EX) —— 中文名 / **上游直给昨收** / 均价 / 换手率 / 内外盘。

        与 /intraday 的区别: 那条是用 m1 反推昨收并合成曲线, 这条是上游一次给齐的
        位行情。turnover_pct / change / pct 是推导值, payload 里用 derived 标了。
        """
        return _respond(lambda: prov.snapshot(symbol),
                        required_key="last", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/orderbook/{symbol}")
    def _orderbook(symbol: str) -> dict:
        """五档盘口(MAC-EX)。

        港股常见情况是买卖价/量全 0(上游不返回), 此时 available 仍为 true —— 总量/
        内外盘是真数据 —— 但 depth_available=false + note, 前端据此写"上游不返回"
        而不是画一排 0。
        """
        return _respond(lambda: prov.orderbook(symbol),
                        required_key="bids", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/boards/{symbol}")
    def _boards(symbol: str) -> dict:
        """所属板块(MAC-EX) —— 该票归类到哪些行业/概念板块, 各板块今日涨跌。

        实测 00700 有 23 条、AAPL 有 17 条(如"港股-网络游戏" / "美股-科技巨头")。
        板块自身带 close/pre_close, 涨跌在后端算好 —— pct 是推导值。
        """
        return _respond(lambda: prov.belong_boards(symbol),
                        required_key="boards", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/board/{board_code}/members")
    def _board_members(board_code: str, limit: int = 50, start: int = 0) -> dict:
        """板块成分股(MAC-EX) —— 涨跌幅 / 量比 / 市值 / 动态PE / 股息率 / 股本。

        board_code 形如 ``US0026`` / ``HK0211``(来自 /boards 或 /boardlist)。单位已核:
        市值=绝对金额、股息率=百分数、总股本=股(由上游万股换算)。
        """
        return _respond(lambda: prov.board_members(board_code, limit=limit, start=start),
                        required_key="members", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/boardlist")
    def _boardlist(kind: str = "US_HY", limit: int = 50) -> dict:
        """板块/概念榜(MAC-EX) —— 每个板块的指数价、涨跌与**板块内领涨股**。

        kind ∈ US_ALL/US_HY/US_GN/HK_ALL/HK_HY/HK_GN; limit 1..150。按涨跌降序。
        """
        return _respond(lambda: prov.board_list(kind=kind, limit=limit),
                        required_key="boards", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/multiday/{symbol}")
    def _multiday(symbol: str, days: int = 5) -> dict:
        """多日分时(MAC-EX) —— 仅港股。

        美股这里是**故意不给**的: 上游那几块数据不是"一天一块", 且日期标签与内容
        不对应(实测: 港股 day_i 收盘 == 次日昨收, 美股这条链对不上)。要给就只能由
        我们指定日期 = 编数据, 所以 fail-closed 并说明原因。
        """
        if days < 1 or days > 5:
            return {"available": False, "reason": f"bad_days: {days} (1..5)",
                    "source": prov.SOURCE_TDX_EX}
        return _respond(lambda: prov.multiday(symbol, days=days),
                        required_key="days", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/adjratio/{symbol}")
    def _adjratio(symbol: str, mode: str = "qfq", limit: int = 640) -> dict:
        """复权系数 —— 港美股均无真源, 恒返回 available:false + 原因。"""
        return _respond(lambda: prov.adjust_ratios(symbol, mode=mode, limit=limit),
                        required_key="ratios", source_hint=source_hint)

    @router.get("/transactions/{symbol}")
    def _transactions(symbol: str, date: str | None = None, limit: int = 200) -> dict:
        """逐笔成交(MAC-EX) —— 港股走 0x23FC/0x2406, 美股走 0x122F。

        ``date`` 为空 = 当日(``YYYY-MM-DD`` 或 ``YYYYMMDD``); ``limit`` 1..1800(单页不翻页)。
        港股价格是上游整数 /1000 = 港元; 港股的 ``nature`` 买卖映射未实测, 原样下发,
        前端不要拿它标买卖方向。
        """
        if limit < 1 or limit > 1800:
            return {"available": False, "reason": f"bad_limit: {limit} (1..1800)",
                    "source": prov.SOURCE_TDX_EX}
        return _respond(lambda: prov.transactions(symbol, date=date, limit=limit),
                        required_key="rows", source_hint=prov.SOURCE_TDX_EX)

    @router.get("/minuterange/{symbol}")
    def _minute_range(symbol: str, days: int = 5, period: str = "m1") -> dict:
        """多日分钟序列(MAC-EX 0x240C 按日循环) —— 突破蜡烛的 700 根上限。

        ⚠️ 形态与 ``/minute`` 不同: 上游这一路**没有真实开高低**, 每根只给该分钟价格
        + 当日累计均价 + 该分钟成交量。``period=m1`` 时四价同值并在 payload 标注;
        更长周期由 m1 确定性聚合(实测 close 与上游一致, 开高低偏差 <0.45%), 同样标注。
        """
        if days < 1 or days > 20:
            return {"available": False, "reason": f"bad_days: {days} (1..20)",
                    "source": prov.SOURCE_TDX_EX}
        return _respond(lambda: prov.minute_range(symbol, days=days, period=period),
                        required_key="days", source_hint=prov.SOURCE_TDX_EX)


_attach(router_hk, "HK")
_attach(router_us, "US")
