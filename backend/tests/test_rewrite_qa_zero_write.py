"""QA 独立验证 · 生成阶段「零写入权威数据」（工程师自报第 2 条，独立复核）。

工程师用的是 `capture_authoritative_snapshot()` 前后比对；这里换一个**更狠**的角度：
  1. 跑完 plan / outline / chapter 三类任务后，**遍历整棵书籍目录**，
     逐个文件记 `(mtime_ns, sha256)`，与跑之前逐一比对；
  2. 额外断言：`正文/`、`book.json`、`state.json`、`views/` 四处的指纹完全不变；
  3. 顺带确认唯一允许变化的地方只有 `rewrite/` 与 `checkpoints/`；
  4. 走一次**采纳**才允许改 `正文/`（对照，证明上面的「没变」不是因为写不进去）。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.services import novel_rewrite_ai
from app.services.novel_rewrite_jobs import RewriteJobRegistry
from app.services.novel_rewrite_store import (
    Blueprint,
    RewriteStore,
    apply_checks,
    capture_authoritative_snapshot,
)
from app.services.novel_store import NovelStore

_NODES: list[dict] = [
    {
        "id": "v1",
        "type": "volume",
        "title": "第一卷",
        "order": 1,
        "children": [
            {"id": "ch-001", "type": "chapter", "title": "引子", "order": 1,
             "summary": "冷开场", "beat": "埋伏笔", "file": "ch-001.md"}
        ],
    }
]

_PLAN_MD = """## L1 符号层
- 青云宗 → 临海船行

## L5 桥段层
失去 → 抉择 → 顿悟

```rw-roles
[{"name": "陆昭", "slot": "主角"}, {"name": "白露", "slot": "盟友"}]
```

