"""QA 独立验证 · F/G 组：诚实不可用 + disclaimer 单点来源。

F：AI 返回垃圾（空串 / 纯空白 / 自然语言 / 缺字段 JSON / 围栏不匹配 / 数组而非对象）
    → ⑦⑧ 必须 `unavailable`，**绝不能 pass**；全仓不得有 mock / 假生成 / 吞异常。
G：`DISCLAIMER_TEXT` 单点定义 —— 用「**改源码里的字面量再整体重载**」的方式验
   「/disclaimer 端点」与「report.disclaimer」是否同步变化（不只跑工程师那条恒等断言）。

许可证：MIT（与宿主项目一致）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from app.services import novel_rewrite_ai
from app.services.novel_rewrite_store import (
    CHECK_ISOMORPHIC_REVERSAL,
    CHECK_KEYS,
    CHECK_ONE_TO_ONE,
    DISCLAIMER_TEXT,
    DISCLAIMER_VERSION,
    AbstractLayer,
    Blueprint,
    FunctionSlot,
    L3Relations,
    L5Beats,
    RebuildLayer,
    RelationEdge,
    build_report,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = BACKEND_ROOT.parent / "frontend" / "src"

# ═══════════════════════ F · 结构化块解析的诚实性 ═══════════════════════

#: 必须解析成 `[]`（**绝不伪造行**）
GARBAGE: list[str] = [
    "",
    "   ",
    "\n\n\t\n",
    "抱歉，我无法完成这个请求。",
    "这是一段纯自然语言，里面没有任何机器可解析的块。",
    "```rw-roles\n```",
    "```rw-roles\nnot json at all\n```",
    "```rw-roles\n{}\n```",                        # 对象而非数组
    "```rw-roles\n[1, 2, 3]\n```",                 # 数组但不是对象元素
    "```rw-roles\n[{\"slot\": \"主角\"}\n```",       # 围栏不匹配
    "rw-roles [{\"name\": \"甲\", \"slot\": \"主角\"}]",  # 根本没有围栏
    "```rw-roles\n[]\n```",                        # 空数组
]

#: 容错解析：模型写成 ```json / ``` 但内容确实是目标结构 → 应当救回来
TOLERANT: list[str] = [
    "```json\n[{\"name\": \"甲\", \"slot\": \"主角\"}]\n```",
    "```\n[{\"name\": \"甲\", \"slot\": \"主角\"}]\n```",
    "```JSON\n[{\"name\": \"甲\", \"slot\": \"主角\"}]\n```",
    "```rw-roles\n说明文字\n[{\"name\": \"甲\", \"slot\": \"主角\"}]\n```",
]

#: 缺 `slot` 的行 —— 当前实现**保留**该行（slot 为空串），见下方 P2 用例
PARTIAL: list[str] = [
    "```rw-roles\n[{\"name\": \"甲\"}]\n```",
    "```rw-roles\n[{\"name\": \"甲\", \"slot\": null}]\n```",
]


@pytest.mark.parametrize("raw", GARBAGE)
def test_character_table_garbage_never_yields_fake_success(raw: str) -> None:
    """`parse_character_table()` 对垃圾输入必须返回 `[]`（**绝不伪造行**）。"""
    assert novel_rewrite_ai.parse_character_table(raw) == []


@pytest.mark.parametrize("raw", TOLERANT)
def test_character_table_tolerates_loose_fences(raw: str) -> None:
    """容错解析器：围栏标签写歪但内容对 → 救回来（降低 ⑦ 恒定 unavailable 的概率）。"""
    roles = novel_rewrite_ai.parse_character_table(raw)
    assert roles == [{"name": "甲", "slot": "主角"}], raw


@pytest.mark.parametrize("raw", PARTIAL)
def test_character_table_drops_slotless_rows(raw: str) -> None:
    """★P2-6 已修★：`slot` 为空的行**丢弃**，与 `parse_reversal_table()` 同口径。

    修之前：[{"name": "甲"}] 会被当成一个「无名功能位」角色，⑦ 的 `new_count`
    被虚增，进而可能误判成「角色数不同 → pass」。
    修之后：空 slot 一律丢弃 → ⑦ 因缺样本走 `unavailable`（诚实不可用，
    绝不冒充通过）。
    """
    roles = novel_rewrite_ai.parse_character_table(raw)
    assert roles == [], raw
    # 后果核对：解析后的角色表里不再有「空 slot」占位，⑦ 拿到的是干净输入。
    # （对照修之前：空 slot 会顶掉一个角色名额，让 ⑦ 误判成「角色数不同 → pass」。）
    from app.services.novel_rewrite_store import check_one_to_one

    _status, metrics = check_one_to_one(
        [FunctionSlot(slot="主角"), FunctionSlot(slot="导师")],
        [{"name": "乙", "slot": "盟友"}, {"name": "丙", "slot": "对手"}],
    )
    assert metrics["new_count"] == 2 and "" not in metrics["new_slots"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "抱歉，我无法完成这个请求。",
        "```rw-reversals\n```",
        "```rw-reversals\n{\"nodes\": []}\n```",
        "```rw-reversals\n[{\"chapter_index\": 1}]\n```",   # 缺 type
        "```rw-reversals\n[1, 2]\n```",
        "```rw-reversals\n[{\"type\": \"身份错位\"}\n```",
        "```rw-reversals\n[]\n```",
    ],
)
def test_reversal_table_garbage_never_yields_fake_success(raw: str) -> None:
    """`parse_reversal_table()` 对垃圾输入必须返回 `[]`。"""
    assert novel_rewrite_ai.parse_reversal_table(raw) == []


def test_valid_blocks_are_parsed() -> None:
    """对照：合法块能正常解析（避免「永远返回 []」式的假诚实）。"""
    md = (
        "## L3\n- some text\n\n"
        "```rw-roles\n[{\"name\": \"甲\", \"slot\": \"主角\"}, {\"name\": \"乙\", \"slot\": \"盟友\"}]\n```\n\n"
        "```rw-reversals\n[{\"type\": \"身份错位\", \"chapter_index\": 2, \"position_ratio\": 0.3}]\n```\n"
    )
    roles = novel_rewrite_ai.parse_character_table(md)
    reversals = novel_rewrite_ai.parse_reversal_table(md)
    assert roles == [{"name": "甲", "slot": "主角"}, {"name": "乙", "slot": "盟友"}]
    assert reversals == [{"type": "身份错位", "chapter_index": 2, "position_ratio": 0.3}]


@pytest.mark.parametrize("raw", GARBAGE)
def test_garbage_ai_output_makes_check_78_unavailable(raw: str) -> None:
    """★硬断言★ AI 吐垃圾 → ⑦⑧ 恒 `unavailable`（**绝不能 pass**）。"""
    blueprint = Blueprint(
        abstract=AbstractLayer(
            function_slots=[FunctionSlot(slot="主角"), FunctionSlot(slot="导师")],
            reversal_types=["身份错位"],
            reversal_positions=[0.6],
        ),
        rebuild=RebuildLayer(
            L3_relations=L3Relations(
                source_graph=[RelationEdge(source="甲", target="乙", kind="师徒", power="高→低")],
                new_graph=[RelationEdge(source="丙", target="丁", kind="同门", power="对等")],
            ),
            L5_beats=L5Beats(source_seq=["受辱"], new_seq=["失去"]),
        ),
    )
    report = build_report(
        rewrite_id="rw-bk-qa-20260101010101-abcd",
        blueprint=blueprint,
        kind="chapter",
        draft_text="正文。",
        character_table=novel_rewrite_ai.parse_character_table(raw),
        reversal_table=novel_rewrite_ai.parse_reversal_table(raw),
    )
    by_key = {item.key: item for item in report.checks}
    assert by_key[CHECK_ONE_TO_ONE].status == "unavailable", raw
    assert by_key[CHECK_ISOMORPHIC_REVERSAL].status == "unavailable", raw
    assert report.summary.adoptable is False
    # unavailable 必须带人工指引
    assert by_key[CHECK_ONE_TO_ONE].human_tip and by_key[CHECK_ISOMORPHIC_REVERSAL].human_tip


def test_every_report_has_exactly_eight_checks() -> None:
    """八项质检一项都不能少（少一项等于少一道闸门）。"""
    report = build_report(
        rewrite_id="rw-bk-qa-20260101010101-abcd",
        blueprint=Blueprint(),
        kind="plan",
        draft_text="x",
    )
    assert [item.key for item in report.checks] == list(CHECK_KEYS)


def test_ai_gate_is_fail_closed() -> None:
    """`require_ai_ready()` 不可用 → 抛 `AiCallError`（503），不返回「可用」。"""
    ok, code, reason = novel_rewrite_ai.ai_ready()
    assert isinstance(ok, bool) and isinstance(code, str) and isinstance(reason, str)
    if not ok:
        with pytest.raises(novel_rewrite_ai.AiCallError):
            novel_rewrite_ai.require_ai_ready()


async def test_generate_rejects_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI 返回空串 / 纯空白 → 视为失败（**不落盘空草稿**）。"""
    calls: list[float] = []

    async def _empty(messages, *, temperature=None, max_tokens=None, timeout=None):
        calls.append(temperature)
        return "   \n\t  "

    monkeypatch.setattr(novel_rewrite_ai, "ai_ready", lambda: (True, "", ""))
    monkeypatch.setattr(novel_rewrite_ai, "generate_ai_text", _empty)
    with pytest.raises(novel_rewrite_ai.AiCallError):
        await novel_rewrite_ai.generate([{"role": "user", "content": "x"}])


