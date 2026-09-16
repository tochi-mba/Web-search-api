# syntax=docker/dockerfile:1

# Playwright's own image already carries a matching Chromium and its system
# libraries, which is far less fragile than installing them by hand.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Dependency layer first so application edits do not invalidate it.
COPY pyproject.toml README.md ./
# git: uv fetches the family's client packages from tagged git sources.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN uv sync --no-dev --no-install-project

COPY app ./app
RUN uv sync --no-dev

# Run as the unprivileged user the base image provides.
USER pwuser

EXPOSE 8006

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8006/health').status==200 else 1)"

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8006"]
