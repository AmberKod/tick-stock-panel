"""强度梯队(Strength Ladder)测试(commit ⑤ 配套)。

覆盖:
- _band_for: 各档位阈值边界
- compute_strength_ladder_for_day: 港美 enriched 含 momentum_20d 时正确落档 + 排序
- load_strength_ladder_history: 持久化后能读回
- upsert_strength_ladder: 按 (date, symbol) upsert, 不破坏其他行
- group_ladder_by_band: DataFrame → {band: [stock dict]} 转换
- API: ?market=hk / ?market=us 返回结构化梯队; market=cn 400; 无效 market 422;
  bands 过滤

不覆盖:
- A 股连板梯队(本批次不动, 维持现状)
- 前端标签切换(单独前端批次)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import strength_ladder

# ───────────────────────── _band_for ─────────────────────────


def test_band_for_threshold_boundaries():
    """动量阈值边界: 25/15/8/3 严格 ≥, 临界值归属高一档。"""
    assert strength_ladder._band_for(0.30) == "m25"
    assert strength_ladder._band_for(0.25) == "m25"
    assert strength_ladder._band_for(0.249) == "m15"
    assert strength_ladder._band_for(0.15) == "m15"
    assert strength_ladder._band_for(0.149) == "m8"
    assert strength_ladder._band_for(0.08) == "m8"
    assert strength_ladder._band_for(0.079) == "m3"
    assert strength_ladder._band_for(0.03) == "m3"
    assert strength_ladder._band_for(0.029) is None
    assert strength_ladder._band_for(-0.05) is None
    assert strength_ladder._band_for(None) is None


# ───────────────────────── compute_strength_ladder_for_day ─────────────────────────


def _write_hk_us_symbol(data_dir: Path, symbol: str, rows: list[dict]) -> None:
    """构造测试用港美 enriched 单文件 (per-symbol 全历史单文件)。"""
    sym_dir = data_dir / "kline_hk_us_enriched" / f"symbol={symbol}"
    sym_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(sym_dir / "part.parquet")


def test_compute_strength_ladder_filters_by_market_suffix(tmp_path):
    """hk: 只取 .HK 标的; us: 只取 .US 标的。"""
    d = date(2026, 9, 10)
    _write_hk_us_symbol(tmp_path, "00001.HK", [{
        "symbol": "00001.HK", "date": d, "close": 100.0, "amount": 1_000_000.0,
        "momentum_20d": 0.30,
    }])
    _write_hk_us_symbol(tmp_path, "AAPL.US", [{
        "symbol": "AAPL.US", "date": d, "close": 200.0, "amount": 5_000_000.0,
        "momentum_20d": 0.20,
    }])

    hk_ladder = strength_ladder.compute_strength_ladder_for_day(tmp_path, d, "hk")
    assert not hk_ladder.is_empty()
    assert hk_ladder["symbol"].unique().to_list() == ["00001.HK"]
    assert hk_ladder["band"].to_list() == ["m25"]   # 0.30 >= 0.25

    us_ladder = strength_ladder.compute_strength_ladder_for_day(tmp_path, d, "us")
    assert not us_ladder.is_empty()
    assert us_ladder["symbol"].unique().to_list() == ["AAPL.US"]
    assert us_ladder["band"].to_list() == ["m15"]   # 0.20 >= 0.15, < 0.25


def test_compute_strength_ladder_only_keeps_top_4_bands(tmp_path):
    """momentum < 0.03 的不入档; 各档位严格边界。"""
    d = date(2026, 9, 10)
    sym_dir = tmp_path / "kline_hk_us_enriched" / "symbol=00001.HK"
    sym_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([
        {"symbol": "00001.HK", "date": d, "momentum_20d": 0.30, "amount": 1.0, "close": 100.0},  # m25
        {"symbol": "00002.HK", "date": d, "momentum_20d": 0.20, "amount": 1.0, "close": 100.0},  # m15
        {"symbol": "00003.HK", "date": d, "momentum_20d": 0.10, "amount": 1.0, "close": 100.0},  # m8
        {"symbol": "00004.HK", "date": d, "momentum_20d": 0.05, "amount": 1.0, "close": 100.0},  # m3
        {"symbol": "00005.HK", "date": d, "momentum_20d": 0.01, "amount": 1.0, "close": 100.0},  # 不入档
        {"symbol": "00006.HK", "date": d, "momentum_20d": -0.05, "amount": 1.0, "close": 100.0}, # 不入档
    ]).write_parquet(sym_dir / "part.parquet")

    df = strength_ladder.compute_strength_ladder_for_day(tmp_path, d, "hk")
    bands = set(df["band"].to_list())
    assert bands == {"m25", "m15", "m8", "m3"}    # 不含 None band
    assert df.height == 4   # 不入档的 2 只被过滤


def test_compute_strength_ladder_rejects_market_cn(tmp_path):
    """market=cn 应抛 ValueError(本批次不覆盖 A 股连板梯队)。"""
    with pytest.raises(ValueError, match="不支持 market='cn'"):
        strength_ladder.compute_strength_ladder_for_day(tmp_path, date(2026, 9, 10), "cn")


def test_compute_strength_ladder_returns_empty_when_dir_missing(tmp_path):
    """kline_hk_us_enriched 不存在 → 空 DataFrame, 不抛。"""
    df = strength_ladder.compute_strength_ladder_for_day(tmp_path, date(2026, 9, 10), "hk")
    assert df.is_empty()


def test_compute_strength_ladder_returns_empty_when_no_momentum_column(tmp_path):
    """momentum_20d 列缺失 → 空 DataFrame。"""
    d = date(2026, 9, 10)
    sym_dir = tmp_path / "kline_hk_us_enriched" / "symbol=00001.HK"
    sym_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{
        "symbol": "00001.HK", "date": d, "close": 100.0, "amount": 1.0,
        # 无 momentum_20d 列
    }]).write_parquet(sym_dir / "part.parquet")

    df = strength_ladder.compute_strength_ladder_for_day(tmp_path, d, "hk")
    assert df.is_empty()


def test_compute_strength_ladder_sorted_by_band_priority(tmp_path):
    """结果按 (band 优先级, momentum 降序) 排序。"""
    d = date(2026, 9, 10)
    sym_dir = tmp_path / "kline_hk_us_enriched" / "symbol=00001.HK"
    sym_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([
        {"symbol": "00001.HK", "date": d, "momentum_20d": 0.04, "close": 100.0, "amount": 1.0},   # m3
        {"symbol": "00002.HK", "date": d, "momentum_20d": 0.28, "close": 100.0, "amount": 1.0},   # m25
        {"symbol": "00003.HK", "date": d, "momentum_20d": 0.16, "close": 100.0, "amount": 1.0},   # m15
        {"symbol": "00004.HK", "date": d, "momentum_20d": 0.26, "close": 100.0, "amount": 1.0},   # m25 (比 00002 弱)
    ]).write_parquet(sym_dir / "part.parquet")

    df = strength_ladder.compute_strength_ladder_for_day(tmp_path, d, "hk")
    bands = df["band"].to_list()
    assert bands == ["m25", "m25", "m15", "m3"]


# ───────────────────────── upsert + load ─────────────────────────


def test_upsert_and_load_roundtrip(tmp_path):
    """upsert 写 → load 读回, 内容一致。"""
    d = date(2026, 9, 10)
    df_in = pl.DataFrame({
        "date": [d, d],
        "market": ["hk", "hk"],
        "band": ["m25", "m15"],
        "symbol": ["00001.HK", "00002.HK"],
        "momentum_20d": [0.30, 0.18],
        "last_close": [100.0, 50.0],
        "amount": [1_000_000.0, 500_000.0],
    })
    strength_ladder.upsert_strength_ladder(tmp_path, df_in, "hk")
    df_out = strength_ladder.load_strength_ladder_history(tmp_path, "hk")
    assert df_out.height == 2
    assert sorted(df_out["symbol"].to_list()) == ["00001.HK", "00002.HK"]
    assert set(df_out["band"].to_list()) == {"m25", "m15"}


def test_upsert_overwrites_same_key_no_duplicates(tmp_path):
    """同 (date, symbol) 再次 upsert → 覆盖, 不出现重复行。"""
    d = date(2026, 9, 10)
    df_v1 = pl.DataFrame({
        "date": [d], "market": ["hk"], "band": ["m25"], "symbol": ["00001.HK"],
        "momentum_20d": [0.30], "last_close": [100.0], "amount": [1_000.0],
    })
    strength_ladder.upsert_strength_ladder(tmp_path, df_v1, "hk")

    df_v2 = pl.DataFrame({
        "date": [d], "market": ["hk"], "band": ["m25"], "symbol": ["00001.HK"],
        "momentum_20d": [0.35], "last_close": [110.0], "amount": [1_100.0],
    })
    strength_ladder.upsert_strength_ladder(tmp_path, df_v2, "hk")

    df_out = strength_ladder.load_strength_ladder_history(tmp_path, "hk")
    assert df_out.height == 1
    assert df_out["momentum_20d"].to_list() == [0.35]
    assert df_out["last_close"].to_list() == [110.0]


def test_load_filters_by_target_date(tmp_path):
    """target_date 过滤: 只返回指定日的行。"""
    d1, d2 = date(2026, 9, 10), date(2026, 9, 11)
    df_in = pl.DataFrame({
        "date": [d1, d2],
        "market": ["hk", "hk"],
        "band": ["m25", "m15"],
        "symbol": ["00001.HK", "00001.HK"],
        "momentum_20d": [0.30, 0.18],
        "last_close": [100.0, 105.0],
        "amount": [1.0, 1.0],
    })
    strength_ladder.upsert_strength_ladder(tmp_path, df_in, "hk")

    df_d1 = strength_ladder.load_strength_ladder_history(tmp_path, "hk", target_date=d1)
    assert df_d1.height == 1
    assert df_d1["date"].to_list()[0] == d1


def test_load_empty_returns_empty_dataframe(tmp_path):
    """持久化文件不存在 → 空 DataFrame。"""
    df = strength_ladder.load_strength_ladder_history(tmp_path, "hk")
    assert df.is_empty()


def test_load_cn_returns_empty():
    """load 调用 cn → 空 DataFrame(本批次不服务 cn)。"""
    df = strength_ladder.load_strength_ladder_history(Path("/tmp"), "cn")
    assert df.is_empty()


# ───────────────────────── group_ladder_by_band ─────────────────────────


def test_group_ladder_by_band_returns_dict_per_band():
    """group_ladder_by_band: 输出 {band: [stock dict, ...]}, 各 band 内部按 momentum 降序。"""
    df = pl.DataFrame({
        "date": [date(2026, 9, 10)] * 4,
        "market": ["hk"] * 4,
        "band": ["m25", "m25", "m15", "m8"],
        "symbol": ["A", "B", "C", "D"],
        "momentum_20d": [0.28, 0.35, 0.18, 0.10],
        "last_close": [10.0, 11.0, 5.0, 2.0],
        "amount": [100.0, 200.0, 50.0, 30.0],
    })
    grouped = strength_ladder.group_ladder_by_band(df)
    # m25 内按 momentum 降序: B(0.35) 在前
    assert grouped["m25"][0]["symbol"] == "B"
    assert grouped["m25"][1]["symbol"] == "A"
    assert grouped["m15"][0]["symbol"] == "C"
    assert grouped["m8"][0]["symbol"] == "D"
    # 没出现的 band 不在 dict 里
    assert "m3" not in grouped


def test_group_ladder_empty_returns_empty_dict():
    grouped = strength_ladder.group_ladder_by_band(pl.DataFrame())
    assert grouped == {}


# ───────────────────────── API ─────────────────────────


def _build_api_client(data_dir: Path):
    """构造 FastAPI app + TestClient, 把 data_dir 通过 middleware 注入。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.strength_ladder import router

    app = FastAPI()
    app.include_router(router)

    class _FakeStore:
        pass
    class _FakeRepo:
        store = _FakeStore()

    @app.middleware("http")
    async def _inject_repo(request, call_next):
        repo = _FakeRepo()
        repo.store.data_dir = data_dir
        request.app.state.repo = repo
        return await call_next(request)

    return TestClient(app)