async def test_generate_temperature_is_layered(monkeypatch: pytest.MonkeyPatch) -> None:
    """温度分层：结构化 0.5 / 创意 0.8（偏离架构 §3.5，已裁定）。"""
    seen: list[float] = []

    async def _spy(messages, *, temperature=None, max_tokens=None, timeout=None):
        seen.append(temperature)
        return "正文"

    monkeypatch.setattr(novel_rewrite_ai, "ai_ready", lambda: (True, "", ""))
    monkeypatch.setattr(novel_rewrite_ai, "generate_ai_text", _spy)

    await novel_rewrite_ai.generate([], temperature=novel_rewrite_ai.PLAN_TEMPERATURE)
    await novel_rewrite_ai.generate([], temperature=novel_rewrite_ai.OUTLINE_TEMPERATURE)
    await novel_rewrite_ai.generate([], temperature=novel_rewrite_ai.CHAPTER_TEMPERATURE)
    assert seen == [0.5, 0.5, 0.8]
    assert novel_rewrite_ai.STRUCTURED_TEMPERATURE == 0.5
    assert novel_rewrite_ai.CREATIVE_TEMPERATURE == 0.8


def test_outline_patch_rejects_garbage() -> None:
    """大纲补丁解析失败必须**抛错**（不落盘半份补丁）。"""
    for raw in ("", "   ", "抱歉我无法完成", "```json\n{}\n```", "[]", "[1,2]"):
        with pytest.raises(ValueError):
            novel_rewrite_ai.parse_outline_patch(raw)


