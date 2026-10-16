"""零依赖 OOXML 导出 (xlsx / docx)。

只用 Python 标准库 ``zipfile`` + ``xml.sax.saxutils``, 不引入 openpyxl /
python-docx / xlsxwriter 等任何第三方依赖。

两条硬规矩:
1. **纯函数** —— 输入数据 → bytes。不发网络请求、不读数据库、不依赖全局状态,
   同样的输入永远得到同样的输出(便于单测直接断言)。
2. **缺失值留空, 绝不编数** —— ``None`` / 空串 / NaN / Inf 一律写成空单元格或
   空文本, 不写任何占位符。缺失原因由调用方以普通文本写进文档(本模块不生成
   任何数值, 只做「数据 → OOXML」的搬运)。
"""
from __future__ import annotations

import io
import math
import zipfile
from typing import Any
from xml.sax.saxutils import escape

__all__ = ["to_docx", "to_xlsx"]

# OOXML 固定命名空间
_SPREADSHEETML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_WORDPROCESSINGML = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PKG_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_PKG_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

# saxutils.escape 默认只处理 & < >;" ' 追加进来(规范要求五个字符全部转义)
_EXTRA_ENTITIES = {"'": "&apos;", '"': "&quot;"}

# xlsx 工作表名的非法字符(Excel 约束)
_SHEET_BAD_CHARS = frozenset('[]:*?/\\')


# =====================================================================
# 公共小工具
# =====================================================================
def _esc(value: Any) -> str:
    """任意值 → 合法 XML 文本。``& < >`` 由 saxutils 处理, ``" '`` 追加。"""
    if value is None:
        return ""
    return escape(str(value), _EXTRA_ENTITIES)


def _text(value: Any) -> str:
    """单元格/段落的可显示文本。空值与非有限数值 → 空串(不写占位符)。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        # NaN / Inf 在 OOXML 数值类型里没有合法表示, 只能是「缺失」→ 留空。
        return ""
    return str(value)


def _display_len(text: str) -> int:
    """按显示宽度估算:CJK 记 2 个字符宽, 其余记 1。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _col_letter(index: int) -> str:
    """0-based 列下标 → 列标(A / B / ... / Z / AA)。"""
    letters = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _sheet_name(raw: str) -> str:
    """规范化工作表名:去掉 Excel 非法字符与控制字符, 截断到 31 字符。"""
    cleaned = []
    for ch in _text(raw).strip():
        if ch in _SHEET_BAD_CHARS or ord(ch) < 32:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    return "".join(cleaned)[:31] or "Sheet1"


