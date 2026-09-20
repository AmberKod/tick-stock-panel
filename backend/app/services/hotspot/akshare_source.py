"""akshare 东方财富热点数据源 (A 股)。

数据接口 (全部为纯 HTTP 请求, 无 MiniRacer 线程安全问题):
- ``ak.stock_board_concept_name_em()``    概念板块列表
- ``ak.stock_board_industry_name_em()``   行业板块列表
- ``ak.stock_board_concept_cons_em()``    概念成分股
- ``ak.stock_board_industry_cons_em()``   行业成分股

单位契约 (关键):
- akshare 东财接口的 涨跌幅/换手率 是**百分数** (3.66 = 3.66%);
  本项目全链路是**小数制** (0.0366), 解析时必须 /100。
- 成交额单位为元, 与 scoring.py 的 log10(amount) 量纲一致。
- 成分股接口不提供 量比/主力净流/涨停/活跃天数/证据数 —— 按
  H-06 契约显式置 False/0, 不从涨幅推断。

降级策略 (fail-closed, 不回退 stub):
- concept 失败 + industry 成功 → 返回 industry 部分 + source_errors (partial)。
- 两个列表都失败 → 空结果 + failed (由 service 层走缓存回退)。
- akshare 未安装/导入失败 → 空结果 + source_errors。
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.services.hotspot.models import (
    HotspotDetail,
    HotspotResults,
    HotspotStock,
    HotspotSummary,
    SourceError,
    utc_now_iso,
)
from app.services.hotspot.scoring import (
    _coerce_bool,
    assign_roles,
    classify_stage,
    compute_board_heat_score,
    safe_float,
    safe_text,
    score_constituent,
)
from app.services.hotspot.source import HotspotSource

logger = logging.getLogger(__name__)


# 东财板块列表接口列名 (中文名, 宽松匹配)
_COL_RANK = ("排名", "rank")
_COL_TOPIC = ("板块名称", "板块名", "name")
_COL_CHANGE_PCT = ("涨跌幅", "涨幅", "change_pct")
_COL_LEADER = ("领涨股票", "领涨股", "领涨股票名")

# 成分股接口列名
_COL_CODE = ("代码", "code")
_COL_NAME = ("名称", "name")
_COL_AMOUNT = ("成交额", "amount")
_COL_TURNOVER = ("换手率", "turnover")


def _pick(row: dict[str, Any], names: Sequence[str]) -> Any:
    """按候选列名顺序取第一个存在的值 (容 akshare 版本列名差异)。"""
    for name in names:
        if name in row:
            return row[name]
    return None


def _row_records(df: Any) -> list[dict[str, Any]]:
    """DataFrame / list[dict] 统一转 records; 空或异常返回 []。

    兼容 pandas DataFrame(有 empty 属性 + to_dict("records")) 与
    测试注入的轻量 fake 对象。
    """
    if df is None:
        return []
    try:
        if bool(getattr(df, "empty", False)):
            return []
    except Exception:
        pass
    if hasattr(df, "to_dict"):
        try:
            records = df.to_dict("records")
        except TypeError:
            return []
        return [r for r in records if isinstance(r, dict)]
    if isinstance(df, (list, tuple)):
        return [r for r in df if isinstance(r, dict)]
    return []


def decorate_a_share_code(code: Any) -> str:
    """东财纯数字代码 → 项目带交易所后缀格式。

    号段规则 (A 股板块成分):
    - 6 开头 (600/601/603/605/688)  → .SH
    - 0/2/3 开头 (000/002/300 等)   → .SZ
    - 4/8 开头 (43x/83x/87x 北交所) → .BJ
    - 920 开头 (北交所新号段)       → .BJ
    - 其余 (已带后缀 / 非标准) 原样返回, 不猜测。
    """
    text = safe_text(code)
    if not text.isdigit() or len(text) != 6:
        return text
    if text[0] == "6":
        return f"{text}.SH"
    if text[0] in "023":
        return f"{text}.SZ"
    if text[0] in "48" or text.startswith("92"):
        return f"{text}.BJ"
    return text


class AkshareHotspotSource(HotspotSource):
    """东方财富概念/行业板块热点源 (akshare 桥接)。

    ``ak`` 构造参数供测试注入 fake 模块; 生产路径为 None → 首次调用时
    延迟 import akshare (可选 extra, 失败返回空结果而非崩溃)。
    """

    name = "akshare"

    def __init__(self, ak: Any = None) -> None:
        self._ak = ak
        # discover 时缓存 topic → summary, 供 fetch_detail 复用列表元数据
        self._summary_by_topic: dict[str, HotspotSummary] = {}

    def supports(self, market: str) -> bool:
        return market == "cn"

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def discover(
        self,
        *,
        market: str = "cn",
        top: int = 20,
    ) -> HotspotResults:
        if market != "cn":
            err = SourceError(
                provider=self.name,
                method="discover",
                message=f"market '{market}' unsupported by akshare source",
            )
            return HotspotResults(
                [],
                provider_used=self.name,
                source_errors=[err.as_str()],
                market=market,
            )

        try:
            ak = self._load_ak()
        except RuntimeError as exc:
            return HotspotResults(
                [],
                provider_used=self.name,
                source_errors=[str(exc)],
                market=market,
            )

        summaries: list[HotspotSummary] = []
        errors: list[str] = []
        for kind, method_name in (
            ("concept", "stock_board_concept_name_em"),
            ("industry", "stock_board_industry_name_em"),
        ):
            fetch = getattr(ak, method_name, None)
            if fetch is None:
                errors.append(
                    SourceError(provider=self.name, method=method_name,
                                message="akshare interface missing").as_str()
                )
                continue
            try:
                rows = _row_records(fetch())
            except Exception as exc:
                message = f"{type(exc).__name__}: {str(exc)[:200]}"
                errors.append(
                    SourceError(provider=self.name, method=method_name, message=message).as_str()
                )
                logger.warning("hotspot akshare %s failed: %s", method_name, message)
                continue
            for row in rows:
                summary = self._row_to_summary(row, kind)
                if summary is not None:
                    summaries.append(summary)

        # 排序: heat_score 降序, 同分按 rank 升序
        summaries.sort(
            key=lambda s: (-s.heat_score, s.rank if s.rank is not None else 1 << 30)
        )
        self._summary_by_topic = {s.topic: s for s in summaries}

        if not summaries and not errors:
            errors.append(
                SourceError(provider=self.name, method="discover",
                            message="no board rows returned").as_str()
            )

        cap = max(int(top), 0)
        used = summaries[:cap] if cap else summaries
        return HotspotResults(
            used,
            provider_used=self.name,
            source_errors=errors,
            market=market,
        )

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------

    def fetch_detail(
        self,
        topic: str,
        *,
        market: str = "cn",
        top_stocks: int = 10,
    ) -> HotspotDetail | None:
        if market != "cn":
            return None
        text = safe_text(topic)
        if not text:
            return None
        try:
            ak = self._load_ak()
        except RuntimeError:
            return None

        kind = getattr(self._summary_by_topic.get(text), "source", "")
        kinds: list[str] = [kind] if kind else ["concept", "industry"]
        if kind:
            kinds.append("industry" if kind == "concept" else "concept")

        for current_kind in kinds:
            method_name = (
                "stock_board_concept_cons_em"
                if current_kind == "concept"
                else "stock_board_industry_cons_em"
            )
            fetch = getattr(ak, method_name, None)
            if fetch is None:
                continue
            try:
                rows = _row_records(fetch(symbol=text))
            except Exception as exc:
                logger.warning(
                    "hotspot akshare %s(%r) failed: %s", method_name, text, exc
                )
                continue
            if not rows:
                continue
            return self._build_detail(text, rows, kind=current_kind, top_stocks=top_stocks)

        return None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _load_ak(self) -> Any:
        if self._ak is not None:
            return self._ak
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                f"akshare unavailable (optional extra 'multi-market' not installed): {exc}"
            ) from exc
        self._ak = ak
        return ak

    def _row_to_summary(self, row: dict[str, Any], kind: str) -> HotspotSummary | None:
        topic = safe_text(_pick(row, _COL_TOPIC))
        if not topic:
            return None
        change_pct = _to_ratio(_pick(row, _COL_CHANGE_PCT))
        rank_raw = safe_float(_pick(row, _COL_RANK))
        rank = int(rank_raw) if rank_raw is not None else None
        heat = compute_board_heat_score(change_pct, rank)
        leader_text = safe_text(_pick(row, _COL_LEADER))
        leaders = [leader_text] if leader_text else []
        summary = HotspotSummary(
            topic=topic,
            name=topic,
            source=kind,
            rank=rank,
            change_pct=change_pct,
            heat_score=round(heat, 4),
            stage=classify_stage(
                latest_score=heat,
                observations=0,
                persistence_score=0,
                trend_score=0,
                cooling_score=0,
            ),
            sample_stock_count=0,
            leaders=leaders,
            # 列表阶段无成分股详情 (akshare 列表接口不提供), 如实标记。
            missing_fields=["leader_stocks"],
            quality_status="partial",
            provider_used=self.name,
            canonical_topic=topic,
            aliases=[topic],
            topic_date=utc_now_iso()[:10],
        )
        return summary

    def _build_detail(
        self,
        topic: str,
        rows: list[dict[str, Any]],
        *,
        kind: str,
        top_stocks: int,
    ) -> HotspotDetail:
        stocks = self._coerce_rows(rows, source=f"{self.name}.{kind}")
        if stocks:
            stocks = assign_roles(stocks)

        cached = self._summary_by_topic.get(topic)
        if cached is not None:
            summary = HotspotSummary(
                **{
                    field: getattr(cached, field)
                    for field in cached.__dataclass_fields__
                }
            )
            summary.source = kind
        else:
            summary = HotspotSummary(
                topic=topic,
                name=topic,
                source=kind,
                heat_score=0.0,
                quality_status="partial",
                missing_fields=["rank", "change_pct", "heat_score"],
                provider_used=self.name,
                canonical_topic=topic,
                aliases=[topic],
                topic_date=utc_now_iso()[:10],
            )

        summary.sample_stock_count = len(stocks)
        summary.leader_stocks = [s for s in stocks if s.role == "核心龙头"][:3]
        if not summary.leader_stocks:
            summary.leader_stocks = stocks[:3]
        summary.leaders = [s.name or s.code for s in summary.leader_stocks if (s.name or s.code)]
        summary.missing_fields = [f for f in summary.missing_fields if f != "leader_stocks"]

        return HotspotDetail(
            summary=summary,
            stocks=stocks[: max(int(top_stocks), 0)],
            timeline=[],
            route=[],
            stock_count=len(stocks),
        )

    @staticmethod
    def _coerce_rows(raw_rows: list[dict[str, Any]], *, source: str) -> list[HotspotStock]:
        stocks: list[HotspotStock] = []
        for row in raw_rows:
            code = decorate_a_share_code(_pick(row, _COL_CODE))
            if not code:
                continue
            stock = HotspotStock(
                code=code,
                name=safe_text(_pick(row, _COL_NAME)),
                change_pct=_to_ratio(_pick(row, _COL_CHANGE_PCT)),
                amount=safe_float(_pick(row, _COL_AMOUNT)),
                turnover_rate=_to_ratio(_pick(row, _COL_TURNOVER)),
                # 东财成分接口不提供量比/主力净流/涨停/活跃天数/证据数:
                # 显式默认 (H-06), 不从涨幅推断。
                volume_ratio=None,
                net_inflow=None,
                is_limit_up=_coerce_bool(_pick(row, ("is_limit_up",))),
                active_days=0,
                evidence_count=0,
                source=source,
                source_confidence=1.0,
                fallback_used=False,
            )
            stock.hot_stock_score = score_constituent(stock)
            stocks.append(stock)
        return stocks


def _to_ratio(value: Any) -> float | None:
    """百分数 (3.66) → 小数制 (0.0366); None 透传, 非法回 None。"""
    if value is None:
        return None
    number = safe_float(value)
    if number is None:
        return None
    return number / 100.0


__all__ = ["AkshareHotspotSource", "decorate_a_share_code"]
