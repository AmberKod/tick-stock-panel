# -*- coding: utf-8 -*-
"""多日分时为何美股解码崩: 直接看上游 minutes 字段的原始取值。"""
from __future__ import annotations

import struct
import sys

from app._vendor.easy_tdx._binary import unpack_from
from app._vendor.easy_tdx.codec.mac_frame import build_mac_request
from app._vendor.easy_tdx.commands.base import BaseCommand
from app._vendor.easy_tdx.ex.commands.login import MacExLoginCmd
from app._vendor.easy_tdx.ex.transport.sync import ExTdxConnection

PORT, TIMEOUT = 7727, 8.0


class _RawCmd(BaseCommand[bytes]):
    """原样要请求、原样吐回 body —— 只为看字节, 不套任何解码。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def build_request(self) -> bytes:
        return self._payload

    def parse_response(self, body: bytes) -> bytes:
        return bytes(body)


def raw_body(host, market, code, days=5):
    conn = ExTdxConnection(host, PORT, TIMEOUT, mac_ex_mode=True)
    conn.connect()
    try:
        conn.execute(MacExLoginCmd())
        return conn.execute(_RawCmd(build_mac_request(0x123E, struct.pack(
            "<H22sIHH6x", market, code.encode("gbk"), 0, days, 1))))
    finally:
        conn.close()


def dump(tag, market, code, host):
    print("===", tag)
    try:
        body = raw_body(host, market, code)
    except Exception as exc:  # noqa: BLE001
        print("  ERR", type(exc).__name__, str(exc)[:120])
        return
    if not isinstance(body, (bytes, bytearray)):
        print("  unexpected:", type(body), str(body)[:120])
        return
    body = bytes(body)
    print("  len(body)=", len(body))
    (count, send_last, page_size, total) = unpack_from("<HBHH", body, 64, "h2")
    print("  count=%d send_last=%d page_size=%d total=%d" % (count, send_last, page_size, total))
    dates = unpack_from("<5I", body, 24, "dates")
    pres = unpack_from("<5f", body, 44, "pres")
    print("  dates=", dates)
    print("  precloses=", ["%.3f" % v for v in pres])
    for d in range(count):
        vals = []
        for t in range(min(page_size, 6)):
            off = 71 + (d * page_size + t) * 14
            if off + 14 > len(body):
                vals.append("EOF")
                break
            (minutes, price, avg, vol, _) = unpack_from("<HffHH", body, off, "tick")
            vals.append((minutes, round(price, 3), vol))
        # 末尾几根
        tail_vals = []
        for t in range(max(0, page_size - 3), page_size):
            off = 71 + (d * page_size + t) * 14
            if off + 14 > len(body):
                break
            (minutes, price, avg, vol, _) = unpack_from("<HffHH", body, off, "tick")
            tail_vals.append((minutes, round(price, 3), vol))
        print("   day%d first=%s" % (d, vals))
        print("         last =%s" % (tail_vals,))
        mx = 0
        for t in range(page_size):
            off = 71 + (d * page_size + t) * 14
            if off + 14 > len(body):
                break
            (minutes, _p, _a, _v, _) = unpack_from("<HffHH", body, off, "tick")
            mx = max(mx, minutes)
        print("         max minutes=%d -> h=%d" % (mx, mx // 60))


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "116.205.135.205"
    dump("00700.HK (OK expected)", 31, "00700", host)
    dump("AAPL.US (crashes today)", 74, "AAPL", host)
