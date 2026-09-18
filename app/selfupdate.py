"""Self-update: pull the newest image via the Docker API (docker.sock) and
replace this container with an identical one running the new image.

Requires /var/run/docker.sock to be mounted into the container (see
docker-compose.yml). When the socket is absent, callers fall back to
printing host-side commands.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("seedmm.selfupdate")

IMAGE = os.getenv("SELFUPDATE_IMAGE", "ghcr.io/daisyyijin/javbus-crawler:latest")

_client = None


def docker_client():
    """Return a Docker API client, or None when unavailable."""
    global _client
    if _client is None:
        try:
            import docker  # docker SDK for Python

            _client = docker.from_env(timeout=300)
            _client.ping()
        except Exception as exc:
            log.debug("docker API unavailable: %s", exc)
            _client = False
    return _client or None


def docker_available() -> bool:
    return docker_client() is not None


def _find_self(client):
    """Locate this container via its HOSTNAME (short container id)."""
    me = os.environ.get("HOSTNAME", "")
    if not me:
        raise RuntimeError("HOSTNAME 未设置，无法定位当前容器")
    for c in client.containers.list(all=True):
        if c.id.startswith(me):
            return c
    raise RuntimeError("未找到当前容器（HOSTNAME=%s）" % me)


def _convert_ports(port_bindings: dict | None):
    """HostConfig.PortBindings -> docker SDK create(ports=...) format."""
    if not port_bindings:
        return None
    out = {}
    for cport, binds in port_bindings.items():
        if not binds:
            continue
        b = binds[0]
        if b.get("HostIp"):
            out[cport] = (b["HostIp"], int(b["HostPort"]))
        else:
            out[cport] = int(b["HostPort"])
    return out or None


# Runs inside the one-shot switch-over helper container (same image, docker
# socket mounted, root). argv: [old_id, new_id, helper_name]
_HELPER_SCRIPT = """\
import sys, time
import docker

time.sleep(2)  # let the parent container's HTTP response flush out first
client = docker.from_env(timeout=60)
old_c = client.containers.get(sys.argv[1])
new_c = client.containers.get(sys.argv[2])
try:
    old_c.stop(timeout=5)   # frees the host port
    new_c.start()           # new container takes over
except Exception as exc:
    print("switch-over failed:", exc, flush=True)
    try:
        old_c.start()       # rollback: the old container keeps serving
    except Exception:
        pass
    sys.exit(1)
old_c.remove(force=True)    # reap the old container
print("switch-over complete", flush=True)
client.containers.get(sys.argv[3]).remove(force=True)  # remove the helper itself
"""


def perform_self_update() -> dict:
    """Pull the latest image and replace this container.

    Returns {"ok": True, "updated": bool, ...}. Raises RuntimeError on
    unrecoverable failure; the old container keeps running on rollback.
    """
    client = docker_client()
    if client is None:
        raise RuntimeError(
            "Docker API 不可用：需要在 docker-compose.yml 挂载 /var/run/docker.sock"
        )

    self_c = _find_self(client)
    current_image_id = self_c.attrs["Image"]  # sha256:...

    log.info("拉取镜像 %s …", IMAGE)
    try:
        new_image = client.images.pull(IMAGE)
    except Exception as exc:
        raise RuntimeError(f"拉取镜像失败: {exc}") from exc

    if new_image.id == current_image_id:
        log.info("镜像已是最新（%s），无需更新", IMAGE)
        return {"ok": True, "updated": False, "message": "镜像已是最新，无需更新"}

    name = self_c.name
    attrs = self_c.attrs
    cfg, host = attrs["Config"], attrs["HostConfig"]
    old_name = f"{name}-old-{int(time.time())}"

    log.info("重建容器 %s（新镜像 %s）", name, new_image.id[:19])
    self_c.rename(old_name)

    def _rollback(exc: Exception, old_stopped: bool) -> RuntimeError:
        """Undo rename (+restart) so the old container keeps serving."""
        log.error("更新失败，回滚: %s", exc)
        old = None
        try:
            old = client.containers.get(old_name)
            old.rename(name)
            if old_stopped:
                old.start()
        except Exception as rex:
            log.error("回滚不完整，请手动恢复 %s: %s", old_name, rex)
        return RuntimeError(f"更新失败（已回滚）: {exc}")

    try:
        new_c = client.containers.create(
            image=IMAGE,
            name=name,
            detach=True,
            environment=cfg.get("Env") or None,
            labels={k: v for k, v in (cfg.get("Labels") or {}).items()
                    if not k.startswith("com.docker.compose")},
            hostname=cfg.get("Hostname") or None,
            ports=_convert_ports(host.get("PortBindings")),
            volumes=host.get("Binds") or None,
            restart_policy=host.get("RestartPolicy") or None,
            network_mode=host.get("NetworkMode") or None,
        )
    except Exception as exc:
        raise _rollback(exc, old_stopped=False) from exc

    # The old container cannot stop *itself* and then start the new one (its
    # process dies with the stop). A short-lived helper container, built from
    # the same image and sharing the docker socket, performs the switch-over:
    #   wait 2s (let this HTTP response flush) -> stop old -> start new ->
    #   remove old -> remove itself. On failure it restarts the old container.
    helper_name = f"{name}-switch"
    try:
        client.containers.get(helper_name).remove(force=True)
    except Exception:
        pass
    try:
        helper = client.containers.create(
            image=attrs["Config"]["Image"] or IMAGE,  # current image: has the SDK
            name=helper_name,
            entrypoint=["python", "-c", _HELPER_SCRIPT, self_c.id, new_c.id, helper_name],
            volumes=["/var/run/docker.sock:/var/run/docker.sock"],
        )
        helper.start()
    except Exception as exc:
        try:
            new_c.remove(force=True)
        except Exception:
            pass
        raise _rollback(exc, old_stopped=False) from exc

    return {
        "ok": True,
        "updated": True,
        "message": "正在切换到新容器（约 5-15 秒），页面将自动恢复",
        "new_image": new_image.id[:19],
    }