def test_no_mock_or_fake_generation_in_rewrite_modules() -> None:
    """★白盒★ 仿写三个新模块里不得引入 mock / 随机 / 假数据兜底。

    只看**代码形态**（import 与随机/假数据调用），
    避免把「绝不 mock 生成」这类纪律注释误判成违规。
    """
    forbidden = re.compile(
        r"^\s*(?:from|import)\s+(?:unittest\.mock|mock|faker|Faker)\b"
        r"|random\.(?:choice|random|shuffle|sample)\s*\("
        r"|\b(?:dummy_data|fake_data|mock_data|假数据|假生成|占位数据)\b",
        re.IGNORECASE,
    )
    for name in ("novel_rewrite_store.py", "novel_rewrite_ai.py", "novel_rewrite_jobs.py"):
        text = (BACKEND_ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        offenders = [
            f"{index}:{line.strip()}"
            for index, line in enumerate(text.splitlines(), start=1)
            if forbidden.search(line)
        ]
        assert not offenders, f"{name} 里出现了 mock/fake 痕迹: {offenders}"


def test_loose_block_fallback_never_cross_contaminates_tags() -> None:
    """★P2-7 已修★：容错兜底**不得**跨标签认领。

    修之前：`_extract_block(..., "rw-reversals")` 找不到本标签时退回「任意围栏块」，
    只要内容以 `[` 开头且含 `"type"` 就采用 —— 于是 `rw-roles` 块里写了 `"type"`
    会被当成反转登记表（⑧ 由 unavailable 变成 warn，报告含义失真）。

    修之后两道闸：
      ① 围栏标签明确属于另一个域 → 跳过（不管内容里有什么字段）；
      ② 内容里同时出现两个域的判别字段 → 不认领（无法判定归属）。
    """
    # 明确是 rw-roles 的块，即使内容里有 "type"，也不被反转表认领
    md = '```rw-roles\n[{"type": "身份错位"}]\n```'
    assert novel_rewrite_ai.parse_character_table(md) == []
    assert novel_rewrite_ai.parse_reversal_table(md) == [], "跨标签兜底仍在生效"

    # 反向：明确是 rw-reversals 的块，不被角色表认领
    md2 = '```rw-reversals\n[{"slot": "主角"}]\n```'
    assert novel_rewrite_ai.parse_character_table(md2) == []
    assert novel_rewrite_ai.parse_reversal_table(md2) == []

    # 无标签 / ```json 的块里同时含两域字段 → 谁都不认领（不是瞎猜）
    md3 = '```json\n[{"slot": "主角", "type": "身份错位"}]\n```'
    assert novel_rewrite_ai.parse_character_table(md3) == []
    assert novel_rewrite_ai.parse_reversal_table(md3) == []

    # 容错仍然有效：```json 里只有本域字段 → 照样救回来（不能为了修串味把容错关掉）
    assert novel_rewrite_ai.parse_character_table(
        '```json\n[{"name": "甲", "slot": "主角"}]\n```'
    ) == [{"name": "甲", "slot": "主角"}]
    assert novel_rewrite_ai.parse_reversal_table(
        '```json\n[{"type": "身份错位", "chapter_index": 1}]\n```'
    ) == [{"type": "身份错位", "chapter_index": 1}]


def test_no_bare_except_swallowing_in_rewrite_modules() -> None:
    """不得用「吞异常返回 200」糊过去：`except` 必须要么记日志要么抛。"""
    pattern = re.compile(r"except\s+Exception\s*(?:as\s+\w+)?\s*:\s*(?:pass|return)\s*$")
    for name in ("novel_rewrite_store.py", "novel_rewrite_ai.py", "novel_rewrite_jobs.py"):
        text = (BACKEND_ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        offenders = [
            f"{index}:{line.strip()}"
            for index, line in enumerate(text.splitlines(), start=1)
            if pattern.search(line)
        ]
        assert not offenders, f"{name} 里有吞异常的分支: {offenders}"


# ═══════════════════════ G · disclaimer 单点 ═══════════════════════


def test_disclaimer_text_defined_exactly_once_in_backend() -> None:
    """★单点★ 免责文案字面量在后端源码里**只出现一次**（常量定义处）。"""
    hits: list[str] = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "本报告是规则命中清单" in text:
            hits.append(str(path.relative_to(BACKEND_ROOT)))
    assert hits == ["app\\services\\novel_rewrite_store.py"] or hits == [
        "app/services/novel_rewrite_store.py"
    ], f"免责文案出现在多处: {hits}"


def test_disclaimer_text_not_duplicated_in_frontend() -> None:
    """★单点★ 前端 `src/` 里不得出现免责文案（任何分支都不自备声明）。"""
    if not FRONTEND_SRC.exists():
        pytest.skip("前端源码不在本仓库路径下")
    offenders: list[str] = []
    for path in FRONTEND_SRC.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        if "本报告是规则命中清单" in text or "不是查重报告" in text:
            offenders.append(str(path.relative_to(FRONTEND_SRC)))
    assert not offenders, f"前端自带了一份免责文案: {offenders}"


def test_pre_report_notice_constant_is_gone() -> None:
    """旧的前端预生成提示常量 `PRE_REPORT_NOTICE_TEXT` 必须彻底消失。"""
    offenders: list[str] = []
    for base in (BACKEND_ROOT / "app", FRONTEND_SRC):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}:
                if "PRE_REPORT_NOTICE_TEXT" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path))
    assert not offenders, f"PRE_REPORT_NOTICE_TEXT 仍存在: {offenders}"


