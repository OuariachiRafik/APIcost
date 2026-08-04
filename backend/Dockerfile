# Python 3.12 is pinned by BUILD_SPEC §2 and enforced here by the uv base image,
# so the container interpreter matches backend/.python-version exactly.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so a source edit does not invalidate the install layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

EXPOSE 8000 8001

# Overridden per service in docker-compose.yml.
CMD ["uvicorn", "apicost.main_proxy:app", "--host", "0.0.0.0", "--port", "8000"]
