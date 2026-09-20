"""市场态势 (market posture) 单测。

钉死三条硬规则 (PRD §1.4 / §3.2 面板 1):
1. **不可用维度不计入分母** —— 既不当 0 也不当中性。这是全篇最容易踩的坑:
   港股 industry 结构性缺失, 一旦"当 0/当防守"计入, 港股就被永久误判成防守。
2. **镜像错误同样要防** —— 美股 industry **可用**, 被误标 unavailable 就白白
   丢掉一个维度。
3. **一票否决 + 全不可用 → unknown(不是 defend)**。

同时按 §2.1 要求, 把阈值常量钉死 (改常量即红)。
"""
from __future__ import annotations

import pytest

from app.services import market_posture as mp


def _inputs(market: str = "hk", **kwargs) -> mp.PostureInputs:
    """构造输入快照的便捷工厂(未传的维度 = 不可用)。"""
    return mp.PostureInputs(market=market, **kwargs)


def _vote_of(result: dict, dim: str) -> str:
    return next(v["vote"] for v in result["votes"] if v["dim"] == dim)


# ─────────────────────────────────────────────────────────────
# case 1: 港美缺维度时不计入分母, 不得判防守
# ─────────────────────────────────────────────────────────────


def test_hk_industry_unavailable_excluded_from_denominator():
    """港股 industry 不可用: 须标 unavailable 且**不计入分母**, 其余偏多 → 进攻。"""
    result = mp.evaluate(_inputs(
        "hk",
        regime={"state": "strong", "score": 82, "date": "2026-09-19"},
        breadth={"total": 1800, "up_pct": 72.0},
        hotspot_stages=["加速主升", "确认扩散", "加速主升"],
        industry=None,
    ))

    assert result["posture"] == mp.POSTURE_ATTACK
    # industry 必须显式列为不可用, 且其票**不是** defend/neutral
    assert result["unavailable_dims"] == ["industry"]
    assert _vote_of(result, "industry") == mp.VOTE_UNAVAILABLE
    # 分母 = 3 (4 个维度 - 1 个不可用), 不是 4
    assert result["tally"]["counted"] == 3
    assert result["tally"]["attack"] == 3
    assert result["tally"]["defend"] == 0
    # evidence 里要有"为什么"的人话说明
    assert any("行业" in e["text"] for e in result["evidence"])


def test_hk_two_dims_unavailable_still_not_defend():
    """港股两个维度不可用(regime 也无数据)时, 严禁退化成防守。

    这是变异验证的关键场景: 若把 unavailable 当防守票计入,
    defend 票会凑到 3 张(热点 1 + 两个不可用 2)而误判成"防守"。
    """
    result = mp.evaluate(_inputs(
        "hk",
        regime=None,
        breadth={"total": 1800, "up_pct": 50.0},
        hotspot_stages=["降温退潮", "降温退潮", "初次异动"],
        industry=None,
    ))

    assert result["posture"] != mp.POSTURE_DEFEND
    assert result["posture"] == mp.POSTURE_BALANCED
    assert set(result["unavailable_dims"]) == {"regime", "industry"}
    assert result["tally"]["counted"] == 2
    assert result["tally"]["defend"] == 1


# ─────────────────────────────────────────────────────────────
# case 2: 一票否决生效
# ─────────────────────────────────────────────────────────────


def test_veto_by_breadth_forces_defend():
    """上涨占比 < 35% → 一票否决, 直接判防守(即便其余维度全线偏多)。"""
    result = mp.evaluate(_inputs(
        "cn",
        regime={"state": "strong", "score": 90},
        breadth={"total": 5000, "up_pct": 20.0},
        hotspot_stages=["加速主升", "加速主升"],
        industry={"leading": [{"name": "半导体", "avg_pct": 0.05}], "lagging": []},
    ))

    assert result["posture"] == mp.POSTURE_DEFEND
    assert result["veto"] is not None
    assert result["veto"]["dim"] == mp.DIM_BREADTH
    assert any("一票否决" in e["text"] for e in result["evidence"])


