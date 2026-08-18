# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.2
ARG CODEX_VERSION=0.147.0

ENV VIRTUAL_ENV=/opt/candytest-venv \
    PATH=/opt/candytest-venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CANDYTEST_DEPLOYMENT=server \
    CANDYTEST_HOST=0.0.0.0 \
    CANDYTEST_PORT=8765 \
    CANDYTEST_DATA_DIR=/data \
    CANDYTEST_OPEN_BROWSER=0 \
    HOME=/tmp/candytest-home \
    XDG_CACHE_HOME=/tmp/candytest-home/.cache \
    XDG_CONFIG_HOME=/tmp/candytest-home/.config \
    XDG_DATA_HOME=/tmp/candytest-home/.local/share

# Runtime-only packages: do not add Rust, a compiler toolchain, or build-essential.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv "$VIRTUAL_ENV" \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VIRTUAL_ENV/bin/pip" install --no-cache-dir -r /tmp/requirements.txt \
    && npm install --global \
        "@earendil-works/pi-coding-agent@${PI_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && rm -f /tmp/requirements.txt

WORKDIR /app
COPY --chown=node:node candytest ./candytest
COPY --chown=node:node run.py ./run.py

# The sentinel makes Docker's first named-volume population preserve the directory
# ownership instead of mounting an empty root-owned volume over /data.
RUN install -d -o node -g node -m 0700 /data \
    && touch /data/.volume-initialized \
    && chown node:node /data/.volume-initialized \
    && chmod 0600 /data/.volume-initialized

USER node

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/candytest-venv/bin/python", "-c", "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:8765/healthz', timeout=3); raise SystemExit(0 if response.status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/candytest-venv/bin/python", "run.py"]
