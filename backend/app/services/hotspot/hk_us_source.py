"""港美行业热点数据源 (hk / us)。

背景:akshare 没有任何港美板块/概念接口 (``board_*`` 全是 A 股, 美股为 0),
所以港美不走"外部板块列表"路线, 改为**本地 instruments 行业分类 x 行情聚合**:

    topic   = ``{market}_instruments.parquet`` 的 ``industry`` 字段
              (港股 31 个行业 / 美股 111 个 >=10 只的细分行业)
    成分股  = 该行业下的 instruments, 取有行情的标的
    涨跌幅  = 成分股**等权平均**涨幅 (与 A 股东财板块涨跌幅口径一致)
    热度    = 复用 ``compute_board_heat_score`` (涨幅 + 排名), 与 A 股板块同口径

行情双路 (用户拍板: 盘中实时, 盘后/失败回落日K):
  1. 盘中 (交易时段内) → ``create_market_realtime_provider`` 批量实时;
     港股走 ``fetch_quotes_batch_sync`` (腾讯优先/新浪补漏, 内部并发分片)。
  2. 盘后 / 实时失败 → ``kline_hk_us_enriched`` 最新交易日 parquet 快照。
  无论哪一路都在 ``provider_used`` 里标注 ``<name>:<mode>``, 并把快照日期
  写进 ``topic_date``。快照日期 != 市场当地今天 → ``stale=True``: 有数据照出,
  但绝不把陈旧数据伪装成实时 (service 会据此把 quality 收敛成 stale)。

字段可得性 (港美与 A 股差异, 一律显式表达, 不推断):
  - ``turnover_rate`` / ``net_inflow`` / ``active_days``: 无数据源 → None, 进 missing_fields。
  - ``is_limit_up``: 港美**无涨跌停制度** (market profile ``has_price_limit()=False``),
    置 False 是"不适用"而非"没查到", 不进 missing_fields。
  - ``volume_ratio``: 日K路取 enriched 的 ``vol_ratio_5d`` (5 日量比, 真实值);
    实时路不可得 → None。口径差异记在 ``HotspotStock.source`` 里。
  - ``amount``: 实时/日K 均可得 (币种 HKD / USD, 与 A 股元不同量纲, 只对内排序用)。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from time import monotonic
from typing import Any

from app.services.hotspot.models import (
    QUALITY_OK,
    QUALITY_PARTIAL,
    HotspotDetail,
    HotspotResults,
    HotspotStock,
    HotspotSummary,
    SourceError,
    utc_now_iso,
)
from app.services.hotspot.scoring import (
    assign_roles,
    classify_stage,
    compute_board_heat_score,
    safe_float,
    safe_text,
)
from app.services.hotspot.source import HotspotSource

logger = logging.getLogger(__name__)

# 行业成分股门槛: 太小的行业平均涨幅噪声大, 不进入热点榜
MIN_MEMBERS = 10

# 实时行情单次批量上限 (腾讯/新浪约 50, 美股 provider 亦分片)
REALTIME_BATCH = 50

# 美股实时: 腾讯批量实测 200 只/次稳定, 全市场约 30 次请求
US_REALTIME_BATCH = 200
# 实时路硬闸门: 超时即回落到 enriched 日K(宁可 stale 也不让请求挂死)
REALTIME_DEADLINE_S = 25.0
REALTIME_TIMEOUT_S = 8.0

# 港美不可得字段 (进 missing_fields, 前端渲染 '—')
_MISSING_FIELDS = ["turnover_rate", "net_inflow", "active_days"]


def _pct_from_quote_row(row: dict[str, Any]) -> float | None:
    """行情行 → 小数制涨跌幅 (0.0266 = +2.66%), 与 enriched 日K 同口径。

    优先用 last_price / prev_close 现算: 腾讯/新浪的 change_pct 是百分数,
    直接塞进聚合会比日K 路径小 100 倍。
    """
    last = safe_float(row.get("last_price") or row.get("price") or row.get("close"))
    prev = safe_float(row.get("prev_close"))
    if last is not None and prev not in (None, 0) and prev > 0 and last > 0:
        return last / prev - 1.0
    pct = safe_float(row.get("change_pct"))
    if pct is None:
        return None
    # 走到这里说明没有 prev_close 可算; 当前美股实时只接腾讯/新浪(百分数), 统一 /100
    return pct / 100.0

_MARKET_SUFFIX = {"hk": ".HK", "us": ".US"}
_PROFILE_MARKET = {"hk": "HK", "us": "US"}


def _industry_key(value: Any) -> str:
    """行业名归一化: 去空白, 空值/占位符返回 ''。"""
    text = safe_text(value).strip()
    if text.lower() in {"", "nan", "none", "null", "-"}:
        return ""
    return text


def _is_in_session(market: str, now: datetime | None = None) -> bool:
    """是否在交易时段 (用市场档案的 tz + sessions 判定)。

    只判"当前本地时间是否落在某个 session 内", 不判节假日 —— 节假日最多让
    热点退回上一交易日快照并标 stale, 不会误报实时数据。
    """
    try:
        from app.markets.registry import get_profile

        profile = get_profile(_PROFILE_MARKET.get(market, market.upper()))
    except Exception:  # 档案不可用时保守走日K路径
        return False
    current = now or profile.now()
    if current.weekday() >= 5:
        return False
    current_time = current.time()
    for session in getattr(profile, "sessions", ()) or ():
        start = getattr(session, "start", None)
        end = getattr(session, "end", None)
        if isinstance(start, dt_time) and isinstance(end, dt_time) and start <= current_time < end:
            return True
    return False


class HkUsIndustryHotspotSource(HotspotSource):
    """港美行业热点源: instruments industry 分组 + 行情聚合。

    依赖全部可注入, 便于测试离线跑:
      - ``instruments_loader(market)`` -> list[dict] (symbol/name/industry)
      - ``quote_loader(market, symbols)`` -> tuple[list[dict], mode, as_of]
    """

    name = "hkus_industry"

    def __init__(
        self,
        data_dir: Path | str | None = None,
        *,
        instruments_loader: Callable[[str], Sequence[dict[str, Any]]] | None = None,
        quote_loader: Callable[[str, list[str]], tuple[Sequence[dict[str, Any]], str, str]] | None = None,
        now_fn: Callable[[str], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else None
        self._instruments_loader = instruments_loader
        self._quote_loader = quote_loader
        self._now_fn = now_fn
        self._rows_by_market: dict[str, list[dict[str, Any]]] = {}
        self._quotes: dict[str, dict[str, dict[str, Any]]] = {}
        self._mode: dict[str, str] = {}
        self._as_of: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 协议
    # ------------------------------------------------------------------

    def supports(self, market: str) -> bool:
        return market in _MARKET_SUFFIX

    def discover(self, *, market: str = "hk", top: int = 20) -> HotspotResults:
        if not self.supports(market):
            err = SourceError(
                provider=self.name, method="discover",
                message=f"market '{market}' unsupported by hk/us industry source",
            )
            return HotspotResults([], provider_used=self.name, source_errors=[err.as_str()], market=market)

        rows = self._load_instruments(market)
        if not rows:
            err = SourceError(
                provider=self.name, method="instruments",
                message=f"no instruments with industry for market '{market}'",
            )
            return HotspotResults([], provider_used=self.name, source_errors=[err.as_str()], market=market)

        symbols = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
        quotes, mode, as_of = self._load_quotes(market, symbols)
        self._mode[market] = mode
        self._as_of[market] = as_of
        if not quotes:
            err = SourceError(
                provider=self.name, method="quotes",
                message=f"no quotes available for market '{market}'",
            )
            return HotspotResults([], provider_used=f"{self.name}:{mode}", source_errors=[err.as_str()], market=market)

        summaries = self._aggregate(
            rows, quotes, market=market, mode=mode, as_of=as_of, today=self._today(market),
        )
        cap = max(int(top), 0)
        used = summaries[:cap] if cap else summaries

        # 容器级 quality 由 HotspotResults 按条目 missing_fields/stale 推导, 不在此处兜底
        return HotspotResults(
            used,
            provider_used=f"{self.name}:{mode}",
            source_errors=[],
            market=market,
        )

    def fetch_detail(
        self,
        topic: str,
        *,
        market: str = "hk",
        top_stocks: int = 10,
    ) -> HotspotDetail | None:
        industry = _industry_key(topic)
        if not industry or not self.supports(market):
            return None

        rows = self._load_instruments(market)
        quotes, mode, as_of = self._load_quotes(
            market, [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
        )
        members = [
            r for r in rows
            if _industry_key(r.get("industry")) == industry and str(r.get("symbol") or "") in quotes
        ]
        if len(members) < MIN_MEMBERS:
            return None

        stocks = self._build_stocks(members, quotes, mode=mode, limit=max(int(top_stocks), 0))
        summary = self._summarize(
            industry, members, quotes, rank=None, market=market, mode=mode, as_of=as_of,
            today=self._today(market),
        )
        summary.sample_stock_count = len(members)
        summary.leader_stocks = stocks[:3]
        summary.leaders = [s.name or s.code for s in stocks[:3]]
        return HotspotDetail(summary=summary, stocks=stocks, stock_count=len(members))

    # ------------------------------------------------------------------
    # 数据加载 (可注入)
    # ------------------------------------------------------------------

    def _load_instruments(self, market: str) -> list[dict[str, Any]]:
        """读 instruments 的行业分类; 无 industry 的标的直接丢弃。"""
        if market in self._rows_by_market:
            return self._rows_by_market[market]
        if self._instruments_loader is not None:
            raw = list(self._instruments_loader(market) or [])
        else:
            raw = self._read_instruments_parquet(market)
        rows = []
        for item in raw:
            industry = _industry_key(item.get("industry"))
            symbol = safe_text(item.get("symbol"))
            if not industry or not symbol:
                continue
            rows.append({"symbol": symbol, "name": safe_text(item.get("name")) or symbol, "industry": industry})
        self._rows_by_market[market] = rows
        return rows

    def _read_instruments_parquet(self, market: str) -> list[dict[str, Any]]:
        if self.data_dir is None:
            return []
        path = Path(self.data_dir) / "instruments" / f"{market}_instruments.parquet"
        if not path.exists():
            logger.warning("hk_us hotspot instruments missing: %s", path)
            return []
        try:
            import polars as pl

            df = pl.read_parquet(path)
        except Exception as exc:  # 读盘失败按无数据处理, 不抛给上层
            logger.warning("hk_us hotspot instruments read failed: %s", exc)
            return []
        columns = {"symbol", "name", "industry"}
        if not columns.issubset(set(df.columns)):
            logger.warning("hk_us hotspot instruments missing columns: %s", df.columns)
            return []
        return df.select("symbol", "name", "industry").to_dicts()

    def _load_quotes(
        self, market: str, symbols: list[str]
    ) -> tuple[dict[str, dict[str, Any]], str, str]:
        """取行情: 盘中实时, 否则 enriched 日K 快照。

        Returns:
            (symbol -> quote dict, mode, as_of)
            mode: "realtime" | "daily" ; as_of: YYYY-MM-DD (实时取今天本地日期)
        """
        cached = self._quotes.get(market)
        if cached is not None:
            return cached, self._mode.get(market, "daily"), self._as_of.get(market, "")

        if self._quote_loader is not None:
            raw, mode, as_of = self._quote_loader(market, symbols)
            quotes = self._normalize_quotes(raw)
        else:
            quotes: dict[str, dict[str, Any]] = {}
            mode = "daily"
            as_of = ""
            if self._is_trading_now(market):
                quotes = self._fetch_realtime(market, symbols)
                if quotes:
                    mode = "realtime"
                    as_of = self._today(market)
            if not quotes:
                quotes, as_of = self._read_daily_quotes(market)
                mode = "daily"

        self._quotes[market] = quotes
        self._mode[market] = mode
        self._as_of[market] = as_of
        return quotes, mode, as_of

    def _is_trading_now(self, market: str) -> bool:
        now = self._now_fn(market) if self._now_fn else None
        return _is_in_session(market, now)

    def _today(self, market: str) -> str:
        now = self._now_fn(market) if self._now_fn else None
        if now is not None:
            return now.date().isoformat()
        try:
            from app.markets.registry import get_profile

            return get_profile(_PROFILE_MARKET.get(market, market.upper())).today().isoformat()
        except Exception:
            return ""

    @staticmethod
    def _normalize_quotes(raw: Sequence[dict[str, Any]] | Any) -> dict[str, dict[str, Any]]:
        """行情记录 → symbol -> row (统一 change_pct 小数制, 过滤极端脏数据)。"""
        out: dict[str, dict[str, Any]] = {}
        for row in raw or []:
            if not isinstance(row, dict):
                continue
            symbol = safe_text(row.get("symbol"))
            if not symbol:
                continue
            change = safe_float(row.get("change_pct"))
            if change is not None and abs(change) > 1.0:
                # 退市重组/数据断层会算出 8508% 的无效涨跌幅 (如 HOS), 直接剔除
                continue
            item = dict(row)
            item["symbol"] = symbol
            item["change_pct"] = change
            out[symbol] = item
        return out

    def _fetch_realtime(self, market: str, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """盘中批量实时; 任一片段失败都不抛, 交给日K兜底。"""
        if not symbols:
            return {}
        try:
            if market == "hk":
                return self._fetch_hk_realtime(symbols)
            return self._fetch_us_realtime(symbols)
        except Exception as exc:
            logger.warning("hk_us hotspot realtime fetch failed (%s): %s", market, exc)
            return {}

    @staticmethod
    def _fetch_hk_realtime(
        symbols: list[str],
        deadline_s: float = REALTIME_DEADLINE_S,
    ) -> dict[str, dict[str, Any]]:
        """港股实时(腾讯优先/新浪补漏)。

        与美股同款硬闸门: 分块检查 deadline, 超时就返回已取部分让上层回落日K,
        避免全市场(近 2800 只)在网络抖动时被无限拖住。
        """
        from app.data_providers.hk_quickquote_provider import fetch_quotes_batch_sync

        quotes: list[dict[str, Any]] = []
        deadline = monotonic() + max(deadline_s, 1.0)
        block = REALTIME_BATCH * 10
        for start in range(0, len(symbols), block):
            # 第一块总是放行(否则慢网络下永远回落到日K), 之后每块前查一次闸门
            if start > 0 and monotonic() > deadline:
                logger.warning(
                    "hk_us hotspot hk realtime deadline hit: %d/%d 已取, 回落日K",
                    len(quotes), len(symbols),
                )
                break
            quotes.extend(fetch_quotes_batch_sync(symbols[start:start + block], batch_size=REALTIME_BATCH) or [])
        out: dict[str, dict[str, Any]] = {}
        for q in quotes or []:
            symbol = safe_text(q.get("symbol"))
            if not symbol:
                continue
            out[symbol] = {
                "symbol": symbol,
                "name": safe_text(q.get("name")),
                "change_pct": safe_float(q.get("change_pct")),
                "amount": safe_float(q.get("amount")),
                "close": safe_float(q.get("price")),
                "vol_ratio_5d": safe_float(q.get("volume_ratio")),
            }
        return out

    @staticmethod
    def _fetch_us_realtime(
        symbols: list[str],
        deadline_s: float = REALTIME_DEADLINE_S,
    ) -> dict[str, dict[str, Any]]:
        """美股实时行情(批量)。

        历史坑: 这里原本走 ``create_market_realtime_provider("us")`` = yfinance,
        而 yfinance 是**逐只**请求(每只还要发多次 HTTP), 盘中把 5653 只行业成分股
        灌进去会挂死几十分钟, 前端拿不到任何响应 —— 热点页表现为"空白"。
        腾讯 ``qt.gtimg.cn/q=usAAPL,usMSFT,...`` 支持逗号拼接批量:
        实测 200 只 0.11s / 覆盖率 100%, 全市场约 30 次请求即完成。

        单位: 腾讯返回 change_pct 是百分数(-0.71 = -0.71%), enriched 日K 是小数
        (0.0266 = +2.66%)。这里统一用 last_price/prev_close 现算, 避免口径错位。

        拿不到/超时 → 返回空 dict, 由上层回落到 enriched 日K(宁可标 stale 也不挂死)。
        """
        try:
            from app.data_providers.tencent_market_provider import TencentMultiMarketProvider
        except Exception as exc:  # 导入失败等同拉取失败
            logger.warning("hk_us hotspot us realtime provider unavailable: %s", exc)
            return {}

        provider = TencentMultiMarketProvider("us", timeout=REALTIME_TIMEOUT_S)
        out: dict[str, dict[str, Any]] = {}
        deadline = monotonic() + max(deadline_s, 1.0)
        try:
            for start in range(0, len(symbols), US_REALTIME_BATCH):
                if monotonic() > deadline:
                    logger.warning(
                        "hk_us hotspot us realtime deadline hit: %d/%d 已取, 回落日K",
                        len(out), len(symbols),
                    )
                    return {}
                batch = symbols[start:start + US_REALTIME_BATCH]
                try:
                    df = provider.get_realtime(symbols=batch)
                except Exception as exc:
                    logger.warning("hk_us hotspot us realtime batch failed: %s", exc)
                    continue
                for row in df.to_dicts() if hasattr(df, "to_dicts") else []:
                    symbol = safe_text(row.get("symbol"))
                    if not symbol:
                        continue
                    out[symbol] = {
                        "symbol": symbol,
                        "name": safe_text(row.get("name")),
                        "change_pct": _pct_from_quote_row(row),
                        "amount": safe_float(row.get("amount")),
                        "close": safe_float(row.get("last_price") or row.get("close")),
                        "vol_ratio_5d": None,  # 实时路不可得(与文档口径一致)
                    }
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # 关闭失败不影响已取数据
                    pass
        return out

    def _read_daily_quotes(self, market: str) -> tuple[dict[str, dict[str, Any]], str]:
        """读 enriched 最新交易日快照。"""
        if self.data_dir is None:
            return {}, ""
        try:
            from app.services.hk_us_overview_builder import _load_latest_rows
        except Exception as exc:  # 复用失败不影响 stub 注入路径
            logger.warning("hk_us hotspot daily loader unavailable: %s", exc)
            return {}, ""
        try:
            df, as_of = _load_latest_rows(Path(self.data_dir), _PROFILE_MARKET.get(market, market.upper()))
        except Exception as exc:
            logger.warning("hk_us hotspot daily read failed: %s", exc)
            return {}, ""
        # 注意: polars 的 is_empty 是方法, getattr 取到的是 bound method (恒真), 必须判 height
        if df is None or getattr(df, "height", 0) == 0:
            return {}, ""
        out = self._normalize_quotes(df.to_dicts())
        return out, as_of.isoformat() if isinstance(as_of, date) else safe_text(as_of)

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def _aggregate(
        self,
        rows: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        *,
        market: str,
        mode: str,
        as_of: str,
        today: str = "",
    ) -> list[HotspotSummary]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row.get("symbol") or "")
            if symbol in quotes:
                groups.setdefault(str(row.get("industry")), []).append(row)

        summaries: list[HotspotSummary] = []
        for industry, members in groups.items():
            if len(members) < MIN_MEMBERS:
                continue
            summaries.append(self._summarize(
                industry, members, quotes, market=market, mode=mode, as_of=as_of, today=today,
            ))

        summaries.sort(key=lambda s: (-s.heat_score, s.topic))
        for index, summary in enumerate(summaries, start=1):
            summary.rank = index
            # rank 参与 heat_score, 先按未排名分数排序再回填最终分
            summary.heat_score = compute_board_heat_score(summary.change_pct, index)
        return summaries

    def _summarize(
        self,
        industry: str,
        members: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        *,
        market: str,
        mode: str,
        as_of: str,
        rank: int | None = None,
        today: str = "",
    ) -> HotspotSummary:
        changes: list[float] = []
        amount = 0.0
        for member in members:
            quote = quotes.get(str(member.get("symbol") or "")) or {}
            change = safe_float(quote.get("change_pct"))
            if change is not None:
                changes.append(change)
            amount += safe_float(quote.get("amount")) or 0.0

        change_pct = sum(changes) / len(changes) if changes else None
        leaders = self._build_stocks(members, quotes, mode=mode, limit=3)
        heat = compute_board_heat_score(change_pct, rank)
        total = len(members)

        return HotspotSummary(
            topic=industry,
            name=industry,
            source="industry",
            rank=rank,
            change_pct=change_pct,
            heat_score=heat,
            trend_score=None,      # 需要历史观测点, 首日无 → None (不伪造)
            persistence_score=None,
            cooling_score=None,
            observations=1,
            state="",
            stage=classify_stage(latest_score=heat, observations=1, trend_score=None, persistence_score=None, cooling_score=None),
            sample_stock_count=total,
            leaders=[s.name or s.code for s in leaders],
            leader_stocks=leaders,
            quality_status=QUALITY_OK if mode == "realtime" else QUALITY_PARTIAL,
            missing_fields=list(_MISSING_FIELDS),
            # 快照日期不是市场当地今天 → 显式 stale; 数据照出, 但绝不冒充实时
            stale=bool(as_of) and bool(today) and as_of != today,
            provider_used=f"{self.name}:{mode}",
            topic_date=as_of,
            snapshot_at=utc_now_iso(),
            snapshot_market=market,
        )

    def _build_stocks(
        self,
        members: list[dict[str, Any]],
        quotes: dict[str, dict[str, Any]],
        *,
        mode: str,
        limit: int,
    ) -> list[HotspotStock]:
        rows: list[HotspotStock] = []
        for member in members:
            symbol = str(member.get("symbol") or "")
            quote = quotes.get(symbol) or {}
            change = safe_float(quote.get("change_pct"))
            if change is None:
                continue
            rows.append(
                HotspotStock(
                    code=symbol,
                    name=safe_text(member.get("name")) or symbol,
                    change_pct=change,
                    amount=safe_float(quote.get("amount")),
                    turnover_rate=None,     # 港美无换手率数据源
                    volume_ratio=safe_float(quote.get("vol_ratio_5d")) if mode == "daily" else None,
                    net_inflow=None,        # 无主力净流数据源
                    is_limit_up=False,      # 港美无涨跌停制度 (不适用, 非不可得)
                    active_days=0,
                    evidence_count=0,
                    source=f"{self.name}:{mode}",
                    source_confidence=0.6,
                )
            )
        rows.sort(key=lambda s: s.change_pct or 0.0, reverse=True)
        if limit:
            rows = rows[:limit]
        # assign_roles 会回填 hot_stock_score 与 role (缺失维度记 0 分)
        return list(assign_roles(rows))

