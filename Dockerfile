# ctb-copilot — single image used by both the api and ui compose services.
# Build once, run twice with different commands.

FROM python:3.11-slim AS base

# uv is the package manager — pulled from Astral's official image so we don't
# need pip-installing it at build time.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (separate layer for cache reuse on code-only changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now copy source + install the project itself.
COPY src ./src
COPY README.md LICENSE ./
# Streamlit reads ./.streamlit/config.toml from the working dir for theme
# + server defaults. Copy it so the container picks up the brand theme.
COPY .streamlit ./.streamlit
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Where DuckDB and uploaded files persist. Mounted as a volume in
# docker-compose.yml so the data survives container restarts and lives on
# the host's disk, not inside the container.
RUN mkdir -p /app/data /app/data/uploads

EXPOSE 8000 8501

# Default command runs the FastAPI service. docker-compose overrides for
# the ui service. Bind to 0.0.0.0 so the port is reachable from the host
# via the published port mapping.
CMD ["uv", "run", "--no-dev", "uvicorn", "ctb_copilot.api:app", \
     "--host", "0.0.0.0", "--port", "8000"]
