"""A git server over HTTPS for the compose end-to-end test. Not for other use.

It serves the bare repositories under ``GIT_PROJECT_ROOT`` with git's own
smart HTTP CGI program, ``git http-backend``, behind the standard library
HTTP server and TLS. It has no authentication, and it accepts a push to
any repository that has ``http.receivepack`` set.

    python3 e2e_gitserver.py <port> <cert.pem> <key.pem>

``e2e_compose.sh`` runs it in a container. It needs Python 3 and git only.
"""

from __future__ import annotations

import os
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PROJECT_ROOT = os.environ.get("GIT_PROJECT_ROOT", "/srv/git")


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """The request body, from Content-Length or from chunked transfer coding."""
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        body = b""
        while True:
            size = int(handler.rfile.readline().split(b";")[0].strip(), 16)
            if size == 0:
                handler.rfile.readline()
                return body
            body += handler.rfile.read(size)
            handler.rfile.readline()
    return handler.rfile.read(int(handler.headers.get("Content-Length") or 0))


class GitHandler(BaseHTTPRequestHandler):
    def _cgi(self) -> None:
        parts = urlsplit(self.path)
        body = read_body(self) if self.command == "POST" else b""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": PROJECT_ROOT,
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": parts.path,
            "QUERY_STRING": parts.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": self.client_address[0],
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
        for name, value in self.headers.items():
            key = "HTTP_" + name.upper().replace("-", "_")
            if key not in ("HTTP_CONTENT_TYPE", "HTTP_CONTENT_LENGTH"):
                env[key] = value
        done = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
        )
        if done.stderr:
            sys.stderr.write(done.stderr.decode("utf-8", "replace"))
        if done.returncode != 0 and not done.stdout:
            self.send_error(502, "git http-backend failed")
            return
        head, _, payload = done.stdout.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode("latin-1").split("\r\n"):
            name, sep, value = line.partition(":")
            if not sep:
                continue
            if name.lower() == "status":
                status = int(value.strip().split(" ")[0])
            else:
                headers.append((name.strip(), value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _cgi
    do_POST = _cgi


def main() -> None:
    port, cert, key = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    server = ThreadingHTTPServer(("0.0.0.0", port), GitHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(f"git server on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
