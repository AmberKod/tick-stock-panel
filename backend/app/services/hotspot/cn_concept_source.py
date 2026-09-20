"""A 股概念/行业热点数据源(本地计算, 零外部请求)。

背景: A 股原先走 akshare 东财板块接口, 依赖 push2.eastmoney.com; 该域名在
代理环境下不可达, 实测 28.7s 超时后返回空列表 —— 热点页长期空白。
参考项目的做法本来就是**本地算**: 同花顺概念/行业成分 x 本地行情聚合,
与港美 HkUsIndustryHotspotSource 同口径。本模块即该路线在 A 股的落地。

口径:
    topic   = ext_gn_ths.所属概念 (或 ext_hy_ths.所属同花顺行业, 分号分隔多值)
    成分股  = 该维度下有本地行情的标的
    涨跌幅  = 成分股**前复权收盘**等权平均涨幅 (与东财板块涨跌幅同口径)
    成交额  = 成分股当日 amount 之和
    涨停    = 按板块涨跌幅阈值判定 (主板 10% / 创业板科创板 20% / 北交所 30%)
    热度    = compute_board_heat_score(涨幅, 排名) + 连板加成

与主线(market_mainline)的区别: 主线按**涨停梯队**打分(需要连板数时序),
本源按**当日涨幅 + 成交额**聚合, 不需要连板列 —— 因此不受 enriched 缺列影响,
概念成分来自 ext_gn_ths 快照(无历史版本), 与主线同样的归属漂移限制。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

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
)
from app.services.hotspot.source import HotspotSource

logger = logging.getLogger(__name__)

# 成分股门槛: 太小的概念平均涨幅噪声大
MIN_MEMBERS = 3

# ext 表名与维度字段
_EXT_TABLE = {"concept": "ext_gn_ths", "industry": "ext_hy_ths"}
_EXT_FIELD = {"concept": "所属概念", "industry": "所属同花顺行业"}

# 涨停阈值(小数制) — 按板块, 留 0.2% 容差吸收四舍五入
_LIMIT_UP_THRESHOLD = [
    (("688", "300", "301"), 0.198),   # 科创板 / 创业板
    (("8", "4", "92"), 0.298),        # 北交所
]
_LIMIT_UP_DEFAULT = 0.098             # 主板


def _limit_up_threshold(symbol: str) -> float:
    for prefixes, threshold in _LIMIT_UP_THRESHOLD:
        if symbol.startswith(prefixes):
            return threshold
    return _LIMIT_UP_DEFAULT


class CnConceptHotspotSource(HotspotSource):
    """A 股概念/行业热点: ext 维度成分 x 本地 enriched 行情聚合。"""

    name = "cn_local_concept"

    def __init__(self, data_dir: Path, kind: str = "concept") -> None:
        self.data_dir = Path(data_dir)
        self.kind = kind if kind in _EXT_TABLE else "concept"
        self._as_of: date | None = None

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_members(self) -> pl.DataFrame:
        """读 ext 快照 → (symbol, member) 展开表。"""
        table = _EXT_TABLE[self.kind]
        field = _EXT_FIELD[self.kind]
        files = sorted((self.data_dir / "ext_data" / table).rglob("*.parquet"))
        if not files:
            return pl.DataFrame()
        df = pl.read_parquet(files[-1])
        if field not in df.columns or "symbol" not in df.columns:
            return pl.DataFrame()
        return (
            df.select(
                pl.col("symbol").cast(pl.String).str.strip_chars().alias("symbol"),
                pl.col(field).cast(pl.String).fill_null("").alias("member"),
            )
            .filter(pl.col("symbol") != "")
            # 概念与行业都是分号分隔多值 (行业还含 "-" 分级, 整体作为一个成员)
            .with_columns(pl.col("member").str.split(";").alias("member"))
            .explode("member")
            .with_columns(pl.col("member").str.strip_chars())
            .filter((pl.col("member") != "") & (pl.col("member") != "-"))
            .unique(subset=["symbol", "member"])
        )

    def _latest_two_dates(self) -> tuple[str, str] | None:
        base = self.data_dir / "kline_daily_enriched"
        if not base.exists():
            return None
        dates = sorted(
            p.name.split("=", 1)[1] for p in base.glob("date=*")
        )
        if len(dates) < 2:
            return None
        return dates[-1], dates[-2]

    def _load_quotes(self) -> pl.DataFrame:
        """最新两个交易日 → 涨跌幅 / 成交额 / 连板数(若有列)。"""
        pair = self._latest_two_dates()
        if not pair:
            return pl.DataFrame()
        latest, prev = pair
        self._as_of = date.fromisoformat(latest)
        base = self.data_dir / "kline_daily_enriched"
        cols = ["symbol", "close", "amount", "open", "high", "low", "volume"]
        today_df = pl.read_parquet(base / f"date={latest}" / "part.parquet")
        prev_df = pl.read_parquet(base / f"date={prev}" / "part.parquet")
        today_cols = [c for c in cols if c in today_df.columns]
        prev_latest = prev_df.select(
            [c for c in ("symbol", "close") if c in prev_df.columns]
        ).rename({"close": "prev_close"})
        out = today_df.select(today_cols).join(prev_latest, on="symbol", how="left")
        if "consecutive_limit_ups" in today_df.columns:
            out = out.join(
                today_df.select("symbol", "consecutive_limit_ups"), on="symbol", how="left"
            )
        return out.with_columns(
            pl.when((pl.col("prev_close") > 0) & pl.col("close").is_not_null())
            .then((pl.col("close") - pl.col("prev_close")) / pl.col("prev_close"))
            .otherwise(None)
            .alias("change_pct")
        )

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    def _aggregate(self, members: pl.DataFrame, quotes: pl.DataFrame, top: int) -> list[HotspotSummary]:
        has_boards = "consecutive_limit_ups" in quotes.columns
        joined = members.join(quotes, on="symbol", how="inner")
        if joined.is_empty():
            return []

        limit_up_expr = pl.col("change_pct") >= pl.col("_limit")
        joined = joined.with_columns(
            pl.col("symbol")
            .map_elements(_limit_up_threshold, return_dtype=pl.Float64)
            .alias("_limit")
        ).with_columns(limit_up_expr.alias("_is_limit_up"))

        agg = (
            joined.group_by("member")
            .agg(
                pl.len().alias("members"),
                pl.col("change_pct").mean().alias("change_pct"),
                pl.col("amount").sum().alias("amount"),
                pl.col("_is_limit_up").sum().alias("limit_up_count"),
                (pl.col("consecutive_limit_ups").max() if has_boards else pl.lit(0)).alias("max_boards"),
                pl.col("symbol").sort_by("change_pct", descending=True).alias("_syms_by_chg"),
                pl.col("change_pct").max().alias("_top_chg"),
            )
            .filter(pl.col("members") >= MIN_MEMBERS)
            .sort("change_pct", descending=True, nulls_last=True)
        )
        if agg.is_empty():
            return []

        rows = agg.to_dicts()
        out: list[HotspotSummary] = []
        for rank, row in enumerate(rows[:top] if top else rows, start=1):
            member = str(row["member"])
            change = safe_float(row.get("change_pct"))
            heat = compute_board_heat_score(change, rank)
            boards = int(row.get("max_boards") or 0)
            if boards:
                heat = min(100.0, heat + min(boards, 5) * 4.0)

            leaders = [str(s) for s in (row.get("_syms_by_chg") or [])[:3]]
            missing: list[str] = []
            if not has_boards:
                missing.append("consecutive_limit_ups")

            out.append(
                HotspotSummary(
                    topic=member,
                    name=member,
                    source=self.kind,
                    rank=rank,
                    change_pct=change,
                    heat_score=round(heat, 2),
                    observations=int(row.get("members") or 0),
                    state=f"limit_up={int(row.get('limit_up_count') or 0)};max_boards={boards}",
                    stage=classify_stage(
                        state=f"limit_up={int(row.get('limit_up_count') or 0)}",
                        latest_score=heat,
                        observations=int(row.get("members") or 0),
                    ),
                    sample_stock_count=int(row.get("members") or 0),
                    leaders=leaders,
                    quality_status=QUALITY_OK if not missing else QUALITY_PARTIAL,
                    missing_fields=missing,
                    provider_used=self.name,
                    stale=False,
                    topic_date=self._as_of.isoformat() if self._as_of else "",
                    snapshot_at=utc_now_iso(),
                    snapshot_market="cn",
                )
            )
        return out

    # ------------------------------------------------------------------
    # 协议
    # ------------------------------------------------------------------

    def supports(self, market: str) -> bool:
        return market == "cn"

    def discover(self, *, market: str = "cn", top: int = 20) -> HotspotResults:
        if not self.supports(market):
            return HotspotResults(
                [], provider_used=self.name, market=market,
                source_errors=[SourceError(self.name, "discover",
                                           f"market '{market}' unsupported").as_str()],
            )
        members = self._load_members()
        if members.is_empty():
            return HotspotResults(
                [], provider_used=self.name, market=market,
                source_errors=[SourceError(self.name, "members",
                                           f"no ext_{self.kind} snapshot found").as_str()],
            )
        quotes = self._load_quotes()
        if quotes.is_empty():
            return HotspotResults(
                [], provider_used=self.name, market=market,
                source_errors=[SourceError(self.name, "quotes",
                                           "enriched 分区不足两个交易日").as_str()],
            )
        return HotspotResults(
            self._aggregate(members, quotes, top),
            provider_used=self.name,
            market=market,
        )

    def fetch_detail(
        self,
        topic: str,
        *,
        market: str = "cn",
        top_stocks: int = 10,
    ) -> HotspotDetail | None:
        members = self._load_members()
        if members.is_empty():
            return None
        quotes = self._load_quotes()
        if quotes.is_empty():
            return None

        pool = members.filter(pl.col("member") == topic).join(quotes, on="symbol", how="inner")
        if pool.is_empty():
            return None

        has_boards = "consecutive_limit_ups" in pool.columns
        rows = (
            pool.with_columns(
                pl.col("symbol").map_elements(_limit_up_threshold, return_dtype=pl.Float64).alias("_limit")
            )
            .with_columns((pl.col("change_pct") >= pl.col("_limit")).alias("_is_limit_up"))
            .sort("change_pct", descending=True, nulls_last=True)
            .to_dicts()
        )

        names = self._load_names()
        stocks: list[HotspotStock] = []
        for row in rows[: max(1, top_stocks)]:
            symbol = str(row.get("symbol") or "")
            change = safe_float(row.get("change_pct"))
            stocks.append(
                HotspotStock(
                    code=symbol,
                    name=names.get(symbol, ""),
                    change_pct=change,
                    amount=safe_float(row.get("amount")),
                    turnover_rate=None,   # enriched 窄表不含换手率, 显式 None
                    volume_ratio=None,
                    is_limit_up=bool(row.get("_is_limit_up")),
                    active_days=int(row.get("consecutive_limit_ups") or 0) if has_boards else 0,
                    hot_stock_score=round(compute_board_heat_score(change, None), 2),
                    source=self.name,
                )
            )
        assign_roles(stocks)

        change = safe_float(rows[0].get("change_pct")) if rows else None
        summary = HotspotSummary(
            topic=topic,
            name=topic,
            source=self.kind,
            rank=None,
            change_pct=change,
            heat_score=round(compute_board_heat_score(change, None), 2),
            observations=len(rows),
            sample_stock_count=len(rows),
            leaders=[s.code for s in stocks[:3]],
            leader_stocks=stocks[:3],
            quality_status=QUALITY_PARTIAL,  # 换手率/量比不可得, 见上
            missing_fields=["turnover_rate", "volume_ratio"],
            provider_used=self.name,
            topic_date=self._as_of.isoformat() if self._as_of else "",
            snapshot_at=utc_now_iso(),
            snapshot_market="cn",
        )
        return HotspotDetail(summary=summary, stocks=stocks, stock_count=len(rows))

    def _load_names(self) -> dict[str, str]:
        """ext 快照里的股票简称 (成分股展示用)。"""
        table = _EXT_TABLE[self.kind]
        files = sorted((self.data_dir / "ext_data" / table).rglob("*.parquet"))
        if not files:
            return {}
        df = pl.read_parquet(files[-1])
        if "symbol" not in df.columns or "股票简称" not in df.columns:
            return {}
        return {
            str(r["symbol"]): str(r["股票简称"] or "")
            for r in df.select("symbol", "股票简称").to_dicts()
        }
