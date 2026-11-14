"""Run history: records the workflow events and exports a detailed report.

Hierarchy:  phone -> IMO app (clone) -> account (name + phone) -> chat

The recorder listens to the same log events the dashboard uses, so nothing
in the workflow has to be changed to feed it. After a run, `offer_export`
asks whether to download the data, which format, and which folder. Files are
named after the date and time of the run, e.g. imo-run_2026-10-27_14-32-05.txt
"""

from __future__ import annotations

import csv
import json
import sys
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

UNKNOWN_ACCOUNT = "(active account)"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _new_dev() -> dict:
    return {"status": "idle", "error": None, "apps": OrderedDict(), "app": None, "acc": None, "chat": None}


def _new_app() -> dict:
    return {"done": False, "skipped": False, "found": [], "accounts": []}


def _get_acc(app: dict, name: str | None, phone: str | None) -> dict:
    name = (name or "").strip() or UNKNOWN_ACCOUNT
    phone = (phone or "").strip()
    for acc in app["accounts"]:
        if phone and acc["phone"] == phone:
            return acc
        if not phone and not acc["phone"] and acc["name"] == name:
            return acc
    acc = {"name": name, "phone": phone, "finished": False, "failed": False, "chats": []}
    app["accounts"].append(acc)
    return acc


class RunRecorder:
    """Collects the run's hierarchy from log events (thread-safe)."""

    def __init__(self, serials: list[str], device_info: dict | None = None):
        self._lock = threading.Lock()
        self._dev: dict[str, dict] = {s: _new_dev() for s in serials}
        self.device_info = device_info or {}
        self.started = datetime.now()
        self.finished: datetime | None = None

    def finish(self) -> None:
        self.finished = datetime.now()

    def wrap(self, inner):
        """Wrap a log function: record the event, then pass it on unchanged."""

        def log(device_id, message, level="info", meta=None):
            try:
                self._record(device_id, message, meta)
            except Exception:
                pass  # recording must never break a run
            inner(device_id, message, level, meta)

        return log

    def _record(self, device_id: str, message: str, meta: Any) -> None:
        event = meta.get("event") if isinstance(meta, dict) else None
        if not event:
            return
        with self._lock:
            dev = self._dev.setdefault(device_id, _new_dev())

            if event == "device_start":
                dev = self._dev[device_id] = _new_dev()
                dev["status"] = "running"
                return
            if event == "device_done":
                dev["status"] = "success"
                dev["error"] = None
                return
            if event == "device_error":
                cleaned = message.replace("Error: ", "", 1)
                dev["status"] = "stopped" if "disconnected by user" in cleaned.lower() else "error"
                dev["error"] = cleaned
                return

            if event in ("imo_instance_start", "imo_instance_skipped", "imo_instance_done"):
                label = meta.get("imoLabel") or "imo"
                app = dev["apps"].setdefault(label, _new_app())
                dev["app"], dev["acc"], dev["chat"] = app, None, None
                if event == "imo_instance_skipped" or meta.get("skipped"):
                    app["skipped"] = True
                if event == "imo_instance_done":
                    app["done"] = True
                return

            app = dev["app"]
            if app is None:
                app = dev["apps"].setdefault("imo", _new_app())
                dev["app"] = app

            if event == "accounts_snapshot":
                found = meta.get("accounts")
                if not found:
                    found = [{"phone": k, "name": k} for k in meta.get("keys", [])]
                app["found"] = [{"phone": f.get("phone") or "", "name": f.get("name") or ""} for f in found]
            elif event in ("account_switch", "account_active", "account_failed"):
                acc = _get_acc(app, meta.get("accountName"), meta.get("accountPhone"))
                dev["acc"], dev["chat"] = acc, None
                if event == "account_active":
                    acc["finished"] = True
                elif event == "account_failed":
                    acc["failed"] = True
                    if meta.get("reason"):
                        acc["reason"] = meta["reason"]
            elif event == "chat_open":
                acc = dev["acc"] or _get_acc(app, None, None)
                dev["acc"] = acc
                chat = {"name": meta.get("chatName") or f"chat_{len(acc['chats']) + 1}",
                        "liked": 0, "alreadyLiked": 0, "failed": 0}
                acc["chats"].append(chat)
                dev["chat"] = chat
            elif event in ("video_liked", "video_already_liked", "video_like_failed"):
                chat = dev["chat"]
                if chat is not None:
                    key = {"video_liked": "liked", "video_already_liked": "alreadyLiked",
                           "video_like_failed": "failed"}[event]
                    chat[key] += 1

    # -- report -------------------------------------------------------------

    def build_report(self, results: list[dict] | None = None) -> dict:
        finished = self.finished or datetime.now()
        res = {r["deviceId"]: r for r in (results or []) if isinstance(r, dict) and r.get("deviceId")}
        devices = []
        with self._lock:
            items = list(self._dev.items())
            for serial, dev in items:
                info = self.device_info.get(serial)
                apps = []
                for label, app in dev["apps"].items():
                    accounts = []
                    for acc in app["accounts"]:
                        chats = [dict(c) for c in acc["chats"]]
                        if acc["failed"]:
                            status = "not logged in" if acc.get("reason") == "not_ready" else "failed"
                        else:
                            status = "finished" if acc["finished"] else "incomplete"
                        accounts.append({
                            "name": acc["name"], "phone": acc["phone"], "status": status,
                            "chats": chats, "totals": _chat_totals(chats),
                        })
                    for f in app["found"]:
                        ph, nm = f.get("phone") or "", f.get("name") or ""
                        if any((ph and a["phone"] == ph) or (not ph and a["name"] == nm) for a in accounts):
                            continue
                        accounts.append({"name": nm or ph or UNKNOWN_ACCOUNT, "phone": ph,
                                         "status": "not processed", "chats": [], "totals": _chat_totals([])})
                    app_status = "skipped" if app["skipped"] else ("done" if app["done"] else "incomplete")
                    apps.append({
                        "label": label, "status": app_status, "accounts": accounts,
                        "totals": _app_totals(accounts),
                    })
                r = res.get(serial, {})
                error = dev["error"] or r.get("error")
                devices.append({
                    "serial": serial,
                    "manufacturer": getattr(info, "manufacturer", None) if info else None,
                    "model": getattr(info, "model", None) if info else None,
                    "androidVersion": getattr(info, "androidVersion", None) if info else None,
                    "status": dev["status"],
                    "error": error,
                    "imoApps": apps,
                    "totals": _device_totals(apps),
                })
        return {
            "generatedAt": finished.isoformat(timespec="seconds"),
            "run": {
                "startedAt": self.started.isoformat(timespec="seconds"),
                "finishedAt": finished.isoformat(timespec="seconds"),
                "durationSeconds": int((finished - self.started).total_seconds()),
            },
            "summary": _overall_totals(devices),
            "devices": devices,
        }


