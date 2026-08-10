# The API, the worker, and the one-shot setup step all run from this image.
# It exists so `docker compose up` is the whole local install: no Python
# toolchain on the host, no virtualenv to activate, no key to copy by hand.
#
# Dependencies are installed before the source is copied, so editing code
# rebuilds one small layer instead of resolving the lockfile again.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# No CMD: every service in docker-compose.yml states the command it runs, so
# reading the compose file tells you what each container actually does.
