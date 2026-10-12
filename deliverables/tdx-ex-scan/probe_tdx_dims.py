# -*- coding: utf-8 -*-
"""探测 easy_tdx 在 7727(EX) 上还能拿到哪些港美股维度。

只读探测: 不写盘。每条用例**独立建连接**, 因为 EX 服务器会因不支持的命令
直接断开连接(连着跑会把后续用例全误判成失败)。
"""
from __future__ import annotations

import sys
from dataclasses import asdict, is_dataclass
from datetime import date

import pandas as pd

from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection
from app._vendor.easy_tdx.mac.commands import (
    BoardListCmd,
    SymbolAuctionCmd,
    SymbolBelongBoardCmd,
    SymbolCapitalFlowCmd,
    SymbolInfoCmd,
    SymbolTickChartCmd,
    SymbolTransactionCmd,
    TickChartsCmd,
    UnusualCmd,
)
from app._vendor.easy_tdx.mac.enums import ExBoardType

EX_PORT = 7727
TIMEOUT = 8.0


def to_rows(raw):
    """命令 parse 结果 → 便于打印的行。"""
    if raw is None:
        return "None"
    if isinstance(raw, pd.DataFrame):
        return raw
    if isinstance(raw, list):
        items = [asdict(x) if is_dataclass(x) else x for x in raw]
        return pd.DataFrame(items) if items else pd.DataFrame()
    if is_dataclass(raw):
        return pd.DataFrame([asdict(raw)])
    return str(raw)[:200]


def run_one(host, tag, mk_cmd):
    conn = ExTdxConnection(host, EX_PORT, TIMEOUT, mac_ex_mode=True)
    try:
        conn.connect()
        conn.execute(MacExLoginCmd())
        rows = to_rows(conn.execute(mk_cmd()))
        if isinstance(rows, str):
            print("  [%s] -> %s" % (tag, rows))
        elif getattr(rows, "empty", True):
            print("  [%s] EMPTY" % tag)
        else:
            print("  [%s] OK rows=%d cols=%s" % (tag, len(rows), list(rows.columns)[:26]))
            print("        " + str(rows.head(1).to_dict("records")[0])[:420])
    except Exception as exc:  # noqa: BLE001 - 探测脚本要吞下一切继续跑
        print("  [%s] ERR %s: %s" % (tag, type(exc).__name__, str(exc)[:110]))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass


CASES = [
    ("info AAPL.US", lambda: SymbolInfoCmd(74, "AAPL")),
    ("info 00700.HK", lambda: SymbolInfoCmd(31, "00700")),
    ("capital_flow AAPL", lambda: SymbolCapitalFlowCmd(74, "AAPL")),
    ("capital_flow 00700", lambda: SymbolCapitalFlowCmd(31, "00700")),
    ("auction AAPL", lambda: SymbolAuctionCmd(74, "AAPL")),
    ("auction 00700", lambda: SymbolAuctionCmd(31, "00700")),
    ("tick_chart AAPL today", lambda: SymbolTickChartCmd(74, "AAPL", None)),
    ("tick_charts AAPL 5d", lambda: TickChartsCmd(74, "AAPL", None, 5)),
    ("tick_charts 00700 5d", lambda: TickChartsCmd(31, "00700", None, 5)),
    ("txns AAPL 1010", lambda: SymbolTransactionCmd(74, "AAPL", date(2026, 10, 10), 0, 20)),
    ("txns 00700 1010", lambda: SymbolTransactionCmd(31, "00700", date(2026, 10, 10), 0, 20)),
    ("belong_board AAPL", lambda: SymbolBelongBoardCmd(74, "AAPL")),
    ("belong_board 00700", lambda: SymbolBelongBoardCmd(31, "00700")),
    ("board_list US_ALL", lambda: BoardListCmd(ExBoardType.US_ALL, 0, 50)),
    ("board_list HK_ALL", lambda: BoardListCmd(ExBoardType.HK_ALL, 0, 50)),
    ("board_list HK_HY", lambda: BoardListCmd(ExBoardType.HK_HY, 0, 50)),
    ("board_list US_HY", lambda: BoardListCmd(ExBoardType.US_HY, 0, 50)),
    ("unusual US", lambda: UnusualCmd(74, 0, 50)),
    ("unusual HK", lambda: UnusualCmd(31, 0, 50)),
]


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "116.205.135.205"
    print("=== EX %s:%d ===" % (host, EX_PORT))
    for tag, mk in CASES:
        run_one(host, tag, mk)


if __name__ == "__main__":
    main()
