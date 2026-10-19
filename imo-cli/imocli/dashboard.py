"""Live Rich dashboard: clean per-device status cards + timestamped log.

Recreates the browser UI (ConnectedDevicesPanel + LiveLogPanel) in the
terminal. Uses the exact same event protocol as the old frontend reducer, so
the numbers shown here match what the Next.js UI would have shown.

Layout (a simple vertical stack — no nested Layouts, so it renders cleanly and
can never collapse on a small terminal):

    imo-cli · concurrent IMO workflow   00:42   devices 1/2 done · liked 37 · failed 0
    ┌─ df94dc73 [● running] ────────────────────────────────┐
    │  1 imo · 2 accounts · 3 chats · 37 liked · 0 failed   │
    │  ▸ imo(1)                                             │
    │    · Alice — 2 chats · 35 liked                       │
    │  now: Chat "টেস্টিং গ্রুপ" opened                     │
    └───────────────────────────────────────────────────────┘
    ┌─ live log ────────────────────────────────────────────┐
    │ 12:03:04 df94dc73: Chat opened …                      │
    └───────────────────────────────────────────────────────┘
    running…  ·  Ctrl+C to stop  ·  imo disconnect <serial>
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .logs import LogFn

LEVEL_STYLE = {"info": "cyan", "success": "green", "error": "red", "warn": "yellow"}
STATUS_STYLE = {
    "running": "yellow",
    "success": "green",
    "error": "red",
    "stopped": "magenta",
    "idle": "white",
}
STATUS_BADGE = {
    "running": "● running",
    "success": "✓ done",
    "error": "✗ error",
    "stopped": "◼ stopped",
    "idle": "○ idle",
}

_LOG_HEIGHT = 12
_MAX_LOGS = 250


def reduce_device(prev: dict, message: str, level: str, meta: Any) -> dict:
    """Port of the frontend reducer — builds the device state hierarchy."""
    d = dict(prev)
    event = (meta or {}).get("event") if meta else None
    if not event:
        return d

    PENDING = "__pending__"

    if event == "device_start":
        return {"status": "running", "imoInstances": OrderedDict()}

    if event == "device_done":
        d["status"] = "success"
        d.pop("error", None)
        return d

    if event == "device_error":
        cleaned = message.replace("Error: ", "", 1)
        d["status"] = "stopped" if "disconnected by user" in cleaned.lower() else "error"
        d["error"] = cleaned
        return d

    if event in ("imo_instance_start", "imo_instance_done"):
        label = (meta or {}).get("imoLabel") or "imo"
        inst = d.setdefault("imoInstances", OrderedDict()).get(label)
        if inst is None:
            inst = {"done": False, "accounts": OrderedDict()}
            d["imoInstances"][label] = inst
        if event == "imo_instance_done":
            inst["done"] = True
        d["currentImoLabel"] = label
        d["currentAccountKey"] = PENDING
        return d

    if event == "account_active":
        label = d.get("currentImoLabel") or "imo"
        inst = d.setdefault("imoInstances", OrderedDict()).get(label)
        if inst is None:
            inst = {"done": False, "accounts": OrderedDict()}
            d["imoInstances"][label] = inst
        name = (meta or {}).get("accountName")
        if not name:
            return d
        if PENDING in inst["accounts"]:
            inst["accounts"][name] = inst["accounts"].pop(PENDING)
        else:
            inst["accounts"].setdefault(name, {"resolved": True, "chats": OrderedDict()})
        return d

    if event == "account_switch":
        label = d.get("currentImoLabel") or "imo"
        inst = d.setdefault("imoInstances", OrderedDict()).get(label)
        if inst is None:
            inst = {"done": False, "accounts": OrderedDict()}
            d["imoInstances"][label] = inst
        name = (meta or {}).get("accountName") or "unknown account"
        inst["accounts"].setdefault(name, {"resolved": True, "chats": OrderedDict()})
        d["currentAccountKey"] = name
        return d

    if event == "chat_open":
        label = d.get("currentImoLabel") or "imo"
        inst = d.setdefault("imoInstances", OrderedDict()).get(label)
        if inst is None:
            inst = {"done": False, "accounts": OrderedDict()}
            d["imoInstances"][label] = inst
        acc_key = d.get("currentAccountKey") or PENDING
        acc = inst["accounts"].setdefault(acc_key, {"resolved": acc_key != PENDING, "chats": OrderedDict()})
        chat_name = (meta or {}).get("chatName") or f"chat_{len(acc['chats']) + 1}"
        acc["chats"].setdefault(chat_name, {"videosLiked": 0, "videosFailed": 0})
        return d

    if event in ("video_liked", "video_like_failed"):
        label = d.get("currentImoLabel")
        inst = (d.get("imoInstances") or {}).get(label) if label else None
        if not inst:
            return d
        acc_key = d.get("currentAccountKey") or PENDING
        acc = inst.get("accounts", {}).get(acc_key)
        if not acc:
            return d
        chat_keys = list(acc.get("chats", {}).keys())
        if not chat_keys:
            return d
        last_chat = acc["chats"][chat_keys[-1]]
        if event == "video_liked":
            last_chat["videosLiked"] = last_chat.get("videosLiked", 0) + 1
        else:
            last_chat["videosFailed"] = last_chat.get("videosFailed", 0) + 1
        return d

    return d


def _device_totals(state: dict) -> dict:
    totals = {"imo": 0, "accounts": 0, "chats": 0, "liked": 0, "failed": 0}
    for inst in state.get("imoInstances", {}).values():
        totals["imo"] += 1
        for acc in inst.get("accounts", {}).values():
            totals["accounts"] += 1
            for chat in acc.get("chats", {}).values():
                totals["chats"] += 1
                totals["liked"] += chat.get("videosLiked", 0)
                totals["failed"] += chat.get("videosFailed", 0)
    return totals


class Dashboard:
    def __init__(self, devices: list[dict]):
        self._devices = {d["serial"]: {"status": "idle", "imoInstances": OrderedDict()} for d in devices}
        self._logs: list[tuple[float, str, str, str]] = []  # (time, deviceId, message, level)
        self._log_lock = threading.Lock()
        self._finished = threading.Event()
        self._start = time.monotonic()

    # -- event plumbing -----------------------------------------------------

    def make_log(self) -> LogFn:
        def log(device_id: str, message: str, level: str = "info", meta: Any = None):
            with self._log_lock:
                self._logs.append((time.monotonic(), device_id, message, level))
                if len(self._logs) > _MAX_LOGS:
                    self._logs = self._logs[-_MAX_LOGS:]
                prev = self._devices.get(device_id, {"status": "idle", "imoInstances": OrderedDict()})
                self._devices[device_id] = reduce_device(prev, message, level, meta)
        return log

    def mark_done(self):
        self._finished.set()

    def _write_checkpoint_log(self, payload: dict) -> None:
        """NDJSON-emitter shim so checkpoint() recovery lines reach the live
        log panel without going through the Dashboard's log function (which
        checkpoint() doesn't have access to)."""
        device_id = payload.get("deviceId", "system")
        message = payload.get("message", "")
        level = payload.get("level", "info")
        with self._log_lock:
            self._logs.append((time.monotonic(), device_id, message, level))
            if len(self._logs) > _MAX_LOGS:
                self._logs = self._logs[-_MAX_LOGS:]

    def device_totals(self, serial: str) -> dict:
        return _device_totals(self._devices.get(serial, {}))

    # -- rendering ----------------------------------------------------------

    def _last_log_for(self, serial: str) -> str | None:
        with self._log_lock:
            for ts, device_id, message, _level in reversed(self._logs):
                if device_id == serial:
                    return message
        return None

    def _device_panel(self, serial: str) -> Panel:
        state = self._devices.get(serial, {"status": "idle", "imoInstances": OrderedDict()})
        status = state.get("status", "idle")
        style = STATUS_STYLE.get(status, "white")
        badge = STATUS_BADGE.get(status, status)

        title = Text(f" {serial}  ", style=f"bold {style}")
        title.append(f"[{badge}]", style=f"dim {style}")

        totals = _device_totals(state)
        t = Text("  ", style="dim")
        t.append(f"{totals['imo']} imo · {totals['accounts']} accounts · {totals['chats']} chats · ", style="dim")
        t.append(f"{totals['liked']} liked", style="green")
        t.append(" · ", style="dim")
        t.append(f"{totals['failed']} failed", style="red" if totals["failed"] else "dim")

        lines: list[RenderableType] = [t]

        if status == "error" and state.get("error"):
            lines.append(Text(f"  ✗ {state['error']}", style="red"))

        for label, inst in state.get("imoInstances", {}).items():
            done = inst.get("done", False)
            marker = "✓" if done else "▸"
            lines.append(Text(f"  {marker} {label}{'  [done]' if done else ''}", style="green" if done else "yellow"))
            for acc_name, acc in inst.get("accounts", {}).items():
                if acc_name == "__pending__":
                    continue  # buffered placeholder, renamed on account_active
                chats = acc.get("chats", {})
                acc_liked = sum(c.get("videosLiked", 0) for c in chats.values())
                lines.append(
                    Text(f"    · {acc_name} — {len(chats)} chats · {acc_liked} liked", style="cyan")
                )

        current = self._last_log_for(serial)
        if current:
            lines.append(Text(f"  now: {current}", style="bright_black"))

        return Panel(Group(*lines), title=title, border_style=style, box=box.ROUNDED, padding=(0, 1))

    def _log_panel(self) -> Panel:
        with self._log_lock:
            # Show the most recent lines — the ones that explain what's
            # happening *now* — instead of clipping them off the bottom.
            logs = list(self._logs)[-(_LOG_HEIGHT - 2):]
        text = Text()
        for ts, device_id, message, level in logs:
            style = LEVEL_STYLE.get(level, "white")
            hhmmss = time.strftime("%H:%M:%S", time.localtime(ts))
            text.append(f"{hhmmss} ", style="dim")
            text.append(f"{device_id}: ", style="bright_black")
            text.append(f"{message}\n", style=style)
        return Panel(text, title="live log", border_style="blue", box=box.ROUNDED, padding=(0, 1), height=_LOG_HEIGHT)

    def _header(self) -> Text:
        with self._log_lock:
            states = list(self._devices.values())
        running = sum(1 for s in states if s.get("status") in ("running", "idle"))
        done = sum(1 for s in states if s.get("status") == "success")
        total_liked = sum(_device_totals(s)["liked"] for s in states)
        total_failed = sum(_device_totals(s)["failed"] for s in states)
        elapsed = int(time.monotonic() - self._start)
        mm, ss = divmod(elapsed, 60)

        text = Text()
        text.append(" imo-cli ", style="bold white on blue")
        text.append("  concurrent IMO workflow", style="bold")
        text.append(f"   {mm:02d}:{ss:02d}", style="bold cyan")
        text.append(f"   devices {done}/{len(states)} done", style="green" if done == len(states) else "yellow")
        text.append(f"   liked {total_liked}", style="green")
        if total_failed:
            text.append(f"   failed {total_failed}", style="red")
        return text

    def _footer(self) -> Text:
        with self._log_lock:
            all_done = all(s.get("status") in ("success", "error", "stopped") for s in self._devices.values())
        text = Text()
        if all_done:
            text.append(" ✓ all devices finished — summary follows", style="green")
        else:
            text.append(" running…  ", style="yellow")
            text.append("Ctrl+C to stop  ·  imo disconnect <serial> to stop one device", style="dim")
        return text

    def _render(self) -> RenderableType:
        header = self._header()
        device_panels = [self._device_panel(s) for s in self._devices]
        devices = Columns(device_panels, expand=True, equal=True)
        return Group(header, "", devices, "", self._log_panel(), self._footer())

    def run(self) -> None:
        """Block until mark_done() is called, rendering live."""
        with Live(self._render(), refresh_per_second=4, screen=False) as live:
            while not self._finished.is_set():
                time.sleep(0.25)
                live.update(self._render())
