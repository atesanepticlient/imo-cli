"""Core IMO automation workflow — direct port of lib/imo-appium-automation.ts.

Key differences from the original:
- Talks to each device through uiautomator2 directly (no Appium server).
- `run_all_devices` fans out to one thread per device (the original awaited
  each device sequentially).
- Page-source parsing uses uiautomator2's dump_hierarchy instead of regex-
  scraping WebDriver page source; the selectors, fallbacks and geometry
  rules are identical.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .adb import connected_device_ids, get_device, is_package_installed, open_app
from .constants import (
    ACCOUNT_ITEM_VIEW,
    ACCOUNT_LIST,
    B_DESC_VIEW,
    B_TITLE_VIEW,
    CL_MESSAGE_ROOT,
    DEBUG_DIR,
    DICE_COMPLETION_RUN,
    DICE_EMOJI,
    HEART_LIKED_FRACTION,
    THUMBS_UP_EMOJI,
    THUMBS_UP_COMPLETION_RUN,
    EXPECTED_PACKAGES,
    HOME_PROFILE_PIC_WRAP,
    IMO_PACKAGE,
    IV_STICKY_TOP,
    LAYOUT_ROOT,
    LIKE_BUTTON_IDS,
    LIKE_BUTTON_SUFFIXES,
    LIKE_KEYWORDS,
    LIKE_MAX_ATTEMPTS,
    LIKE_STATIC_THRESHOLD,
    LIKE_VERIFY_INTERVAL,
    LIKE_VERIFY_POLLS,
    VIEWER_OPEN_TIMEOUT,
    VIEWER_SETTLE,
    MULTISPACE_PACKAGE,
    MULTISPACE_TILE,
    NEW_MESSAGE_KEYWORDS,
    NON_LIKE_BLACKLIST_KEYWORDS,
    RV_CONTENT,
    STORY_PACKAGE,
    THUMB_IDS,
    THUMB_SUFFIXES,
    TOGGLE,
    TV_APP_NAME,
    V_CLICK,
    XIV_SWITCH_ACCOUNT,
)
from .device import (
    current_package,
    ensure_ready,
    find_pinned_chat,
    handle_popups,
    is_app_package,
    is_chat_list_visible,
    tap,
    wait_for_package,
)
from .logs import LogFn, make_log

# Thread-safe manual disconnect flags (same semantics as requestDisconnect).
DISCONNECT_REQUESTS: set[str] = set()
_DISCONNECT_LOCK = threading.Lock()

# STOP stopping system: hard-stop the current account media scan on an exact
# standalone STOP message row, then switch account.
STOP_MARKER_TEXT = "STOP"

# Devices that already logged the "no 🎲 in dump" diagnostic — once per run
# per device, so the log stays readable.
_DICE_SAMPLE_LOGGED: set[str] = set()

# Per-device count of auto-captured screenful dumps (dice_debug_<serial>_<n>.xml
# in the project root) — capped so a diagnostic run stays fast.
_DICE_DUMPED: dict[str, int] = {}
_MAX_DICE_DUMPS = 4

# Hard cap on chat-screenful scans — guarantees the like loop terminates even
# on pathological chats (each screenful processes several media rows).
MAX_LIKE_SCREENFULS = 500


def request_disconnect(device_id: str) -> None:
    with _DISCONNECT_LOCK:
        DISCONNECT_REQUESTS.add(device_id)


def clear_disconnect(device_id: str) -> None:
    with _DISCONNECT_LOCK:
        DISCONNECT_REQUESTS.discard(device_id)


def _is_disconnect_requested(device_id: str) -> bool:
    with _DISCONNECT_LOCK:
        return device_id in DISCONNECT_REQUESTS


def checkpoint(d, device_id: str, target_pkg: str = MULTISPACE_PACKAGE) -> None:
    """At natural boundaries: honor a manual disconnect request, and make sure
    we're inside Multi Space or IMO. If the foreground app has drifted
    (launcher, settings, crash dialog, unrelated app), relaunch Multi Space
    quickly and continue instead of aborting the whole device run.
    """
    if _is_disconnect_requested(device_id):
        with _DISCONNECT_LOCK:
            DISCONNECT_REQUESTS.discard(device_id)
        raise RuntimeError("Disconnected by user")

    pkg = current_package(d)
    if not pkg or is_app_package(pkg):
        return

    for attempt in range(2):
        _emit_recovery(device_id, pkg)
        open_app(device_id, MULTISPACE_PACKAGE)
        if wait_for_package(d, MULTISPACE_PACKAGE, timeout=5.0):
            return
        pkg = current_package(d)
        if is_app_package(pkg):
            return

    raise RuntimeError(
        f"Could not return to Multi Space after unexpected foreground app ({pkg})"
    )


# Side channel so checkpoint() (which has no log function) can surface a
# recovery line. cli.py assigns the NDJSON emitter here before a run starts.
_checkpoint_emit = None


def workflow_checkpoint_emit(emit) -> None:
    global _checkpoint_emit
    _checkpoint_emit = emit


def _emit_recovery(device_id: str, pkg: str) -> None:
    emit = _checkpoint_emit
    if emit is not None:
        make_log(emit)(device_id, f"Recovered from home screen ({pkg}) — relaunching app", "info")


# ---------------------------------------------------------------------------
# Page-source helpers (uiautomator2 dump_hierarchy -> node list)
# ---------------------------------------------------------------------------

def parse_page_nodes(d, compressed: bool = True):
    """Return a list of {resourceId, text, bounds, className, clickable, ...}
    from the current uiautomator2 hierarchy dump.

    Node classes differ from Appium (android.view.View vs android.widget.*),
    so the automation only keys on resource-id, text, bounds, class, clickable
    and selected — the same attributes the original regexes matched.

    `compressed=False` includes nodes that have no resource-id/text/desc (e.g.
    a bare clickable icon) — used when hunting for an unknown control.
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
                "contentDesc": attr.get("content-desc", ""),
                "clickable": attr.get("clickable") == "true",
                "selected": attr.get("selected") == "true",
                "checked": attr.get("checked") == "true",
            }
        )
    return nodes


def contains(outer, inner) -> bool:
    ox1, oy1, ox2, oy2 = outer
    ix1, iy1, ix2, iy2 = inner
    return ix1 >= ox1 and iy1 >= oy1 and ix2 <= ox2 and iy2 <= oy2


# ---------------------------------------------------------------------------
# Multi Space tile traversal
# ---------------------------------------------------------------------------

def _scroll_rv_to_top(d) -> None:
    """Scroll Multi Space grid back to the top so tile searches start clean."""
    try:
        w, h = d.window_size()
        center_x = w // 2
        d.swipe(center_x, int(h * 0.25), center_x, int(h * 0.75), duration=0.15)
        time.sleep(0.1)
    except Exception:
        pass


def _scroll_rv_by_one_row(d, nodes) -> None:
    rv = next((n for n in nodes if n["resourceId"] == RV_CONTENT or n["resourceId"].endswith(":id/rvContent")), None)
    if rv:
        rv_bounds = rv["bounds"]
    else:
        rv_bounds = (33, 282, 1167, 2368)
    first_tile = next((n for n in nodes if n["resourceId"] == LAYOUT_ROOT or n["resourceId"].endswith(":id/layout_root")), None)
    row_height = first_tile["bounds"][3] - first_tile["bounds"][1] if first_tile else 349

    center_x = (rv_bounds[0] + rv_bounds[2]) // 2
    start_y = rv_bounds[3] - row_height
    end_y = start_y - row_height
    d.swipe(center_x, start_y, center_x, end_y, duration=0.25)
    time.sleep(0.3)


def _discover_multispace_imo_tiles(d) -> list[str]:
    """Scan Multi Space grid to discover all existing IMO clone labels instantly."""
    discovered = set()
    nodes = parse_page_nodes(d)
    for n in nodes:
        if n["resourceId"] == TV_APP_NAME or n["resourceId"].endswith(":id/tvAppName"):
            t = n["text"].strip()
            if re.match(r"^imo(\s*\(\d+\)|\s*\d+)?$", t, re.IGNORECASE):
                discovered.add(t)

    # If few tiles found, scroll down one row to check for more
    if 0 < len(discovered) < 8:
        _scroll_rv_by_one_row(d, nodes)
        nodes2 = parse_page_nodes(d)
        for n in nodes2:
            if n["resourceId"] == TV_APP_NAME or n["resourceId"].endswith(":id/tvAppName"):
                t = n["text"].strip()
                if re.match(r"^imo(\s*\(\d+\)|\s*\d+)?$", t, re.IGNORECASE):
                    discovered.add(t)
        _scroll_rv_to_top(d)

    def sort_key(label: str):
        m = re.search(r"\d+", label)
        num = int(m.group()) if m else 0
        return (0 if num == 0 else 1, num)

    return sorted(discovered, key=sort_key)


