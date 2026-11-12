"""Minimal fake of the LightBurn 2.2 REST API, for tests only.

Implements: POST /api/connect, GET /api/units, GET /api/material-library, GET /api/project,
GET /api/events (SSE), POST /api/file/open, POST /api/file/upload. Tokens are checked the way
LightBurn documents them: hex(HMAC-SHA256(secret, floor(unix_time / 60))), current or previous
minute.
"""
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLightBurn(object):
    def __init__(self, approve=True, units=None, library=None, project=None,
                 import_result=None, send_events=True, html_on_upload=False):
        self.approve = approve
        self.units = units or {"units": "mm/s", "distance_units": "mm"}
        self.library = library if library is not None else {"product": "lightburn", "name": "Lib",
                                                            "filename": "x.clb", "entries": []}
        self.project = project or {"filename": "", "modified": False, "shape_count": 0}
        self.import_result = import_result or {"success": True}
        self.send_events = send_events
        self.html_on_upload = html_on_upload   # another web server on the port
        self.secrets = []
        self.received = []        # (path, headers dict, body bytes)
        self.pairings = 0
        self._subscribers = []
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                h = self.headers.get("Authorization", "")
                if not h.startswith("Bearer "):
                    return False
                tok = h[7:]
                now = int(time.time()) // 60
                for s in fake.secrets:
                    for m in (now, now - 1):
                        good = hmac.new(s.encode(), str(m).encode(), hashlib.sha256).hexdigest()
                        if hmac.compare_digest(good, tok):
                            return True
                return False

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):
                body = self._body()
                if self.path == "/api/connect":
                    fake.pairings += 1
                    req = json.loads(body.decode("utf-8"))
                    assert req["application_name"] and req["capabilities"]
                    if not fake.approve:
                        return self._json(403, {"error": "Declined"})
                    secret = "secret%d" % fake.pairings
                    fake.secrets.append(secret)
                    return self._json(200, {"status": "ok", "secret": secret})
                if not self._authorized():
                    return self._json(401, {"error": "Unauthorized"})
                if self.path in ("/api/file/open", "/api/file/upload"):
                    fake.received.append((self.path, dict(self.headers), body))
                    if fake.html_on_upload:
                        data = b"<html>hello</html>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    self._json(202, {"status": "accepted"})
                    if fake.send_events:
                        threading.Timer(0.2, fake._emit, args=(
                            "file_imported", fake.import_result)).start()
                    return
                return self._json(404, {"error": "Not found"})

            def do_GET(self):
                if not self._authorized():
                    return self._json(401, {"error": "Unauthorized"})
                if self.path == "/api/units":
                    return self._json(200, fake.units)
                if self.path == "/api/material-library":
                    return self._json(200, fake.library)
                if self.path == "/api/project":
                    return self._json(200, fake.project)
                if self.path == "/api/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b":connected\n\n")
                    self.wfile.flush()
                    ev = threading.Event()
                    box = []
                    with fake._lock:
                        fake._subscribers.append((ev, box))
                    if ev.wait(10):
                        name, data = box[0]
                        try:
                            self.wfile.write(("event: %s\ndata: %s\n\n" % (
                                name, json.dumps(data))).encode("utf-8"))
                            self.wfile.flush()
                        except OSError:
                            pass
                    self.close_connection = True
                    return
                return self._json(404, {"error": "Not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _emit(self, name, data):
        with self._lock:
            subs, self._subscribers = self._subscribers, []
        for ev, box in subs:
            box.append((name, data))
            ev.set()

    def revoke_all(self):
        self.secrets = []

    def close(self):
        self.server.shutdown()
        self.server.server_close()
