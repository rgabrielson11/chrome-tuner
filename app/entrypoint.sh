#!/bin/bash
# Root part: prepare /config, then drop to uid 99:100 for everything else.
set -e

PUID=${PUID:-99}
PGID=${PGID:-100}

if [ "$(id -u)" = "0" ]; then
    mkdir -p /config/chrome /config/scripts /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
    [ -f /config/channels.yaml ] || cp /app/channels.example.yaml /config/channels.yaml
    chown -R "$PUID:$PGID" /config
    exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups /app/entrypoint.sh
fi

# ---- unprivileged from here ----
RES=${RESOLUTION:-1920x1080}
mkdir -p "$HOME" /tmp/pulse
# a restarted (not recreated) container keeps /tmp: clear stale locks/sockets
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /tmp/pulse/native
rm -rf "$HOME/.config/pulse" /tmp/pulse-* "$HOME"/.pulse*

echo "[entrypoint] starting Xvfb ${RES}"
Xvfb :99 -screen 0 "${RES}x24" -nolisten tcp -ac &
for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

echo "[entrypoint] starting Openbox (window manager, needed for fullscreen)"
openbox --sm-disable >/dev/null 2>&1 &

echo "[entrypoint] starting PulseAudio (null sink 'virtual')"
(
  while true; do
    pulseaudio -n -F /app/pulse.pa --exit-idle-time=-1 --daemonize=no --realtime=no \
      --high-priority=no --use-pid-file=no --log-target=stderr --log-level=error 2>/dev/null
    echo "[entrypoint] pulseaudio exited - restarting"
    rm -f /tmp/pulse/native; rm -rf "$HOME/.config/pulse"; sleep 1
  done
) &
for i in $(seq 1 50); do [ -S /tmp/pulse/native ] && break; sleep 0.1; done

VNC_ARGS="-display :99 -forever -shared -localhost -rfbport 5900 -quiet -noxdamage"
if [ -n "$VNC_PASSWORD" ]; then
    x11vnc -storepasswd "$VNC_PASSWORD" /tmp/home/vncpass >/dev/null 2>&1
    VNC_ARGS="$VNC_ARGS -rfbauth /tmp/home/vncpass"
else
    VNC_ARGS="$VNC_ARGS -nopw"
fi
echo "[entrypoint] starting x11vnc + noVNC on :6080"
x11vnc $VNC_ARGS >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo "[entrypoint] starting tuner server"
exec python3 -u /app/tuner.py
