r"""OpenBB 深度数据层 —— 实时盘口 / 三大报表 / SEC 报送（九章融合 P1）。

摘取自九章量化终端 (jiuzhang-algor) 的 ``openbb_client.py``（MIT License,
Copyright (c) 2026 小鱼总的小圈子, 见仓根 THIRD_PARTY_NOTICES.md）。
摘取日期: 2026-10-09。摘取方式: 原样移植, 仅做以下 TSP 适配:

1. ``BASE``/``TIMEOUT`` 从模块常量改为 ``settings.openbb_base_url`` /
   ``settings.openbb_timeout_s`` (容器默认 host.docker.internal:6900,
   宿主直跑时用 127.0.0.1);
2. 对外返回统一包 ``{"source": "openbb", "data": ...}`` —— 原始 results
   结构原样保留在 data 内, 外层 source 标记天然过 source_gate 闸门
   (list 型 results 无法携带 source 键, 外层包装是唯一一致做法);
3. fail-closed: ``summary_text`` 原版吞异常返回错误字符串, TSP 侧改为
   **直接上抛** —— provider 内部不吞异常不造假数据, 由 API 层接住转
   ``available: false``;
4. opener 显式 ``ProxyHandler({})`` 清空代理环境 —— 目标是本机 OpenBB
   服务, 走任何代理必然失败;
5. ``health()`` 未摘取 (TSP 无此调用方, API 层以 provider 抛错判定不可用)。

运维依赖: OpenBB API 服务是宿主常驻服务, 由
``D:\OpenBB\start-openbb-api.ps1`` 启动 (与 sec_relay 同级)。
服务没起 / 超时 / 404 → provider 抛异常, 绝不返回假数据。

用法:
    from app.data_providers.jiuzhang_openbb_provider import quote
    d = quote("NVDA")   # {"source": "openbb", "data": {...盘口 dict...}}

上游许可 (MIT):

    Copyright (c) 2026 小鱼总的小圈子

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

SOURCE = "openbb"
DEFAULT_PROVIDER = "yfinance"

# 本机 OpenBB 服务, 必须禁用代理(进程可能继承了 HTTP_PROXY, 走代理必然失败)。
# 模块级单例, 全部请求复用。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base() -> str:
    """BASE 从 settings 读 (容器/宿主部署差异由配置吸收)。"""
    from app.config import settings
    return settings.openbb_base_url.rstrip("/")


def _timeout() -> float:
    from app.config import settings
    return settings.openbb_timeout_s


def _get(path: str, **params):
    """调 OpenBB REST, 返回 OBBject 里的 results (list 或 dict)。

    服务没起 / 超时 / 404 → 异常原样上抛 (fail-closed, 不吞不造假)。
    """
    ps = {k: v for k, v in params.items() if v not in (None, "")}
    url = _base() + path
    if ps:
        url += "?" + urllib.parse.urlencode(ps)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _OPENER.open(req, timeout=_timeout()) as r:
        body = json.loads(r.read().decode("utf-8"))
    if isinstance(body, dict) and "detail" in body and "results" not in body:
        raise RuntimeError(str(body["detail"])[:200])
    return body.get("results", body) if isinstance(body, dict) else body


def norm_symbol(code: str, market: str = "US") -> str:
    """TSP 代码 -> OpenBB/yfinance 代码。港股 00700 -> 0700.HK。"""
    c = (code or "").strip().upper()
    if not c:
        return c
    if c.endswith(".HK"):
        return c
    if market == "HK" or (len(c) == 5 and c.isdigit()):
        return c.lstrip("0").rjust(4, "0") + ".HK"
    return c


def quote(code: str, market: str = "US") -> dict:
    """实时盘口: bid/ask/最新价/50日/200日均线/年内高低。

    返回 ``{"source": "openbb", "data": {...单标的盘口 dict...}}``。
    """
    sym = norm_symbol(code, market)
    rows = _get("/equity/price/quote", symbol=sym, provider=DEFAULT_PROVIDER)
    if isinstance(rows, list):
        data = rows[0] if rows else {}
    else:
        data = rows
    return {"source": SOURCE, "data": data}


def kline(code: str, market: str = "US", start_date: str | None = None,
          provider: str = DEFAULT_PROVIDER) -> dict:
    """日线, 返回 ``{"source": "openbb", "data": [{date,open,high,low,close,volume}]}``。"""
    sym = norm_symbol(code, market)
    rows = _get("/equity/price/historical", symbol=sym, provider=provider,
                start_date=start_date)
    out = []
    for r in rows or []:
        out.append({
            "date": str(r.get("date"))[:10],
            "open": r.get("open"), "high": r.get("high"),
            "low": r.get("low"), "close": r.get("close"),
            "volume": r.get("volume"),
        })
    return {"source": SOURCE, "data": out}


def statement(code: str, kind: str = "income", market: str = "US",
              period: str = "annual", limit: int = 3) -> dict:
    """三大报表: income | balance | cash。

    返回 ``{"source": "openbb", "data": [报表期 dict 列表]}``。
    """
    sym = norm_symbol(code, market)
    path = "/equity/fundamental/" + kind
    rows = _get(path, symbol=sym, provider=DEFAULT_PROVIDER,
                period=period, limit=limit)
    return {"source": SOURCE, "data": rows if isinstance(rows, list) else []}


def filings(code: str, market: str = "US", limit: int = 8,
            form_type: str | None = None) -> dict:
    """SEC EDGAR 报送列表(免费 provider, 无需 key)。

    返回 ``{"source": "openbb", "data": [报送记录列表]}``。
    """
    sym = norm_symbol(code, market)
    ps: dict = {"symbol": sym, "provider": "sec", "limit": limit}
    if form_type:
        ps["form_type"] = form_type
    rows = _get("/equity/fundamental/filings", **ps)
    return {"source": SOURCE, "data": rows if isinstance(rows, list) else []}


def institutional(code: str, market: str = "US", limit: int = 20) -> dict:
    """机构持股(13F 位): 谁持有、增持还是减持。

    走 ``nasdaq`` provider —— ⚠️ **不用** ``/ownership/form_13f``: 那个端点
    只接受 ``provider=sec``, 而本机 OpenBB 的 SEC 出口取不到数(空返回),
    nasdaq 版给的是同一类信息(机构名单 + 持股数 + 变动)且实测有数据。

    ⚠️ ``market_value`` 是源站原值, **单位存疑**(实测 AAPL: Vanguard
    14.26 亿股对应 market_value 4.8 亿, 与 14.26亿×336 美元差 1000 倍, 疑似
    千美元口径)。所以这里**不做换算**, 字段命名带 ``_raw``, 前端不展示它 ——
    持股数与变动百分比才是判断机构动向的可靠量, 不拿存疑数字充数。

    返回 ``{"source": "openbb", "data": [机构持股行]}``。
    """
    sym = norm_symbol(code, market)
    rows = _get("/equity/ownership/institutional", symbol=sym, provider="nasdaq", limit=limit)
    out = []
    for r in rows or []:
        out.append({
            "date": str(r.get("date"))[:10] if r.get("date") else None,
            "owner": r.get("owner_name"),
            "shares_held": r.get("shares_held"),
            "shares_change": r.get("shares_change"),
            "shares_change_pct": r.get("shares_change_percent"),
            "market_value_raw": r.get("market_value"),
            "url": r.get("url"),
        })
    return {"source": SOURCE, "data": out}


def insider_trading(code: str, market: str = "US", limit: int = 20) -> dict:
    """内部人交易(Form 4 明细): 高管/董事买卖了多少、什么价、交易后持股。

    与 ``filings``(报送列表, 只有报送类型与日期)不同 —— 这里给的是**交易
    本身**: 交易类型(Automatic Sell / Buy / Award 等)、股数、成交价、交易后
    持股、持有人身份(Officer / Director)。走 nasdaq provider, 免费无 key。

    返回 ``{"source": "openbb", "data": [交易行]}``。
    """
    sym = norm_symbol(code, market)
    rows = _get("/equity/ownership/insider_trading", symbol=sym, provider="nasdaq", limit=limit)
    out = []
    for r in rows or []:
        out.append({
            "transaction_date": str(r.get("transaction_date"))[:10] if r.get("transaction_date") else None,
            "owner": r.get("owner_name"),
            "title": r.get("owner_title"),
            "ownership_type": r.get("ownership_type"),
            "transaction_type": r.get("transaction_type"),
            "shares": r.get("securities_transacted"),
            "price": r.get("transaction_price"),
            "shares_held_after": r.get("shares_held"),
            "url": r.get("url"),
        })
    return {"source": SOURCE, "data": out}


def estimates(code: str, market: str = "US") -> dict:
    """分析师一致预期: 目标价高/低/均值/中位数 + 评级 + 覆盖机构数。

    走 ``yfinance`` provider。这是美股侧对标 A股「研报」位的数据 ——
    ``recommendation`` 与 ``number_of_analysts`` 都是真实汇总, 不合成。

    ⚠️ 空 results(比如被 yfinance 限流、或标的无覆盖)由 API 层转
    available:false, 这里不做任何兜底填充。

    返回 ``{"source": "openbb", "data": {...单标的预期 dict...}}``。
    """
    sym = norm_symbol(code, market)
    rows = _get("/equity/estimates/consensus", symbol=sym, provider="yfinance")
    if isinstance(rows, list):
        row = rows[0] if rows else {}
    else:
        row = rows if isinstance(rows, dict) else {}
    return {"source": SOURCE, "data": {
        "symbol": row.get("symbol") or sym,
        "currency": row.get("currency"),
        "current_price": row.get("current_price"),
        "target_high": row.get("target_high"),
        "target_low": row.get("target_low"),
        "target_consensus": row.get("target_consensus"),
        "target_median": row.get("target_median"),
        "recommendation": row.get("recommendation"),
        "recommendation_mean": row.get("recommendation_mean"),
        "number_of_analysts": row.get("number_of_analysts"),
    }}


def summary_text(code: str, market: str = "US") -> dict:
    """给前端/大模型用的一段摘要文本。

    返回 ``{"source": "openbb", "data": {"text": "..."}}``。
    与原版差异: quote 失败时异常直接上抛 (fail-closed), 不再吞掉拼
    "OpenBB 摘要失败: ..." 字符串 —— 由 API 层接住转 available:false。
    """
    q = quote(code, market)
    row = q.get("data") or {}
    if not row:
        return {"source": SOURCE, "data": {"text": "OpenBB 无此标的数据"}}

    def f(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "-"

    text = (f"{row.get('symbol', code)} 最新价 {f(row.get('last_price'))} "
            f"(bid {f(row.get('bid'))} / ask {f(row.get('ask'))}), "
            f"日内 {f(row.get('low'))}~{f(row.get('high'))}, "
            f"50日均线 {f(row.get('ma_50d'))}, 200日均线 {f(row.get('ma_200d'))}, "
            f"年内高低 {f(row.get('year_low'))} / {f(row.get('year_high'))}")
    return {"source": SOURCE, "data": {"text": text}}


class JiuzhangOpenBBProvider:
    """OpenBB 深度数据 provider (九章 openbb_client.py 摘取版)。

    ``source: "openbb"`` 在每个方法返回体外层携带, 天然过
    source_gate 白名单。
    """

    name = "jiuzhang_openbb"
    source = SOURCE

    def quote(self, code: str, market: str = "US") -> dict:
        return quote(code, market)

    def kline(self, code: str, market: str = "US",
              start_date: str | None = None) -> dict:
        return kline(code, market, start_date=start_date)

    def statement(self, code: str, kind: str = "income", market: str = "US",
                  period: str = "annual", limit: int = 3) -> dict:
        return statement(code, kind=kind, market=market, period=period, limit=limit)

    def filings(self, code: str, market: str = "US", limit: int = 8,
                form_type: str | None = None) -> dict:
        return filings(code, market=market, limit=limit, form_type=form_type)

    def institutional(self, code: str, market: str = "US", limit: int = 20) -> dict:
        return institutional(code, market=market, limit=limit)

    def insider_trading(self, code: str, market: str = "US", limit: int = 20) -> dict:
        return insider_trading(code, market=market, limit=limit)

    def estimates(self, code: str, market: str = "US") -> dict:
        return estimates(code, market=market)

    def summary_text(self, code: str, market: str = "US") -> dict:
        return summary_text(code, market)
