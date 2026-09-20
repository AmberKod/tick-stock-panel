#!/usr/bin/env python3
"""从 us_universe.csv 严格同步生成 us_instruments.parquet。

复用 ``sync_us_instruments`` 的严格链路 (use_akshare=False, allow_demo=False)：
读 ``data/instruments/us_universe.csv`` → normalize → 唯一化 → 写
``data/instruments/us_instruments.parquet``。要求结果 >=500 只，否则拒绝覆盖。

用法:
    python backend/scripts/sync_us_instruments.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.hk_data_adapter import sync_us_instruments


def main() -> int:
    data_dir = REPO_ROOT / "data"
    n = sync_us_instruments(data_dir, use_akshare=False, allow_demo=False)
    print(f"美股 instruments 同步完成: {n} 只 → {data_dir / 'instruments' / 'us_instruments.parquet'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
