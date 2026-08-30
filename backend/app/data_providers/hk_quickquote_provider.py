"""港股实时行情 — 腾讯 r_hk* 优先, 新浪 hk* 降级。

移植自 ai_stock_tools/src/backend/app/data_provider/quick_quote_fetcher.py
港股部分 (port 自其 _is_hk_code / _normalize_hk_code / _tencent_prefix / _sina_prefix)。

设计原则:
- 不挂 quote_service 轮询链路 (港股没有 TickFlow 实时流, HTTP 拉一次算一次)
- 腾讯优先: 含 PE/PB/市值 (字段 80+); 新浪降级: 含 OHLCV 不含 PE/PB
- 网络/解析失败 → 返回空 df, 不抛错
- 内部 5 位代码 (如 "00700") → 腾讯请求 r_hk00700 / 新浪 hk00700
- 指数 ^HSI → 腾讯 hkHSI / 新浪 hkHSI (兼容 HSI.HK 内部格式)

返回 schema (统一):
    symbol (5位.HK), name, price, pre_close, open, high, low,
    volume, amount, change_pct, source, quote_ts, market
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import normalize_instruments

logger = logging.getLogger(__name__)

HK_TZ = ZoneInfo("Asia/Hong_Kong")

# 内部 symbol 后缀 → 各源前缀
_TENCENT_HK_STOCK_PREFIX = "r_hk"   # 腾讯: 带 r_ 前缀获实时
_SINA_HK_STOCK_PREFIX = "hk"
_TENCENT_HK_INDEX_PREFIX = "hk"     # 指数直接 hkHSI 形式
_SINA_HK_INDEX_PREFIX = "hk"

# 内部 HSI.HK 格式 → 腾讯/新浪指数代码
_INDEX_CODE_MAP = {
    "HSI.HK": "HSI",
    "HSTECH.HK": "HSTECH",
    "HSCEI.HK": "HSCEI",
}


def _is_hk_stock_symbol(symbol: str) -> bool:
    """判断是否港股个股 (5 位数字 .HK 后缀, 或 5 位纯数字, 或 HK 前缀)。"""
    s = str(symbol or "").strip().upper()
    if s.endswith(".HK"):
        code = s[:-3]
        return len(code) == 5 and code.isdigit()
    # 5 位数字 (如 "00700")
    if len(s) == 5 and s.isdigit():
        return True
    # HK00700 (腾讯/新浪用过的形式)
    if s.startswith("HK") and len(s) > 2 and s[2:].isdigit():
        return True
    return False


def _is_hk_index(symbol: str) -> bool:
    """判断是否港股指数 (HSI.HK / HSTECH.HK / HSCEI.HK / ^HSI 等)。"""
    s = str(symbol or "").strip().upper()
    if s in _INDEX_CODE_MAP:
        return True
    if s.startswith("^") and s[1:] in _INDEX_CODE_MAP.values():
        return True
    return False


def _stock_code(symbol: str) -> str:
    """从 '00700.HK' / '00700' / 'hk00700' 提取 5 位代码。"""
    s = str(symbol or "").strip().upper()
    s = s.removeprefix("HK.").removeprefix("HK").removesuffix(".HK")
    if s.isdigit():
        return s.zfill(5)
    return s


def _tencent_symbol(symbol: str) -> str:
    """内部格式 → 腾讯请求代码。

    00700.HK → r_hk00700;  HSI.HK → hkHSI;  ^HSI → hkHSI
    """
    s = str(symbol or "").strip().upper()
    if _is_hk_index(s):
        idx_code = _INDEX_CODE_MAP.get(s, s.removeprefix("^"))
        return f"{_TENCENT_HK_INDEX_PREFIX}{idx_code}"
    if _is_hk_stock_symbol(s):
        return f"{_TENCENT_HK_STOCK_PREFIX}{_stock_code(s)}"
    return s  # 兜底原样


def _sina_symbol(symbol: str) -> str:
    """内部格式 → 新浪请求代码。00700.HK → hk00700; HSI.HK → hkHSI。"""
    s = str(symbol or "").strip().upper()
    if _is_hk_index(s):
        idx_code = _INDEX_CODE_MAP.get(s, s.removeprefix("^"))
        return f"{_SINA_HK_INDEX_PREFIX}{idx_code}"
    if _is_hk_stock_symbol(s):
        return f"{_SINA_HK_STOCK_PREFIX}{_stock_code(s)}"
    return s


# ── 腾讯 (qt.gtimg.cn) ──────────────────────────────

_TENCENT_FIELDS_RE = re.compile(r'="([^"]*)"')


async def _fetch_tencent(symbol: str, timeout: float = 5.0) -> dict | None:
    """腾讯财经行情。

    返回 dict 字段:
        symbol (原始内部), name, price, pre_close, open, high, low,
        volume, amount, change_pct, source="tencent"
    """
    sym = _tencent_symbol(symbol)
    url = f"https://qt.gtimg.cn/q={sym}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as headers_client:
            resp = await headers_client.get(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://stockapp.finance.qq.com/"}
            )
        resp.raise_for_status()
        # 腾讯返回 GBK
        try:
            raw = resp.content.decode("gbk")
        except UnicodeDecodeError:
            raw = resp.text
    except Exception as exc:  # noqa: BLE001
        logger.debug("tencent quote fetch failed %s: %s", sym, exc)
        return None

    m = _TENCENT_FIELDS_RE.search(raw)
    if not m:
        return None
    fields = m.group(1).split("~")
    if len(fields) < 40:
        return None

    def _f(i: int) -> float | None:
        try:
            v = fields[i]
            return float(v) if v else None
        except (ValueError, IndexError):
            return None

    name = fields[1] if len(fields) > 1 else ""
    code = fields[2] if len(fields) > 2 else ""
    price = _f(3)
    pre_close = _f(4)
    open_p = _f(5)
    volume_hands = _f(6)  # 腾讯是"手"为单位
    amount = _f(37)  # 成交额, 万
    high = _f(33)
    low = _f(34)
    change_pct = _f(32)  # 涨跌幅, 百分制

    # 港股成交量以"股"为单位, 腾讯返回是"手", 需 * 100 (但 1手=100 是默认, 实际有变)
    # 不强转, 留原始 hands 单位
    volume_shares = volume_hands * 100 if volume_hands else None

    return {
        "symbol": _normalize_internal_symbol(symbol),
        "name": name,
        "code": code,
        "price": price,
        "pre_close": pre_close,
        "open": open_p,
        "high": high,
        "low": low,
        "volume": volume_shares,
        "amount": amount,
        "change_pct": change_pct,
        "source": "tencent",
    }


# ── 新浪 (hq.sinajs.cn) ──────────────────────────────

async def _fetch_sina(symbol: str, timeout: float = 5.0) -> dict | None:
    """新浪财经行情 (港股降级源, 字段少但稳定)。

    返回 dict 不含 PE/PB/市值, 含 OHLCV。
    """
    sym = _sina_symbol(symbol)
    url = f"https://hq.sinajs.cn/list={sym}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as sina_client:
            resp = await sina_client.get(
                url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
            )
        resp.raise_for_status()
        # 新浪返回 GBK
        try:
            raw = resp.content.decode("gbk")
        except UnicodeDecodeError:
            raw = resp.text
    except Exception as exc:  # noqa: BLE001
        logger.debug("sina quote fetch failed %s: %s", sym, exc)
        return None

    m = _TENCENT_FIELDS_RE.search(raw)
    if not m:
        return None
    fields = m.group(1).split(",")
    if len(fields) < 10 or not fields[0]:
        return None

    def _f(i: int) -> float | None:
        try:
            v = fields[i]
            return float(v) if v else None
        except (ValueError, IndexError):
            return None

    name = fields[0]
    open_p = _f(1)
    pre_close = _f(2)
    price = _f(3)
    high = _f(4)
    low = _f(5)
    # 新浪港股: fields[8] 成交量, fields[9] 成交额 (元)
    volume = _f(8)
    amount = _f(9)
    change_pct = None
    if price is not None and pre_close:
        change_pct = (price - pre_close) / pre_close * 100

    return {
        "symbol": _normalize_internal_symbol(symbol),
        "name": name,
        "code": _stock_code(symbol),
        "price": price,
        "pre_close": pre_close,
        "open": open_p,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": amount,
        "change_pct": round(change_pct, 4) if change_pct is not None else None,
        "source": "sina",
    }


def _normalize_internal_symbol(symbol: str) -> str:
    """把 '00700' / 'hk00700' / 'HK00700' / '00700.HK' 统一成 '00700.HK'。"""
    s = str(symbol or "").strip().upper()
    if s.endswith(".HK"):
        return s
    if _is_hk_stock_symbol(s):
        return f"{_stock_code(s)}.HK"
    return s


async def fetch_quote(symbol: str, timeout: float = 5.0) -> dict | None:
    """单只港股行情 — 腾讯优先, 新浪降级, 失败返回 None。

    sync 包装见 fetch_quote_sync (行情拉取常在同步上下文, 如 FastAPI endpoint)。
    """
    if _is_hk_index(symbol):
        # 指数走腾讯 (新浪指数字段更稀)
        r = await _fetch_tencent(symbol, timeout=timeout)
        if r is not None:
            return r
        return await _fetch_sina(symbol, timeout=timeout)

    if not _is_hk_stock_symbol(symbol):
        return None

    r = await _fetch_tencent(symbol, timeout=timeout)
    if r is not None:
        return r
    return await _fetch_sina(symbol, timeout=timeout)


def fetch_quote_sync(symbol: str, timeout: float = 5.0) -> dict | None:
    """同步包装: FastAPI endpoint 常用同步路径。"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已在事件循环内, 走 run_in_executor 避免阻塞
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(asyncio.run, fetch_quote(symbol, timeout)).result()
        return loop.run_until_complete(fetch_quote(symbol, timeout))
    except RuntimeError:
        return asyncio.run(fetch_quote(symbol, timeout))


