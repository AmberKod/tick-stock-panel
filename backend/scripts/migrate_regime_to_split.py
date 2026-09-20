"""一次性把老单文件 regime 历史切到分市场目录(commit ① 收尾)。

作用:
- 老 regime 历史总在 data/regime_history/part.parquet(单文件)。
- 新切分路径是 data/regime_history/{cn,hk,us}/part.parquet。
- regime_builder.load_regime_history 在 cn 找不到新切分时会回退读老单文件,
  本脚本是给"想立刻清理 legacy 老文件"的运维场景准备的: 执行一次, 把 legacy 文件
  按 market='cn' 复制到 cn/part.parquet, 然后把 legacy 重命名为 .legacy.bak。

运行时:
    uv run python -m backend.scripts.migrate_regime_to_split
    # 或
    PYTHONPATH=. python backend/scripts/migrate_regime_to_split.py

幂等: 重跑时如果 cn/part.parquet 已存在(老并发), 跳过; legacy 已备份, 不动。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import polars as pl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate_regime_to_split")


def _data_dir_default() -> Path:
    """默认 data_dir: backend/data(开发)/ data(项目根), 按 settings 兼容解析。"""
    # 优先 env DATA_DIR
    import os
    env = os.environ.get("TICKFLOW_DATA_DIR") or os.environ.get("DATA_DIR")
    if env:
        return Path(env)
    # 退化: 项目根下的 data/
    here = Path(__file__).resolve()
    project_root = here.parents[2]
    return project_root / "data"


def migrate(data_dir: Path) -> dict:
    """把 legacy 老单文件迁移到 cn/part.parquet(若有), 返回处理摘要。"""
    legacy = data_dir / "regime_history" / "part.parquet"
    target_cn = data_dir / "regime_history" / "cn" / "part.parquet"
    target_cn.parent.mkdir(parents=True, exist_ok=True)

    result = {
        "legacy_exists": legacy.exists(),
        "target_cn_exists": target_cn.exists(),
        "rows_migrated": 0,
        "backup": None,
    }

    if not legacy.exists():
        logger.info("legacy file not found (%s), nothing to migrate", legacy)
        return result
    if target_cn.exists():
        logger.info("cn/part.parquet already exists, skipping (avoid overwrite)")
        return result

    try:
        df = pl.read_parquet(legacy)
    except Exception as e:
        logger.warning("read legacy parquet failed (%s): %s", legacy, e)
        return result

    result["rows_migrated"] = df.height
    target_cn.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(target_cn)
    logger.info("migrated %d rows -> %s", df.height, target_cn)

    # 重命名 legacy 为 .legacy.bak(幂等: 文件存在就不动)
    backup = legacy.with_suffix(legacy.suffix + ".legacy.bak")
    if not backup.exists():
        legacy.rename(backup)
        result["backup"] = str(backup)
        logger.info("legacy renamed to %s", backup)
    else:
        logger.info("legacy backup already exists (%s), leaving as-is", backup)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="数据目录(默认从 env DATA_DIR 或项目根 data/ 取)",
    )
    args = parser.parse_args()
    data_dir = args.data_dir or _data_dir_default()
    logger.info("data_dir = %s", data_dir)
    summary = migrate(data_dir)
    print(f"summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
