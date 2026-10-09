#!/usr/bin/env python3
"""SEC EDGAR 宿主中转代理(给 Docker 容器借道用)。

背景(2026-10-09 判别性测试实测):
- 容器内直连 data.sec.gov 被 SNI 阻断: 带 SNI 握手 3/3 SSL EOF,
  不带 SNI 握手 OK; 同一时段宿主直连 3/3 放行。
- 机场节点出口(香港 HKT/新加坡)被 SEC 的 Akamai CDN 拒绝(TLS RST),
  订阅内多节点均如此。
- ⇒ 唯一稳定通路 = 宿主 Windows 协议栈直连。

方案: 容器 -> 本代理(宿主协议栈发起对 SEC 的连接) -> SEC。
本代理只做 CONNECT 隧道字节转发, 不解密/不缓存/不记内容。

安全设计:
- 目标白名单: 仅 *.sec.gov 的 80/443, 其余一律 403(不会变成开放代理)。
- 源白名单: 仅接受 loopback 与 RFC1918 网段(本机/Docker/WSL)。

用法(宿主 PowerShell):
  python scripts/sec_relay.py            # 前台跑, 端口 17897
  python scripts/sec_relay.py --port 17897
容器侧(.env): SEC_PROXY=http://host.docker.internal:17897
"""
from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler

ALLOWED_HOST_SUFFIX = ".sec.gov"
ALLOWED_HOST_EXACT = "sec.gov"
ALLOWED_PORTS = (80, 443)
BUF = 65536
IDLE_TIMEOUT_S = 120.0


def _client_ip_allowed(ip: str) -> bool:
    return (
        ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("172.")
        or ip == "::1"
        or ip.startswith("fd")
        or ip.startswith("fe80")
    )


def _target_allowed(host: str, port: int) -> bool:
    host_ok = host == ALLOWED_HOST_EXACT or host.endswith(ALLOWED_HOST_SUFFIX)
    return host_ok and port in ALLOWED_PORTS


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_CONNECT(self):  # noqa: N802 (http.server 接口约定)
        client_ip = self.client_address[0]
        if not _client_ip_allowed(client_ip):
            self.send_error(403, "client not allowed")
            return
        host, _, port_s = self.path.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            self.send_error(400, "bad target")
            return
        if not _target_allowed(host, port):
            self.log_line(f"DENY {host}:{port} (whitelist is *{ALLOWED_HOST_SUFFIX} only)")
            self.send_error(403, "target not allowed")
            return
        try:
            upstream = socket.create_connection((host, port), timeout=15)
        except OSError as exc:
            self.log_line(f"DIAL-FAIL {host}:{port} {exc}")
            self.send_error(502, "upstream unreachable")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.log_line(f"RELAY {host}:{port}")
        self._pump(self.connection, upstream)

    def _pump(self, a: socket.socket, b: socket.socket) -> None:
        """select 单线程双向字节泵。任一侧 EOF/错误即整体收尾。"""
        a.settimeout(None)
        b.settimeout(None)
        try:
            socks = [a, b]
            while True:
                r, _, _ = select.select(socks, [], [], IDLE_TIMEOUT_S)
                if not r:
                    return  # 空闲超时
                for s in r:
                    data = s.recv(BUF)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    # 非 CONNECT 一律拒绝(本代理只做隧道)
    def do_GET(self):  # noqa: N802
        self.send_error(403, "CONNECT only")

    do_POST = do_PUT = do_DELETE = do_HEAD = do_GET  # noqa: N815

    def log_line(self, msg: str) -> None:
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stdout.flush()

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        pass


class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    ap = argparse.ArgumentParser(description="SEC EDGAR relay (CONNECT tunnel, sec.gov only)")
    ap.add_argument("--port", type=int, default=17897)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="默认 0.0.0.0 以便容器经 host.docker.internal 访问; "
                         "已有源 IP 白名单兜底")
    args = ap.parse_args()
    with RelayServer((args.bind, args.port), RelayHandler) as srv:
        print(f"sec_relay listening on {args.bind}:{args.port} "
              f"(only *{ALLOWED_HOST_SUFFIX}:{ALLOWED_PORTS}, idle-timeout {IDLE_TIMEOUT_S:.0f}s)",
              flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("bye", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
