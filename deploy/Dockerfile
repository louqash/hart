# hart image. Build context: repository root.
#   docker build -f deploy/Dockerfile -t hart:local .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    DISABLE_AUTOUPDATER=1 \
    TZ=UTC \
    HART_ENV=production \
    CLAUDE_CONFIG_DIR=/home/app/.claude

# tzdata: set TZ (and HART_TZ) to the athlete's time zone at run time — "today" is their local day.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# Project code; .claude/ carries the agent prompts served as MCP prompts.
COPY src ./src
COPY .claude/agents ./.claude/agents
COPY .claude/skills ./.claude/skills
# Working directory for Claude runs (settings only; no project files).
COPY deploy/claude-workspace ./deploy/claude-workspace
# The repo's files may be mode 0700 on the host; code stays root-owned but
# must be readable by the unprivileged app user.
RUN uv sync --frozen --no-dev \
    && chmod -R a+rX /app \
    && mkdir -p /app/data /home/app/.claude \
    && chown -R app:app /app/data /home/app

# Which commit this image is built from (System page, /healthz). Last, so a new commit doesn't
# invalidate the cached layers above:
#   docker build --build-arg HART_COMMIT=$(git describe --always --dirty) ...
ARG HART_COMMIT=unknown
ARG HART_BUILT_AT=
ENV HART_COMMIT=${HART_COMMIT} HART_BUILT_AT=${HART_BUILT_AT}

USER app
EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5).status == 200 else 1)"]

CMD ["/app/.venv/bin/hart", "serve", "--host", "0.0.0.0"]
