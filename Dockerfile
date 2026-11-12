# Share the Python interpreter and OS between the build and runtime environments.
FROM python:3.15.2-slim-bookworm AS base

# Compile platform-independent CSS natively, including when the Python image is
# cross-built (the Bun-based standalone compiler does not work under QEMU).
FROM --platform=$BUILDPLATFORM python:3.15.2-slim-bookworm AS assets
ARG BUILDARCH
ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

# Official v4.1.7 checksums, matching the checked-in CSS toolchain. Select only
# explicitly supported native build architectures and verify before execution.
RUN case "$BUILDARCH" in \
      amd64) TAILWIND_ASSET=tailwindcss-linux-x64; TAILWIND_SHA256=0706292935a9773c6c879e17d23625322e4490e7e8f7a0ad0e688faae4def981 ;; \
      arm64) TAILWIND_ASSET=tailwindcss-linux-arm64; TAILWIND_SHA256=8c419a662196f05726551ad006bd8341f47b8bedf8e0cb651687d9ae3c0ae066 ;; \
      *) echo "Unsupported asset build architecture: $BUILDARCH" >&2; exit 1 ;; \
    esac \
  && curl --fail --location --show-error --proto '=https' --tlsv1.2 \
      --retry 3 --connect-timeout 10 --max-time 600 \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v4.1.7/$TAILWIND_ASSET" \
      --output /tmp/tailwindcss \
  && echo "$TAILWIND_SHA256  /tmp/tailwindcss" | sha256sum --check --strict - \
  && install -m 0755 /tmp/tailwindcss /usr/local/bin/tailwindcss \
  && rm /tmp/tailwindcss
COPY . /app
RUN rm -f assets/css/output.css \
  && tailwindcss -i assets/css/input.css -o assets/css/output.css --minify \
  && test -s assets/css/output.css

# ---- Build stage: install target-platform Python dependencies ----
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.18 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
WORKDIR /app
COPY pyproject.toml uv.lock ./

# Install production dependencies once, into the environment copied to runtime.
RUN uv sync --locked --no-dev

# Copy the rest of the app
COPY . /app

# Only the platform-independent stylesheet crosses from the native asset stage.
COPY --from=assets /app/assets/css/output.css /app/assets/css/output.css

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

# Validate settings and apply migrations before opening the web listening socket.
CMD ["/bin/sh", "/app/bin/start-web"]
