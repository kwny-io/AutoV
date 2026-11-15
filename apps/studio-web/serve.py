"""studio-web 本地开发服务器：托管前端静态文件，并把 /api/<slug> 反代到后端。

前端是原生 ES 模块，没有构建步骤。之所以要反代而不是让浏览器直连后端：48
个服务都没有装 CORS 中间件，不往生产边界里逐个加 `CORSMiddleware` 只为本地
预览。

`X-Tenant-Id` 原样转发，**不注入**默认值——漏带租户的请求应当被服务拒绝。
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLIC = HERE / "public"

# 前端用的短 slug -> 后端挂载前缀。与 backend.py 的 SLAUGS 同一套编号。
SLUG_TO_PREFIX = {
    "s01": "s01", "s02": "s02", "s03": "s03", "s04": "s04",
    "s05": "s05", "s06": "s06", "s07": "s07", "s08": "s08",
    "s09": "s09", "s10": "s10", "s11": "s11", "s12": "s12",
    "s13": "s13", "s14": "s14", "s15": "s15", "s16": "s16",
    "s17": "s17", "s18": "s18", "s19": "s19", "s20": "s20",
    "s21": "s21", "s22": "s22", "s23": "s23", "s24": "s24",
    "s25": "s25", "s26": "s26", "s27": "s27", "s28": "s28",
    "s29": "s29", "s30": "s30", "s31": "s31", "s32": "s32",
    "s33": "s33", "s34": "s34", "s35": "s35", "s36": "s36",
    "s37": "s37", "s38": "s38", "s39": "s39", "s40": "s40",
    "s41": "s41", "s42": "s42", "s43": "s43", "s44": "s44",
    "s45": "s45", "s46": "s46", "s47": "s47", "s48": "s48",
}

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _static(self, rel):
        target = (PUBLIC / rel).resolve()
        if target != PUBLIC and PUBLIC not in target.parents:
            self._json(403, {"code": "PATH_ESCAPE"})
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._json(404, {"code": "NOT_FOUND", "path": rel})
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _proxy(self, slug, rest):
        prefix = SLUG_TO_PREFIX.get(slug)
        if prefix is None:
            self._json(404, {"code": "UNKNOWN_SERVICE", "slug": slug})
            return
        port = int(os.environ.get("STUDIO_BACKEND_PORT", "8800"))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/{prefix}/{rest}", data=body,
            method=self.command)
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP:
                request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as upstream:
                self._send(upstream.status, upstream.read(),
                           upstream.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            ctype = exc.headers.get_content_type() if exc.headers else \
                "application/json"
            self._send(exc.code, exc.read(), ctype)
        except urllib.error.URLError as exc:
            self._json(502, {"code": "UPSTREAM_DOWN",
                             "detail": str(exc.reason)})

    def _dispatch(self):
        path = self.path.split("?", 1)[0]
        query = self.path[len(path):]
        if path.startswith("/api/"):
            parts = path[len("/api/"):].split("/", 1)
            self._proxy(parts[0], (parts[1] if len(parts) > 1 else "") + query)
            return
        if path == "/services.json":
            self._json(200, {"backend": os.environ.get(
                "STUDIO_BACKEND_PORT", "8800")})
            return
        self._static("index.html" if path == "/" else path.lstrip("/"))

    do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = _dispatch


def main():
    port = int(os.environ.get("STUDIO_WEB_PORT", "8080"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"studio-web  http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
