# Multi-stage build:
#   1. tailwind  - rebuilds app/static/app.css from current templates
#   2. runtime   - slim python base, app code + built CSS, runs gunicorn+uvicorn

# --- Stage 1: Tailwind CSS build ----------------------------------------------
FROM debian:bookworm-slim AS tailwind

ARG TAILWIND_VERSION=3.4.13
ARG TARGETPLATFORM
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN case "$TARGETPLATFORM" in \
      "linux/amd64") PLATFORM="linux-x64" ;; \
      "linux/arm64") PLATFORM="linux-arm64" ;; \
      *) echo "Unsupported platform: $TARGETPLATFORM" && exit 1 ;; \
    esac && \
    curl -sSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-${PLATFORM}" && \
    chmod +x /usr/local/bin/tailwindcss

COPY tailwind ./tailwind
COPY app/templates ./app/templates
RUN tailwindcss \
      -c tailwind/tailwind.config.js \
      -i tailwind/input.css \
      -o app/static/app.css \
      --minify

# --- Stage 2: Runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq for psycopg, ca-certs for outbound HTTPS (yfinance, kite, bhavcopy).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpq5 ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --shell /usr/sbin/nologin app
WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY run.py ./run.py
# Bring the built CSS over (vendor JS is checked in under app/static/vendor).
COPY --from=tailwind /build/app/static/app.css ./app/static/app.css

# Data dir is a volume in compose; create it so the path always exists.
RUN mkdir -p /app/data && chown -R app:app /app

USER app
EXPOSE 8000

# Tini reaps zombies + handles SIGTERM cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Migrations run inside the same process before workers fork.
CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --workers 2 --timeout 180 --access-logfile - --error-logfile -"]