def test_veto_by_regime_forces_defend():
    """regime 属 weak/lean_weak → 一票否决, 直接判防守。"""
    for state in ("weak", "lean_weak"):
        result = mp.evaluate(_inputs(
            "us",
            regime={"state": state, "score": 20},
            breadth={"total": 3000, "up_pct": 80.0},
            hotspot_stages=["加速主升", "加速主升", "确认扩散"],
            industry={"leading": [{"name": "Technology", "avg_pct": 0.04}], "lagging": []},
        ))
        assert result["posture"] == mp.POSTURE_DEFEND
        assert result["veto"] is not None
        assert result["veto"]["dim"] == mp.DIM_REGIME


def test_veto_does_not_fire_when_dim_unavailable():
    """否决维度本身不可用时, 不能用"缺失"去否决(缺失 ≠ 偏空)。"""
    result = mp.evaluate(_inputs(
        "cn",
        regime=None,                                   # regime 不可用
        breadth={"total": 5000, "up_pct": 75.0},       # 广度偏多
        hotspot_stages=["加速主升", "确认扩散"],
        industry={"leading": [{"name": "银行", "avg_pct": 0.02}], "lagging": []},
    ))

    assert result["veto"] is None
    assert result["posture"] == mp.POSTURE_ATTACK
    assert result["tally"]["counted"] == 3


# ─────────────────────────────────────────────────────────────
# case 3: 全部维度不可用 → unknown, 不是 defend
# ─────────────────────────────────────────────────────────────


def test_all_dims_unavailable_returns_unknown():
    """全部不可用 → unknown;绝不能因为"什么都没查到"就判防守。"""
    for market in ("cn", "hk", "us"):
        result = mp.evaluate(mp.PostureInputs(market=market))
        assert result["posture"] == mp.POSTURE_UNKNOWN
        assert result["tally"]["counted"] == 0
        assert result["veto"] is None
        # 港股 industry 属静态不可用, 仍然全部列为不可用
        assert len(result["unavailable_dims"]) == len(mp.VOTING_DIMS)


def test_no_veto_when_breadth_missing():
    """广度缺失时不得用"没数据"触发否决线(up_pct 缺失 ≠ 0%)。"""
    result = mp.evaluate(mp.PostureInputs(market="cn", breadth={"total": 0, "up_pct": 0}))
    assert result["posture"] == mp.POSTURE_UNKNOWN
    assert result["veto"] is None


# ─────────────────────────────────────────────────────────────
# 镜像错误: 美股 industry 必须可用
# ─────────────────────────────────────────────────────────────


def test_hk_industry_used_when_data_present():
    """港股静态声明了 industry 不可用, 但**真有数据时不许丢弃**(镜像错误)。

    实测: data/instruments/hk_instruments.parquet 带 sector 列且绝大多数非空,
    与 PRD §1.4「港股 universe 无该字段」的表述不符。故实现上静态声明只在
    **数据确实缺失**时生效 —— 有数据时照常投票, 避免"把可用误判成不可用"。
    """
    result = mp.evaluate(_inputs(
        "hk",
        regime={"state": "strong", "score": 80},
        breadth={"total": 1800, "up_pct": 70.0},
        hotspot_stages=["加速主升", "确认扩散"],
        industry={"leading": [{"name": "半导体", "avg_pct": 0.06}], "lagging": []},
    ))

    assert "industry" not in result["unavailable_dims"]
    assert _vote_of(result, "industry") == mp.VOTE_ATTACK
    assert result["tally"]["counted"] == 4


def test_us_industry_is_available_not_unavailable():
    """美股 industry 可用(NASDAQ sector 聚合), 不得被标成不可用。"""
    result = mp.evaluate(_inputs(
        "us",
        regime={"state": "strong", "score": 78},
        breadth={"total": 2500, "up_pct": 65.0},
        hotspot_stages=["加速主升", "确认扩散"],
        industry={
            "leading": [{"name": "Technology", "avg_pct": 0.03}],
            "lagging": [{"name": "Energy", "avg_pct": -0.01}],
        },
    ))

    assert "industry" not in result["unavailable_dims"]
    assert _vote_of(result, "industry") == mp.VOTE_ATTACK
    assert result["tally"]["counted"] == 4
    assert result["posture"] == mp.POSTURE_ATTACK