def _zip(parts: dict[str, str]) -> bytes:
    """把 {部件名: xml 文本} 打包成 OOXML(zip + deflate)。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, xml.encode("utf-8"))
    return buf.getvalue()


# =====================================================================
# xlsx
# =====================================================================
def _inline_cell(ref: str, text: str) -> str:
    """文本单元格(inlineStr, 不需要 sharedStrings 部件)。"""
    return (
        f'<c r="{ref}" t="inlineStr">'
        f'<is><t xml:space="preserve">{_esc(text)}</t></is>'
        "</c>"
    )


def _cell_xml(ref: str, value: Any) -> str:
    """单个单元格。数值走 <v>(不套 t="str"), 文本走 inlineStr, 空值留空。"""
    if value is None:
        return f'<c r="{ref}"/>'
    if isinstance(value, bool):
        return _inline_cell(ref, "TRUE" if value else "FALSE")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return f'<c r="{ref}"/>'
        number = str(value) if isinstance(value, int) else repr(value)
        return f'<c r="{ref}"><v>{number}</v></c>'
    text = str(value)
    return _inline_cell(ref, text) if text else f'<c r="{ref}"/>'


def _col_width(texts: list[str]) -> str:
    """列宽 = 最宽单元格 + 2 的余量, 夹在 [8, 60]。"""
    widest = max((_display_len(t) for t in texts), default=0)
    return f"{min(60, max(8, widest + 2)):g}"


def to_xlsx(sheet_name: str, columns: list[str], rows: list[list]) -> bytes:
    """把「表头 + 数据行」写成最小合法 xlsx, 返回 bytes。

    - ``columns``: 表头文本列表。
    - ``rows``: 数据行, 每行一个值列表; 行比 columns 短 → 缺的单元格留空,
      行比 columns 长 → 多余的值丢弃(表头为准)。
    - 数值写 <v>, 文本写 inlineStr, 空值写空单元格; 列宽按内容自适应。
    """
    header = [_text(c) for c in (columns or [])]
    grid: list[list[Any]] = [list(header)] + [list(r) for r in (rows or [])]

    # 列宽:把该列所有单元格(含表头)的文本收集起来取最宽
    per_column: list[list[str]] = [[] for _ in header]
    for row in grid:
        for ci in range(len(header)):
            per_column[ci].append(_text(row[ci]) if ci < len(row) else "")

    cols_xml = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{_col_width(per_column[i])}" customWidth="1"/>'
        for i in range(len(header))
    )

    rows_xml = []
    for ri, row in enumerate(grid, start=1):
        cells = "".join(
            _cell_xml(f"{_col_letter(ci)}{ri}", row[ci] if ci < len(row) else None)
            for ci in range(len(header))
        )
        rows_xml.append(f'<row r="{ri}">{cells}</row>')

    sheet = (
        _XML_DECL +
        f'<worksheet xmlns="{_SPREADSHEETML}">'
        f"<cols>{cols_xml}</cols>"
        f'<sheetData>{"".join(rows_xml)}</sheetData>'
        "</worksheet>"
    )
    content_types = (
        _XML_DECL +
        f'<Types xmlns="{_PKG_CONTENT_TYPES}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    root_rels = (
        _XML_DECL +
        f'<Relationships xmlns="{_PKG_RELATIONSHIPS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_RELATIONSHIPS}/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        _XML_DECL +
        f'<workbook xmlns="{_SPREADSHEETML}" xmlns:r="{_OFFICE_RELATIONSHIPS}">'
        f'<sheets><sheet name="{_esc(_sheet_name(sheet_name))}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        _XML_DECL +
        f'<Relationships xmlns="{_PKG_RELATIONSHIPS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_RELATIONSHIPS}/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    return _zip({
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/worksheets/sheet1.xml": sheet,
    })


# =====================================================================
# docx
# =====================================================================
def _run(text: str, *, bold: bool = False, size: int | None = None) -> str:
    """一个 run。文字一律走 _esc。"""
    props = ""
    if bold or size:
        props = "<w:rPr>" + ("<w:b/>" if bold else "") + (
            f'<w:sz w:val="{size}"/>' if size else ""
        ) + "</w:rPr>"
    return (
        "<w:r>"
        f"{props}"
        f'<w:t xml:space="preserve">{_esc(text)}</w:t>'
        "</w:r>"
    )


def _paragraph(text: Any, *, bold: bool = False, size: int | None = None,
               align: str | None = None) -> str:
    text = _text(text)
    props = f'<w:jc w:val="{align}"/>' if align else ""
    props = f"<w:pPr>{props}</w:pPr>" if props else ""
    run = _run(text, bold=bold, size=size) if text else ""
    return f"<w:p>{props}{run}</w:p>"


def _table_xml(table: dict) -> str:
    """``{"columns": [...], "rows": [[...]]}`` → w:tbl。"""
    columns = [_text(c) for c in (table.get("columns") or [])]
    grid: list[list[Any]] = [list(columns)] + [list(r) for r in (table.get("rows") or [])]
    texts = [[_text(v) for v in row] for row in grid]

    widths: list[int] = []
    for ci in range(len(columns)):
        widest = max(
            (_display_len(row[ci]) for row in texts if ci < len(row)),
            default=0,
        )
        widths.append(max(900, min(5000, (widest + 2) * 240)))

    rows_xml = []
    for ri, row in enumerate(texts):
        cells = []
        for ci in range(len(columns)):
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{widths[ci]}" w:type="dxa"/></w:tcPr>'
                f'<w:p>{_run(row[ci] if ci < len(row) else "", bold=(ri == 0))}</w:p>'
                "</w:tc>"
            )
        # 首行作为表头重复出现在每页顶部
        header_pr = "<w:trPr><w:tblHeader/></w:trPr>" if ri == 0 else ""
        rows_xml.append(f"<w:tr>{header_pr}{''.join(cells)}</w:tr>")

    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="999999"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    return (
        "<w:tbl>"
        '<w:tblPr><w:tblW w:w="0" w:type="auto"/>'
        f"<w:tblBorders>{borders}</w:tblBorders></w:tblPr>"
        f"{''.join(rows_xml)}"
        "</w:tbl>"
    )


def to_docx(title: str, sections: list[dict]) -> bytes:
    """把「标题 + 分节」写成最小合法 docx, 返回 bytes。

    ``sections`` 形如::

        [{"heading": "绩效指标",
          "paragraphs": ["总收益率: 0.12", ...],
          "table": {"columns": ["指标", "数值"], "rows": [["总收益率", 0.12]]}}]

    每个 section 的三个键都可缺省; ``table`` 缺省则不渲染表格。
    """
    body: list[str] = []
    if _text(title):
        body.append(_paragraph(title, bold=True, size=36, align="center"))

    for section in sections or []:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        if _text(heading):
            body.append(_paragraph(heading, bold=True, size=28))
        for para in section.get("paragraphs") or []:
            body.append(_paragraph(para))
        table = section.get("table")
        if isinstance(table, dict) and (table.get("columns") or table.get("rows")):
            body.append(_table_xml(table))
            # 表格后必须跟一个段落, 否则 Word 认为文档在表格处被截断
            body.append("<w:p></w:p>")

    if not body:
        body.append("<w:p></w:p>")

    document = (
        _XML_DECL +
        f'<w:document xmlns:w="{_WORDPROCESSINGML}">'
        f'<w:body>{"".join(body)}</w:body>'
        "</w:document>"
    )
    content_types = (
        _XML_DECL +
        f'<Types xmlns="{_PKG_CONTENT_TYPES}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    root_rels = (
        _XML_DECL +
        f'<Relationships xmlns="{_PKG_RELATIONSHIPS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_RELATIONSHIPS}/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    return _zip({
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "word/document.xml": document,
    })
