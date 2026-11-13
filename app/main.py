# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Homelab Club Patchrack - web app to check and apply apt updates on Debian/Ubuntu servers."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncssh
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import crypto, db, notify, release
from .jobs import bus, get_server, manager, publish_server, run_scheduled_checks, scheduler_loop, server_public
from .sshops import SSHOpError, HostKeyChanged, Target, connect, fingerprint, run, test_sudo

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("patchrack")
logging.getLogger("asyncssh").setLevel(logging.WARNING)
STATIC = Path(__file__).parent / "static"
VERSION = "0.2.1"
# Where the source code of this build lives. AGPL section 13 asks that people using a
# (modified) copy over the network can get its source, so the app links to it.
SOURCE_URL = os.environ.get("PATCHRACK_SOURCE_URL", "").strip()


def ensure_app_key() -> None:
    if not db.has_setting("app_private_key"):
        key = asyncssh.generate_private_key("ssh-ed25519", comment="patchrack")
        db.set_setting("app_private_key", key.export_private_key().decode())
        log.info("Generated the Patchrack SSH key pair")


def app_public_key() -> str:
    key = asyncssh.import_private_key(db.get_setting("app_private_key"))
    return key.export_public_key().decode().strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    db.verify_master_key()
    ensure_app_key()
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Homelab Club Patchrack", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=crypto.SESSION_KEY, session_cookie="patchrack_session",
                   max_age=60 * 60 * 12, same_site="strict",
                   https_only=os.environ.get("COOKIE_SECURE", "false").lower() == "true")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Simple CSRF guard on top of SameSite=strict: state-changing API calls need a custom header,
    # which browsers never add to cross-site form posts.
    if request.url.path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE"):
        if request.headers.get("x-patchrack") != "1":
            return JSONResponse({"detail": "Missing request header"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'")
    return resp


# ---- auth -----------------------------------------------------------------------
_failures: dict[str, list[float]] = {}


def user_required(request: Request) -> str:
    user = request.session.get("user")
    if not user or not db.q1("SELECT 1 FROM users WHERE username=?", (user,)):
        raise HTTPException(401, "Sign in to continue")
    return user


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.get("/api/session")
def session_state(request: Request):
    has_user = db.q1("SELECT 1 FROM users") is not None
    return {"setup_required": not has_user, "user": request.session.get("user") if has_user else None,
            "version": VERSION, "license": "AGPL-3.0-or-later", "source_url": SOURCE_URL or None}


@app.post("/api/setup")
def setup(body: Credentials, request: Request):
    if db.q1("SELECT 1 FROM users"):
        raise HTTPException(409, "An account already exists")
    if len(body.password) < 10:
        raise HTTPException(400, "Use a password of at least 10 characters")
    db.ex("INSERT INTO users(username,pw_hash) VALUES(?,?)", (body.username.strip(),
                                                              crypto.hash_password(body.password)))
    request.session["user"] = body.username.strip()
    return {"ok": True}


@app.post("/api/login")
def login(body: Credentials, request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _failures.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many failed sign-ins. Wait five minutes and try again.")
    row = db.q1("SELECT * FROM users WHERE username=?", (body.username.strip(),))
    if not row or not crypto.verify_password(body.password, row["pw_hash"]):
        _failures[ip] = recent + [time.time()]
        raise HTTPException(401, "Username or password is incorrect")
    _failures.pop(ip, None)
    request.session.clear()
    request.session["user"] = row["username"]
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


class PasswordChange(BaseModel):
    current: str
    new: str = Field(min_length=10, max_length=256)


@app.put("/api/account/password")
def change_password(body: PasswordChange, user: str = Depends(user_required)):
    row = db.q1("SELECT * FROM users WHERE username=?", (user,))
    if not crypto.verify_password(body.current, row["pw_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    db.ex("UPDATE users SET pw_hash=? WHERE id=?", (crypto.hash_password(body.new), row["id"]))
    return {"ok": True}


# ---- servers ----------------------------------------------------------------------
HOST_RE = re.compile(r"^[A-Za-z0-9.\-_:]+$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]*\$?$")


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(22, ge=1, le=65535)
    username: str = Field("patchrack", min_length=1, max_length=32)
    key_source: str = Field("app", pattern="^(app|custom)$")
    private_key: str | None = None      # empty = keep current
    passphrase: str | None = None
    become: str = Field("sudo", pattern="^(sudo|sudo_password|root)$")
    sudo_password: str | None = None
    upgrade_mode: str = Field("full", pattern="^(full|safe)$")
    autoremove: bool = True
    reboot_policy: str = Field("if_required", pattern="^(never|if_required|always)$")
    notes: str | None = Field(None, max_length=500)
    group_id: int | None = None


def _validate(body: ServerIn) -> None:
    if body.group_id is not None and not db.q1("SELECT 1 FROM server_groups WHERE id=?", (body.group_id,)):
        raise HTTPException(400, "That group doesn't exist anymore")
    if not HOST_RE.match(body.host):
        raise HTTPException(400, "Host must be a hostname or IP address")
    if not USER_RE.match(body.username):
        raise HTTPException(400, "Username contains characters Linux does not allow")
    if body.key_source == "custom" and body.private_key:
        try:
            asyncssh.import_private_key(body.private_key, passphrase=body.passphrase or None)
        except Exception:
            raise HTTPException(400, "The private key could not be read. Paste the full key in OpenSSH "
                                     "format, and enter its passphrase if it has one.")


def _save_secrets(sid: int, body: ServerIn) -> None:
    sets, args = [], []
    if body.key_source == "custom" and body.private_key:
        sets += ["private_key_enc=?", "passphrase_enc=?"]
        args += [crypto.encrypt(body.private_key.strip() + "\n", f"server:{sid}:private_key"),
                 crypto.encrypt(body.passphrase, f"server:{sid}:passphrase")]
    if body.key_source == "app":
        sets += ["private_key_enc=NULL", "passphrase_enc=NULL"]
    if body.become == "sudo_password" and body.sudo_password:
        sets.append("sudo_password_enc=?")
        args.append(crypto.encrypt(body.sudo_password, f"server:{sid}:sudo_password"))
    if body.become != "sudo_password":
        sets.append("sudo_password_enc=NULL")
    if sets:
        db.ex(f"UPDATE servers SET {', '.join(sets)} WHERE id=?", (*args, sid))


@app.get("/api/servers")
def list_servers(user: str = Depends(user_required)):
    return [server_public(r) for r in db.q("SELECT * FROM servers ORDER BY name COLLATE NOCASE")]


@app.post("/api/servers")
def create_server(body: ServerIn, user: str = Depends(user_required)):
    _validate(body)
    if body.key_source == "custom" and not body.private_key:
        raise HTTPException(400, "Paste the private key, or use the Patchrack key")
    if body.become == "sudo_password" and not body.sudo_password:
        raise HTTPException(400, "Enter the sudo password, or choose passwordless sudo")
    sid = db.ex("INSERT INTO servers(name,host,port,username,key_source,become,upgrade_mode,autoremove,"
                "reboot_policy,notes,group_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (body.name.strip(), body.host.strip(), body.port, body.username, body.key_source,
                 body.become, body.upgrade_mode, int(body.autoremove), body.reboot_policy, body.notes,
                 body.group_id, time.time()))
    _save_secrets(sid, body)
    publish_server(sid)
    return server_public(get_server(sid))


@app.put("/api/servers/{sid}")
def update_server(sid: int, body: ServerIn, user: str = Depends(user_required)):
    row = get_server(sid)
    if not row:
        raise HTTPException(404, "Server not found")
    _validate(body)
    if body.key_source == "custom" and not body.private_key and not row["private_key_enc"]:
        raise HTTPException(400, "Paste the private key, or use the Patchrack key")
    if body.become == "sudo_password" and not body.sudo_password and not row["sudo_password_enc"]:
        raise HTTPException(400, "Enter the sudo password, or choose passwordless sudo")
    host_changed = (body.host.strip(), body.port) != (row["host"], row["port"])
    db.ex("UPDATE servers SET name=?,host=?,port=?,username=?,key_source=?,become=?,upgrade_mode=?,"
          "autoremove=?,reboot_policy=?,notes=?,group_id=?"
          + (",host_key=NULL,host_key_fp=NULL" if host_changed else "") + " WHERE id=?",
          (body.name.strip(), body.host.strip(), body.port, body.username, body.key_source, body.become,
           body.upgrade_mode, int(body.autoremove), body.reboot_policy, body.notes, body.group_id, sid))
    _save_secrets(sid, body)
    publish_server(sid)
    return server_public(get_server(sid))


@app.delete("/api/servers/{sid}")
def delete_server(sid: int, user: str = Depends(user_required)):
    if manager.busy_job(sid):
        raise HTTPException(409, "Wait for the running job to finish first")
    db.ex("DELETE FROM servers WHERE id=?", (sid,))
    bus.publish("server_deleted", {"id": sid})
    return {"ok": True}


@app.post("/api/servers/{sid}/forget-host-key")
def forget_host_key(sid: int, user: str = Depends(user_required)):
    db.ex("UPDATE servers SET host_key=NULL, host_key_fp=NULL, last_error=NULL, status='unknown' WHERE id=?",
          (sid,))
    publish_server(sid)
    return {"ok": True}


@app.post("/api/servers/{sid}/test")
async def test_server(sid: int, user: str = Depends(user_required)):
    row = get_server(sid)
    if not row:
        raise HTTPException(404, "Server not found")
    steps = []
    try:
        t = Target.from_row(row)
        conn, new_key = await connect(t)
    except SSHOpError as exc:
        return {"ok": False, "steps": [{"ok": False, "msg": str(exc)}]}
    try:
        fp = fingerprint(new_key) if new_key else row["host_key_fp"]
        if new_key:
            db.ex("UPDATE servers SET host_key=?, host_key_fp=? WHERE id=?", (new_key, fp, sid))
            steps.append({"ok": True, "msg": f"Connected. Trusted host key {fp} (first connection)."})
        else:
            steps.append({"ok": True, "msg": f"Connected. Host key matches {fp}."})
        code, out = await run(conn, t, ". /etc/os-release; echo \"$PRETTY_NAME\"; command -v apt-get", timeout=15)
        lines = out.strip().splitlines()
        os_name = lines[0] if lines else "unknown"
        has_apt = any(l.endswith("apt-get") for l in lines)
        steps.append({"ok": has_apt, "msg": f"{os_name}" + ("" if has_apt else " (apt-get not found)")})
        ok, msg = await test_sudo(conn, t)
        steps.append({"ok": ok, "msg": msg})
        db.ex("UPDATE servers SET os_name=? WHERE id=?", (os_name, sid))
    except SSHOpError as exc:
        steps.append({"ok": False, "msg": str(exc)})
    finally:
        conn.close()
    publish_server(sid)
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


class UpdateOptions(BaseModel):
    reboot: str = Field("never", pattern="^(never|if_required|always)$")
    autoremove: bool = True


@app.post("/api/servers/{sid}/check")
async def check_server(sid: int, user: str = Depends(user_required)):
    return _start(sid, "check")


@app.post("/api/servers/{sid}/update")
async def update_server_packages(sid: int, opts: UpdateOptions, user: str = Depends(user_required)):
    return _start(sid, "update", opts.model_dump())


def _start(sid: int, kind: str, options: dict | None = None):
    try:
        return manager.start(sid, kind, "manual", options).public()
    except KeyError:
        raise HTTPException(404, "Server not found")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


class DockerUpdate(BaseModel):
    container_ids: list[str] = Field(min_length=1, max_length=200)
    remove_old_images: bool = True


class DockerPrune(BaseModel):
    images: bool = True
    build_cache: bool = True
    containers: bool = False
    networks: bool = True


def _docker_ready(sid: int):
    row = get_server(sid)
    if not row:
        raise HTTPException(404, "Server not found")
    if row["docker_status"] != "ok":
        raise HTTPException(409, "Docker isn't available on this server. Run a check first.")
    return row


@app.post("/api/servers/{sid}/docker/update")
async def docker_update(sid: int, body: DockerUpdate, user: str = Depends(user_required)):
    row = _docker_ready(sid)
    known = {c["id"]: c for c in (json.loads(row["docker_json"]) or {}).get("containers", [])}
    blocked = [known[i]["name"] for i in body.container_ids if i in known and known[i]["check"] in ("self", "excluded")]
    if blocked:
        raise HTTPException(400, f"Patchrack doesn't update these containers: {', '.join(blocked)}")
    if any(not re.fullmatch(r"[0-9a-f]{12,64}", i) for i in body.container_ids):
        raise HTTPException(400, "Invalid container id")
    return _start(sid, "docker_update", body.model_dump())


@app.post("/api/servers/{sid}/docker/prune")
async def docker_prune(sid: int, body: DockerPrune, user: str = Depends(user_required)):
    _docker_ready(sid)
    if not any(body.model_dump().values()):
        raise HTTPException(400, "Choose at least one thing to clean up")
    return _start(sid, "docker_prune", body.model_dump())


# ---- groups --------------------------------------------------------------------------
class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class ServerGroupIn(BaseModel):
    group_id: int | None = None


def groups_public() -> list[dict]:
    rows = db.q("SELECT g.id, g.name, COUNT(s.id) AS servers FROM server_groups g "
                "LEFT JOIN servers s ON s.group_id = g.id GROUP BY g.id ORDER BY g.name COLLATE NOCASE")
    return [dict(r) for r in rows]


def _group_name(body: GroupIn) -> str:
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(400, "Give the group a name")
    return name


@app.get("/api/groups")
def list_groups(user: str = Depends(user_required)):
    return groups_public()


@app.post("/api/groups")
def create_group(body: GroupIn, user: str = Depends(user_required)):
    name = _group_name(body)
    if db.q1("SELECT 1 FROM server_groups WHERE name=?", (name,)):
        raise HTTPException(409, f"A group called {name} already exists")
    gid = db.ex("INSERT INTO server_groups(name, created_at) VALUES(?,?)", (name, time.time()))
    bus.publish("groups", {"groups": groups_public()})
    return {"id": gid, "name": name, "servers": 0}


@app.put("/api/groups/{gid}")
def rename_group(gid: int, body: GroupIn, user: str = Depends(user_required)):
    name = _group_name(body)
    if not db.q1("SELECT 1 FROM server_groups WHERE id=?", (gid,)):
        raise HTTPException(404, "Group not found")
    if db.q1("SELECT 1 FROM server_groups WHERE name=? AND id<>?", (name, gid)):
        raise HTTPException(409, f"A group called {name} already exists")
    db.ex("UPDATE server_groups SET name=? WHERE id=?", (name, gid))
    bus.publish("groups", {"groups": groups_public()})
    return {"ok": True}


@app.delete("/api/groups/{gid}")
def delete_group(gid: int, user: str = Depends(user_required)):
    members = [r["id"] for r in db.q("SELECT id FROM servers WHERE group_id=?", (gid,))]
    db.ex("UPDATE servers SET group_id=NULL WHERE group_id=?", (gid,))
    db.ex("DELETE FROM server_groups WHERE id=?", (gid,))
    bus.publish("groups", {"groups": groups_public()})
    for sid in members:
        publish_server(sid)
    return {"ok": True}


@app.put("/api/servers/{sid}/group")
def set_server_group(sid: int, body: ServerGroupIn, user: str = Depends(user_required)):
    if not get_server(sid):
        raise HTTPException(404, "Server not found")
    if body.group_id is not None and not db.q1("SELECT 1 FROM server_groups WHERE id=?", (body.group_id,)):
        raise HTTPException(400, "That group doesn't exist anymore")
    db.ex("UPDATE servers SET group_id=? WHERE id=?", (body.group_id, sid))
    publish_server(sid)
    bus.publish("groups", {"groups": groups_public()})
    return server_public(get_server(sid))


@app.post("/api/groups/{gid}/check")
async def check_group(gid: int, user: str = Depends(user_required)):
    ids = [r["id"] for r in db.q("SELECT id FROM servers WHERE group_id=?", (gid,))]
    if ids:
        asyncio.create_task(run_scheduled_checks("manual", ids))
    return {"ok": True, "servers": len(ids)}


# ---- release upgrades --------------------------------------------------------------------
class ReleaseUpgradeIn(BaseModel):
    reboot: bool = True
    backup_confirmed: bool = False
    confirm_name: str = ""


@app.post("/api/servers/{sid}/release-upgrade")
async def release_upgrade(sid: int, body: ReleaseUpgradeIn, user: str = Depends(user_required)):
    row = get_server(sid)
    if not row:
        raise HTTPException(404, "Server not found")
    if body.confirm_name.strip() != row["name"]:
        raise HTTPException(400, f"Type the server name, {row['name']}, to confirm")
    if not body.backup_confirmed:
        raise HTTPException(400, "Confirm that you have a backup or snapshot of this server")
    rel = json.loads(row["release_json"]) if row["release_json"] else {}
    if rel.get("unsupported"):
        raise HTTPException(409, rel["unsupported"])
    if (rel.get("last") or {}).get("state") == "running":
        raise HTTPException(409, "A release upgrade is already running on this server")
    if not rel.get("available"):
        raise HTTPException(409, "No new release was found for this server. Run a check first.")
    failed = [c["text"] for c in release.preflight(row, rel) if not c["ok"]]
    if failed:
        raise HTTPException(409, failed[0])
    return _start(sid, "release_upgrade", {"reboot": body.reboot, "target_name": rel.get("target_name")})


@app.post("/api/servers/{sid}/release-upgrade/follow")
async def follow_release_upgrade(sid: int, user: str = Depends(user_required)):
    row = get_server(sid)
    if not row:
        raise HTTPException(404, "Server not found")
    rel = json.loads(row["release_json"]) if row["release_json"] else {}
    if (rel.get("last") or {}).get("state") != "running":
        raise HTTPException(409, "No release upgrade is running on this server. Run a check to refresh.")
    return _start(sid, "release_upgrade", {"attach": True, "reboot": False})   # no reboot nobody asked for


@app.post("/api/check-all")
async def check_all(user: str = Depends(user_required)):
    asyncio.create_task(run_scheduled_checks("manual"))
    return {"ok": True}


class BulkUpdate(UpdateOptions):
    server_ids: list[int]


@app.post("/api/update-many")
async def update_many(body: BulkUpdate, user: str = Depends(user_required)):
    started, skipped = [], []
    for sid in body.server_ids:
        try:
            started.append(manager.start(sid, "update", "manual",
                                         {"reboot": body.reboot, "autoremove": body.autoremove}).public())
        except (KeyError, RuntimeError) as exc:
            skipped.append({"id": sid, "reason": str(exc)})
    return {"started": started, "skipped": skipped}


# ---- jobs & live streams ---------------------------------------------------------
@app.get("/api/jobs")
def jobs_active(user: str = Depends(user_required)):
    return manager.active()


@app.get("/api/jobs/{jid}")
def job_detail(jid: str, user: str = Depends(user_required)):
    job = manager.jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    return {**job.public(), "lines": job.lines}


@app.get("/api/jobs/{jid}/stream")
async def job_stream(jid: str, request: Request, user: str = Depends(user_required)):
    job = manager.jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")

    async def gen():
        idx = 0
        while True:
            while idx < len(job.lines):
                yield f"event: line\ndata: {json.dumps(job.lines[idx])}\n\n"
                idx += 1
            if job.status != "running":
                yield f"event: done\ndata: {json.dumps(job.public())}\n\n"
                return
            if await request.is_disconnected():
                return
            async with job.cond:
                try:
                    await asyncio.wait_for(job.cond.wait(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/events")
async def events(request: Request, user: str = Depends(user_required)):
    q = bus.subscribe()

    async def gen():
        try:
            yield ": hello\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    msg = await asyncio.wait_for(q.get(), 20)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- history -----------------------------------------------------------------------
@app.get("/api/history")
def history(limit: int = 100, server_id: int | None = None, user: str = Depends(user_required)):
    limit = max(1, min(limit, 500))
    if server_id:
        rows = db.q("SELECT id,job_id,server_id,server_name,kind,trigger,result,summary,started_at,finished_at "
                    "FROM history WHERE server_id=? ORDER BY started_at DESC LIMIT ?", (server_id, limit))
    else:
        rows = db.q("SELECT id,job_id,server_id,server_name,kind,trigger,result,summary,started_at,finished_at "
                    "FROM history ORDER BY started_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


@app.get("/api/history/{hid}/log", response_class=PlainTextResponse)
def history_log(hid: int, user: str = Depends(user_required)):
    row = db.q1("SELECT log FROM history WHERE id=?", (hid,))
    if not row:
        raise HTTPException(404, "Not found")
    return row["log"] or ""


@app.delete("/api/history")
def clear_history(user: str = Depends(user_required)):
    db.ex("DELETE FROM history")
    return {"ok": True}


# ---- settings ------------------------------------------------------------------------
class SettingsIn(BaseModel):
    schedule_enabled: bool
    check_interval_hours: float = Field(ge=0.25, le=168)
    max_parallel: int = Field(ge=1, le=20)
    reboot_timeout_min: int = Field(ge=2, le=60)
    notify_updates_found: bool
    notify_update_done: bool
    notify_update_failed: bool
    notify_unreachable: bool
    notify_release_available: bool = True
    notify_container_updates: bool = True
    notify_reminder: str = Field("daily", pattern="^(daily|every|never)$")
    pushover_enabled: bool
    pushover_token: str | None = None   # empty = keep
    pushover_user: str | None = None
    prowl_enabled: bool
    prowl_apikey: str | None = None
    app_url: str = Field("", max_length=300)


PLAIN_KEYS = ["schedule_enabled", "check_interval_hours", "max_parallel", "reboot_timeout_min",
              "notify_updates_found", "notify_update_done", "notify_update_failed", "notify_unreachable", "notify_container_updates", "notify_release_available",
              "notify_reminder",
              "pushover_enabled", "prowl_enabled", "app_url"]


def settings_public() -> dict:
    d = {k: db.get_setting(k) for k in PLAIN_KEYS}
    d["has_pushover_token"] = db.has_setting("pushover_token")
    d["has_pushover_user"] = db.has_setting("pushover_user")
    d["has_prowl_apikey"] = db.has_setting("prowl_apikey")
    last = float(db.get_setting("last_scheduled_check") or 0)
    d["last_scheduled_check"] = last
    d["next_scheduled_check"] = (max(last + float(d["check_interval_hours"] or 6) * 3600, time.time())
                                 if d["schedule_enabled"] else None)
    return d


@app.get("/api/settings")
def get_settings(user: str = Depends(user_required)):
    return settings_public()


@app.put("/api/settings")
def put_settings(body: SettingsIn, user: str = Depends(user_required)):
    if body.app_url and not re.match(r"^https?://", body.app_url):
        raise HTTPException(400, "The app address must start with http:// or https://")
    old_parallel = db.get_setting("max_parallel")
    for k in PLAIN_KEYS:
        db.set_setting(k, getattr(body, k))
    for k in ("pushover_token", "pushover_user", "prowl_apikey"):
        v = getattr(body, k)
        if v:
            db.set_setting(k, v.strip())
    if old_parallel != body.max_parallel:
        manager.reset_parallel()
    return settings_public()


class SecretClear(BaseModel):
    key: str = Field(pattern="^(pushover_token|pushover_user|prowl_apikey)$")


@app.post("/api/settings/clear-secret")
def clear_secret(body: SecretClear, user: str = Depends(user_required)):
    db.ex("DELETE FROM settings WHERE key=?", (body.key,))
    return settings_public()


class TestNotify(BaseModel):
    channel: str = Field(pattern="^(pushover|prowl)$")


@app.post("/api/notify/test")
async def test_notify(body: TestNotify, user: str = Depends(user_required)):
    res = await notify.send("Patchrack test", "Notifications from Patchrack are working.", 0, only=body.channel)
    ok, msg = res.get(body.channel, (False, "Not configured"))
    enabled = bool(db.get_setting(f"{body.channel}_enabled"))
    notify.record("test", "Patchrack test", f"Test through {notify.LABELS[body.channel]}", res)
    return {"ok": ok, "message": msg, "enabled": enabled}


@app.get("/api/notifications")
def notification_log(user: str = Depends(user_required)):
    rows = db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")
    return [{"at": r["at"], "kind": r["kind"], "title": r["title"], "message": r["message"],
             "results": json.loads(r["results"] or "{}"), "note": r["note"]} for r in rows]


# ---- SSH key & server setup script ------------------------------------------------------
@app.get("/api/ssh-key")
def ssh_key(username: str = "patchrack", user: str = Depends(user_required)):
    if not USER_RE.match(username):
        raise HTTPException(400, "Invalid username")
    pub = app_public_key()
    return {"public_key": pub, "fingerprint": asyncssh.import_public_key(pub).get_fingerprint("sha256"),
            "setup_script": setup_script(pub, username)}


@app.post("/api/ssh-key/rotate")
def rotate_key(user: str = Depends(user_required)):
    if manager.active():
        raise HTTPException(409, "Wait for running jobs to finish first")
    key = asyncssh.generate_private_key("ssh-ed25519", comment="patchrack")
    db.set_setting("app_private_key", key.export_private_key().decode())
    return ssh_key("patchrack", user)


def setup_script(pub: str, username: str) -> str:
    script = """bash -s <<'PATCHRACK'
set -eu
U=@USER@
KEY='@KEY@'
[ "$(id -u)" -eq 0 ] || { echo "Run this as root (sudo -i first)"; exit 1; }
command -v sudo >/dev/null || { apt-get update -qq && apt-get install -y -qq sudo; }
id "$U" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$U"
H=$(getent passwd "$U" | cut -d: -f6)
install -d -m 700 -o "$U" -g "$U" "$H/.ssh"
touch "$H/.ssh/authorized_keys"
grep -qF "$KEY" "$H/.ssh/authorized_keys" || echo "$KEY" >> "$H/.ssh/authorized_keys"
chown "$U:$U" "$H/.ssh/authorized_keys"; chmod 600 "$H/.ssh/authorized_keys"
# Release upgrade helper: the only extra command Patchrack may run as root (see its comments)
HELPER=/usr/local/sbin/patchrack-release-upgrade
cat > "$HELPER.new" <<'PRHELPER'
@HELPER_SOURCE@PRHELPER
install -m 755 -o root -g root "$HELPER.new" "$HELPER"; rm -f "$HELPER.new"
APT=$(command -v apt-get); DPKG=$(command -v dpkg); SCTL=$(command -v systemctl)
DOCKER=$(command -v docker || true)
T=$(mktemp)
cat > "$T" <<EOF
# Managed by Patchrack: lets $U run updates, release upgrades, manage Docker and reboot
Defaults:$U env_keep += "DEBIAN_FRONTEND NEEDRESTART_MODE APT_LISTCHANGES_FRONTEND LC_ALL"
$U ALL=(root) NOPASSWD: $APT, $DPKG --configure -a, $SCTL reboot, $HELPER${DOCKER:+, $DOCKER}
EOF
visudo -cf "$T" >/dev/null && install -m 440 -o root -g root "$T" /etc/sudoers.d/patchrack
rm -f "$T"
echo "Done: $U can now be managed by Patchrack${DOCKER:+ (including Docker)}."
PATCHRACK"""
    helper = release.HELPER_SOURCE if release.HELPER_SOURCE.endswith("\n") else release.HELPER_SOURCE + "\n"
    return script.replace("@USER@", username).replace("@KEY@", pub).replace("@HELPER_SOURCE@", helper)


# ---- frontend ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/{path:path}")
def spa(path: str):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
