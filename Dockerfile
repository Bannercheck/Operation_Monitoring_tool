# Watchover — dashboard, live receiver and built-in host agent in one image.
#   ./watchover.sh install                    (builds this image on the server from the source tree; see docs/KURULUM_DOCKER.md)
#   docker build -t watchover:local .         (plain build)
# OpenShift (restricted-v2 SCC, deploy/openshift): the process runs as any UID with GID 0, so everything it writes (/data, $HOME)
# belongs to group 0 with the owner's rights (chgrp 0 + chmod g=u); no port below 1024, no root step at start, the root file system
# may be read-only (writes go to /data, the log volumes and /tmp). Docker keeps running it as UID 10001.
# Base images are fully qualified (docker.io/library/…): podman build on RHEL (short-name-mode=enforcing) never stops to ask.
# stage 1: the React interface (web/) -> web/dist, served by the API container
FROM docker.io/library/node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

# stage 2: the Go host agent (wo-agent), built exactly as Dockerfile.receiver builds it. keyd (same image) hashes and signs these
# binaries for the agents' self-update; the dashboard copies them into the receiver's spool, so what is served is what was signed.
FROM docker.io/library/golang:1.26.10-alpine AS goagent
WORKDIR /build/goagent
COPY goagent/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/wo-agent-linux-amd64 . \
    && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags="-s -w" -o /out/wo-agent-linux-arm64 .

FROM docker.io/library/python:3.11-slim AS base
ARG GIT_REV=-
ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WATCHOVER_DOCKER=1 WATCHOVER_GIT_REV=$GIT_REV \
    WATCHOVER_HOME=/data KNOWLEDGE_DB=/data/knowledge.db ACTIONS_DB=/data/actions.db PLAYBOOK_DB=/data/playbook.db \
    LIVE_SPOOL=/data/live/events.jsonl WATCHOVER_LOG=/data/watchover.log LIVE_PORT=8600 HOME=/home/watchover
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends curl tini && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /home/watchover --uid 10001 --gid 0 watchover && mkdir -p /app /data /keys /secrets /updates \
    && chown -R 10001:0 /app /data /keys /secrets /updates /home/watchover && chmod -R g=u /app /data /updates /home/watchover && chmod 0700 /keys
WORKDIR /app
COPY pyproject.toml README.md CHANGELOG.md requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade "pip>=26.2" "setuptools>=83" "wheel>=0.46.2" \
    && pip install --no-cache-dir -e ".[mcp,postgres,api]"
COPY app.py agent.py mcp_server.py ./
COPY assets ./assets
COPY samples ./samples
COPY scripts ./scripts
# vendor command-template packs: the server catalogue (act_templates) loads them at start (docs/design/action-agent.md §5.9)
COPY agent_actions ./agent_actions
COPY .streamlit ./.streamlit
COPY --from=web /web/dist ./web/dist
COPY --from=goagent /out/ ./goagent/dist/
RUN chown -R 10001:0 /app && chmod -R g=u /app
USER 10001
VOLUME ["/data"]
EXPOSE 8501 8600 8765 8502
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD curl -fsS http://127.0.0.1:8501/api/health >/dev/null || exit 1
ENTRYPOINT ["tini", "--"]
# the product: React interface + API on 8501 (the mcp and legacy services in docker-compose.yml override the command)
CMD ["python", "-m", "watchover.api", "--port", "8501"]