def test_disclaimer_identity_across_exits() -> None:
    """两个出口（常量 / 报告）必须**同一个字符串对象**，不是两份抄写。"""
    from app.services.novel_rewrite_store import Disclaimer

    assert Disclaimer().text == DISCLAIMER_TEXT
    assert Disclaimer().version == DISCLAIMER_VERSION
    report = build_report(
        rewrite_id="rw-bk-qa-20260101010101-abcd",
        blueprint=Blueprint(),
        kind="plan",
        draft_text="x",
    )
    assert report.disclaimer.text == DISCLAIMER_TEXT
    assert report.disclaimer.version == DISCLAIMER_VERSION


def _load_module(name: str, source: str, filename: str, package: str) -> types.ModuleType:
    """把源码文本装载成一个（临时的、不污染磁盘的）模块。"""
    module = types.ModuleType(name)
    module.__file__ = filename
    module.__package__ = package
    sys.modules[name] = module
    exec(compile(source, filename, "exec"), module.__dict__)
    return module


def test_disclaimer_mutation_propagates_to_both_exits() -> None:
    """★换角度★ 把**源码里的免责字面量**改掉再整体重载：

    `/disclaimer` 端点 与 `report.disclaimer` 必须**同步**变成新文案 ——
    只要有一处是「另抄了一份」，这条就会红。
    """
    marker = "【QA 突变标记】"
    store_path = BACKEND_ROOT / "app" / "services" / "novel_rewrite_store.py"
    api_path = BACKEND_ROOT / "app" / "api" / "novel_rewrite.py"
    original_source = store_path.read_text(encoding="utf-8")

    assert DISCLAIMER_TEXT.split("。")[0] in original_source
    mutated_source = original_source.replace(
        DISCLAIMER_TEXT.split("。")[0], marker + DISCLAIMER_TEXT.split("。")[0], 1
    )
    assert mutated_source != original_source, "字面量替换没生效（常量写法变了？）"

    saved_modules = {
        key: sys.modules.get(key)
        for key in ("app.services.novel_rewrite_store",)
    }
    new_api_name = "app.api.novel_rewrite_qamut"
    try:
        mutated_store = _load_module(
            "app.services.novel_rewrite_store",
            mutated_source,
            str(store_path),
            "app.services",
        )
        assert marker in mutated_store.DISCLAIMER_TEXT

        spec = importlib.util.spec_from_file_location(new_api_name, api_path)
        assert spec and spec.loader
        mutated_api = importlib.util.module_from_spec(spec)
        sys.modules[new_api_name] = mutated_api
        spec.loader.exec_module(mutated_api)

        # 出口一：`/disclaimer` 端点（无 Depends，可直接调）
        payload = mutated_api.get_disclaimer("bk-qa")
        assert marker in payload["disclaimer"]["text"], (
            "端点没有跟着常量变 —— 端点另起了一份文案"
        )
        assert payload["disclaimer"]["version"] == mutated_store.DISCLAIMER_VERSION

        # 出口二：报告里的 disclaimer
        report = mutated_store.build_report(
            rewrite_id="rw-bk-qa-20260101010101-abcd",
            blueprint=mutated_store.Blueprint(),
            kind="chapter",
            draft_text="x",
        )
        assert marker in report.disclaimer.text, "报告没有跟着常量变"
        assert report.disclaimer.text == payload["disclaimer"]["text"]
    finally:
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        sys.modules.pop(new_api_name, None)

    # 还原后，真实模块仍然导出原常量（确认没被污染）
    from app.services import novel_rewrite_store as real

    assert real.DISCLAIMER_TEXT == DISCLAIMER_TEXT
    assert marker not in real.DISCLAIMER_TEXT


def test_disclaimer_version_is_stable_string() -> None:
    """`DISCLAIMER_VERSION` 是稳定串（前端据此判断声明是否过期）。"""
    assert DISCLAIMER_VERSION.startswith("rw-disclaimer-")
    assert DISCLAIMER_VERSION == "rw-disclaimer-v1"


def test_honesty_note_has_no_promise_words() -> None:
    """预检诚实声明不含任何承诺词。"""
    from app.services.novel_rewrite_store import PRECHECK_HONESTY_NOTE, PRECHECK_SAMPLE

    for word in ("保证", "承诺", "确保", "无风险", "已通过查重", "不会侵权"):
        assert word not in PRECHECK_HONESTY_NOTE
        assert word not in PRECHECK_SAMPLE
    assert PRECHECK_HONESTY_NOTE