def _chat_totals(chats: list[dict]) -> dict:
    return {
        "chats": len(chats),
        "liked": sum(c["liked"] for c in chats),
        "alreadyLiked": sum(c["alreadyLiked"] for c in chats),
        "failed": sum(c["failed"] for c in chats),
    }


def _app_totals(accounts: list[dict]) -> dict:
    t = {"accounts": len(accounts),
         "accountsFinished": sum(1 for a in accounts if a["status"] == "finished"),
         "chats": 0, "liked": 0, "alreadyLiked": 0, "failed": 0}
    for a in accounts:
        for k in ("chats", "liked", "alreadyLiked", "failed"):
            t[k] += a["totals"][k]
    return t


def _device_totals(apps: list[dict]) -> dict:
    t = {"imoApps": len(apps),
         "imoAppsSkipped": sum(1 for a in apps if a["status"] == "skipped"),
         "accounts": 0, "accountsFinished": 0, "chats": 0, "liked": 0, "alreadyLiked": 0, "failed": 0}
    for a in apps:
        for k in ("accounts", "accountsFinished", "chats", "liked", "alreadyLiked", "failed"):
            t[k] += a["totals"][k]
    return t


def _overall_totals(devices: list[dict]) -> dict:
    t = {"devices": len(devices), "imoApps": 0, "imoAppsSkipped": 0, "accounts": 0,
         "accountsFinished": 0, "chats": 0, "liked": 0, "alreadyLiked": 0, "failed": 0}
    for d in devices:
        for k in ("imoApps", "imoAppsSkipped", "accounts", "accountsFinished",
                  "chats", "liked", "alreadyLiked", "failed"):
            t[k] += d["totals"][k]
    return t


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _line(t: dict) -> str:
    return (f"chats {t['chats']} | liked {t['liked']} | "
            f"already liked {t['alreadyLiked']} | failed {t['failed']}")


