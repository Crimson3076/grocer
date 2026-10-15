FROM python:3.12-slim-bookworm


ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=America/New_York

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc fluxbox novnc websockify tini tzdata fonts-liberation curl \
    && ln -sf /usr/share/novnc/vnc.html /usr/share/novnc/index.html \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Real Google Chrome (not bundled Chromium) through Patchright, which hides automation fingerprints
RUN pip install --no-cache-dir -r requirements.txt \
    && patchright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

COPY grocer ./grocer
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ARG GROCER_REVISION=development
ENV GROCER_REVISION=$GROCER_REVISION

EXPOSE 8790 6080
VOLUME /data
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://localhost:8790/health || exit 1
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
