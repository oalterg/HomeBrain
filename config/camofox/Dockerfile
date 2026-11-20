# The agent's web browser: camofox-browser (jo-inc, MIT) driving Camoufox, a
# Firefox fork that sites detect as an ordinary browser. Built on the box from
# the pins in config/versions.json (scripts/common.sh: ensure_camofox), so no
# third-party image runs here. It renders untrusted pages, so it runs in a
# container with no host mounts and a fresh profile on every start.
FROM node:22.23.5-trixie-slim

ARG CAMOFOX_REPO
ARG CAMOFOX_COMMIT
ARG CAMOUFOX_VERSION
ARG CAMOUFOX_RELEASE
ARG CAMOUFOX_URL
ARG CAMOUFOX_SHA256

# Firefox's runtime libraries, as upstream's Dockerfile lists them, without
# yt-dlp: HomeBrain turns the YouTube plugin off.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 libxcomposite1 \
        libxcursor1 libxdamage1 libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 \
        libxtst6 libegl1 libgl1-mesa-dri libgbm1 xvfb \
        fonts-liberation fonts-noto-color-emoji fontconfig \
        ca-certificates curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# camoufox-js launches the browser from CAMOUFOX_INSTALL_DIR and ignores
# XDG_CACHE_HOME; camofox-browser reads XDG_CACHE_HOME. Both name one path.
ENV XDG_CACHE_HOME=/opt/camoufox-cache
ENV CAMOUFOX_INSTALL_DIR=/opt/camoufox-cache/camoufox

# Camoufox from its author's release, checked against the pinned digest.
# Upstream downloads it without a checksum. The release zip has no
# version.json, and without one every tab fails to open.
RUN mkdir -p "$CAMOUFOX_INSTALL_DIR" \
    && curl -fsSL -o /tmp/camoufox.zip "$CAMOUFOX_URL" \
    && echo "${CAMOUFOX_SHA256}  /tmp/camoufox.zip" | sha256sum -c - \
    && (unzip -q /tmp/camoufox.zip -d "$CAMOUFOX_INSTALL_DIR" || true) \
    && rm /tmp/camoufox.zip \
    && test -x "$CAMOUFOX_INSTALL_DIR/camoufox-bin" \
    && printf '{"version":"%s","release":"%s"}' "$CAMOUFOX_VERSION" "$CAMOUFOX_RELEASE" \
        > "$CAMOUFOX_INSTALL_DIR/version.json"

# camofox-browser at the pinned commit, unmodified. CAMOFOX_SKIP_DOWNLOAD keeps
# its postinstall from fetching Camoufox a second time. better-sqlite3 is
# compiled here rather than fetched as a prebuilt binary.
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3 \
    && git init -q . \
    && git fetch -q --depth 1 "$CAMOFOX_REPO" "$CAMOFOX_COMMIT" \
    && git checkout -q FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$CAMOFOX_COMMIT" \
    && rm -rf .git \
    && CAMOFOX_SKIP_DOWNLOAD=1 npm_config_build_from_source=true npm ci --omit=dev \
    && npm cache clean --force \
    && apt-get purge -y --auto-remove build-essential python3 \
    && rm -rf /var/lib/apt/lists/*

# uBlock Origin from addons.mozilla.org, fetched once here instead of on every
# start: fewer ads means smaller snapshots for the agent.
RUN node --input-type=module -e \
        "import { DefaultAddons, maybeDownloadAddons } from 'camoufox-js/dist/addons.js'; await maybeDownloadAddons(DefaultAddons);" \
    && test -n "$(ls -A "$CAMOUFOX_INSTALL_DIR/addons")" \
    && chmod -R a+rX /opt/camoufox-cache

# Plugins off: YouTube (yt-dlp), persistence (a profile on disk) and VNC.
COPY camofox.config.json /app/camofox.config.json

ENV NODE_ENV=production
ENV CAMOFOX_PORT=9377
ENV HOME=/home/camofox
RUN useradd -u 10001 -m -d /home/camofox -s /usr/sbin/nologin camofox
USER camofox
EXPOSE 9377
CMD ["node", "--max-old-space-size=128", "server.js"]