def _find_tile_coords(d, target_names: list[str] | str) -> dict | None:
    """Search the Multi Space grid for a tile matching any of target_names."""
    if isinstance(target_names, str):
        target_names = [target_names]
    targets = {t.strip().lower() for t in target_names}

    max_scroll_attempts = 4
    for scroll_attempt in range(max_scroll_attempts + 1):
        nodes = parse_page_nodes(d)
        for root in [n for n in nodes if n["resourceId"] == LAYOUT_ROOT or n["resourceId"].endswith(":id/layout_root")]:
            name_node = next(
                (
                    n
                    for n in nodes
                    if (n["resourceId"] == TV_APP_NAME or n["resourceId"].endswith(":id/tvAppName"))
                    and contains(root["bounds"], n["bounds"])
                    and n["text"].strip().lower() in targets
                ),
                None,
            )
            if not name_node:
                continue
            click_node = next(
                (
                    n
                    for n in nodes
                    if (n["resourceId"] == V_CLICK or n["resourceId"].endswith(":id/vClick"))
                    and contains(root["bounds"], n["bounds"])
                ),
                None,
            )
            b = click_node["bounds"] if click_node else root["bounds"]
            return {
                "x": (b[0] + b[2]) // 2,
                "y": (b[1] + b[3]) // 2,
            }
        if scroll_attempt < max_scroll_attempts:
            _scroll_rv_by_one_row(d, nodes)
    return None


def _open_tile_and_return(d, device_id: str, log, coords: dict, label: str) -> None:
    log(device_id, f"Opening {label}...", "info", {"event": "imo_instance_start", "imoLabel": label})
    tap(d, coords["x"], coords["y"])

    # Wait until IMO starts inside Multi Space (<3s)
    deadline = time.time() + 6.0
    while time.time() < deadline:
        nodes = parse_page_nodes(d)
        if handle_popups(d, device_id, log, nodes=nodes):
            break
        if is_chat_list_visible(d, nodes=nodes) or any(
            n["resourceId"] == HOME_PROFILE_PIC_WRAP or n["resourceId"].endswith(":id/home_profile_pic_wrap")
            for n in nodes
        ):
            break
        time.sleep(0.2)

    time.sleep(0.3)
    handle_popups(d, device_id, log)

    run_full_imo_workflow(d, device_id, log)

    # Return cleanly to Multi Space grid
    for _ in range(3):
        nodes = parse_page_nodes(d)
        if any(n["resourceId"] == LAYOUT_ROOT or n["resourceId"].endswith(":id/layout_root") for n in nodes):
            break
        d.press("back")
        time.sleep(0.2)

    open_app(device_id, MULTISPACE_PACKAGE)
    wait_for_package(d, MULTISPACE_PACKAGE, timeout=4.0)
    time.sleep(0.3)
    log(device_id, f"{label} finished and closed", "success", {"event": "imo_instance_done", "imoLabel": label})


def open_all_imo_apps_from_multispace(d, device_id: str, log: LogFn) -> None:
    log(device_id, "Launching Multi Space for the current Android user...", "info")
    open_app(device_id, MULTISPACE_PACKAGE)
    if not wait_for_package(d, MULTISPACE_PACKAGE, timeout=5.0):
        log(device_id, "Multi Space did not come to foreground in time", "error")
    time.sleep(0.5)
    handle_popups(d, device_id, log)

    tiles = _discover_multispace_imo_tiles(d)
    if not tiles:
        # Fallback to standard clone names if discovery returned empty
        tiles = ["imo", "imo(1)", "imo(2)", "imo(3)"]

    log(device_id, f"Found {len(tiles)} IMO clone(s): {', '.join(tiles)}", "info")

    for label in tiles:
        checkpoint(d, device_id, MULTISPACE_PACKAGE)
        log(device_id, f"Opening tile: {label}", "info")
        coords = _find_tile_coords(d, [label])
        if not coords:
            log(device_id, f'Could not locate tile "{label}" — skipping', "warn")
            continue
        _open_tile_and_return(d, device_id, log, coords, label)

    log(device_id, "Finished processing all IMO clones in Multi Space", "success")


# ---------------------------------------------------------------------------
# Profile / switch-account
# ---------------------------------------------------------------------------

