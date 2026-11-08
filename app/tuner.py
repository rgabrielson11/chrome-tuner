#!/usr/bin/env python3
"""chrome-tuner: a one-tuner virtual HDHomeRun that plays a web page in Chrome
and serves the screen + audio as MPEG-TS (H.264/AAC) for Emby Live TV.

Endpoints
  /discover.json /lineup.json /lineup_status.json /device.xml   HDHomeRun emulation
  /auto/v<num>  /stream/<num>                                   live MPEG-TS
  /playlist.m3u /epg.xml                                        M3U tuner + XMLTV guide
  /                                                             status + login helper
"""
import asyncio
import collections
import hashlib
import html
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from aiohttp import web
from playwright.async_api import async_playwright

from guide import Guide
from notify import Notifier
from watchdog import Watchdog

# ---------------------------------------------------------------- settings
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CHANNELS_FILE = CONFIG_DIR / "channels.yaml"
SCRIPTS_DIR = CONFIG_DIR / "scripts"
PROFILE_DIR = CONFIG_DIR / "chrome"

PORT = int(os.environ.get("PORT", "5004"))
NOVNC_PORT = int(os.environ.get("NOVNC_PORT", "6080"))
RESOLUTION = os.environ.get("RESOLUTION", "1920x1080")
FRAMERATE = os.environ.get("FRAMERATE", "30")
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "6M")
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "160k")
ENCODER = os.environ.get("ENCODER", "auto").lower()          # auto | nvenc | x264
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "20"))   # seconds after last viewer
TUNER_NAME = os.environ.get("TUNER_NAME", "Chrome Tuner")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
DEVICE_ID = os.environ.get("DEVICE_ID") or hashlib.md5(TUNER_NAME.encode()).hexdigest()[:8].upper()
CHROME_BIN = os.environ.get("CHROME_BIN", "/usr/bin/google-chrome-stable")
EXTRA_CHROME_ARGS = os.environ.get("CHROME_ARGS", "").split()
START_URL = os.environ.get("START_URL", "https://tv.youtube.com/live")
DEBUG = os.environ.get("DEBUG", "") not in ("", "0", "false")

TUNER = None
GUIDE = None
WATCHER = None
NOTIFIER = None
WATCHDOG = None
QUEUE_MAX = 400          # ~25 MB of buffered TS per viewer before it's dropped
READ_SIZE = 188 * 348    # ~64 KB, TS-packet aligned
W, H = (int(x) for x in RESOLUTION.lower().split("x"))

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tuner")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# ---------------------------------------------------------------- channels
def load_channels():
    try:
        data = yaml.safe_load(CHANNELS_FILE.read_text()) or {}
    except Exception as e:  # noqa: BLE001
        log.error("channels.yaml unreadable: %s", e)
        return []
    out = []
    for c in data.get("channels") or []:
        if not c:
            continue
        c["url"] = str(c.get("url") or "current")
        c["number"] = str(c.get("number", len(out) + 1))
        c.setdefault("name", f"Channel {c['number']}")
        c["click"] = c.get("click") or []
        out.append(c)
    return out


def find_channel(num):
    return next((c for c in load_channels() if c["number"] == str(num)), None)


# ---------------------------------------------------------------- page JS
JS_FIND = r"""
() => {
  let best = null, bestScore = 0;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    const area = Math.max(r.width * r.height, (v.videoWidth || 0) * (v.videoHeight || 0) / 4);
    if (area < 10000) continue;                       // ignore thumbnails / ad pixels
    const playing = !v.paused && v.readyState >= 2;
    const score = area * (playing ? 4 : 1) * (v.readyState >= 1 ? 2 : 1);
    if (score > bestScore) { best = v; bestScore = score; }
  }
  for (const e of document.querySelectorAll('[data-tuner-video]'))
    if (e !== best) e.removeAttribute('data-tuner-video');
  if (!best) return null;
  best.setAttribute('data-tuner-video', '1');
  return {score: bestScore, paused: best.paused, ready: best.readyState,
          w: best.videoWidth, h: best.videoHeight};
}
"""

JS_ACTIVATE = r"""
() => {
  const v = document.querySelector('[data-tuner-video]');
  if (!v) return false;
  if (v.muted) v.muted = false;
  if (v.volume < 1) v.volume = 1;
  if (v.paused) { const p = v.play(); if (p) p.catch(() => {}); }
  return true;
}
"""

