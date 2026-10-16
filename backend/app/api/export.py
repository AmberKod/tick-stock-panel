"""导出 API —— 选股结果 → xlsx, 回测结果 → docx。

零依赖 OOXML(见 ``app/services/ooxml_export.py``), 不引入 openpyxl / python-docx。

取数来源(实地读码确认, 不臆造字段):
- 选股: ``app/api/screener.py::_cached_with_realtime`` —— 与策略页同一套读取
  逻辑(``app/services/strategy_cache.py::read_cache`` 的盘后缓存 + 监控引擎
  ``latest_strategy_results()`` 的实时叠加 + 概念热度失效校验)。
- 回测: ``app/services/backtest.py::BacktestService.run`` —— 传入
  ``BacktestConfig`` 跑一次信号回测, 取 ``BacktestResult`` 的
  stats / equity_curve / trades / per_symbol_stats。

上游不可用时 **返回 200 + 最小文档**, 文档内写明 ``unavailable`` 与原因;
既不 500, 也不编造任何数值。缺失字段留空单元格 / 空文本。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.services.ooxml_export import to_docx, to_xlsx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/export", tags=["export"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# 不可用标记:文档内必须原样出现这个词, 方便人/机一眼区分「没数据」与「有数据」
UNAVAILABLE = "unavailable"

# 导出明细的行数上限(超出部分不导出, 但会在文档里写明截断与总数, 不是伪造)
MAX_TRADES = 500
MAX_EQUITY_POINTS = 500

# 选股表列顺序:这几个键优先靠前, 其余按首次出现的顺序排
_SCREEN_PREFERRED_COLUMNS = ("symbol", "name", "score")
# 回测明细列顺序
_TRADE_PREFERRED_COLUMNS = (
    "symbol", "entry_date", "exit_date", "entry_price", "exit_price", "pnl_pct", "duration",
)


# =====================================================================
# 公共小工具
# =====================================================================
def _content_disposition(filename: str) -> str:
    """中文文件名:ASCII 兜底名 + ``filename*=UTF-8''...``。"""
    ascii_name = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "_" for ch in filename
    ).strip("._-")
    return (
        f'attachment; filename="{ascii_name or "export"}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _binary_response(payload: bytes, mime: str, filename: str) -> Response:
    return Response(
        content=payload,
        media_type=mime,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


def _ordered_columns(rows: list[dict], preferred: tuple[str, ...]) -> list[str]:
    """按「优先列 + 首次出现顺序」取并集, 保证同一批数据导出列顺序稳定。"""
    seen: list[str] = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    ordered = [c for c in preferred if c in seen]
    ordered.extend(c for c in seen if c not in ordered)
    return ordered


def _grid(rows: list[dict], columns: list[str]) -> list[list[Any]]:
    """dict 列表 → 二维值列表。某个 dict 缺列 → 该单元格是 None(导出成空)。"""
    return [[row.get(col) for col in columns] for row in rows]


def _missing_keys(rows: list[dict], columns: list[str]) -> list[str]:
    """统计「部分行缺失」的字段, 用于文档末尾写明缺失原因。"""
    if not rows:
        return []
    missing = []
    for col in columns:
        absent = sum(1 for row in rows if col not in row)
        if absent:
            missing.append(f"{col}: {absent}/{len(rows)} 行缺失该字段, 已导出为空单元格")
    return missing


def _notes_rows(columns: list[str], notes: list[str]) -> list[list[Any]]:
    """把说明文字追加到表尾:整条说明写进第一列, 其余留空。"""
    if not columns or not notes:
        return []
    trailing: list[list[Any]] = [["数据说明"] + [None] * (len(columns) - 1)]
    trailing.extend([note] + [None] * (len(columns) - 1) for note in notes)
    return trailing


def _truncate(items: list, cap: int) -> tuple[list, int]:
    """截断到 cap 条, 返回 (保留的, 原始总数)。"""
    return items[:cap], len(items)


# =====================================================================
# GET /api/export/screen.xlsx —— 选股结果
# =====================================================================
def _load_screen_cache(request: Request) -> dict:
    """选股结果取数:复用策略页同一套缓存读取逻辑。"""
    from app.api import screener as screener_api

    return screener_api._cached_with_realtime(request)


@router.get("/screen.xlsx")
def screen_xlsx(
    request: Request,
    strategy_id: str | None = Query(None, description="只导出该策略; 不传导出全部策略"),
    limit: int = Query(0, ge=0, description="每个策略最多导出的行数, 0 表示不限"),
) -> Response:
    """导出选股结果为 xlsx。上游无数据 → 200 + 只有一行 unavailable 说明。"""
    notes: list[str] = []
    reason: str | None = None
    cached: dict | None = None

    try:
        cached = _load_screen_cache(request)
    except Exception as e:  # 上游任何异常都不允许变成 500, 但要写进文档
        logger.warning("export/screen: 读取选股结果失败: %s", e)
        reason = f"读取选股结果失败: {e}"

    as_of = cached.get("as_of") if isinstance(cached, dict) else None
    results: dict[str, dict] = {}
    if isinstance(cached, dict):
        results = {
            sid: result
            for sid, result in (cached.get("results") or {}).items()
            if isinstance(result, dict)
        }

    if reason is None:
        if strategy_id:
            if strategy_id in results:
                results = {strategy_id: results[strategy_id]}
            else:
                results = {}
                reason = f"策略 {strategy_id} 当前没有结果"
        elif not results:
            reason = "当前没有可用的选股结果缓存(请先运行选股或策略监控)"

    sheet_rows: list[dict] = []
    multi_strategy = len(results) > 1
    for sid, result in results.items():
        warnings = [str(w) for w in (result.get("warnings") or []) if w]
        notes.extend(f"{sid}: {w}" for w in warnings)
        rows = [r for r in (result.get("rows") or []) if isinstance(r, dict)]
        total = int(result.get("total") or 0)
        if limit and len(rows) > limit:
            notes.append(f"{sid}: 命中 {len(rows)} 只, 本次仅导出前 {limit} 只")
            rows = rows[:limit]
        if not rows:
            notes.append(
                f"{sid}: 该策略当前命中 0 只(total={total}), 无明细可导出"
            )
            continue
        if multi_strategy:
            sheet_rows.extend({"策略": sid, **row} for row in rows)
        else:
            sheet_rows.extend(rows)

    if reason is not None:
        # 最小文档:不编一行数据, 只写明状态与原因
        columns = ["状态", "说明"]
        body: list[list[Any]] = [[UNAVAILABLE, reason]]
        if notes:
            body.extend(_notes_rows(columns, notes))
        payload = to_xlsx("选股结果", columns, body)
        return _binary_response(payload, XLSX_MIME, f"选股结果_{date.today().isoformat()}.xlsx")

    if not sheet_rows:
        notes.append("所有策略当前命中 0 只, 表中无数据行(未编造任何内容)")

    preferred = ("策略", *_SCREEN_PREFERRED_COLUMNS) if multi_strategy else _SCREEN_PREFERRED_COLUMNS
    columns = _ordered_columns(sheet_rows, preferred)
    notes.extend(_missing_keys(sheet_rows, columns))

    body = _grid(sheet_rows, columns)
    body.extend(_notes_rows(columns, notes))
    payload = to_xlsx("选股结果", columns, body)
    filename = f"选股结果_{as_of or date.today().isoformat()}.xlsx"
    return _binary_response(payload, XLSX_MIME, filename)


# =====================================================================
# GET /api/export/backtest.docx —— 回测结果
# =====================================================================
def _run_backtest(request: Request, symbols: list[str], start: date, end: date,
                  entries: list[str], exits: list[str], stop_loss_pct: float | None,
                  max_hold_days: int | None, asset_type: str, matching: str):
    """回测取数:直接跑 BacktestService.run(现存的信号回测入口)。"""
    from app.services.backtest import BacktestConfig, BacktestService

    config = BacktestConfig(
        symbols=symbols,
        start=start,
        end=end,
        entries=entries,
        exits=exits,
        stop_loss_pct=stop_loss_pct,
        max_hold_days=max_hold_days,
        matching=matching,  # type: ignore[arg-type]
        asset_type=asset_type,
    )
    return BacktestService(request.app.state.repo).run(config)


def _unavailable_docx(title: str, reason: str, notes: list[str] | None = None) -> bytes:
    """上游不可用时的最小 docx:写明 unavailable + 原因, 不含任何数值。"""
    sections: list[dict] = [{
        "heading": "状态",
        "paragraphs": [f"{UNAVAILABLE} — {reason}"],
    }]
    if notes:
        sections.append({"heading": "说明", "paragraphs": list(notes)})
    return to_docx(title, sections)


@router.get("/backtest.docx")
def backtest_docx(
    request: Request,
    symbols: str = Query(..., description="逗号分隔的股票代码, 例 600519,000001"),
    start: date | None = Query(None, description="起始日, 默认近 3 年"),
    end: date | None = Query(None, description="结束日, 默认今天"),
    entries: str = Query("", description="逗号分隔的买入信号"),
    exits: str = Query("", description="逗号分隔的卖出信号"),
    stop_loss_pct: float | None = Query(None, description="止损比例, 例 -0.05"),
    max_hold_days: int | None = Query(None, description="最大持仓天数"),
    asset_type: str = Query("stock", description="stock / etf / index / hk / us"),
    matching: str = Query("close_t", description="close_t 或 open_t+1"),
) -> Response:
    """跑一次信号回测并导出 docx 报告。上游不可用 → 200 + unavailable 说明。"""
    end_date = end or date.today()
    start_date = start or (end_date - timedelta(days=365 * 3))
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    entry_list = [s.strip() for s in entries.split(",") if s.strip()]
    exit_list = [s.strip() for s in exits.split(",") if s.strip()]
    title = f"回测报告 {start_date.isoformat()} ~ {end_date.isoformat()}"

    if not symbol_list:
        payload = _unavailable_docx(title, "未提供股票代码(symbols 为空)")
        return _binary_response(payload, DOCX_MIME, f"{title}.docx")

    try:
        result = _run_backtest(
            request, symbol_list, start_date, end_date, entry_list, exit_list,
            stop_loss_pct, max_hold_days, asset_type, matching,
        )
    except Exception as e:  # vectorbt 缺失 / 数据缺失 / 计算失败, 一律写进文档
        logger.warning("export/backtest: 回测失败: %s", e)
        payload = _unavailable_docx(title, f"回测无法完成: {e}")
        return _binary_response(payload, DOCX_MIME, f"{title}.docx")

    stats = dict(getattr(result, "stats", None) or {})
    error = stats.pop("error", None)
    if error:
        payload = _unavailable_docx(title, f"回测引擎返回错误: {error}")
        return _binary_response(payload, DOCX_MIME, f"{title}.docx")

    config = dict(getattr(result, "config", None) or {})
    trades = list(getattr(result, "trades", None) or [])
    equity = list(getattr(result, "equity_curve", None) or [])
    per_symbol = list(getattr(result, "per_symbol_stats", None) or [])
    notes: list[str] = []

    overview = [
        f"run_id: {getattr(result, 'run_id', None) or '(缺失)'}",
        f"回测区间: {config.get('start') or start_date.isoformat()} ~ "
        f"{config.get('end') or end_date.isoformat()}",
        f"标的数: {len(symbol_list)}",
        f"标的: {', '.join(symbol_list)}",
        f"买入信号: {', '.join(entry_list) or '(未指定)'}",
        f"卖出信号: {', '.join(exit_list) or '(未指定)'}",
        f"撮合方式: {config.get('matching') or matching}",
        f"止损: {config.get('stop_loss_pct') if config.get('stop_loss_pct') is not None else '(未设置)'}",
        f"最大持仓天数: {config.get('max_hold_days') if config.get('max_hold_days') is not None else '(未设置)'}",
        f"手续费: {config.get('fees_pct') if config.get('fees_pct') is not None else '(缺失)'}",
        f"滑点(bps): {config.get('slippage_bps') if config.get('slippage_bps') is not None else '(缺失)'}",
    ]
    sections: list[dict] = [{"heading": "回测概览", "paragraphs": overview}]

    stats_rows = [[key, value] for key, value in stats.items()]
    if stats_rows:
        sections.append({
            "heading": "绩效指标",
            "table": {"columns": ["指标", "数值"], "rows": stats_rows},
        })
    else:
        notes.append("绩效指标(stats)为空, 未导出任何指标数值")

    if trades:
        shown, total = _truncate(trades, MAX_TRADES)
        columns = _ordered_columns(shown, _TRADE_PREFERRED_COLUMNS)
        sections.append({
            "heading": "交易明细",
            "paragraphs": [f"共 {total} 笔交易" + (f", 本次导出前 {len(shown)} 笔" if total > len(shown) else "")],
            "table": {"columns": columns, "rows": _grid(shown, columns)},
        })
    else:
        sections.append({"heading": "交易明细", "paragraphs": ["本次回测没有产生交易记录。"]})

    if per_symbol:
        columns = _ordered_columns(per_symbol, ("symbol", "total_return"))
        sections.append({
            "heading": "分标的收益",
            "table": {"columns": columns, "rows": _grid(per_symbol, columns)},
        })
    else:
        notes.append("分标的统计(per_symbol_stats)为空")

    if equity:
        shown, total = _truncate(equity, MAX_EQUITY_POINTS)
        columns = _ordered_columns(shown, ("date", "value"))
        sections.append({
            "heading": "净值曲线",
            "paragraphs": [f"共 {total} 个交易日" + (f", 本次导出前 {len(shown)} 个" if total > len(shown) else "")],
            "table": {"columns": columns, "rows": _grid(shown, columns)},
        })
    else:
        notes.append("净值曲线(equity_curve)为空")

    if notes:
        sections.append({"heading": "数据说明", "paragraphs": notes})

    payload = to_docx(title, sections)
    return _binary_response(payload, DOCX_MIME, f"{title}.docx")
