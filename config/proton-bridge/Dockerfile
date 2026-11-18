# Proton Mail Bridge built from Proton's own source at the pinned tag. Bridge
# holds the account credentials and sees every mail in plaintext, so the image
# is built on the box rather than pulled from a third party.
FROM golang:1.26.9-bookworm AS build
ARG BRIDGE_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsecret-1-dev libfido2-dev libcbor-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "v${BRIDGE_VERSION}" https://github.com/ProtonMail/proton-bridge.git /src
WORKDIR /src
RUN make build-nogui

FROM debian:bookworm-20261104-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg pass socat libsecret-1-0 libfido2-1 \
    && rm -rf /var/lib/apt/lists/*
# The bridge binary itself, not the launcher: the launcher runs whatever
# version Bridge last downloaded, which would replace the pinned build.
COPY --from=build /src/bridge /usr/local/bin/bridge
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
