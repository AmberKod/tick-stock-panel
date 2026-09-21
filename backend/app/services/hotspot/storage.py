"""热点工作区持久化(polars parquet 写入 / 读取)。

数据布局:

    backend/data/hotspot/
    ├── <market>/topics.parquet      # 每个市场各自的最新快照 (cn / hk / us)
    ├── <market>/constituents/<topic>.parquet  # 每个市场各自的成分股
    ├── topics.parquet              # 【已废弃】迁移前的不分片快照, 首次访问即拆走
    ├── history/
    │   ├── topics.jsonl           # 每个交易日追加 topic 行 (带 market)
    │   └── constituents.jsonl     # 每个交易日追加每 topic 成分股行 (带 market)
    └── job_state.json              # 最近一次 sync 状态/时间戳

history 的 market 字段 (2026-09-20 起): 三个市场共用一个 jsonl, 同名 topic
只有靠 market 才能区分。**存量老行没有 market 字段 = market 未知**, 按 market
过滤时一律不匹配 (绝不倒推成 cn); 需要的话另做一次显式迁移, 不在默认路径里。

设计要点:
  - <market>/topics.parquet 是"当前活动快照", 按市场分片:某市场 sync 时只覆盖
    自己那一份, 不会抹掉别的市场(此前单文件会被最后查询的市场整文件覆盖)。
  - 首次读写时会把旧的不分片 topics.parquet 按行内 snapshot_market 拆到各分片。
  - <market>/constituents/<topic>.parquet 同样按市场分片:成分股只按 topic 名分文件,
    而港美产出的是行业名、A 股是概念名, 同名 topic 不分片就会互相整文件覆盖。
    该文件此前从未落过盘(目录不存在), 所以没有做旧数据迁移。
  - 与项目惯例一致,全部走 polars,避免混 pandas/json 各自适配。
  - 全部路径走 ``Path``,方便测试用临时目录。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.services.hotspot.models import QUALITY_OK, QUALITY_PARTIAL, HotspotStock, HotspotSummary
from app.services.hotspot.scoring import safe_float, safe_text

logger = logging.getLogger(__name__)


_HOTSPOT_DIR_NAME = "hotspot"
_TOPICS_FILE = "topics.parquet"
_LEGACY_TOPICS_FILE = "topics.parquet"
_CONSTITUENTS_DIR = "constituents"
_HISTORY_DIR = "history"
_JOB_STATE_FILE = "job_state.json"
_JOB_STATE_LOCK = threading.Lock()
_SNAPSHOT_PUBLISH_LOCK = threading.Lock()
_LEGACY_MIGRATION_LOCK = threading.Lock()
# 分片目录只接受这几个市场名; 其余值单独归目录, 绝不落到 cn 桶里冒充 A 股数据。
_SUPPORTED_TOPIC_MARKETS = ("cn", "hk", "us")


def hotspot_root(data_dir: Path) -> Path:
    """热点工作区根目录。"""
    return Path(data_dir) / _HOTSPOT_DIR_NAME


def topics_path(data_dir: Path, market: str = "cn") -> Path:
    """当前市场的快照路径: ``hotspot/<market>/topics.parquet``。"""
    return hotspot_root(data_dir) / _market_dir_name(market) / _TOPICS_FILE


def legacy_topics_path(data_dir: Path) -> Path:
    """迁移前的不分片快照路径(仅供一次性迁移使用)。"""
    return hotspot_root(data_dir) / _LEGACY_TOPICS_FILE


def constituents_dir(data_dir: Path, market: str = "cn") -> Path:
    """成分股目录: ``hotspot/<market>/constituents``。

    与 topics 一样按市场分片: 成分股只按 topic 名分文件, 而港美产出的是行业名、
    A 股是概念名, 撞名概率不低。不分片的话同名 topic 会互相整文件覆盖。
    """
    return hotspot_root(data_dir) / _market_dir_name(market) / _CONSTITUENTS_DIR


def history_dir(data_dir: Path) -> Path:
    return hotspot_root(data_dir) / _HISTORY_DIR


def job_state_path(data_dir: Path) -> Path:
    return hotspot_root(data_dir) / _JOB_STATE_FILE


# ---------------------------------------------------------------------------
# sanitize & path
# ---------------------------------------------------------------------------

_INVALID_PATH_CHARS = re.compile(r"[^一-鿿\w\s.\-]")


def safe_topic_filename(topic: str) -> str:
    """将 topic 名转为可作为文件名的安全串。"""
    cleaned = safe_text(topic)
    cleaned = _INVALID_PATH_CHARS.sub("_", cleaned).strip()
    if not cleaned:
        cleaned = "unnamed"
    return cleaned[:96]  # 防止极长名称


# ---------------------------------------------------------------------------
# topics 分片与市场归一化
# ---------------------------------------------------------------------------

_MARKET_DIR_RE = re.compile(r"[^a-z0-9_\-]")


def _market_dir_name(market: str) -> str:
    """市场分片目录名:归一化到小写;空值或非预期市场落 ``unknown``, 不混进 cn。"""
    key = _MARKET_DIR_RE.sub("_", safe_text(market).strip().lower())
    return key or "unknown"


def _snapshot_market_of(row: dict[str, Any]) -> str:
    """迁移用:取一行数据的所属市场, 空值/未知值归 ``cn``。

    与迁移前读取侧 ``snapshot_market in {"", "cn"}`` 的语义保持一致 ——
    没有 snapshot_market 字段的旧快照本来就只能被 A 股查询命中。
    """
    key = safe_text(row.get("snapshot_market")).strip().lower()
    return key if key in _SUPPORTED_TOPIC_MARKETS else "cn"


def _merge_into_shard(target: Path, rows: list[dict[str, Any]]) -> None:
    """把迁移行并入目标分片;已存在的 topic 以分片内现有行为准, 只补齐缺失行。"""
    existing: list[dict[str, Any]] = []
    if target.exists():
        try:
            existing = pl.read_parquet(target).to_dicts()
        except Exception as exc:
            # 分片损坏时以可解析的迁移行覆盖, 不做静默丢弃。
            logger.warning("_merge_into_shard: 读取 %s 失败, 以迁移行覆盖: %s", target, exc)
            existing = []
    known = {safe_text(row.get("topic")) for row in existing}
    merged = existing + [row for row in rows if safe_text(row.get("topic")) not in known]
    if not merged:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        [{key: row.get(key) for key in _TOPIC_SCHEMA} for row in merged],
        schema=_TOPIC_SCHEMA,
    )
    _write_parquet_snapshot(target, frame)


def _migrate_legacy_topics(data_dir: Path) -> None:
    """把迁移前的不分片 ``topics.parquet`` 按市场拆到各自分片(只做一次)。

    旧布局是整文件覆盖, 一次港美股查询就会把 A 股快照整份抹掉;反过来旧文件里
    也可能残留多个市场的行。这里按行内 ``snapshot_market`` 拆分, 每行都能落到
    它真正所属的市场, 拆分成功才删除旧文件;读取失败则原样保留, 下次访问再试。
    """
    legacy = legacy_topics_path(data_dir)
    if not legacy.exists():
        return
    with _LEGACY_MIGRATION_LOCK:
        if not legacy.exists():
            return
        try:
            rows = pl.read_parquet(legacy).to_dicts()
        except Exception as exc:
            logger.warning("_migrate_legacy_topics: 读取 %s 失败, 保留原文件: %s", legacy, exc)
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(_snapshot_market_of(row), []).append(row)
        for market, market_rows in grouped.items():
            _merge_into_shard(topics_path(data_dir, market), market_rows)
        legacy.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# topics.parquet
# ---------------------------------------------------------------------------

_CONSTITUENT_SCHEMA = {
    "code": pl.Utf8,
    "name": pl.Utf8,
    "change_pct": pl.Float64,
    "amount": pl.Float64,
    "turnover_rate": pl.Float64,
    "volume_ratio": pl.Float64,
    "net_inflow": pl.Float64,
    "is_limit_up": pl.Boolean,
    "active_days": pl.Int64,
    "evidence_count": pl.Int64,
    "role": pl.Utf8,
    "hot_stock_score": pl.Float64,
    "source": pl.Utf8,
    "source_confidence": pl.Float64,
    "fallback_used": pl.Boolean,
}

_TOPIC_SCHEMA = {
    "topic": pl.Utf8,
    "name": pl.Utf8,
    "source": pl.Utf8,
    "rank": pl.Int64,
    "change_pct": pl.Float64,
    "heat_score": pl.Float64,
    "trend_score": pl.Float64,
    "persistence_score": pl.Float64,
    "cooling_score": pl.Float64,
    "observations": pl.Int64,
    "state": pl.Utf8,
    "stage": pl.Utf8,
    "sample_stock_count": pl.Int64,
    "leaders": pl.List(pl.Utf8),
    "leader_stocks": pl.List(pl.Struct(_CONSTITUENT_SCHEMA)),
    "quality_status": pl.Utf8,
    "missing_fields": pl.List(pl.Utf8),
    "canonical_topic": pl.Utf8,
    "aliases": pl.List(pl.Utf8),
    "provider_used": pl.Utf8,
    "fallback_used": pl.Boolean,
    "source_errors": pl.List(pl.Utf8),
    "stale": pl.Boolean,
    "stale_age_hours": pl.Float64,
    "topic_date": pl.Utf8,
    "snapshot_at": pl.Utf8,
    "snapshot_market": pl.Utf8,
}


def _summary_to_dict(item: HotspotSummary) -> dict[str, Any]:
    """把 dataclass 序列化为 parquet 友好的 dict(含 list 字段)。"""
    return {
        "topic": safe_text(item.topic),
        "name": safe_text(item.name) or safe_text(item.topic),
        "source": safe_text(item.source),
        "rank": int(safe_float(item.rank) or 0) if item.rank is not None else None,
        "change_pct": safe_float(item.change_pct),
        "heat_score": float(safe_float(item.heat_score) or 0.0),
        "trend_score": safe_float(item.trend_score),
        "persistence_score": safe_float(item.persistence_score),
        "cooling_score": safe_float(item.cooling_score),
        "observations": int(safe_float(item.observations) or 0),
        "state": safe_text(item.state),
        # stage 未判定时原样存 None — 不要在这里兜 "初次异动": 读回时会被渲染
        # 成"数据判定它处于初次异动阶段", 而事实只是没观测到趋势三维度。
        "stage": safe_text(item.stage) or None,
        "sample_stock_count": int(safe_float(item.sample_stock_count) or 0),
        "leaders": list(item.leaders or []),
        "leader_stocks": [_stock_to_dict(stock) for stock in item.leader_stocks],
        "quality_status": safe_text(item.quality_status) or "partial",
        "missing_fields": list(item.missing_fields or []),
        "canonical_topic": safe_text(item.canonical_topic) or safe_text(item.topic),
        "aliases": list(item.aliases or []),
        "provider_used": safe_text(item.provider_used),
        "fallback_used": bool(item.fallback_used),
        "source_errors": list(item.source_errors or []),
        "stale": bool(item.stale),
        "stale_age_hours": safe_float(item.stale_age_hours),
        "topic_date": safe_text(item.topic_date),
        "snapshot_at": safe_text(item.snapshot_at),
        "snapshot_market": safe_text(item.snapshot_market),
    }


def _write_parquet_snapshot(path: Path, frame: pl.DataFrame) -> None:
    """用独占临时文件发布完整数据和元数据, 失败时保留上一份快照。"""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        frame.write_parquet(temporary)
        # Windows 的同时替换可能争用目标句柄, 只串行化最后一步发布。
        with _SNAPSHOT_PUBLISH_LOCK:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_topics(data_dir: Path, items: list[HotspotSummary], market: str = "cn") -> Path:
    """整文件覆盖写**指定市场**的 snapshot。空列表 → 删该市场分片。

    只影响 ``hotspot/<market>/topics.parquet``, 其他市场的快照不受影响。
    """
    _migrate_legacy_topics(data_dir)
    path = topics_path(data_dir, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        if path.exists():
            path.unlink()
        return path
    df = pl.DataFrame([_summary_to_dict(item) for item in items], schema=_TOPIC_SCHEMA)
    _write_parquet_snapshot(path, df)
    return path


def read_topics(data_dir: Path, market: str = "cn") -> list[HotspotSummary]:
    """读取指定市场的 snapshot;缺失则返回空列表。"""
    _migrate_legacy_topics(data_dir)
    path = topics_path(data_dir, market)
    if not path.exists():
        return []
    try:
        df = pl.read_parquet(path)
    except Exception as exc:
        logger.warning("read_topics: 读取 %s 失败:%s", path, exc)
        return []
    return [_row_to_summary(row) for row in df.to_dicts()]


def _row_to_summary(row: dict[str, Any]) -> HotspotSummary:
    """读回完整 summary, 旧缓存缺失的龙头详情明确列为 missing_fields。"""
    heat_score = safe_float(row.get("heat_score"), default=50.0)
    missing_fields = list(row.get("missing_fields") or [])
    if row.get("leader_stocks") is None and "leader_stocks" not in missing_fields:
        missing_fields.append("leader_stocks")
    quality_status = safe_text(row.get("quality_status")) or QUALITY_PARTIAL
    if missing_fields and quality_status == QUALITY_OK:
        quality_status = QUALITY_PARTIAL
    return HotspotSummary(
        topic=safe_text(row.get("topic")),
        name=safe_text(row.get("name")),
        source=safe_text(row.get("source")),
        rank=int(safe_float(row.get("rank")) or 0) if row.get("rank") is not None else None,
        change_pct=safe_float(row.get("change_pct")),
        heat_score=50.0 if heat_score is None else heat_score,
        trend_score=safe_float(row.get("trend_score")),
        persistence_score=safe_float(row.get("persistence_score")),
        cooling_score=safe_float(row.get("cooling_score")),
        observations=int(safe_float(row.get("observations")) or 0),
        state=safe_text(row.get("state")),
        stage=safe_text(row.get("stage")) or None,
        sample_stock_count=int(safe_float(row.get("sample_stock_count")) or 0),
        leaders=list(row.get("leaders") or []),
        leader_stocks=[_row_to_stock(stock) for stock in (row.get("leader_stocks") or [])],
        quality_status=quality_status,
        missing_fields=missing_fields,
        canonical_topic=safe_text(row.get("canonical_topic")),
        aliases=list(row.get("aliases") or []),
        provider_used=safe_text(row.get("provider_used")),
        fallback_used=bool(row.get("fallback_used")),
        source_errors=list(row.get("source_errors") or []),
        stale=bool(row.get("stale")),
        stale_age_hours=safe_float(row.get("stale_age_hours")),
        topic_date=safe_text(row.get("topic_date")),
        snapshot_at=safe_text(row.get("snapshot_at")),
        snapshot_market=safe_text(row.get("snapshot_market")),
    )


# ---------------------------------------------------------------------------
# constituents/<topic>.parquet
# ---------------------------------------------------------------------------

def _stock_to_dict(stock: HotspotStock) -> dict[str, Any]:
    return {
        "code": safe_text(stock.code),
        "name": safe_text(stock.name),
        "change_pct": safe_float(stock.change_pct),
        "amount": safe_float(stock.amount),
        "turnover_rate": safe_float(stock.turnover_rate),
        "volume_ratio": safe_float(stock.volume_ratio),
        "net_inflow": safe_float(stock.net_inflow),
        "is_limit_up": bool(stock.is_limit_up),
        "active_days": int(safe_float(stock.active_days) or 0),
        "evidence_count": int(safe_float(stock.evidence_count) or 0),
        "role": safe_text(stock.role),
        "hot_stock_score": float(safe_float(stock.hot_stock_score) or 0.0),
        "source": safe_text(stock.source),
        "source_confidence": safe_float(stock.source_confidence),
        "fallback_used": bool(stock.fallback_used),
    }


def write_constituents(
    data_dir: Path, topic: str, stocks: list[HotspotStock], market: str = "cn",
) -> Path | None:
    """写单个 topic 的成分股(写到该市场的分片下)。stocks 为空则删文件(保持目录干净)。"""
    if not stocks:
        delete_constituents(data_dir, topic, market=market)
        return None
    base = constituents_dir(data_dir, market)
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{safe_topic_filename(topic)}.parquet"
    df = pl.DataFrame([_stock_to_dict(s) for s in stocks], schema=_CONSTITUENT_SCHEMA)
    _write_parquet_snapshot(target, df)
    return target


def read_constituents(data_dir: Path, topic: str, market: str = "cn") -> list[HotspotStock]:
    path = constituents_dir(data_dir, market) / f"{safe_topic_filename(topic)}.parquet"
    if not path.exists():
        return []
    try:
        df = pl.read_parquet(path)
    except Exception as exc:
        logger.warning("read_constituents: 读取 %s 失败:%s", path, exc)
        return []
    return [_row_to_stock(row) for row in df.to_dicts()]


def delete_constituents(data_dir: Path, topic: str, market: str = "cn") -> None:
    path = constituents_dir(data_dir, market) / f"{safe_topic_filename(topic)}.parquet"
    if path.exists():
        path.unlink()


def _row_to_stock(row: dict[str, Any]) -> HotspotStock:
    return HotspotStock(
        code=safe_text(row.get("code")),
        name=safe_text(row.get("name")),
        change_pct=safe_float(row.get("change_pct")),
        amount=safe_float(row.get("amount")),
        turnover_rate=safe_float(row.get("turnover_rate")),
        volume_ratio=safe_float(row.get("volume_ratio")),
        net_inflow=safe_float(row.get("net_inflow")),
        is_limit_up=bool(row.get("is_limit_up")),
        active_days=int(safe_float(row.get("active_days")) or 0),
        evidence_count=int(safe_float(row.get("evidence_count")) or 0),
        role=safe_text(row.get("role")),
        hot_stock_score=float(safe_float(row.get("hot_stock_score")) or 0.0),
        source=safe_text(row.get("source")),
        source_confidence=safe_float(row.get("source_confidence")),
        fallback_used=bool(row.get("fallback_used")),
    )


# ---------------------------------------------------------------------------
# history JSONL
# ---------------------------------------------------------------------------

def append_history_row(
    data_dir: Path,
    items: Iterable[HotspotSummary],
    *,
    market: str,
    generated_at: str | None = None,
    filename: str = "topics.jsonl",
) -> Path:
    """把一批 summary 追加到 history JSONL,每行一条记录。

    ``market`` **必填**: 与按市场分片的 topics.parquet 不同, history 是三个市场
    共用的同一个 jsonl。港美开始落盘后, 同名 topic (A 股概念名 / 港美行业名) 在
    同一个文件里只有靠 market 才能区分, 否则任何回放/趋势都会把三个市场的数据
    当成一条时间序列。

    存量老行 (2026-09-20 之前写入) 没有这个字段 —— 它们的 market 是**未知**,
    读取侧一律不匹配任何 market (见 ``load_history_jsonl``), 绝不倒推成 "cn"。
    """
    base = history_dir(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    target = base / filename
    timestamp = generated_at or datetime.now(UTC).isoformat()
    with target.open("a", encoding="utf-8") as handle:
        for item in items:
            record = {
                "generated_at": timestamp,
                "market": safe_text(market),
                "topic": safe_text(item.topic),
                "name": safe_text(item.name) or safe_text(item.topic),
                "source": safe_text(item.source),
                "rank": item.rank,
                "change_pct": item.change_pct,
                "heat_score": item.heat_score,
                "trend_score": item.trend_score,
                "persistence_score": item.persistence_score,
                "cooling_score": item.cooling_score,
                "observations": item.observations,
                "state": item.state,
                "stage": item.stage,
                "sample_stock_count": item.sample_stock_count,
                "leaders": list(item.leaders or []),
                "quality_status": item.quality_status,
                "topic_date": item.topic_date,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


def load_history_jsonl(
    data_dir: Path,
    *,
    filename: str = "topics.jsonl",
    market: str | None = None,
) -> Iterator[dict[str, Any]]:
    """惰性遍历 history JSONL 行;跳过坏行。

    ``market`` 不为 None 时只返回 ``market`` 字段等于该值的行。

    ⚠️ 存量老行没有 market 字段, 它们的 market 是**未知**, 这里一律不匹配 ——
    哪怕它们的 source 看着像 A 股 (``cn_local_concept``), 那也只是旁证, 不是
    落盘时写下的事实。宁可让这批行在按 market 的口径里缺席, 也不伪造成 "cn"。
    """
    path = history_dir(data_dir) / filename
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if market is not None and safe_text(row.get("market")) != market:
            continue
        yield row


def write_constituents_history(
    data_dir: Path,
    topic: str,
    stocks: Iterable[HotspotStock],
    *,
    market: str,
    generated_at: str | None = None,
) -> Path:
    """把成分股追加到 constituents.jsonl,每行含 market+topic+stock 一行。

    ``market`` 必填, 理由同 ``append_history_row``: 成分股只按 topic 名分文件,
    而港美产出的是行业名、A 股是概念名, 撞名概率不低, 缺了 market 无法区分。
    """
    base = history_dir(data_dir)
    base.mkdir(parents=True, exist_ok=True)
    target = base / "constituents.jsonl"
    timestamp = generated_at or datetime.now(UTC).isoformat()
    with target.open("a", encoding="utf-8") as handle:
        for stock in stocks:
            record = {
                "generated_at": timestamp,
                "market": safe_text(market),
                "topic": topic,
                **_stock_to_dict(stock),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target


# ---------------------------------------------------------------------------
# job_state.json — 简易同步状态
# ---------------------------------------------------------------------------

def read_job_state(data_dir: Path) -> dict[str, Any]:
    path = job_state_path(data_dir)
    if not path.exists():
        return {"last_run": None, "last_status": None, "rows": 0, "markets": {}, "last_success_at": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            state["last_success_at"] = _success_times(state)
            return state
    except (OSError, ValueError):
        logger.warning("read_job_state: cannot read hotspot job state")
    return {"last_run": None, "last_status": None, "rows": 0, "markets": {}, "last_success_at": {}}


def _success_times(state: dict[str, Any]) -> dict[str, str]:
    """兼容旧 success.last_run, 按市场保留已知成功时间。"""
    stored = state.get("last_success_at")
    times = dict(stored) if isinstance(stored, dict) else {}
    last_run = safe_text(state.get("last_run"))
    if state.get("last_status") == "success" and last_run and state.get("rows", 0):
        markets = state.get("markets") or {state.get("market") or "cn": state["rows"]}
        for market, rows in markets.items():
            if rows:
                times.setdefault(market, last_run)
    return times


def write_job_state(data_dir: Path, payload: dict[str, Any]) -> Path:
    path = job_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 只串行化小文件的状态合并; 数据源请求和快照计算在锁外完成。
    with _JOB_STATE_LOCK:
        previous = read_job_state(data_dir)
        success_times = _success_times(previous)
        for market, timestamp in _success_times(payload).items():
            if market not in success_times or payload.get("last_status") == "success":
                success_times[market] = timestamp
        merged = {
            **payload,
            "markets": {**previous.get("markets", {}), **payload.get("markets", {})},
            "last_success_at": success_times,
        }
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

class HotspotStorage:
    """聚合 hot spot 持久化操作的浅封装(service 层使用)。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    @property
    def root(self) -> Path:
        return hotspot_root(self.data_dir)

    # topics
    def write_topics(self, items: list[HotspotSummary], market: str = "cn") -> Path:
        return write_topics(self.data_dir, items, market=market)

    def read_topics(self, market: str = "cn") -> list[HotspotSummary]:
        return read_topics(self.data_dir, market=market)

    # constituents
    def write_constituents(
        self, topic: str, stocks: list[HotspotStock], market: str = "cn",
    ) -> Path | None:
        return write_constituents(self.data_dir, topic, stocks, market=market)

    def read_constituents(self, topic: str, market: str = "cn") -> list[HotspotStock]:
        return read_constituents(self.data_dir, topic, market=market)

    def delete_constituents(self, topic: str, market: str = "cn") -> None:
        delete_constituents(self.data_dir, topic, market=market)

    # history
    def append_history(
        self,
        items: Iterable[HotspotSummary],
        *,
        market: str,
        **kwargs: Any,
    ) -> Path:
        return append_history_row(self.data_dir, items, market=market, **kwargs)

    def load_history(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        return load_history_jsonl(self.data_dir, **kwargs)

    def append_constituents_history(
        self,
        topic: str,
        stocks: Iterable[HotspotStock],
        *,
        market: str,
    ) -> Path:
        return write_constituents_history(self.data_dir, topic, stocks, market=market)

    # job state
    def read_job_state(self) -> dict[str, Any]:
        return read_job_state(self.data_dir)

    def write_job_state(self, payload: dict[str, Any]) -> Path:
        return write_job_state(self.data_dir, payload)
