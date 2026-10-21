# syntax=docker/dockerfile:1
#
# One Dockerfile for every VoltMart Python service.
#
# The build context is the `services/` directory so that the shared
# `voltmart_common` package can be copied in alongside the service itself:
#
#   docker build -f services/Dockerfile --build-arg SERVICE=cart -t voltmart-cart services/
#
# Keeping a single Dockerfile means a change to the base image, the user model
# or the entrypoint happens once instead of seven times.

FROM python:3.12-slim AS base

ARG SERVICE
ARG SERVICE_VERSION=1.0.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# curl is used by the container healthcheck only. Everything else the slim
# image already provides.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own layer, so editing application code does not
# re-run pip install.
COPY ${SERVICE}/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The shared library, then the service's own code.
COPY shared/voltmart_common /app/voltmart_common
COPY ${SERVICE}/ /app/

# Run as a non-root user with no login shell and no write access to the code it
# executes. If the process is compromised it cannot modify its own image layer.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 voltmart \
    && chown -R root:root /app \
    && chmod -R a-w /app
USER 10001

ENV SERVICE_VERSION=${SERVICE_VERSION} \
    PORT=8080 \
    BIND_HOST=0.0.0.0 \
    STORAGE_BACKEND=memory \
    LOG_LEVEL=info

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