def open_profile(d, device_id: str, log) -> None:
    # Ensure we're on the home screen first (back out of any lingering
    # chat/Story screen) so the profile avatar is visible.
    for _ in range(3):
        if d(resourceId=HOME_PROFILE_PIC_WRAP).exists(timeout=0.4):
            break
        try:
            if d(resourceIdMatches=r".*:id/home_profile_pic_wrap").exists(timeout=0.2):
                break
        except Exception:
            pass
        pkg = current_package(d)
        if is_app_package(pkg):
            d.press("back")
            time.sleep(0.4)
        else:
            break

    profile = d(resourceId=HOME_PROFILE_PIC_WRAP)
    if not profile.exists(timeout=0.5):
        try:
            profile = d(resourceIdMatches=r".*:id/home_profile_pic_wrap")
        except Exception:
            pass
    if profile.exists(timeout=3.0):
        profile.click()
        log(device_id, "Profile opened", "success")
        return

    # Fallback: the avatar sits at the top-left of the home screen.
    # Use the layout_home_profile container if present, else top-left corner.
    nodes = parse_page_nodes(d)
    container = next(
        (
            n
            for n in nodes
            if n["resourceId"] == f"{IMO_PACKAGE}:id/layout_home_profile"
            or n["resourceId"].endswith(":id/layout_home_profile")
        ),
        None,
    )
    if container is not None:
        b = container["bounds"]
        tap(d, (b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        log(device_id, "Profile opened (by container)", "success")
        return
    w, h = d.window_size()
    tap(d, w // 8, h // 16)  # approximate top-left avatar
    log(device_id, "Profile opened (by top-left fallback)", "success")


def _scroll_account_list(d, nodes) -> None:
    list_node = next(
        (n for n in nodes if n["resourceId"] == ACCOUNT_LIST or n["resourceId"].endswith(":id/account_list")),
        None,
    )
    if list_node:
        lb = list_node["bounds"]
    else:
        lb = (0, 737, 1200, 1132)
    item_node = next(
        (n for n in nodes if n["resourceId"] == ACCOUNT_ITEM_VIEW or n["resourceId"].endswith(":id/account_item_view")),
        None,
    )
    item_height = item_node["bounds"][3] - item_node["bounds"][1] if item_node else 197

    center_x = (lb[0] + lb[2]) // 2
    start_y = lb[3] - 20
    end_y = start_y - item_height
    d.swipe(center_x, start_y, center_x, end_y, duration=0.3)
    time.sleep(0.6)


def open_switch_account(d, device_id: str, log, clicked_account_ids: set[str] | None = None) -> bool:
    """Click the Switch Account entry, then pick an unclicked account row.

    Returns True if an account was selected, False when the list is exhausted.
    """
    clicked_account_ids = clicked_account_ids if clicked_account_ids is not None else set()

    # Step 1: find + click the Switch Account entry (4s window).
    target = None
    deadline = time.time() + 4.0
    while time.time() < deadline:
        nodes = parse_page_nodes(d)
        switch = next(
            (
                n
                for n in nodes
                if n["resourceId"] == XIV_SWITCH_ACCOUNT
                or n["resourceId"].endswith(":id/xiv_switch_account")
                or n["text"].strip().lower() == "switch account"
            ),
            None,
        )
        if switch is not None:
            b = switch["bounds"]
            target = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            break
        time.sleep(0.2)
    if target is None:
        raise RuntimeError("Switch Account entry not found")
    tap(d, *target)
    log(device_id, f'Clicked "Switch Account" at x:{target[0]}, y:{target[1]}', "success")
    time.sleep(0.5)  # let the account list render

    # Step 2: iterate visible account rows, scrolling one item at a time.
    for scroll_attempt in range(8):
        for row in get_account_rows(d):
            key = account_key(row)
            if not key or key in clicked_account_ids:
                continue
            log(device_id, f'Selecting account "{row["name"]}" at x:{row["x"]}, y:{row["y"]}', "info")
            tap(d, row["x"], row["y"])
            clicked_account_ids.add(key)
            return True
        if scroll_attempt < 7:
            log(device_id, "No new unclicked accounts visible, scrolling 1 item height...", "info")
            _scroll_account_list(d, parse_page_nodes(d))
    log(device_id, "Finished checking account list — all available accounts have been clicked.", "warn")
    return False


def get_account_rows(d) -> list[dict]:
    """Parse the account list into {name, desc, x, y, isCurrent} rows.

    Rows are ACCOUNT_ITEM_VIEW nodes (or clickable RelativeLayouts) inside the
    account_list container. Title / desc / current-toggle are matched to their
    row by bounds containment — never by list position — so an account's name
    can't get attached to the wrong row.
    """
    nodes = parse_page_nodes(d)
    container = next(
        (n for n in nodes if n["resourceId"] == ACCOUNT_LIST or n["resourceId"].endswith(":id/account_list")),
        None,
    )
    if container is None:
        return []
    _, cy1, _, cy2 = container["bounds"]

    def in_container(y: float) -> bool:
        return cy1 <= y <= cy2

    rows = []
    for n in nodes:
        b = n["bounds"]
        y_center = (b[1] + b[3]) / 2
        if not in_container(y_center):
            continue
        cls = n.get("className", "")
        rid = n.get("resourceId", "")
        if rid == ACCOUNT_ITEM_VIEW or rid.endswith(":id/account_item_view") or (
            cls == "android.widget.RelativeLayout" and n.get("clickable")
        ):
            rows.append(
                {
                    "x": (b[0] + b[2]) // 2,
                    "y": int(y_center),
                    "yTop": b[1],
                    "yBottom": b[3],
                    "name": "",
                    "desc": "",
                    "isCurrent": False,
                }
            )

    def y_center(n) -> float:
        b = n["bounds"]
        return (b[1] + b[3]) / 2

    for i, row in enumerate(rows):
        title = next(
            (
                n
                for n in nodes
                if (n["resourceId"] == B_TITLE_VIEW or n["resourceId"].endswith(":id/b_title_view"))
                and row["yTop"] <= y_center(n) <= row["yBottom"]
            ),
            None,
        )
        desc = next(
            (
                n
                for n in nodes
                if (n["resourceId"] == B_DESC_VIEW or n["resourceId"].endswith(":id/b_desc_view"))
                and row["yTop"] <= y_center(n) <= row["yBottom"]
            ),
            None,
        )
        toggle = next(
            (
                n
                for n in nodes
                if (n["resourceId"] == TOGGLE or n["resourceId"].endswith(":id/toggle"))
                and n.get("selected") is True
                and row["yTop"] <= y_center(n) <= row["yBottom"]
            ),
            None,
        )
        row["name"] = title["text"] if title is not None else f"account_{i}"
        row["desc"] = desc["text"] if desc is not None else ""
        row["isCurrent"] = toggle is not None
    return rows


# ---------------------------------------------------------------------------
# Pinned chats / like recent videos
# ---------------------------------------------------------------------------

# Finger travel of the slow chat drag (0.14h → 0.92h). Scroll delivery can
# never exceed finger travel (touch slop only reduces it), so a bottom band
# starting above this value can never contain a genuinely new post.
_SCROLL_TRAVEL = 0.78

# Bottom band of the screen excluded from processing on screenfuls after the
# first. In a chat the newest posts sit at the BOTTOM, so scrolling toward
# history reveals OLDER posts at the TOP while already-processed rows slide
# out the BOTTOM. The bottom band therefore only ever contains rows from the
# previous screenful (re-tapping them would toggle their like OFF); a new
# post can only enter at the top and can never land inside the band — nothing
# is ever skipped.
_BOTTOM_REPEAT_BAND = 0.18


def _select_todo_media(media, screen_h: int, screenful_index: int):
    """Media rows to process this screenful: everything on the first screenful;
    on later screenfuls, everything ABOVE the bottom repeat band.
    """
    if screenful_index == 0:
        return media
    limit = int((1.0 - _BOTTOM_REPEAT_BAND) * screen_h)
    return [(key, (x, y)) for key, (x, y) in media if y <= limit]


def _is_chat_list_present(d) -> bool:
    try:
        # One RPC: the regex also matches the exact CL_MESSAGE_ROOT id.
        return d(resourceIdMatches=r".*:id/cl_message_root").exists(timeout=0.2)
    except Exception:
        return False


def _wait_for_chat_list(d, timeout: float = 1.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_chat_list_present(d):
            return True
        time.sleep(0.1)
    return False


def _back_to_chat_list(d, timeout: float = 4.0) -> bool:
    """Press back (repeatedly if needed) until the chat list is visible.

    Works within Multi Space and IMO containers.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_chat_list_present(d):
            return True
        pkg = current_package(d)
        if not is_app_package(pkg):
            return False
        d.press("back")
        time.sleep(0.3)
    return _is_chat_list_present(d)


def _visible_media_from_nodes(nodes: list[dict]) -> list[tuple[tuple[int, int, int, int], tuple[int, int]]]:
    """Return (key, center) for media thumbnails, bottom-to-top by row order.

    Non-media rows (text/voice/emoji) are skipped, not stopping the scan, so a
    chat that interleaves text between videos still gets ALL its media liked.
    Keys use thumbnail bounds so a post is not re-processed after a small
    scroll.
    """
    rows = sorted(
        [
            n
            for n in nodes
            if n["resourceId"] == CL_MESSAGE_ROOT or n["resourceId"].endswith(":id/cl_message_root")
        ],
        key=lambda n: n["bounds"][1],
        reverse=True,
    )
    # Single-pass thumb index (same matches as the old 6-ids × rows scan):
    # collect every thumbnail node's vertical center once, then attach each
    # to the rows it overlaps. Identical (key, center) output, ~6x fewer ops.
    thumb_centers: list[tuple[float, tuple[int, int, int, int]]] = []
    for n in nodes:
        rid = n["resourceId"]
        if (
            rid in THUMB_IDS
            or rid.endswith(":id/iv_thumb")
            or rid.endswith(":id/iv_image")
            or rid.endswith(":id/iv_photo")
        ):
            b = n["bounds"]
            thumb_centers.append(((b[1] + b[3]) / 2, tuple(b)))
    media = []
    for row in rows:
        rb = row["bounds"]
        hit = next((tb for yc, tb in thumb_centers if rb[1] <= yc <= rb[3]), None)
        if hit is None:
            continue  # text/voice/emoji row — skip it, keep scanning for media
        tb = hit
        media.append(
            (
                tuple(tb),
                ((tb[0] + tb[2]) // 2, (tb[1] + tb[3]) // 2),
            )
        )
    return media


# No geometric post tracker: any coordinate-based dedup risks mis-identifying
# new posts after a scroll (which skips them entirely). Instead we scroll by
# a single large no-fling drag per screenful and process every media row
# outside the bottom repeat band; any row that still reappears at the
# boundary (the last video of the previous screenful) is handled safely by
# the liked-color cache in click_like_button (skipped, never un-liked).


def _dice_nodes(nodes: list[dict]) -> list[dict]:
    """Every node in the dump that carries the 🎲 emoji (text or content-desc)."""
    return [
        n
        for n in nodes
        if DICE_EMOJI in (n.get("text") or "") or DICE_EMOJI in (n.get("contentDesc") or "")
    ]


def _max_consecutive_dice_run(nodes: list[dict], rows: list[dict]) -> int:
    """Longest run of consecutive messages (chat order) that contain 🎲.

    A message counts when ANY node carrying the dice emoji OVERLAPS the
    message row's bounds — builds differ in how the emoji is laid out (inside
    the bubble, in a sibling node outside it, or even in a message row with a
    different id), so we never rely on exact containment or on the emoji being
    a descendant of the row. Dice nodes that match no row become their own
    messages, so a run made of non-standard rows is still detected. Any
    non-dice message in between resets the run.
    """
    dice = _dice_nodes(nodes)
    if not dice:
        return 0

    def overlaps(b1: tuple, b2: tuple) -> bool:
        return b1[1] < b2[3] and b2[1] < b1[3]

    messages: list[tuple[int, bool]] = []  # (y, is_dice), chat order
    for row in rows:
        rb = row["bounds"]
        messages.append((rb[1], any(overlaps(rb, n["bounds"]) for n in dice)))
    for n in dice:
        nb = n["bounds"]
        if not any(overlaps(rb, nb) for rb in (r["bounds"] for r in rows)):
            messages.append(((nb[1] + nb[3]) // 2, True))
    messages.sort(key=lambda m: m[0])

    run = best = 0
    for _, is_dice in messages:
        run = run + 1 if is_dice else 0
        if run > best:
            best = run
    return best


# A real message's text / big-emoji node is TALL (roughly one text line or a
# large emoji). Small labels inside the row's bounds — timestamps, read
# states, sender names, channel/chats headers — must NOT count as message
# content, or every row would look "non-empty" and the structural dice signal
# would never fire (real dumps show channel posts with an owner_name header
# inside each row).
_MIN_MESSAGE_CONTENT_H = 36
_LABEL_ID_MARKERS = ("owner_name", "date", "msg_state", "last_seen", "chat_name", "sender")


def _is_label_node(n: dict) -> bool:
    rid = n.get("resourceId") or ""
    return any(m in rid for m in _LABEL_ID_MARKERS)


def _max_consecutive_empty_run(nodes: list[dict], rows: list[dict]) -> int:
    """Longest run of consecutive rows that are neither media nor message text.

    Some IMO builds render the 🎲 as an ANIMATED emoji image — the bubble has
    an image and NO text/content-desc in the dump at all, so a content-based
    check can never see it. On those builds the dice messages are bubbles with
    no media thumbnail and no real message content (owner_name / date / state
    labels don't count). Three consecutive such rows are the task-complete
    signal there; any media or text row in between resets the run.
    """
    run = best = 0
    for row in sorted(rows, key=lambda n: n["bounds"][1]):
        rb = row["bounds"]
        inner = [n for n in nodes if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]]
        has_thumb = any(n["resourceId"] in THUMB_IDS for n in inner)
        has_content = any(
            (n.get("text") or n.get("contentDesc") or "").strip()
            and (n["bounds"][3] - n["bounds"][1]) >= _MIN_MESSAGE_CONTENT_H
            and not _is_label_node(n)
            for n in inner
        )
        run = run + 1 if (not has_thumb and not has_content) else 0
        if run > best:
            best = run
    return best


def _max_consecutive_emoji_placeholder_run(nodes: list[dict], rows: list[dict]) -> int:
    """Longest run of consecutive rows whose message text is the emoji
    placeholder "..".

    This is the definitive marker on animated-emoji builds (confirmed from a
    real device dump): the dice is rendered as an image and the message
    TextView (tv_message) exposes only the placeholder text ".." — no emoji
    codepoint, no empty text. Three consecutive such rows are the
    task-complete signal; a real text row or a media row in between resets the
    run.
    """
    run = best = 0
    for row in sorted(rows, key=lambda n: n["bounds"][1]):
        rb = row["bounds"]
        inner = [n for n in nodes if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]]
        is_emoji = any(
            (n.get("text") or "").strip() in ("..", "...")
            and "tv_message" in (n.get("resourceId") or "")
            for n in inner
        )
        run = run + 1 if is_emoji else 0
        if run > best:
            best = run
    return best


def _row_has_thumb_up_message(row: dict, nodes: list[dict], img) -> bool:
    """True for a real standalone 👍 message row.

    IMO can render large emoji stickers as bitmaps with no useful text/content-
    desc. The screenshot therefore acts as a fallback: the standard 👍 sticker
    has a large yellow body. We only inspect non-media chat rows, so the heart
    reaction toolbar and video thumbnails cannot trigger this marker.
    """
    rb = row["bounds"]
    # First prefer exact accessibility text/content-desc when available.
    for n in nodes:
        nb = n["bounds"]
        if rb[1] <= (nb[1] + nb[3]) // 2 <= rb[3]:
            if (n.get("text") or "").strip() == THUMBS_UP_EMOJI or (n.get("contentDesc") or "").strip() == THUMBS_UP_EMOJI:
                return True
    if img is None:
        return False
    try:
        w, h = img.size
        x0, x1 = max(0, rb[0]), min(w, rb[2])
        y0, y1 = max(0, rb[1]), min(h, rb[3])
        if x1 <= x0 or y1 <= y0:
            return False
        # Sample every 4 pixels. Yellow 👍 pixels: strongly red/green, low blue.
        # One crop + byte slicing instead of a getpixel call per sample; the
        # same grid (x0,y0 + 4k) and the same thresholds, so identical results.
        cw = x1 - x0
        data = img.crop((x0, y0, x1, y1)).convert("RGB").tobytes()
        total = 0
        yellow = 0
        minx = w; maxx = -1; miny = h; maxy = -1
        for y in range(y0, y1, 4):
            off = (y - y0) * cw * 3
            row = data[off:off + cw * 3]
            for i, (r, g, b) in enumerate(zip(row[0::12], row[1::12], row[2::12])):
                total += 1
                if r >= 205 and g >= 145 and b <= 105 and r - b >= 105 and g - b >= 60:
                    x = x0 + i * 4
                    yellow += 1
                    minx = min(minx, x); maxx = max(maxx, x)
                    miny = min(miny, y); maxy = max(maxy, y)
        if total == 0 or yellow < 450:
            return False
        # Require a reasonably large sticker, not a tiny yellow UI accent.
        return (yellow / total) >= 0.018 and (maxx - minx) >= 45 and (maxy - miny) >= 55
    except Exception:
        return False


def _thumbs_up_completion_run(d, nodes: list[dict], rows: list[dict], shot: dict | None = None) -> int:
    """Longest consecutive chat-row run containing the 👍 completion marker.

    Text pass first: an exact 👍 text/desc hit is decisive on its own, so a
    3-run there returns without paying for a screenshot. The screenshot
    fallback only runs when the text pass is inconclusive (bitmap stickers
    with no text) — identical results, fewer screencaps.
    """
    ordered = sorted(rows, key=lambda r: r["bounds"][1])
    run = best = 0
    for row in ordered:
        rb = row["bounds"]
        inner = [n for n in nodes if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]]
        is_media = any(n["resourceId"] in THUMB_IDS for n in inner)
        if is_media:
            run = 0
            continue
        text_hit = any(
            (n.get("text") or "").strip() == THUMBS_UP_EMOJI
            or (n.get("contentDesc") or "").strip() == THUMBS_UP_EMOJI
            for n in nodes
            if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]
        )
        run = run + 1 if text_hit else 0
        best = max(best, run)
    if best >= THUMBS_UP_COMPLETION_RUN:
        return best
    img = _screenshot(d)
    if shot is not None:
        shot["img"] = img  # reused below as the chat image (same screen, no action in between)
    if img is None:
        return best
    run = best = 0
    for row in ordered:
        rb = row["bounds"]
        inner = [n for n in nodes if rb[1] <= (n["bounds"][1] + n["bounds"][3]) // 2 <= rb[3]]
        is_media = any(n["resourceId"] in THUMB_IDS for n in inner)
        marker = (not is_media) and _row_has_thumb_up_message(row, nodes, img)
        run = run + 1 if marker else 0
        best = max(best, run)
    return best


def _media_already_loved_in_chat(img, bounds: tuple[int, int, int, int]) -> bool:
    """Detect a filled red/pink heart beside a media message before opening it."""
    if img is None:
        return False
    try:
        w, h = img.size
        x0, y0, x1, y1 = bounds
        # IMO's reaction heart is just to the right of the media bubble.
        rx0 = min(w - 1, max(0, x1 + 10)); rx1 = min(w, x1 + 125)
        ry0 = max(0, y0 - 8); ry1 = min(h, y0 + min(105, max(70, (y1-y0)//2)))
        if rx1 <= rx0 or ry1 <= ry0:
            return False
        cw = rx1 - rx0
        # One crop + byte slicing instead of ~1000 getpixel calls; the same
        # every-3rd-pixel grid as before.
        data = img.crop((rx0, ry0, rx1, ry1)).convert("RGB").tobytes()
        red = 0
        minx = miny = 10 ** 9
        maxx = maxy = -1
        for yy in range(0, ry1 - ry0, 3):
            row = data[yy * cw * 3:(yy + 1) * cw * 3]
            for i, (r, g, b) in enumerate(zip(row[0::9], row[1::9], row[2::9])):
                if r >= 165 and r > g * 1.45 and r > b * 1.15 and g <= 145:
                    red += 1
                    x = i * 3
                    minx = min(minx, x); maxx = max(maxx, x)
                    miny = min(miny, yy); maxy = max(maxy, yy)
        # Enough red AND shaped like a heart-sized blob: a thin red line, a
        # badge dot or a speck of red in a photo must not count as "liked"
        # (that false positive made real, unliked posts get skipped).
        return red >= 18 and (maxx - minx) >= 12 and (maxy - miny) >= 12
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Go to new-message system: after opening a chat, jump once to newest message.
# ---------------------------------------------------------------------------

def _node_label(node: dict) -> str:
    return " ".join(
        part.strip()
        for part in (
            node.get("resourceId", ""),
            node.get("text", ""),
            node.get("contentDesc", ""),
        )
        if part and part.strip()
    ).lower()


def _is_new_message_label(node: dict) -> bool:
    label = _node_label(node)
    return any(k in label for k in NEW_MESSAGE_KEYWORDS)


def _is_numeric_badge(node: dict) -> bool:
    text = (node.get("text") or "").strip()
    return bool(text) and len(text) <= 4 and text.isdigit() and 0 < int(text) <= 9999


def _lower_right_candidate(nodes: list[dict], w: int, h: int) -> tuple[int, int] | None:
    """Best lower-right floating-pill candidate, or None.

    Geometry gate (avoids blind taps): center x >= w*0.70 (near w*0.76),
    y in h*0.55-h*0.92 (covers h*0.62-0.88), 28-150px per side,
    aspect ratio 0.55-1.8, clickable preferred.
    """
    best = None
    for node in sorted(nodes, key=lambda n: (n["bounds"][1], n["bounds"][0])):
        b = node["bounds"]
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw < 28 or bw > 150 or bh < 28 or bh > 150:
            continue
        ratio = bw / bh if bh else 0
        if ratio < 0.55 or ratio > 1.8:
            continue
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        if cx < int(w * 0.70):
            continue
        if cy < int(h * 0.55) or cy > int(h * 0.92):
            continue
        if node.get("clickable"):
            return (cx, cy)
        if best is None:
            best = (cx, cy)
    return best


def _find_new_message_button(d, device_id, log):
    nodes = parse_page_nodes(d, compressed=False)
    if not nodes:
        return None
    # 1. semantic clickable match
    for node in nodes:
        if node.get("clickable") and _is_new_message_label(node):
            b = node["bounds"]
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), "semantic"
    try:
        w, h = d.window_size()
        w, h = int(w), int(h)
    except Exception:
        return None
    # 2. unread-badge visible: pair it with the lower-right floating candidate.
    if any(_is_numeric_badge(n) for n in nodes):
        hit = _lower_right_candidate(nodes, w, h)
        if hit is not None:
            return hit, "badge-geometry"
    # 3. "below are new messages" divider visible: same lower-right candidate.
    # Labels computed once (same strings the old code compared, no re-parse).
    labels = [_node_label(n) for n in nodes]
    if any("below" in lab and ("new" in lab or "unread" in lab) for lab in labels):
        hit = _lower_right_candidate(nodes, w, h)
        if hit is not None:
            return hit, "divider-geometry"
    # 4. pure geometry fallback (gated lower-right pill only — never blind).
    hit = _lower_right_candidate(
        [n for n in nodes if n.get("clickable")], w, h
    )
    if hit is not None:
        return hit, "geometry"
    return None


def _jump_to_newest_message_if_needed(d, device_id, log) -> bool:
    if _is_disconnect_requested(device_id):
        with _DISCONNECT_LOCK:
            DISCONNECT_REQUESTS.discard(device_id)
        raise RuntimeError("Disconnected by user")
    found = _find_new_message_button(d, device_id, log)
    if not found:
        log(
            device_id,
            "No new-message control visible; continuing immediately",
            "info",
            {"event": "already_at_newest"},
        )
        return False
    (x, y), source = found
    log(
        device_id,
        f"New-message control found ({source}); jumping to newest",
        "info",
        {"event": "jump_to_newest"},
    )
    d.click(x, y)
    time.sleep(0.12)
    return True


# ---------------------------------------------------------------------------
# STOP stopping system helpers
# ---------------------------------------------------------------------------

def _stop_marker_in_row(row_bounds, nodes) -> bool:
    target = STOP_MARKER_TEXT.casefold()
    for node in nodes:
        if not contains(row_bounds, node["bounds"]):
            continue
        for value in (node.get("text") or "", node.get("contentDesc") or ""):
            if value.strip() and value.strip().casefold() == target:
                return True
    return False


def _is_thumb_node(n: dict) -> bool:
    rid = n.get("resourceId", "")
    return rid in THUMB_IDS or any(rid.endswith(f":id/{s}") for s in THUMB_SUFFIXES)


def _chat_heart_state(d, key) -> bool | None:
    """Ground truth from the chat itself: is the reaction heart now beside the
    media row whose thumbnail bounds are `key`?

    True / False = heart present / absent. None = cannot tell (chat list not
    visible, the row is no longer exactly where it was, or no screenshot) — the
    caller must not tap blindly in that case.
    """
    if not _wait_for_chat_list(d, timeout=2.0):
        return None
    nodes = parse_page_nodes(d)
    if not any(_is_thumb_node(n) and tuple(n["bounds"]) == tuple(key) for n in nodes):
        return None
    img = _screenshot(d)
    if img is None:
        return None
    return _media_already_loved_in_chat(img, key)


def _resolve_pending_media(d, device_id: str, log, key, xy, first_result: str) -> str:
    """Final answer for a media item whose first attempt was not a confirmed like.

    Returns "liked", "already_liked" or "failed". Never taps blindly: the like
    button is only pressed again when the chat shows NO heart for the item, and
    the viewer re-checks the heart before tapping, so a post that did get liked
    is never toggled off.
    """
    x, y = xy
    state = _chat_heart_state(d, key)

    if state is True:
        if first_result == "unverified":
            log(device_id, "Liked post (confirmed in chat)", "info", {"event": "video_liked"})
            return "liked"
        return "already_liked"

    if state is None:
        log(device_id, "Could not re-check this post (chat list moved) — not retrying blindly", "warn")
        _save_debug(d, device_id, f"recheck_{first_result}")
        return "failed"

    if first_result == "no_story":
        # Already tapped twice and it never opened — not a likeable item.
        log(device_id, "Item would not open — no like possible", "warn")
        _save_debug(d, device_id, "no_story")
        return "failed"

    log(device_id, "No heart in chat yet — retrying this post", "warn")
    result = like_single_media(d, device_id, log, x, y)
    if result == "liked":
        return "liked"
    if result == "already_liked":
        return "liked" if first_result == "unverified" else "already_liked"
    if result == "unverified" and _chat_heart_state(d, key) is True:
        log(device_id, "Liked post (confirmed in chat)", "info", {"event": "video_liked"})
        return "liked"
    _save_debug(d, device_id, f"final_{result}")
    return "failed"


def like_recent_videos_if_present(d, device_id: str, log) -> bool:
    """Like every media post in the chat, walking newest → oldest.

    Speed & completeness design:
    - One hierarchy dump per screenful; EVERY media row above the repeat band
      is processed, bottom-to-top (newest first). Nothing between screenfuls
      can be skipped — that was the old bug where a 0.55-screen filter plus
      short scrolls double-scrolled and dropped posts.
    - A single slow no-fling drag scrolls by ~0.78 screenful (finger
      0.14h → 0.92h, i.e. downward, revealing OLDER posts — newer posts sit
      at the bottom of a chat). The drag stays entirely INSIDE the message
      list: starting on the status bar / chat header (y < 0.12h) or ending in
      the nav-bar / system-gesture zone (y > 0.93h) silently swallows the
      gesture on many devices — that is exactly why earlier runs only liked
      the first screenful. The long travel also shoves every processed row
      (including the last video of the previous screenful) past the bottom of
      the screen, so it cannot be re-tapped after the scroll.
    - Termination: three consecutive dumps showing the exact same media rows
      mean the list stopped moving (top of history). A swallowed slow drag
      gets one faster-flick retry before we give up.
    """

    def scroll_up_one_screenful(flick: bool = False, size=None) -> None:
        # `size` is passed by the caller (one window_size IPC per screenful);
        # fetched here only for direct unit-test calls. Same gesture either way.
        if size is None:
            size = d.window_size()
        if flick:
            # Faster, shorter flick — a different gesture to try when a slow
            # drag is swallowed (sticky list). Only used as a last resort
            # before declaring the top of history; any movement beats stopping
            # with posts still unprocessed.
            d.swipe(size[0] // 2, int(size[1] * 0.35), size[0] // 2, int(size[1] * 0.85), duration=0.2)
        else:
            # Finger travels DOWN (top → bottom), so the list scrolls UP toward
            # OLDER posts (newest are at the bottom of a chat). A slow drag
            # scrolls by roughly the finger travel with no fling overshoot, so
            # nothing between screenfuls is ever jumped over. The drag starts
            # below the chat header and ends above the nav-bar / system-gesture
            # zone — starting on a non-scrollable edge silently swallows the
            # whole gesture on many devices (that was the original "can't
            # scroll" bug). 0.14h → 0.92h is a long travel: it moves the last
            # video liked last screenful well past the bottom of the screen so
            # it can't be re-tapped (and the bottom repeat band + liked-color
            # cache cover whatever boundary row still slips through).
            d.swipe(size[0] // 2, int(size[1] * 0.14), size[0] // 2, int(size[1] * 0.92), duration=0.5)
        time.sleep(0.3)  # let the list settle before the next dump

    if not _wait_for_chat_list(d, timeout=5.0):
        log(device_id, "No messages found in chat", "error")
        return

    # Go to new-message system: jump once to the newest message before any
    # media scan (single tap when the pill is visible, else continue at once).
    _jump_to_newest_message_if_needed(d, device_id, log)

    processed_count = 0
    liked_count = 0
    already_liked_count = 0
    failed_count = 0
    no_rows_streak = 0
    last_media_keys: frozenset | None = None
    stuck_streak = 0
    screenful_index = 0

    for _ in range(MAX_LIKE_SCREENFULS):
        if _is_disconnect_requested(device_id):
            with _DISCONNECT_LOCK:
                DISCONNECT_REQUESTS.discard(device_id)
            raise RuntimeError("Disconnected by user")

        # Keep taps safe: if the app drifted, recover before touching anything.
        checkpoint(d, device_id, MULTISPACE_PACKAGE)

        nodes = parse_page_nodes(d)
        rows = [
            n
            for n in nodes
            if n["resourceId"] == CL_MESSAGE_ROOT or n["resourceId"].endswith(":id/cl_message_root")
        ]
        if not rows:
            # We're not on the chat list (a story was left open, a share sheet
            # appeared, …). Back out to the chat and retry instead of giving
            # up on the whole chat.
            no_rows_streak += 1
            if no_rows_streak >= 3 or not _back_to_chat_list(d, timeout=3.0):
                log(device_id, "No messages visible in chat", "error")
                break
            log(device_id, "Returned to chat list after stray screen", "warn")
            continue
        no_rows_streak = 0

        # One window_size IPC per screenful, shared by the scroll gesture and
        # the repeat-band limit below (same values, fewer round-trips).
        screen_size = d.window_size()
        screen_h = int(screen_size[1])

        # Completion marker: exactly three SEPARATE 👍 chat messages.
        # Check BEFORE processing media, so the third marker can never be liked.
        shot: dict = {}
        thumbs_run = _thumbs_up_completion_run(d, nodes, rows, shot)
        if thumbs_run >= THUMBS_UP_COMPLETION_RUN:
            log(
                device_id,
                f"Found {THUMBS_UP_COMPLETION_RUN} separate 👍 messages — stopping likes",
                "success",
                {"event": "thumbs_up_completion_signal", "count": thumbs_run},
            )
            return True

        # STOP stopping system: hard-stop on an exact standalone STOP row.
        # Checked BEFORE media processing so STOP itself is never liked; a hit
        # returns True so the caller exits this chat and switches account.
        def stop_visible(nodes) -> bool:
            rows = [
                n
                for n in nodes
                if n["resourceId"] == CL_MESSAGE_ROOT
                or n["resourceId"].endswith(":id/cl_message_root")
            ]
            return any(_stop_marker_in_row(r["bounds"], nodes) for r in rows)

        if stop_visible(nodes):
            log(
                device_id,
                "STOP marker found — hard-stopping scan, switching account",
                "success",
                {"event": "stop_signal"},
            )
            return True

        media = _visible_media_from_nodes(nodes)  # bottom-to-top: (key, center)
        keys = frozenset(key for key, _ in media)
        if keys and keys == last_media_keys:
            # The same media rows are visible again — the list didn't move
            # (top of history or a swallowed scroll). Give the scroll a second
            # chance with a faster flick before declaring the top; only break
            # when the list has stayed put across three different attempts.
            stuck_streak += 1
            if stuck_streak >= 3:
                log(device_id, "Reached the top of chat history", "info", {"event": "video_scan_end"})
                break
            if stuck_streak == 2:
                log(device_id, "List did not move with a slow drag — trying a faster flick", "warn")
                scroll_up_one_screenful(flick=True, size=screen_size)
            else:
                log(device_id, "Scrolling half screen to load older media...", "info")
                scroll_up_one_screenful(size=screen_size)
            continue
        stuck_streak = 0
        last_media_keys = keys

        if not media:
            # Text-only screenful — scroll on through to find more media.
            log(device_id, "Scrolling half screen to load older media...", "info")
            scroll_up_one_screenful(size=screen_size)
            continue

        todo = _select_todo_media(media, screen_h, screenful_index)
        screenful_index += 1

        if not todo:
            # Everything visible is a repeat from the previous screenful.
            log(device_id, "Scrolling half screen to load older media...", "info")
            scroll_up_one_screenful(size=screen_size)
            continue

        chat_img = shot.get("img") or _screenshot(d)
        pending: list[tuple[tuple, tuple[int, int], str]] = []
        for key, (x, y) in todo:
            if _media_already_loved_in_chat(chat_img, key):
                processed_count += 1
                already_liked_count += 1
                log(device_id, "Video already liked/loved — skipping", "info", {"event": "video_already_liked"})
                continue
            processed_count += 1
            log(device_id, f"Processing continuous media item #{processed_count}", "info")
            result = like_single_media(d, device_id, log, x, y)
            if result == "liked":
                liked_count += 1
            elif result == "already_liked":
                already_liked_count += 1
            else:
                # "unverified" / "failed" / "no_story": nothing is counted as
                # done until it has been re-checked in the chat below.
                pending.append((key, (x, y), result))

        # Audit: every item whose like was not CONFIRMED gets looked at again
        # while the chat is still on this screenful (rows have not moved).
        for key, xy, first in pending:
            _raise_if_disconnected(device_id)
            final = _resolve_pending_media(d, device_id, log, key, xy, first)
            if final == "liked":
                liked_count += 1
            elif final == "already_liked":
                already_liked_count += 1
            else:
                failed_count += 1
                log(
                    device_id,
                    "Could not confirm the like on this post — see the debug screenshot",
                    "warn",
                    {"event": "video_like_failed"},
                )

        log(device_id, "Scrolling half screen to load older media...", "info")
        scroll_up_one_screenful(size=screen_size)

    log(
        device_id,
        f"Finished continuous sequence! Total media processed: {processed_count}, Liked: {liked_count}, Already liked: {already_liked_count}, Failed: {failed_count}",
        "success",
        {
            "event": "video_batch_complete",
            "processedCount": processed_count,
            "likedCount": liked_count,
            "failedCount": failed_count,
        },
    )
    return False


def _chat_rows_present(d) -> bool:
    """True while IMO's chat message rows are on screen (exact id, or the same
    id under any package prefix used by a Multi Space clone)."""
    try:
        # The regex also matches the exact CL_MESSAGE_ROOT id, so one RPC
        # answers what two did (the second only ran when the first was False).
        return d(resourceIdMatches=r".*:id/cl_message_root").exists(timeout=0.05)
    except Exception:
        return False


def _wait_for_imo_viewer(d, timeout: float = VIEWER_OPEN_TIMEOUT) -> bool:
    """Wait until the chat list is hidden (we're in a story/photo viewer).

    The rows must be gone on two consecutive checks, so a one-off gap in the
    hierarchy during the open animation is not mistaken for an open viewer.
    """
    deadline = time.time() + timeout
    gone = 0
    while time.time() < deadline:
        if _chat_rows_present(d):
            gone = 0
        else:
            gone += 1
            if gone >= 2:
                return True
        time.sleep(0.05)
    return False


def _raise_if_disconnected(device_id: str) -> None:
    if _is_disconnect_requested(device_id):
        with _DISCONNECT_LOCK:
            DISCONNECT_REQUESTS.discard(device_id)
        raise RuntimeError("Disconnected by user")


def _recover_to_chat(d) -> None:
    """Best-effort: get back to the chat list after an unexpected error."""
    try:
        _back_to_chat_list(d, timeout=3.0)
    except Exception:
        pass


# Debug screenshots of posts that could not be confirmed liked (capped per
# device so a bad run cannot fill the disk).
_DEBUG_SAVED: dict[str, int] = {}
_MAX_DEBUG_PER_DEVICE = 60


def _save_debug(d, device_id: str, tag: str, img=None) -> str | None:
    if not DEBUG_DIR:
        return None
    try:
        if _DEBUG_SAVED.get(device_id, 0) >= _MAX_DEBUG_PER_DEVICE:
            return None
        if img is None:
            img = _screenshot(d)
        if img is None:
            return None
        os.makedirs(DEBUG_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", device_id)
        path = os.path.join(DEBUG_DIR, f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png")
        img.save(path)
        _DEBUG_SAVED[device_id] = _DEBUG_SAVED.get(device_id, 0) + 1
        return path
    except Exception:
        return None


def like_single_media(d, device_id: str, log, x: int, y: int) -> str:
    """Open one media item, like it, CONFIRM the like, and return to the chat.

    Returns:
      "liked"         the heart turned red after our tap (confirmed)
      "already_liked" the post was already liked; nothing was tapped
      "unverified"    something changed after the tap but the heart could not
                      be confirmed red — the caller re-checks it in the chat
      "failed"        the like tap had no effect after every retry
      "no_story"      the item would not open (tapped twice)

    One bad item never aborts the chat — the caller decides what to do next.
    """
    _raise_if_disconnected(device_id)
    try:
        opened = False
        for attempt in range(2):
            d.click(x, y)
            log(device_id, "Opening media item...", "info")
            wait = VIEWER_OPEN_TIMEOUT if attempt == 0 else max(2.0, VIEWER_OPEN_TIMEOUT - 1.0)
            if _wait_for_imo_viewer(d, timeout=wait):
                # Let the viewer finish animating in: a tap on its controls
                # while it is still opening is silently swallowed.
                # _wait_for_imo_viewer already needed two consecutive
                # "rows gone" reads, so no third dump is needed here.
                time.sleep(VIEWER_SETTLE)
                opened = True
                break
            if attempt == 0:
                log(device_id, "Media did not open — tapping it again", "warn")
                time.sleep(0.3)
        if not opened:
            return "no_story"

        result = click_like_button(d, device_id, log)
        if result in ("failed", "unverified"):
            _save_debug(d, device_id, f"like_{result}")

        # Back out to the chat list.
        d.press("back")
        if not _wait_for_chat_list(d, timeout=0.8):
            _back_to_chat_list(d, timeout=3.0)
        return result
    except RuntimeError as e:
        if "Disconnected by user" in str(e):
            raise
        _recover_to_chat(d)
        return "failed"
    except Exception:
        _recover_to_chat(d)
        return "failed"


def _center_of(el) -> tuple[int, int] | None:
    try:
        b = el.info.get("bounds")
        if not b:
            return None
        return ((b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2)
    except Exception:
        return None


# The story viewer's like button sits at a FIXED position per device, so once
# a tap there has been CONFIRMED (heart turned red) the (x, y, half) is reused
# and no hierarchy dump is needed for later items. A tap that has no effect
# drops the cache and the button is located again.
_LIKE_POS_CACHE: dict[str, tuple[int, int, int]] = {}

# Devices that already logged the story-diagnostics line.
_DIAGNOSTICS_LOGGED: set[str] = set()


def _is_non_like_control(n: dict) -> bool:
    """True if node text/id/desc matches a known non-like UI control (marketplace, menu, etc.)."""
    rid = (n.get("resourceId") or "").lower()
    text = (n.get("text") or "").lower()
    desc = (n.get("contentDesc") or "").lower()
    combined = f"{rid} {text} {desc}"
    return any(b in combined for b in NON_LIKE_BLACKLIST_KEYWORDS)


def _half_for_bounds(b) -> int:
    """Half-size of the square patch sampled at the heart's centre."""
    return max(8, min(28, min(b[2] - b[0], b[3] - b[1]) // 4))


def _match_known_like(nodes, window_size: tuple[int, int] | None = None):
    """Known-id and keyword matches over a node list, strictly avoiding headers/menus.

    Returns ((cx, cy), source, half) or None.
    """
    min_y = int(0.16 * window_size[1]) if window_size else 180
    max_y = int(0.96 * window_size[1]) if window_size else 2400

    # 1. Exact ID & Suffix matching
    for n in nodes:
        b = n.get("bounds", (0, 0, 0, 0))
        if b[1] < min_y or b[3] > max_y:
            continue
        if _is_non_like_control(n):
            continue
        rid = n.get("resourceId", "")
        if rid in LIKE_BUTTON_IDS or any(
            rid.endswith(f":id/{s}") or rid.endswith(f"/{s}") or rid == s
            for s in LIKE_BUTTON_SUFFIXES
        ):
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), f"id:{rid}", _half_for_bounds(b)

    # 2. Keyword matching
    for n in nodes:
        b = n.get("bounds", (0, 0, 0, 0))
        if b[1] < min_y or b[3] > max_y:
            continue
        if _is_non_like_control(n):
            continue
        hay = f"{n['resourceId']} {n['text']} {n.get('contentDesc', '')}".lower()
        if any(k in hay for k in LIKE_KEYWORDS):
            return (
                ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2),
                f"keyword:{n['resourceId'] or n['text'] or n.get('contentDesc') or '?'}",
                _half_for_bounds(b),
            )
    return None


def _find_like_button(d, device_id: str, log, full: bool = True):
    """Locate the Story like control, returning ((cx, cy), source, half).

    Never returns None: when nothing in the hierarchy matches, the usual
    right-bar heart position is returned as a guess (the caller still VERIFIES
    the tap, so a wrong guess is detected instead of silently counted).
    """
    override = os.environ.get("IMO_LIKE_BUTTON_ID")
    if override:
        el = d(resourceId=override)
        if el.exists(timeout=0.2):
            c = _center_of(el)
            if c:
                return c, f"override:{override}", 10

    try:
        w, h = d.window_size()
    except Exception:
        w, h = 720, 1520

    nodes = parse_page_nodes(d)
    hit = _match_known_like(nodes, (w, h))
    if hit:
        return hit

    if full:
        nodes_uncompressed = parse_page_nodes(d, compressed=False)
        hit = _match_known_like(nodes_uncompressed, (w, h))
        if hit:
            return hit
        _log_story_diagnostics_once(d, device_id, log, nodes_uncompressed or nodes)

    # Safe fallback: On Story/Video screen, right-side heart column (x~88%, y~65%)
    return (int(w * 0.88), int(h * 0.65)), "right-bar:heart", 12


def _log_story_diagnostics_once(d, device_id: str, log, nodes) -> None:
    """Log the story-screen node list once so uncatalogued IDs can be seen."""
    if device_id in _DIAGNOSTICS_LOGGED:
        return
    _DIAGNOSTICS_LOGGED.add(device_id)
    if not nodes:
        log(device_id, "Story screen shows no nodes — using safe video heart gesture", "info")
        return
    parts = []
    for n in nodes[:30]:
        rid = n["resourceId"]
        text = n["text"]
        desc = n.get("contentDesc", "")
        clk = "·clickable" if n.get("clickable") else ""
        label = rid or text or desc or "?"
        parts.append(f"{label}{clk} {n['bounds']}")
    log(device_id, "Story nodes: " + " | ".join(parts), "info")


# ---------------------------------------------------------------------------
# Heart detection (pure functions over a PIL image — unit-testable)
# ---------------------------------------------------------------------------

def _is_heart_red(r: int, g: int, b: int) -> bool:
    """Strong red / red-pink, the colour of a liked heart. Skin tones, orange
    and ordinary video colours fail the g / (r-g) tests."""
    return r >= 170 and r - g >= 90 and r - b >= 40 and g <= 130


def _patch_pixels(img, cx: int, cy: int, half: int) -> list[tuple[int, int, int]]:
    """RGB triplets of the square patch around (cx, cy), clipped to the image."""
    w, h = img.size
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(w, cx + half + 1), min(h, cy + half + 1)
    if x1 <= x0 or y1 <= y0:
        return []
    data = img.crop((x0, y0, x1, y1)).convert("RGB").tobytes()
    return list(zip(data[0::3], data[1::3], data[2::3]))


def _heart_fraction(img, cx: int, cy: int, half: int) -> float:
    """Fraction of the patch around the heart icon that is heart-red. A solid
    (liked) heart fills the centre; an outline (unliked) heart does not."""
    px = _patch_pixels(img, cx, cy, half)
    if not px:
        return 0.0
    return sum(1 for r, g, b in px if _is_heart_red(r, g, b)) / len(px)


def _patch_change(img_a, img_b, cx: int, cy: int, half: int) -> float:
    """Mean colour distance between the same patch in two screenshots."""
    a = _patch_pixels(img_a, cx, cy, half)
    b = _patch_pixels(img_b, cx, cy, half)
    if not a or len(a) != len(b):
        return 0.0
    return sum(_color_distance(p, q) for p, q in zip(a, b)) / len(a)


def _screenshot(d):
    """One full-screen PIL RGB image via uiautomator2's persistent-connection
    screencap (no subprocess spawn — much faster than adb exec-out), or None
    on failure."""
    try:
        img = d.screenshot()
        if img is None:
            return None
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception:
        return None


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


# ---------------------------------------------------------------------------
# Closed-loop like: tap -> verify the heart turned red -> retry if swallowed
# ---------------------------------------------------------------------------

def _tap_like_and_verify(d, device_id: str, log, target: tuple[int, int, int]) -> str:
    """One tap on the like button, then watch the heart.

    Returns "liked" (heart turned red), "already_liked" (it was red before we
    touched anything — nothing tapped), "swallowed" (nothing on screen changed,
    so the tap did not register) or "unverified" (the icon changed but not to a
    recognisable red heart, or no screenshot was available).
    """
    cx, cy, half = target
    img0 = _screenshot(d)
    if img0 is None:
        d.click(cx, cy)
        return "unverified"

    if _heart_fraction(img0, cx, cy, half * 3) >= 0.25:
        # The area AROUND the icon is red too (a red video frame): a red heart
        # cannot be told apart from the background, so the picture proves
        # nothing either way. Tap once and let the chat (which shows the heart
        # beside the post) be the judge — never trust, never re-tap blindly.
        d.click(cx, cy)
        time.sleep(0.2)
        return "unverified"

    if _heart_fraction(img0, cx, cy, half) >= HEART_LIKED_FRACTION:
        # Red before we tapped. Confirm with a second look so a transient red
        # flash in a video is not mistaken for an already-liked post.
        time.sleep(0.2)
        img1 = _screenshot(d)
        if img1 is None or _heart_fraction(img1, cx, cy, half) >= HEART_LIKED_FRACTION:
            log(device_id, "Post already liked — skipping", "info")
            return "already_liked"
        img0 = img1

    d.click(cx, cy)

    last = None
    for poll in range(max(1, LIKE_VERIFY_POLLS)):
        # The screencap itself takes a while, so the first look needs only a
        # short pause; later polls keep the full interval (same poll count).
        time.sleep(min(0.05, LIKE_VERIFY_INTERVAL) if poll == 0 else LIKE_VERIFY_INTERVAL)
        img = _screenshot(d)
        if img is None:
            continue
        last = img
        if _heart_fraction(img, cx, cy, half) >= HEART_LIKED_FRACTION:
            return "liked"

    if last is None:
        return "unverified"
    if _patch_change(img0, last, cx, cy, half) < LIKE_STATIC_THRESHOLD:
        return "swallowed"
    return "unverified"


def click_like_button(d, device_id: str, log) -> str:
    """Like the current post and CONFIRM it. Returns "liked", "already_liked",
    "unverified" or "failed".

    A tap that does nothing ("swallowed") is never counted as "already liked":
    the cached position is dropped, the button is located again and the tap is
    repeated, up to LIKE_MAX_ATTEMPTS times.
    """
    cached = _LIKE_POS_CACHE.get(device_id)
    for attempt in range(1, max(1, LIKE_MAX_ATTEMPTS) + 1):
        _raise_if_disconnected(device_id)
        used_cache = attempt == 1 and cached is not None
        if used_cache:
            target = cached
        else:
            (cx, cy), _source, half = _find_like_button(d, device_id, log, full=True)
            target = (cx, cy, half)

        outcome = _tap_like_and_verify(d, device_id, log, target)

        if outcome == "liked":
            _LIKE_POS_CACHE[device_id] = target
            log(device_id, "Liked post", "info", {"event": "video_liked"})
            return "liked"
        if outcome == "already_liked":
            return "already_liked"
        if outcome == "unverified":
            # A position that did not produce a confirmed like is never reused.
            _LIKE_POS_CACHE.pop(device_id, None)
            if used_cache and attempt < LIKE_MAX_ATTEMPTS:
                # On a playing video a tap at a stale spot also "changes" the
                # picture, so it looks unverified rather than swallowed. Look
                # up where the button really is; if it is somewhere else, the
                # cached spot was simply wrong — tap the right one. (If it is
                # the same spot we do NOT tap again: that could un-like it.)
                (fx, fy), _s, _h = _find_like_button(d, device_id, log, full=True)
                if abs(fx - target[0]) + abs(fy - target[1]) > 40:
                    log(device_id, "Like button is not where it was — retrying at its real position", "warn")
                    continue
            log(device_id, "Like tapped but not confirmed — will re-check in chat", "warn")
            return "unverified"

        # Swallowed: the viewer was not ready, or the cached spot was wrong.
        _LIKE_POS_CACHE.pop(device_id, None)
        if attempt < LIKE_MAX_ATTEMPTS:
            log(device_id, f"Like tap had no effect (attempt {attempt}) — retrying", "warn")
            time.sleep(0.4)
    log(device_id, "Like tap never registered", "warn")
    return "failed"


def process_all_pinned_chats(d, device_id: str, log) -> bool:
    processed_count = 0
    while True:
        checkpoint(d, device_id, MULTISPACE_PACKAGE)
        nodes = parse_page_nodes(d)
        pins = [
            n
            for n in nodes
            if n["resourceId"] == IV_STICKY_TOP or n["resourceId"].endswith(":id/iv_sticky_top")
        ]
        if not pins:
            log(device_id, "No pinned chat found" if processed_count == 0 else "No more pinned chats", "info")
            break
        if processed_count >= len(pins):
            log(device_id, "All pinned chats processed", "success")
            break
        found = find_pinned_chat(d, processed_count, nodes=nodes)
        if found is None:
            log(device_id, "Could not locate next pinned chat row, stopping", "error")
            break
        chat_name = found["chat_name"]
        d.click(found["x"], found["y"])
        log(device_id, f'Chat "{chat_name}" opened', "info", {"event": "chat_open", "chatName": chat_name})
        completed = like_recent_videos_if_present(d, device_id, log)
        log(device_id, f'Chat "{chat_name}" processed', "success", {"event": "chat_done", "chatName": chat_name})
        if completed:
            # Leave the group chat before opening profile/switch-account.
            d.press("back")
            time.sleep(0.15)
            log(device_id, "Exited group chat after 👍👍👍 — switching account", "success", {"event": "chat_exit_for_account_switch"})
            return True
        processed_count += 1
    return False


# ---------------------------------------------------------------------------
# Full per-imo-instance workflow
# ---------------------------------------------------------------------------

def run_full_imo_workflow(d, device_id: str, log: LogFn) -> None:
    processed_accounts: set[str] = set()

    ensure_ready(d, device_id, log)
    log(device_id, "IMO opened successfully", "success")

    while True:
        checkpoint(d, device_id, MULTISPACE_PACKAGE)

        process_all_pinned_chats(d, device_id, log)

        open_profile(d, device_id, log)
        open_switch_account(d, device_id, log)

        time.sleep(0.5)  # let the account list render

        rows = get_account_rows(d)
        if not rows:
            log(device_id, "Could not read the account list, stopping", "error")
            break

        current = next((r for r in rows if r["isCurrent"]), None)
        if current:
            processed_accounts.add(account_key(current))
            # Tells the frontend which account the chats it just watched
            # actually belonged to (only knowable now).
            log(device_id, f'Finished account "{current["name"]}"', "info", {"event": "account_active", "accountName": current["name"]})

        next_row = next(
            (r for r in rows if not r["isCurrent"] and account_key(r) not in processed_accounts),
            None,
        )
        if next_row is None:
            log(device_id, "No more accounts left to process for this imo instance", "success")
            break

        log(device_id, f'Switching to account "{next_row["name"]}"', "info", {"event": "account_switch", "accountName": next_row["name"]})
        tap(d, next_row["x"], next_row["y"])

        # Wait for the newly-selected account's home screen.
        if not (d(resourceId=HOME_PROFILE_PIC_WRAP).exists(timeout=8.0) or d(resourceIdMatches=r".*:id/home_profile_pic_wrap").exists(timeout=0.2)):
            log(device_id, "New account home screen did not appear", "warn")
        time.sleep(0.3)


def account_key(row: dict) -> str:
    return f'{row["name"]}|{row["desc"]}'


# ---------------------------------------------------------------------------
# Device-level entry point
# ---------------------------------------------------------------------------

def run_imo_workflow_on_device(device_id: str, log: LogFn) -> dict:
    clear_disconnect(device_id)
    mode = "multispace"
    try:
        log(device_id, "Starting...", "info", {"event": "device_start"})
        d = get_device(device_id)
        # Do not gate on package enumeration; XOS may hide profile packages from adb.
        open_all_imo_apps_from_multispace(d, device_id, log)
        log(device_id, "Workflow complete", "success", {"event": "device_done"})
        return {
            "deviceId": device_id,
            "success": True,
            "package": current_package(d),
            "mode": mode,
        }
    except Exception as e:
        log(device_id, f"Error: {e}", "error", {"event": "device_error"})
        return {"deviceId": device_id, "success": False, "error": str(e), "mode": mode}
    finally:
        clear_disconnect(device_id)


def run_all_devices(log: LogFn) -> list[dict]:
    """Run the workflow on every connected device, one thread per device.

    This is the headline performance change: the original
    `runAllDevices(log)` awaited `runImoWorkflowOnDevice` in a for-loop, so
    device N+1 never started until device N finished. Here all devices run
    simultaneously.
    """
    devices = connected_device_ids()
    if not devices:
        log("system", "No connected devices found", "error")
        return [{"error": "No connected devices found."}]

    results: list[dict] = []
    threads = []

    def worker(serial: str):
        results.append(run_imo_workflow_on_device(serial, log))

    for serial in devices:
        t = threading.Thread(target=worker, args=(serial,), name=f"imo-{serial}")
        t.daemon = True
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results
