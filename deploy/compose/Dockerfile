# syntax=docker/dockerfile:1
FROM postgres:17-bookworm AS postgres
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 deploy/compose/pg-entrypoint.sh /usr/local/bin/laxagent-pg-entrypoint
ENTRYPOINT ["laxagent-pg-entrypoint"]
CMD ["postgres"]

FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM ubuntu:24.04 AS app
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    HOME=/home/laxagent
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv bubblewrap socat bash coreutils procps util-linux \
    ca-certificates golang-go build-essential git curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /bin/bash laxagent \
    && mkdir -p /home/laxagent/laxcode \
    && chown laxagent:laxagent /home/laxagent/laxcode
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --no-install-project --python /usr/bin/python3
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --locked --no-dev --python /usr/bin/python3
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY config/builtin_skills/ ./config/builtin_skills/
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
COPY deploy/compose/entrypoint.py ./deploy/compose/entrypoint.py
USER laxagent
EXPOSE 8080
ENTRYPOINT ["python3", "/app/deploy/compose/entrypoint.py"]
