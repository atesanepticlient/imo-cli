"""ADB bridge — device discovery, app launch, screencap.

Direct port of lib/adb.ts / lib/screen-capture.ts / lib/adb-path.ts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import uiautomator2 as u2

KNOWN_APPS = {
    "facebook": ("Facebook", "com.facebook.katana"),
    "facebook_lite": ("Facebook Lite", "com.facebook.lite"),
    "messenger": ("Messenger", "com.facebook.orca"),
    "instagram": ("Instagram", "com.instagram.android"),
    "whatsapp": ("WhatsApp", "com.whatsapp"),
}

_WINDOWS_HINTS = [
    Path(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
    Path(r"C:\platform-tools\adb.exe"),
]
_UNIX_HINTS = [
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    "/usr/bin/adb",
    str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
    str(Path.home() / "Android/Sdk/platform-tools/adb"),
]


def find_adb() -> str:
    """Resolve the adb binary (ADB_PATH env -> PATH -> common install paths)."""
    env = __import__("os").environ.get("ADB_PATH")
    if env:
        return env
    found = shutil.which("adb")
    if found:
        return found
    candidates = _WINDOWS_HINTS + _UNIX_HINTS
    for c in candidates:
        expanded = c.expandvars() if isinstance(c, Path) else c
        if isinstance(c, Path) and expanded.exists():
            return str(expanded)
        if isinstance(c, str) and Path(c).exists():
            return c
    raise RuntimeError(
        "adb not found. Install Android platform-tools (or set ADB_PATH)."
    )


ADB_PATH = find_adb()


def adb(args: list[str], serial: str | None = None, timeout: float = 8.0) -> str:
    full = (["-s", serial] if serial else []) + args
    try:
        out = subprocess.run(
            [ADB_PATH, *full],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb {' '.join(args)} timed out") from None
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "adb command failed").strip())
    return out.stdout.strip()


def open_app(serial: str, pkg: str) -> None:
    """Launch an app by package via monkey (works without knowing the activity)."""
    try:
        out = adb(
            ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
            serial,
            timeout=15.0,
        )
    except RuntimeError as e:
        if "timed out" in str(e) or is_package_installed(serial, pkg):
            first = next((ln.strip() for ln in str(e).splitlines() if ln.strip()), "monkey failed")
            raise RuntimeError(first[:120]) from None
        raise RuntimeError(f'"{pkg}" isn\'t installed on this device') from None
    if not re.search(r"Events injected:\s*1", out, re.IGNORECASE):
        if "No activities found" in out or not is_package_installed(serial, pkg):
            raise RuntimeError(f'"{pkg}" isn\'t installed on this device')
        # monkey can dump its whole usage/arg list on failure; keep one short line.
        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        raise RuntimeError((first[:120] + " ...") if len(first) > 120 else first or "monkey command produced no confirmation")


def is_package_installed(serial: str, pkg: str) -> bool:
    """Best-effort package check without selecting an Android user/profile."""
    for args in (["shell", "pm", "path", pkg], ["shell", "pm", "list", "packages", pkg]):
        try:
            out = adb(args, serial, timeout=8.0)
            if args[2] == "path" and any(x.strip().startswith("package:") for x in out.splitlines()):
                return True
            if args[2] == "list" and any(x.strip() == f"package:{pkg}" for x in out.splitlines()):
                return True
        except Exception:
            pass
    return False


@dataclass
class DeviceInfo:
    serial: str
    state: str
    model: str
    manufacturer: str | None = None
    androidVersion: str | None = None
    battery: int | None = None


_INFO_MARKER = "@@@"
_INFO_CMD = (
    "getprop ro.product.manufacturer ; echo @@@ ; "
    "getprop ro.product.model ; echo @@@ ; "
    "getprop ro.build.version.release ; echo @@@ ; "
    "dumpsys battery"
)


def _device_info(serial: str) -> dict:
    try:
        out = adb(["shell", _INFO_CMD], serial, timeout=8.0)
        parts = [p.strip() for p in out.split(_INFO_MARKER)]
        manufacturer = parts[0] or None
        model = parts[1] or None
        android_version = parts[2] or None
        battery_dump = parts[3] if len(parts) > 3 else ""
        match = re.search(r"level:\s*(\d+)", battery_dump, re.IGNORECASE)
        return {
            "manufacturer": manufacturer,
            "model": model,
            "androidVersion": android_version,
            "battery": int(match.group(1)) if match else None,
        }
    except Exception:
        return {"manufacturer": None, "model": None, "androidVersion": None, "battery": None}


def _raw_device_list() -> list[dict]:
    out = adb(["devices", "-l"])
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        props = {}
        for p in parts[2:]:
            if ":" in p:
                k, _, v = p.partition(":")
                props[k] = v
        devices.append({"serial": serial, "state": state, **props})
    return devices


def list_devices() -> list[DeviceInfo]:
    raw = _raw_device_list()
    # Same results as the sequential version; per-device `adb shell` info
    # fetches run concurrently so multi-phone `devices`/`doctor` don't pay
    # each 8s timeout back-to-back. Order of `adb devices -l` is preserved.
    infos: dict[str, dict] = {}
    targets = [d["serial"] for d in raw if d["state"] == "device"]

    def fetch(serial: str) -> None:
        infos[serial] = _device_info(serial)

    threads = [threading.Thread(target=fetch, args=(s,), daemon=True) for s in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = []
    for d in raw:
        if d["state"] != "device":
            result.append(
                DeviceInfo(
                    serial=d["serial"],
                    state=d["state"],
                    model=d.get("model") or "Unknown",
                )
            )
            continue
        info = infos.get(d["serial"], {})
        result.append(
            DeviceInfo(
                serial=d["serial"],
                state="device",
                model=info.get("model") or d.get("model") or "Unknown model",
                manufacturer=info.get("manufacturer") or "Unknown maker",
                androidVersion=info.get("androidVersion"),
                battery=info.get("battery"),
            )
        )
    return result


def connected_device_ids() -> list[str]:
    return [d.serial for d in list_devices() if d.state == "device"]


# --- screencap --------------------------------------------------------------

def capture_frame(serial: str) -> bytes:
    """Grab one PNG frame via `adb exec-out screencap -p`."""
    try:
        out = subprocess.run(
            [ADB_PATH, "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=4.0,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("screencap timed out") from None
    if out.returncode != 0 or len(out.stdout) < 100:
        raise RuntimeError("empty frame from screencap")
    return out.stdout


# --- cached uiautomator2 connections ----------------------------------------

_DEVICE_CACHE: dict[str, Any] = {}
_DEVICE_LOCK = threading.Lock()


def get_device(serial: str) -> Any:
    """Return a cached u2.Device for the serial (one ADB session per device).

    Uses connect_usb (same code path as connect(serial), but explicit that we
    only drive USB-attached devices). connect_usb auto-pushes u2.jar and
    starts the UIAutomator server on first use.

    uiautomator2 is imported lazily so plain CLI commands (devices/doctor
    text paths, --help) don't pay its import cost; behavior is unchanged.
    """
    import uiautomator2 as u2

    with _DEVICE_LOCK:
        d = _DEVICE_CACHE.get(serial)
        if d is None:
            d = u2.connect_usb(serial)
            # Element wait timeout for exists()/find calls — keeps the UI
            # automation responsive while still allowing for slow renders.
            d.settings["wait_timeout"] = 5.0
            _DEVICE_CACHE[serial] = d
        return d
