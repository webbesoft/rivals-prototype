# Share the Python interpreter and OS between the build and runtime environments.
FROM python:3.15.2-slim-bookworm AS base

# ---- Build stage: install dependencies & build static assets ----
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.18 /uv /usr/local/bin/uv

# Set a deterministic locale and reduce interactive prompts
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Copy only dependency files first to leverage Docker cache
COPY pyproject.toml uv.lock ./

# Install system build tools minimal (if you need wheels/build deps)
# Use --network=host workaround for networking issues during build
RUN apt-get update \
  && apt-get install -y --no-install-recommends gcc build-essential libpq-dev ca-certificates curl \
  && apt-get clean

# Install Tailwind CSS standalone CLI for linux-x64
RUN curl -sLO https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 \
  && chmod +x tailwindcss-linux-x64 \
  && mv tailwindcss-linux-x64 /usr/local/bin/tailwindcss

# Install production dependencies once, into the environment copied to runtime.
RUN uv sync --locked --no-dev

# Copy the rest of the app
COPY . /app

# Compile Tailwind CSS
RUN tailwindcss -i assets/css/input.css -o assets/css/output.css --minify

# Collect static files into /app/staticfiles (adjust STATIC_ROOT in settings accordingly)
# Verify that we ignore the input.css which contains invalid css for whitenoise
RUN python manage.py collectstatic --noinput --ignore "css/input.css"

# (Optional) Compile optimized .pyc files
RUN python -OO -m compileall -q /app

# ---- Final stage: minimal runtime image ----
FROM base AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DJANGO_SETTINGS_MODULE=rivalspy.settings \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_NO_SYNC=1 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Create a non-root user for runtime
RUN addgroup --system app && adduser --system --ingroup app app \
  && mkdir -p /app/staticfiles /app/mediafiles \
  && chown -R app:app /app /app/staticfiles /app/mediafiles

ENV UV_CACHE_DIR=/app/.cache/uv
RUN mkdir -p /app/.cache/uv && chown -R app:app /app/.cache

# Keep uv for operational commands; UV_NO_SYNC makes them reuse the built environment.
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

# Copy application files and the collected static files
COPY --from=builder /app /app

# Ensure ownership (runtime user)
RUN chown -R app:app /app

USER app

EXPOSE 8000

# Start immediately using the dependencies installed during the build.
CMD ["python", "-m", "uvicorn", "rivalspy.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
