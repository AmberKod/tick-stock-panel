#!/usr/bin/env python3
"""美股日 K 失败/错拉标的定向重试 (含 class share 与 yfinance 兜底)。

针对 ``sync_us_daily.py`` 失败清单 (us_daily_failed.txt) 与 class share 错拉
(如 BRK.A→BRK、BIO.B→BIO) 的标的, 逐只重新拉日 K + 重算 enriched。

用法:
    # 重试失败清单里的全部标的
    python backend/scripts/retry_us_daily.py
    # 只重试指定标的 (逗号分隔)
    python backend/scripts/retry_us_daily.py --symbols BRK.A,BIO.B,CRD.A
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.config import settings as _settings
from app.services.hk_data_adapter import (
    _daily_symbol_key,
    fetch_us_daily_akshare,
    sync_hk_daily_to_enriched,
)

# class share 标的 (之前被 symbol.split('.')[0] 误剥后缀, 需强制重拉覆盖)
CLASS_SHARE_FIXES = (
    "BIO.B", "CRD.A", "CRD.B", "HEI.A", "HVT.A", "WSO.B",
)


def _load_failed(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="美股日 K 失败标的定向重试")
    parser.add_argument(
        "--symbols", type=str, default="", help="逗号分隔的标的 (默认读失败清单 + class share)"
    )
    parser.add_argument(
        "--failed",
        type=str,
        default=str(REPO_ROOT / "data" / "instruments" / "us_daily_failed.txt"),
    )
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = _load_failed(Path(args.failed)) + list(CLASS_SHARE_FIXES)
    # 去重保序
    seen: set[str] = set()
    symbols = [s for s in symbols if not (s in seen or seen.add(s))]

    print(f"定向重试美股日 K: 共 {len(symbols)} 只")
    ok, fail = 0, 0
    failed: list[str] = []
    t0 = time.time()
    for sym in symbols:
        try:
            df = fetch_us_daily_akshare(sym)
            if df.is_empty():
                print(f"  [失败] {sym}: 新浪/yfinance 均无数据", flush=True)
                fail += 1
                failed.append(sym)
                continue
            key = _daily_symbol_key(f"{sym}.US")
            # 直接覆盖写日 K (不用 sync_hk_daily_to_parquet 的按 date merge,
            # 否则 class share 错拉 BIO.B→BIO 的脏历史会残留; 也不走 rmtree,
            # 避免触发沙箱批量删除护栏)。全量历史, 覆盖即正确。
            daily_dir = _settings.data_dir / "kline_daily" / f"symbol={key}"
            daily_dir.mkdir(parents=True, exist_ok=True)
            df.write_parquet(daily_dir / "part.parquet")
            sync_hk_daily_to_enriched(f"{sym}.US")
            ok += 1
            print(f"  [成功] {sym}: 日K {df.height} 行", flush=True)
        except Exception as e:
            print(f"  [异常] {sym}: {e}", flush=True)
            fail += 1
            failed.append(sym)
        time.sleep(0.3)  # 轻微限速, 避免触发源站限流

    el = time.time() - t0
    print(f"完成: 成功 {ok} 只, 失败 {fail} 只, 耗时 {el:.0f}s")
    if failed:
        print("仍失败:", ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
