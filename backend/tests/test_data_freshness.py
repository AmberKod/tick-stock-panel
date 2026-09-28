"""数据新鲜度画像单测。

覆盖: 各分区布局 (A股 per-date / 港美 per-symbol) 的日期提取、
五种状态判定、缺口区间不倒挂。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import data_freshness

TODAY = date(2026, 9, 18)


def _write(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [date.fromisoformat(r[1]) for r in rows],
            "close": [1.0] * len(rows),
        }
    ).write_parquet(path)


# 撑起历史深度的基线日期: 不写它的话每个用例都只有几天历史,
# 会被统一判成 shallow, 掩盖真正要测的状态。
_OLD = "2025-01-02"


def _cn(root: Path, table: str, days: list[str], *, history: bool = True) -> None:
    for d in ([_OLD] if history else []) + days:
        _write(root / table / f"date={d}" / "part.parquet", [("000001.SZ", d)])


def _sym(root: Path, table: str, symbol: str, days: list[str], *, history: bool = True) -> None:
    rows = ([_OLD] if history else []) + days
    _write(root / table / f"symbol={symbol}" / "part.parquet",
           [(symbol, d) for d in rows])


# ── A 股 (per-date 分区) ───────────────────────────────────────


def test_cn_ok_when_within_tolerance(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-15", "2026-09-16", "2026-09-17"])
    _cn(tmp_path, "kline_daily", ["2026-09-16", "2026-09-17"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "ok"
    assert r["latest_date"] == "2026-09-17"
    assert r["earliest_date"] == _OLD
    assert r["stale_days"] == 1
    assert r["gap"] is None
    assert r["coverage_unit_label"] == "交易日"


def test_cn_stale_beyond_tolerance(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-08", "2026-09-10"])
    _cn(tmp_path, "kline_daily", ["2026-09-10"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "stale"
    assert r["gap"]["from"] == "2026-09-11"
    assert r["gap"]["to"] == "2026-09-18"
    assert r["gap"]["missing_days"] == 8


def test_cn_behind_raw_when_enriched_lags(tmp_path):
    """原始数据已到位但 enriched 没算 → 不是'没拉到', 是'没算'。"""
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-15"])
    _cn(tmp_path, "kline_daily", ["2026-09-16", "2026-09-17"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "behind_raw"
    assert r["gap"]["from"] == "2026-09-16"
    assert r["gap"]["to"] == "2026-09-17"


def test_empty_market_has_no_gap(tmp_path):
    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "empty"
    assert r["latest_date"] is None
    assert r["gap"] is None  # 全新部署不编造区间


# ── 港美股 (per-symbol 分区, 抽样) ──────────────────────────────


def test_hk_modal_date_and_coverage(tmp_path):
    # 8 只标的在最新日, 2 只落后一天 → 众数 = 09-18, 覆盖率 0.8
    for i in range(8):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK",
             ["2026-09-16", "2026-09-17", "2026-09-18"])
    for i in range(8, 10):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK",
             ["2026-09-16", "2026-09-17"])
    for i in range(10):
        _sym(tmp_path, "kline_daily", f"{i:05d}.HK", ["2026-09-18"])

    r = data_freshness.market_freshness(tmp_path, "HK", today=TODAY, sample=10)
    assert r["status"] == "ok"
    assert r["latest_date"] == "2026-09-18"
    assert r["coverage_ratio"] == pytest.approx(0.8)
    assert r["coverage_units"] == 10
    assert r["coverage_unit_label"] == "标的"


def test_hk_partial_when_symbols_scattered(tmp_path):
    """各标的停在不同日期 → 众数占比低, 判定为部分同步。"""
    targets = ["2026-09-18", "2026-09-17", "2026-09-16", "2026-09-15", "2026-09-14"]
    for i in range(10):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK", [targets[i % 5]])
        _sym(tmp_path, "kline_daily", f"{i:05d}.HK", ["2026-09-18"])

    r = data_freshness.market_freshness(tmp_path, "HK", today=TODAY, sample=10)
    assert r["status"] == "partial"
    assert r["coverage_ratio"] <= 0.5
    # 日期已到最新, 缺口区间退化成"当天", 绝不出现 from > to
    assert r["gap"]["from"] == r["gap"]["to"]
    assert r["gap"]["missing_days"] == 1


def test_us_stale_gap(tmp_path):
    for i in range(5):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.US", ["2026-09-10", "2026-09-11"])
        _sym(tmp_path, "kline_daily", f"{i:05d}.US", ["2026-09-11"])

    r = data_freshness.market_freshness(tmp_path, "US", today=TODAY, sample=5)
    assert r["status"] == "stale"
    assert r["gap"]["from"] == "2026-09-12"
    assert r["gap"]["to"] == "2026-09-18"


# ── 历史深度不足 ───────────────────────────────────────────────


def test_history_insufficient_suggests_longer_range(tmp_path):
    """只有 10 天历史 → 建议补一整年, 而不是只补尾部几天。"""
    recent = [f"2026-09-{d:02d}" for d in range(8, 18)]
    _cn(tmp_path, "kline_daily_enriched", recent, history=False)
    _cn(tmp_path, "kline_daily", recent, history=False)

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "shallow"
    assert r["history_insufficient"] is True
    assert r["gap"]["from"] == "2025-09-18"   # today - 365
    assert r["gap"]["to"] == "2026-09-18"


# ── 聚合层 ────────────────────────────────────────────────────


def test_get_data_freshness_marks_sampled_cache(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-17"])

    first = data_freshness.get_data_freshness(tmp_path, ("CN",), active_job={"id": "j1"})
    assert first["cached"] is False
    assert first["active_job"] == {"id": "j1"}

    second = data_freshness.get_data_freshness(tmp_path, ("CN",), active_job=None)
    assert second["cached"] is True
    assert second["active_job"] is None  # 活跃任务实时注入, 不走缓存

    data_freshness.invalidate_cache()
    assert data_freshness.get_data_freshness(tmp_path, ("CN",)) ["cached"] is False


def test_unknown_market_degrades_gracefully(tmp_path):
    payload = data_freshness.get_data_freshness(tmp_path, ("CN", "HK", "US"))
    assert len(payload["markets"]) == 3
    assert {m["market"] for m in payload["markets"]} == {"CN", "HK", "US"}


# ── API 层: 活跃任务市场推断 ────────────────────────────────────


def test_job_market_inference():
    from app.api.data import _job_market

    assert _job_market({"result": {"market": "HK"}}, None) == "HK"
    assert _job_market({}, "HK 日K同步 1629/2816") == "HK"
    assert _job_market({}, "美股日K同步 3/6071") == "US"
    assert _job_market({}, "A股盘后管道") == "CN"
    assert _job_market({}, "无市场信息") is None


# ── A1: 港美同步健康度 (sync_health) ───────────────────────────
# 背景: 09-25 HK job 失败但 freshness 判 ok (parquet 日期未落后);
# 09-27 US job 标 succeeded 但 6071 只零落盘。sync_health 补 job 失败态感知。


def _fake_market_daily_job(
    *, market: str, status: str, finished: str,
    failed: list[str] | None = None, completed: list[str] | None = None,
    total: int | None = None, error: str | None = None,
) -> dict:
    """构造 market_daily 类 job 的 _summary 形态 (list_recent 返回结构)。"""
    log = [{"stage": "sync_instruments", "msg": f"同步 {market} 全量标的池…"}]
    return {
        "id": f"job-{market.lower()}-{finished}",
        "status": status,
        "stage": "market_daily_sync",
        "progress": 100 if status == "succeeded" else 0,
        "stage_pct": 0,
        "started_at": finished,
        "finished_at": finished,
        "duration_s": 1.0,
        "result": None if status == "failed" else {
            "market": market,
            "operation": "daily_download",
            "symbols_total": total,
            "completed_symbols": completed or [],
            "failed_symbols": failed or [],
        },
        "error": error,
        "log": log,
    }


def _patch_jobs(monkeypatch: pytest.MonkeyPatch, jobs: list[dict]) -> None:
    """把 JobStore 单例的 list_recent 换成喂假 job 的桩。

    注意: app.api.data 顶部的 ``job_store`` 是**模块**别名, 单例在
    app.services.pipeline_jobs.job_store — _sync_health_for 内部取的就是单例,
    桩也必须打在单例上。
    """
    from app.services.pipeline_jobs import job_store as store

    monkeypatch.setattr(store, "list_recent", lambda limit=20: jobs[:limit])
    monkeypatch.setattr(store, "active_id", lambda: None)


def test_sync_health_failed_job(monkeypatch: pytest.MonkeyPatch):
    """09-25 事故重放: job failed (universe 失败, 无 result) → job_failed。"""
    from app.api.data import _sync_health_for

    job = _fake_market_daily_job(
        market="HK", status="failed", finished="2026-09-25T10:00:00Z",
        error="港股全量 instruments 获取失败，拒绝写入 demo 快照",
    )
    # 失败 job 无 result → market 从日志文本推断
    job["result"] = None
    _patch_jobs(monkeypatch, [job])
    # 固定 now 避免依赖真实时钟
    import datetime as _dt

    frozen = _dt.datetime(2026, 9, 25, 12, 0, tzinfo=_dt.UTC)
    out = _sync_health_for("HK", now=frozen)
    assert out["sync_health"] == "job_failed"
    assert "instruments" in out["sync_health_detail"]


def test_sync_health_mostly_failed_fake_success(monkeypatch: pytest.MonkeyPatch):
    """09-27 事故重放: succeeded 但 6071/6071 全失败 → mostly_failed。"""
    import datetime as _dt

    from app.api.data import _sync_health_for

    job = _fake_market_daily_job(
        market="US", status="succeeded", finished="2026-09-27T05:59:01Z",
        failed=[f"{i:05d}.US" for i in range(6071)],
        completed=[],
        total=6071,
    )
    _patch_jobs(monkeypatch, [job])
    frozen = _dt.datetime(2026, 9, 27, 12, 0, tzinfo=_dt.UTC)
    out = _sync_health_for("US", now=frozen)
    assert out["sync_health"] == "mostly_failed"
    assert "6071/6071" in out["sync_health_detail"]


def test_sync_health_ok(monkeypatch: pytest.MonkeyPatch):
    """正常: succeeded 且失败占比 ≤ 0.5 → ok。"""
    import datetime as _dt

    from app.api.data import _sync_health_for

    job = _fake_market_daily_job(
        market="HK", status="succeeded", finished="2026-09-26T11:00:00Z",
        failed=["00001.HK"], completed=["00700.HK", "09988.HK", "03690.HK"],
        total=3,
    )
    _patch_jobs(monkeypatch, [job])
    frozen = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)
    out = _sync_health_for("HK", now=frozen)
    assert out["sync_health"] == "ok"


def test_sync_health_no_recent_run(monkeypatch: pytest.MonkeyPatch):
    """26h 窗口外 / 无任何 job → no_recent_run。"""
    import datetime as _dt

    from app.api.data import _sync_health_for

    old_job = _fake_market_daily_job(
        market="HK", status="succeeded", finished="2026-09-01T10:00:00Z",
        completed=["00700.HK"], total=1,
    )
    _patch_jobs(monkeypatch, [old_job])
    frozen = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)
    assert _sync_health_for("HK", now=frozen)["sync_health"] == "no_recent_run"

    # job_store 读取抛错也降级 no_recent_run, 不让状态栏 500
    from app.services.pipeline_jobs import job_store as store

    def _boom(limit=20):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(store, "list_recent", _boom)
    out = _sync_health_for("HK", now=frozen)
    assert out["sync_health"] == "no_recent_run"
    assert "读取失败" in out["sync_health_detail"]


def test_sync_health_ignores_other_market_and_other_jobs(monkeypatch: pytest.MonkeyPatch):
    """A 股管道 job (daily_days) / 其他市场 job 不影响 HK 判定。"""
    import datetime as _dt

    from app.api.data import _sync_health_for

    cn_job = {
        "id": "cn-pipeline", "status": "failed", "stage": "enriched",
        "progress": 0, "stage_pct": 0,
        "started_at": "2026-09-26T09:00:00Z", "finished_at": "2026-09-26T09:30:00Z",
        "duration_s": 1800, "result": {"daily_days": 300}, "error": "boom",
        "log": [{"stage": "enriched", "msg": "重算指标"}],
    }
    us_job = _fake_market_daily_job(
        market="US", status="failed", finished="2026-09-26T09:40:00Z",
        error="US down",
    )
    hk_ok = _fake_market_daily_job(
        market="HK", status="succeeded", finished="2026-09-26T10:00:00Z",
        completed=["00700.HK"], total=1,
    )
    _patch_jobs(monkeypatch, [us_job, cn_job, hk_ok])
    frozen = _dt.datetime(2026, 9, 26, 12, 0, tzinfo=_dt.UTC)
    assert _sync_health_for("HK", now=frozen)["sync_health"] == "ok"
    assert _sync_health_for("US", now=frozen)["sync_health"] == "job_failed"
    # A 股管道 job 不带市场文本 → 不参与 HK/US 判定


def test_freshness_endpoint_injects_sync_health(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """/api/data/freshness 响应中 HK/US 带 sync_health, CN 不带。"""
    import datetime as _dt

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.data as data_api
    from app.services.pipeline_jobs import job_store as store

    hk_failed = _fake_market_daily_job(
        market="HK", status="failed", finished=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        error="港股全量 instruments 获取失败",
    )
    monkeypatch.setattr(store, "list_recent", lambda limit=20: [hk_failed])
    monkeypatch.setattr(store, "active_id", lambda: None)

    app = FastAPI()
    app.include_router(data_api.router)

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    @app.middleware("http")
    async def _inject_repo(request, call_next):
        request.app.state.repo = _FakeRepo()
        return await call_next(request)

    # 不用 with 上下文管理器: 进程退出阶段的 portal 清理在部分环境会拖死 pytest
    client = TestClient(app)
    resp = client.get("/api/data/freshness")
    client.close()
    assert resp.status_code == 200
    body = resp.json()
    by_market = {item["market"]: item for item in body["markets"]}
    assert by_market["HK"]["sync_health"] == "job_failed"
    # US 无 job 记录 → no_recent_run; CN 走盘后管道不注入
    assert by_market["US"]["sync_health"] == "no_recent_run"
    assert "sync_health" not in by_market["CN"] or by_market["CN"].get("sync_health") is None
