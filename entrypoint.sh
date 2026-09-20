#!/bin/sh
# Docker entrypoint: running as root, fix /data ownership (bind mounts may be
# owned by root on the host), then drop privileges to nobody. When a docker
# socket is mounted (web one-click self-update), run with the socket's owning
# GID so access is granted via the group bits instead of chmod 666 (a
# world-writable socket would let every host user control Docker).
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R nobody:nogroup /data 2>/dev/null || true
    RUN_USER="nobody:nogroup"
    if [ -S /var/run/docker.sock ]; then
        # gosu accepts numeric GIDs in user:group. Socket perms are typically
        # srw-rw---- root:docker, so adopting GID=docker grants rw access.
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)
        if [ -n "$SOCK_GID" ]; then
            RUN_USER="nobody:$SOCK_GID"
        fi
    fi
    # nobody's HOME (/nonexistent) cannot host the p115client import-time
    # cache dir (~/.p115client.cache.d) — give the process a writable HOME.
    HOME=/data
    export HOME
    exec gosu "$RUN_USER" "$@"
fi

exec "$@"
