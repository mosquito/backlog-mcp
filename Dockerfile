# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.9.28@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb AS uv

FROM python:3.14.7-alpine3.24@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01 AS build

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN set -eu; \
    pointers="$(grep -rl '^version https://git-lfs.github.com/spec/v1$' src/backlog_mcp/model src/backlog_mcp/static/vendor || true)"; \
    if [ -n "$pointers" ]; then \
        printf '%s\n%s\n' 'Git LFS objects are missing; run git lfs pull before building.' "$pointers" >&2; \
        exit 1; \
    fi

RUN UV_HTTP_TIMEOUT=120 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    uv sync --frozen --no-dev --no-editable

FROM python:3.14.7-alpine3.24@sha256:9e9fde4d32eedce0b661d9ab91e826b62dddf28e928c230ec55f1866cac66b01

LABEL org.opencontainers.image.title="backlog-mcp" \
      org.opencontainers.image.description="A project backlog over MCP" \
      org.opencontainers.image.licenses="MIT"

ENV BACKLOG_MCP_DATABASE=/data/backlog.sqlite3 \
    BACKLOG_MCP_PROTOCOL=http \
    BACKLOG_MCP_HTTP_LISTEN=0.0.0.0 \
    BACKLOG_MCP_HTTP_PORT=3009 \
    BACKLOG_MCP_HTTP_NO_CONSOLE=true \
    HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache

RUN apk add --no-cache dumb-init=1.2.5-r4 \
    && addgroup -S -g 65532 backlog \
    && adduser -S -D -H -u 65532 -G backlog backlog \
    && mkdir /app /data \
    && chown -R 65532:65532 /app /data

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

EXPOSE 3009
VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["/app/.venv/bin/backlog-mcp"]
