"""本机 HTTP 服务（只绑 127.0.0.1）：

  GET  /                主界面（注入一次性 token）
  GET  /overlay         字幕条
  GET  /ui/<file>       静态资源（CSS/JS/图标）
  GET  /events          SSE：事件批 + 电平 + 计时，~12 次/秒
  POST /api/<method>    JSON API（需 X-LT-Token 头；自定义头会触发 CORS 预检，网页无法跨站调用）
  GET  /media/<id>/...  记录里的 wav（支持 Range，WebKit 播放/拖动进度需要）

Token 只出现在本进程渲染的页面里，别的本机网页拿不到，也就调不了 API、听不了录音。
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import records
from .api import Api
from .config import CFG

UI_DIR = Path(__file__).parent / "ui"
_MEDIA_RE = re.compile(r"^/media/([0-9-]+)/(audio/[A-Za-z0-9_.-]+\.wav)$")


class AppServer:
    def __init__(self, api: Api, port: int = 0):
        self.api = api
        self.token = secrets.token_urlsafe(18)
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def overlay_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/overlay"

    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="lt-http", daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()


def _make_handler(app: AppServer):
    api = app.api
    engine = api.engine

    class H(BaseHTTPRequestHandler):
        server_version = "LiveTranslator"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # 安静
            pass

        # ------------------------------------------------------------ 工具
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """防 DNS rebinding：只认 127.0.0.1 / localhost 的 Host；带 Origin 的请求也必须同源。"""
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{app.port}", f"localhost:{app.port}"):
                return False
            origin = self.headers.get("Origin")
            return origin in (None, "null", f"http://127.0.0.1:{app.port}", f"http://localhost:{app.port}")

        def _token_ok(self, qs: dict) -> bool:
            tok = self.headers.get("X-LT-Token") or (qs.get("token") or [""])[0]
            return secrets.compare_digest(tok, app.token)

        def _page(self, name: str):
            html = (UI_DIR / name).read_text(encoding="utf-8")
            html = html.replace("__LT_TOKEN__", app.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                       {"Content-Security-Policy":
                        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                        "script-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:; "
                        "connect-src 'self'"})

        # ------------------------------------------------------------ GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            path = unquote(u.path)
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            try:
                if path in ("/", "/index.html"):
                    return self._page("index.html")
                if path == "/overlay":
                    return self._page("overlay.html")
                if path.startswith("/ui/"):
                    return self._static(path[4:])
                if path == "/events":
                    if not self._token_ok(qs):
                        return self._json(403, {"error": "forbidden"})
                    return self._sse((qs.get("client") or ["main"])[0])
                m = _MEDIA_RE.match(path)
                if m:
                    if not self._token_ok(qs):
                        return self._json(403, {"error": "forbidden"})
                    return self._media(m.group(1), m.group(2))
                self._json(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _static(self, rel: str):
            f = (UI_DIR / rel).resolve()
            if UI_DIR.resolve() not in f.parents or not f.is_file():
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, f.read_bytes(), ctype)

        def _media(self, rec_id: str, rel: str):
            try:
                d = records.record_dir(CFG["output_dir"], rec_id)
            except FileNotFoundError:
                return self._json(404, {"error": "not found"})
            f = d / rel
            if not f.is_file():
                return self._json(404, {"error": "not found"})
            size = f.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            code = 200
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                try:
                    if a:
                        start = int(a)
                        end = int(b) if b else size - 1
                    else:                          # bytes=-N：最后 N 字节
                        start = max(0, size - int(b))
                    end = min(end, size - 1)
                    code = 206
                except ValueError:
                    start, end = 0, size - 1
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with f.open("rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _sse(self, client: str):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            sub = engine.hub.subscribe(client)
            try:
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                last_beat = 0.0
                while not engine.stop_flag.is_set():
                    events = sub.drain(0.08)
                    now = time.time()
                    s = engine.session
                    payload = {"events": events, "levels": engine.levels(),
                               "elapsed": s.elapsed() if s else None,
                               "pending": s.pending if s else 0}
                    if events or now - last_beat > 0.08:
                        last_beat = now
                        data = json.dumps(payload, ensure_ascii=False, default=str)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                engine.hub.unsubscribe(sub)

        # ------------------------------------------------------------ POST /api
        def do_POST(self):
            u = urlparse(self.path)
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if not u.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._token_ok({}):
                return self._json(403, {"error": "forbidden"})
            name = u.path[5:]
            if name not in Api.EXPOSED:
                return self._json(404, {"error": f"no method {name}"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if not isinstance(body, dict):
                    body = {}
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "bad json"})
            try:
                result = getattr(api, name)(**body)
                self._json(200, {"ok": True, "result": result})
            except FileNotFoundError:
                self._json(200, {"ok": False, "error": "记录不存在或已被删除"})
            except TypeError as e:
                self._json(400, {"ok": False, "error": f"参数错误: {e}"})
            except Exception as e:
                traceback.print_exc()
                self._json(200, {"ok": False, "error": str(e)})

        def do_OPTIONS(self):                 # 不放行任何跨域预检
            self._send(403, b"", "text/plain")

    return H
