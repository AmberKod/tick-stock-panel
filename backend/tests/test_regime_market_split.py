"""regime 按市场切分持久化测试(commit ① 配套)。

覆盖:
- _normalize_market 兜底与合法值
- 新切分路径 data/regime_history/{cn,hk,us}/part.parquet
- 老 cn 单文件路径兼容(legacy 兜底读)
- upsert 写新切分时不污染 cn 既有数据(分市场独立)
- regime_path 自动为各市场建子目录
- detect_stale_dates 港美按 per-symbol 目录整体比较

不触动 run_regime_batch 的指标计算层(那是 commit ② 的事),
只验证"持久化分目录 + market 参数兼容"这一层。
"""
from __future__ import annotations

import time
from datetime import date

import polars as pl

from app.services import regime_builder

# ───────────────────────── _normalize_market 兜底 ─────────────────────────


def test_normalize_market_canonical_passthrough():
    """合法三市场原样返回。"""
    assert regime_builder._normalize_market("cn") == "cn"
    assert regime_builder._normalize_market("hk") == "hk"
    assert regime_builder._normalize_market("us") == "us"


def test_normalize_market_defaults_to_cn():
    """None/未知输入兜底 cn。"""
    assert regime_builder._normalize_market(None) == "cn"
    assert regime_builder._normalize_market("") == "cn"
    assert regime_builder._normalize_market("xx") == "cn"


def test_normalize_market_aliases():
    """历史/业务别名归一到三市场。"""
    assert regime_builder._normalize_market("A") == "cn"
    assert regime_builder._normalize_market("cn_a") == "cn"
    assert regime_builder._normalize_market("HKEX") == "hk"
    assert regime_builder._normalize_market("港股") == "hk"
    assert regime_builder._normalize_market("us_market") == "us"
    assert regime_builder._normalize_market("美股") == "us"


# ───────────────────────── regime_path 分目录 ─────────────────────────


def test_regime_path_split_per_market(tmp_path):
    """三个市场落到各自的子目录, 不混。"""
    p_cn = regime_builder.regime_path(tmp_path, market="cn")
    p_hk = regime_builder.regime_path(tmp_path, market="hk")
    p_us = regime_builder.regime_path(tmp_path, market="us")
    assert p_cn == tmp_path / "regime_history" / "cn" / "part.parquet"
    assert p_hk == tmp_path / "regime_history" / "hk" / "part.parquet"
    assert p_us == tmp_path / "regime_history" / "us" / "part.parquet"


def test_regime_path_default_market_is_cn(tmp_path):
    """不传 market 默认 cn(老调用零回归)。"""
    p_default = regime_builder.regime_path(tmp_path)
    p_cn = regime_builder.regime_path(tmp_path, market="cn")
    assert p_default == p_cn


# ───────────────────────── upsert + 读: 各市场独立 ─────────────────────────


def _row(date_str: str, score: float) -> dict:
    return {"date": date.fromisoformat(date_str), "score": score, "state": "range"}


def _df(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema={"date": pl.Date, "score": pl.Float64, "state": pl.Utf8})
    return pl.DataFrame(rows, schema={"date": pl.Date, "score": pl.Float64, "state": pl.Utf8})


def test_upsert_and_load_per_market_independent(tmp_path):
    """三个市场各写各的, 互不污染: cn 写 a 行, hk/us 仍读为空。"""
    # cn 写一批
    cn_rows = _df([_row("2026-09-10", 60.0), _row("2026-09-11", 65.0)])
    regime_builder.upsert_regime_history(tmp_path, cn_rows, market="cn")
    assert (tmp_path / "regime_history" / "cn" / "part.parquet").exists()

    # hk / us 此时为空
    assert regime_builder.load_regime_history(tmp_path, market="hk").is_empty()
    assert regime_builder.load_regime_history(tmp_path, market="us").is_empty()

    # hk 写另一批
    hk_rows = _df([_row("2026-09-11", 50.0)])
    regime_builder.upsert_regime_history(tmp_path, hk_rows, market="hk")
    assert (tmp_path / "regime_history" / "hk" / "part.parquet").exists()

    # cn 仍是 cn 的两行, 不受 hk 影响
    cn_loaded = regime_builder.load_regime_history(tmp_path, market="cn")
    assert cn_loaded.height == 2
    assert sorted(cn_loaded["score"].to_list()) == [60.0, 65.0]


def test_upsert_creates_subdirs(tmp_path):
    """upsert 自动 mkdir 子目录(不需要调用方预建)。"""
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 50.0)]), market="hk"
    )
    assert (tmp_path / "regime_history" / "hk").exists()


def test_upsert_overwrites_same_date_no_duplicates(tmp_path):
    """同一天重复 upsert, 同一市场不应出现重复行(覆盖而非追加)。"""
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 50.0)]), market="cn"
    )
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 70.0)]), market="cn"
    )
    loaded = regime_builder.load_regime_history(tmp_path, market="cn")
    assert loaded.height == 1
    assert loaded["score"].to_list() == [70.0]


