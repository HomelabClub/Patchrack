# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Docker on managed servers.

Patchrack talks to the Docker Engine API through `docker system dial-stdio`, run over the
existing SSH connection (with sudo when needed). No Docker port is opened and no agent is
installed. The same API calls Watchtower-style tools make are used to:

- find containers whose image has a newer version in its registry (digest comparison,
  done by the server's own Docker daemon, so registry access works as it does on the host)
- pull the new image and recreate the container with its exact configuration, rolling back
  to the old container if the new one does not start
- prune unused images, build cache, stopped containers and networks, reporting space freed
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from typing import Awaitable, Callable

from .sshops import SSHOpError, Target, _wrap, run

LineCb = Callable[[str], Awaitable[None] | None]
SKIP_LABEL = "patchrack.enable"           # set to "false" on a container to leave it alone
OLD_SUFFIX = "-patchrack-old"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class DockerError(SSHOpError):
    pass


class DockerNoAccess(DockerError):
    pass


class DockerStopped(DockerError):
    pass


async def _emit(cb: LineCb | None, line: str) -> None:
    if cb:
        res = cb(line)
        if asyncio.iscoroutine(res):
            await res


def human_bytes(n: float | int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


# ---- image references ------------------------------------------------------------
def split_ref(ref: str) -> tuple[str, str | None, str | None]:
    """'lscr.io/linuxserver/sonarr:latest' -> (repo, tag, digest)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    return ref, tag, digest


def familiar(repo: str) -> str:
    """Normalise a repository name the way Docker shows it ('docker.io/library/nginx' -> 'nginx')."""
    for prefix in ("docker.io/", "index.docker.io/", "registry-1.docker.io/"):
        if repo.startswith(prefix):
            repo = repo[len(prefix):]
    if repo.startswith("library/") and repo.count("/") == 1:
        repo = repo[len("library/"):]
    return repo


# ---- API transport ------------------------------------------------------------------
class DockerAPI:
    """Minimal HTTP/1.1 client for the Docker API, one request per dial-stdio process."""

    def __init__(self, conn, target: Target):
        self.conn = conn
        self.t = target

    async def request(self, method: str, path: str, body=None, *, on_json=None, timeout: float = 120):
        cmd = _wrap(self.t, "docker system dial-stdio", root=True)
        proc = await self.conn.create_process(cmd, encoding=None)
        try:
            if self.t.become == "sudo_password":
                proc.stdin.write(((self.t.sudo_password or "") + "\n").encode())
            data = b"" if body is None else json.dumps(body).encode()
            head = [f"{method} {path} HTTP/1.1", "Host: docker", "User-Agent: Patchrack",
                    "Connection: close", f"Content-Length: {len(data)}"]
            if body is not None:
                head.append("Content-Type: application/json")
            # stdin stays open until the response is read; closing it early cancels the request
            proc.stdin.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
            return await asyncio.wait_for(self._read(proc, on_json), timeout)
        except asyncio.TimeoutError:
            raise DockerError(f"Docker did not answer {method} {path.split('?')[0]} within {int(timeout)}s")
        finally:
            try:
                proc.stdin.write_eof()
            except Exception:
                pass
            proc.close()

    async def _read(self, proc, on_json):
        try:
            header = await proc.stdout.readuntil(b"\r\n\r\n")
        except Exception:
            err = b""
            try:
                err = await asyncio.wait_for(proc.stderr.read(), 3)
            except Exception:
                pass
            msg = err.decode(errors="replace").strip()
            if "not allowed" in msg or "password is required" in msg or "permission denied" in msg.lower():
                raise DockerNoAccess("Docker is installed, but Patchrack may not use it. Run the updated "
                                     "setup command on this server.")
            if "daemon running" in msg or "docker.sock" in msg or "connection refused" in msg.lower():
                raise DockerStopped("Docker is installed, but the Docker service isn't running")
            raise DockerError(f"Could not reach the Docker daemon: {msg or 'no response'}")
        lines = header.decode(errors="replace").split("\r\n")
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            raise DockerError(f"Unexpected answer from Docker: {lines[0][:80]}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        chunks: list[bytes] = []
        buf = b""

        async def feed(chunk: bytes):
            nonlocal buf
            if on_json is None:
                chunks.append(chunk)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    res = on_json(obj)
                    if asyncio.iscoroutine(res):
                        await res

        if headers.get("transfer-encoding", "").lower() == "chunked":
            while True:
                size_line = await proc.stdout.readuntil(b"\r\n")
                size = int(size_line.strip().split(b";")[0] or b"0", 16)
                if size == 0:
                    break
                await feed(await proc.stdout.readexactly(size))
                await proc.stdout.readexactly(2)
        elif "content-length" in headers:
            n = int(headers["content-length"])
            if n:
                await feed(await proc.stdout.readexactly(n))
        else:
            await feed(await proc.stdout.read())
        if on_json is not None and buf.strip():
            await feed(b"\n")
        raw = b"".join(chunks)
        data = None
        if raw and "json" in headers.get("content-type", ""):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
        return status, data, raw

    async def call(self, method: str, path: str, body=None, *, ok=(200, 201, 204, 304), **kw):
        status, data, raw = await self.request(method, path, body, **kw)
        if status not in ok:
            msg = (data or {}).get("message") if isinstance(data, dict) else raw.decode(errors="replace")[:300]
            raise DockerError(f"Docker refused {method} {path.split('?')[0]}: {msg or status}")
        return data

    async def get(self, path: str, **kw):
        return await self.call("GET", path, **kw)


async def docker_installed(conn, t: Target) -> bool:
    code, out = await run(conn, t, "command -v docker", timeout=15)
    return code == 0 and out.strip() != ""


async def free_bytes(conn, t: Target, path: str) -> int | None:
    code, out = await run(conn, t, f"df -B1 --output=avail {path} 2>/dev/null | tail -n1", timeout=15)
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


# ---- check ---------------------------------------------------------------------------
async def inspect(conn, t: Target, on_line: LineCb | None = None) -> dict:
    """Return Docker facts for the server, including which containers have image updates."""
    api = DockerAPI(conn, t)
    version = await api.get("/version")
    info = await api.get("/info")
    containers = await api.get("/containers/json?all=1")
    await _emit(on_line, f"Docker {version.get('Version')}: {len(containers)} container(s)")

    image_cache: dict[str, dict | None] = {}
    remote_cache: dict[str, tuple[str | None, str | None]] = {}

    async def image(ref_or_id: str):
        if ref_or_id not in image_cache:
            status, data, _ = await api.request("GET", f"/images/{urllib.parse.quote(ref_or_id, safe='/:@')}/json")
            image_cache[ref_or_id] = data if status == 200 else None
        return image_cache[ref_or_id]

    async def remote_digest(ref: str):
        if ref not in remote_cache:
            status, data, raw = await api.request(
                "GET", f"/distribution/{urllib.parse.quote(ref, safe='/:@')}/json", timeout=45)
            if status == 200 and data:
                remote_cache[ref] = (data.get("Descriptor", {}).get("digest"), None)
            else:
                msg = (data or {}).get("message") if isinstance(data, dict) else raw.decode(errors="replace")
                remote_cache[ref] = (None, _registry_hint(msg or f"HTTP {status}"))
        return remote_cache[ref]

    out = []
    for c in containers:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
        labels = c.get("Labels") or {}
        ref = c.get("Image", "")
        if ref.startswith("sha256:"):
            detail = await api.get(f"/containers/{c['Id']}/json")
            ref = detail.get("Config", {}).get("Image", ref)
        entry = {
            "id": c["Id"], "name": name, "image": ref, "state": c.get("State"), "status": c.get("Status"),
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "created": c.get("Created"), "check": "current", "note": "",
        }
        out.append(entry)
        if name.endswith(OLD_SUFFIX):
            entry.update(check="excluded", note="Left over from an interrupted update")
            continue
        if labels.get(SKIP_LABEL, "").lower() == "false":
            entry.update(check="excluded", note=f"Excluded with the {SKIP_LABEL}=false label")
            continue
        if "patchrack" in ref.lower() or "patchrack" in name.lower():
            entry.update(check="self", note="This is Patchrack itself. Update it from its own host.")
            continue
        repo, tag, digest = split_ref(ref)
        if digest or ref.startswith("sha256:"):
            entry.update(check="pinned", note="Pinned to an exact image version")
            continue
        full = f"{repo}:{tag or 'latest'}"
        entry["image"] = full
        # a newer image may already be pulled; the running one is then no longer tagged
        tagged = await image(full)
        if tagged and tagged.get("Id") and tagged["Id"] != c["ImageID"]:
            entry.update(check="update", note="A newer image is already downloaded")
            continue
        local = await image(c["ImageID"])
        repo_digests = [d.split("@", 1)[1] for d in (local or {}).get("RepoDigests") or []
                        if familiar(d.split("@", 1)[0]) == familiar(repo)]
        if not repo_digests:
            entry.update(check="local", note="Built locally, so there is no registry version to compare")
            continue
        remote, err = await remote_digest(full)
        if err:
            entry.update(check="unknown", note=err)
        elif remote not in repo_digests:
            entry.update(check="update", note="A newer image is available")
    updates = [e for e in out if e["check"] == "update"]
    if updates:
        await _emit(on_line, "Container updates: " + ", ".join(e["name"] for e in updates))
    df = await disk_usage(api)
    root = info.get("DockerRootDir") or "/var/lib/docker"
    return {
        "version": version.get("Version"), "api": version.get("ApiVersion"), "root": root,
        "containers": out, "updates": len(updates), "running": sum(1 for e in out if e["state"] == "running"),
        "reclaimable": df, "free": await free_bytes(conn, t, root), "checked_at": time.time(),
    }


def _registry_hint(msg: str) -> str:
    low = msg.lower()
    if "unauthorized" in low or "denied" in low or "authentication" in low:
        return "The registry requires a login, so Patchrack can't compare versions"
    if "not found" in low or "manifest unknown" in low:
        return "This tag was not found in its registry"
    if "toomanyrequests" in low or "rate limit" in low:
        return "The registry rate limit was reached; try again later"
    return f"Couldn't reach the registry: {msg[:140]}"


async def disk_usage(api: DockerAPI) -> dict:
    status, data, _ = await api.request("GET", "/system/df", timeout=120)
    if status != 200 or not data:
        return {}
    images = sum(max(i.get("Size", 0) - max(i.get("SharedSize", 0), 0), 0)
                 for i in data.get("Images") or [] if i.get("Containers", 0) == 0)
    build = sum(b.get("Size", 0) for b in data.get("BuildCache") or [] if not b.get("InUse"))
    stopped = sum(c.get("SizeRw", 0) or 0 for c in data.get("Containers") or [] if c.get("State") != "running")
    return {"images": images, "build_cache": build, "containers": stopped, "total": images + build + stopped,
            "unused_images": sum(1 for i in data.get("Images") or [] if i.get("Containers", 0) == 0),
            "stopped_containers": sum(1 for c in data.get("Containers") or [] if c.get("State") != "running")}


# ---- update -----------------------------------------------------------------------------
async def pull(api: DockerAPI, ref: str, on_line: LineCb) -> None:
    repo, tag, _ = split_ref(ref)
    seen: set[str] = set()
    errors: list[str] = []

    async def progress(obj: dict):
        if obj.get("error"):
            errors.append(obj["error"])
            return
        status = obj.get("status", "")
        if obj.get("progressDetail") and status in ("Downloading", "Extracting", "Waiting"):
            return
        key = f"{obj.get('id', '')}{status}"
        if key in seen:
            return
        seen.add(key)
        if status.startswith(("Status:", "Digest:")) or status in ("Pull complete", "Already exists"):
            await _emit(on_line, f"  {obj.get('id', '') + ': ' if obj.get('id') else ''}{status}")

    qs = urllib.parse.urlencode({"fromImage": repo, "tag": tag or "latest"})
    await api.call("POST", f"/images/create?{qs}", on_json=progress, timeout=1800)
    if errors:
        raise DockerError(f"Pulling {ref} failed: {errors[-1]}")


def _create_body(info: dict, image_ref: str, network_mode: str | None = None) -> tuple[dict, dict]:
    """Build a create request that reproduces the container, plus the extra networks to attach."""
    cfg = dict(info["Config"])
    host = dict(info["HostConfig"])
    cfg["Image"] = image_ref
    cid = info["Id"]
    if cfg.get("Hostname") == cid[:12]:
        cfg.pop("Hostname")           # default hostname is the old container id; let Docker assign
    cfg.pop("MacAddress", None)        # moved to the network endpoint in newer API versions
    if network_mode:
        host["NetworkMode"] = network_mode
    if (host.get("NetworkMode") or "").startswith("container:"):
        # these come from the container whose network is shared and can't be set again
        cfg.pop("Hostname", None)
        cfg.pop("Domainname", None)
    # keep anonymous volumes (e.g. a database's VOLUME) instead of starting with empty ones
    actual = {m.get("Destination"): m.get("Name") for m in info.get("Mounts") or []
              if m.get("Type") == "volume" and m.get("Name")}
    # Compose declares anonymous volumes as mounts without a source: point them at the existing volume
    mounts = []
    for m in host.get("Mounts") or []:
        m = dict(m)
        if m.get("Type") == "volume" and not m.get("Source") and actual.get(m.get("Target")):
            m["Source"] = actual[m["Target"]]
        mounts.append(m)
    if mounts:
        host["Mounts"] = mounts
    binds = list(host.get("Binds") or [])
    covered = {b.split(":")[1] for b in binds if b.count(":") >= 1}
    covered |= {m.get("Target") for m in mounts}
    # `docker run -v /path` style anonymous volumes appear only in the container's mounts
    for dest, vol in actual.items():
        if HEX64.match(vol) and dest not in covered:
            binds.append(f"{vol}:{dest}")
    host["Binds"] = binds or None
    mode = host.get("NetworkMode") or ""
    endpoints: dict[str, dict] = {}
    if not mode.startswith("container:") and mode not in ("host", "none"):
        for net, ep in (info.get("NetworkSettings", {}).get("Networks") or {}).items():
            cleaned = {k: ep[k] for k in ("IPAMConfig", "Links", "DriverOpts", "MacAddress") if ep.get(k)}
            aliases = [a for a in ep.get("Aliases") or [] if a not in (cid[:12], cid)]
            if aliases:
                cleaned["Aliases"] = aliases
            endpoints[net] = cleaned
    first = mode if mode in endpoints else next(iter(endpoints), None)
    body = {**cfg, "HostConfig": host}
    if first:
        body["NetworkingConfig"] = {"EndpointsConfig": {first: endpoints[first]}}
    extra = {k: v for k, v in endpoints.items() if k != first}
    return body, extra


async def _wait_ready(api: DockerAPI, cid: str, timeout: int = 90) -> str:
    """Wait until the container is running steadily (and healthy, if it has a health check)."""
    deadline = time.time() + timeout
    await asyncio.sleep(4)
    stable = 0
    while True:
        info = await api.get(f"/containers/{cid}/json")
        state = info.get("State", {})
        health = (state.get("Health") or {}).get("Status")
        if info.get("RestartCount", 0) >= 3:
            raise DockerError(f"The new container keeps restarting (exit code {state.get('ExitCode')})")
        if not state.get("Running") and not state.get("Restarting"):
            raise DockerError(f"The new container stopped (exit code {state.get('ExitCode')})")
        if health == "unhealthy":
            raise DockerError("The new container reports itself unhealthy")
        if state.get("Restarting") or health not in (None, "healthy"):
            stable = 0
        else:
            stable += 1
            if stable >= 2:
                return health or "running"
        if time.time() > deadline:
            raise DockerError("The new container did not become ready in time")
        await asyncio.sleep(3)


async def recreate(api: DockerAPI, cid: str, image_ref: str | None, on_line: LineCb,
                   network_mode: str | None = None, dep_failures: list | None = None) -> str:
    """Replace a container with an identical one (optionally on a new image). Rolls back on failure."""
    info = await api.get(f"/containers/{cid}/json")
    name = info["Name"].lstrip("/")
    was_running = info.get("State", {}).get("Running", False)
    image_ref = image_ref or info["Config"]["Image"]
    body, extra_nets = _create_body(info, image_ref, network_mode)
    labels = dict(body.get("Labels") or {})
    if "com.docker.compose.image" in labels:
        # Compose remembers which image it created the container from; keep it in sync so the
        # next `docker compose up` doesn't recreate the container again
        img = await api.get(f"/images/{urllib.parse.quote(image_ref, safe='/:@')}/json")
        labels["com.docker.compose.image"] = img["Id"]
        body["Labels"] = labels

    all_containers = await api.get("/containers/json?all=1")
    dependents = [c["Id"] for c in all_containers
                  if (c.get("HostConfig") or {}).get("NetworkMode") in (f"container:{cid}", f"container:{name}")]

    timeout = info["Config"].get("StopTimeout") or 10
    await _emit(on_line, f"  stopping {name}")
    await api.call("POST", f"/containers/{cid}/stop?t={timeout}", timeout=timeout + 60)
    old_name = name + OLD_SUFFIX
    await api.call("POST", f"/containers/{cid}/rename?name={urllib.parse.quote(old_name)}")
    new_id = None
    try:
        created = await api.call("POST", f"/containers/create?name={urllib.parse.quote(name)}", body)
        new_id = created["Id"]
        for net, ep in extra_nets.items():
            await api.call("POST", f"/networks/{urllib.parse.quote(net)}/connect",
                           {"Container": new_id, "EndpointConfig": ep})
        if was_running:
            await _emit(on_line, f"  starting new {name}")
            await api.call("POST", f"/containers/{new_id}/start")
            state = await _wait_ready(api, new_id)
            await _emit(on_line, f"  {name} is {state}")
    except Exception as exc:
        await _emit(on_line, f"  {exc}. Restoring the previous container.")
        if new_id:
            await api.request("DELETE", f"/containers/{new_id}?force=true")
        await api.request("POST", f"/containers/{cid}/rename?name={urllib.parse.quote(name)}")
        if was_running:
            await api.request("POST", f"/containers/{cid}/start")
        raise DockerError(f"{name}: {exc}. The previous container was restored.") from exc
    await api.call("DELETE", f"/containers/{cid}?force=true&v=false")

    for dep in dependents:
        dep_name = dep[:12]
        try:
            dep_info = await api.get(f"/containers/{dep}/json")
            dep_name = dep_info["Name"].lstrip("/")
            await _emit(on_line, f"  reconnecting {dep_name}, which shares {name}'s network")
            await recreate(api, dep, None, on_line, network_mode=f"container:{new_id}",
                           dep_failures=dep_failures)
        except DockerError as exc:
            if dep_failures is not None:
                dep_failures.append((dep_name, str(exc)))
            await _emit(on_line, f"  could not reconnect {dep_name}: {exc}")
    return new_id


async def update_containers(conn, t: Target, container_ids: list[str], remove_old: bool,
                            on_line: LineCb, on_phase: LineCb) -> dict:
    api = DockerAPI(conn, t)
    updated, failed, unchanged = [], [], []
    old_images: set[str] = set()
    # track by name: recreating a container also recreates the ones sharing its network,
    # which gives those new ids
    names = []
    for cid in container_ids:
        try:
            names.append((await api.get(f"/containers/{cid}/json"))["Name"].lstrip("/"))
        except DockerError:
            failed.append((cid[:12], "container no longer exists"))
    for cname in names:
        try:
            info = await api.get(f"/containers/{urllib.parse.quote(cname)}/json")
        except DockerError:
            failed.append((cname, "container no longer exists"))
            continue
        name = info["Name"].lstrip("/")
        ref = info["Config"]["Image"]
        repo, tag, digest = split_ref(ref)
        if digest or ref.startswith("sha256:"):
            unchanged.append(name)
            continue
        ref = f"{repo}:{tag or 'latest'}"
        await on_phase(f"Updating {name}")
        try:
            await _emit(on_line, f"$ docker pull {ref}")
            await pull(api, ref, on_line)
            new_image = (await api.get(f"/images/{urllib.parse.quote(ref, safe='/:@')}/json"))["Id"]
            if new_image == info["Image"]:
                await _emit(on_line, f"  {name} already runs the newest image")
                unchanged.append(name)
                continue
            dep_failures: list = []
            await recreate(api, info["Id"], ref, on_line, dep_failures=dep_failures)
            old_images.add(info["Image"])
            updated.append(name)
            failed.extend(dep_failures)
        except DockerError as exc:
            await _emit(on_line, str(exc))
            failed.append((name, str(exc)))
    removed = 0
    if remove_old and old_images:
        await on_phase("Removing the old images")
        for img in old_images:
            status, _, _ = await api.request("DELETE", f"/images/{img}")
            if status == 200:
                removed += 1
        if removed:
            await _emit(on_line, f"Removed {removed} old image{'' if removed == 1 else 's'}")
        else:
            await _emit(on_line, "The old images are still used by other containers, so they were kept")
    return {"updated": updated, "failed": failed, "unchanged": unchanged, "images_removed": removed}


# ---- prune -----------------------------------------------------------------------------
async def prune(conn, t: Target, opts: dict, root: str, on_line: LineCb, on_phase: LineCb) -> dict:
    api = DockerAPI(conn, t)
    before = await free_bytes(conn, t, root)
    reclaimed, removed = 0, {}

    async def step(label, method_path, key, count_key):
        nonlocal reclaimed
        await on_phase(label)
        data = await api.call("POST", method_path, timeout=900) or {}
        space = data.get(key) or 0
        n = len(data.get(count_key) or [])
        reclaimed += space
        removed[count_key] = n
        await _emit(on_line, f"  removed {n}, freed {human_bytes(space)}")

    if opts.get("containers"):
        await step("Removing stopped containers", "/containers/prune", "SpaceReclaimed", "ContainersDeleted")
    if opts.get("images"):
        f = urllib.parse.quote(json.dumps({"dangling": ["false"]}))
        await step("Removing unused images", f"/images/prune?filters={f}", "SpaceReclaimed", "ImagesDeleted")
    if opts.get("build_cache"):
        await step("Removing build cache", "/build/prune", "SpaceReclaimed", "CachesDeleted")
    if opts.get("networks"):
        await step("Removing unused networks", "/networks/prune", "SpaceReclaimed", "NetworksDeleted")
    after = await free_bytes(conn, t, root)
    gained = (after - before) if (after is not None and before is not None) else None
    return {"reclaimed": reclaimed, "free_before": before, "free_after": after, "gained": gained,
            "removed": removed}
