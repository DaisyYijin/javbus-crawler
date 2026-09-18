# Override with a registry mirror when docker.io is unreachable, e.g.
#   docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY app/ app/
COPY entrypoint.sh /entrypoint.sh
RUN mkdir -p /data && chown -R nobody:nogroup /data /app && chmod +x /entrypoint.sh

EXPOSE 7878

# Starts as root to fix /data ownership (bind mounts are often root-owned on
# the host), then drops privileges to nobody via gosu. See entrypoint.sh.
# Default command: web UI (serve). One-shot crawl: docker run ... crawl --pages 1-5
ENTRYPOINT ["/entrypoint.sh", "python", "-m", "app.main"]
CMD []