# ───────────────────────── 老单文件路径兼容 ─────────────────────────


def test_legacy_single_file_cn_fallback(tmp_path):
    """老迁移前的 data/regime_history/part.parquet 仍可读(cn 兜底)。"""
    legacy_dir = tmp_path / "regime_history"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "part.parquet"
    legacy_df = _df([_row("2026-09-09", 55.0)])
    legacy_df.write_parquet(legacy_path)

    # cn 找不到新切分文件时回退老路径
    loaded = regime_builder.load_regime_history(tmp_path, market="cn")
    assert loaded.height == 1
    assert loaded["score"].to_list() == [55.0]

    # hk/us 找不到新切分 = 空(不读 legacy)
    assert regime_builder.load_regime_history(tmp_path, market="hk").is_empty()
    assert regime_builder.load_regime_history(tmp_path, market="us").is_empty()


def test_new_path_takes_precedence_over_legacy(tmp_path):
    """新切分文件存在时, 不会再回退到老单文件 — 但 upsert 调用仍会"读到 legacy → 合并 →
    写入新切分路径",这是 commit ① 的设计意图(老 cn 数据零迁移自动并入)。

    这里检查"load 新切分路径"的窗口: 先 upsert 一次把 legacy 写入新路径, 此后直接
    读新切分应只看到新切分的内容, 不再去找 legacy。
    """
    legacy_dir = tmp_path / "regime_history"
    legacy_dir.mkdir(parents=True)
    # 老文件有一个 09-09 行
    _df([_row("2026-09-09", 55.0)]).write_parquet(legacy_dir / "part.parquet")
    # upsert: legacy 自动合并到新切分
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 70.0)]), market="cn"
    )

    # 现在新切分存在, load 只看新切分(legacy 不再被读)
    # 注: 上面 upsert 调用本身会把 legacy 合并到新切分, 所以现在 legacy 是"已迁移"状态
    loaded = regime_builder.load_regime_history(tmp_path, market="cn")
    # 应该看到 09-09(从 legacy 来) + 09-10(本次新增)两行
    assert loaded.height == 2
    dates = sorted(d.isoformat() for d in loaded["date"].to_list())
    assert dates == ["2026-09-09", "2026-09-10"]

    # 删除新切分路径再 load: 此时 legacy 不再被合并, 因为 legacy 在 upsert 时已被消费
    # — commit ① 仅承诺"读取 legacy 兜底",不写回 legacy。所以 legacy 的 mtime/内容
    # 不会被这次流程删除,但 legacy 在生产环境的"合并迁移"语义是上面这条 assertion 的形态。
    # 这里再验证: 完全删除新切分后, load 会回退到 legacy 兜底(legacy 此时仍存在)
    (tmp_path / "regime_history" / "cn" / "part.parquet").unlink()
    loaded_legacy_only = regime_builder.load_regime_history(tmp_path, market="cn")
    # 回退 legacy 兜底: 仅 09-09 一行
    assert loaded_legacy_only.height == 1
    assert loaded_legacy_only["date"].to_list()[0].isoformat() == "2026-09-09"


# ───────────────────────── refreshed phase 不串市场 ─────────────────────────


def test_refresh_phase_per_market_independent(tmp_path):
    """refresh_phase_labels 只刷新自己 market 的数据, 不影响其它市场。"""
    # 给 cn 写带阶段判定所需指标的"假"行(不需要真实, 走 is_empty 路径即可)
    cn_rows = _df([_row("2026-09-10", 60.0)])
    regime_builder.upsert_regime_history(tmp_path, cn_rows, market="cn")
    # cn 缺 phase 指标必填列, refresh 应当返回 0 但不抛
    n_cn = regime_builder.refresh_phase_labels(tmp_path, market="cn")
    assert n_cn == 0

    # hk 仍空目录, refresh 不影响(且不抛)
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 70.0)]), market="hk"
    )
    n_hk = regime_builder.refresh_phase_labels(tmp_path, market="hk")
    assert n_hk == 0

    # 两份数据仍然各自在自己目录
    cn_loaded = regime_builder.load_regime_history(tmp_path, market="cn")
    hk_loaded = regime_builder.load_regime_history(tmp_path, market="hk")
    assert cn_loaded["score"].to_list() == [60.0]
    assert hk_loaded["score"].to_list() == [70.0]


# ───────────────────────── coverage API 走分市场路径 ─────────────────────────


