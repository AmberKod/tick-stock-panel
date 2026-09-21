"""批次 B 独立验收测试 (QA)。

锁定两批提交的三条纪律边界:

1. ``classify_stage`` 的 None / 0 语义:未观测(None/NaN/不可解析) → 未判定;
   显式 0 → 照旧判定。**正反都测**,因为"把可用误判成不可用"同样违反纪律。
2. 快照落盘 → 读回不能把 null 变回"初次异动"假徽标。
3. A 股概念源不传趋势三维度 → stage 为 None,但列表照常可用(不是退化成空白/报错)。

这些用例刻意构造成"改回旧实现就会红":任一条 ``or "初次异动"`` /
``or 0.0`` 兜底复活,下面至少一条断言会失败。
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from app.api.hotspots import summary_to_dict
from app.services.hk_us_overview_builder import _load_latest_rows
from app.services.hotspot.cn_concept_source import CnConceptHotspotSource
from app.services.hotspot.models import HotspotResults, HotspotSummary
from app.services.hotspot.scoring import classify_stage
from app.services.hotspot.service import _limit_results
from app.services.hotspot.storage import read_topics, write_topics

PREV = "2026-09-16"
LATEST = "2026-09-17"

# 真实数据目录(项目根 data/);不存在则跳过依赖真实 parquet 的复算用例
_REAL_DATA = Path(__file__).resolve().parents[4] / "data"
_NEEDS_REAL_DATA = pytest.mark.skipif(
    not (_REAL_DATA / "kline_hk_us_enriched").exists(),
    reason="真实 data/ 不可用,跳过 parquet 复算",
)


def _as_d(value):
    return value.date() if hasattr(value, "date") else value


# ---------------------------------------------------------------------------
# 1. classify_stage: None(未观测) vs 0(观测到 0)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # 什么都不给
        {"latest_score": 10},  # 只有当期热度,趋势维度全未知
        {"observations": 5, "latest_score": 90},
        {"trend_score": None, "cooling_score": None, "persistence_score": None},
        {"trend_score": float("nan"), "cooling_score": None, "persistence_score": None},
        {"trend_score": None, "cooling_score": float("nan"), "persistence_score": None},
        {"trend_score": None, "cooling_score": None, "persistence_score": float("nan")},
        {"trend_score": "N/A", "cooling_score": "N/A", "persistence_score": "N/A"},
    ],
)
def test_classify_stage_unknown_returns_none(kwargs):
    """趋势三维度全部**未知** → 未判定(None),绝不冒充「初次异动」。"""
    assert classify_stage(**kwargs) is None


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        # 显式 0 = 观测到趋势为 0 的真实结论,仍走原分支 → 初次异动
        (
            {"trend_score": 0, "cooling_score": 0, "persistence_score": 0,
             "latest_score": 10, "observations": 0},
            "初次异动",
        ),
        # 至少一维有观测值就判定(部分未知按 0 参与比较,沿用既有规则)
        ({"trend_score": 0, "cooling_score": None, "persistence_score": None}, "初次异动"),
        ({"persistence_score": 0, "trend_score": None, "cooling_score": None}, "初次异动"),
        ({"cooling_score": 0, "trend_score": None, "persistence_score": None}, "初次异动"),
        # 有观测值时该出什么阶段就出什么阶段,不能因为别的维度缺失就变未判定
        ({"trend_score": 0, "cooling_score": 6}, "分歧放量"),
        ({"trend_score": 0, "persistence_score": 70, "latest_score": 0}, "确认扩散"),
        (
            {"trend_score": 9, "cooling_score": 0, "persistence_score": 60,
             "latest_score": 80, "observations": 3},
            "加速主升",
        ),
        # 降温需要 state 命中或 cooling>=8,单靠负 trend 不成立 → 回落初次异动
        ({"trend_score": -9, "cooling_score": 0, "persistence_score": 0, "latest_score": 10}, "初次异动"),
        ({"trend_score": -9, "cooling_score": 0, "persistence_score": 0,
          "latest_score": 10, "state": "weakening"}, "降温退潮"),
    ],
)
def test_classify_stage_observed_values_still_classified(kwargs, expected):
    """显式观测值(含 0)照旧判定 —— 不能把"可用"误判成"不可用"。"""
    assert classify_stage(**kwargs) == expected


def test_classify_stage_zero_is_not_treated_as_missing():
    """0 与 None 必须可区分:同一 latest_score 下 0→初次异动, None→未判定。"""
    assert classify_stage(trend_score=0, cooling_score=0, persistence_score=0, latest_score=10) == "初次异动"
    assert classify_stage(trend_score=None, cooling_score=None, persistence_score=None, latest_score=10) is None


# ---------------------------------------------------------------------------
# 2. 快照落盘 → 读回不能复活假徽标
# ---------------------------------------------------------------------------

def _summary(stage: str | None, topic: str = "T") -> HotspotSummary:
    return HotspotSummary(
        topic=topic, name=topic, source="qa", rank=1, change_pct=0.01,
        heat_score=60.0, stage=stage, snapshot_market="cn",
    )


def test_storage_roundtrip_keeps_stage_null(tmp_path):
    """stage=None 写盘再读回必须还是 None —— 不是 "初次异动",也不是空串。"""
    data_dir = tmp_path / "data"
    write_topics(data_dir, [_summary(None), _summary("加速主升", topic="U")], market="cn")

    read_back = read_topics(data_dir, market="cn")
    by_topic = {item.topic: item for item in read_back}
    assert set(by_topic) == {"T", "U"}
    assert by_topic["T"].stage is None, "null 被兜成了假徽标"
    assert by_topic["T"].stage != "初次异动"
    assert by_topic["T"].stage != ""
    # 有值的不能被抹掉
    assert by_topic["U"].stage == "加速主升"


def test_storage_roundtrip_keeps_stage_null_after_two_hops(tmp_path):
    """再写一次(覆盖)仍保持 null:读回→再写→再读回,防止二次写入时复活兜底。"""
    data_dir = tmp_path / "data"
    write_topics(data_dir, [_summary(None)], market="cn")
    first = read_topics(data_dir, market="cn")
    write_topics(data_dir, first, market="cn")
    second = read_topics(data_dir, market="cn")
    assert len(second) == 1
    assert second[0].stage is None


def test_api_payload_stage_is_null_not_sentinel():
    """API 层 stage 必须是 JSON null,不是 "初次异动" / "" 之类的哨兵值。"""
    payload = summary_to_dict(_summary(None))
    assert "stage" in payload
    assert payload["stage"] is None

    payload_set = summary_to_dict(_summary("降温退潮"))
    assert payload_set["stage"] == "降温退潮"


# ---------------------------------------------------------------------------
# 3. A 股概念源:stage 为 None,但列表不能退化
# ---------------------------------------------------------------------------

def _ext(root: Path, rows: list[tuple[str, str, str]]) -> None:
    path = root / "ext_data" / "ext_gn_ths" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "股票代码": [r[0] for r in rows],
        "股票简称": [r[1] for r in rows],
        "所属概念": [r[2] for r in rows],
        "symbol": [r[0] for r in rows],
    }).write_parquet(path)


def _quotes(root: Path, day: str, rows: list[tuple[str, float, float]]) -> None:
    path = root / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows], "close": [r[1] for r in rows],
        "amount": [r[2] for r in rows], "open": [r[1] for r in rows],
        "high": [r[1] for r in rows], "low": [r[1] for r in rows],
        "volume": [100.0] * len(rows),
    }).write_parquet(path)


def test_cn_concept_source_stage_is_none_but_list_still_usable(tmp_path):
    """走真实 CnConceptHotspotSource:stage 整列 None,但列表照常出榜不报错。"""
    members = [("600001.SH", "甲", "转基因"), ("300001.SZ", "乙", "转基因"), ("000001.SZ", "丙", "转基因")]
    _ext(tmp_path, members)
    _quotes(tmp_path, PREV, [(s, 10.0, 1e8) for s, _, _ in members])
    _quotes(tmp_path, LATEST, [(s, 11.0, 1e8) for s, _, _ in members])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)

    # ① 不是"什么都不显示":照常有条目、有涨幅、有样本数
    assert res.is_usable and len(res) == 1
    row = res[0]
    assert row.topic == "转基因"
    assert row.change_pct is not None and row.change_pct > 0
    assert row.sample_stock_count == 3
    assert row.heat_score > 0
    assert row.topic_date == LATEST
    # ② 趋势三维度确实没算过 → stage 未判定,且三维度保持 None(不是 0)
    assert row.stage is None
    assert row.trend_score is None
    assert row.persistence_score is None
    assert row.cooling_score is None
    # ③ stage=未判定 **不等于** 数据缺失:不该把 stage 塞进 missing_fields 造成二次降级。
    #    (本 fixture 的 enriched 没有连板列, partial 来自 consecutive_limit_ups, 与 stage 无关)
    assert "stage" not in row.missing_fields
    assert row.missing_fields == ["consecutive_limit_ups"]
    assert row.quality_status == "partial"
    assert not res.source_errors


def test_cn_concept_source_sample_coverage_is_none(tmp_path):
    """CN 源不计算覆盖率 → 必须 None,不许挂假数字(如 100% / covered==universe)。"""
    members = [("600001.SH", "甲", "转基因"), ("300001.SZ", "乙", "转基因"), ("000001.SZ", "丙", "转基因")]
    _ext(tmp_path, members)
    _quotes(tmp_path, PREV, [(s, 10.0, 1e8) for s, _, _ in members])
    _quotes(tmp_path, LATEST, [(s, 11.0, 1e8) for s, _, _ in members])

    res = CnConceptHotspotSource(tmp_path).discover(market="cn", top=10)
    assert res.sample_coverage is None
    assert res.to_dict()["sample_coverage"] is None
    # 截断 top 不许把 None 变成别的
    assert _limit_results(res, 1).sample_coverage is None
    assert HotspotResults(list(res), sample_coverage=None).to_dict()["sample_coverage"] is None


def test_cn_stage_null_survives_service_persist_and_read(tmp_path):
    """service 落盘 → 读回整条链路:A 股 stage 仍为 None(端到端,不只是 storage 层)。"""
    from app.services.hotspot.service import discover_hotspots

    members = [("600001.SH", "甲", "转基因"), ("300001.SZ", "乙", "转基因"), ("000001.SZ", "丙", "转基因")]
    _ext(tmp_path, members)
    _quotes(tmp_path, PREV, [(s, 10.0, 1e8) for s, _, _ in members])
    _quotes(tmp_path, LATEST, [(s, 11.0, 1e8) for s, _, _ in members])

    data_dir = tmp_path / "data"
    source = CnConceptHotspotSource(tmp_path)
    fresh = discover_hotspots(data_dir, market="cn", top=10, refresh=True, source=source)
    assert len(fresh) == 1
    assert fresh[0].stage is None

    cached = discover_hotspots(data_dir, market="cn", top=10, refresh=False, source=source)
    assert len(cached) == 1
    assert cached[0].stage is None, "落盘再读回把 null 复活成了假徽标"


# ---------------------------------------------------------------------------
# 4. 覆盖率数字:用真实 parquet 独立复算, 与被测函数对账
# ---------------------------------------------------------------------------

def _facts(market: str) -> dict:
    """只用 polars 直扫 parquet 得到的事实(不经过被测代码,避免自证)。"""
    suffix = f".{market.upper()}"
    inst = pl.read_parquet(_REAL_DATA / "instruments" / f"{market.lower()}_instruments.parquet")
    universe = {
        str(s) for s in inst["symbol"].cast(pl.Utf8, strict=False).unique().to_list()
        if s and str(s).endswith(suffix)
    }
    lf = pl.scan_parquet(
        str(_REAL_DATA / "kline_hk_us_enriched" / "symbol=*" / "part.parquet"),
        cast_options=pl.ScanCastOptions(integer_cast="allow-float"),
    )
    latest = (
        lf.filter(pl.col("symbol").str.ends_with(suffix))
        .select("symbol", "date").collect()
        .group_by("symbol").agg(pl.col("date").max().alias("latest_date"))
    )
    as_of = _as_d(latest["latest_date"].max())
    hit = {
        str(s) for s in lf.filter(
            pl.col("symbol").str.ends_with(suffix) & (pl.col("date") == as_of)
        ).select(pl.col("symbol").cast(pl.Utf8, strict=False).unique()).collect()["symbol"].to_list()
        if s
    }
    stale = [
        _as_d(v) for sym, v in zip(latest["symbol"].to_list(), latest["latest_date"].to_list(), strict=True)
        if str(sym) in universe and _as_d(v) != as_of
    ]
    buckets: dict[str, int] = {}
    for v in stale:
        buckets[v.isoformat()] = buckets.get(v.isoformat(), 0) + 1
    return {
        "universe": len(universe),
        "covered": len(hit & universe),
        "as_of": as_of.isoformat() if as_of else None,
        "stale_symbols": len(stale),
        "stale_buckets": [{"date": k, "count": c} for k, c in sorted(buckets.items(), key=lambda i: -i[1])[:3]],
    }


@_NEEDS_REAL_DATA
@pytest.mark.parametrize("market", ["HK", "US"])
def test_sample_coverage_matches_independent_recompute(market):
    """实现报的覆盖率必须等于 parquet 直算的事实:分子/分母/stale 桶逐项对齐。"""
    fact = _facts(market)
    cov = _load_latest_rows(_REAL_DATA, market).coverage

    assert cov is not None, "universe 读得到却报 None = 把可用误判成不可用"
    assert cov["universe"] == fact["universe"]
    assert cov["covered"] == fact["covered"]
    assert cov["as_of"] == fact["as_of"]
    assert cov["stale_symbols"] == fact["stale_symbols"]
    assert cov["stale_buckets"] == fact["stale_buckets"]
    # ratio 是计数比值,不是打分:必须等于 covered/universe 四舍五入
    assert cov["ratio"] == pytest.approx(round(fact["covered"] / fact["universe"], 4))
    assert 0.0 <= cov["ratio"] <= 1.0


@_NEEDS_REAL_DATA
def test_hk_coverage_is_partial_and_us_is_healthy():
    """港股覆盖率明显偏低(幸存者偏差要显式化),美股健康 → 前端阈值 0.95 两侧各一例。"""
    hk = _load_latest_rows(_REAL_DATA, "HK").coverage
    us = _load_latest_rows(_REAL_DATA, "US").coverage
    assert hk is not None and us is not None
    assert hk["ratio"] < 0.95, "港股若不触发提示,幸存者偏差又被藏起来了"
    assert us["ratio"] >= 0.95, "美股健康样本不该刷存在感"
    # 分母必须来自 instruments,不能拿命中数冒充
    assert hk["universe"] > hk["covered"]
    assert us["universe"] > us["covered"]
