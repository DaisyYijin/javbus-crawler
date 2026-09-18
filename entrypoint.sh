#!/bin/sh
# Docker entrypoint: running as root, fix /data ownership (bind mounts may be
# owned by root on the host), make the docker socket usable by the app user
# (needed for web one-click self-update), then drop privileges to nobody.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R nobody:nogroup /data 2>/dev/null || true
    if [ -S /var/run/docker.sock ]; then
        # Grant the unprivileged app user access so the web UI can self-update.
        # (Equivalent to adding the app user to the host's docker group.)
        chmod 666 /var/run/docker.sock 2>/dev/null || true
    fi
    exec gosu nobody:nogroup "$@"
fi

exec "$@"
