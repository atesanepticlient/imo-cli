"""Shared constants for the IMO automation CLI."""

MULTISPACE_PACKAGE = "com.dualspacepro.multispace"
IMO_PACKAGE = "com.imo.android.imoim"
STORY_PACKAGE = "com.imo.android.imoim.Story"

# The single-tile label searched for when Multi Space uses an unnumbered tile
# (older layout) instead of imo(1), imo(2), ...
MULTISPACE_TILE = "imo"

# Packages we expect to ever be in the foreground while the workflow runs.
# In Multi Space, cloned apps can run under the Multi Space package or IMO package.
EXPECTED_PACKAGES = [MULTISPACE_PACKAGE, IMO_PACKAGE, STORY_PACKAGE]

# Android permission selectors across old (Android 7-9) and new (Android 10-15) versions.
PERMISSION_BUTTONS = [
    "com.android.permissioncontroller:id/permission_allow_button",
    "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.android.permissioncontroller:id/permission_allow_one_time_button",
    "com.google.android.permissioncontroller:id/permission_allow_button",
    "com.google.android.permissioncontroller:id/permission_allow_foreground_only_button",
    "com.google.android.permissioncontroller:id/permission_allow_one_time_button",
    "com.android.packageinstaller:id/permission_allow_button",
    "android:id/button1",  # Standard system dialog OK/Allow
]

# Popups, update prompts, and interstitial ad close buttons (IMO & Multi Space).
POPUP_BUTTONS = [
    f"{IMO_PACKAGE}:id/close",
    f"{IMO_PACKAGE}:id/iv_close",
    f"{IMO_PACKAGE}:id/btn_close",
    f"{IMO_PACKAGE}:id/dialog_close",
    f"{IMO_PACKAGE}:id/iv_cancel",
    f"{IMO_PACKAGE}:id/btn_cancel",
    f"{IMO_PACKAGE}:id/negative_button",
    f"{MULTISPACE_PACKAGE}:id/iv_close",
    f"{MULTISPACE_PACKAGE}:id/ivClose",
    f"{MULTISPACE_PACKAGE}:id/img_close",
    f"{MULTISPACE_PACKAGE}:id/btn_close",
    f"{MULTISPACE_PACKAGE}:id/close",
    f"{MULTISPACE_PACKAGE}:id/dialog_close",
    f"{MULTISPACE_PACKAGE}:id/tt_video_ad_close",
    f"{MULTISPACE_PACKAGE}:id/tt_reward_full_count_down",
    "android:id/button2",  # Standard system dialog Cancel/Close
]

# Multi Space tile-grid resource ids.
LAYOUT_ROOT = f"{MULTISPACE_PACKAGE}:id/layout_root"
TV_APP_NAME = f"{MULTISPACE_PACKAGE}:id/tvAppName"
V_CLICK = f"{MULTISPACE_PACKAGE}:id/vClick"
RV_CONTENT = f"{MULTISPACE_PACKAGE}:id/rvContent"

# IMO resource ids (suffix matching is also used for virtualized instances).
MESSAGE_LAYOUT = f"{IMO_PACKAGE}:id/message_layout"
IV_STICKY_TOP = f"{IMO_PACKAGE}:id/iv_sticky_top"
HOME_PROFILE_PIC_WRAP = f"{IMO_PACKAGE}:id/home_profile_pic_wrap"
XIV_SWITCH_ACCOUNT = f"{IMO_PACKAGE}:id/xiv_switch_account"
ACCOUNT_LIST = f"{IMO_PACKAGE}:id/account_list"
ACCOUNT_ITEM_VIEW = f"{IMO_PACKAGE}:id/account_item_view"
B_TITLE_VIEW = f"{IMO_PACKAGE}:id/b_title_view"
B_DESC_VIEW = f"{IMO_PACKAGE}:id/b_desc_view"
TOGGLE = f"{IMO_PACKAGE}:id/toggle"
CL_MESSAGE_ROOT = f"{IMO_PACKAGE}:id/cl_message_root"
LIKE_BUTTON = f"{STORY_PACKAGE}:id/like_button"
STORY_BACK = f"{STORY_PACKAGE}:id/back"

