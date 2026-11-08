# chrome-tuner

A single-tuner virtual HDHomeRun for Emby Live TV. It runs Google Chrome on a
virtual display inside a container, captures the screen and audio, and serves
it as an MPEG-TS stream (H.264 via NVENC + AAC). Emby sees it as an ordinary
HDHomeRun tuner, so whatever is playing in that Chrome window — e.g. a YouTube
TV channel — becomes a Live TV channel.

Built for watching one sports stream at a time from a streaming service you
already pay for. Only use it with content you have legitimate access to;
some services' terms prohibit recording.

## How it works

```
Chrome (Xvfb + PulseAudio + Openbox)  ──►  ffmpeg x11grab + pulse  ──►  NVENC H.264 / AAC, MPEG-TS
        ▲                                                                       │
   noVNC :6080 (you pick the channel / sign in)          HTTP :5004 (HDHomeRun API, M3U, XMLTV, stream)
                                                                                ▼
                                                                         Emby Live TV
```

- **Tuning.** When Emby opens the stream, the encoder starts immediately (so
  Emby gets data before its timeout), then the tuner finds the largest playing
  `<video>` on the page (including iframes), unmutes it and makes it
  fullscreen. 20 s after the last viewer leaves, the encoder stops.
- **`url: current` channels** capture whatever page is open in the tuner
  browser — pick the channel yourself through noVNC before watching. Channels
  with a fixed URL are navigated to on tune (with optional click selectors).
- **Guide.** For YouTube TV, the tuner reads the station id from the player's
  `youtubei/v1/player` response, loads the YouTube TV guide in a hidden
  background tab, and serves that station's schedule at `/epg.xml`. When the
  station changes it can ask Emby to refresh its guide.
- **Channel lock + watchdog.** Off at container start. Once locked, the
  watchdog checks every 10 s that Chrome is alive, the page is still on the
  locked station and the video is advancing, dismisses "Still watching?"
  prompts, and recovers in steps (resume → reload → reopen the station's live
  airing). Problems and recoveries are sent to Pushover.

## Install (Unraid)

Add `https://github.com/rgabrielson11/unraid-docker-templates` under
*Docker → Template repositories*, then add the **ChromeTuner** container.
The template uses `ghcr.io/rgabrielson11/chrome-tuner:latest`, built by GitHub
Actions on every push to `main`.

Requirements: the Nvidia Driver plugin for NVENC (falls back to CPU x264
without it) and `--shm-size=2g` (already in the template's extra parameters).

Ports: `5004` (tuner API / streams / status page) and `6080` (noVNC).
Appdata: `/config` holds `channels.yaml`, the Chrome profile (your logins) and
the guide cache.

## Emby setup

1. **Live TV → TV Sources → HD Homerun**, address `http://<server>:5004`.
   (Or an M3U tuner at `http://<server>:5004/playlist.m3u`.)
2. Install the **XmlTV** plugin, then add a guide source with
   `http://<server>:5004/epg.xml`, enabled only for this tuner.
3. Open `http://<server>:5004/`, click **Open in tuner browser**, sign in to
   your streaming service in the noVNC window and start a channel.

## Configuration

`/config/channels.yaml` (edited live, no restart):

```yaml
channels:
  - number: "1001"
    name: "Chrome Tuner Live"
    url: "current"          # or a fixed page URL
    # click: ["button.watch-live"]   # fixed-URL channels: selectors clicked after load
    # script: mysite.js               # fixed-URL channels: JS in /config/scripts
```

| Variable | Default | Purpose |
|---|---|---|
| `TUNER_NAME` | Chrome Tuner | Name shown in Emby |
| `VIDEO_BITRATE` | 6M | H.264 bitrate |
| `RESOLUTION` / `FRAMERATE` | 1920x1080 / 30 | Virtual display and output |
| `ENCODER` | auto | `auto`, `nvenc` or `x264` |
| `IDLE_TIMEOUT` | 20 | Seconds after the last viewer before the encoder stops |
| `START_URL` | https://tv.youtube.com/live | Page Chrome opens on start |
| `EMBY_URL` / `EMBY_API_KEY` | | Trigger Emby's guide refresh on station change |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | | Watchdog alerts |
| `VNC_PASSWORD` | | Optional noVNC password (blank = none; LAN only) |
| `NVIDIA_VISIBLE_DEVICES` | all | GPU UUID for NVENC |
| `DEBUG` | | `1` for verbose ffmpeg/Chrome logs |

## HTTP endpoints

| Path | |
|---|---|
| `/` | Status page: tuner state, guide, channel lock toggle, recent events |
| `/discover.json`, `/lineup.json`, `/lineup_status.json`, `/device.xml` | HDHomeRun emulation |
| `/auto/v<num>`, `/stream/<num>` | Live MPEG-TS |
| `/playlist.m3u`, `/epg.xml` | M3U tuner + XMLTV guide |
| `/api/status` | JSON status (encoder, viewers, station, lock) |
| `/api/lock` | `GET` lock status; `POST {"locked": true\|false}` or `?locked=1\|0` |

`examples/home-assistant-package.yaml` turns these into Home Assistant sensors
and a channel-lock switch.

## Known limits

- YouTube TV caps Chrome on Linux at 720p (Widevine L3); streams start low and
  ramp up over 10–60 s.
- One tuner, one stream. Emby's own transcoding handles multiple clients.
- Site layouts change; the generic video finder handles most players, and
  per-channel `click:`/`script:` covers the rest.

## Development

```
python3 tests/test_watchdog.py   # watchdog state machine against a simulated browser
python3 tests/test_http.py       # lock API, status page, Pushover payload
docker build -t chrome-tuner .
```
