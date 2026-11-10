# SPDX-License-Identifier: AGPL-3.0-or-later
FROM python:3.13-slim

# Set by the release workflow; shown in the app as the "Source code" link
ARG PATCHRACK_SOURCE_URL=""
LABEL org.opencontainers.image.title="Homelab Club Patchrack" \
      org.opencontainers.image.description="Check and install apt and Docker container updates on your Linux servers" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="${PATCHRACK_SOURCE_URL}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATCHRACK_DATA=/data \
    PATCHRACK_SOURCE_URL=${PATCHRACK_SOURCE_URL}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as an unprivileged user; /data holds the encrypted database
RUN useradd --system --uid 10001 --home /app patchrack \
    && mkdir -p /data && chown patchrack:patchrack /data
USER patchrack
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
