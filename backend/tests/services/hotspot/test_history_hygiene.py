"""热点 history jsonl 数据卫生 — 写入幂等 / 老行保护 / 存量清洗脚本测试。

验收二维 (项目惯例):
- 维度 A (断言形式): 行数计数 + 按业务键重读断言 (每个键只出现一次),
  并显式覆盖 market=None 老行独立键语义。
- 维度 B (执行落点): 幂等用例直接调真实的 ``append_history_row`` /
  ``write_constituents_history``, 清洗用例跑真实 CLI 入口 ``main``。
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from app.services.hotspot.models import HotspotStock, HotspotSummary
from app.services.hotspot.storage import (
    append_history_row,
    history_dir,
    load_history_jsonl,
    write_constituents_history,
)

_BACKEND = Path(__file__).resolve().parents[3]
_SCRIPT = _BACKEND / "scripts" / "clean_hotspot_history.py"


def _summary(topic: str, *, topic_date: str = "2026-09-22") -> HotspotSummary:
    return HotspotSummary(topic=topic, name=topic, heat_score=80.0, topic_date=topic_date)


def _stock(code: str) -> HotspotStock:
    return HotspotStock(code=code, name=code, hot_stock_score=75.0)


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# 写入侧幂等 — 真实调用点 append_history_row
# ---------------------------------------------------------------------------


def test_append_history_row_is_idempotent(tmp_path):
    """同一批 items 调两次, 行数只增一次的量; 每个业务键只出现一次。"""
    items = [_summary("固态电池"), _summary("算力租赁")]
    append_history_row(tmp_path, items, market="cn", generated_at="2026-09-22T01:00:00+00:00")
    once = history_dir(tmp_path) / "topics.jsonl"
    assert len(_read_lines(once)) == 2

    # 第二次: 同批 (含跨天 generated_at — 模拟"源返回旧快照再落盘") 不得再写
    append_history_row(tmp_path, items, market="cn", generated_at="2026-09-23T01:00:00+00:00")
    rows = _read_lines(once)
    assert len(rows) == 2, f"幂等失败: 重复落盘产生了新行 ({len(rows)} 行)"

    keys = [(r["market"], r["topic"], r["topic_date"]) for r in rows]
    assert all(c == 1 for c in Counter(keys).values()), f"业务键出现重复: {keys}"

    # 不同 topic_date 是新组合, 必须正常写入 (幂等不误伤)
    append_history_row(
        tmp_path, [_summary("固态电池", topic_date="2026-09-24")],
        market="cn", generated_at="2026-09-24T01:00:00+00:00",
    )
    assert len(_read_lines(once)) == 3


def test_append_history_row_none_market_legacy_rows_not_swallowed(tmp_path):
    """market=None 的存量老行是独立键: append cn 同 topic 同日期不得吃掉老行。"""
    legacy = history_dir(tmp_path) / "topics.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    old_row = {
        "generated_at": "2026-09-18T01:00:00+00:00",
        "topic": "固态电池",
        "topic_date": "2026-09-18",
        "heat_score": 70.0,
        # 无 market 字段 — 2026-09-20 港美上线前的老行形态
    }
    legacy.write_text(json.dumps(old_row, ensure_ascii=False) + "\n", encoding="utf-8")

    append_history_row(
        tmp_path, [_summary("固态电池", topic_date="2026-09-22")],
        market="cn", generated_at="2026-09-22T01:00:00+00:00",
    )

    rows = _read_lines(legacy)
    assert len(rows) == 2, f"老行被吞或新行被漏: {len(rows)} 行"
    assert any("market" not in r for r in rows), "老行 (无 market) 必须原样保留"
    # 老行不参与任何 market 口径 (读取侧语义回归)
    cn_rows = list(load_history_jsonl(tmp_path, market="cn"))
    assert len(cn_rows) == 1, "老行不得被计入 cn 口径"


def test_write_constituents_history_is_idempotent(tmp_path):
    """constituents: 同一 (market, topic, code, 天) 只落一行; 新的一天正常写。"""
    write_constituents_history(
        tmp_path, "固态电池", [_stock("300001.SZ"), _stock("600000.SH")],
        market="cn", generated_at="2026-09-22T01:30:00+00:00",
    )
    target = history_dir(tmp_path) / "constituents.jsonl"
    assert len(_read_lines(target)) == 2

    # 同一天重复落盘 (generated_at 只差分钟) — 不得产生新行
    write_constituents_history(
        tmp_path, "固态电池", [_stock("300001.SZ"), _stock("600000.SH")],
        market="cn", generated_at="2026-09-22T08:30:00+00:00",
    )
    assert len(_read_lines(target)) == 2, "同日重复落盘产生了新行"

    # 次日是新的天级键, 必须正常写入
    write_constituents_history(
        tmp_path, "固态电池", [_stock("300001.SZ")],
        market="cn", generated_at="2026-09-23T01:30:00+00:00",
    )
    assert len(_read_lines(target)) == 3


def test_append_history_row_missing_file_still_writes(tmp_path):
    """文件不存在 → 查重退化, 直接写 (保持既有行为)。"""
    append_history_row(
        tmp_path, [_summary("新话题")], market="hk",
        generated_at="2026-09-22T02:00:00+00:00",
    )
    rows = _read_lines(history_dir(tmp_path) / "topics.jsonl")
    assert len(rows) == 1
    assert rows[0]["market"] == "hk"


# ---------------------------------------------------------------------------
# 清洗脚本 — 真实 CLI 入口
# ---------------------------------------------------------------------------


def _make_fixture(history: Path) -> None:
    """迷你数据: topics 3 组 (2 组重复) + 1 老行 + 1 坏行; constituents 2 组 1 重复。"""
    rows = [
        {"generated_at": "2026-09-20T01:00:00+00:00", "market": "cn", "topic": "AI", "topic_date": "2026-09-19", "v": 1},
        {"generated_at": "2026-09-21T01:00:00+00:00", "market": "cn", "topic": "AI", "topic_date": "2026-09-19", "v": 2},  # 重复, 更新
        {"generated_at": "2026-09-21T01:00:00+00:00", "market": "us", "topic": "半導体", "topic_date": "2026-09-19", "v": 3},
        {"generated_at": "2026-09-18T01:00:00+00:00", "topic": "老行无市场", "topic_date": "2026-09-18"},  # 老行 (无 market)
        {"generated_at": "2026-09-22T01:00:00+00:00", "market": "hk", "topic": "金融", "topic_date": "2026-09-22", "v": 5},
    ]
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    lines.append("{broken json !!")  # 坏行
    (history / "topics.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    crows = [
        {"generated_at": "2026-09-22T01:00:00+00:00", "market": "cn", "topic": "AI", "code": "300001.SZ", "v": 1},
        {"generated_at": "2026-09-22T09:00:00+00:00", "market": "cn", "topic": "AI", "code": "300001.SZ", "v": 2},  # 同日重复, 更新
        {"generated_at": "2026-09-23T01:00:00+00:00", "market": "cn", "topic": "AI", "code": "300001.SZ", "v": 3},  # 新的一天
    ]
    (history / "constituents.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in crows) + "\n", encoding="utf-8",
    )


def _run_cli(data_dir: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--data-dir", str(data_dir), *flags],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_clean_script_dedupes_and_keeps_legacy_and_bad_lines(tmp_path):
    """清洗: 重复组压 1 (保留最新) + 老行/坏行原样保留。"""
    data_dir = tmp_path / "data"
    history = data_dir / "hotspot" / "history"
    history.mkdir(parents=True)
    _make_fixture(history)

    result = _run_cli(data_dir, "--dry-run", "--yes")
    assert result.returncode == 0, result.stderr

    # dry-run 不得改文件 (原始行数不变, 含坏行)
    raw = (history / "topics.jsonl").read_text(encoding="utf-8")
    assert len([ln for ln in raw.splitlines() if ln.strip()]) == 6

    result = _run_cli(data_dir, "--yes")
    assert result.returncode == 0, result.stderr

    # topics: 6 行 (5 好 + 1 坏) → 4 好行各留 1 (cn-AI 重复压 1) + 1 坏行 = 5 行
    raw_text = (history / "topics.jsonl").read_text(encoding="utf-8")
    raw_lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    good: list[dict] = []
    bad: list[str] = []
    for line in raw_lines:
        try:
            good.append(json.loads(line))
        except json.JSONDecodeError:
            bad.append(line)
    assert len(good) == 4, f"应保留 4 个唯一组, 实际 {len(good)}"
    assert len(bad) == 1, f"坏行必须原样保留, 实际 {bad}"
    # 重复组保留的是 generated_at 最新那行 (v=2)
    ai_rows = [r for r in good if r.get("topic") == "AI" and r.get("market") == "cn"]
    assert len(ai_rows) == 1 and ai_rows[0]["v"] == 2, f"应保留最新行, 实际 {ai_rows}"
    # 老行仍在
    assert any("market" not in r for r in good), "market=None 老行必须保留"

    # constituents: 3 行同 (market,topic,code) 但天级键分两组 (09-22 / 09-23) → 2 行
    ckept = _read_lines(history / "constituents.jsonl")
    assert len(ckept) == 2, f"constituents 应保留 2 行 (同日压 1 + 次日), 实际 {len(ckept)}"
    assert ckept[0]["v"] == 2, "同日重复应保留最新 (v=2)"


def test_clean_script_dry_run_writes_nothing(tmp_path):
    """--dry-run 严格不改文件 (内容逐字节不变)。"""
    data_dir = tmp_path / "data"
    history = data_dir / "hotspot" / "history"
    history.mkdir(parents=True)
    _make_fixture(history)
    before_topics = (history / "topics.jsonl").read_text(encoding="utf-8")
    before_const = (history / "constituents.jsonl").read_text(encoding="utf-8")

    result = _run_cli(data_dir, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout

    assert (history / "topics.jsonl").read_text(encoding="utf-8") == before_topics
    assert (history / "constituents.jsonl").read_text(encoding="utf-8") == before_const
    assert not list(history.glob("*.tmp")), "dry-run 不得留下 tmp 文件"


def test_clean_script_requires_yes_without_dry_run(tmp_path):
    """无 --dry-run 且无 --yes → 拒绝执行 (防误触)。"""
    data_dir = tmp_path / "data"
    (data_dir / "hotspot" / "history").mkdir(parents=True)
    result = _run_cli(data_dir)
    assert result.returncode == 2
    assert "--yes" in result.stderr