def batch_quotes_to_df(quotes: list[dict]) -> pl.DataFrame:
    """批量行情转 polars DataFrame, 统一字段。

    任意字段缺失填 null, 列名与 normalize_daily 兼容以便后续统一入库。
    """
    if not quotes:
        return pl.DataFrame()
    cols = [
        "symbol", "name", "code", "price", "pre_close", "open", "high", "low",
        "volume", "amount", "change_pct", "source", "quote_ts",
    ]
    rows = []
    now_ms = int(datetime.now(HK_TZ).timestamp() * 1000)
    for q in quotes:
        rows.append({
            "symbol": q.get("symbol"),
            "name": q.get("name"),
            "code": q.get("code"),
            "price": q.get("price"),
            "pre_close": q.get("pre_close"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "volume": q.get("volume"),
            "amount": q.get("amount"),
            "change_pct": q.get("change_pct"),
            "source": q.get("source"),
            "quote_ts": now_ms,
        })
    return pl.DataFrame(rows).select(cols)


# ── Provider 协议适配 (M1 占位, M2 真正接 quote_service 轮询) ──

class HKQuickQuoteProvider:
    """港股实时行情 provider (M1 阶段: 仅作为 get_realtime 入口注册到数据源池)。

    M1 不挂 quote_service 轮询链路, 走 /api/hk/realtime/{symbol} 直拉。
    M2 接入轮询时, 此 provider 取代 TickFlowProvider 负责 HK 标的。
    """

    name = "hk_quickquote"
    capabilities = ProviderCapabilities(
        realtime=True,
        # instruments/daily/adj_factor/minute/financial 全部 False
    )

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        """M1 走 hk_data_adapter 的内置 10 龙头, 不直接调 quickquote。"""
        from app.services.hk_data_adapter import load_demo_instruments
        return load_demo_instruments()

    def get_daily(self, *args, **kwargs):  # noqa: ARG002
        return pl.DataFrame()

    def get_adj_factors(self, *args, **kwargs):  # noqa: ARG002
        return pl.DataFrame()

    def get_minute(self, *args, **kwargs):  # noqa: ARG002
        return pl.DataFrame()

    def get_realtime(
        self,
        universes: list[str] | None = None,  # noqa: ARG002
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        """批量拉取 (M1 阶段: 同步逐个拉, 失败该标跳过)。

        M2 优化: 改 httpx.AsyncClient 并发拉取 + 限流。
        """
        if not symbols:
            return pl.DataFrame()
        quotes: list[dict] = []
        for s in symbols:
            q = fetch_quote_sync(s, timeout=3.0)
            if q is not None:
                quotes.append(q)
        return batch_quotes_to_df(quotes)
