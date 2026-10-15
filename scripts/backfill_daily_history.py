"""按年分段回填 A 股日 K 历史到 kline_daily (容器内执行)。

背景
----
daily_pipeline 首次同步只拉最近 1 年 (start = today - 365d), 导致 kline_daily 最早
只到 2025-09-24。数据源本身没有这个限制 —— 实测 TickFlow klines.batch 传任意
start_time/end_time 都能返回 2015 年数据 (每段一年 ~244 根)。本脚本就是把这个能力
用起来, 把历史补到 2015-01-01。

设计要点
--------
1. **按年分段**: 一年 244 根 × 100 标的/请求 = ~2.4 万行, 实测单次请求能完整返回
   (不会像请求更宽区间那样被服务端行数上限截断丢标的)。
2. **失败可见**: sync_and_persist_daily_batch 的 failed_out 会收集出错的分块标的,
   脚本对同一段重跑 MAX_ROUND 轮, 仍失败的写进 checkpoint 并在结束时打印。
   绝不把"部分成功"当成功。
3. **断点续跑**: 每段完成后写 data/checkpoints/backfill_daily_history.json,
   重跑自动跳过已完成且无失败残留的年份。
4. **等待安全**: 默认先轮询 /api/pipeline/jobs 等后台 pipeline 跑完再动手,
   避免与 market_daily_sync 抢写同一批 date 分区。

用法 (容器内)
-------------
    docker exec TickFlow_Stock_Panel uv run python /app/scripts/backfill_daily_history.py --help
    # 先小样本试跑一年验证正确性 + 计时
    ... --years 2015 --limit-symbols 200
    # 正式回填
    ... --years 2015..2025 --no-wait=false

    # 回填完后重算指标
    ... --rebuild-enriched
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")

CHECKPOINT_NAME = "backfill_daily_history.json"
MAX_ROUND = 3


def _bootstrap() -> None:
    """让 `app` 包可导入 (容器内 cwd=/app, 但也兼容在 backend/ 下跑)。"""
    here = Path(__file__).resolve()
    for cand in (here.parents[1], Path.cwd()):
        if (cand / "app" / "tickflow").is_dir():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return
    raise SystemExit("找不到 app 包根目录, 请在容器内 (/app) 运行本脚本")


_bootstrap()

from app.tickflow.policy import detect_capabilities  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402
from app.services import kline_sync  # noqa: E402


def parse_years(spec: str) -> list[int]:
    """`2015` / `2015..2025` / `2015,2016,2017` → 年份列表(升序去重)。"""
    spec = spec.strip()
    years: set[int] = set()
    if ".." in spec:
        lo, hi = spec.split("..", 1)
        years.update(range(int(lo), int(hi) + 1))
    elif "," in spec:
        years.update(int(p) for p in spec.split(",") if p.strip())
    else:
        years.add(int(spec))
    return sorted(y for y in years if 1990 <= y <= datetime.now().year)


def wait_for_pipeline(base_url: str, timeout_s: int, poll_s: int = 20) -> bool:
    """轮询 pipeline jobs, 直到没有 running 任务。返回 True=可以开工。"""
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/pipeline/jobs", timeout=10) as r:
                data = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            logger.warning("查询 pipeline 状态失败(%s), 继续", e)
            return True
        running = [j for j in data.get("jobs", []) if j.get("status") == "running"]
        if not running:
            logger.info("后台任务空闲, 开始回填")
            return True
        j = running[0]
        logger.info(
            "等待后台任务 %s (%s %s%%) 结束…", j.get("id"), j.get("stage"), j.get("progress")
        )
        time.sleep(poll_s)
    logger.error("等待后台任务超时(%ds), 放弃", timeout_s)
    return False


def load_universe(limit: int | None) -> list[str]:
    """标的池: 复用 extend_history 的解析逻辑(pool + instruments 兜底)。"""
    from app.services.extend_history import _resolve_universe

    capset = detect_capabilities()
    universe = _resolve_universe(capset)
    if limit:
        universe = universe[:limit]
    logger.info("universe = %d 只", len(universe))
    return universe


def checkpoint_path(data_dir: Path) -> Path:
    d = data_dir / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    return d / CHECKPOINT_NAME


def load_cp(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("checkpoint 损坏(%s), 从头开始", e)
    return {"years": {}, "updated_at": None}


def year_done(cp: dict, year: int, universe_size: int) -> bool:
    """该年是否已完成到当前的标的规模。

    小样本试跑(--limit-symbols)只处理了前 N 只, 不能据此认为整年完成; 否则正式
    全量跑会被 checkpoint 拦掉, 悄悄只补了 200 只票还报告成功。
    """
    rec = cp["years"].get(str(year))
    if not rec:
        return False
    if rec.get("failed"):
        return False
    return int(rec.get("universe_size", 0)) >= universe_size


def save_cp(path: Path, cp: dict) -> None:
    cp["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")


def backfill_year(
    repo: KlineRepository, universe: list[str], year: int, already: list[str]
) -> dict:
    """回填单年。already = 上一轮已成功落盘的标的(续跑时跳过)。"""
    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31, 23, 59, 59)
    # 不补未来区间: 数据源对 end > today 也只是返回到今天, 无害但浪费一次往返。
    if end > datetime.now():
        end = datetime.now()

    pending = [s for s in universe if s not in set(already)]
    failed: list[str] = []
    ok: list[str] = list(already)
    rows = 0

    t0 = time.time()
    for rnd in range(1, MAX_ROUND + 1):
        if not pending:
            break
        batch_failed: list[str] = []
        batch_ok: list[str] = []
        written = kline_sync.sync_and_persist_daily_batch(
            pending, repo, detect_capabilities(),
            start_date=start, end_date=end,
            request_timeout_seconds=90.0,
            failed_out=batch_failed,
            successful_out=batch_ok,
        )
        rows += written
        failed = sorted(set(batch_failed))
        ok += batch_ok
        logger.info(
            "  %d 年 第%d轮: 请求 %d 只, 写入 %d 行, 成功 %d, 失败 %d",
            year, rnd, len(pending), written, len(batch_ok), len(failed),
        )
        if not failed:
            break
        pending = failed
        time.sleep(2.0 * rnd)

    # 该年真没有返回数据的标的(未上市/长期停牌/退市)不算失败 —— 数据源对"请求日期
    # 早于上市日"本就返回空。真正失败的是 batch_failed 里那些(请求异常)。
    return {
        "rows": rows,
        "ok": len(set(ok)),
        "failed": failed,
        "seconds": round(time.time() - t0, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="按年分段回填 A 股日 K 历史")
    ap.add_argument("--years", default="2015..2025", help="年份: 2015 | 2015..2025 | 2015,2016")
    ap.add_argument("--limit-symbols", type=int, default=0, help="只取前 N 只标的(试跑用)")
    ap.add_argument("--no-wait", action="store_true", help="不等后台 pipeline")
    ap.add_argument("--wait-timeout", type=int, default=2400, help="等后台任务最长秒数")
    ap.add_argument("--pipeline-url", default="http://127.0.0.1:3018")
    ap.add_argument("--rebuild-enriched", action="store_true", help="回填后全量重算指标")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="不写 checkpoint (小样本试跑用, 避免把部分结果记成整年完成)")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划")
    args = ap.parse_args()

    years = parse_years(args.years)
    if not years:
        logger.error("没有有效年份: %s", args.years)
        return 2

    store = DataStore()
    repo = KlineRepository(store)
    cp_path = checkpoint_path(repo.store.data_dir)
    cp = load_cp(cp_path)

    plan = [(y, cp["years"].get(str(y), {}).get("failed", [])) for y in years]
    logger.info("计划回填年份: %s", [y for y, _ in plan])
    logger.info("checkpoint: %s%s", cp_path, " (本次不写入)" if args.no_checkpoint else "")
    for y, failed in plan:
        rec = cp["years"].get(str(y), {})
        logger.info("  %d: ok=%d universe_size=%s 残留失败=%d",
                    y, rec.get("ok", 0), rec.get("universe_size"), len(failed))

    if args.dry_run:
        return 0

    if not args.no_wait and not wait_for_pipeline(args.pipeline_url, args.wait_timeout):
        return 3

    universe = load_universe(args.limit_symbols or None)

    exit_code = 0
    for year, prior_failed in plan:
        prior_rec = cp["years"].get(str(year), {})
        already = prior_rec.get("symbols", []) or []
        if year_done(cp, year, len(universe)):
            logger.info("[%d] 已完成整段 (universe_size=%d), 跳过", year, prior_rec.get("universe_size"))
            continue
        logger.info("[%d] 开始回填… (待处理 %d 只)", year, len(universe))
        try:
            res = backfill_year(repo, universe, year, already)
        except Exception as e:  # noqa: BLE001
            logger.exception("[%d] 回填异常: %s", year, e)
            res = {"rows": 0, "ok": 0, "failed": list(prior_failed), "seconds": 0}
            exit_code = 1
        res["symbols"] = sorted(set(already + list(universe)))
        res["universe_size"] = len(universe)
        if args.no_checkpoint:
            logger.info("[%d] 完成(不写 checkpoint): rows=%d ok=%d failed=%d (%ss)",
                        year, res["rows"], res["ok"], len(res["failed"]), res["seconds"])
        else:
            cp["years"][str(year)] = res
            save_cp(cp_path, cp)
            logger.info(
            "[%d] 完成: rows=%d ok=%d failed=%d (%ss)",
            year, res["rows"], res["ok"], len(res["failed"]), res["seconds"],
        )
        if res["failed"]:
            exit_code = 1
            logger.warning("[%d] 以下标的请求失败: %s", year, res["failed"][:20])

    if args.rebuild_enriched:
        logger.info("全部回填完成, 开始全量重算 enriched (可能很久)…")
        from app.indicators.pipeline import run_pipeline

        run_pipeline()
        logger.info("enriched 重算完成")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