def _seed_hk_ladder(tmp_path: Path, d: date) -> None:
    """种一份 4 档各 1 只的港股数据。"""
    df = pl.DataFrame({
        "date": [d] * 4,
        "market": ["hk"] * 4,
        "band": ["m25", "m15", "m8", "m3"],
        "symbol": ["00001.HK", "00002.HK", "00003.HK", "00004.HK"],
        "momentum_20d": [0.30, 0.18, 0.10, 0.05],
        "last_close": [100.0, 50.0, 30.0, 10.0],
        "amount": [1_000_000.0, 500_000.0, 300_000.0, 100_000.0],
    })
    strength_ladder.upsert_strength_ladder(tmp_path, df, "hk")


def test_api_returns_400_for_market_cn(tmp_path):
    """market=cn 应 400(本批次不覆盖 A 股连板梯队)。"""
    client = _build_api_client(tmp_path)
    r = client.get("/api/strength_ladder?market=cn")
    assert r.status_code == 400


def test_api_returns_422_for_invalid_market(tmp_path):
    client = _build_api_client(tmp_path)
    r = client.get("/api/strength_ladder?market=zz")
    assert r.status_code == 422


def test_api_us_falls_back_to_empty_dataframe(tmp_path):
    """market=us 时若没数据 → 返回 total_count=0, 不 500。"""
    client = _build_api_client(tmp_path)
    r = client.get("/api/strength_ladder?market=us")
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "us"
    assert body["bands"] == {}
    assert body["total_count"] == 0


def test_api_hk_with_seeded_data(tmp_path):
    """market=hk + 种子数据 → 返回 4 档各 1 只标的。"""
    d = date(2026, 9, 10)
    _seed_hk_ladder(tmp_path, d)
    client = _build_api_client(tmp_path)
    r = client.get(f"/api/strength_ladder?market=hk&date={d.isoformat()}")
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "hk"
    assert body["date"] == d.isoformat()
    assert set(body["bands"].keys()) == {"m25", "m15", "m8", "m3"}
    assert body["total_count"] == 4
    assert body["bands"]["m25"][0]["symbol"] == "00001.HK"
    assert body["bands"]["m25"][0]["momentum_20d"] == 0.30


def test_api_bands_filter(tmp_path):
    """bands=m25,m15 只返回这两档。"""
    d = date(2026, 9, 10)
    _seed_hk_ladder(tmp_path, d)
    client = _build_api_client(tmp_path)
    r = client.get(f"/api/strength_ladder?market=hk&date={d.isoformat()}&bands=m25,m15")
    assert r.status_code == 200
    body = r.json()
    assert set(body["bands"].keys()) == {"m25", "m15"}
    assert body["total_count"] == 2