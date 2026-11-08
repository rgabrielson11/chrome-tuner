"""Pushover notifications for chrome-tuner, rate-limited per event key."""
import logging
import os
import time

import aiohttp

log = logging.getLogger("tuner")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")
PUSHOVER_DEVICE = os.environ.get("PUSHOVER_DEVICE", "")
TITLE = os.environ.get("TUNER_NAME", "Chrome Tuner")
MIN_GAP = float(os.environ.get("NOTIFY_MIN_GAP", "300"))   # seconds between repeats of one key


class Notifier:
    def __init__(self):
        self.enabled = bool(PUSHOVER_TOKEN and PUSHOVER_USER)
        self._last = {}
        self.history = []          # (time, message) for the status page
        if not self.enabled:
            log.info("pushover: not configured (set PUSHOVER_TOKEN and PUSHOVER_USER)")

    async def send(self, message, key=None, priority=0, force=False):
        now = time.time()
        self.history = (self.history + [(now, message)])[-20:]
        log.info("notify: %s", message)
        if key and not force and now - self._last.get(key, 0) < MIN_GAP:
            return False
        if key:
            self._last[key] = now
        if not self.enabled:
            return False
        data = {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER, "title": TITLE,
                "message": message, "priority": str(priority)}
        if PUSHOVER_DEVICE:
            data["device"] = PUSHOVER_DEVICE
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post("https://api.pushover.net/1/messages.json", data=data) as r:
                    if r.status != 200:
                        log.warning("pushover: HTTP %s %s", r.status, (await r.text())[:200])
                        return False
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("pushover failed: %s", e)
            return False
