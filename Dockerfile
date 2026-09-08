FROM node:22-bookworm-slim

ARG PI_VERSION=0.84.2
ARG CODEX_VERSION=0.147.0

ENV DEBIAN_FRONTEND=noninteractive \
    CANDYTEST_DEPLOYMENT=server \
    CANDYTEST_HOST=0.0.0.0 \
    CANDYTEST_PORT=8765 \
    CANDYTEST_DATA_DIR=/data \
    HOME=/tmp/candytest-home \
    XDG_CONFIG_HOME=/tmp/candytest-home/.config \
    XDG_CACHE_HOME=/tmp/candytest-home/.cache \
    XDG_DATA_HOME=/tmp/candytest-home/.local/share \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
        tini \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && npm install --global --no-fund --no-audit \
        @earendil-works/pi-coding-agent@${PI_VERSION} \
        @openai/codex@${CODEX_VERSION} \
    && mkdir -p /app /data /tmp/candytest-home/.config /tmp/candytest-home/.cache /tmp/candytest-home/.local/share \
    && chown -R node:node /app /data /tmp/candytest-home \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy only application files; local databases and credentials never enter the image.
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --chown=node:node run.py ./
COPY --chown=node:node candytest ./candytest

USER node

EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "mkdir -p \"$HOME\" \"$XDG_CONFIG_HOME\" \"$XDG_CACHE_HOME\" \"$XDG_DATA_HOME\" && exec /opt/venv/bin/python run.py"]
