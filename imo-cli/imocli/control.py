"""Background control server (disconnect requests) + MJPEG screen mirror.

Control server: a tiny HTTP endpoint so any process (or the browser UI that
used /api/workflows/disconnect) can ask the CLI to gracefully stop one device.
Mirror server: serves each device's live screencap stream as
multipart/x-mixed-replace, exactly like /api/screen/[serial] in the original.
Both run on stdlib http.server — no extra dependencies.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from http import HTTPStatus
from urllib.parse import urlparse

from .adb import capture_frame
from .workflow import request_disconnect

MJPEG_BOUNDARY = "imo-frame"


class _DisconnectHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            payload = json.loads(body or b"{}")
            device_id = payload.get("deviceId")
        except (ValueError, json.JSONDecodeError):
            device_id = None
        if not device_id:
            self._send(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "deviceId required"})
            return
        request_disconnect(device_id)
        self._send(HTTPStatus.OK, {"ok": True, "deviceId": device_id})

    def do_GET(self):  # noqa: N802
        self._send(HTTPStatus.OK, {"ok": True, "endpoints": ["POST /disconnect"]})


def start_disconnect_server(port: int = 8770) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _DisconnectHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


# --- MJPEG mirror ----------------------------------------------------------

class _MjpegHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        serial = urlparse(self.path).path.strip("/")
        if not serial:
            self.send_error(404)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    frame = capture_frame(serial)
                except Exception:
                    time.sleep(1.0)
                    continue
                self.wfile.write(f"--{MJPEG_BOUNDARY}\r\n".encode())
                self.wfile.write(b"Content-Type: image/png\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                time.sleep(0.35)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass


def start_mirror_server(port: int = 8771) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _MjpegHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
