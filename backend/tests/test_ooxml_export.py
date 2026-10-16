"""零依赖 OOXML 导出 (app/services/ooxml_export.py + app/api/export.py) 测试。

守护四条:
1. 生成的 xlsx/docx 是真 OOXML —— zipfile 能开, 必需部件齐全, XML 能解析。
2. 中文与 ``& < > " '`` 不破坏 XML —— 解析回来文本逐字一致。
3. 数值是数值、空值是空值 —— 数值不套 ``t="str"``, None/NaN/Inf 不写占位符。
4. 上游 unavailable 时 API 仍返回 200, 文档内写明 reason, **不含任何伪造数值**;
   源码里不得出现 ``random.`` / ``_DEMO_`` 这类兜底造数痕迹。

TestClient 坑:必须显式声明本机地址 ``client=("127.0.0.1", 5555)``, 否则被
main.py 的「未设密码 + 非内网」守卫判 403。主应用 lifespan 启动较慢(约 20s),
故 API 用例共用一个 module 级 client。
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.ooxml_export import to_docx, to_xlsx

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

XLSX_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/worksheets/sheet1.xml",
}
DOCX_PARTS = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}

_SOURCES = [
    Path(__file__).resolve().parents[1] / "app" / "services" / "ooxml_export.py",
    Path(__file__).resolve().parents[1] / "app" / "api" / "export.py",
]


# =====================================================================
# 解析辅助
# =====================================================================
def _unzip(payload: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(payload))


def _sheet_rows(payload: bytes) -> list[dict[str, tuple[str, str | None]]]:
    """解析 sheet1.xml → [{列标: (类型, 文本)}], 类型 ∈ n / s / empty。"""
    with _unzip(payload) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    rows: list[dict[str, tuple[str, str | None]]] = []
    for row in root.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row"):
        cells: dict[str, tuple[str, str | None]] = {}
        for cell in row.findall(f"{SHEET_NS}c"):
            col = "".join(ch for ch in (cell.get("r") or "") if ch.isalpha())
            if cell.get("t") == "inlineStr":
                node = cell.find(f"{SHEET_NS}is/{SHEET_NS}t")
                cells[col] = ("s", node.text or "")
            else:
                node = cell.find(f"{SHEET_NS}v")
                cells[col] = ("n", node.text) if node is not None else ("empty", None)
        rows.append(cells)
    return rows


def _docx_texts(payload: bytes) -> list[str]:
    with _unzip(payload) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return [node.text or "" for node in root.iter(f"{WORD_NS}t")]


def _docx_tables(payload: bytes) -> list[list[list[str]]]:
    """解析 w:tbl → [[[单元格文本, ...], ...], ...]。"""
    with _unzip(payload) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    tables = []
    for tbl in root.iter(f"{WORD_NS}tbl"):
        rows = []
        for tr in tbl.findall(f"{WORD_NS}tr"):
            cells = []
            for tc in tr.findall(f"{WORD_NS}tc"):
                texts = [t.text or "" for t in tc.iter(f"{WORD_NS}t")]
                cells.append("".join(texts))
            rows.append(cells)
        tables.append(rows)
    return tables


# =====================================================================
# 一、xlsx 结构与编码
# =====================================================================
def test_xlsx_contains_required_parts():
    payload = to_xlsx("选股结果", ["代码", "名称"], [["600519", "贵州茅台"]])
    with _unzip(payload) as archive:
        assert archive.testzip() is None, "zip 内部数据损坏"
        names = set(archive.namelist())
    assert names >= XLSX_PARTS, f"缺少必需部件: {XLSX_PARTS - names}"
    # workbook.xml 里必须挂上 sheet1, 否则 Excel 打不开
    with _unzip(payload) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
        wb_rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    assert 'r:id="rId1"' in workbook
    assert 'Target="worksheets/sheet1.xml"' in wb_rels


def test_xlsx_roundtrip_chinese_and_special_chars():
    """中文 + & < > \" ' 五个字符必须逐字还原, 且 XML 仍可解析。"""
    weird = 'A股&B <tag> "引用" \'单引\' 中文'
    payload = to_xlsx("中文表&名", ["列&1", "列<2>"], [[weird, None], ["贵州茅台", 1.5]])
    rows = _sheet_rows(payload)
    assert rows[0]["A"] == ("s", "列&1")
    assert rows[0]["B"] == ("s", "列<2>")
    assert rows[1]["A"] == ("s", weird)
    assert rows[2]["B"] == ("n", "1.5")
    with _unzip(payload) as archive:
        workbook = archive.read("xl/workbook.xml").decode("utf-8")
    assert 'name="中文表&amp;名"' in workbook