JS_FILL = r"""
(sel) => {
  if (!document.getElementById('__tuner_css')) {
    const s = document.createElement('style');
    s.id = '__tuner_css';
    s.textContent = `
      [data-tuner-fill]{position:fixed!important;inset:0!important;width:100vw!important;
        height:100vh!important;max-width:none!important;max-height:none!important;margin:0!important;
        padding:0!important;border:0!important;z-index:2147483647!important;background:#000!important;
        object-fit:contain!important;transform:none!important;visibility:visible!important;opacity:1!important}
      html,body{overflow:hidden!important;background:#000!important}`;
    document.documentElement.appendChild(s);
  }
  const el = document.querySelector(sel);
  if (!el) return false;
  el.setAttribute('data-tuner-fill', '1');
  for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
    const cs = getComputedStyle(a);
    if (cs.transform !== 'none' || cs.filter !== 'none' || cs.perspective !== 'none' ||
        (cs.contain && cs.contain !== 'none')) {
      for (const p of ['transform', 'filter', 'perspective', 'contain'])
        a.style.setProperty(p, 'none', 'important');
    }
  }
  return true;
}
"""

JS_FULLSCREEN = r"""
async () => {
  const el = document.querySelector('[data-tuner-fs]');
  if (!el) return 'none';
  if (document.fullscreenElement === el) return 'already';
  try { await el.requestFullscreen({navigationUI: 'hide'}); return 'ok'; }
  catch (e) { return 'err: ' + e.message; }
}
"""

JS_CLICK_TARGET = r"""
() => {
  if (document.getElementById('__tuner_click')) return;
  const d = document.createElement('div');
  d.id = '__tuner_click';
  d.style.cssText = 'position:fixed;left:0;top:0;width:6px;height:6px;z-index:2147483647;background:transparent';
  d.addEventListener('click', e => { e.stopPropagation(); e.preventDefault(); }, true);
  document.documentElement.appendChild(d);
}
"""

SLATE_HTML = """<html><body style="margin:0;background:#000;color:#ccc;display:flex;
align-items:center;justify-content:center;height:100vh;font:48px sans-serif">
Tuning {name}&hellip;</body></html>"""


