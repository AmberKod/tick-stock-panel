# -*- coding: utf-8 -*-
"""第二轮: 五档盘口 / 板块成分股 / 多日分时美股为何炸。"""
from __future__ import annotations

import sys

from app._vendor.easy_tdx.codec.bitmap import PresetField
from app._vendor.easy_tdx.ex.commands.get_instrument_quote import GetExInstrumentQuoteCmd
from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection
from app._vendor.easy_tdx.mac.client import _convert_board_code
from app._vendor.easy_tdx.mac.commands import BoardMembersQuotesCmd, TickChartsCmd

EX_PORT = 7727
TIMEOUT = 8.0


def fresh(host):
    c = ExTdxConnection(host, EX_PORT, TIMEOUT, mac_ex_mode=True)
    c.connect()
    c.execute(MacExLoginCmd())
    return c


def run(host, tag, fn):
    try:
        c = fresh(host)
        try:
            out = fn(c)
        finally:
            c.close()
        if out is None:
            print("  [%s] None" % tag)
            return
        if isinstance(out, list):
            print("  [%s] list n=%d" % (tag, len(out)))
            if out:
                first = out[0]
                print("        " + str(first)[:520])
            return
        if hasattr(out, "shape") and out.shape[0]:
            print("  [%s] rows=%d cols=%s" % (tag, len(out), list(out.columns)[:40]))
            print("        " + str(out.head(1).to_dict("records")[0])[:520])
            return
        if hasattr(out, "shape"):
            print("  [%s] EMPTY DF" % tag)
            return
        print("  [%s] %s" % (tag, str(out)[:520]))
    except Exception as exc:  # noqa: BLE001
        print("  [%s] ERR %s: %s" % (tag, type(exc).__name__, str(exc)[:140]))


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "116.205.135.205"
    print("=== %s:%d ===" % (host, EX_PORT))
    run(host, "quote5 AAPL.US", lambda c: c.execute(GetExInstrumentQuoteCmd(74, "AAPL")))
    run(host, "quote5 00700.HK", lambda c: c.execute(GetExInstrumentQuoteCmd(31, "00700")))
    run(host, "board_members US0026", lambda c: c.execute(BoardMembersQuotesCmd(
        board_code=_convert_board_code("US0026"), start=0, page_size=8,
        fields=PresetField.COMMON)))
    run(host, "board_members HK0211", lambda c: c.execute(BoardMembersQuotesCmd(
        board_code=_convert_board_code("HK0211"), start=0, page_size=8,
        fields=PresetField.COMMON)))
    run(host, "tick_charts AAPL 5d", lambda c: c.execute(TickChartsCmd(74, "AAPL", None, 5)))


if __name__ == "__main__":
    main()
