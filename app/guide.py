"""YouTube TV guide scraping for chrome-tuner.

Loads tv.youtube.com/live in a background tab, captures the guide JSON the page
fetches (youtubei/v1/browse -> epgRowRenderer), and keeps a cache of stations and
airings. The station currently being watched is found by matching the video id
in the tuner page's watch URL against every video id in each station's row.
Optionally asks Emby to refresh its guide when the station changes.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp

log = logging.getLogger("tuner")

CACHE_FILE = Path(os.environ.get("CONFIG_DIR", "/config")) / "guide.json"
GUIDE_URL = "https://tv.youtube.com/live"
EMBY_URL = os.environ.get("EMBY_URL", "").rstrip("/")
EMBY_API_KEY = os.environ.get("EMBY_API_KEY", "")
MAX_AGE = float(os.environ.get("GUIDE_MAX_AGE", str(3 * 3600)))
VID_RE = re.compile(r"/watch/([A-Za-z0-9_-]{6,})")


def _text(node):
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def _video_ids(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "videoId" and isinstance(v, str):
                out.add(v)
            else:
                _video_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _video_ids(v, out)


def _rows(obj, out):
    if isinstance(obj, dict):
        if "epgRowRenderer" in obj:
            out.append(obj["epgRowRenderer"])
            return
        for v in obj.values():
            _rows(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _rows(v, out)


class Guide:
    def __init__(self):
        self.stations = {}      # station id -> {name, icon, airings: [...]}
        self.vid2station = {}   # video id -> station id
        self.fetched = 0.0
        self.current = None     # station id being watched
        self.last_vid = None
        self._fetch_lock = asyncio.Lock()
        self._refresh_task = None
        self._last_refresh = 0.0
        self._load()

    # ------------------------------------------------------------ cache
    def _load(self):
        try:
            d = json.loads(CACHE_FILE.read_text())
            self.stations = d.get("stations", {})
            self.vid2station = d.get("vid2station", {})
            self.fetched = d.get("fetched", 0.0)
            self.current = d.get("current")
            if self.current:
                log.info("guide: last station %s", self.station_name())
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("guide cache unreadable: %s", e)

    def _save(self):
        try:
            CACHE_FILE.write_text(json.dumps({
                "stations": self.stations, "vid2station": self.vid2station,
                "fetched": self.fetched, "current": self.current}))
        except Exception as e:  # noqa: BLE001
            log.warning("guide cache write failed: %s", e)

    # ------------------------------------------------------------ parsing
    def _ingest(self, data):
        rows = []
        _rows(data, rows)
        n_air = 0
        for row in rows:
            st = (row.get("station") or {}).get("epgStationRenderer") or {}
            sid = st.get("stationId") or st.get("tenxId")
            if not sid:
                continue
            entry = self.stations.setdefault(sid, {"name": "", "icon": "", "airings": {}})
            if isinstance(entry.get("airings"), list):   # from an older cache
                entry["airings"] = {str(a["start"]): a for a in entry["airings"]}
            entry["name"] = _text(st.get("name")) or _text(st.get("callSign")) or entry["name"]
            thumbs = (st.get("icon") or {}).get("thumbnails") or []
            if thumbs:
                url = thumbs[0].get("url", "")
                entry["icon"] = ("https:" + url) if url.startswith("//") else url
            for a in row.get("airings") or []:
                ar = a.get("epgAiringRenderer")
                if not ar:
                    continue
                try:
                    start, end = int(ar["beginTimeMs"]) // 1000, int(ar["endTimeMs"]) // 1000
                except (KeyError, ValueError):
                    continue
                nav = ((ar.get("navigationEndpoint") or {}).get("watchEndpoint") or {})
                entry["airings"][str(start)] = {
                    "start": start, "end": end,
                    "title": _text(ar.get("title")) or _text(ar.get("primaryText")),
                    "desc": _text(ar.get("quaternaryText")),
                    "vid": nav.get("videoId") or ar.get("videoId") or "",
                    "params": nav.get("params") or "",
                }
                n_air += 1
            vids = set()
            _video_ids(row, vids)
            for v in vids:
                self.vid2station[v] = sid
        return len(rows), n_air

    def _prune(self):
        cutoff = time.time() - 6 * 3600
        for st in self.stations.values():
            st["airings"] = {k: a for k, a in st["airings"].items() if a["end"] > cutoff}

    # ------------------------------------------------------------ fetching
    async def fetch(self, browser):
        """Load the YouTube TV guide in a background tab and capture its data."""
        async with self._fetch_lock:
            if browser.browser is None:
                return False
            ctx = browser.browser.contexts[0]
            before = set(ctx.pages)
            payloads = []
            page = None
            try:
                cdp = await browser.browser.new_browser_cdp_session()
                await cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
                for _ in range(20):
                    new = [p for p in ctx.pages if p not in before]
                    if new:
                        page = new[0]
                        break
                    await asyncio.sleep(0.2)
                if page is None:
                    log.warning("guide: could not open background tab")
                    return False
                browser.guide_pages.add(page)

                async def on_resp(r):
                    if "youtubei/v1/browse" not in r.url:
                        return
                    try:
                        body = await r.body()
                    except Exception:  # noqa: BLE001
                        return
                    if b"epgRowRenderer" in body:
                        payloads.append(body)

                page.on("response", lambda r: asyncio.create_task(on_resp(r)))
                await page.goto(GUIDE_URL, wait_until="domcontentloaded", timeout=45000)
                for _ in range(50):          # first guide page, then give pagination a moment
                    if payloads:
                        break
                    await asyncio.sleep(0.5)
                await asyncio.sleep(5)
            except Exception as e:  # noqa: BLE001
                log.warning("guide fetch failed: %s", str(e).splitlines()[0])
            finally:
                if page is not None:
                    browser.guide_pages.discard(page)
                    try:
                        await page.close()
                    except Exception:  # noqa: BLE001
                        pass
            if not payloads:
                log.warning("guide: no guide data captured (signed out of YouTube TV?)")
                return False
            rows = airs = 0
            for body in payloads:
                try:
                    r, a = self._ingest(json.loads(body))
                    rows, airs = rows + r, airs + a
                except Exception as e:  # noqa: BLE001
                    log.warning("guide parse: %s", e)
            self._prune()
            self.fetched = time.time()
            self._save()
            log.info("guide: %d stations, %d airings loaded", len(self.stations), airs)
            return True

    # ------------------------------------------------------------ station tracking
    def live_url(self, sid):
        """Watch URL for what's airing now on a station (from the cached guide)."""
        st = self.stations.get(sid or "")
        if not st:
            return None
        now = time.time()
        for a in st["airings"].values():
            if a["start"] <= now < a["end"] and a.get("vid"):
                q = f"?vp={a['params']}" if a.get("params") else ""
                return f"https://tv.youtube.com/watch/{a['vid']}{q}"
        return None

    def station_name(self, sid=None):
        st = self.stations.get(sid or self.current or "")
        return st["name"] if st else None

    async def maintain(self, browser):
        """Periodic: keep the guide cache fresh."""
        if time.time() - self.fetched > MAX_AGE:
            await self.fetch(browser)

    async def on_player(self, browser, data):
        """Called with each youtubei/v1/player response from the tab being watched.
        videoDetails.channelId is the YouTube TV station id, author its name."""
        vd = data.get("videoDetails") or {}
        sid, author = vd.get("channelId"), vd.get("author") or ""
        if not sid:
            return
        if sid not in self.stations or time.time() - self.fetched > MAX_AGE:
            await self.fetch(browser)
        st = self.stations.setdefault(sid, {"name": "", "icon": "", "airings": {}})
        if author and not st["name"]:
            st["name"] = author
        if sid != self.current:
            log.info("guide: now watching %s (%d guide entries)", st["name"] or sid, len(st["airings"]))
            self.current = sid
            self._save()
            self.schedule_emby_refresh()

    # ------------------------------------------------------------ Emby
    def schedule_emby_refresh(self):
        if not (EMBY_URL and EMBY_API_KEY):
            return
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._emby_refresh())

    async def _emby_refresh(self):
        await asyncio.sleep(15)       # settle: user may still be flipping channels
        wait = 90 - (time.time() - self._last_refresh)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {"X-Emby-Token": EMBY_API_KEY}
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as s:
                async with s.get(f"{EMBY_URL}/ScheduledTasks") as r:
                    r.raise_for_status()
                    tasks = await r.json()
                task = next((t for t in tasks if t.get("Key") == "RefreshGuide"), None)
                if not task:
                    log.warning("emby: Refresh Guide task not found")
                    return
                if task.get("State") == "Running":
                    log.info("emby: guide refresh already running")
                    return
                async with s.post(f"{EMBY_URL}/ScheduledTasks/Running/{task['Id']}") as r:
                    r.raise_for_status()
            self._last_refresh = time.time()
            log.info("emby: guide refresh started")
        except Exception as e:  # noqa: BLE001
            log.warning("emby refresh failed: %s", e)

    # ------------------------------------------------------------ XMLTV
    def programmes(self, start, stop):
        """Airings for the current station between start and stop (epoch s),
        with gaps filled by 1-hour blocks named after the station."""
        st = self.stations.get(self.current or "")
        if not st:
            return None
        name = st["name"]
        airs = sorted((a for a in st["airings"].values() if a["end"] > start and a["start"] < stop),
                      key=lambda a: a["start"])
        out, t = [], start
        for a in airs:
            while t < a["start"]:
                e = min(a["start"], t + 3600)
                out.append({"start": t, "end": e, "title": name, "desc": f"Live on {name}"})
                t = e
            if a["end"] <= t:
                continue
            out.append({"start": max(a["start"], t), "end": a["end"], "title": a["title"] or name,
                        "desc": (a["desc"] + f" ({name})").strip() if a["desc"] else f"On {name}"})
            t = a["end"]
        while t < stop:
            out.append({"start": t, "end": t + 3600, "title": name, "desc": f"Live on {name}"})
            t += 3600
        return out
