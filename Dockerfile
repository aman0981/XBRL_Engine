# syntax=docker/dockerfile:1.7
#
# Multi-stage build: a builder layer compiles wheels (lxml + numpy + pillow
# wheels for arelle, asyncpg, etc.), then a slim runtime layer copies just the
# installed site-packages + app source. Cuts final image by ~150 MB versus a
# single-stage build that retains build-essential + apt caches.

ARG PYTHON_VERSION=3.12

# --- builder ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tools needed only at build time. lxml needs libxml/libxslt headers; psycopg
# isn't used (asyncpg is pure-binary) but build-essential covers anything else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Build into a private venv so we can copy a clean tree into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip && pip install -r /tmp/requirements.txt

# --- runtime ---------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    XIE_RAW_DATA_DIR=/data/raw \
    XIE_ARELLE_CACHE_DIR=/data/arelle_cache \
    XIE_LOG_JSON=true

# Runtime needs the shared libs lxml + arelle/pillow link against, plus curl
# for the HEALTHCHECK probe. Note: NO build-essential.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system xie \
    && useradd --system --gid xie --no-create-home --shell /usr/sbin/nologin xie

# Bring in the pre-built venv from the builder.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=xie:xie src ./src
COPY --chown=xie:xie alembic.ini .
COPY --chown=xie:xie alembic ./alembic

# Persistent volume mount points — declared so a bind/volume must be attached
# in compose / Hetzner deploy; the in-image dirs exist with the right owner.
RUN install -d -o xie -g xie /data /data/raw /data/arelle_cache

USER xie

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --retries=3 --start-period=30s \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

# why: --forwarded-allow-ips=* would trust X-Forwarded-* from any peer; behind
# a reverse proxy on the same Docker network the peer IP is in the RFC1918
# space. 127.0.0.1 covers app-on-same-host loopback testing. Override at
# deploy time if your proxy lives elsewhere.
CMD ["uvicorn", "xie.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips=172.16.0.0/12,127.0.0.1"]