# ---------------------------------------------------------------- browser
class Browser:
    def __init__(self):
        self.proc = None
        self.pw = None
        self.browser = None
        self.page = None
        self.auto_close_popups = False
        self.guide_pages = set()
        self.on_player = None
        self.launches = 0
        self._lock = asyncio.Lock()

    def _prepare_profile(self):
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # container hostnames change, so stale locks would make Chrome refuse the profile
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = PROFILE_DIR / name
            if p.is_symlink() or p.exists():
                p.unlink()
        prefs = PROFILE_DIR / "Default" / "Preferences"
        if prefs.exists():
            try:
                data = json.loads(prefs.read_text())
                data.setdefault("profile", {})["exit_type"] = "Normal"
                data["profile"]["exited_cleanly"] = True
                prefs.write_text(json.dumps(data))
            except Exception as e:  # noqa: BLE001
                log.warning("could not patch Chrome Preferences: %s", e)

    def alive(self):
        return (self.proc is not None and self.proc.returncode is None
                and self.browser is not None and self.browser.is_connected())

    async def ensure(self):
        async with self._lock:
            if self.alive() and self.page and not self.page.is_closed():
                return self.page
            if self.alive():
                ctx = self.browser.contexts[0]
                self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                return self.page
            await self._launch()
            return self.page

    async def _launch(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self._prepare_profile()
        args = [
            CHROME_BIN,
            f"--user-data-dir={PROFILE_DIR}",
            "--remote-debugging-port=9222",
            "--remote-debugging-address=127.0.0.1",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            "--test-type",
            "--window-position=0,0",
            f"--window-size={W},{H}",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=Translate,MediaRouter,GlobalMediaControls,HardwareMediaKeyHandling",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--hide-scrollbars",
            "--noerrdialogs",
            "--password-store=basic",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            *EXTRA_CHROME_ARGS,
            START_URL,
        ]
        log.info("launching Chrome")
        self.launches += 1
        out = None if DEBUG else asyncio.subprocess.DEVNULL
        self.proc = await asyncio.create_subprocess_exec(*args, stdout=out, stderr=out)
        if self.pw is None:
            self.pw = await async_playwright().start()
        last = None
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                self.browser = await self.pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise RuntimeError(f"Chrome did not come up: {last}")
        ctx = self.browser.contexts[0]
        ctx.on("page", self._on_page)
        ctx.on("response", self._on_response)
        pages = ctx.pages
        self.page = pages[0] if pages else await ctx.new_page()
        for p in pages[1:]:
            await p.close()
        log.info("Chrome ready (%s)", self.browser.version)

    def _on_response(self, r):
        if "youtubei/v1/player?" not in r.url or self.on_player is None:
            return
        try:
            page = r.frame.page
        except Exception:  # noqa: BLE001
            return
        if page in self.guide_pages or "/watch/" not in page.url:
            return       # ignore guide tabs and hover previews in the guide grid
        asyncio.create_task(self._player_json(r))

    async def _player_json(self, r):
        try:
            data = await r.json()
            await self.on_player(self, data)
        except Exception as e:  # noqa: BLE001
            log.debug("player response: %s", e)

    def _on_page(self, p):
        if self.auto_close_popups and p is not self.page and p not in self.guide_pages:
            log.info("closing popup %s", p.url)
            asyncio.create_task(p.close())

    async def goto(self, url, timeout=45000):
        page = await self.ensure()
        for p in page.context.pages:
            if p is not page and p not in self.guide_pages:
                await p.close()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log.warning("navigation to %s: %s", url, str(e).splitlines()[0])
        return page

    async def slate(self, name):
        page = await self.ensure()
        try:
            await page.set_content(SLATE_HTML.format(name=html.escape(name)), timeout=5000)
        except Exception:  # noqa: BLE001
            pass

    async def active_page(self):
        """The tab the user has in front (what x11grab sees), ignoring guide tabs."""
        page = await self.ensure()
        cands = [p for p in page.context.pages
                 if p not in self.guide_pages and not p.url.startswith(("chrome:", "devtools:"))]
        if len(cands) > 1:
            for p in cands:
                try:
                    if await asyncio.wait_for(p.evaluate("() => document.visibilityState"), 2) == "visible":
                        return p
                except Exception:  # noqa: BLE001
                    continue
        return cands[0] if cands else page

    async def exit_fullscreen(self):
        try:
            page = await self.active_page()
            await asyncio.wait_for(page.evaluate(
                "() => document.fullscreenElement && document.exitFullscreen()"), 5)
        except Exception as e:  # noqa: BLE001
            log.debug("exit fullscreen: %s", e)

    async def blank(self):
        try:
            page = await self.ensure()
            await page.goto("about:blank", timeout=10000)
        except Exception as e:  # noqa: BLE001
            log.warning("blank: %s", e)


# ---------------------------------------------------------------- page driver
async def _eval(target, js, arg=None, timeout=4.0):
    coro = target.evaluate(js) if arg is None else target.evaluate(js, arg)
    return await asyncio.wait_for(coro, timeout)


async def ensure_video(page, state):
    """Find the main video, unmute/play it and make it fill the frame."""
    best = None
    for frame in page.frames:
        if frame.is_detached():
            continue
        try:
            info = await _eval(frame, JS_FIND)
        except Exception:  # noqa: BLE001
            continue
        if info and (best is None or info["score"] > best[1]["score"]):
            best = (frame, info)
    if not best:
        if not state.get("warned"):
            log.info("no video found on page yet")
            state["warned"] = True
        return False
    frame, info = best
    state["warned"] = False
    if state.get("last_frame") != frame.url:
        log.info("video found (%sx%s, %s) in %s", info["w"], info["h"],
                 "paused" if info["paused"] else "playing",
                 "main page" if frame is page.main_frame else frame.url[:80])
        state["last_frame"] = frame.url
    await _eval(frame, JS_ACTIVATE)

    # Walk up to the top frame, marking the element to fill/fullscreen at each level
    try:
        if frame is page.main_frame:
            await _eval(frame, "() => { for (const e of document.querySelectorAll('[data-tuner-fs]')) e.removeAttribute('data-tuner-fs'); document.querySelector('[data-tuner-video]').setAttribute('data-tuner-fs','1'); }")
        else:
            await _eval(frame, JS_FILL, "[data-tuner-video]")
            child = frame
            while child.parent_frame is not None:
                parent = child.parent_frame
                el = await child.frame_element()
                if parent is page.main_frame:
                    await el.evaluate("(e) => { for (const x of document.querySelectorAll('[data-tuner-fs]')) if (x !== e) x.removeAttribute('data-tuner-fs'); e.setAttribute('data-tuner-fs','1'); e.setAttribute('allowfullscreen',''); }")
                else:
                    await el.evaluate("(e) => e.setAttribute('data-tuner-mark','1')")
                    await _eval(parent, JS_FILL, "[data-tuner-mark]")
                child = parent
    except Exception as e:  # noqa: BLE001
        log.debug("marking failed: %s", e)
        return True

    # Fullscreen needs user activation: click a tiny overlay we inject (a real input
    # event), then request fullscreen while the activation is still valid.
    try:
        if await _eval(page.main_frame, "() => !!document.fullscreenElement && document.fullscreenElement.hasAttribute('data-tuner-fs')"):
            state["fs"] = "already"
            return True
        await _eval(page.main_frame, JS_CLICK_TARGET)
        await page.mouse.click(2, 2)
        await _eval(page.main_frame, "() => { const e = document.getElementById('__tuner_click'); if (e) e.remove(); }")
        r = await asyncio.wait_for(page.evaluate(JS_FULLSCREEN), 5)
        if r not in ("already",) and r != state.get("fs"):
            log.info("fullscreen: %s", r)
        state["fs"] = r
        if r and r.startswith("err"):
            await _eval(page.main_frame, JS_FILL, "[data-tuner-fs]")
    except Exception as e:  # noqa: BLE001
        log.debug("fullscreen failed: %s", e)
    return True


def is_live_page(ch):
    return str(ch.get("url", "")).strip().lower() in ("current", "")


async def drive_channel(browser, ch, is_current):
    """Navigate to the channel page and keep its video playing full-frame.
    A channel with url: current captures whatever page is already open."""
    if is_live_page(ch):
        log.info("capturing the page already open in the tuner browser")
        state = {}
        while is_current():
            try:
                await ensure_video(await browser.active_page(), state)
            except Exception as e:  # noqa: BLE001
                log.warning("driver: %s", str(e).splitlines()[0])
                state = {}
            await asyncio.sleep(3)
        return
    await browser.slate(ch["name"])
    browser.auto_close_popups = True
    page = await browser.goto(ch["url"])
    for sel in ch["click"]:
        if not is_current():
            return
        try:
            await page.locator(sel).first.click(timeout=15000)
            log.info("clicked %s", sel)
            await asyncio.sleep(1.5)
        except Exception as e:  # noqa: BLE001
            log.warning("click %s failed: %s", sel, str(e).splitlines()[0])
    if ch.get("script"):
        try:
            js = (SCRIPTS_DIR / ch["script"]).read_text()
            await page.evaluate(js)
            log.info("ran script %s", ch["script"])
        except Exception as e:  # noqa: BLE001
            log.warning("script %s failed: %s", ch["script"], e)

    state, started = {}, time.monotonic()
    while is_current():
        try:
            page = await browser.ensure()
            if page.url in ("about:blank", "") and time.monotonic() - started > 5:
                log.warning("page lost (Chrome restarted?) - reloading channel")
                page = await browser.goto(ch["url"])
                state = {}
            await ensure_video(page, state)
        except Exception as e:  # noqa: BLE001
            log.warning("driver: %s", str(e).splitlines()[0])
            state = {}
        await asyncio.sleep(2 if time.monotonic() - started < 60 else 8)


# ---------------------------------------------------------------- encoder
async def detect_encoder():
    if ENCODER in ("x264", "nvenc"):
        return ENCODER
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode == 0:
        return "nvenc"
    log.warning("NVENC unavailable, falling back to libx264: %s", err.decode()[-200:].strip())
    return "x264"


def ffmpeg_cmd(encoder):
    br = VIDEO_BITRATE
    gop = str(int(FRAMERATE) * 2)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-thread_queue_size", "1024", "-f", "x11grab", "-draw_mouse", "0",
        "-framerate", FRAMERATE, "-video_size", RESOLUTION, "-i", ":99.0+0,0",
        "-thread_queue_size", "1024", "-f", "pulse", "-ac", "2", "-ar", "48000",
        "-i", "virtual.monitor",
        "-map", "0:v", "-map", "1:a",
        "-vf", "format=yuv420p",
    ]
    if encoder == "nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-profile:v", "high",
                "-rc", "cbr", "-b:v", br, "-maxrate", br, "-bufsize", br,
                "-g", gop, "-bf", "0", "-forced-idr", "1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-profile:v", "high", "-b:v", br, "-maxrate", br, "-bufsize", br,
                "-g", gop, "-keyint_min", gop]
    cmd += ["-af", "aresample=async=1000", "-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", "48000",
            "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0",
            "-flush_packets", "1", "pipe:1"]
    return cmd


