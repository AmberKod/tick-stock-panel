#!/usr/bin/env python3
"""SEC EDGAR 宿主中转代理(给 Docker 容器借道用; 2026-10-09 v2 升级为智能分流)。

背景(2026-10-09 判别性测试实测):
- 容器内直连 data.sec.gov 被 SNI 阻断: 带 SNI 握手 3/3 SSL EOF,
  不带 SNI 握手 OK; 同一时段宿主直连 3/3 放行。
- 机场节点出口(香港 HKT/新加坡)被 SEC 的 Akamai CDN 拒绝(TLS RST)。
- OpenBB(宿主服务)内部也访问 data.sec.gov, 其 aiohttp 只认环境变量代理,
  而 7897 对 sec.gov 分流 DIRECT -> 同样被阻断。
- ⇒ 唯一稳定通路 = 宿主 Windows 协议栈直连。

v2 智能分流(一个代理解决两类需求):
- 目标是 *.sec.gov:80/443  -> 宿主直连(绕过一切代理, 实测稳定)
- 其它目标                -> 转发给上游代理(默认 7897, 可 --upstream 关闭)
这样 OpenBB 把 HTTP_PROXY 指到本 relay 即可: SEC 走直连, yfinance 等走上游。

安全设计:
- 源白名单: 仅接受 loopback 与 RFC1918 网段(本机/Docker/WSL)。
- --upstream 未配置且目标非 SEC 时返回 403(退化为纯 SEC relay, v1 行为)。

用法(宿主 PowerShell):
  python scripts/sec_relay.py --upstream http://127.0.0.1:7897   # 推荐
  python scripts/sec_relay.py                                    # 纯 SEC(旧行为)
容器侧(.env): SEC_PROXY=http://host.docker.internal:17897
OpenBB 侧:    HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:17897
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
UPSTREAM = ""  # 由 --upstream 参数注入; 非 SEC 目标经它转发


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


def _upstream_allowed(host: str, port: int) -> bool:
    """非 SEC 目标: 仅在配置了上游代理时放行(转给上游)。"""
    port_ok = port in (80, 443, 8080, 8443)
    return bool(UPSTREAM) and port_ok


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
        is_sec = _target_allowed(host, port)
        if is_sec:
            upstream_dst = (host, port)          # SEC: 宿主直连
        elif _upstream_allowed(host, port):
            upstream_dst = None                   # 非 SEC: 走上游代理
        else:
            self.log_line(f"DENY {host}:{port} (sec.gov direct or upstream needed)")
            self.send_error(403, "target not allowed")
            return
        try:
            if upstream_dst is not None:
                upstream = socket.create_connection(upstream_dst, timeout=15)
            else:
                # 经上游代理建立隧道(CONNECT 上游, 由上游代连目标)
                import urllib.parse
                u = urllib.parse.urlsplit(UPSTREAM)
                proxy_host, proxy_port = u.hostname, u.port or 80
                upstream = socket.create_connection((proxy_host, proxy_port), timeout=15)
                req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                       f"Host: {host}:{port}\r\n\r\n")
                upstream.sendall(req.encode("ascii"))
                # 读上游的 CONNECT 响应头
                resp = b""
                while b"\r\n\r\n" not in resp:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        raise OSError("upstream closed during CONNECT")
                    resp += chunk
                status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                if " 200 " not in status_line:
                    raise OSError(f"upstream CONNECT failed: {status_line[:80]}")
        except OSError as exc:
            self.log_line(f"DIAL-FAIL {host}:{port} {exc}")
            self.send_error(502, "upstream unreachable")
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.log_line(f"{'RELAY-direct' if is_sec else 'RELAY-upstream'} {host}:{port}")
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
    global UPSTREAM
    ap = argparse.ArgumentParser(description="SEC relay (smart: sec.gov direct, others via upstream)")
    ap.add_argument("--port", type=int, default=17897)
    ap.add_argument("--bind", default="0.0.0.0",
                    help="默认 0.0.0.0 以便容器经 host.docker.internal 访问")
    ap.add_argument("--upstream", default="",
                    help="非 SEC 流量转发的上游代理, 如 http://127.0.0.1:7897; 留空则非 SEC 一律 403")
    args = ap.parse_args()
    UPSTREAM = args.upstream.rstrip("/") if args.upstream else ""
    with RelayServer((args.bind, args.port), RelayHandler) as srv:
        print(f"sec_relay listening on {args.bind}:{args.port} "
              f"(sec.gov -> direct; upstream={UPSTREAM or 'OFF(non-sec 403)'}; "
              f"idle-timeout {IDLE_TIMEOUT_S:.0f}s)",
              flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("bye", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
