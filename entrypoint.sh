#!/bin/sh
# Docker entrypoint: running as root, fix /data ownership (bind mounts may be
# owned by root on the host), then drop privileges to nobody before serving.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R nobody:nogroup /data 2>/dev/null || true
    exec gosu nobody:nogroup "$@"
fi

exec "$@"