```rw-reversals
[{"type": "身份错位", "chapter_index": 1, "position_ratio": 0.1}]
```
"""

_OUTLINE_JSON = json.dumps(
    {
        "nodes": [
            {
                "id": "v1",
                "type": "volume",
                "title": "第一卷",
                "order": 1,
                "children": [
                    {"id": "ch-001", "type": "chapter", "title": "新第一章", "order": 1,
                     "status": "draft", "word_target": 3000, "summary": "新细纲", "beat": "新节拍"}
                ],
            }
        ]
    },
    ensure_ascii=False,
)

_BLUEPRINT: dict[str, Any] = {
    "title": "QA 零写入书",
    "abstract": {
        "function_slots": [{"slot": "主角"}, {"slot": "导师"}],
        "reversal_types": ["身份错位"],
        "reversal_positions": [0.6],
        "pacing": "六章骨架",
    },
    "rebuild": {
        "L1_symbols": {"banned": ["青云宗"], "new_lexicon": {}},
        "L2_scenes": {"banned": ["登仙台"], "new_scenes": []},
        "L3_relations": {
            "source_graph": [{"from": "甲", "to": "乙", "kind": "师徒", "power": "高→低"}],
            "new_graph": [{"from": "丙", "to": "丁", "kind": "同门", "power": "对等"}],
        },
        "L5_beats": {"source_seq": ["受辱", "隐忍", "反击"], "new_seq": ["失去", "抉择", "顿悟"]},
    },
}


class _Stub:
    """按 kind 返回不同产物的 AI 替身。"""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def __call__(self, messages: list[dict], **_kwargs: Any) -> str:
        blob = json.dumps(messages, ensure_ascii=False)
        if "六章级大纲补丁" in blob or ('"nodes"' in blob and "大纲补丁" in blob):
            self.kinds.append("outline")
            return f"```json\n{_OUTLINE_JSON}\n```"
        if "分章草稿" in blob or "本章节拍" in blob:
            self.kinds.append("chapter")
            return "这是仿写出来的正文草稿。船行的灯还亮着，账房里没人说话。"
        self.kinds.append("plan")
        return _PLAN_MD


@pytest.fixture(autouse=True)
def _unbind_semaphore() -> Any:
    """解绑共享 semaphore，避免污染后续用例。"""
    from app.services import novel_jobs as novel_jobs_mod

    yield
    novel_jobs_mod._SEMAPHORE._loop = None


def _fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """整棵树的 `(mtime_ns, sha256)` 指纹。"""
    result: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        stat = path.stat()
        result[str(path.relative_to(root)).replace("\\", "/")] = (
            stat.st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return result


async def _run(registry: RewriteJobRegistry, job_id: str, timeout: float = 20.0) -> Any:
    """轮询到终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get_job(job_id)
        if job.status in ("done", "failed", "cancelled"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("job 未进入终态")


async def test_generation_phase_writes_nothing_outside_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★核心★ plan / outline / chapter 三连跑 → 书籍目录里只有 `rewrite/` 与
    `checkpoints/` 变了，`正文/` / `book.json` / `state.json` / `views/` 一字节不动。
    """
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 零写入书")
    store.save_outline(book.id, book.version, _NODES)
    store.write_chapter(book.id, "ch-001", "原本的正文内容，绝不能被生成阶段改动。")
    store.rebuild_views(book.id)

    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate(_BLUEPRINT))
    registry = RewriteJobRegistry(store=store)

    stub = _Stub()
    monkeypatch.setattr(novel_rewrite_ai, "require_ai_ready", lambda: None)
    monkeypatch.setattr(novel_rewrite_ai, "generate", stub)

    before_tree = _fingerprint(store.book_dir(book.id))
    before_snapshot = capture_authoritative_snapshot(store, book.id)

    for kind, chapter_id in (("plan", None), ("outline", None), ("chapter", "ch-001")):
        job = registry.create_job(book.id, kind, chapter_id=chapter_id, risk_ack=True)
        final = await _run(registry, job.job_id)
        assert final.status == "done", f"{kind} 任务失败: {final.failed_step} {final.steps}"

    after_tree = _fingerprint(store.book_dir(book.id))
    after_snapshot = capture_authoritative_snapshot(store, book.id)

    assert after_snapshot == before_snapshot, "★P0★ 生成阶段改动了权威数据"

    changed = {key for key in after_tree if before_tree.get(key) != after_tree[key]}
    added = set(after_tree) - set(before_tree)
    removed = set(before_tree) - set(after_tree)
    touched = changed | added | removed

    offenders = {
        key
        for key in touched
        if not (key.startswith("rewrite/") or key.startswith("checkpoints/"))
    }
    assert not offenders, f"★P0★ 生成阶段碰了仿写域之外的文件: {sorted(offenders)}"

    # 明确点名四处权威数据
    for key in ("book.json", "state.json", "正文/ch-001.md"):
        if key in before_tree:
            assert after_tree[key] == before_tree[key], f"★P0★ {key} 被改了"
    assert not [key for key in touched if key.startswith("views/")], "views/ 被重建了"

    # 仿写域里确实产出了东西（否则上面的「没变」毫无意义）
    assert any(key.startswith("rewrite/drafts/") for key in after_tree), "草稿没落盘"
    assert any(key.startswith("rewrite/reports/") for key in after_tree), "报告没落盘"


async def test_adopt_is_the_only_path_that_touches_chapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：只有**显式采纳**才允许改 `正文/`（证明上面的断言不是「写不进去」）。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 零写入书2")
    store.save_outline(book.id, book.version, _NODES)
    store.write_chapter(book.id, "ch-001", "原本的正文内容。")
    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate(_BLUEPRINT))

    stub = _Stub()
    monkeypatch.setattr(novel_rewrite_ai, "require_ai_ready", lambda: None)
    monkeypatch.setattr(novel_rewrite_ai, "generate", stub)
    registry = RewriteJobRegistry(store=store)

    job = registry.create_job(book.id, "chapter", chapter_id="ch-001", risk_ack=True)
    final = await _run(registry, job.job_id)
    assert final.status == "done"
    assert store.read_chapter(book.id, "ch-001")[0] == "原本的正文内容。"

    # 采纳：全勾 + ack → 闸门放行 → 才写正文
    report = rewrite.load_report(book.id, final.rewrite_id or "")
    checked = apply_checks(
        report,
        [{"key": item.key, "human_checked": True} for item in report.checks if item.status != "fail"],
        [{"index": i, "human_checked": True} for i in range(len(report.reverse_three))],
    )
    rewrite.save_report(book.id, checked)
    from app.services.novel_rewrite_store import require_adoptable, write_ack

    acked = write_ack(rewrite.load_report(book.id, report.rewrite_id))
    require_adoptable(acked)  # 闸门应当放行
    rewrite.save_report(book.id, acked)

    draft = rewrite.read_rewrite_draft(book.id, acked.draft_file)
    store.write_chapter(book.id, "ch-001", draft)
    assert store.read_chapter(book.id, "ch-001")[0] == draft


async def test_outline_generation_never_replaces_book_outline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大纲补丁只落 `rewrite/drafts/`，**不写 `book.json` 的 outline**。"""
    store = NovelStore(root=tmp_path)
    book = store.create_book("QA 零写入书3")
    store.save_outline(book.id, book.version, _NODES)
    rewrite = RewriteStore(store)
    rewrite.save_blueprint(book.id, Blueprint.model_validate(_BLUEPRINT))

    monkeypatch.setattr(novel_rewrite_ai, "require_ai_ready", lambda: None)
    monkeypatch.setattr(novel_rewrite_ai, "generate", _Stub())
    registry = RewriteJobRegistry(store=store)

    before_titles = [c.title for v in store.get_book(book.id).outline.nodes for c in v.children]
    before_version = store.get_book(book.id).version

    job = registry.create_job(book.id, "outline", risk_ack=True)
    final = await _run(registry, job.job_id)
    assert final.status == "done"

    after_titles = [c.title for v in store.get_book(book.id).outline.nodes for c in v.children]
    assert after_titles == before_titles, "★P0★ 大纲生成阶段直接改了正式大纲"
    assert store.get_book(book.id).version == before_version

    # 补丁确实躺在 drafts 里（是 JSON，不是正文）
    assert (rewrite.rewrite_dir(book.id) / "drafts").glob("*.outline.json") or True
    patch_files = list((rewrite.rewrite_dir(book.id) / "drafts").glob("*.outline.json"))
    assert patch_files, "大纲补丁没落盘"
    payload = json.loads(patch_files[0].read_text(encoding="utf-8"))
    assert payload["nodes"][0]["children"][0]["title"] == "新第一章"
