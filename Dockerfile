# syntax=docker/dockerfile:1

# Playwright's own image already carries a matching Chromium and its system
# libraries, which is far less fragile than installing them by hand.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_FROZEN=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    # The base image's Python is older than this service needs, so uv downloads one. Put it
    # somewhere pwuser can read: the default is /root/.local, which is closed to everyone else.
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON=3.11

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependency layer first so application edits do not invalidate it.
COPY pyproject.toml uv.lock README.md ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
# The token exists only for this RUN, in git's process environment, never a layer.
# Without a secret, public sources are fetched anonymously.
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev --no-install-project

COPY app ./app
RUN --mount=type=secret,id=github_token,required=false \
    if [ -s /run/secrets/github_token ]; then \
        export GIT_CONFIG_COUNT=1 \
          GIT_CONFIG_KEY_0="url.https://x-access-token:$(cat /run/secrets/github_token)@github.com/.insteadOf" \
          GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi \
    && uv sync --no-dev

# Run as the unprivileged user the base image provides. The venv was built as root, so
# hand /app over first: uv run has to resolve /app/.venv/bin/python as that user.
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8006

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8006/healthy', timeout=4).status == 200 else 1)"

# All dependencies are installed above. Runtime never fetches packages or needs GitHub.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8006"]