def test_us_industry_defend_vote_counts():
    """美股领跌行业 ≤ -1% → industry 投防守, 且这张票要真进分母。"""
    result = mp.evaluate(_inputs(
        "us",
        regime={"state": "range", "score": 50},
        breadth={"total": 2500, "up_pct": 50.0},
        hotspot_stages=["降温退潮", "降温退潮"],
        industry={
            "leading": [{"name": "Utilities", "avg_pct": 0.002}],
            "lagging": [{"name": "Energy", "avg_pct": -0.03}],
        },
    ))

    assert _vote_of(result, "industry") == mp.VOTE_DEFEND
    assert result["tally"]["counted"] == 4
    assert result["tally"]["defend"] == 2
    # 2 张防守票 < 门槛 3 → 均衡, 不是防守
    assert result["posture"] == mp.POSTURE_BALANCED


# ─────────────────────────────────────────────────────────────
# §2.1: 阈值常量钉死
# ─────────────────────────────────────────────────────────────


def test_thresholds_pinned():
    """阈值常量必须与 PRD §3.2 面板 1 一致;改动必须同时改本测试(有意为之才放行)。"""
    assert mp.UP_PCT_FLOOR == 35.0
    assert mp.UP_PCT_ATTACK == 60.0
    assert mp.VETO_REGIME_STATES == ("weak", "lean_weak")
    assert mp.HOTSPOT_STAGE_RATIO == 0.5
    assert mp.INDUSTRY_ATTACK_PCT == 0.01
    assert mp.INDUSTRY_DEFEND_PCT == -0.01
    assert mp.MIN_VOTES_FOR_VERDICT == 3
    assert mp.VOTING_DIMS == ("regime", "breadth", "hotspots", "industry")


def test_unavailable_matrix_pinned():
    """不可用矩阵按市场分别声明: cn 0 个 / us 0 个 / hk 1 个(industry)。"""
    assert mp.STATIC_UNAVAILABLE_DIMS["cn"] == frozenset()
    assert mp.STATIC_UNAVAILABLE_DIMS["us"] == frozenset()
    assert mp.STATIC_UNAVAILABLE_DIMS["hk"] == frozenset({"industry"})


def test_up_pct_floor_boundary():
    """否决线边界: 恰好 35% 不否决, 34.9% 否决。"""
    kwargs = {
        "regime": {"state": "range", "score": 50},
        "hotspot_stages": ["初次异动"],
        "industry": {"leading": [{"name": "X", "avg_pct": 0.001}], "lagging": []},
    }
    at_floor = mp.evaluate(_inputs("cn", breadth={"total": 100, "up_pct": 35.0}, **kwargs))
    below = mp.evaluate(_inputs("cn", breadth={"total": 100, "up_pct": 34.9}, **kwargs))
    assert at_floor["veto"] is None
    assert below["veto"] is not None
    assert below["posture"] == mp.POSTURE_DEFEND


# ─────────────────────────────────────────────────────────────
# 采集层: 只读、不触发同步
# ─────────────────────────────────────────────────────────────


