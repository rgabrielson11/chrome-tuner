import asyncio, os, sys
os.environ.update(PUSHOVER_TOKEN="tok123", PUSHOVER_USER="usr456", CONFIG_DIR="/tmp/cfg")
os.makedirs("/tmp/cfg", exist_ok=True)
open("/tmp/cfg/channels.yaml","w").write('channels:\n  - number: "1001"\n    name: "Test"\n    url: current\n')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import notify, tuner, watchdog


got = []
async def fake_pushover(req):
    got.append(dict(await req.post())); return web.json_response({"status": 1})

class Page:
    url = "https://tv.youtube.com/watch/XYZ"
class Browser:
    launches = 1
    async def active_page(s): return Page()
class Guide:
    current = "FOX"; fetched = 0
    def station_name(s, sid=None): return "FOX 11"
class Tuner:
    def status(s): return {"encoder": "nvenc", "streaming": False, "channel": None, "viewers": 0, "since": None,
                          "station": "FOX 11", "guide_age_min": 3,
                          "lock": tuner.WATCHDOG.status(), "pushover": tuner.NOTIFIER.enabled}

async def main():
    # fake pushover endpoint
    papp = web.Application(); papp.router.add_post("/1/messages.json", fake_pushover)
    psrv = TestServer(papp); await psrv.start_server()
    real_post_url = "https://api.pushover.net/1/messages.json"
    import aiohttp
    orig = aiohttp.ClientSession.post
    def patched(self, url, **kw):
        return orig(self, str(psrv.make_url("/1/messages.json")) if url == real_post_url else url, **kw)
    aiohttp.ClientSession.post = patched

    tuner.NOTIFIER = notify.Notifier()
    tuner.GUIDE = Guide()
    tuner.TUNER = Tuner()
    tuner.WATCHDOG = watchdog.Watchdog(Browser(), tuner.GUIDE, tuner.NOTIFIER, lambda: {"viewers": 0})

    app = web.Application()
    app.add_routes([web.get("/", tuner.index), web.post("/lock", tuner.lock_form),
                    web.route("*", "/api/lock", tuner.api_lock)])
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/api/lock"); print("GET /api/lock ->", r.status, await r.json())
        r = await c.post("/api/lock", json={"locked": True}); print("POST locked=true ->", r.status, await r.json())
        r = await c.get("/"); body = await r.text()
        assert "Locked</b> to <b>FOX 11" in body and "Release lock" in body, body[:2000]
        print("status page shows lock: OK")
        r = await c.post("/lock", data={"on": "0"}, allow_redirects=False); print("form release ->", r.status, r.headers.get("Location"))
        r = await c.post("/api/lock?locked=1"); print("POST ?locked=1 ->", r.status, (await r.json())["station"])
        r = await c.post("/api/lock", data="junk"); print("bad request ->", r.status)
        r = await c.get("/"); assert "Recent events" in await r.text(); print("events list: OK")
    print("pushover requests:", [(g["token"], g["user"], g["message"]) for g in got])
    await psrv.close()
asyncio.run(main())
