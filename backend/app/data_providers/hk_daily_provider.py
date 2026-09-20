"""Hong Kong exchange-raw daily bars and separately versioned adjustments.

Sina's fixed, locally installed decoder handles its compressed price string.
Remote JavaScript is never evaluated. Tencent's ``day`` array is unadjusted,
even when the endpoint name contains ``fq``; quantities are historical shares.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import polars as pl

from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import normalize_market_symbols
from app.markets.hk import HK_TZ

logger = logging.getLogger(__name__)

SINA_DAILY_URL = "https://finance.sina.com.cn/stock/hkstock/{code}/klc2_kl.js"
SINA_FACTOR_URL = "https://finance.sina.com.cn/stock/hkstock/{code}/qfq.js"
TENCENT_DAILY_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY_VERIFY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DAILY_SOURCES = (
    {"id": "sina_hk_daily", "label": "新浪港股原始日线", "role": "primary"},
    {"id": "tencent_hk_daily", "label": "腾讯港股原始日线", "role": "fallback"},
)
ADJUSTMENT_SOURCES = ({"id": "sina_hk_qfq", "label": "新浪港股复权因子"},)
PRICE_METADATA_COLUMNS = (
    "source", "currency", "volume_unit", "amount_source", "price_adjustment",
    "price_schema_version", "adjustment_source", "adjustment_version",
    "adjustment_as_of", "observed_at", "raw_price_verified", "verification_source",
)
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_DECODE_LOCK = threading.Lock()
_DECODER: Any = None


def _source_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, ValueError):
        return str(exc)[:180]
    return type(exc).__name__


@dataclass(frozen=True)
class DailyFetchResult:
    frame: pl.DataFrame
    items: tuple[dict, ...]
    adjustments: pl.DataFrame
    verification_archives: tuple[dict, ...] = ()


def _decode_sina_rows(encoded: str) -> list[dict]:
    """Decode only data, with the audited decoder shipped by optional AkShare."""
    global _DECODER
    with _DECODE_LOCK:
        if _DECODER is None:
            import py_mini_racer
            from akshare.stock.cons import hk_js_decode

            _DECODER = py_mini_racer.MiniRacer()
            _DECODER.eval(hk_js_decode)
        rows = _DECODER.call("d", encoded, timeout=5000)
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError("新浪日线解码结果不是有界行情数组")
    return rows


def _get_text(client: httpx.Client, url: str, params: dict | None = None) -> str:
    """Bound response size and retry one transient transport/service failure."""
    for attempt in range(2):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if url == EASTMONEY_VERIFY_URL:
                headers["Referer"] = "https://emweb.securities.eastmoney.com/"
            with client.stream("GET", url, params=params, headers=headers) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES:
                        raise ValueError("日线响应超过允许大小")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8-sig")
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            if attempt:
                raise
        except httpx.HTTPStatusError as exc:
            if attempt or exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                raise
    raise RuntimeError("日线数据请求失败")


# 腾讯 WAF 熔断: WAF 拦截 (403/429/501) 会刷新封禁窗口, 被拦期间继续请求
# 既浪费配额又延长封禁。连续 N 次 WAF 状态后打开熔断, 冷却期内直接本地
# 快速失败 (不打网络); 冷却结束后的第一个请求是半开探测, 失败则翻倍冷却
# (15 → 30 → 60 分钟封顶), 成功则完全复位。进程内全实例共享。
_TENCENT_WAF_STATUS = frozenset({403, 429, 501})
_TENCENT_FAILURE_THRESHOLD = 5
_TENCENT_COOLDOWN_SECONDS = 15 * 60
_TENCENT_COOLDOWN_MAX_SECONDS = 60 * 60
_TENCENT_CIRCUIT_LOCK = threading.Lock()
_TENCENT_CIRCUIT = {"failures": 0, "opens": 0, "blocked_until": 0.0}


def _tencent_now() -> float:
    return time.monotonic()


def _reset_tencent_circuit() -> None:
    """Reset the shared breaker (test isolation; also safe in production)."""
    with _TENCENT_CIRCUIT_LOCK:
        _TENCENT_CIRCUIT.update(failures=0, opens=0, blocked_until=0.0)


def _tencent_get_text(client: httpx.Client, params: dict) -> str:
    """Route every Tencent fqkline call through the shared WAF circuit breaker."""
    with _TENCENT_CIRCUIT_LOCK:
        if _tencent_now() < _TENCENT_CIRCUIT["blocked_until"]:
            remaining = (_TENCENT_CIRCUIT["blocked_until"] - _tencent_now()) / 60
            raise ValueError(f"腾讯日 K 接口熔断中 (WAF 连续拦截, 冷却约 {remaining:.0f} 分钟)")
    try:
        text = _get_text(client, TENCENT_DAILY_URL, params)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _TENCENT_WAF_STATUS:
            with _TENCENT_CIRCUIT_LOCK:
                if _TENCENT_CIRCUIT["opens"]:
                    opens = _TENCENT_CIRCUIT["opens"] + 1  # 半开探测失败 → 翻倍冷却
                elif _TENCENT_CIRCUIT["failures"] + 1 >= _TENCENT_FAILURE_THRESHOLD:
                    opens = 1
                else:
                    _TENCENT_CIRCUIT["failures"] += 1
                    raise
                cooldown = min(
                    _TENCENT_COOLDOWN_SECONDS * 2 ** (opens - 1), _TENCENT_COOLDOWN_MAX_SECONDS,
                )
                _TENCENT_CIRCUIT.update(failures=0, opens=opens, blocked_until=_tencent_now() + cooldown)
        raise
    with _TENCENT_CIRCUIT_LOCK:
        if _TENCENT_CIRCUIT["opens"] or _TENCENT_CIRCUIT["failures"]:
            _TENCENT_CIRCUIT.update(failures=0, opens=0, blocked_until=0.0)
    return text


def _json_assignment(text: str, variable: str) -> Any:
    match = re.match(r"\s*var\s+" + re.escape(variable) + r"\s*=\s*", text)
    if match is None:
        raise ValueError("数据响应的证券身份不匹配")
    try:
        payload, index = json.JSONDecoder().raw_decode(text[match.end():])
    except (TypeError, ValueError) as exc:
        raise ValueError("数据响应不是安全 JSON") from exc
    trailing = text[match.end() + index:].strip().lstrip(";").strip()
    if trailing and not re.fullmatch(r"/\*[\s\S]*\*/", trailing):
        raise ValueError("数据响应包含额外可执行内容")
    return payload


def parse_sina_factors(
    text: str, symbol: str, coverage_end: date, observed_at: str,
) -> pl.DataFrame:
    """Convert cumulative qfq multipliers to pipeline pre/post event ratios.

    An entry at an ex-date applies from that date onward. The 1900 baseline
    is necessary: without it, the oldest observed event cannot be recovered.
    """
    symbol = normalize_market_symbols([symbol], "HK")[0]
    payload = _json_assignment(text, f"hk{symbol[:5]}qfq")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("复权因子快照为空或无效")
    values: dict[date, float] = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row["d"]))
            value = float(row["f"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("复权因子包含无效日期或比值") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError("复权因子必须为有限正数")
        if day in values and values[day] != value:
            raise ValueError("复权因子同日来源冲突")
        values[day] = value
    ordered = sorted(values.items())
    if ordered[0][0] != date(1900, 1, 1):
        raise ValueError("复权因子缺少全历史初始基准")
    canonical = json.dumps([(day.isoformat(), value) for day, value in ordered], separators=(",", ":"))
    version = hashlib.sha256(f"sina-cumulative-to-event-v1:{symbol}:{canonical}".encode()).hexdigest()
    result: list[dict] = []
    previous = ordered[0][1]
    for index, (day, value) in enumerate(ordered):
        result.append({
            "symbol": symbol, "trade_date": day,
            "ex_factor": 1.0 if index == 0 else value / previous,
            "source": "sina_hk_qfq", "observed_at": observed_at,
            "coverage_end": coverage_end, "version": version,
            "cumulative_factor": value,
        })
        previous = value
    return pl.DataFrame(result).with_columns(pl.col("ex_factor").cast(pl.Float64))


def _validated_rows(rows: list[dict], symbol: str, source: str, observed_at: str) -> pl.DataFrame:
    normalized: dict[date, dict] = {}
    dropped_bad_rows = 0
    for row in rows:
        if row.get("symbol", symbol) != symbol:
            raise ValueError("日线数据含其他证券代码")
        try:
            day = date.fromisoformat(str(row["date"])[:10])
            values = {name: float(row[name]) for name in ("open", "high", "low", "close", "volume")}
        except (KeyError, TypeError, ValueError):
            dropped_bad_rows += 1
            continue
        if day > datetime.now(HK_TZ).date():
            raise ValueError("日线返回未来交易日期")
        if not all(math.isfinite(value) for value in values.values()):
            dropped_bad_rows += 1
            continue
        if values["volume"] < 0:
            dropped_bad_rows += 1
            continue
        # Source halt rows are not traded bars; never turn a carried close into
        # a fictitious trade. Coverage below still reports an absent session.
        if values["open"] == 0 and values["high"] == 0:
            continue
        # The fixed Sina decoder occasionally introduces ~1e-5 floating error
        # at an OHLC bound. Reconcile only that documented precision, not an
        # economically inconsistent bar (which must fail and use the fallback).
        for name in ("open", "close"):
            if 0 < values["low"] - values[name] <= 0.00005:
                values[name] = values["low"]
            if 0 < values[name] - values["high"] <= 0.00005:
                values[name] = values["high"]
        if (min(values[name] for name in ("open", "high", "low", "close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])):
            # 09-15 抽样实证: 13/20 只港股的 sina 全历史含少量坏行 (汇丰 8 行/
            # 港交所 22 行级别), 单行坏即整只抛错会让 65% 标的 primary 全废、
            # 腾讯补位又被深历史 501 拦 → empty_result。改为剔除该行并计数,
            # 剔除日若为真实交易日, 由 fallback 源在 _merge_raw 天然补位;
            # 若两源皆无则 coverage 校验报告 missing (真停牌)。
            dropped_bad_rows += 1
            continue
        item = {"symbol": symbol, "date": day, **values, "amount": None,
                "source": source, "price_adjustment": "unadjusted", "amount_source": None,
                "volume_unit": "share", "currency": None,
                "price_schema_version": 1, "observed_at": observed_at, "raw_price_verified": True}
        # Neither raw endpoint's optional amount field has an independently
        # established contract; preserving null is safer than estimating it.
        if day in normalized and normalized[day] != item:
            raise ValueError("日线同源同日行情冲突")
        normalized[day] = item
    if not normalized:
        return pl.DataFrame()
    if dropped_bad_rows:
        logger.warning(
            "hk 日线 %s (%s): 剔除 %d 坏行 (占 %d 行), 待 fallback 补位或判停牌",
            symbol, source, dropped_bad_rows, len(rows),
        )
    return pl.DataFrame(list(normalized.values())).with_columns(
        pl.col("amount").cast(pl.Float64), pl.col("amount_source").cast(pl.String),
        pl.col("currency").cast(pl.String), pl.col("price_schema_version").cast(pl.Int64),
    ).sort("date")


def _as_plain_date(value: Any) -> date:
    """把 datetime 压成 date。

    ``isinstance(datetime, date)`` 恒为真, 但两者判等与哈希不等价 —— 这个坑
    在 regime_builder / verify 脚本 / market_daily merge 闸门里各踩过一次,
    这里统一收敛, 别再出现第四处。
    """
    return value.date() if isinstance(value, datetime) else value


def _raw_conflicts(primary: pl.DataFrame, fallback: pl.DataFrame) -> pl.DataFrame:
    if primary.is_empty() or fallback.is_empty():
        return pl.DataFrame()
    compared = primary.join(fallback, on=["symbol", "date"], suffix="_backup")
    if compared.is_empty():
        return compared
    return compared.filter(pl.any_horizontal([
        (pl.col(name) - pl.col(f"{name}_backup")).abs() > 0.0051
        for name in ("open", "high", "low", "close")
    ]) | ((pl.col("volume") - pl.col("volume_backup")).abs() > 1.0))


def _merge_raw(primary: pl.DataFrame, fallback: pl.DataFrame) -> pl.DataFrame:
    if primary.is_empty():
        return fallback
    if fallback.is_empty():
        return primary
    conflicts = _raw_conflicts(primary, fallback)
    if not conflicts.is_empty():
        overlap = primary.join(fallback, on=["symbol", "date"]).height
        # 09-15 实测 00005.HK: 84 行重叠仅 1 行开盘差 0.9 (量一致, 源间精度
        # 口径差异), 单行冲突即整只拒绝会重演"单行坏废整只"。少量冲突行
        # (≤3 行且 ≤5%) 剔除该日 (宁缺毋滥, coverage 报 missing 走维护);
        # 大量冲突仍抛错 —— 那才是真的有源坏了, 必须停下来人工核。
        if overlap and len(conflicts) <= 3 and len(conflicts) / overlap <= 0.05:
            # datetime 是 date 的子类但判等/哈希不等价, 落到 is_in 会静默不匹配;
            # 统一压成纯 date, 避免"剔除没生效、冲突日照写"的静默失败。
            bad_days = sorted(_as_plain_date(day) for day in conflicts["date"].to_list())
            logger.warning(
                "hk 原始日线重叠区 %d 行中 %d 行源间差异, 取主源口径: %s",
                overlap, len(conflicts), [str(day) for day in bad_days],
            )
            # 只从 fallback 剔除: 主源整行 (含已交叉一致的 high/low/close/volume)
            # 保留, coverage 不报缺, enriched 不会因单日差异停算。
            fallback = fallback.filter(~pl.col("date").is_in(bad_days))
        else:
            raise ValueError("raw_source_conflict")
    return pl.concat([fallback, primary], how="diagonal_relaxed").unique(
        ["symbol", "date"], keep="last",
    ).sort("date")


_OHLCV_FIELDS = ("open", "high", "low", "close", "volume")


def _ohlcv_matches(first: dict, second: dict) -> bool:
    return all(abs(first[name] - second[name]) <= (1.0 if name == "volume" else 0.0051)
               for name in _OHLCV_FIELDS)


def _eastmoney_verification_frame(raw_response: str, symbol: str, observed_at: str) -> pl.DataFrame:
    payload = json.loads(raw_response.lstrip("\ufeff"))
    if not isinstance(payload, dict):
        raise ValueError("第三来源核验响应不是行情对象")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("第三来源核验行情内容无效")
    if payload.get("rc", 0) != 0 or str(data.get("code")) != symbol[:5] or data.get("market") != 116:
        raise ValueError("第三来源的证券或市场身份不匹配")
    entries = data.get("klines")
    if not isinstance(entries, list) or not entries or len(entries) >= 1000:
        raise ValueError("第三来源核验窗口为空或被截断")
    rows: list[dict] = []
    for entry in entries:
        values = str(entry).split(",")
        if len(values) < 6:
            raise ValueError("第三来源缺少原始 OHLCV")
        rows.append(dict(zip(("date", "open", "close", "high", "low", "volume"), values[:6], strict=True)))
    frame = _validated_rows(rows, symbol, "eastmoney_hk_daily_check", observed_at)
    if frame.is_empty():
        raise ValueError("第三来源核验窗口没有有效交易日")
    return frame


def build_hk_raw_verification_archive(
    symbol: str, *, raw_response: bytes | str, source_url: str, observed_at: str,
) -> dict:
    """Validate an explicit archive import without trusting supplied row summaries."""
    symbol = normalize_market_symbols([symbol], "HK")[0]
    url = urlsplit(source_url)
    expected = urlsplit(EASTMONEY_VERIFY_URL)
    params = parse_qs(url.query)
    if (url.scheme != expected.scheme or url.netloc != expected.netloc or url.path != expected.path
            or params.get("secid") != [f"116.{symbol[:5]}"] or params.get("fqt") != ["0"]
            or params.get("klt") != ["101"]
            or params.get("fields2", [""])[0].split(",")[:6] != ["f51", "f52", "f53", "f54", "f55", "f56"]):
        raise ValueError("核验资料 URL 没有确认证券身份和原始 OHLCV 口径")
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed.tzinfo is None or observed > datetime.now(UTC):
        raise ValueError("核验资料观察时刻须带时区且不能来自未来")
    content = raw_response if isinstance(raw_response, bytes) else raw_response.encode("utf-8")
    if len(content) > _MAX_RESPONSE_BYTES:
        raise ValueError("核验资料超过允许大小")
    body = content.decode("utf-8")
    frame = _eastmoney_verification_frame(body, symbol, observed_at)
    if frame["date"].max() > observed.astimezone(HK_TZ).date():
        raise ValueError("核验资料包含观察时刻之后的行情")
    return {"schema_version": 1, "symbol": symbol, "source": "eastmoney_hk_daily_check",
            "source_url": source_url, "observed_at": observed_at,
            "response_sha256": hashlib.sha256(content).hexdigest(), "raw_response": body}


def _validated_verification_archive(archive: dict, symbol: str) -> pl.DataFrame:
    if (not isinstance(archive, dict) or archive.get("schema_version") != 1 or archive.get("symbol") != symbol
            or archive.get("source") != "eastmoney_hk_daily_check"):
        raise ValueError("核验资料版本、证券或来源不匹配")
    verified = build_hk_raw_verification_archive(
        symbol, raw_response=archive["raw_response"], source_url=archive["source_url"],
        observed_at=archive["observed_at"],
    )
    if verified["response_sha256"] != archive.get("response_sha256"):
        raise ValueError("核验资料原始回包 hash 不匹配")
    return _eastmoney_verification_frame(archive["raw_response"], symbol, archive["observed_at"])


class HKDailyProvider:
    """Two exchange-raw sources; adjustment availability remains independent."""

    name = "hk_daily"
    capabilities = ProviderCapabilities(daily=True, adj_factor=True)

    def __init__(self, transport: httpx.BaseTransport | None = None, timeout: float = 10.0) -> None:
        self.transport = transport
        self.timeout = max(0.01, float(timeout))

    def _sina(
        self, client: httpx.Client, symbol: str, observed_at: str,
        start: date | None = None, end: date | None = None,
    ) -> pl.DataFrame:
        text = _get_text(client, SINA_DAILY_URL.format(code=symbol[:5]))
        variable = f"KLC_K2_{symbol[:5]}"
        # Older versions of the same documented file use this variable name.
        if re.match(r"\s*var\s+KLC_KL_hk", text):
            variable = f"KLC_KL_hk{symbol[:5]}"
        encoded = _json_assignment(text, variable)
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("新浪原始日线为空")
        decoded = _decode_sina_rows(encoded)
        source_first = min((date.fromisoformat(str(row["date"])[:10]) for row in decoded), default=None)
        selected = [row for row in decoded if (start is None or str(row["date"])[:10] >= start.isoformat())
                    and (end is None or str(row["date"])[:10] <= end.isoformat())]
        frame = _validated_rows(selected, symbol, "sina_hk_daily", observed_at)
        return frame.with_columns(pl.lit(source_first, dtype=pl.Date).alias("source_first_date")) if not frame.is_empty() else frame

    def _tencent(
        self, client: httpx.Client, symbol: str, start: date, end: date, observed_at: str,
    ) -> tuple[pl.DataFrame, str | None]:
        frames: list[pl.DataFrame] = []
        currencies: set[str] = set()
        current = start
        while current <= end:
            # An individual request is bounded below Tencent's 640-bar limit.
            finish = min(end, current + timedelta(days=365))
            code = f"hk{symbol[:5]}"
            payload = json.loads(_tencent_get_text(
                client, {"param": f"{code},day,{current},{finish},640,"}))
            if payload.get("code") != 0:
                raise ValueError("腾讯日线返回失败状态")
            node = (payload.get("data") or {}).get(code)
            if not isinstance(node, dict) or "day" not in node:
                raise ValueError("腾讯未返回可核实的原始 day 日线")
            raw_rows = node["day"]
            if not isinstance(raw_rows, list) or len(raw_rows) >= 640:
                raise ValueError("腾讯日线窗口被截断")
            rows: list[dict] = []
            for entry in raw_rows:
                if not isinstance(entry, list) or len(entry) < 6:
                    raise ValueError("腾讯日线 OHLCV 字段不全")
                rows.append(dict(zip(("date", "open", "close", "high", "low", "volume"), entry[:6], strict=True)))
            frame = _validated_rows(rows, symbol, "tencent_hk_daily", observed_at)
            if not frame.is_empty():
                frames.append(frame.filter(pl.col("date").is_between(current, finish)))
            quote = (node.get("qt") or {}).get(code) or []
            if quote and len(quote) > 2 and quote[2] != symbol[:5]:
                raise ValueError("腾讯报价证券身份不匹配")
            if len(quote) > 75:
                currency = str(quote[75]).strip().upper()
                currency = "CNY" if currency == "RMB" else currency
                if currency in {"HKD", "CNY", "USD"}:
                    currencies.add(currency)
            current = finish + timedelta(days=1)
        if len(currencies) > 1:
            raise ValueError("腾讯证券币种冲突")
        currency = next(iter(currencies), None)
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        return frame, currency

    def _calendar(self, client: httpx.Client, start: date, end: date) -> set[date]:
        """Use actual HSI sessions, without interpreting index quantities."""
        current = start
        sessions: set[date] = set()
        while current <= end:
            finish = min(end, current + timedelta(days=365))
            payload = json.loads(_tencent_get_text(
                client, {"param": f"hkHSI,day,{current},{finish},640,"}))
            node = (payload.get("data") or {}).get("hkHSI")
            if payload.get("code") != 0 or not isinstance(node, dict) or not isinstance(node.get("day"), list):
                raise ValueError("港股交易日历来源无效")
            if len(node["day"]) >= 640:
                raise ValueError("港股交易日历窗口被截断")
            for row in node["day"]:
                day = date.fromisoformat(str(row[0]))
                if current <= day <= finish:
                    sessions.add(day)
            current = finish + timedelta(days=1)
        if not sessions:
            raise ValueError("港股交易日历区间为空")
        return sessions

    def _verify_conflicts(
        self, client: httpx.Client, symbol: str, primary: pl.DataFrame,
        fallback: pl.DataFrame, observed_at: str, archives: list[dict] | None = None,
    ) -> tuple[pl.DataFrame, list[dict], list[dict]]:
        """Resolve a conflicting row only with independent full-OHLCV evidence."""
        conflicts = _raw_conflicts(primary, fallback)
        dates = sorted(conflicts["date"].to_list())
        verified_rows: dict[date, dict] = {}
        provenance: dict[date, dict] = {}
        saved_rows: dict[date, list[tuple[dict, dict]]] = {}
        for archive in archives or []:
            if archive.get("symbol") != symbol:
                continue
            try:
                cached_frame = _validated_verification_archive(archive, symbol)
            except (KeyError, TypeError, ValueError):
                # An invalid or edited archive never establishes a price. The
                # live verification below still has to prove the whole row.
                continue
            for row in cached_frame.to_dicts():
                if row["date"] in dates:
                    saved_rows.setdefault(row["date"], []).append((row, archive))
        for row in conflicts.to_dicts():
            day = row["date"]
            saved = saved_rows.get(day, [])
            if not saved:
                continue
            first = {name: row[name] for name in _OHLCV_FIELDS}
            second = {name: row[f"{name}_backup"] for name in _OHLCV_FIELDS}
            third, archive = saved[0]
            if (any(not _ohlcv_matches(third, candidate) for candidate, _ in saved[1:])
                    or _ohlcv_matches(first, third) == _ohlcv_matches(second, third)):
                continue
            verified_rows[day] = third
            provenance[day] = {name: archive[name] for name in ("source_url", "observed_at", "response_sha256")}
            provenance[day]["verification_cached"] = True
        missing = [day for day in dates if day not in verified_rows]
        fetched_archives: list[dict] = []
        for year in sorted({day.year for day in missing}):
            selected_dates = [day for day in missing if day.year == year]
            params = {
                "secid": f"116.{symbol[:5]}", "klt": "101", "fqt": "0",
                "beg": min(selected_dates).strftime("%Y%m%d"), "end": max(selected_dates).strftime("%Y%m%d"),
                "lmt": "1000", "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            }
            body = _get_text(client, EASTMONEY_VERIFY_URL, params)
            archive = build_hk_raw_verification_archive(
                symbol, raw_response=body, source_url=str(httpx.URL(EASTMONEY_VERIFY_URL, params=params)),
                observed_at=observed_at,
            )
            frame = _eastmoney_verification_frame(body, symbol, observed_at)
            fetched_archives.append(archive)
            verified_rows.update({row["date"]: row for row in frame.to_dicts() if row["date"] in selected_dates})
            for day in selected_dates:
                provenance[day] = {name: archive[name] for name in ("source_url", "observed_at", "response_sha256")}
                provenance[day]["verification_cached"] = False
        resolved: list[dict] = []
        evidence: list[dict] = []
        for row in conflicts.to_dicts():
            day = row["date"]
            third = verified_rows.get(day)
            if third is None:
                raise ValueError("第三来源缺少争议交易日, 不能选择价格")
            first = {name: row[name] for name in _OHLCV_FIELDS}
            second = {name: row[f"{name}_backup"] for name in _OHLCV_FIELDS}
            first_matches, second_matches = _ohlcv_matches(first, third), _ohlcv_matches(second, third)
            if first_matches == second_matches:
                raise ValueError("第三来源未唯一确认争议日的完整原始 OHLCV")
            source_frame = primary if first_matches else fallback
            chosen = source_frame.filter(pl.col("date") == day).row(0, named=True)
            chosen["verification_source"] = "eastmoney_hk_daily_check"
            resolved.append(chosen)
            evidence.append({"date": day.isoformat(), "primary": first, "fallback": second,
                             "verification": {name: third[name] for name in _OHLCV_FIELDS},
                             "verification_source": "eastmoney_hk_daily_check", "selected_source": chosen["source"],
                             **provenance[day]})
        remaining = _merge_raw(primary.filter(~pl.col("date").is_in(dates)), fallback.filter(~pl.col("date").is_in(dates)))
        frames = [remaining, pl.DataFrame(resolved)] if not remaining.is_empty() else [pl.DataFrame(resolved)]
        return pl.concat(frames, how="diagonal_relaxed").sort("date"), evidence, fetched_archives

    def get_daily_with_report(
        self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock", *, verification_archives: list[dict] | None = None,
    ) -> DailyFetchResult:
        if asset_type != "stock":
            raise ValueError("港股日线适配器仅支持证券日线")
        selected = normalize_market_symbols(symbols, "HK")
        end = (end_time.date() if end_time else datetime.now(HK_TZ).date())
        start = start_time.date() if start_time else end - timedelta(days=365)
        if start > end:
            raise ValueError("起始日期不能晚于截止日期")
        frames: list[pl.DataFrame] = []
        adjustments: list[pl.DataFrame] = []
        items: list[dict] = []
        new_archives: list[dict] = []
        calendars: dict[tuple[date, date], set[date]] = {}
        with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=True) as client:
            for symbol in selected:
                observed_at = datetime.now(UTC).isoformat()
                item: dict = {"symbol": symbol, "status": "failed", "reason": None,
                              "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                              "observed_at": observed_at, "attempted_sources": [], "fallback_used": False,
                              "raw_updated": False, "enriched_updated": False}
                primary = pl.DataFrame()
                fallback = pl.DataFrame()
                source_errors: dict[str, str] = {}
                primary_first: date | None = None
                try:
                    item["attempted_sources"].append("sina_hk_daily")
                    primary = self._sina(client, symbol, observed_at, start, end)
                    if not primary.is_empty():
                        primary_first = primary["source_first_date"][0]
                        primary = primary.drop("source_first_date")
                        primary = primary.filter(pl.col("date").is_between(start, end))
                except Exception as exc:
                    source_errors["sina_hk_daily"] = _source_error(exc)
                currency: str | None = None
                try:
                    item["attempted_sources"].append("tencent_hk_daily")
                    # 腾讯补位窗口只拉近 120 天: 深历史段 fqkline 会被 WAF 501 拒
                    # (09-15 实测 1998 起段 100% 501), 而 sina primary 已覆盖历史
                    # 主体。腾讯只需提供: 币种 (quote[75], 近段就有) + 重叠段交叉
                    # 核验 + 近段补位。sina 剔除的深历史坏行由 coverage 校验报
                    # missing 走后续维护, 不再赌腾讯深历史。primary 整体失败时
                    # 腾讯仍是唯一来源, 保持原完整窗口。
                    if primary.is_empty():
                        fallback_start = start
                    else:
                        fallback_start = max(start, end - timedelta(days=120))
                    fallback, currency = self._tencent(client, symbol, fallback_start, end, observed_at)
                except Exception as exc:
                    source_errors["tencent_hk_daily"] = _source_error(exc)
                try:
                    frame = _merge_raw(primary, fallback)
                except ValueError:
                    try:
                        frame, evidence, fetched_archives = self._verify_conflicts(
                            client, symbol, primary, fallback, observed_at, verification_archives,
                        )
                        new_archives.extend(fetched_archives)
                        item.update(verification_source="eastmoney_hk_daily_check", source_conflicts=evidence,
                                    verification_cached=any(row["verification_cached"] for row in evidence))
                    except Exception as exc:
                        item.update(reason="两个原始日线来源存在冲突, 独立来源未能唯一核实整行行情, 已停止发布",
                                    reason_code="raw_source_conflict", verification_error=_source_error(exc))
                        items.append(item)
                        continue
                if frame.is_empty():
                    item.update(reason="主备来源均未返回请求区间内的有效原始日线", reason_code="empty_result", source_errors=source_errors)
                    items.append(item)
                    continue
                expected_start = max(start, primary_first or start)
                clock = datetime.now(HK_TZ)
                closed_end = clock.date() if clock.hour >= 17 else clock.date() - timedelta(days=1)
                expected_end = min(end, closed_end)
                # 只允许"已收盘会话"落盘: 新浪/腾讯盘中都会返回当日未完成 K 线, 一旦
                # 落盘会与次日"已收盘口径"的复权快照冲突 (快照 coverage_end 只到前一日),
                # 使该标的永久卡死 (09-16 实证 00005.HK 残留 2026-09-16 占位行)。
                # 原实现仅在交易日历成功时才裁剪; 腾讯 501 时日历不可用, 裁剪被跳过,
                # 占位行趁虚而入。此处改为无条件按 closed_end 裁剪。
                if expected_end >= expected_start:
                    completed = frame.filter(pl.col("date") <= expected_end)
                    if completed.height != frame.height:
                        logger.warning(
                            "hk 日线 %s: 剔除 %d 行未收盘当日 K 线 (仅保留 <= %s 的已完成交易日)",
                            symbol, frame.height - completed.height, expected_end,
                        )
                    frame = completed
                    if frame.is_empty():
                        item.update(reason="请求区间内没有已收盘的原始日线", reason_code="empty_result", source_errors=source_errors)
                        items.append(item)
                        continue
                actual_start, actual_end = frame["date"].min(), frame["date"].max()
                sources = frame["source"].unique().sort().to_list()
                item.update(source="+".join(sources), daily_sources=sources,
                            fallback_used="tencent_hk_daily" in sources,
                            actual_start=actual_start.isoformat(), actual_end=actual_end.isoformat(),
                            currency=currency, volume_unit="share", price_adjustment="unadjusted",
                            source_errors=source_errors)
                coverage_ok = False
                try:
                    calendar_key = (expected_start, expected_end)
                    if calendar_key not in calendars:
                        calendars[calendar_key] = self._calendar(client, expected_start, expected_end)
                    expected = calendars[calendar_key]
                    missing = sorted(expected - set(frame["date"].to_list()))
                    item.update(calendar_source="tencent_hsi_sessions", missing_dates=[day.isoformat() for day in missing], missing_dates_count=len(missing))
                    coverage_ok = not missing
                    if not missing and expected:
                        # Only completed sessions can become daily research bars.
                        frame = frame.filter(pl.col("date") <= max(expected))
                        actual_end = frame["date"].max()
                        item["actual_end"] = actual_end.isoformat()
                except Exception as exc:
                    item["calendar_error"] = _source_error(exc)
                item["coverage_complete"] = coverage_ok
                item["status"] = "ok" if coverage_ok else "partial"
                if not coverage_ok:
                    item.update(reason="实际交易日期未覆盖请求边界; 节假日、停牌或源缺口尚未确认", reason_code="coverage_incomplete")
                factors = pl.DataFrame()
                try:
                    text = _get_text(client, SINA_FACTOR_URL.format(code=symbol[:5]))
                    factors = parse_sina_factors(text, symbol, actual_end, observed_at)
                    adjustments.append(factors)
                    item.update(adjustment_source="sina_hk_qfq", adjustment_version=factors["version"][0],
                                adjustment_as_of=actual_end.isoformat())
                except Exception as exc:
                    item.update(status="partial", reason="原始日线已取得, 但复权因子不可用; 需要已验证且覆盖该区间的快照", reason_code="adjustment_unavailable", adjustment_error=_source_error(exc))
                frame = frame.with_columns(
                    pl.lit(currency, dtype=pl.String).alias("currency"),
                    pl.lit(item.get("adjustment_source"), dtype=pl.String).alias("adjustment_source"),
                    pl.lit(item.get("adjustment_version"), dtype=pl.String).alias("adjustment_version"),
                    pl.lit(actual_end if not factors.is_empty() else None, dtype=pl.Date).alias("adjustment_as_of"),
                )
                frames.append(frame)
                items.append(item)
        return DailyFetchResult(
            pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame(),
            tuple(items), pl.concat(adjustments, how="diagonal_relaxed") if adjustments else pl.DataFrame(),
            tuple(new_archives),
        )

    def get_daily(
        self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        return self.get_daily_with_report(symbols, start_time, end_time, asset_type).frame

    def get_adj_factors(
        self, symbols: list[str], start_time: datetime | None, end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        return self.get_daily_with_report(symbols, start_time, end_time, asset_type).adjustments

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        raise NotImplementedError("港股证券资料由独立证券数据源提供")

    def get_minute(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        raise NotImplementedError("此数据源不提供分钟行情")

    def get_realtime(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        raise NotImplementedError("此数据源不提供实时行情")
