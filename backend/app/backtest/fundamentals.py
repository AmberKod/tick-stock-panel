"""财务因子: 基于本地财务快照的点时 (point-in-time) 无未来函数接入。

数据契约:
- 输入为 data/financials/metrics/part.parquet, 每行一份报告期指标;
- ``announce_date`` 是公告日。因子只在 **严格晚于公告日的交易日** 才有值
  (公告多在盘后发布, 保守取 T+1 生效), 此前保持 null;
- 财报历史按 (symbol, period_end) 累积 (见 services/financial_sync.py),
  同一期以最新公告为准;
- 无财务数据的标的/日期一律为 null, 绝不填 0 (填 0 会污染截面排名,
  例如资产负债率 0 会被当成最优杠杆)。下游 IC/分层/评分对 null 自动剔除。

性能:
- 财务表约数千行, join_asof 按 symbol 分组回填, 对百万行面板的代价是
  毫秒级; 矩阵路径每个因子只物化一张 float32 TxN 矩阵 (T~900, N~5500
  约 20MB), 且仅在策略/挖掘请求该因子时才构建。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

# 财务因子名 -> (metrics 表列名, 是否需要除以收盘价)
# pb_latest 单列声明为 bps 倒数口径: 因子值 = close / bps。
FUNDAMENTAL_FACTORS: dict[str, dict[str, Any]] = {
    "pb_latest": {"column": "bps", "price_ratio": True},
    "roe_latest": {"column": "roe", "price_ratio": False},
    "gross_margin_latest": {"column": "gross_margin", "price_ratio": False},
    "net_margin_latest": {"column": "net_margin", "price_ratio": False},
    "revenue_yoy_latest": {"column": "revenue_yoy", "price_ratio": False},
    "net_income_yoy_latest": {"column": "net_income_yoy", "price_ratio": False},
    "debt_ratio_latest": {"column": "debt_to_asset_ratio", "price_ratio": False},
}

FUNDAMENTAL_FACTOR_NAMES = frozenset(FUNDAMENTAL_FACTORS)

# Existing public financial names plus market valuation aliases. These are
# derived at read time, never copied out of current HK instrument snapshots.
HK_FINANCIAL_ALIASES = {
    **{name: spec["column"] for name, spec in FUNDAMENTAL_FACTORS.items()},
    "pe_ttm": "eps_ttm", "pb": "bps", "raw_pb": "bps",
    "gross_margin": "gross_margin", "net_margin": "net_margin",
    "revenue_yoy": "revenue_yoy", "net_income_yoy": "net_income_yoy",
    "roe": "roe", "debt_to_asset_ratio": "debt_to_asset_ratio",
    "turnover_rate": "float_shares",
}
HK_FINANCIAL_NAMES = frozenset(HK_FINANCIAL_ALIASES)
HK_PRICE_RATIO_NAMES = frozenset({"pb_latest", "pb", "raw_pb", "pe_ttm"})
HK_DENOMINATOR_NAMES = HK_PRICE_RATIO_NAMES | {"turnover_rate"}
# ISO 4217 numeric codes let immutable numeric matrices retain historical
# currency identity without introducing object arrays into shared caches.
HK_CURRENCY_CODES = {"HKD": 344, "CNY": 156, "USD": 840}


def _hk_snapshot_signature(data_dir: Path) -> tuple:
    signature = []
    for filename in ("hk.parquet", "part.parquet"):
        path = data_dir / "financials" / "metrics" / filename
        try:
            stat = path.stat()
            signature.append((filename, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            signature.append((filename, 0, 0))
    return tuple(signature)


@lru_cache(maxsize=16)
def _load_hk_snapshot_cached(root: str, signature: tuple, columns: tuple[str, ...], today: date) -> pl.DataFrame | None:
    from app.enriched_generation import EnrichedGenerationUnavailableError
    from app.services.financial_sync import get_financial_df

    data_dir = Path(root)
    frame = get_financial_df(data_dir, "metrics", market="HK")
    if _hk_snapshot_signature(data_dir) != signature:
        raise EnrichedGenerationUnavailableError("港股财务数据在读取过程中发生变化")
    if frame.is_empty():
        return None
    return frame.select([
        "symbol", "period_end", "announce_date", "revision_id", "source",
        "report_currency", "field_provenance", *columns,
    ]).with_columns(pl.col("announce_date").alias("_announce")).sort(["symbol", "_announce", "period_end"])


def _is_hk_frame(frame: pl.DataFrame | None) -> bool:
    return frame is not None and not frame.is_empty() and "symbol" in frame.columns and bool(
        frame["symbol"].cast(pl.String).str.ends_with(".HK").all()
    )


def load_fundamental_snapshot(
    data_dir: Path | None, *, market: str | None = None, names: Any = None,
) -> pl.DataFrame | None:
    """读取财务指标快照; 文件缺失或无有效行时返回 None。

    返回列: symbol, _announce (Date), 以及各因子对应的 metrics 列。
    """
    if data_dir is None:
        return None
    if market is not None and market.upper() == "HK":
        requested = HK_FINANCIAL_NAMES if names is None else set(names) & HK_FINANCIAL_NAMES
        columns = {HK_FINANCIAL_ALIASES[name] for name in requested}
        if not columns:
            return None
        return _load_hk_snapshot_cached(str(data_dir.resolve()), _hk_snapshot_signature(data_dir), tuple(sorted(columns)), date.today())
    path = data_dir / "financials" / "metrics" / "part.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        logger.warning("读取财务指标快照失败: %s", exc)
        return None
    needed = {"symbol", "announce_date"} | {
        spec["column"] for spec in FUNDAMENTAL_FACTORS.values()
    }
    if not needed.issubset(frame.columns):
        logger.warning("财务指标快照缺少列: %s", sorted(needed - set(frame.columns)))
        return None
    snapshot = (
        frame.select(sorted(needed))
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("announce_date").is_not_null()
        )
        .with_columns(
            pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10).str.to_date().alias("_announce")
        )
        .sort(["symbol", "_announce"])
    )
    if snapshot.is_empty():
        return None
    return snapshot


def _hk_financial_timeline(snapshot: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """One event timeline supplies panel and matrix values, with strict publication gates."""
    from app.data_providers.hk_financial_provider import HK_FINANCIAL_SOURCE, financial_date

    events: dict[str, dict[date, list[dict]]] = {}
    for row in snapshot.iter_rows(named=True):
        announced = financial_date(row.get("announce_date"))
        period = financial_date(row.get("period_end"))
        if announced is None or period is None or period > announced:
            continue
        row["period_end"] = period
        row["announce_date"] = announced
        try:
            row["_provenance"] = json.loads(row.get("field_provenance") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(row["_provenance"], dict):
            continue
        # A field from a later publication cannot inherit a primary row's date.
        field_dates = [financial_date(info.get("announce_date")) for info in row["_provenance"].values() if isinstance(info, dict)]
        announced = max([announced, *(value for value in field_dates if value is not None)])
        row["announce_date"] = announced
        events.setdefault(row["symbol"], {}).setdefault(announced, []).append(row)
    timeline: list[dict] = []
    for symbol, dated in sorted(events.items()):
        state: dict[str, dict[date, tuple]] = {field: {} for field in columns}
        latest_period: date | None = None
        for announced, rows in sorted(dated.items()):
            for row in rows:
                latest_period = max(latest_period, row["period_end"]) if latest_period else row["period_end"]
                for field in columns:
                    value = row.get(field)
                    info = row["_provenance"].get(field)
                    if value is None or not isinstance(info, dict) or not np.isfinite(float(value)):
                        continue
                    priority = 10 if row.get("source") == HK_FINANCIAL_SOURCE else 0
                    key = row["period_end"]
                    previous = state[field].get(key)
                    candidate = (announced, priority, float(value), info, str(info.get("source") or row.get("source", "")))
                    if previous is not None and previous[0] == announced:
                        if previous[1] < priority:
                            continue
                        if previous[1] == priority and previous[2] != float(value):
                            # Two unsequenced versions on one publication date are ambiguous.
                            candidate = (announced, priority, None, info, str(row.get("source", "")))
                    state[field][key] = candidate
            result: dict[str, Any] = {"symbol": symbol, "__hk_fin_date": announced}
            for field in columns:
                # A newer published report starts a new reporting period for
                # every requested field. Missing fields may use another source
                # or revision of that period, never silently use an older one.
                selected = state[field].get(latest_period)
                prefix = f"__hk_fin_{field}"
                result[prefix] = selected[2] if selected else None
                info = selected[3] if selected and selected[2] is not None else {}
                basis = info.get("per_share_basis")
                basis = basis if isinstance(basis, dict) else {}
                result[prefix + "_basis_start"] = financial_date(basis.get("valid_from")) if basis.get("verified") is True else None
                result[prefix + "_basis_end"] = financial_date(basis.get("valid_to")) if basis.get("verified") is True else None
                result[prefix + "_currency"] = str(info.get("currency", "")).upper() or None
                result[prefix + "_source"] = selected[4] if selected and selected[2] is not None else None
            timeline.append(result)
    if not timeline:
        return pl.DataFrame()
    expressions = [pl.col("__hk_fin_date").cast(pl.Date)]
    for field in columns:
        prefix = f"__hk_fin_{field}"
        expressions.extend([pl.col(prefix).cast(pl.Float64), pl.col(prefix + "_basis_start").cast(pl.Date),
                            pl.col(prefix + "_basis_end").cast(pl.Date), pl.col(prefix + "_currency").cast(pl.String),
                            pl.col(prefix + "_source").cast(pl.String)])
    return pl.DataFrame(timeline, infer_schema_length=None).with_columns(expressions).sort(["symbol", "__hk_fin_date"])


def attach_hk_financial_fields(
    panel: pl.DataFrame, snapshot: pl.DataFrame | None, names: Any, *, include_provenance: bool = False,
) -> pl.DataFrame:
    """Attach requested HK fields, retaining previous reports on an announcement day."""
    requested = sorted(set(names) & HK_FINANCIAL_NAMES)
    if not requested or panel.is_empty():
        return panel
    columns = sorted({HK_FINANCIAL_ALIASES[name] for name in requested})
    timeline = _hk_financial_timeline(snapshot, columns) if snapshot is not None else pl.DataFrame()
    if timeline.is_empty():
        return panel.with_columns([pl.lit(None, dtype=pl.Float64).alias(name) for name in requested])
    result = panel.with_row_index("__hk_fin_order").sort(["symbol", "date"]).join_asof(
        timeline, left_on="date", right_on="__hk_fin_date", by="symbol", strategy="backward",
        allow_exact_matches=False, check_sortedness=False,
    )
    expressions = []
    for name in requested:
        field = HK_FINANCIAL_ALIASES[name]
        prefix = f"__hk_fin_{field}"
        value = pl.col(prefix)
        if name in HK_PRICE_RATIO_NAMES:
            if not {"raw_close", "currency", "raw_price_verified"}.issubset(result.columns):
                value = pl.lit(None, dtype=pl.Float64)
            else:
                valid = ((pl.col(prefix) > 0) & pl.col("raw_close").is_finite() & (pl.col("raw_close") > 0)
                         & (pl.col("raw_price_verified").cast(pl.Float64, strict=False) == 1.0).fill_null(False)
                         & (pl.col("currency") == pl.col(prefix + "_currency"))
                         & (pl.col("date") >= pl.col(prefix + "_basis_start"))
                         & (pl.col("date") <= pl.col(prefix + "_basis_end")))
                value = pl.when(valid).then(pl.col("raw_close") / pl.col(prefix)).otherwise(None)
        elif name == "turnover_rate":
            if not {"volume", "volume_unit"}.issubset(result.columns):
                value = pl.lit(None, dtype=pl.Float64)
            else:
                valid = ((pl.col(prefix) > 0) & pl.col("volume").is_finite() & (pl.col("volume") >= 0)
                         & (pl.col("volume_unit") == "share")
                         & (pl.col("date") >= pl.col(prefix + "_basis_start"))
                         & (pl.col("date") <= pl.col(prefix + "_basis_end")))
                value = pl.when(valid).then(pl.col("volume") / pl.col(prefix) * 100).otherwise(None)
        expressions.append(value.alias(name))
    result = result.with_columns(expressions).sort("__hk_fin_order")
    if include_provenance:
        return result.drop("__hk_fin_order")
    return result.drop([name for name in result.columns if name.startswith("__hk_fin_")])


def require_hk_financial_coverage(frame: pl.DataFrame, names: Any, *, context: str) -> None:
    """Reject an unsatisfied requested field instead of silently emptying all candidates."""
    requested = sorted(set(names) & HK_FINANCIAL_NAMES)
    if not requested or frame.is_empty():
        return
    missing = [name for name in requested if name not in frame.columns or not frame[name].cast(pl.Float64, strict=False).is_finite().any()]
    if missing:
        raise ValueError(f"港股历史财务不可计算({context}):{', '.join(missing)};目标日期之前缺少可核对的公告、字段或币种/每股基准")


def attach_fundamental_factors(
    panel: pl.DataFrame,
    snapshot: pl.DataFrame | None,
    names: Any,
) -> pl.DataFrame:
    """把财务因子列按公告日门控地并入日频面板。

    - snapshot 为 None (本地无财务数据): 产出全 null 列, 保持面板形状,
      由上层决定是否报"无财务数据"错误;
    - 面板必须已按 (symbol, date) 排序 (存储与挖掘路径均满足)。
    """
    if _is_hk_frame(snapshot) or _is_hk_frame(panel):
        return attach_hk_financial_fields(panel, snapshot, names)
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    missing_columns = [name for name in requested if name not in panel.columns]
    if not missing_columns:
        return panel

    if snapshot is None:
        return panel.with_columns([
            pl.lit(None, dtype=pl.Float64).alias(name)
            for name in missing_columns
        ])

    columns = sorted(
        {FUNDAMENTAL_FACTORS[name]["column"] for name in missing_columns}
    )
    right = snapshot.select(["symbol", "_announce", *columns]).sort(["symbol", "_announce"])
    joined = panel.join_asof(
        right,
        left_on="date",
        right_on="_announce",
        by="symbol",
        strategy="backward",
        check_sortedness=False,  # 双侧均已按 (symbol, key) 排序, 免除逐组检查开销
    )
    announced = pl.col("_announce").is_not_null() & (pl.col("date") > pl.col("_announce"))
    expressions = []
    for name in missing_columns:
        spec = FUNDAMENTAL_FACTORS[name]
        source = pl.col(spec["column"])
        if spec["price_ratio"]:
            value = (
                pl.when(source > 0)
                .then(pl.col("close") / source)
                .otherwise(None)
            )
        else:
            value = source
        expressions.append(
            pl.when(announced).then(value).otherwise(None).alias(name)
        )
    return joined.with_columns(expressions)


def build_fundamental_matrices(
    market: Any,
    snapshot: pl.DataFrame | None,
    names: Any,
    *,
    price_metadata: pl.DataFrame | None = None,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建财务因子 TxN float32 字段。

    与 attach_fundamental_factors 同一口径: 公告日次一交易日起前向填充,
    无数据为 NaN。pb 类因子在矩阵侧用 close / bps 现算。
    """
    if _is_hk_frame(snapshot) or all(str(symbol).endswith(".HK") for symbol in market.symbols):
        requested = sorted(set(names) & HK_FINANCIAL_NAMES)
        if not requested:
            return {}
        days = np.asarray([label[:10] for label in market.timestamp_labels], dtype="datetime64[D]")
        rows = {"symbol": np.tile(np.asarray(market.symbols), len(days)), "date": np.repeat(days, len(market.symbols))}
        if "turnover_rate" in requested:
            rows["volume"] = market.volume.reshape(-1)
            unit = market.fields.get("__hk_volume_is_shares")
            if unit is not None:
                rows["volume_unit"] = ["share" if value == 1 else None for value in unit.reshape(-1)]
        for field in ("raw_close", "raw_price_verified", "currency"):
            values = market.fields.get(field)
            if values is not None:
                rows[field] = values.reshape(-1)
        code = market.fields.get("__hk_currency_code")
        if "currency" not in rows and code is not None:
            inverse_codes = {value: name for name, value in HK_CURRENCY_CODES.items()}
            rows["currency"] = [inverse_codes.get(value) for value in code.reshape(-1)]
        panel = pl.DataFrame(rows)
        if price_metadata is not None and not price_metadata.is_empty() and set(requested) & HK_DENOMINATOR_NAMES:
            metadata_columns = [name for name in ("raw_close", "raw_price_verified", "currency", "volume", "volume_unit") if name in price_metadata.columns]
            panel = panel.drop([name for name in metadata_columns if name in panel.columns]).join(
                price_metadata.select("symbol", "date", *metadata_columns).unique(subset=["symbol", "date"], keep="none"),
                on=["symbol", "date"], how="left", maintain_order="left",
            )
        attached = attach_hk_financial_fields(panel, snapshot, requested)
        return {name: attached[name].to_numpy().astype(np.float32).reshape(market.shape) for name in requested}
    requested = [str(name) for name in names if str(name) in FUNDAMENTAL_FACTOR_NAMES]
    if not requested:
        return {}

    shape = market.shape
    result: dict[str, np.ndarray] = {}
    if snapshot is None:
        for name in requested:
            result[name] = np.full(shape, np.nan, dtype=np.float32)
        return result

    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    labels = market.timestamp_labels
    label_dates = np.array([label[:10] for label in labels], dtype="datetime64[D]")

    raw_columns = {
        FUNDAMENTAL_FACTORS[name]["column"]: np.full(shape, np.nan, dtype=np.float32)
        for name in requested
    }
    announce_text = snapshot["announce_date"].str.slice(0, 10)
    for row_index, symbol in enumerate(snapshot["symbol"].to_list()):
        column_index = asset_index.get(symbol)
        if column_index is None:
            continue
        announce = announce_text[row_index]
        if announce is None:
            continue
        # 公告日之后 (严格大于) 的首个时间行索引
        start = int(np.searchsorted(label_dates, np.datetime64(announce, "D"), side="right"))
        if start >= shape[0]:
            continue
        for column, target in raw_columns.items():
            value = snapshot[column][row_index]
            if value is None or not np.isfinite(float(value)):
                continue
            target[start:, column_index] = float(value)

    for name in requested:
        spec = FUNDAMENTAL_FACTORS[name]
        source = raw_columns[spec["column"]]
        if spec["price_ratio"]:
            with np.errstate(divide="ignore", invalid="ignore"):
                matrix = (market.close / source).astype(np.float32)
            matrix[~(source > 0)] = np.nan
            matrix[np.isinf(matrix)] = np.nan
        else:
            matrix = source
        result[name] = matrix
    return result


def attach_matrix_fundamental_fields(
    market: Any, data_dir: Path | None, names: Any, *, price_metadata: pl.DataFrame | None = None,
) -> Any:
    """把财务因子作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。"""
    import dataclasses

    is_hk = bool(market.symbols) and all(str(symbol).endswith(".HK") for symbol in market.symbols)
    accepted = HK_FINANCIAL_NAMES if is_hk else FUNDAMENTAL_FACTOR_NAMES
    requested = [str(name) for name in names if str(name) in accepted]
    if not requested:
        return market
    snapshot = load_fundamental_snapshot(data_dir, market="HK", names=requested) if is_hk else load_fundamental_snapshot(data_dir)
    extra = build_fundamental_matrices(market, snapshot, requested, price_metadata=price_metadata)
    if not extra:
        return market
    merged = {**dict(market.fields), **extra}
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(market, fields=MappingProxyType(merged))
