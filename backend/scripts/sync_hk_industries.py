"""港股行业映射同步脚本。

数据源: 东方财富 RPT_HKF10_INFO_ORGPROFILE 接口
URL:    https://datacenter.eastmoney.com/securities/api/data/v1/get
字段:   SECUCODE / BELONG_INDUSTRY (中文, 31 类 ICB/恒生分类)

接口特性:
- pageSize 硬上限 500 (pageSize>500 也只返 500)
- IN 列表 filter 不支持 (ANTLR 报错)
- 单只 filter `(SECUCODE="00700.HK")` 稳定
- 实测 8 并发 9.6 qps

执行:
    backend/.venv/Scripts/python.exe backend/scripts/sync_hk_industries.py
    backend/.venv/Scripts/python.exe backend/scripts/sync_hk_industries.py --dry-run
    backend/.venv/Scripts/python.exe backend/scripts/sync_hk_industries.py --concurrency 16
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import polars as pl
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

INSTRUMENTS_PATH = PROJECT_ROOT / "data" / "instruments" / "hk_instruments.parquet"
PROGRESS_PATH = PROJECT_ROOT / "data" / "instruments" / "hk_industry_sync.progress.json"
LOG_PATH = PROJECT_ROOT / "data" / "instruments" / "hk_industry_sync.log"

EAST_MONEY_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REQUEST_TIMEOUT = 15
DEFAULT_CONCURRENCY = 8

# 批量模式: 实测 pageSize 硬上限 500 (超过也只返 500), 全库约 14 页
BULK_PAGE_SIZE = 500
BULK_MAX_PAGES = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("sync_hk_industries")


# 落盘: sector/industry 字段合并 (其他字段不变)
def fetch_industry(security_code: str, session: requests.Session, retries: int = 3) -> tuple[str, str | None]:
    """返回 (security_code, BELONG_INDUSTRY 或 None)。"""
    secucode = f"{security_code}.HK"
    params = {
        "reportName": "RPT_HKF10_INFO_ORGPROFILE",
        "columns": "SECUCODE,SECURITY_CODE,BELONG_INDUSTRY",
        "filter": f'(SECUCODE="{secucode}")',
        "pageNumber": "1",
        "pageSize": "500",
        "source": "F10",
        "client": "PC",
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(EAST_MONEY_URL, params=params, timeout=REQUEST_TIMEOUT)
            data = r.json()
            if not data.get("success"):
                last_err = data.get("message") or "success=false"
                time.sleep(0.5 * (attempt + 1))
                continue
            rows = data.get("result", {}).get("data", []) if isinstance(data.get("result"), dict) else []
            if rows:
                industry = rows[0].get("BELONG_INDUSTRY")
                return security_code, (industry.strip() if industry else None)
            return security_code, None
        except Exception as e:
            last_err = repr(e)[:120]
            time.sleep(0.5 * (attempt + 1))
    logger.warning("FAIL %s: %s", secucode, last_err)
    return security_code, None


def fetch_all_industries(
    session: requests.Session,
    page_size: int = BULK_PAGE_SIZE,
    retries: int = 3,
) -> dict[str, str]:
    """无 filter 整页拉全市场港股行业 → {5位 code: BELONG_INDUSTRY}。

    实测(2026-09-19): 不带 filter 时 pageSize=500 生效, 全库 6925 条 / 14 页,
    比逐只 filter(2816 次请求且大量返回空)快两个数量级。逐只模式保留作补漏。
    """
    out: dict[str, str] = {}
    page = 1
    while page <= BULK_MAX_PAGES:
        params = {
            "reportName": "RPT_HKF10_INFO_ORGPROFILE",
            "columns": "SECUCODE,SECURITY_CODE,BELONG_INDUSTRY",
            "pageNumber": str(page),
            "pageSize": str(page_size),
            "source": "F10",
            "client": "PC",
        }
        data: dict[str, Any] = {}
        for attempt in range(retries):
            try:
                r = session.get(EAST_MONEY_URL, params=params, timeout=REQUEST_TIMEOUT)
                data = r.json()
                if data.get("success"):
                    break
            except Exception as e:
                logger.warning("bulk page %d 第 %d 次失败: %r", page, attempt + 1, repr(e)[:100])
            time.sleep(0.6 * (attempt + 1))
        if not data.get("success"):
            logger.warning("bulk page %d 拉取失败, 停止翻页 (已收 %d 条)", page, len(out))
            break

        result = data.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            raw = str(row.get("SECURITY_CODE") or row.get("SECUCODE") or "")
            code = raw.replace(".HK", "").strip().zfill(5)
            industry = (row.get("BELONG_INDUSTRY") or "").strip()
            if code and industry:
                out[code] = industry
        logger.info("bulk page %d/%s 收 %d 条 (累计 %d)",
                    page, result.get("pages"), len(rows), len(out))
        try:
            pages = int(result.get("pages") or 0)
        except (TypeError, ValueError):
            pages = 0
        if not rows or page >= pages:
            break
        page += 1
    return out


def load_universe_codes() -> list[str]:
    """从 hk_instruments.parquet 读取 5 位 security_code (去除 .HK 后缀)。"""
    df = pl.read_parquet(INSTRUMENTS_PATH, columns=["symbol", "code"])
    codes: list[str] = []
    for row in df.iter_rows(named=True):
        code = row.get("code")
        sym = row.get("symbol")
        if code:
            codes.append(str(code).zfill(5))
        elif sym and ".HK" in sym:
            codes.append(sym.replace(".HK", "").zfill(5))
    return sorted(set(codes))


def load_progress() -> set[str]:
    if PROGRESS_PATH.exists():
        try:
            with open(PROGRESS_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return set(d.get("done", []))
        except Exception:
            return set()
    return set()


def save_progress(done: set[str]) -> None:
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump({"done": sorted(done), "ts": time.time()}, f, ensure_ascii=False)


def main() -> int:
    p = argparse.ArgumentParser(description="港股行业映射同步 (东方财富 RPT_HKF10_INFO_ORGPROFILE)")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--limit", type=int, default=0, help="仅同步前 N 只 (调试用)")
    p.add_argument("--dry-run", action="store_true", help="不写 parquet, 仅打印统计")
    p.add_argument("--restart", action="store_true", help="忽略 progress, 重新全量拉")
    p.add_argument("--per-symbol", action="store_true",
                   help="逐只 filter 模式(默认批量整页; 逐只仅用于补漏)")
    args = p.parse_args()

    # ── 批量模式: 14 次请求拿全市场行业, 不依赖 progress ──
    if not args.per_symbol:
        t0 = time.time()
        with requests.Session() as session:
            industry_map = fetch_all_industries(session)
        logger.info("批量拉取完成 %d 只, 耗时 %.1fs", len(industry_map), time.time() - t0)
        ind_counter: dict[str, int] = {}
        for v in industry_map.values():
            ind_counter[v] = ind_counter.get(v, 0) + 1
        logger.info("行业分布 (共 %d 类):", len(ind_counter))
        for k in sorted(ind_counter, key=lambda x: -ind_counter[x]):
            logger.info("  %-16s %d", k, ind_counter[k])
        return _write_industries(industry_map, dry_run=args.dry_run)

    codes = load_universe_codes()
    if args.limit > 0:
        codes = codes[: args.limit]
    logger.info("universe 共 %d 只港股", len(codes))

    done = set() if args.restart else load_progress()
    todo = [c for c in codes if c not in done]
    logger.info("已完成 %d, 待拉 %d", len(done), len(todo))

    if not todo:
        logger.info("无待拉项, 退出")
        return 0

    industry_map: dict[str, str | None] = {}
    failed: list[str] = []
    t0 = time.time()
    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(fetch_industry, c, session): c for c in todo}
            completed = 0
            for fut in as_completed(futures):
                code, industry = fut.result()
                industry_map[code] = industry
                if industry is None:
                    failed.append(code)
                completed += 1
                done.add(code)
                if completed % 200 == 0 or completed == len(todo):
                    elapsed = time.time() - t0
                    qps = completed / elapsed if elapsed > 0 else 0
                    logger.info("进度 %d/%d (%.1f%%) %.1f qps, 失败 %d",
                                completed, len(todo), 100 * completed / len(todo),
                                qps, len(failed))
                    save_progress(done)

    save_progress(done)
    elapsed = time.time() - t0
    logger.info("全量拉取完成 %d 只, 耗时 %.1fs (%.1f qps), 失败/无行业 %d",
                len(industry_map), elapsed, len(industry_map) / elapsed if elapsed > 0 else 0, len(failed))

    # 统计行业分布
    ind_counter: dict[str, int] = {}
    for v in industry_map.values():
        if v:
            ind_counter[v] = ind_counter.get(v, 0) + 1
    logger.info("行业分布 (共 %d 类):", len(ind_counter))
    for k in sorted(ind_counter, key=lambda x: -ind_counter[x]):
        logger.info("  %-16s %d", k, ind_counter[k])

    return _write_industries(industry_map, dry_run=args.dry_run)


def _write_industries(industry_map: dict[str, str | None], *, dry_run: bool = False) -> int:
    """把 {5位 code: 行业} 合并进 hk_instruments.parquet 的 sector/industry。"""
    if dry_run:
        logger.info("[dry-run] 不写 parquet, 退出")
        return 0

    # 用 code (5位) 对齐 universe
    def lookup_industry(code: str | None) -> str | None:
        if not code:
            return None
        return industry_map.get(str(code).zfill(5))

    # 落盘: sector/industry 字段合并
    # 关键: lookup_industry 查不到时保留原 sector (避免进度机制/接口失败清掉已有数据)
    df = pl.read_parquet(INSTRUMENTS_PATH)
    before = df["sector"].drop_nulls().shape[0]
    logger.info("原 parquet shape=%s, sector 非空 %d", df.shape, before)

    sector_col: list[str | None] = []
    industry_col: list[str | None] = []
    hit = miss = 0
    for row in df.iter_rows(named=True):
        ind = lookup_industry(row.get("code"))
        if ind:
            sector_col.append(ind)
            industry_col.append(ind)
            hit += 1
        else:
            # 保留原 sector/industry (如果原本有)
            sector_col.append(row.get("sector"))
            industry_col.append(row.get("industry"))
            miss += 1
    df = df.with_columns([
        pl.Series("sector", sector_col, dtype=pl.String),
        pl.Series("industry", industry_col, dtype=pl.String),
    ])
    after = df["sector"].drop_nulls().shape[0]
    logger.info("新 parquet shape=%s, sector 非空 %d (本次命中 %d, 保留 %d)",
                df.shape, after, hit, miss)

    df.write_parquet(INSTRUMENTS_PATH)
    logger.info("已落盘 %s (%d 行), sector 覆盖 %d → %d", INSTRUMENTS_PATH, df.shape[0], before, after)
    return 0


if __name__ == "__main__":
    sys.exit(main())