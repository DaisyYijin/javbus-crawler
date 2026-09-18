# Override with a registry mirror when docker.io is unreachable, e.g.
#   docker build --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# Non-root user; /data is expected to be a mounted volume.
RUN mkdir -p /data && chown -R nobody:nogroup /data /app
USER nobody

EXPOSE 7878

# Default command: web UI (serve). One-shot crawl: docker run ... crawl --pages 1-5
ENTRYPOINT ["python", "-m", "app.main"]
CMD []