def render_text(report: dict) -> str:
    out: list[str] = []
    run, s = report["run"], report["summary"]
    out.append("IMO RUN REPORT")
    out.append("=" * 70)
    out.append(f"Started   : {run['startedAt'].replace('T', ' ')}")
    out.append(f"Finished  : {run['finishedAt'].replace('T', ' ')}")
    out.append(f"Duration  : {_hms(run['durationSeconds'])}")
    out.append("")
    out.append("OVERVIEW")
    out.append("-" * 70)
    out.append(f"Phones           : {s['devices']}")
    out.append(f"IMO apps         : {s['imoApps']}  (skipped: {s['imoAppsSkipped']})")
    out.append(f"Accounts         : {s['accounts']}  (finished: {s['accountsFinished']})")
    out.append(f"Chats            : {s['chats']}")
    out.append(f"Liked            : {s['liked']}")
    out.append(f"Already liked    : {s['alreadyLiked']}")
    out.append(f"Failed           : {s['failed']}")

    for i, dev in enumerate(report["devices"], 1):
        t = dev["totals"]
        maker = (dev.get("manufacturer") or "").strip()
        model = (dev.get("model") or "").strip()
        name = " ".join(x for x in (maker, model) if x) or "Unknown phone"
        out.append("")
        out.append("=" * 70)
        out.append(f"PHONE {i}: {name}")
        out.append(f"  Serial        : {dev['serial']}")
        if dev.get("androidVersion"):
            out.append(f"  Android       : {dev['androidVersion']}")
        out.append(f"  Status        : {dev['status']}")
        if dev.get("error"):
            out.append(f"  Error         : {dev['error']}")
        out.append(f"  IMO apps      : {t['imoApps']}  (skipped: {t['imoAppsSkipped']})")
        out.append(f"  Accounts      : {t['accounts']}  (finished: {t['accountsFinished']})")
        out.append(f"  Totals        : {_line(t)}")

        for app in dev["imoApps"]:
            at = app["totals"]
            out.append("")
            out.append(f"  IMO APP {app['label']}  [{app['status']}]")
            out.append(f"    Accounts: {at['accounts']} (finished {at['accountsFinished']}) | {_line(at)}")
            if not app["accounts"]:
                out.append("    (no accounts processed)")
            for acc in app["accounts"]:
                phone = f" (phone {acc['phone']})" if acc["phone"] else ""
                out.append(f"    - Account: {acc['name']}{phone}  [{acc['status']}]")
                out.append(f"        {_line(acc['totals'])}")
                for c in acc["chats"]:
                    out.append(f"        * Chat \"{c['name']}\": liked {c['liked']}, "
                               f"already liked {c['alreadyLiked']}, failed {c['failed']}")
    out.append("")
    return "\n".join(out)


def render_csv_rows(report: dict) -> list[list[Any]]:
    header = ["phone_serial", "phone_maker", "phone_model", "android", "phone_status",
              "imo_app", "imo_status", "account_name", "account_phone", "account_status",
              "chat_name", "liked", "already_liked", "failed"]
    rows: list[list[Any]] = [header]
    for dev in report["devices"]:
        base = [dev["serial"], dev.get("manufacturer") or "", dev.get("model") or "",
                dev.get("androidVersion") or "", dev["status"]]
        if not dev["imoApps"]:
            rows.append(base + [""] * 9)
        for app in dev["imoApps"]:
            abase = base + [app["label"], app["status"]]
            if not app["accounts"]:
                rows.append(abase + [""] * 7)
            for acc in app["accounts"]:
                cbase = abase + [acc["name"], acc["phone"], acc["status"]]
                if not acc["chats"]:
                    rows.append(cbase + ["", 0, 0, 0])
                for c in acc["chats"]:
                    rows.append(cbase + [c["name"], c["liked"], c["alreadyLiked"], c["failed"]])
    return rows


# ---------------------------------------------------------------------------
# Saving + the question at the end of a run
# ---------------------------------------------------------------------------

def save_report(report: dict, folder: Path, fmt: str, stamp: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    formats = ["txt", "json", "csv"] if fmt == "all" else [fmt]
    paths: list[Path] = []
    for ext in formats:
        path = folder / f"imo-run_{stamp}.{ext}"
        n = 1
        while path.exists():
            path = folder / f"imo-run_{stamp}_{n}.{ext}"
            n += 1
        if ext == "txt":
            path.write_text(render_text(report), encoding="utf-8")
        elif ext == "json":
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            # utf-8-sig so Excel shows Bangla / emoji names correctly
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                csv.writer(fh).writerows(render_csv_rows(report))
        paths.append(path)
    return paths


def offer_export(console, recorder: RunRecorder, results: list[dict] | None) -> None:
    """Ask: download the run data? y -> choose format and folder; n -> exit."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        console.print()
        if not typer.confirm("Do you want to download the run data (phones > IMO apps > accounts > chats)?",
                             default=False):
            return
        fmt = ""
        while fmt not in ("txt", "json", "csv", "all"):
            fmt = typer.prompt("File format (txt / json / csv / all)", default="txt").strip().lower()

        downloads = Path.home() / "Downloads"
        default_dir = downloads if downloads.is_dir() else Path.cwd()
        report = recorder.build_report(results)
        stamp = (recorder.finished or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")

        for _ in range(3):
            raw = typer.prompt("Folder to save the file(s) in", default=str(default_dir)).strip()
            folder = Path(raw).expanduser()
            try:
                paths = save_report(report, folder, fmt, stamp)
            except OSError as e:
                console.print(f"[red]Could not write to {folder}: {e}[/red]")
                continue
            for p in paths:
                console.print(f"[green]Saved:[/green] {p}")
            return
        console.print("[yellow]No file saved.[/yellow]")
    except (typer.Abort, KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Export cancelled.[/yellow]")