def test_xlsx_numeric_is_number_and_empty_is_not_filled():
    """数值走 <v> 且不套 t=\"str\"; None / NaN / Inf 一律留空, 不写占位符。"""
    payload = to_xlsx("t", ["数值", "空值"], [[1, None], [2.5, float("nan")], [-3, float("inf")]])
    rows = _sheet_rows(payload)
    assert rows[1]["A"] == ("n", "1")
    assert rows[1]["B"] == ("empty", None)
    assert rows[2]["A"] == ("n", "2.5")
    assert rows[2]["B"] == ("empty", None)
    assert rows[3]["A"] == ("n", "-3")
    assert rows[3]["B"] == ("empty", None)
    with _unzip(payload) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert 't="str"' not in sheet, "数值单元格不得退化成字符串类型"


def test_xlsx_short_row_pads_empty_cells():
    """行比表头短 → 缺的列是空单元格, 不补占位符。"""
    payload = to_xlsx("t", ["a", "b", "c"], [["x"]])
    rows = _sheet_rows(payload)
    assert rows[1]["A"] == ("s", "x")
    assert rows[1].get("B") == ("empty", None)
    assert rows[1].get("C") == ("empty", None)


def test_xlsx_column_width_is_adaptive():
    payload = to_xlsx("t", ["短", "很长很长很长的一个列名"], [[1, "x"]])
    with _unzip(payload) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    widths = [float(m) for m in re.findall(r'<col [^>]*width="([\d.]+)"', sheet)]
    assert len(widths) == 2
    assert widths[1] > widths[0], "长列应该比短列宽"


# =====================================================================
# 二、docx 结构与编码
# =====================================================================
def test_docx_contains_required_parts():
    payload = to_docx("标题", [{"heading": "小节", "paragraphs": ["正文"]}])
    with _unzip(payload) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
    assert names >= DOCX_PARTS, f"缺少必需部件: {DOCX_PARTS - names}"


def test_docx_roundtrip_chinese_and_special_chars():
    weird = '贵州茅台 & 五粮液 <tag> "引用" \'单引\''
    payload = to_docx("回测报告 & 中文", [
        {"heading": "小节 <带尖括号>", "paragraphs": [weird]},
    ])
    texts = _docx_texts(payload)
    assert "回测报告 & 中文" in texts
    assert "小节 <带尖括号>" in texts
    assert weird in texts


def test_docx_renders_table():
    payload = to_docx("t", [{"heading": "绩效指标", "table": {
        "columns": ["指标", "数值"],
        "rows": [["总收益率", 0.1234], ["缺失值", None]],
    }}])
    tables = _docx_tables(payload)
    assert len(tables) == 1
    assert tables[0][0] == ["指标", "数值"]
    assert tables[0][1] == ["总收益率", "0.1234"]
    assert tables[0][2] == ["缺失值", ""], "缺失值必须是空单元格, 不是占位符"


def test_docx_with_no_content_is_still_valid():
    payload = to_docx("", [])
    assert set(zipfile.ZipFile(io.BytesIO(payload)).namelist()) >= DOCX_PARTS
    ET.fromstring(zipfile.ZipFile(io.BytesIO(payload)).read("word/document.xml"))


# =====================================================================
# 三、源码护栏:不得有造数兜底, 不得引入第三方 OOXML 库
# =====================================================================
@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_source_has_no_fake_data_or_third_party_ooxml(path: Path):
    source = path.read_text(encoding="utf-8")
    assert "random." not in source, f"{path.name} 出现 random. 造数"
    assert "_DEMO_" not in source, f"{path.name} 出现 _DEMO_ 占位数据"
    for banned in ("openpyxl", "python-docx", "docx", "xlsxwriter"):
        assert f"import {banned}" not in source, f"{path.name} 引入了 {banned}"


