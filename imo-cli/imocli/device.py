"""uiautomator2 device wrapper + popup handling.

Ports handle-popup.ts (ensureReady / handlePopups) and the tap/scroll helpers
from imo-appium-automation.ts onto uiautomator2's direct device API.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .adb import get_device
from .constants import (
    EXPECTED_PACKAGES,
    IMO_PACKAGE,
    IV_STICKY_TOP,
    MESSAGE_LAYOUT,
    MULTISPACE_PACKAGE,
    PERMISSION_BUTTONS,
    POPUP_BUTTONS,
    STORY_PACKAGE,
)

if TYPE_CHECKING:
    import uiautomator2 as u2


def current_package(d: "u2.Device") -> str:
    try:
        return d.app_current()["package"]
    except Exception:
        return ""


def is_app_package(pkg: str) -> bool:
    """True if pkg is Multi Space, IMO, or their sub-processes."""
    if not pkg:
        return False
    return any(p in pkg for p in EXPECTED_PACKAGES)


def wait_for_package(d: "u2.Device", pkg: str, timeout: float = 10.0) -> bool:
    """True once `pkg` is the foreground package (polling app_current)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        curr = current_package(d)
        if curr == pkg or pkg in curr:
            return True
        time.sleep(0.3)
    return False


def tap(d: "u2.Device", x: int, y: int) -> None:
    d.click(x, y)


def parse_page_nodes(d: "u2.Device", compressed: bool = True) -> list[dict]:
    """Return a list of {resourceId, text, bounds, className, clickable, ...}
    from the current uiautomator2 hierarchy dump in ~40ms.
    """
    import xml.etree.ElementTree as ET

    nodes = []
    try:
        xml_str = d.dump_hierarchy(compressed=compressed)
    except Exception:
        return nodes
    if not xml_str:
        return nodes
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return nodes

    for elem in root.iter():
        attr = elem.attrib
        rid = attr.get("resource-id", "")
        text = (attr.get("text") or "").strip()
        b = attr.get("bounds")
        if not b:
            continue
        try:
            x1, y1, x2, y2 = map(int, b.replace("[", "").replace("]", ",").split(",")[:4])
        except (ValueError, IndexError):
            continue
        nodes.append(
            {
                "resourceId": rid,
                "text": text,
                "bounds": (x1, y1, x2, y2),
                "className": attr.get("class", ""),
                "package": attr.get("package", ""),
                "contentDesc": (attr.get("content-desc") or "").strip(),
                "clickable": attr.get("clickable") == "true",
                "selected": attr.get("selected") == "true",
                "checked": attr.get("checked") == "true",
            }
        )
    return nodes


def handle_popups(d: "u2.Device", device_id: str, log, nodes: list[dict] | None = None) -> bool:
    """Fast in-memory popup dismissal (<50ms).

    Covers Android 7 through 15 system permission dialogs, Multi Space
    interstitial/close buttons, and IMO in-app popups.
    """
    if nodes is None:
        nodes = parse_page_nodes(d)
    if not nodes:
        return False

    perm_rids = set(PERMISSION_BUTTONS)
    popup_rids = set(POPUP_BUTTONS)
    perm_texts = {"allow", "while using the app", "only this time"}
    close_texts = {"skip", "close", "cancel"}

    for n in nodes:
        rid = n["resourceId"]
        txt = n["text"].lower()
        desc = n["contentDesc"].lower()
        b = n["bounds"]
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2

        if rid in perm_rids or any(rid.endswith(f":id/{p.split(':id/')[-1]}") for p in perm_rids if ":id/" in p):
            log(device_id, f"Popup: Allow ({rid or n['text']})", "info")
            d.click(cx, cy)
            time.sleep(0.3)
            return True

        if txt in perm_texts or desc in perm_texts:
            log(device_id, f"Popup: Permission ({n['text'] or n['contentDesc']})", "info")
            d.click(cx, cy)
            time.sleep(0.3)
            return True

        if rid in popup_rids or any(rid.endswith(f":id/{p.split(':id/')[-1]}") for p in popup_rids if ":id/" in p):
            log(device_id, f"Popup: Close ({rid})", "info")
            d.click(cx, cy)
            time.sleep(0.3)
            return True

        if txt in close_texts or desc in close_texts:
            if (b[2] - b[0] < 400) and (b[3] - b[1] < 200):
                log(device_id, f"Popup: Dismiss ({n['text'] or n['contentDesc']})", "info")
                d.click(cx, cy)
                time.sleep(0.3)
                return True

    return False


def is_chat_list_visible(d: "u2.Device", nodes: list[dict] | None = None) -> bool:
    """True if IMO's chat list root is present."""
    if nodes is not None:
        return any(
            n["resourceId"] == MESSAGE_LAYOUT
            or n["resourceId"].endswith(":id/message_layout")
            or n["resourceId"].endswith(":id/chats_list")
            for n in nodes
        )
    return d(resourceId=MESSAGE_LAYOUT).exists(timeout=0.1) or d(resourceId=CL_MESSAGE_ROOT).exists(timeout=0.1)


def ensure_ready(d: "u2.Device", device_id: str, log, max_retry: int = 6) -> None:
    """Dismiss popups and return to the IMO chat list in milliseconds."""
    for _ in range(max_retry):
        nodes = parse_page_nodes(d)
        if handle_popups(d, device_id, log, nodes=nodes):
            continue
        if is_chat_list_visible(d, nodes=nodes):
            log(device_id, "IMO ready", "info")
            return
        pkg = current_package(d)
        if is_app_package(pkg):
            d.press("back")
        time.sleep(0.3)
    raise RuntimeError("Unable to reach chat list.")


def find_pinned_chat(d: "u2.Device", pin_index: int, nodes: list[dict] | None = None) -> dict | None:
    """Return {x, y, chat_name} for the pin_index-th pinned chat row in memory."""
    if nodes is None:
        nodes = parse_page_nodes(d)
    if not nodes:
        return None

    pins = [
        n
        for n in nodes
        if n["resourceId"] == IV_STICKY_TOP or n["resourceId"].endswith(":id/iv_sticky_top")
    ]
    if pin_index >= len(pins):
        return None
    pins.sort(key=lambda p: p["bounds"][1])
    pin = pins[pin_index]
    pin_center_y = (pin["bounds"][1] + pin["bounds"][3]) / 2

    chats = [
        n
        for n in nodes
        if n["resourceId"] == MESSAGE_LAYOUT or n["resourceId"].endswith(":id/message_layout")
    ]
    for chat in chats:
        b = chat["bounds"]
        if b[1] <= pin_center_y <= b[3]:
            name = "chat"
            for n in nodes:
                if (
                    b[1] <= (n["bounds"][1] + n["bounds"][3]) / 2 <= b[3]
                    and n.get("className", "").endswith("TextView")
                ):
                    t = n["text"].strip()
                    if t and t != pin.get("text", ""):
                        name = t
                        break
            return {
                "x": (b[0] + b[2]) // 2,
                "y": (b[1] + b[3]) // 2,
                "chat_name": name,
            }
    return None


