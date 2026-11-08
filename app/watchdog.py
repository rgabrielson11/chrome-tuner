"""Channel lock + stream watchdog for chrome-tuner.

Off at container start. When the lock is turned on, the station currently
playing in the tuner browser is remembered and checked every CHECK_INTERVAL:
  - Chrome alive (relaunches are noticed)
  - still on a YouTube TV watch page, on the locked station
  - the video is actually advancing (not paused/ended/stalled)
  - "Still watching?" style prompts are dismissed automatically
Problems are recovered in steps (resume -> reload -> re-open the station's live
airing from the guide) and reported via Pushover.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger("tuner")

CHECK_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "10"))
STALL_SECS = float(os.environ.get("WATCHDOG_STALL_SECS", "30"))
MAX_FAST_ATTEMPTS = 5          # then retry every SLOW_RETRY seconds
SLOW_RETRY = 120

# Runs in the page: video health across all <video> elements (largest playing
# one wins) and dismissal of idle / "still watching" prompts.
JS_HEALTH = r"""
() => {
  const vids = [...document.querySelectorAll('video')];
  let best = null, bestArea = -1;
  for (const v of vids) {
    const r = v.getBoundingClientRect();
    const a = r.width * r.height;
    if (a > bestArea) { best = v; bestArea = a; }
  }
  let dismissed = null;
  const re = /(still watching|continue watching|keep watching|i'?m still here|yes,? i'?m here|resume|continue)/i;
  for (const b of document.querySelectorAll('button, [role=button], tp-yt-paper-button, ytu-button')) {
    const t = (b.innerText || b.getAttribute('aria-label') || '').trim();
    if (t && t.length < 40 && re.test(t) && b.offsetParent !== null) {
      b.click(); dismissed = t; break;
    }
  }
  return {
    url: location.href,
    has_video: !!best,
    paused: best ? best.paused : null,
    ended: best ? best.ended : null,
    ready: best ? best.readyState : null,
    time: best ? best.currentTime : null,
    w: best ? best.videoWidth : 0,
    dismissed,
  };
}
"""


def _fmt_secs(s):
    s = int(s)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


class Watchdog:
    def __init__(self, browser, guide, notifier, tuner_status):
        self.browser = browser
        self.guide = guide
        self.notify = notifier
        self.tuner_status = tuner_status       # callable -> Tuner.status()
        self.locked = False
        self.station = None
        self.station_label = ""
        self.lock_url = None
        self.locked_at = None
        self.state = "unlocked"                 # unlocked | ok | problem | recovering | failed
        self.problem = None
        self.problem_since = None
        self.attempts = 0
        self.next_attempt = 0.0
        self._last_time = None
        self._last_advance = time.time()     # stall clock (reset to give recovery a grace period)
        self._progress_at = 0.0              # last time the video really advanced
        self.attempt_at = 0.0
        self._failed_sent = False
        self._launches_seen = 0
        self._task = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ control
    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def set_lock(self, on):
        async with self._lock:
            if not on:
                if self.locked:
                    log.info("watchdog: lock released (%s)", self.station_label)
                self.locked = False
                self.state = "unlocked"
                self.problem = None
                return True, "Lock released"
            page = await self.browser.active_page()
            url = page.url
            if "tv.youtube.com/watch/" not in url:
                return False, "Start a channel in the tuner browser first (no YouTube TV stream open)."
            self.station = self.guide.current
            self.station_label = self.guide.station_name(self.station) or "current stream"
            self.lock_url = url
            self.locked = True
            self.locked_at = time.time()
            self.state = "ok"
            self.problem = None
            self.attempts = 0
            self._last_time = None
            self._last_advance = time.time()
            self._launches_seen = self.browser.launches
            log.info("watchdog: locked to %s (%s)", self.station_label, url)
            await self.notify.send(f"Channel locked: {self.station_label}", key="locked", force=True)
            return True, f"Locked to {self.station_label}"

    def status(self):
        return {
            "locked": self.locked,
            "station": self.station_label if self.locked else None,
            "state": self.state,
            "problem": self.problem,
            "problem_for": _fmt_secs(time.time() - self.problem_since) if self.problem_since and self.problem else None,
            "attempts": self.attempts,
            "locked_since": time.strftime("%H:%M", time.localtime(self.locked_at)) if self.locked and self.locked_at else None,
        }

    # ------------------------------------------------------------ loop
    async def _loop(self):
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            if not self.locked:
                continue
            try:
                async with self._lock:
                    if self.locked:
                        await self._check()
            except Exception as e:  # noqa: BLE001
                log.warning("watchdog check error: %s", str(e).splitlines()[0])

    async def _check(self):
        problem = None

        # Chrome relaunched since the last check?
        if self.browser.launches != self._launches_seen:
            self._launches_seen = self.browser.launches
            problem = "Chrome restarted"

        page = await self.browser.active_page()
        try:
            h = await asyncio.wait_for(page.evaluate(JS_HEALTH), 5)
        except Exception as e:  # noqa: BLE001
            h = None
            problem = problem or f"page not responding ({str(e).splitlines()[0][:60]})"

        if h:
            if h.get("dismissed"):
                log.info("watchdog: dismissed prompt '%s'", h["dismissed"])
            now = time.time()
            t = h.get("time")
            if t is not None and self._last_time is not None and t > self._last_time + 0.5:
                self._last_advance = now
                self._progress_at = now
            self._last_time = t

            if "tv.youtube.com/watch/" not in h["url"]:
                problem = problem or "left the channel page"
            elif self.station and self.guide.current and self.guide.current != self.station:
                problem = problem or f"switched to {self.guide.station_name() or 'another channel'}"
            elif not h["has_video"]:
                problem = problem or "no video on the page"
            elif h["ended"]:
                problem = problem or "stream ended"
            elif now - self._last_advance > STALL_SECS:
                why = "paused" if h["paused"] else "stalled"
                problem = problem or f"video {why} for {_fmt_secs(now - self._last_advance)}"

        if not problem and self.problem and self._progress_at <= max(self.attempt_at, self.problem_since or 0):
            # nothing visibly wrong yet, but the video hasn't moved since we intervened:
            # still in the grace period - don't call it recovered until it plays
            if time.time() - self._last_advance > STALL_SECS:
                problem = self.problem
            else:
                return
        if problem:
            await self._handle_problem(problem, page, h)
        elif self.problem:
            took = _fmt_secs(time.time() - (self.problem_since or time.time()))
            log.info("watchdog: recovered after %s (%s)", took, self.problem)
            await self.notify.send(f"Recovered: back on {self.station_label} after {took} "
                                   f"(was: {self.problem})", key="recovered", force=True)
            self.problem = None
            self.problem_since = None
            self.attempts = 0
            self.state = "ok"

    async def _handle_problem(self, problem, page, h):
        now = time.time()
        if not self.problem:
            self.problem = problem
            self.problem_since = now
            self.attempts = 0
            self.next_attempt = 0
            self._failed_sent = False
            st = self.tuner_status()
            watching = f" while {st['viewers']} viewer(s) watching" if st.get("viewers") else ""
            log.warning("watchdog: %s%s", problem, watching)
            await self.notify.send(f"Problem on {self.station_label}: {problem}{watching}. Recovering...",
                                   key="problem", priority=0)
        else:
            self.problem = problem       # keep the description current
        if now < self.next_attempt:
            return
        self.attempts += 1
        self.attempt_at = now
        self.state = "recovering"
        step = self.attempts
        try:
            if step == 1 and h and h.get("has_video") and h.get("paused"):
                log.info("watchdog: attempt %d - resume playback", step)
                await page.evaluate("() => { for (const v of document.querySelectorAll('video')) "
                                    "{ v.muted = false; const p = v.play(); if (p) p.catch(()=>{}); } }")
            elif step <= 2 and "tv.youtube.com/watch/" in (h or {}).get("url", "") and \
                    not problem.startswith("switched"):
                log.info("watchdog: attempt %d - reload page", step)
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            else:
                url = None
                if self.station:
                    if not self.guide.live_url(self.station):
                        await self.guide.fetch(self.browser)
                    url = self.guide.live_url(self.station)
                url = url or self.lock_url
                log.info("watchdog: attempt %d - reopen %s", step, url)
                await self.browser.goto(url)
        except Exception as e:  # noqa: BLE001
            log.warning("watchdog recovery step failed: %s", str(e).splitlines()[0])
        self._last_time = None
        self._last_advance = time.time()      # give the stream time to start
        if self.attempts >= MAX_FAST_ATTEMPTS:
            if not self._failed_sent:
                self._failed_sent = True
                await self.notify.send(f"Still failing on {self.station_label} after {self.attempts} "
                                       f"attempts ({problem}). Will keep retrying every "
                                       f"{SLOW_RETRY // 60} min.", key="failed", priority=1)
            self.state = "failed"
            self.next_attempt = now + SLOW_RETRY
        else:
            self.next_attempt = now + CHECK_INTERVAL * (1 + self.attempts)   # 20s, 30s, 40s...