# =====================================================================
# 四、API 层
# =====================================================================
@pytest.fixture(scope="module")
def client():
    """主应用 + export 路由。必须声明本机地址, 否则被鉴权守卫判 403。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from app.api.export import router as export_router
    from app.main import app

    # 路由注册由接线方统一做; 测试里自行挂载, 不改动 main.py。
    # 必须**前插**:main.py 末尾有 `/{full_path:path}` 的 SPA 兜底路由,
    # append 的话 /api/export/* 会被它吃掉返回 index.html。
    if not any(getattr(r, "path", "").startswith("/api/export") for r in app.routes):
        app.router.routes = list(export_router.routes) + list(app.router.routes)
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        yield c


def test_screen_xlsx_unavailable_returns_200_with_reason(client, monkeypatch):
    """上游无选股结果 → 200 + 文档内写明 unavailable + reason, 且不编数据。"""
    from app.api import screener as screener_api

    monkeypatch.setattr(
        screener_api, "_cached_with_realtime",
        lambda request: {"as_of": None, "results": {}, "updated_at": None},
    )
    r = client.get("/api/export/screen.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename*=UTF-8''" in r.headers["content-disposition"]

    rows = _sheet_rows(r.content)
    body = rows[1]
    assert body["A"][1] == "unavailable"
    assert body["B"][1], "必须写明不可用原因"
    # 除了表头与这一行状态说明, 不允许出现任何数值单元格
    numbers = [
        value for row in rows[1:] for kind, value in row.values() if kind == "n"
    ]
    assert numbers == [], f"unavailable 文档里不该有数值: {numbers}"


def test_screen_xlsx_exports_rows_with_chinese(client, monkeypatch):
    """有选股结果 → 表头 + 明细 + 缺失字段留空, 中文正常。"""
    from app.api import screener as screener_api

    monkeypatch.setattr(
        screener_api, "_cached_with_realtime",
        lambda request: {
            "as_of": "2026-10-16",
            "results": {"ma_golden": {
                "as_of": "2026-10-16",
                "total": 2,
                "rows": [
                    {"symbol": "600519", "name": "贵州茅台", "score": 9.5},
                    {"symbol": "000001", "name": "平安银行"},  # 缺 score → 空单元格
                ],
            }},
            "updated_at": None,
        },
    )
    r = client.get("/api/export/screen.xlsx")
    assert r.status_code == 200
    rows = _sheet_rows(r.content)
    assert rows[0]["A"][1] == "symbol"
    assert rows[1]["B"][1] == "贵州茅台"
    assert rows[1]["C"] == ("n", "9.5")
    assert rows[2].get("C") == ("empty", None), "缺失字段必须是空单元格"


def test_backtest_docx_unavailable_returns_200_with_reason(client, monkeypatch):
    """回测引擎不可用 → 200 + unavailable + reason, 不含任何伪造数值。"""
    from app.services import backtest as backtest_service

    def _boom():
        raise backtest_service.VectorbtUnavailableError("vectorbt 未安装")

    monkeypatch.setattr(backtest_service, "_get_vbt", _boom)
    r = client.get("/api/export/backtest.docx?symbols=600519,000001")
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    texts = _docx_texts(r.content)
    joined = "\n".join(texts)
    assert "unavailable" in joined
    assert "vectorbt 未安装" in joined, "必须写明不可用原因"
    # 标题(含日期)之外的正文不得出现小数数值 —— 没有数据就不该有数字
    body = "\n".join(texts[1:])
    assert re.search(r"\d+\.\d+", body) is None, f"unavailable 文档里出现数值: {body}"
    assert _docx_tables(r.content) == [], "unavailable 文档不该有数据表"


def test_backtest_docx_exports_sections_when_available(client, monkeypatch):
    """回测成功 → 概览 / 绩效 / 交易明细 / 净值曲线分节齐全, 空值不留占位。"""
    from app.services.backtest import BacktestResult, BacktestService

    fake = BacktestResult(
        run_id="abc123",
        config={"start": "2025-01-01", "end": "2025-06-30", "matching": "close_t",
                "stop_loss_pct": None, "max_hold_days": None, "fees_pct": 0.0002,
                "slippage_bps": 5},
        stats={"Total Return [%]": 12.5, "Max Drawdown [%]": -3.25},
        equity_curve=[{"date": "2025-01-02", "value": 1.0}],
        trades=[{"symbol": "600519", "entry_date": "2025-01-02",
                 "exit_date": "2025-02-03", "pnl_pct": 0.08}],
        per_symbol_stats=[{"symbol": "600519", "total_return": 0.08}],
    )
    monkeypatch.setattr(BacktestService, "run", lambda self, config: fake)

    r = client.get("/api/export/backtest.docx?symbols=600519&entries=macd_golden")
    assert r.status_code == 200
    texts = _docx_texts(r.content)
    joined = "\n".join(texts)
    assert "回测概览" in joined
    assert "abc123" in joined
    assert "绩效指标" in joined
    assert "交易明细" in joined
    assert "净值曲线" in joined
    tables = _docx_tables(r.content)
    # 绩效 / 交易 / 分标的 / 净值 四张表
    assert len(tables) == 4
    assert tables[0][1] == ["Total Return [%]", "12.5"]
    assert tables[2][1][0] == "600519", "分标的收益表首列应是代码"
