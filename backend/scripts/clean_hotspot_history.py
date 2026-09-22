#!/usr/bin/env python
"""一次性存量清洗：热点 history jsonl 去重。

背景 (2026-09-22 实测): `append_history_row` 此前无条件 append, 源返回旧快照
时 topic_date 不变、再次落盘 ⇒ 同一 (market, topic, topic_date) 反复写入。
写入侧已改为幂等 (见 storage.py), 本脚本负责清理既有存量: 每组保留
generated_at 最新一行 (同 generated_at 取文件顺序最后一行 —— 文件顺序即
写入顺序)。坏行 (json 解析失败) 原样保留, 不静默丢。

market=None 的存量老行 (2026-09-20 港美上线前写入) 是独立键, 不与任何有
market 的行合并 —— None ≠ cn/hk/us, 与读取侧 load_history_jsonl 的不伪造
口径一致。

用法:
    python clean_hotspot_history.py --data-dir <path> [--dry-run] [--yes]

幂等可重放: 纯函数式转换 (读原文件 → 写 tmp → os.replace 原子替换)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# topics: (market, topic, topic_date); constituents: 无 topic_date 字段,
# 日期键退用 generated_at 前 10 位 (天级)。
SPEC: dict[str, dict[str, Any]] = {
    "topics.jsonl": {
        "key_fields": ("market", "topic", "topic_date"),
        "day_field": None,
    },
    "constituents.jsonl": {
        "key_fields": ("market", "topic", "code", "generated_at"),
        "day_field": "generated_at",
    },
}


def _row_key(row: dict[str, Any], spec: dict[str, Any]) -> tuple[Any, ...]:
    """按规格取业务键; day_field 字段值截前 10 位作天级键。"""
    values: list[Any] = []
    for field in spec["key_fields"]:
        raw = row.get(field)
        if spec["day_field"] is not None and field == spec["day_field"]:
            values.append(str(raw)[:10] if raw is not None else None)
        else:
            values.append(raw)
    return tuple(values)


def clean_file(path: Path, *, dry_run: bool) -> dict[str, Any]:
    """清洗单个 jsonl; 返回 before/after 报告 (行数/组数/保留的 None-market 行)。"""
    spec = SPEC[path.name]
    if not path.exists():
        return {"file": path.name, "exists": False}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    good: list[tuple[tuple[Any, ...], str, str, int]] = []  # (key, generated_at, raw, idx)
    bad_lines: list[str] = []
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad_lines.append(raw)
            continue
        if not isinstance(row, dict):
            bad_lines.append(raw)
            continue
        good.append((_row_key(row, spec), str(row.get("generated_at") or ""), raw, idx))

    # 每组保留 generated_at 最新一行; 并列时取 idx 最大者 (文件顺序 = 写入顺序)
    best: dict[tuple[Any, ...], tuple[str, str, int]] = {}
    for key, gen_at, raw, idx in good:
        cur = best.get(key)
        if cur is None or (gen_at, idx) >= (cur[0], cur[2]):
            best[key] = (gen_at, raw, idx)

    kept_by_idx = sorted((idx, raw) for _, raw, idx in best.values())
    kept_raw_lines = [raw for _, raw in kept_by_idx]
    none_market_kept = sum(
        1 for key in best if key[0] is None
    )

    report = {
        "file": path.name,
        "exists": True,
        "before_lines": len(good) + len(bad_lines),
        "after_lines": len(kept_raw_lines) + len(bad_lines),
        "unique_groups": len(best),
        "removed": len(good) - len(kept_raw_lines),
        "bad_lines_kept": len(bad_lines),
        "none_market_groups_kept": none_market_kept,
    }

    if not dry_run:
        out_lines = kept_raw_lines + bad_lines
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
        os.replace(tmp, path)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="热点 history jsonl 存量去重清洗")
    parser.add_argument("--data-dir", required=True, help="data 目录 (含 hotspot/history/)")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计, 不写文件")
    parser.add_argument("--yes", action="store_true", help="确认执行 (无 --dry-run 时必需)")
    args = parser.parse_args(argv)

    history_dir = Path(args.data_dir) / "hotspot" / "history"
    if not history_dir.exists():
        print(f"[clean_hotspot_history] 目录不存在: {history_dir}", file=sys.stderr)
        return 2

    if not args.dry_run and not args.yes:
        print("[clean_hotspot_history] 实跑需要 --yes (或先 --dry-run 核对)", file=sys.stderr)
        return 2

    mode = "DRY-RUN" if args.dry_run else "APPLY"
    print(f"[clean_hotspot_history] mode={mode} dir={history_dir}")
    for filename in SPEC:
        report = clean_file(history_dir / filename, dry_run=args.dry_run)
        if not report.get("exists"):
            print(f"  - {report['file']}: 不存在, 跳过")
            continue
        print(
            f"  - {report['file']}: "
            f"{report['before_lines']} -> {report['after_lines']} 行 "
            f"(去重 {report['removed']} 行, 唯一组 {report['unique_groups']}, "
            f"坏行保留 {report['bad_lines_kept']}, "
            f"market=None 组保留 {report['none_market_groups_kept']})"
        )
    print(f"[clean_hotspot_history] {'未写文件 (dry-run)' if args.dry_run else '完成'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