# Suffixes for like controls across IMO versions and virtualized containers.
LIKE_BUTTON_SUFFIXES = (
    "like_button",
    "iv_like",
    "iv_zan",
    "rl_like",
    "ll_like",
    "ib_like",
    "btn_like",
    "like",
    "like_icon",
    "tv_like",
    "imkit_like",
    "story_like",
    "video_like",
    "feed_like",
    "iv_praise",
    "btn_praise",
    "heart",
    "iv_heart",
    "btn_heart",
    "view_like",
    "layout_like",
)

# Story like-control resource-ids seen across IMO versions.
LIKE_BUTTON_IDS = (
    # Story-package ids seen across IMO versions…
    f"{STORY_PACKAGE}:id/like_button",
    f"{STORY_PACKAGE}:id/iv_like",
    f"{STORY_PACKAGE}:id/iv_zan",
    f"{STORY_PACKAGE}:id/rl_like",
    f"{STORY_PACKAGE}:id/ll_like",
    f"{STORY_PACKAGE}:id/ib_like",
    f"{STORY_PACKAGE}:id/btn_like",
    f"{STORY_PACKAGE}:id/like",
    f"{STORY_PACKAGE}:id/like_icon",
    f"{STORY_PACKAGE}:id/tv_like",
    f"{STORY_PACKAGE}:id/imkit_like",
    f"{STORY_PACKAGE}:id/story_like",
    f"{STORY_PACKAGE}:id/video_like",
    f"{STORY_PACKAGE}:id/iv_praise",
    f"{STORY_PACKAGE}:id/btn_praise",
    f"{STORY_PACKAGE}:id/heart",
    f"{STORY_PACKAGE}:id/iv_heart",
    # …and the same controls living in the main package on some builds.
    f"{IMO_PACKAGE}:id/like",
    f"{IMO_PACKAGE}:id/iv_like",
    f"{IMO_PACKAGE}:id/iv_zan",
    f"{IMO_PACKAGE}:id/btn_like",
    f"{IMO_PACKAGE}:id/like_icon",
    f"{IMO_PACKAGE}:id/tv_like",
    f"{IMO_PACKAGE}:id/imkit_like",
    f"{IMO_PACKAGE}:id/story_like",
    f"{IMO_PACKAGE}:id/video_like",
    f"{IMO_PACKAGE}:id/feed_like",
    f"{IMO_PACKAGE}:id/iv_praise",
    f"{IMO_PACKAGE}:id/btn_praise",
    f"{IMO_PACKAGE}:id/heart",
    f"{IMO_PACKAGE}:id/iv_heart",
    f"{IMO_PACKAGE}:id/btn_heart",
    # …and Multi Space virtual container prefixes.
    f"{MULTISPACE_PACKAGE}:id/like_button",
    f"{MULTISPACE_PACKAGE}:id/iv_like",
    f"{MULTISPACE_PACKAGE}:id/iv_zan",
    f"{MULTISPACE_PACKAGE}:id/btn_like",
    f"{MULTISPACE_PACKAGE}:id/heart",
)

# Substrings matched against resource-id / content-desc / text when locating
# the like control in the hierarchy dump (covers ids that change per build).
LIKE_KEYWORDS = ("like", "zan", "heart", "praise", "点赞", "赞", "লাইক")

# Negative keywords: elements containing these must NEVER be matched as a like button.
NON_LIKE_BLACKLIST_KEYWORDS = (
    "market",
    "shop",
    "cart",
    "store",
    "buy",
    "nav",
    "menu",
    "more",
    "share",
    "comment",
    "gift",
    "send",
    "emoji",
    "input",
    "voice",
    "audio",
    "back",
    "setting",
)

