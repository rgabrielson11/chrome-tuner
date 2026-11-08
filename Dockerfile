FROM debian:bookworm-slim

LABEL org.opencontainers.image.source="https://github.com/rgabrielson11/chrome-tuner" \
      org.opencontainers.image.description="Single-tuner virtual HDHomeRun: a Chrome tab (screen + audio) as an Emby Live TV channel"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    DISPLAY=:99 \
    PULSE_SERVER=unix:/tmp/pulse/native \
    HOME=/tmp/home \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates wget gnupg \
        xvfb x11vnc novnc websockify \
        pulseaudio \
        ffmpeg \
        python3 python3-pip python3-aiohttp python3-yaml \
        fonts-liberation fonts-noto-core fonts-noto-color-emoji \
        procps tini openbox \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor > /usr/share/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && pip3 install --no-cache-dir --break-system-packages playwright \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -f -g 100 users \
    && useradd -u 99 -g 100 -M -d /tmp/home -s /usr/sbin/nologin tuner || true

COPY app/ /app/
RUN chmod +x /app/entrypoint.sh

EXPOSE 5004 6080
VOLUME /config

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
