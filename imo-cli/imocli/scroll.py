"""Standalone scrolling system, extracted from imo-cli's workflow.

Self-contained: needs only a uiautomator2 device object (`d`) — it imports
nothing else from the `imocli` package, so it can be dropped into any project.

Three scroll layers are included:

1. ``scroll_grid_to_top`` / ``scroll_grid_by_one_row``
   Multi Space tile grid: back to the top, or one tile-row down.

2. ``scroll_account_list_by_one_item``
   IMO account-switcher list: one account item at a time.

3. ``scroll_chat_one_screenful`` + ``ChatHistoryWalker``
   The chat walker used by the like scanner: iterates a chat newest → oldest
   in verified screenfuls.

Why the chat walker is not just `swipe in a loop`:

- One hierarchy dump per screenful; EVERY item of the dump is handed to the
  caller before scrolling on, so nothing between screenfuls can be skipped.
- The scroll is one slow, no-fling drag whose finger travels DOWN from 0.14h
  to 0.92h (newest messages sit at the BOTTOM of an IMO chat, so the content
  glides toward older history at the top). A slow drag moves roughly by the
  finger travel with no fling overshoot, and 0.14h → 0.92h is a long travel:
  it shoves every processed row (including the last item of the previous
  screenful) past the bottom of the screen, so it cannot be re-tapped.
- The drag stays entirely INSIDE the message list: starting on the chat
  header / status bar (y < 0.12h) or ending in the nav-bar gesture zone
  (y > 0.93h) silently swallows the whole gesture on many devices.
- Termination is verified, not assumed: the exact set of visible item keys is
  compared between dumps. If the list did not move, a slow drag is retried
  once, then a faster flick is tried, and only after three identical dumps is
  the walker satisfied it reached the top of history.
- On screenfuls after the first, a bottom band (default 18% of the screen)
  is excluded from processing: it can only contain rows from the previous
  screenful, and a new item can only enter at the top — nothing is skipped,
  but already-processed rows are never handed out again.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "Screenful",
    "ChatHistoryWalker",
    "scroll_grid_to_top",
    "scroll_grid_by_one_row",
    "scroll_account_list_by_one_item",
    "scroll_chat_one_screenful",
]


# ---------------------------------------------------------------------------
# Tuning knobs (env-overridable, same convention as the rest of imo-cli)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Pause after a chat scroll gesture, before the next hierarchy dump.
CHAT_SCROLL_SETTLE = _env_float("IMO_SCROLL_SETTLE", 0.3)

# Chat drag geometry: finger 0.14h -> 0.92h (see module docstring).
CHAT_DRAG_START_Y = 0.14
CHAT_DRAG_END_Y = 0.92
CHAT_DRAG_DURATION = 0.5

# Faster, shorter fallback gesture, tried when a slow drag is swallowed.
CHAT_FLICK_START_Y = 0.35
CHAT_FLICK_END_Y = 0.85
CHAT_FLICK_DURATION = 0.2

# Bottom band excluded from processing on screenfuls after the first.
BOTTOM_REPEAT_BAND = _env_float("IMO_BOTTOM_REPEAT_BAND", 0.18)

# Hard cap on hierarchy scans — guarantees the walk terminates even on a
# pathological (never-ending / never-moving) chat.
MAX_SCREENFULS = _env_int("IMO_MAX_SCREENFULS", 500)

# Identical dumps needed before the walker declares "top of history".
STUCK_STRIKES = 3

# Fallback geometry for the Multi Space grid / account list when the current
# dump does not contain the container nodes (matches the original tool).
RV_FALLBACK_BOUNDS = (33, 282, 1167, 2368)
TILE_FALLBACK_HEIGHT = 349
ACCOUNT_LIST_FALLBACK_BOUNDS = (0, 737, 1200, 1132)
ACCOUNT_ITEM_FALLBACK_HEIGHT = 197

GRID_ROW_SETTLE = 0.3
ACCOUNT_ITEM_SETTLE = 0.6


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _find(nodes: list[dict] | None, rid_suffix: str) -> dict | None:
    """First node whose resource-id ends with `:id/<rid_suffix>`.

    Matches both the exact package-qualified id and the same id under any
    virtualized (Multi Space) package prefix.
    """
    tag = f":id/{rid_suffix}"
    for n in nodes or ():
        if n.get("resourceId", "").endswith(tag):
            return n
    return None


def _bounds_height(bounds) -> int:
    return bounds[3] - bounds[1]


# ---------------------------------------------------------------------------
# 1. Multi Space tile grid
# ---------------------------------------------------------------------------

def scroll_grid_to_top(d) -> None:
    """One long swipe back toward the top of the Multi Space tile grid."""
    w, h = d.window_size()
    cx = w // 2
    d.swipe(cx, int(h * 0.25), cx, int(h * 0.75), duration=0.15)
    time.sleep(0.1)


def scroll_grid_by_one_row(d, nodes: list[dict] | None = None) -> None:
    """Scroll the Multi Space grid down exactly one tile row.

    Row height comes from the first visible tile (`layout_root`); the gesture
    runs inside the `rvContent` container so it never hits the status bar.
    """
    rv = _find(nodes, "rvContent")
    rv_bounds = rv["bounds"] if rv else RV_FALLBACK_BOUNDS

    tile = _find(nodes, "layout_root")
    row_height = _bounds_height(tile["bounds"]) if tile else TILE_FALLBACK_HEIGHT

    cx = (rv_bounds[0] + rv_bounds[2]) // 2
    start_y = rv_bounds[3] - row_height
    d.swipe(cx, start_y, cx, start_y - row_height, duration=0.25)
    time.sleep(GRID_ROW_SETTLE)


# ---------------------------------------------------------------------------
# 2. Account-switcher list
# ---------------------------------------------------------------------------

def scroll_account_list_by_one_item(d, nodes: list[dict] | None = None) -> None:
    """Scroll the account list down exactly one account item."""
    lst = _find(nodes, "account_list")
    lb = lst["bounds"] if lst else ACCOUNT_LIST_FALLBACK_BOUNDS

    item = _find(nodes, "account_item_view")
    item_height = _bounds_height(item["bounds"]) if item else ACCOUNT_ITEM_FALLBACK_HEIGHT

    cx = (lb[0] + lb[2]) // 2
    start_y = lb[3] - 20
    d.swipe(cx, start_y, cx, start_y - item_height, duration=0.3)
    time.sleep(ACCOUNT_ITEM_SETTLE)


# ---------------------------------------------------------------------------
# 3. Chat screenful gesture + verified walker
# ---------------------------------------------------------------------------

def scroll_chat_one_screenful(d, size: tuple[int, int] | None = None, flick: bool = False) -> None:
    """Scroll the open IMO chat by roughly one screenful toward OLDER messages.

    `size` is the (w, h) from a single ``d.window_size()`` call, shared with
    the caller's repeat-band math to save one IPC round-trip per screenful.
    Pass ``flick=True`` for the faster fallback gesture used when a slow drag
    is swallowed by a sticky list.
    """
    if size is None:
        size = d.window_size()
    cx = size[0] // 2
    if flick:
        d.swipe(
            cx, int(size[1] * CHAT_FLICK_START_Y),
            cx, int(size[1] * CHAT_FLICK_END_Y),
            duration=CHAT_FLICK_DURATION,
        )
    else:
        d.swipe(
            cx, int(size[1] * CHAT_DRAG_START_Y),
            cx, int(size[1] * CHAT_DRAG_END_Y),
            duration=CHAT_DRAG_DURATION,
        )
    time.sleep(CHAT_SCROLL_SETTLE)


@dataclass
class Screenful:
    """One verified screenful of chat content handed to the caller.

    index:  0-based screenful number (0 = the first, newest one).
    size:   (w, h) captured for this screenful.
    nodes:  the parsed hierarchy dump the items were taken from.
    todo:   [(key, (x, y)), ...] — items to process, newest first.
    """

    index: int
    size: tuple[int, int]
    nodes: list = field(default_factory=list)
    todo: list = field(default_factory=list)


class ChatHistoryWalker:
    """Iterate an open IMO chat in verified screenfuls, newest → oldest.

    Usage:

        def dump_nodes() -> list[dict]:        # your hierarchy parser here
            ...

        def find_items(nodes) -> list[tuple]:  # (key, (x, y)), bottom-to-top
            ...

        for sf in ChatHistoryWalker(d, dump_nodes, find_items, on_event=log):
            for key, (x, y) in sf.todo:
                ...process the item (tap it, like it, ...)...
            # returning to the loop advances the scroll automatically

    `key` must uniquely (and stably) identify an item while it stays on
    screen — this is what the stuck-detection compares between dumps.

    The walker scrolls by one screenful each time the caller comes back for
    the next Screenful; it never scrolls while the caller is still working
    on the current `todo`. Stops (StopIteration) on:

    - three consecutive dumps with the same item keys (top of history),
    - `max_screenfuls` hierarchy scans (safety cap).
    """

    def __init__(
        self,
        d,
        dump_nodes: Callable[[], list],
        find_items: Callable[[list], list],
        *,
        bottom_repeat_band: float = BOTTOM_REPEAT_BAND,
        max_screenfuls: int = MAX_SCREENFULS,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        self._d = d
        self._dump_nodes = dump_nodes
        self._find_items = find_items
        self._band = bottom_repeat_band
        self._max = max_screenfuls
        self._on_event = on_event

        self._size: tuple[int, int] | None = None
        self._index = 0            # screenfuls yielded so far
        self._scans = 0            # hierarchy scans so far
        self._last_keys: frozenset | None = None
        self._stuck = 0            # consecutive identical dumps
        self._need_scroll = False  # caller finished a screenful
        self._done = False

    def __iter__(self) -> "ChatHistoryWalker":
        return self

    def __next__(self) -> Screenful:
        if self._need_scroll:
            # The caller finished the previous screenful — move on before the
            # next scan. Any still-visible row from that screenful is either
            # in the bottom repeat band (filtered below) or has scrolled out.
            self._scroll()
            self._need_scroll = False

        while not self._done:
            if self._scans >= self._max:
                self._done = True
                self._event("max_screenfuls", f"Stopped after {self._max} hierarchy scans")
                break
            self._scans += 1

            nodes = self._dump_nodes()
            self._size = self._d.window_size()
            items = self._find_items(nodes)
            keys = frozenset(key for key, _ in items)

            if keys and keys == self._last_keys:
                self._stuck += 1
                if self._stuck >= STUCK_STRIKES:
                    self._event("top_of_history", "Reached the top of chat history")
                    self._done = True
                    break
                if self._stuck == 2:
                    self._event("stuck_flick", "List did not move with a slow drag — trying a faster flick")
                    self._scroll(flick=True)
                else:
                    self._event("scroll", "Scrolling one screenful to load older media...")
                    self._scroll()
                continue
            self._stuck = 0
            self._last_keys = keys

            if not items:
                self._event("scroll", "Scrolling one screenful to load older media...")
                self._scroll()
                continue

            todo = self._select_todo(items)
            if not todo:
                # Everything visible is a repeat from the previous screenful.
                self._event("scroll", "Scrolling one screenful to load older media...")
                self._scroll()
                continue

            sf = Screenful(index=self._index, size=self._size, nodes=nodes, todo=todo)
            self._index += 1
            self._need_scroll = True
            return sf

        raise StopIteration

    # -- internals ---------------------------------------------------------

    def _select_todo(self, items: list) -> list:
        """All items on the first screenful; on later screenfuls drop the
        bottom repeat band (it can only hold rows from the previous
        screenful — a brand-new item can only ever enter at the top)."""
        if self._index == 0:
            return list(items)
        limit = int((1.0 - self._band) * self._size[1])
        return [(key, (x, y)) for key, (x, y) in items if y <= limit]

    def _scroll(self, flick: bool = False) -> None:
        scroll_chat_one_screenful(self._d, size=self._size, flick=flick)

    def _event(self, name: str, message: str) -> None:
        if self._on_event:
            self._on_event(name, message)


# ---------------------------------------------------------------------------
# Manual smoke run: python -m imocli.scroll [serial]
# Scrolls the currently-open chat and prints what each screenful contains.
# ---------------------------------------------------------------------------

def _demo() -> None:  # pragma: no cover
    import sys
    import xml.etree.ElementTree as ET

    import uiautomator2 as u2

    serial = sys.argv[1] if len(sys.argv) > 1 else None
    d = u2.connect_usb(serial) if serial else u2.connect()

    def dump_nodes() -> list[dict]:
        try:
            xml = d.dump_hierarchy(compressed=True)
        except Exception:
            return []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        out = []
        for el in root.iter():
            b = el.attrib.get("bounds")
            if not b:
                continue
            try:
                x1, y1, x2, y2 = map(int, b.replace("[", "").replace("]", ",").split(",")[:4])
            except (ValueError, IndexError):
                continue
            out.append({"resourceId": el.attrib.get("resource-id", ""), "bounds": (x1, y1, x2, y2)})
        return out

    def find_items(nodes: list[dict]) -> list:
        rows = sorted(
            (n for n in nodes if n["resourceId"].endswith(":id/cl_message_root")),
            key=lambda n: n["bounds"][1],
            reverse=True,  # bottom-to-top = newest first
        )
        return [
            (tuple(n["bounds"]), ((n["bounds"][0] + n["bounds"][2]) // 2, (n["bounds"][1] + n["bounds"][3]) // 2))
            for n in rows
        ]

    walker = ChatHistoryWalker(
        d, dump_nodes, find_items,
        on_event=lambda name, msg: print(f"[{name}] {msg}"),
    )
    for sf in walker:
        print(f"screenful {sf.index}: {len(sf.todo)} item(s)")
        for key, (x, y) in sf.todo:
            print(f"  item at ({x}, {y})  key={key}")


if __name__ == "__main__":  # pragma: no cover
    _demo()
