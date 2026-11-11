"""本地示例站点服务器（仅依赖 Python 标准库）。

特性：
- 正常提供 example-site/site 下的静态页面；
- 可通过 /set-variant?variant=b 切换到“静默改写后”的内容，
  此时同一个 /detail/notice_XXX.html 会返回 site_variant_b 中的页面；
- /control.html 提供切换按钮，/variant 返回当前版本 JSON。
"""
from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
SITE = BASE / "site"
VARIANT_B = BASE / "site_variant_b"

_state_lock = threading.Lock()
_current_variant = "a"


def get_variant() -> str:
    with _state_lock:
        return _current_variant


def set_variant(value: str) -> str:
    global _current_variant
    value = (value or "").strip().lower()
    if value not in {"a", "b"}:
        value = "a"
    with _state_lock:
        _current_variant = value
    return value


class ExampleSiteHandler(BaseHTTPRequestHandler):
    server_version = "ExampleTenderSite/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[example-site] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _safe_path(self, url_path: str, variant: str) -> Path | None:
        rel = unquote(url_path).lstrip("/") or "index.html"
        # 允许 /detail/notice_001.html 这一类路径，拒绝目录穿越。
        parts = Path(rel).parts
        if any(part in {"..", "."} for part in parts):
            return None
        candidate = SITE / rel
        if variant == "b" and rel.startswith("detail/"):
            alt = VARIANT_B / rel
            if alt.is_file():
                return alt
        return candidate if candidate.is_file() else None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/variant":
            body = json.dumps({"variant": get_variant(), "available": ["a", "b"]}).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return

        if path in {"/set-variant", "/switch", "/toggle"}:
            if path == "/toggle":
                set_variant("b" if get_variant() == "a" else "a")
            else:
                set_variant(
                    (query.get("variant") or query.get("v") or query.get("to") or ["a"])[0]
                )
            self._redirect("/control.html")
            return

        if path in {"/", "/control"}:
            path = "/control.html" if path == "/control" else "/index.html"

        target = self._safe_path(path, get_variant())
        if target is None:
            self._send(b"404 Not Found", "text/plain; charset=utf-8", status=404)
            return

        content_type, _ = mimetypes.guess_type(str(target))
        if target.suffix.lower() in {".html", ".htm"}:
            content_type = "text/html; charset=utf-8"
        elif target.suffix.lower() == ".css":
            content_type = "text/css; charset=utf-8"
        body = target.read_bytes()
        self._send(body, content_type or "application/octet-stream")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        # 控制端允许表单 POST；切换参数也可直接放在查询串中。
        if urlparse(self.path).path in {"/set-variant", "/switch", "/toggle"}:
            self.do_GET()
            return
        self._send(b"405 Method Not Allowed", "text/plain; charset=utf-8", status=405)


def main() -> None:
    # 直接运行本文件时，如果尚未生成静态页面，先自动生成一份。
    if not (SITE / "index.html").is_file() or not (VARIANT_B / "detail").is_dir():
        import generate_site

        generate_site.main()
    port = int(os.environ.get("PORT", "80"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), ExampleSiteHandler)
    print(f"示例站点已启动：http://{host}:{port}/ （控制台 /control.html）", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
