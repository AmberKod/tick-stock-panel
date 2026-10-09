# -*- coding: utf-8 -*-
"""美股基本面数据层 —— SEC EDGAR XBRL（官方、免费、无需 API Key）。

摘取自九章量化终端 (jiuzhang-algor) 的 ``us_fund.py``（MIT License,
Copyright (c) 2026 小鱼总的小圈子, 见仓根 THIRD_PARTY_NOTICES.md）。
摘取日期: 2026-10-09。摘取方式: 原样移植, 仅做以下 TSP 适配:

1. 磁盘缓存目录 ``CACHE_DIR`` 从模块旁 ``.cache`` 改为
   ``settings.data_dir / "us_fund_cache"`` (与 job_store 同级), 可用
   :func:`set_cache_dir` 覆盖, 构造器亦可传入;
2. 所有 HTTP 请求经 ``_opener`` 发出 —— ``ProxyHandler({})`` 显式清空
   代理: SEC EDGAR 必须直连 (本机代理 7897 出口 IP 会被 SEC 封,
   调研实测带代理访问 SEC 全部 SSL: UNEXPECTED_EOF, 清空后 200);
3. 对外返回数据带 ``source: "sec-edgar"`` (原版即有, 保持), 天然过
   source_gate 闸门;
4. 金额保持 SEC 原始美元值, 不做单位换算 (AAPL 最新季度营收
   $109.42B 为原始 XBRL val)。

为什么不用 yfinance: 雅虎财经非官方接口, 国内网络与共享 IP 下频繁
403/429, 实测本机 HTTP 403。SEC data.sec.gov 实测可用且数据权威
(来自 10-K/10-Q 原始报表)。

用法:
    from app.data_providers.jiuzhang_us_fund_provider import fundamentals
    d = fundamentals("NVDA")     # 返回 dict/None

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
import os
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE = "sec-edgar"

UA = os.environ.get("SEC_UA", "JiuzhangQuant/1.0 (local research; contact@example.com)")


def _default_cache_dir() -> Path:
    """TSP 统一落 ``settings.data_dir / "us_fund_cache"`` (与 job_store 同级)。"""
    from app.config import settings
    return settings.data_dir / "us_fund_cache"


def set_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """覆盖磁盘缓存目录 (测试用)。传 None 恢复默认 settings.data_dir 派生。"""
    global CACHE_DIR
    CACHE_DIR = Path(path) if path is not None else _default_cache_dir()
    os.makedirs(CACHE_DIR, exist_ok=True)


CACHE_DIR: Path = _default_cache_dir()
os.makedirs(CACHE_DIR, exist_ok=True)

# SEC EDGAR 必须直连: ProxyHandler({}) 显式清空代理环境 (含 http_proxy/
# https_proxy/HTTP_PROXY/HTTPS_PROXY), 绝不走本机 7897 —— 部分代理出口 IP
# 会被 SEC 封禁。模块级单例, 全部请求复用。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# XBRL 概念候选项: 不同公司用的标签不一样, 逐个尝试直到拿到数据
CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "net_income": ["NetIncomeLoss"],
    "operating_income": ["OperatingIncomeLoss"],
    "gross_profit": ["GrossProfit"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
}
CN = {
    "revenue": "营业收入", "net_income": "净利润", "operating_income": "营业利润",
    "gross_profit": "毛利润", "assets": "总资产", "liabilities": "总负债",
    "equity": "股东权益", "eps_diluted": "稀释每股收益", "operating_cash_flow": "经营现金流",
}

_TICKERS = None
_TICKERS_TS = 0


def _get(url, timeout=25, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
    with _opener.open(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            data = gzip.decompress(data)
    return data if raw else json.loads(data.decode("utf-8"))


def _disk_cache(name, ttl, fn):
    p = os.path.join(str(CACHE_DIR), name)
    try:
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl:
            with open(p, "r", encoding="utf-8") as f:
                logger.info("us_fund disk cache hit: %s", name)
                return json.load(f)
    except Exception:
        pass
    v = fn()
    if v is not None:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(v, f)
        except Exception:
            pass
    return v


def ticker_map():
    """ticker(大写) -> cik(int)。官方映射表, 本地缓存 7 天。"""
    global _TICKERS, _TICKERS_TS
    if _TICKERS and (time.time() - _TICKERS_TS) < 3600:
        return _TICKERS
    def _load():
        try:
            d = _get("https://www.sec.gov/files/company_tickers.json")
            return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in d.values()}
        except Exception:
            return None
    m = _disk_cache("company_tickers.json", 7 * 86400, _load)
    if m:
        _TICKERS, _TICKERS_TS = m, time.time()
    return _TICKERS or {}


def cik_of(ticker):
    return ticker_map().get(str(ticker).upper().strip())


def _concept_series(cik, concept, timeout=25):
    url = ("https://data.sec.gov/api/xbrl/companyconcept/CIK%010d/us-gaap/%s.json" % (cik, concept))
    try:
        d = _get(url, timeout=timeout)
    except Exception:
        return None
    units = (d.get("units") or {})
    rows = []
    for unit, arr in units.items():
        if unit not in ("USD", "USD/shares", "shares"):
            continue
        for x in arr or []:
            rows.append(x)
    if not rows:
        return None
    # 季度(10-Q, 单季 fp=Q1..Q4) 与 年度(10-K, FY)
    def _is_q(x):
        return x.get("form") == "10-Q" and x.get("start") and x.get("end") and \
               _days(x["start"], x["end"]) <= 120
    def _is_y(x):
        return x.get("form") == "10-K" and _days(x.get("start") or "", x.get("end") or "") > 300
    def _dedup(arr):
        """同一报告期会被多次报送(修正/重述), 按 (start,end) 去重只保留最新 filed。"""
        seen = {}
        for x in arr:
            key = (x.get("start"), x.get("end"))
            if key not in seen or (x.get("filed") or "") > (seen[key].get("filed") or ""):
                seen[key] = x
        return sorted(seen.values(), key=lambda x: (x["end"], x.get("filed") or ""))
    q = _dedup([x for x in rows if _is_q(x)])
    y = _dedup([x for x in rows if _is_y(x)])
    return {"unit": "USD/shares" if "USD/shares" in units else "USD",
            "quarterly": [{"start": x.get("start"), "end": x.get("end"), "fp": x.get("fp"),
                           "val": x.get("val"), "fy": x.get("fy"), "form": x.get("form")}
                          for x in q[-12:]],
            "annual": [{"end": x.get("end"), "val": x.get("val"), "fy": x.get("fy")}
                       for x in y[-8:]],
            "latest": (q[-1]["val"] if q else (y[-1]["val"] if y else None))}


def _recent(days):
    import datetime
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def _days(a, b):
    try:
        import datetime
        da = datetime.date(*[int(v) for v in a.split("-")])
        db = datetime.date(*[int(v) for v in b.split("-")])
        return abs((db - da).days)
    except Exception:
        return 0


class JiuzhangUSFundProvider:
    """SEC EDGAR 美股基本面 provider (九章 us_fund.py 摘取版)。

    ``source: "sec-edgar"`` 在 :meth:`fundamentals` 返回体中携带,
    天然过 source_gate 白名单。
    """

    name = "jiuzhang_us_fund"
    source = SOURCE

    def __init__(self, cache_dir: str | os.PathLike[str] | None = None):
        if cache_dir is not None:
            set_cache_dir(cache_dir)

    def fundamentals(self, ticker, metrics=None):
        return fundamentals(ticker, metrics=metrics)


def fundamentals(ticker, metrics=None):
    """拉取美股基本面。返回 dict 或 None（非美股/无数据时）。

    返回体含 ``source: "sec-edgar"`` 与 ``ticker``/``cik``/``metrics``/
    ``count``; 金额为 SEC 原始美元值, 不做单位换算。
    """
    ticker = str(ticker).upper().strip()
    cik = cik_of(ticker)
    if not cik:
        return None
    keys = metrics or list(CONCEPTS.keys())
    out = {"ticker": ticker, "cik": cik, "metrics": {}, "source": SOURCE}
    got = 0
    for k in keys:
        best, best_score = None, None
        for concept in CONCEPTS.get(k, []):
            s = _disk_cache("c%d_%s.json" % (cik, concept), 86400,
                            lambda c=concept: _concept_series(cik, c))
            if not s or not (s.get("quarterly") or s.get("annual")):
                continue
            q, a = s.get("quarterly") or [], s.get("annual") or []
            newest = max([x["end"] for x in q] or [x["end"] for x in a] or [""])
            # 优先取"有季度数据 + 报告期最新"的概念(不同公司标签差异很大)
            score = (1 if q else 0, len(q), newest)
            if best_score is None or score > best_score:
                best, best_score = dict(s), score
            time.sleep(0.12)  # SEC 限速: 10 req/s
            if q and newest >= _recent(400):
                break        # 已是近一年内的季度数据, 不必再试
        if best:
            best["label_cn"] = CN.get(k, k)
            out["metrics"][k] = best
            got += 1
    if not got:
        return None
    out["count"] = got
    return out


def summary_text(d):
    """把基本面压成给模型看的紧凑文本（控制 token）。"""
    if not d:
        return ""
    lines = ["标的 %s (SEC CIK %d) 基本面数据（来自 10-K/10-Q 原始报表）:" % (d["ticker"], d["cik"])]
    for k, s in (d.get("metrics") or {}).items():
        label = s.get("label_cn") or k
        q = s.get("quarterly") or []
        a = s.get("annual") or []
        if q:
            seg = ", ".join("%s: %s" % (x["end"], _fmt(x["val"], s.get("unit"))) for x in q[-5:])
            lines.append("  %s 近%d个季度: %s" % (label, min(5, len(q)), seg))
        elif a:
            seg = ", ".join("%s: %s" % (x["end"], _fmt(x["val"], s.get("unit"))) for x in a[-3:])
            lines.append("  %s 近%d个年度: %s" % (label, min(3, len(a)), seg))
    return "\n".join(lines)


def _fmt(v, unit):
    if v is None:
        return "—"
    try:
        if unit == "USD/shares":
            return "%.2f" % float(v)
        av = abs(float(v))
        if av >= 1e9:
            return "%.2fB" % (float(v) / 1e9)
        if av >= 1e6:
            return "%.2fM" % (float(v) / 1e6)
        return "%.0f" % float(v)
    except Exception:
        return str(v)
