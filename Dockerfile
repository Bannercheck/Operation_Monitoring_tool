# Watchover — dashboard, live receiver and built-in host agent in one image.
#   ./watchover.sh install                    (builds this image on the server from the source tree; see docs/KURULUM_DOCKER.md)
#   docker build -t watchover:local .         (plain build)
# stage 1: the React interface (web/) -> web/dist, served by the API container
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web ./
RUN npm run build

FROM python:3.11-slim AS base
ARG GIT_REV=-
ARG VERSION=dev
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    WATCHOVER_DOCKER=1 WATCHOVER_GIT_REV=$GIT_REV \
    WATCHOVER_HOME=/data KNOWLEDGE_DB=/data/knowledge.db ACTIONS_DB=/data/actions.db PLAYBOOK_DB=/data/playbook.db \
    LIVE_SPOOL=/data/live/events.jsonl WATCHOVER_LOG=/data/watchover.log LIVE_PORT=8600
RUN apt-get update && apt-get install -y --no-install-recommends curl tini && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 watchover && mkdir -p /app /data && chown -R watchover:watchover /app /data
WORKDIR /app
COPY pyproject.toml README.md CHANGELOG.md requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[mcp,postgres,api]"
COPY app.py agent.py mcp_server.py ./
COPY assets ./assets
COPY samples ./samples
COPY scripts ./scripts
COPY .streamlit ./.streamlit
COPY --from=web /web/dist ./web/dist
RUN chown -R watchover:watchover /app
USER watchover
VOLUME ["/data"]
EXPOSE 8501 8600 8765 8502
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD curl -fsS http://127.0.0.1:8501/api/health >/dev/null || exit 1
ENTRYPOINT ["tini", "--"]
# the product: React interface + API on 8501 (the mcp and legacy services in docker-compose.yml override the command)
CMD ["python", "-m", "watchover.api", "--port", "8501"]
