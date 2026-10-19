"""Log events: same NDJSON protocol as the Next.js workflow stream.

Each event is one JSON line:
  {"type":"log","deviceId":...,"message":...,"level":"info|success|error",
   "meta":{...}|null,"timestamp":ISO8601}
and the run ends with {"type":"done","results":[...]}.

`meta.event` drives the frontend state machine, so the event names and their
payload fields must stay byte-for-byte compatible with the original
`hook/Useworkflowstream.tsx` reducer.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

LogLevel = str  # "info" | "success" | "error"
LogMeta = dict | None
LogFn = Callable[[str, str, LogLevel, LogMeta], None]


def make_log(emit: Callable[[dict], None]) -> LogFn:
    """Wrap a line-emitter into a LogFn that appends structured meta."""

    def log(device_id: str, message: str, level: LogLevel = "info", meta: LogMeta = None):
        emit(
            {
                "type": "log",
                "deviceId": device_id,
                "message": message,
                "level": level,
                "meta": meta or None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    return log


class NdJsonWriter:
    """Newline-delimited JSON stream (file, stdout, or any .write() object)."""

    def __init__(self, stream=sys.stdout, live: bool = True):
        self._stream = stream
        self._live = live
        self._lock = __import__("threading").Lock()

    def emit(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._lock:
            self._stream.write(line)
            if self._live:
                self._stream.flush()

    def close(self) -> None:
        if self._live:
            try:
                self._stream.flush()
            except Exception:
                pass