# ---------------------------------------------------------------- tuner
def _close_queue(q):
    while not q.empty():
        q.get_nowait()
    q.put_nowait(None)


class Tuner:
    def __init__(self, browser, encoder):
        self.browser = browser
        self.encoder = encoder
        self.lock = asyncio.Lock()
        self.proc = None
        self.channel = None
        self.clients = set()
        self.gen = 0
        self.driver = None
        self.idle = None
        self.since = None
        self.stderr = collections.deque(maxlen=20)

    def status(self):
        return {
            "encoder": self.encoder,
            "streaming": self.proc is not None and self.proc.returncode is None,
            "channel": self.channel and {k: self.channel[k] for k in ("number", "name", "url")},
            "viewers": len(self.clients),
            "since": self.since,
            "station": GUIDE.station_name() if GUIDE else None,
            "guide_age_min": int((time.time() - GUIDE.fetched) / 60) if GUIDE and GUIDE.fetched else None,
            "lock": WATCHDOG.status() if WATCHDOG else None,
            "pushover": bool(NOTIFIER and NOTIFIER.enabled),
        }

    async def attach(self, ch):
        async with self.lock:
            if self.idle:
                self.idle.cancel()
                self.idle = None
            if self.proc is None or self.proc.returncode is not None:
                await self._start_ffmpeg()
                self.channel = None
            if self.channel is None or self.channel["number"] != ch["number"]:
                if self.channel:
                    log.info("switching %s -> %s", self.channel["name"], ch["name"])
                for q in self.clients:
                    _close_queue(q)
                self.clients.clear()
                self.channel = ch
                self.since = datetime.now().isoformat(timespec="seconds")
                self.gen += 1
                gen = self.gen
                if self.driver:
                    self.driver.cancel()
                self.driver = asyncio.create_task(
                    drive_channel(self.browser, ch, lambda: self.gen == gen))
                log.info("tuned %s (%s)", ch["name"], ch["url"])
            q = asyncio.Queue(maxsize=QUEUE_MAX)
            self.clients.add(q)
            log.info("viewer connected (%d)", len(self.clients))
            return q

    def detach(self, q):
        if q in self.clients:
            self.clients.discard(q)
            log.info("viewer left (%d)", len(self.clients))
        if not self.clients and self.idle is None and self.channel is not None:
            self.idle = asyncio.create_task(self._idle_stop())

    async def _idle_stop(self):
        await asyncio.sleep(IDLE_TIMEOUT)
        async with self.lock:
            self.idle = None
            if not self.clients:
                log.info("idle - releasing tuner")
                await self._stop()

    async def _stop(self):
        self.gen += 1
        if self.driver:
            self.driver.cancel()
            self.driver = None
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        live = self.channel is not None and is_live_page(self.channel)
        self.channel = None
        self.since = None
        self.browser.auto_close_popups = False
        if live:
            await self.browser.exit_fullscreen()
        else:
            await self.browser.blank()

    async def _start_ffmpeg(self):
        cmd = ffmpeg_cmd(self.encoder)
        log.debug("ffmpeg: %s", " ".join(cmd))
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        asyncio.create_task(self._pump(self.proc))
        asyncio.create_task(self._drain_stderr(self.proc))
        log.info("encoder started (%s, %s @ %sfps, %s)", self.encoder, RESOLUTION, FRAMERATE, VIDEO_BITRATE)

    async def _pump(self, proc):
        try:
            while True:
                data = await proc.stdout.read(READ_SIZE)
                if not data:
                    break
                for q in list(self.clients):
                    try:
                        q.put_nowait(data)
                    except asyncio.QueueFull:
                        log.warning("viewer too slow - dropping")
                        self.clients.discard(q)
                        _close_queue(q)
        finally:
            rc = await proc.wait()
            if self.proc is proc:
                log.error("ffmpeg exited (%s): %s", rc, " | ".join(self.stderr))
                if self.clients and NOTIFIER:
                    asyncio.create_task(NOTIFIER.send(
                        f"Encoder stopped with {len(self.clients)} viewer(s) watching: "
                        f"{(self.stderr[-1] if self.stderr else 'exit ' + str(rc))[:150]}",
                        key="encoder", priority=1))
                for q in self.clients:
                    _close_queue(q)
                self.clients.clear()
                self.proc = None
                self.channel = None
                self.gen += 1

    async def _drain_stderr(self, proc):
        async for line in proc.stderr:
            s = line.decode(errors="replace").strip()
            if s:
                self.stderr.append(s)
                log.debug("ffmpeg: %s", s)