def test_collect_inputs_reads_regime_and_hotspots_readonly(tmp_path, monkeypatch):
    """collect_inputs 走 regime parquet + 热点快照只读路径, 不触发 discover/refresh。"""
    import polars as pl

    from app.services.hotspot.models import HotspotSummary

    data_dir = tmp_path / "data"
    (data_dir / "regime_history" / "cn").mkdir(parents=True)
    pl.DataFrame({"date": ["2026-09-18", "2026-09-19"], "state": ["range", "strong"],
                  "score": [50, 80]}).write_parquet(data_dir / "regime_history" / "cn" / "part.parquet")

    def _fake_read_topics(_data_dir, market="cn"):
        return [HotspotSummary(topic="t1", stage="加速主升"), HotspotSummary(topic="t2", stage="确认扩散")]

    monkeypatch.setattr("app.services.hotspot.storage.read_topics", _fake_read_topics)
    monkeypatch.setattr("app.services.hotspot.storage.read_job_state", lambda _d: {})

    inputs = mp.collect_inputs("cn", data_dir=data_dir)
    assert inputs.regime is not None
    assert inputs.regime["state"] == "strong"
    assert inputs.regime_as_of == "2026-09-19"
    assert inputs.hotspot_stages == ["加速主升", "确认扩散"]
    # 无 repo → A 股广度不可用(标 unavailable, 不是 0)
    assert inputs.breadth is None


def test_hotspots_refresh_path_never_called(monkeypatch, tmp_path):
    """硬约束: posture 不得走 discover(refresh=True)/refresh 这类外部源路径。"""
    import app.services.hotspot.service as hotspot_service

    def _boom(*args, **kwargs):
        raise AssertionError("posture 触发了热点同步路径")

    monkeypatch.setattr(hotspot_service, "discover_hotspots", _boom)
    monkeypatch.setattr(hotspot_service, "refresh_hotspots", _boom)
    monkeypatch.setattr("app.services.hotspot.storage.read_topics", lambda _d, market="cn": [])
    monkeypatch.setattr("app.services.hotspot.storage.read_job_state", lambda _d: {})

    result = mp.compute_market_posture("cn", data_dir=tmp_path)
    assert result["posture"] == mp.POSTURE_UNKNOWN


# ─────────────────────────────────────────────────────────────
# 路由层: GET /api/overview/posture
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def overview_client(monkeypatch):
    """只挂 overview 路由的隔离 app(避免主应用 lifespan 副作用)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.overview import invalidate_posture_cache
    from app.api.overview import router as overview_router

    invalidate_posture_cache()
    monkeypatch.setattr(
        "app.api.overview.compute_market_posture",
        lambda market, **kw: {
            "market": market, "posture": mp.POSTURE_BALANCED, "votes": [],
            "unavailable_dims": [], "evidence": [], "as_of": "2026-09-19",
        },
    )
    app = FastAPI()
    app.include_router(overview_router)
    yield TestClient(app)
    invalidate_posture_cache()


def test_posture_route_defaults_to_three_markets(overview_client):
    r = overview_client.get("/api/overview/posture")
    assert r.status_code == 200
    body = r.json()
    assert [m["market"] for m in body["markets"]] == ["cn", "hk", "us"]
    assert body["as_of"] == "2026-09-19"
    for item in body["markets"]:
        assert {"market", "posture", "votes", "unavailable_dims", "evidence"} <= set(item)


def test_posture_route_rejects_unknown_market(overview_client):
    r = overview_client.get("/api/overview/posture?markets=cn,jp")
    assert r.status_code == 400


def test_posture_route_cached(overview_client, monkeypatch):
    """60s 缓存生效: 第二次请求不重算(日级判断, 不能被高频打穿)。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.api.overview.compute_market_posture",
        lambda market, **kw: (calls.append(market), {
            "market": market, "posture": mp.POSTURE_BALANCED, "votes": [],
            "unavailable_dims": [], "evidence": [], "as_of": None,
        })[1],
    )
    overview_client.get("/api/overview/posture")
    overview_client.get("/api/overview/posture")
    assert len(calls) == 3


@pytest.mark.parametrize("market", ["cn", "hk", "us"])
def test_compute_market_posture_never_raises(market, tmp_path):
    """任何一路数据缺失都不能抛异常(整屏不能 500), 只会降级为 unavailable/unknown。"""
    result = mp.compute_market_posture(market, data_dir=tmp_path)
    assert result["market"] == market
    assert result["posture"] in {
        mp.POSTURE_ATTACK, mp.POSTURE_BALANCED, mp.POSTURE_DEFEND, mp.POSTURE_UNKNOWN,
    }
    assert result["posture"] == mp.POSTURE_UNKNOWN
