FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# Non-root user; /data is expected to be a mounted volume.
RUN mkdir -p /data && chown -R nobody:nogroup /data /app
USER nobody

ENTRYPOINT ["python", "-m", "app.main"]
CMD ["--pages", "1"]
