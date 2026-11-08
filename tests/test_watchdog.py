import asyncio, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import watchdog as wd
wd.STALL_SECS = 30

class Page:
    def __init__(s): s.url="https://tv.youtube.com/watch/ABC"; s.t=100.0; s.paused=False; s.has_video=True; s.ended=False; s.frozen=False; s.actions=[]
    async def evaluate(s, js, *a):
        if "dismissed" in js:
            if not s.frozen and not s.paused: s.t += 10
            return {"url": s.url, "has_video": s.has_video, "paused": s.paused, "ended": s.ended, "ready": 4, "time": s.t, "w": 1280, "dismissed": None}
        s.actions.append("play"); s.paused=False; s.frozen=False
    async def reload(s, **k): s.actions.append("reload")
class Browser:
    def __init__(s): s.page=Page(); s.launches=1; s.gotos=[]
    async def active_page(s): return s.page
    async def goto(s, url): s.gotos.append(url); s.page.url=url; s.page.frozen=False; s.page.paused=False
class Guide:
    current="FOX"; names={"FOX":"FOX 11","CBS":"CBS 2"}; fetches=0
    def station_name(s, sid=None): return s.names.get(sid or s.current)
    def live_url(s, sid): return "https://tv.youtube.com/watch/LIVE-"+sid
    async def fetch(s, b): s.fetches+=1
class Notifier:
    def __init__(s): s.msgs=[]
    async def send(s, m, **k): s.msgs.append(m)

clock=[1000.0]
wd.time.time=lambda: clock[0]
async def tick(w, secs=10):
    clock[0]+=secs; await w._check()

async def main():
    b, g, n = Browser(), Guide(), Notifier()
    w = wd.Watchdog(b, g, n, lambda: {"viewers": 1})
    # lock refused if not on a watch page
    b.page.url="https://tv.youtube.com/live"; ok,msg = await w.set_lock(True); assert not ok, msg
    b.page.url="https://tv.youtube.com/watch/ABC"; ok,msg = await w.set_lock(True); assert ok and w.station=="FOX", msg
    for _ in range(3): await tick(w)
    assert w.state=="ok" and not w.problem, w.status()

    # 1) paused -> resume
    b.page.paused=True
    for _ in range(4): await tick(w)
    assert "play" in b.page.actions, (w.status(), b.page.actions)
    for _ in range(2): await tick(w)
    assert w.state=="ok" and not w.problem, w.status()
    print("paused->resumed OK:", [m for m in n.msgs][-2:])

    # 2) frozen (stalled but not paused) -> reload
    b.page.actions.clear(); b.page.frozen=True
    for _ in range(4): await tick(w)
    assert "reload" in b.page.actions, (w.status(), b.page.actions)
    b.page.frozen=False
    for _ in range(3): await tick(w)
    assert w.state=="ok", w.status()
    print("stall->reload OK:", n.msgs[-1])

    # 3) channel drift -> reopen station live url
    g.current="CBS"; await tick(w)
    assert b.gotos and b.gotos[-1].endswith("LIVE-FOX"), b.gotos
    g.current="FOX"; await tick(w); await tick(w)
    assert w.state=="ok", w.status()
    print("drift->reopen OK:", n.msgs[-1])

    # 4) Chrome restart lands on /live -> reopen
    b.launches+=1; b.page=Page(); b.page.url="https://tv.youtube.com/live"
    await tick(w); await tick(w)
    assert b.gotos[-1].endswith("LIVE-FOX"), b.gotos
    await tick(w); await tick(w)
    assert w.state=="ok", w.status()
    print("chrome restart OK:", n.msgs[-1])

    # 5) permanent failure -> 'failed' notification + slow retries
    b.page.frozen=True; gotos_before=len(b.gotos)
    orig_goto=b.goto
    async def bad_goto(url): b.gotos.append(url)        # navigation doesn't fix it
    b.goto=bad_goto
    async def bad_reload(**k): b.page.actions.append("reload")
    b.page.reload=bad_reload
    for _ in range(40): await tick(w)
    assert w.state=="failed", w.status()
    assert any("Still failing" in m for m in n.msgs), n.msgs
    print("failure escalation OK:", [m for m in n.msgs if "Still failing" in m][0])

    # 6) unlock stops checks
    ok,msg = await w.set_lock(False); assert ok and w.state=="unlocked"
    print("\nAll notifications sent:"); [print("  -", m) for m in n.msgs]
asyncio.run(main())