def test_regime_coverage_per_market(tmp_path):
    """get_regime_coverage 按市场独立返回元信息。"""
    # cn 写 1 行
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 50.0)]), market="cn"
    )
    cov_cn = regime_builder.get_regime_coverage(tmp_path, market="cn")
    assert cov_cn["rows"] == 1
    assert cov_cn["earliest_date"] == "2026-09-10"
    assert cov_cn["latest_date"] == "2026-09-10"

    # hk 空: rows=0
    cov_hk = regime_builder.get_regime_coverage(tmp_path, market="hk")
    assert cov_hk["rows"] == 0


# ───────────────────────── enriched_date_set 分市场 ─────────────────────────


def test_enriched_date_set_cn_default_returns_empty_for_fake_repo(tmp_path):
    """A 股 enriched 不存在时返回空集(repo 仅需 data_dir 属性)。"""
    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    assert regime_builder.enriched_date_set(_FakeRepo()) == set()


def test_enriched_date_set_cn_walks_date_partitioned_dir(tmp_path):
    """cn: 扫 kline_daily_enriched/date=*/part.parquet。"""
    enriched_dir = tmp_path / "kline_daily_enriched"
    for d in ("2026-09-10", "2026-09-11", "2026-09-12"):
        part_dir = enriched_dir / f"date={d}"
        part_dir.mkdir(parents=True)
        pl.DataFrame(
            {"date": [date.fromisoformat(d)]}
        ).write_parquet(part_dir / "part.parquet")

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    dates = regime_builder.enriched_date_set(_FakeRepo(), market="cn")
    assert dates == {
        date.fromisoformat("2026-09-10"),
        date.fromisoformat("2026-09-11"),
        date.fromisoformat("2026-09-12"),
    }


def test_enriched_date_set_hk_walks_symbol_partitioned_dir(tmp_path):
    """hk/us: 扫 kline_hk_us_enriched/symbol=*.HK/part.parquet, 取 distinct date。"""
    enriched_dir = tmp_path / "kline_hk_us_enriched"
    sym_a = enriched_dir / "symbol=00001.HK"
    sym_b = enriched_dir / "symbol=00002.HK"
    sym_a.mkdir(parents=True)
    sym_b.mkdir(parents=True)
    pl.DataFrame(
        {"date": [date(2026, 9, 10), date(2026, 9, 11)]}
    ).write_parquet(sym_a / "part.parquet")
    pl.DataFrame(
        {"date": [date(2026, 9, 11), date(2026, 9, 12)]}
    ).write_parquet(sym_b / "part.parquet")

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    hk_dates = regime_builder.enriched_date_set(_FakeRepo(), market="hk")
    assert hk_dates == {
        date(2026, 9, 10),
        date(2026, 9, 11),
        date(2026, 9, 12),
    }

    # us: 同目录但限定 .US 后缀, hk 测试不该影响 us
    assert regime_builder.enriched_date_set(_FakeRepo(), market="us") == set()


# ───────────────────────── detect_stale_dates 分市场 ─────────────────────────


def test_detect_stale_dates_cn_legacy_path(tmp_path):
    """老迁移前: cn 仍可用 legacy 路径做 stale 检测(配套 cn legacy)。"""
    # 旧 regime 写在 legacy 路径
    regime_dir = tmp_path / "regime_history"
    regime_dir.mkdir(parents=True)
    legacy_path = regime_dir / "part.parquet"
    legacy_df = _df([_row("2026-09-10", 50.0)])
    legacy_df.write_parquet(legacy_path)
    # 等几十毫秒确保 enriched mtime 更新
    time.sleep(0.05)
    # enriched 在 date 分区(更新 mtime)
    enriched_dir = tmp_path / "kline_daily_enriched"
    part_dir = enriched_dir / "date=2026-09-10"
    part_dir.mkdir(parents=True)
    pl.DataFrame({"date": [date(2026, 9, 10)]}).write_parquet(part_dir / "part.parquet")

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    stale = regime_builder.detect_stale_dates(tmp_path, _FakeRepo(), market="cn")
    assert date(2026, 9, 10) in stale


def test_detect_stale_dates_hk_uses_kline_hk_us_dir(tmp_path):
    """hk: 检测 kline_hk_us_enriched 任何 parquet mtime > regime hk 时整市场全量。"""
    # 给 hk 写一份 regime
    regime_builder.upsert_regime_history(
        tmp_path, _df([_row("2026-09-10", 60.0)]), market="hk"
    )
    time.sleep(0.05)
    # 在 kline_hk_us_enriched 新增 symbol parquet, mtime 必然更新
    enriched_dir = tmp_path / "kline_hk_us_enriched" / "symbol=00001.HK"
    enriched_dir.mkdir(parents=True)
    pl.DataFrame({"date": [date(2026, 9, 10)]}).write_parquet(enriched_dir / "part.parquet")

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    stale = regime_builder.detect_stale_dates(tmp_path, _FakeRepo(), market="hk")
    # 整市场全量重算(港美 per-symbol 特性)
    assert date(2026, 9, 10) in stale