# ---------------------------------------------------------------- HTTP
def base_url(request):
    return BASE_URL or f"http://{request.host}"


async def discover(request):
    b = base_url(request)
    return web.json_response({
        "FriendlyName": TUNER_NAME, "Manufacturer": "Silicondust", "ManufacturerURL": "https://www.silicondust.com/",
        "ModelNumber": "HDTC-2US", "FirmwareName": "hdhomeruntc_atsc", "FirmwareVersion": "20200101",
        "DeviceID": DEVICE_ID, "DeviceAuth": "chrometuner", "TunerCount": 1,
        "BaseURL": b, "LineupURL": f"{b}/lineup.json",
    })


async def lineup(request):
    b = base_url(request)
    return web.json_response([
        {"GuideNumber": c["number"], "GuideName": c["name"], "URL": f"{b}/auto/v{c['number']}", "HD": 1}
        for c in load_channels()])


async def lineup_status(request):
    return web.json_response({"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]})


async def lineup_post(request):
    return web.Response(text="")


async def device_xml(request):
    b = base_url(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><URLBase>{b}</URLBase>
<specVersion><major>1</major><minor>0</minor></specVersion>
<device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
<friendlyName>{html.escape(TUNER_NAME)}</friendlyName><manufacturer>Silicondust</manufacturer>
<modelName>HDTC-2US</modelName><modelNumber>HDTC-2US</modelNumber><serialNumber/>
<UDN>uuid:{DEVICE_ID}-chrome-tuner</UDN></device></root>"""
    return web.Response(text=xml, content_type="application/xml")


async def playlist(request):
    b = base_url(request)
    lines = [f'#EXTM3U url-tvg="{b}/epg.xml" x-tvg-url="{b}/epg.xml"']
    for c in load_channels():
        lines.append(
            f'#EXTINF:-1 tvg-id="{c["number"]}" tvg-chno="{c["number"]}" tvg-name="{c["name"]}" '
            f'tvg-logo="{c.get("logo") or ""}" group-title="Chrome Tuner",{c["name"]}')
        lines.append(f"{b}/stream/{c['number']}")
    return web.Response(text="\n".join(lines) + "\n", content_type="audio/x-mpegurl")


async def epg(request):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=now.hour % 2) - timedelta(hours=2)
    fmt = "%Y%m%d%H%M%S +0000"
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="chrome-tuner">']
    chans = load_channels()
    for c in chans:
        out.append(f'<channel id="{c["number"]}"><display-name>{html.escape(c["name"])}</display-name>'
                   f'<display-name>{c["number"]}</display-name>'
                   + (f'<icon src="{html.escape(c["logo"])}"/>' if c.get("logo") else "") + "</channel>")
    for c in chans:
        title = html.escape(c.get("epg_title") or c["name"])
        progs = GUIDE.programmes(int(start.timestamp()), int((start + timedelta(hours=52)).timestamp())) \
            if GUIDE and is_live_page(c) else None
        if progs:
            for pr in progs:
                s_ = datetime.fromtimestamp(pr["start"], timezone.utc)
                e_ = datetime.fromtimestamp(pr["end"], timezone.utc)
                out.append(f'<programme channel="{c["number"]}" start="{s_.strftime(fmt)}" stop="{e_.strftime(fmt)}">'
                           f'<title>{html.escape(pr["title"])}</title><desc>{html.escape(pr["desc"])}</desc></programme>')
            continue
        for i in range(26):
            s = start + timedelta(hours=2 * i)
            e = s + timedelta(hours=2)
            out.append(f'<programme channel="{c["number"]}" start="{s.strftime(fmt)}" stop="{e.strftime(fmt)}">'
                       f'<title>{title}</title><desc>Live stream from {html.escape(c["url"])}</desc>'
                       f'<category>Sports</category></programme>')
    out.append("</tv>")
    return web.Response(text="\n".join(out), content_type="application/xml")


async def stream(request):
    num = request.match_info["num"]
    ch = find_channel(num)
    if not ch:
        raise web.HTTPNotFound(text=f"no channel {num}")
    if request.method == "HEAD":
        return web.Response(headers={"Content-Type": "video/mp2t"})
    tuner = TUNER
    log.info("stream request ch %s from %s", num, request.remote)
    q = await tuner.attach(ch)
    resp = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-cache",
                                       "Connection": "close"})
    try:
        await resp.prepare(request)
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            await resp.write(chunk)
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        tuner.detach(q)
    return resp


async def index(request):
    st = TUNER.status()
    host = request.host.split(":")[0]
    novnc = f"http://{host}:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale"
    b = base_url(request)
    rows = "".join(
        f"<tr><td>{c['number']}</td><td>{html.escape(c['name'])}</td>"
        f"<td><a href='{b}/stream/{c['number']}'>stream</a></td>"
        f"<td style='word-break:break-all'>{html.escape(c['url'])}</td></tr>" for c in load_channels())
    lk = st.get("lock") or {}
    if lk.get("locked"):
        state_txt = {"ok": "&#9989; playing normally", "recovering": "&#128295; recovering",
                     "failed": "&#10060; recovery failing", "problem": "&#9888; problem"}.get(lk["state"], lk["state"])
        prob = (f"<br>Problem: {html.escape(lk['problem'])} (for {lk['problem_for']}, "
                f"{lk['attempts']} recovery attempts)") if lk.get("problem") else ""
        lock_html = (f"<p><b>Locked</b> to <b>{html.escape(lk['station'] or '')}</b> since {lk['locked_since']} "
                     f"&middot; {state_txt}{prob}</p>"
                     "<form method='post' action='/lock'><input type='hidden' name='on' value='0'>"
                     "<button>Release lock</button></form>")
    else:
        lock_html = ("<p><b>Unlocked.</b> Pick a channel in the tuner browser, then lock it. While locked, a "
                     "watchdog keeps that channel playing and recovers it if it stalls, ends or changes.</p>"
                     "<form method='post' action='/lock'><input type='hidden' name='on' value='1'>"
                     "<button>Lock current channel</button></form>")
    lock_html += ("<p style='color:#777;font-size:13px'>Pushover alerts: "
                  + ("on" if st.get("pushover") else "off (set PUSHOVER_TOKEN / PUSHOVER_USER)") + "</p>")
    if NOTIFIER and NOTIFIER.history:
        lock_html += "<details><summary>Recent events</summary><ul>" + "".join(
            f"<li>{time.strftime('%H:%M:%S', time.localtime(t))} &mdash; {html.escape(m)}</li>"
            for t, m in reversed(NOTIFIER.history)) + "</ul></details>"
    page = f"""<!doctype html><html><head><title>{html.escape(TUNER_NAME)}</title>
<meta http-equiv="refresh" content="15">
<style>body{{font:15px system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left}}
code{{background:#f3f3f3;padding:2px 5px;border-radius:4px}}input[type=url]{{width:70%;padding:6px}}</style></head><body>
<h1>{html.escape(TUNER_NAME)}</h1>
<p><b>Status:</b> {"streaming <b>" + html.escape(st["channel"]["name"]) + f"</b> to {st['viewers']} viewer(s) since {st['since']}" if st["streaming"] and st["channel"] else "idle"}
&middot; encoder <code>{st["encoder"]}</code><br>
<b>Guide:</b> {html.escape(st["station"] or "no station detected yet")}
{f"&middot; YouTube TV guide fetched {st['guide_age_min']} min ago" if st["guide_age_min"] is not None else ""}</p>
<h2>Channel lock</h2>
{lock_html}
<h2>Emby setup</h2>
<p>HDHomeRun tuner: <code>{b}</code><br>
M3U tuner: <code>{b}/playlist.m3u</code><br>
XMLTV guide: <code>{b}/epg.xml</code></p>
<h2>Channels</h2><p>Edit <code>/config/channels.yaml</code> &mdash; changes apply on the next tune.</p>
<table><tr><th>#</th><th>Name</th><th></th><th>URL</th></tr>{rows}</table>
<h2>Log in to a site</h2>
<p>Open a page in the tuner's browser, then use the <a href="{novnc}" target="_blank">remote screen (noVNC)</a> to sign in.
Cookies are kept in <code>/config/chrome</code>. Channels with <code>url: current</code> capture whatever page you leave open here.</p>
<form method="post" action="/browse" target="_blank"><input type="url" name="url" placeholder="https://..." required>
<button>Open in tuner browser</button></form>
</body></html>"""
    return web.Response(text=page, content_type="text/html")


async def browse(request):
    tuner = TUNER
    if tuner.status()["streaming"]:
        raise web.HTTPConflict(text="Tuner is streaming - stop playback in Emby first.")
    url = (await request.post()).get("url", "")
    if not url.startswith(("http://", "https://")):
        raise web.HTTPBadRequest(text="bad url")
    tuner.browser.auto_close_popups = False
    asyncio.create_task(tuner.browser.goto(url))
    host = request.host.split(":")[0]
    raise web.HTTPFound(f"http://{host}:{NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale")


async def lock_form(request):
    on = (await request.post()).get("on") == "1"
    ok, msg = await WATCHDOG.set_lock(on)
    if not ok:
        return web.Response(text=f"<p>{html.escape(msg)}</p><p><a href='/'>Back</a></p>", content_type="text/html")
    raise web.HTTPFound("/")


async def api_lock(request):
    """GET: lock status. POST/PUT {"locked": true|false} or ?locked=1|0 to set (for Home Assistant)."""
    if request.method in ("POST", "PUT"):
        want = request.query.get("locked")
        if want is None:
            try:
                want = (await request.json()).get("locked")
            except Exception:  # noqa: BLE001
                want = None
        if want is None:
            raise web.HTTPBadRequest(text='send {"locked": true|false} or ?locked=1|0')
        on = str(want).lower() in ("1", "true", "on", "yes")
        ok, msg = await WATCHDOG.set_lock(on)
        return web.json_response({"ok": ok, "message": msg, **WATCHDOG.status()}, status=200 if ok else 409)
    return web.json_response(WATCHDOG.status())


async def api_status(request):
    return web.json_response(TUNER.status())


async def page_watcher(browser):
    """Keep the YouTube TV guide cache fresh (station changes arrive via player responses)."""
    while True:
        try:
            await GUIDE.maintain(browser)
        except Exception as e:  # noqa: BLE001
            log.debug("watcher: %s", e)
        await asyncio.sleep(300)


async def on_startup(app):
    if not CHANNELS_FILE.exists():
        shutil.copy("/app/channels.example.yaml", CHANNELS_FILE)
    browser = Browser()
    await browser.ensure()
    encoder = await detect_encoder()
    log.info("encoder: %s", encoder)
    global TUNER, GUIDE
    TUNER = Tuner(browser, encoder)
    GUIDE = Guide()
    browser.on_player = GUIDE.on_player
    global NOTIFIER, WATCHDOG
    NOTIFIER = Notifier()
    WATCHDOG = Watchdog(browser, GUIDE, NOTIFIER, TUNER.status)
    WATCHDOG.start()
    global WATCHER
    WATCHER = asyncio.create_task(page_watcher(browser))


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.add_routes([
        web.get("/", index),
        web.post("/browse", browse),
        web.get("/api/status", api_status),
        web.post("/lock", lock_form),
        web.route("*", "/api/lock", api_lock),
        web.get("/discover.json", discover),
        web.get("/lineup.json", lineup),
        web.get("/lineup_status.json", lineup_status),
        web.post("/lineup.post", lineup_post),
        web.get("/lineup.post", lineup_post),
        web.get("/device.xml", device_xml),
        web.get("/playlist.m3u", playlist),
        web.get("/epg.xml", epg),
        web.route("*", "/auto/v{num}", stream),
        web.route("*", "/stream/{num}", stream),
    ])
    log.info("%s listening on :%d (device %s)", TUNER_NAME, PORT, DEVICE_ID)
    web.run_app(app, port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
