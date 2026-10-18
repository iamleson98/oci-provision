"""Minimal dependency-free status HTTP server (health, JSON API, HTML page)."""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>oci-github-runner</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         background:#0d1117; color:#c9d1d9; padding:24px; }
  h1 { font-size:16px; color:#58a6ff; margin:0 0 18px; letter-spacing:.5px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; max-width:900px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 14px; }
  .card .k { color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:.8px; }
  .card .v { font-size:22px; color:#58a6ff; margin-top:4px; }
  .card .v.warn { color:#d29922; } .card .v.bad { color:#f85149; } .card .v.ok { color:#3fb950; }
  table { border-collapse:collapse; margin-top:20px; max-width:900px; width:100%; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid #21262d; font-size:12px; }
  th { color:#8b949e; text-transform:uppercase; font-size:11px; letter-spacing:.8px; }
  .muted { color:#8b949e; margin-top:18px; font-size:12px; }
</style></head><body>
<h1>OCI GITHUB RUNNER &mdash; FLEET STATUS</h1>
<div class="grid">
  <div class="card"><div class="k">runners running</div><div class="v" id="r">-</div></div>
  <div class="card"><div class="k">busy</div><div class="v" id="b">-</div></div>
  <div class="card"><div class="k">desired</div><div class="v" id="d">-</div></div>
  <div class="card"><div class="k">queued jobs</div><div class="v" id="q">-</div></div>
  <div class="card"><div class="k">pool (min-max)</div><div class="v" id="p">-</div></div>
  <div class="card"><div class="k">disk free</div><div class="v" id="f">-</div></div>
</div>
<table id="events"><thead><tr><th>time (utc)</th><th>event</th></tr></thead><tbody></tbody></table>
<p class="muted">auto-refresh 5s &middot; JSON: <a href="/api/status">/api/status</a> &middot; health: /healthz</p>
<script>
fetch('/api/status' + (location.search.includes('token=') ? location.search : ''))
 .then(r => r.json())
 .then(s => {
   set('r', s.containers_running, s.containers_running > 0 ? '' : 'warn');
   set('b', s.runners_busy, s.runners_busy > 0 ? 'ok' : '');
   set('d', s.desired ?? '-');
   set('q', Object.values(s.queued || {}).reduce((a,c)=>a+c,0),
       Object.values(s.queued || {}).reduce((a,c)=>a+c,0) > 0 ? 'warn' : '');
   set('p', (s.min_runners ?? '-') + ' - ' + (s.max_runners ?? '-'));
   set('f', (s.disk_free_gb ?? '-') + ' GB', (s.disk_free_gb ?? 99) < 10 ? 'bad' : '');
   const tb = document.querySelector('#events tbody');
   tb.innerHTML = (s.events || []).map(e =>
     '<tr><td>' + (e.ts || '').replace('T',' ').slice(0,19) + '</td><td>' + e.msg + '</td></tr>').join('');
 })
 .catch(() => {});
function set(id, val, cls) { const el = document.getElementById(id); el.textContent = val;
  el.className = 'v ' + (cls || ''); }
</script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    cfg: object = None
    state: dict = {}
    idle_check: object = None  # callable -> bool

    def log_message(self, fmt, *args):  # silence default request logging
        pass

    def _authorized(self, qs: dict) -> bool:
        token = getattr(self.cfg, "status_token", "")
        if not token:
            return True
        supplied = (qs.get("token") or [""])[0] or self.headers.get("X-Status-Token", "")
        return supplied == token

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        qs = parse_qs(url.query)

        if url.path == "/healthz":
            healthy = bool(self.state.get("healthy"))
            self._send(200 if healthy else 503, b"ok\n" if healthy else b"unhealthy\n")
            return

        if url.path == "/idle-shutdown":
            # Called from localhost by the host systemd timer (burst profile).
            ready = bool(self.idle_check and self.idle_check())
            self._send(200, b"poweroff\n" if ready else b"wait\n")
            return

        if url.path in ("/", "/index.html"):
            if not self._authorized(qs):
                self._send(401, b"unauthorized\n")
                return
            self._send(200, PAGE.encode(), ctype="text/html; charset=utf-8")
            return

        if url.path == "/api/status":
            if not self._authorized(qs):
                self._send(401, b"unauthorized\n")
                return
            try:
                payload = json.dumps(self.state, default=str).encode()
            except (TypeError, ValueError):
                payload = b'{"error":"serialize failed"}'
            self._send(200, payload, ctype="application/json")
            return

        self._send(404, b"not found\n")


def start_server(cfg, state: dict, idle_check) -> tuple[ThreadingHTTPServer, threading.Thread]:
    # staticmethod() keeps callable class attributes from being re-bound to
    # the handler instance (works for both lambdas and bound methods).
    handler = type(
        "BoundHandler",
        (_Handler,),
        {"cfg": cfg, "state": state, "idle_check": staticmethod(idle_check)},
    )
    server = ThreadingHTTPServer((cfg.status_bind, cfg.status_port), handler)
    thread = threading.Thread(target=server.serve_forever, name="status-http", daemon=True)
    thread.start()
    return server, thread


def render_page() -> str:  # used by the waker variant
    return PAGE
