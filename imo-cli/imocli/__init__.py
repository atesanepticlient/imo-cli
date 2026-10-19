"""imo — concurrent IMO automation CLI built on uiautomator2.

Architecture vs. the original Next.js/Appium dashboard:
- No Appium server: uiautomator2 (atx-agent + UiAutomator2 instrumentation over
  adb) talks to each device directly, one hop instead of
  browser -> Next.js -> WebdriverIO -> Appium -> uiautomator2-server -> device.
- Concurrent: every connected device runs its workflow in its own thread
  (the original `runAllDevices` awaited each device sequentially).
- Same structured NDJSON event protocol, so any consumer of the old stream
  can parse the new one unchanged.
"""

__version__ = "0.1.0"
