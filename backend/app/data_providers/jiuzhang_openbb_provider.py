# -*- coding: utf-8 -*-
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
            return ("%.2f" % float(v))
        except Exception:
            return "-"

    text = ("%s 最新价 %s (bid %s / ask %s), 日内 %s~%s, "
            "50日均线 %s, 200日均线 %s, 年内高低 %s / %s" % (
                row.get("symbol", code), f(row.get("last_price")),
                f(row.get("bid")), f(row.get("ask")),
                f(row.get("low")), f(row.get("high")),
                f(row.get("ma_50d")), f(row.get("ma_200d")),
                f(row.get("year_low")), f(row.get("year_high"))))
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

    def summary_text(self, code: str, market: str = "US") -> dict:
        return summary_text(code, market)
