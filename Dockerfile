# ---- Build stage: install dependencies & build static assets ----
# Use a uv-derived image (uv preinstalled) as the builder so uv can create the environment
# Pin uv/python tag for reproducible builds (replace 0.8.18-bookworm-slim with the version you test).
FROM ghcr.io/astral-sh/uv:0.8.18-python3.12-bookworm-slim AS builder

# Set a deterministic locale and reduce interactive prompts
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=system \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy only dependency files first to leverage Docker cache
COPY pyproject.toml uv.lock ./

# Install system build tools minimal (if you need wheels/build deps)
# Use --network=host workaround for networking issues during build
RUN apt-get update \
  && apt-get install -y --no-install-recommends gcc build-essential libpq-dev ca-certificates curl \
  && apt-get clean

# Sync the project into the image (creates system-installed packages because UV_PROJECT_ENVIRONMENT=system)
# --locked uses uv.lock, giving reproducible installs (recommended).
RUN uv sync --locked

# Copy the rest of the app
COPY . /app

# Collect static files into /app/staticfiles (adjust STATIC_ROOT in settings accordingly)
# Run migrations as part of build only if you prefer baked DB state (commonly migrations are run at deploy time).
RUN uv run python manage.py collectstatic --noinput

# (Optional) Compile optimized .pyc files
RUN uv run python -OO -m compileall -q /app

# ---- Final stage: minimal runtime image ----
# Use a slim python base and copy installed system packages from builder
FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DJANGO_SETTINGS_MODULE=rivalspy.settings \
    PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Create a non-root user for runtime
RUN addgroup --system app && adduser --system --ingroup app app \
  && mkdir -p /app/staticfiles /app/mediafiles \
  && chown -R app:app /app /app/staticfiles /app/mediafiles

ENV UV_CACHE_DIR=/app/.cache/uv
RUN mkdir -p /app/.cache/uv && chown -R app:app /app/.cache

# Copy system-wide installed packages and binaries created during build (site-packages, scripts)
# This copies /usr/local from the builder into runtime. It is the simplest reliable way
# to bring installed Python packages across multi-stage builds when installed into the system.
COPY --from=builder /usr/local /usr/local
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

# Copy application files and the collected static files
COPY --from=builder /app /app

# Ensure ownership (runtime user)
RUN chown -R app:app /app

USER app

EXPOSE ${PORT}

# Production entrypoint:
# Use `uv run` to run uvicorn from the uv-managed environment.
CMD ["uv", "run", "uvicorn", "rivalspy.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
