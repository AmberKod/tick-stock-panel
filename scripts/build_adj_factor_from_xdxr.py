"""从通达信除权事件 + 本地日K 生成 A 股复权因子 adj_factor/all.parquet (容器内执行)。

背景
----
``data/adj_factor`` 一直是 0 个 parquet(TickFlow ex_factors 对免费档返回 0 行),
enriched 的前复权从未生效(raw == adj)。tdx_xdxr_provider.adj_ratios 已经把
"事件 → 每日因子"的算法跑通, 但它走腾讯拉日K、上限 640 根, 不适合批量落盘。

本脚本在回填历史完成后运行:
  1. 前收盘直接读本地 kline_daily (全历史, 不依赖外部源);
  2. 除权事件读通达信 GetXdxrInfoCmd(0x0f00) (真逐条权益公告, 非黑盒比值);
  3. 每个事件算出**事件级**比值 r = (前收 - 每股分红 + 配股价×配股比)
       / ((1 + 送转比 + 配股比) × 前收);
  4. 写 (symbol, trade_date, ex_factor=r) 到 adj_factor/all.parquet ——
     正是 indicators/pipeline._apply_adj_factor 期望的 schema(事件级、非累积,
     管道内部自己做 cum_prod + join_asof)。

口径注释
--------
- 除权日若停牌(本地无该日 bar), r 生效日顺延到复牌首日(其前一根作前收)。
- 事件早于本地数据起点(前收拿不到)的跳过并计数 —— 不猜。2015 起的数据窗口内,
  这只影响"数据起点当天恰好除权"的极端情况。
- 拿不到事件的票(从未除权/新股)天然没有行, 与 TickFlow 版行为一致。

用法
----
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/build_adj_factor_from_xdxr.py
    # 之后全量重算 enriched (前复权生效):
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/build_adj_factor_from_xdxr.py --rebuild-enriched
    # 试跑 200 只
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/build_adj_factor_from_xdxr.py --limit-symbols 200
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import polars as pl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("adj_factor")


def _bootstrap() -> None:
    here = Path(__file__).resolve()
    for cand in (here.parents[1], Path.cwd()):
        if (cand / "app" / "tickflow").is_dir():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return
    raise SystemExit("找不到 app 包根目录, 请在容器内 (/app) 运行本脚本")


_bootstrap()

from app.data_providers.tdx_xdxr_provider import TdxXdxrUnavailableError, xdxr_events  # noqa: E402


def load_local_closes(data_dir: Path) -> dict[str, list[tuple[str, float]]]:
    """从本地 kline_daily 读每票升序 [(date, close)]。"""
    lf = (
        pl.scan_parquet(str(data_dir / "kline_daily" / "date=*" / "*.parquet"))
        .select("symbol", "date", "close")
        .filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
    )
    df = lf.collect()
    out: dict[str, list[tuple[str, float]]] = {}
    for sym_t, sub in df.sort("date").group_by("symbol"):
        sym = sym_t[0] if isinstance(sym_t, (tuple, list)) else sym_t
        # date32 collect 后是 datetime.date; xdxr 事件日期是 'YYYY-MM-DD' 字符串,
        # 统一成 str 才能和 effective_index 的 bisect 比较。
        ds = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in sub["date"].to_list()]
        out[sym] = list(
            zip(ds, sub["close"].to_list(), strict=True)
        )
    logger.info("本地日K覆盖 %d 只", len(out))
    return out


def event_ratio(ev: dict, prev_close: float) -> float | None:
    """事件级除权比值; 参数异常返回 None(不猜)。"""
    cash = ev.get("cash_per_share") or 0.0
    bonus = ev.get("bonus_share_ratio") or 0.0
    rights = ev.get("rights_issue_ratio") or 0.0
    rights_price = ev.get("rights_issue_price") or 0.0
    den = (1.0 + bonus + rights) * prev_close
    num = prev_close - cash + rights_price * rights
    if den <= 0 or num <= 0:
        return None
    return num / den


def effective_index(dates: list[str], event_date: str) -> int | None:
    """事件生效的本地 bar 下标: 精确命中, 否则顺延到第一个 >= event_date 的交易日。"""
    import bisect

    i = bisect.bisect_left(dates, event_date)
    return i if i < len(dates) else None


def build_one(symbol: str, bars: list[tuple[str, float]]) -> tuple[list[dict], int]:
    """单票 → ([(trade_date, ex_factor)], skipped_gap)。

    xdxr_events 只收 6 位纯代码(见 _market_for), 传 '000001.SZ' 会被
    fail-closed 拦掉, 所以这里先剥后缀再问, 落盘仍用完整 symbol。
    skipped_gap: 因"事件日与前一根 bar 间隔过大"而跳过的事件数 —— 本地数据
    存在空洞时(如历史回填中断), 顺延会跨洞把 2015 年的价当成昨收, r 全错;
    与其写错因子不如跳过(45 天窗 ≈ 最长法定假期 + 常规停牌)。
    """
    code = symbol.split(".")[0]
    try:
        payload = xdxr_events(code)
    except TdxXdxrUnavailableError:
        return [], 0
    from datetime import date as _date

    def _d(s: str) -> _date:
        return _date.fromisoformat(s)

    dates = [d for d, _ in bars]
    closes = dict(bars)
    rows: list[dict] = []
    skipped_gap = 0
    for ev in payload["data"]["events"]:
        if ev.get("category") != 1:
            continue
        idx = effective_index(dates, ev["date"])
        if idx is None or idx == 0:
            continue  # 数据窗口外或无前收 —— 不猜
        prev_date = dates[idx - 1]
        if (_d(dates[idx]) - _d(prev_date)).days > 45:
            skipped_gap += 1
            continue
        r = event_ratio(ev, closes[prev_date])
        if r is None:
            continue
        rows.append({"symbol": symbol, "trade_date": dates[idx], "ex_factor": round(r, 10)})
    return rows, skipped_gap


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 A 股 adj_factor/all.parquet")
    ap.add_argument("--limit-symbols", type=int, default=0, help="只处理前 N 只(试跑)")
    ap.add_argument("--rebuild-enriched", action="store_true", help="完成后全量重算 enriched")
    args = ap.parse_args()

    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore())
    data_dir = repo.store.data_dir
    out_path = data_dir / "adj_factor" / "all.parquet"

    closes = load_local_closes(data_dir)
    universe = sorted(closes)
    # 历史深度预检: 复权因子要求"除权日前一根 bar 就是真实昨收", 本地日K有大空洞时
    # 算出来的 r 不可信(脚本会按 45 天窗跳过, 但跳多了说明数据没准备好)。
    depth = sorted(len(v) for v in closes.values())
    median_depth = depth[len(depth) // 2]
    logger.info("每票 bar 数中位数=%d, 最大=%d", median_depth, depth[-1])
    if median_depth < 800:
        logger.warning(
            "历史深度中位数只有 %d 根 —— 像是还没跑 scripts/backfill_daily_history.py。"
            "跨洞事件会被跳过, 覆盖率会低。建议先回填历史再生成因子。", median_depth)
    if args.limit_symbols:
        universe = universe[: args.limit_symbols]
    logger.info("处理 %d 只", len(universe))

    all_rows: list[dict] = []
    no_event = 0
    err = 0
    gap_skipped = 0
    t0 = time.time()
    for i, sym in enumerate(universe, 1):
        try:
            rows, skipped = build_one(sym, closes[sym])
        except Exception as e:  # noqa: BLE001
            err += 1
            if err <= 10:
                logger.warning("%s 事件获取失败: %s", sym, e)
            continue
        gap_skipped += skipped
        if not rows:
            no_event += 1
        all_rows.extend(rows)
        if i % 500 == 0:
            logger.info("进度 %d/%d, 事件行 %d, 无事件 %d, 跨洞跳过 %d, 失败 %d (%.0fs)",
                        i, len(universe), len(all_rows), no_event, gap_skipped, err,
                        time.time() - t0)

    logger.info("完成: %d 只, 事件行 %d, 无事件/从未除权 %d, 跨洞跳过 %d, 失败 %d, 耗时 %.0fs",
                len(universe), len(all_rows), no_event, gap_skipped, err, time.time() - t0)
    if err > len(universe) * 0.2:
        logger.error("失败率过高(%d/%d), 疑似 TDX 不可达, 放弃写盘(fail-closed)", err, len(universe))
        return 1
    if not all_rows:
        logger.error("0 行事件, 不写空文件")
        return 1

    df = pl.DataFrame(all_rows).sort(["symbol", "trade_date"]).unique(["symbol", "trade_date"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_path)
    logger.info("已写 %s (%d 行, %d 只票有除权事件)",
                out_path, df.height, df["symbol"].n_unique())

    # 刷新 DuckDB 视图, 让本次管道/查询立刻可见
    d = data_dir.as_posix()
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW adj_factor AS "
            f"SELECT * FROM read_parquet('{d}/adj_factor/**/*.parquet', union_by_name=true)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("刷新视图失败(不影响文件): %s", e)

    if args.rebuild_enriched:
        logger.info("全量重算 enriched (前复权生效, 可能很久)…")
        from app.indicators.pipeline import run_pipeline

        n = run_pipeline()
        logger.info("enriched 重算完成: %d 行", n)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