# Max RGB distance between the like button's color before and after a tap that
# still counts as "no change" (already-liked / swallowed tap). A real like
# toggles the heart to a brand color, hundreds of units away.
LIKE_COLOR_THRESHOLD = 12.0

# ---------------------------------------------------------------------------
# Like-accuracy settings. Every value can be overridden with an environment
# variable of the same name (e.g. `set IMO_VIEWER_SETTLE=0.5`) so a slow phone
# can be given more time without editing code.
# ---------------------------------------------------------------------------
import os as _os


def _env_float(name: str, default: float) -> float:
    try:
        return float(_os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# How long to wait for a tapped media item to open (chat list disappears).
# Old value was 0.8s, which on a slow phone / Multi Space dropped the item.
VIEWER_OPEN_TIMEOUT = _env_float("IMO_VIEWER_OPEN_TIMEOUT", 3.0)

# Pause after the viewer opens so its controls are ready to take a tap.
# A tap that lands while the viewer is still animating in is swallowed.
VIEWER_SETTLE = _env_float("IMO_VIEWER_SETTLE", 0.15)

# After tapping like: how many times / how often to re-check that the heart
# turned red before deciding the tap was swallowed.
LIKE_VERIFY_POLLS = int(_env_float("IMO_LIKE_VERIFY_POLLS", 4))
LIKE_VERIFY_INTERVAL = _env_float("IMO_LIKE_VERIFY_INTERVAL", 0.15)

# Fraction of the heart-icon centre patch that must be heart-red to call the
# post "liked". An outline heart is mostly transparent, a liked one is solid.
HEART_LIKED_FRACTION = _env_float("IMO_HEART_LIKED_FRACTION", 0.35)

# Max average colour change in the icon patch that still counts as "nothing
# happened" (the tap was swallowed) rather than "something changed".
LIKE_STATIC_THRESHOLD = _env_float("IMO_LIKE_STATIC_THRESHOLD", 18.0)

# Total tap attempts on one post before giving up (and leaving it for the
# end-of-screenful audit).
LIKE_MAX_ATTEMPTS = int(_env_float("IMO_LIKE_MAX_ATTEMPTS", 3))

# Folder where screenshots of posts that could NOT be confirmed liked are
# saved, so a miss can be inspected. Set IMO_DEBUG_DIR= (empty) to disable.
DEBUG_DIR = _os.environ.get("IMO_DEBUG_DIR", "imo_debug")

# Media thumbnails in chat rows — photos and videos share these ids.
THUMB_IDS = (
    f"{IMO_PACKAGE}:id/iv_thumb",
    f"{IMO_PACKAGE}:id/iv_image",
    f"{IMO_PACKAGE}:id/iv_photo",
    f"{MULTISPACE_PACKAGE}:id/iv_thumb",
    f"{MULTISPACE_PACKAGE}:id/iv_image",
    f"{MULTISPACE_PACKAGE}:id/iv_photo",
)

# Suffixes for media thumbnails.
THUMB_SUFFIXES = ("iv_thumb", "iv_image", "iv_photo")

# The dice emoji used as a task-complete signal: when this many CONSECUTIVE
# chat messages contain it, the work in the current chat is done — stop liking
# here and move on to the next Multi Space clone.
DICE_EMOJI = "\U0001F3B2"  # 🎲
DICE_COMPLETION_RUN = 3

# Chat completion marker: three separate 👍 message rows.
THUMBS_UP_EMOJI = "👍"
THUMBS_UP_COMPLETION_RUN = 3

# Go-to-new-message control keywords (semantic match for the "jump to newest"
# pill/button, across EN + Bangla builds).
NEW_MESSAGE_KEYWORDS = (
    "new message",
    "new messages",
    "new msg",
    "new msgs",
    "latest message",
    "latest messages",
    "go to latest",
    "go to newest",
    "scroll to latest",
    "scroll to newest",
    "নতুন মেসেজ",
    "নতুন বার্তা",
)
