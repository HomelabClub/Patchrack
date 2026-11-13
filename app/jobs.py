# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
"""Background jobs (check / update), live log streaming, event bus and scheduler."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

from . import db, dockerops, notify, release
from .sshops import Facts, HostKeyChanged, SSHOpError, Target, connect, fingerprint, gather_facts, run, \
    wait_for_reboot, APT_OPTS

log = logging.getLogger("patchrack.jobs")

MAX_LOG_LINES = 5000


# ---- event bus (dashboard live updates) ----------------------------------------
class Bus:
    def __init__(self):
        self.subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)

    def publish(self, kind: str, data: dict) -> None:
        msg = json.dumps({"type": kind, "data": data})
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


bus = Bus()


# ---- serialisation ---------------------------------------------------------------
def server_public(row) -> dict:
    d = {k: row[k] for k in row.keys() if not k.endswith("_enc") and k not in ("packages_json", "host_key")}
    info = json.loads(row["packages_json"]) if row["packages_json"] else {}
    d["packages"] = info.get("pkgs", [])
    d["kept_back"] = info.get("kept_back", [])
    d["reboot_reason"] = info.get("reboot_reason", "")
    d["docker"] = json.loads(row["docker_json"]) if row["docker_json"] else None
    d.pop("docker_json", None)
    rel = json.loads(row["release_json"]) if row["release_json"] else None
    if rel:
        rel["checks"] = release.preflight(row, rel)
        rel["ready"] = bool(rel.get("available") and not rel.get("unsupported")
                            and all(c["ok"] for c in rel["checks"]))
    d["release"] = rel
    d.pop("release_json", None)
    d["has_custom_key"] = bool(row["private_key_enc"])
    d["has_passphrase"] = bool(row["passphrase_enc"])
    d["has_sudo_password"] = bool(row["sudo_password_enc"])
    d["reboot_required"] = bool(row["reboot_required"])
    d["autoremove"] = bool(row["autoremove"])
    d["busy"] = manager.busy_job(row["id"])
    return d


def get_server(sid: int):
    return db.q1("SELECT * FROM servers WHERE id=?", (sid,))


def publish_server(sid: int) -> None:
    row = get_server(sid)
    if row:
        bus.publish("server", server_public(row))


# ---- jobs ----------------------------------------------------------------------
@dataclass
class Job:
    id: str
    server_id: int
    server_name: str
    kind: str
    trigger: str
    options: dict = field(default_factory=dict)
    status: str = "running"         # running | success | failed
    summary: str = ""
    lines: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    phase: str = "Starting"
    result: dict = field(default_factory=dict)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None

    async def log(self, msg: str, level: str = "out") -> None:
        if len(self.lines) >= MAX_LOG_LINES:
            if len(self.lines) == MAX_LOG_LINES:
                self.lines.append({"t": time.time(), "lvl": "info", "msg": "… log truncated"})
            return
        self.lines.append({"t": time.time(), "lvl": level, "msg": msg})
        async with self.cond:
            self.cond.notify_all()

    async def set_phase(self, phase: str) -> None:
        self.phase = phase
        await self.log(phase, "phase")
        bus.publish("job", self.public())

    def public(self) -> dict:
        return {"id": self.id, "server_id": self.server_id, "server_name": self.server_name,
                "kind": self.kind, "trigger": self.trigger, "status": self.status, "phase": self.phase,
                "summary": self.summary, "started_at": self.started_at, "finished_at": self.finished_at,
                "options": self.options, "result": self.result}


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.by_server: dict[int, str] = {}
        self._sem: asyncio.Semaphore | None = None

    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(max(1, int(db.get_setting("max_parallel") or 3)))
        return self._sem

    def reset_parallel(self) -> None:
        self._sem = None

    def busy_job(self, sid: int) -> str | None:
        jid = self.by_server.get(sid)
        if jid and self.jobs.get(jid) and self.jobs[jid].status == "running":
            return jid
        return None

    def active(self) -> list[dict]:
        return [j.public() for j in self.jobs.values() if j.status == "running"]

    def _prune(self) -> None:
        done = sorted((j for j in self.jobs.values() if j.status != "running"), key=lambda j: j.started_at)
        for j in done[:-50]:
            self.jobs.pop(j.id, None)

    def start(self, sid: int, kind: str, trigger: str = "manual", options: dict | None = None) -> Job:
        row = get_server(sid)
        if row is None:
            raise KeyError(sid)
        if self.busy_job(sid):
            raise RuntimeError(f"{row['name']} is already busy")
        job = Job(id=secrets.token_hex(8), server_id=sid, server_name=row["name"], kind=kind,
                  trigger=trigger, options=options or {})
        self.jobs[job.id] = job
        self.by_server[sid] = job.id
        self._prune()
        runner = {"check": self._check, "update": self._update, "docker_update": self._docker_update,
                  "docker_prune": self._docker_prune, "release_upgrade": self._release_upgrade}[kind]
        job.task = asyncio.create_task(self._wrap(job, runner))
        bus.publish("job", job.public())
        publish_server(sid)
        return job

    async def _wrap(self, job: Job, runner) -> None:
        async with self.sem():
            try:
                await runner(job)
                job.status = "success"
            except HostKeyChanged as exc:
                job.status, job.summary = "failed", str(exc)
                await job.log(str(exc), "error")
                self._mark_error(job.server_id, "error", str(exc))
            except SSHOpError as exc:
                job.status, job.summary = "failed", str(exc)
                await job.log(str(exc), "error")
                unreachable = str(exc).startswith("Could not reach")
                if unreachable or not job.kind.startswith("docker"):
                    self._mark_error(job.server_id, "unreachable" if unreachable else "error", str(exc))
            except asyncio.CancelledError:
                job.status, job.summary = "failed", "Cancelled"
                await job.log("Cancelled", "error")
            except Exception as exc:  # unexpected - keep the app alive and record it
                log.exception("job %s crashed", job.id)
                job.status, job.summary = "failed", f"Unexpected error: {exc}"
                await job.log(job.summary, "error")
                self._mark_error(job.server_id, "error", job.summary)
        job.finished_at = time.time()
        job.phase = "Finished" if job.status == "success" else "Failed"
        async with job.cond:
            job.cond.notify_all()
        db.ex("INSERT INTO history(job_id,server_id,server_name,kind,trigger,result,summary,log,started_at,"
              "finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
              (job.id, job.server_id, job.server_name, job.kind, job.trigger, job.status, job.summary,
               "\n".join(f"[{l['lvl']}] {l['msg']}" for l in job.lines)[-400_000:],
               job.started_at, job.finished_at))
        bus.publish("job", job.public())
        publish_server(job.server_id)
        if job.kind == "update":
            if job.status == "success":
                await notify.event("update_done", f"{job.server_name} updated", job.summary)
            else:
                await notify.event("update_failed", f"{job.server_name}: update failed", job.summary, 1)
        elif job.kind == "release_upgrade":
            if job.status == "success":
                await notify.event("update_done", f"{job.server_name}: release upgrade finished", job.summary)
            else:
                await notify.event("update_failed", f"{job.server_name}: release upgrade failed", job.summary, 1)
        elif job.kind == "docker_update":
            if job.status == "success":
                await notify.event("update_done", f"{job.server_name}: containers updated", job.summary)
            else:
                await notify.event("update_failed", f"{job.server_name}: container update failed",
                                   job.summary, 1)

    # ---- state ----
    def _mark_error(self, sid: int, status: str, msg: str) -> None:
        db.ex("UPDATE servers SET status=?, last_error=?, last_check_at=? WHERE id=?",
              (status, msg, time.time(), sid))

    def _store_facts(self, sid: int, f: Facts, updated: bool = False) -> None:
        if f.packages:
            status = "updates"
        elif f.reboot_required:
            status = "reboot"
        else:
            status = "up_to_date"
        db.ex("UPDATE servers SET status=?, pending_count=?, security_count=?, kept_back_count=?, "
              "reboot_required=?, packages_json=?, os_name=?, kernel=?, uptime_s=?, last_error=NULL, "
              "last_check_at=?" + (", last_update_at=?" if updated else "") + " WHERE id=?",
              (status, len(f.packages), f.security_count, len(f.kept_back), int(f.reboot_required),
               json.dumps({"pkgs": f.packages, "kept_back": f.kept_back, "reboot_reason": f.reboot_reason}),
               f.os_name, f.kernel, f.uptime_s, time.time(),
               *((time.time(),) if updated else ()), sid))

    async def _connect(self, job: Job, t: Target):
        await job.set_phase(f"Connecting to {t.username}@{t.host}:{t.port}")
        conn, new_key = await connect(t)
        if new_key:
            fp = fingerprint(new_key)
            db.ex("UPDATE servers SET host_key=?, host_key_fp=? WHERE id=?", (new_key, fp, t.id))
            await job.log(f"First connection: trusted host key {fp}", "info")
        return conn

    # ---- check ----
    async def _check(self, job: Job) -> None:
        row = get_server(job.server_id)
        t = Target.from_row(row)
        conn = await self._connect(job, t)
        apt_error = None
        try:
            await job.set_phase("Refreshing package lists")
            try:
                facts = await gather_facts(conn, t, on_line=job.log)
            except HostKeyChanged:
                raise
            except SSHOpError as exc:
                apt_error = exc
            docker = await self._check_docker(job, conn, t)
            rel = None
            if not apt_error:
                await job.set_phase("Looking for a new release")
                rel = await release.detect(conn, t, facts)
                self._store_release(t.id, rel)
        finally:
            conn.close()
        if apt_error:
            raise apt_error
        self._store_facts(t.id, facts)
        job.result = {"pending": len(facts.packages), "security": facts.security_count,
                      "reboot_required": facts.reboot_required,
                      "container_updates": (docker or {}).get("updates", 0)}
        job.summary = describe(facts)
        if docker and docker.get("updates"):
            u = docker["updates"]
            job.summary = job.summary.rstrip(".") + f"; {u} container update{'' if u == 1 else 's'}."
        if rel and rel.get("available"):
            job.summary = job.summary.rstrip(".") + f"; {rel['target_name']} is available."
        await job.log(job.summary, "result")

    def _store_release(self, sid: int, info: dict) -> None:
        db.ex("UPDATE servers SET release_json=? WHERE id=?", (json.dumps(info), sid))

    # ---- release upgrade ----
    async def _release_upgrade(self, job: Job) -> None:
        opts = job.options
        row = get_server(job.server_id)
        t = Target.from_row(row)
        attach = bool(opts.get("attach"))
        before = json.loads(row["release_json"]) if row["release_json"] else {}
        target_name = before.get("target_name") or "the new release"
        conn = await self._connect(job, t)
        try:
            if not attach:
                await job.set_phase("Checking the requirements again")
                facts = await gather_facts(conn, t, on_line=job.log)
                self._store_facts(t.id, facts)
                info = await release.detect(conn, t, facts)
                self._store_release(t.id, info)
                problems = [c["text"] for c in release.preflight(get_server(t.id), info) if not c["ok"]]
                if info.get("unsupported"):
                    problems.insert(0, info["unsupported"])
                elif not info.get("available"):
                    problems.insert(0, "No new release is available for this server anymore.")
                if problems:
                    raise SSHOpError("The upgrade didn't start. " + " ".join(problems))
                target_name = info["target_name"]
                await job.set_phase(f"Starting the upgrade to {target_name}")
                code, out = await run(conn, t, f"{release.HELPER_PATH} start {info['family']} "
                                               f"{info.get('target') or '-'}", root=True, timeout=60)
                if code != 0 or "started" not in out:
                    raise SSHOpError(f"The upgrade helper didn't start the upgrade: {out.strip()[-300:]}")
        finally:
            conn.close()

        await job.set_phase(f"Upgrading to {target_name}. This usually takes 30 to 90 minutes; "
                            "you can close this panel")
        state, switched_off = await self._follow_upgrade(job, t)
        if state.get("state") != "success":
            row = get_server(t.id)
            if row["release_json"]:   # show the failure in the server's details right away
                info = json.loads(row["release_json"])
                info["last"] = state
                self._store_release(t.id, info)
                publish_server(t.id)
            raise SSHOpError(f"The release upgrade failed (exit code {state.get('exit', '?')}). The log shows "
                             "where. The server may be partly upgraded: check it over SSH before installing "
                             "other updates.")
        from_name, to_name = state.get("from", "the old release"), state.get("to", target_name)
        conn = await self._connect(job, t)
        try:
            facts = await gather_facts(conn, t, refresh=False)
            reboot = opts.get("reboot", True)
            if reboot:
                await job.set_phase("Rebooting into the new release")
                try:
                    await run(conn, t, "systemctl reboot", root=True, timeout=20)
                except Exception:
                    pass
        finally:
            conn.close()
        if reboot:
            timeout_s = max(int(db.get_setting("reboot_timeout_min") or 10), 15) * 60
            await job.set_phase(f"Waiting for {t.name} to come back")
            await wait_for_reboot(t, facts.boot_id, timeout_s, job.log)
        await job.set_phase("Checking the new release")
        conn = await self._connect(job, t)
        try:
            facts = await gather_facts(conn, t, on_line=job.log)
            info = await release.detect(conn, t, facts)
        finally:
            conn.close()
        self._store_facts(t.id, facts, updated=True)
        self._store_release(t.id, info)
        parts = [f"Upgraded from {from_name} to {to_name}"]
        parts.append("rebooted" if reboot else "reboot it soon to finish")
        if facts.packages:
            parts.append(f"{len(facts.packages)} more update{'' if len(facts.packages) == 1 else 's'} available")
        job.summary = ", ".join(parts) + "."
        if switched_off:
            job.summary += (f" These package sources were switched off during the upgrade: {switched_off}. "
                            "Turn them back on once they support the new release.")
        job.result = {"from": from_name, "to": to_name, "rebooted": reboot, "switched_off": switched_off}
        await job.log(job.summary, "result")

    async def _follow_upgrade(self, job: Job, t: Target) -> tuple[dict, str]:
        """Stream the helper's log until the upgrade ends. Reconnects when SSH drops."""
        offset, state, switched_off = 0, {}, ""
        deadline = time.time() + 4 * 3600
        conn, started, lost_since = None, time.time(), None
        cmd = ('L=/var/log/patchrack-release-upgrade.log; S=$(stat -c %s "$L" 2>/dev/null || echo 0); '
               'if [ "$S" -gt {off} ]; then tail -c +$(({off}+1)) "$L" | head -c $(($S-{off})); echo; fi; '
               'echo "@@size=$S"; ' + release.HELPER_PATH + ' status | sed "s/^/@@/"')
        try:
            while time.time() < deadline:
                try:
                    if conn is None:
                        conn, _ = await connect(t, timeout=15)
                        if lost_since:
                            await job.log("Connection restored.", "info")
                        lost_since = None
                    code, out = await run(conn, t, cmd.format(off=offset), timeout=120)
                except HostKeyChanged:
                    raise
                except (SSHOpError, OSError, Exception) as exc:  # the connection can drop while sshd upgrades
                    if conn is not None:
                        conn.close()
                    conn = None
                    if lost_since is None:
                        lost_since = time.time()
                        await job.log(f"Lost the connection ({exc.__class__.__name__}); the upgrade continues "
                                      "on the server. Reconnecting…", "info")
                    elif time.time() - lost_since > 30 * 60:
                        raise SSHOpError("Lost contact with the server for 30 minutes during the upgrade. "
                                         "Check it over SSH or its console.")
                    await asyncio.sleep(10)
                    continue
                meta = {}
                for line in out.splitlines():
                    if line.startswith("@@"):
                        k, _, v = line[2:].partition("=")
                        meta[k] = v
                    elif line.strip():
                        if line.startswith("SWITCHED_OFF_SOURCES="):
                            switched_off = line.split("=", 1)[1].strip()
                        await job.log(line, "phase" if line.startswith("== ") else "out")
                try:
                    offset = int(meta.get("size", offset))
                except ValueError:
                    pass
                state = {k: v for k, v in meta.items() if k != "size"}
                if state.get("state") in ("success", "failed"):
                    return state, switched_off
                if state.get("stale") == "1" and time.time() - started > 60:
                    raise SSHOpError("The upgrade process on the server stopped without finishing. "
                                     "Check /var/log/patchrack-release-upgrade.log on the server.")
                await asyncio.sleep(5)
            raise SSHOpError("The upgrade is still running after 4 hours. Patchrack stopped following it; "
                             "check the server directly.")
        finally:
            if conn is not None:
                conn.close()

    async def _check_docker(self, job: Job, conn, t: Target) -> dict | None:
        """Look for Docker and container image updates. Never raises: Docker is optional."""
        try:
            if not await dockerops.docker_installed(conn, t):
                self._store_docker(t.id, "none", None)
                return None
            await job.set_phase("Checking Docker containers for image updates")
            info = await dockerops.inspect(conn, t, on_line=job.log)
            self._store_docker(t.id, "ok", info)
            return info
        except dockerops.DockerStopped as exc:
            await job.log(str(exc), "warn")
            self._store_docker(t.id, "stopped", None, str(exc))
        except dockerops.DockerNoAccess as exc:
            await job.log(str(exc), "warn")
            self._store_docker(t.id, "no_access", None, str(exc))
        except (SSHOpError, Exception) as exc:  # Docker trouble must not hide the apt result
            await job.log(f"Docker check failed: {exc}", "warn")
            self._store_docker(t.id, "error", None, str(exc))
        return None

    def _store_docker(self, sid: int, status: str, info: dict | None, error: str | None = None) -> None:
        if info is None and status in ("no_access", "error", "stopped"):
            db.ex("UPDATE servers SET docker_status=?, docker_error=? WHERE id=?", (status, error, sid))
            return
        db.ex("UPDATE servers SET docker_status=?, docker_json=?, docker_updates=?, docker_error=? WHERE id=?",
              (status, json.dumps(info) if info else None, (info or {}).get("updates", 0), error, sid))

    # ---- docker: update containers ----
    async def _docker_update(self, job: Job) -> None:
        ids = job.options.get("container_ids") or []
        row = get_server(job.server_id)
        t = Target.from_row(row)
        conn = await self._connect(job, t)
        try:
            res = await dockerops.update_containers(conn, t, ids, bool(job.options.get("remove_old_images")),
                                                    job.log, job.set_phase)
            await job.set_phase("Checking containers again")
            info = await dockerops.inspect(conn, t, on_line=job.log)
            self._store_docker(t.id, "ok", info)
        finally:
            conn.close()
        def n(count, word):
            return f"{count} {word}{'' if count == 1 else 's'}"

        def names(items):
            items = list(items)
            return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

        parts = []
        if res["updated"]:
            parts.append(f"Updated {names(res['updated'])}")
        if res["unchanged"]:
            parts.append(f"{names(res['unchanged'])} already ran the newest image")
        if res["failed"]:
            parts.append(f"Failed: {names(n for n, _ in res['failed'])}")
        if not res["failed"]:
            parts.append("All containers are up to date" if not info["updates"]
                         else f"{n(info['updates'], 'more container update')} available")
        job.summary = ". ".join(parts) + "."
        job.result = {"updated": res["updated"], "failed": [n for n, _ in res["failed"]],
                      "remaining": info["updates"], "images_removed": res["images_removed"]}
        await job.log(job.summary, "result" if not res["failed"] else "error")
        if res["failed"]:
            raise SSHOpError(job.summary)

    # ---- docker: prune ----
    async def _docker_prune(self, job: Job) -> None:
        row = get_server(job.server_id)
        t = Target.from_row(row)
        info = json.loads(row["docker_json"]) if row["docker_json"] else {}
        root = info.get("root") or "/var/lib/docker"
        conn = await self._connect(job, t)
        try:
            res = await dockerops.prune(conn, t, job.options, root, job.log, job.set_phase)
            await job.set_phase("Measuring disk usage")
            info = await dockerops.inspect(conn, t)
            self._store_docker(t.id, "ok", info)
        finally:
            conn.close()
        hb = dockerops.human_bytes
        freed = max(res["reclaimed"], res["gained"] or 0)
        s = f"Freed {hb(freed)}"
        if res["free_before"] is not None and res["free_after"] is not None:
            s += f". Free disk space went from {hb(res['free_before'])} to {hb(res['free_after'])}"
        job.summary = s + "."
        job.result = {**res, "freed": freed}
        await job.log(job.summary, "result")

    # ---- update ----
    async def _update(self, job: Job) -> None:
        opts = job.options
        reboot = opts.get("reboot", "never")          # never | if_required | always
        autoremove = bool(opts.get("autoremove", True))
        row = get_server(job.server_id)
        t = Target.from_row(row)
        timeout_min = int(db.get_setting("reboot_timeout_min") or 10)
        conn = await self._connect(job, t)
        try:
            await job.set_phase("Refreshing package lists")
            before = await gather_facts(conn, t, on_line=job.log)
            await job.log(f"{len(before.packages)} package(s) to install"
                          + (f", {before.security_count} security" if before.security_count else ""), "info")

            mode = "dist-upgrade" if t.upgrade_mode == "full" else "upgrade"
            if before.packages:
                await job.set_phase(f"Installing updates (apt-get {mode})")
                code, out = await run(conn, t, f"apt-get {APT_OPTS} -y {mode}", root=True,
                                      on_line=job.log, timeout=3600)
                if code != 0 and "dpkg was interrupted" in out:
                    await job.set_phase("Repairing an interrupted dpkg run")
                    await run(conn, t, "dpkg --configure -a", root=True, on_line=job.log, timeout=1800)
                    code, out = await run(conn, t, f"apt-get {APT_OPTS} -y {mode}", root=True,
                                          on_line=job.log, timeout=3600)
                if code != 0:
                    raise SSHOpError(f"apt-get {mode} failed (exit {code}). Check the log for the cause.")
            else:
                await job.log("No packages to install.", "info")

            if autoremove:
                await job.set_phase("Removing packages that are no longer needed")
                code, _ = await run(conn, t, f"apt-get {APT_OPTS} -y autoremove", root=True,
                                    on_line=job.log, timeout=900)
                if code != 0:
                    await job.log("autoremove failed; continuing", "warn")

            await job.set_phase("Checking the result")
            after = await gather_facts(conn, t, refresh=False)
            need_reboot = after.reboot_required
            do_reboot = reboot == "always" or (reboot == "if_required" and need_reboot)
            if do_reboot:
                await job.set_phase("Rebooting" + (f" ({after.reboot_reason})" if after.reboot_reason else ""))
                try:
                    await run(conn, t, "systemctl reboot", root=True, timeout=20)
                except (SSHOpError, Exception):
                    pass  # the connection drops when the server goes down
        finally:
            conn.close()

        if do_reboot:
            await job.set_phase(f"Waiting for {t.name} to come back (up to {timeout_min} min)")
            after = await wait_for_reboot(t, after.boot_id, timeout_min * 60, job.log)

        self._store_facts(t.id, after, updated=True)
        installed = len(before.packages)
        parts = [f"Installed {installed} update(s)" if installed else "No updates were needed"]
        if do_reboot:
            parts.append("rebooted")
        if after.packages:
            parts.append(f"{len(after.packages)} still pending")
        elif after.reboot_required:
            parts.append("a reboot is still required" if not do_reboot else "reboot still flagged")
        else:
            parts.append("system is up to date")
        job.summary = ", ".join(parts) + "."
        job.result = {"installed": installed, "rebooted": do_reboot, "pending": len(after.packages),
                      "reboot_required": after.reboot_required}
        await job.log(job.summary, "result")


def describe(f: Facts) -> str:
    if f.packages:
        s = f"{len(f.packages)} update(s) available"
        if f.security_count:
            s += f", {f.security_count} security"
        if f.reboot_required:
            s += "; reboot required"
        return s + "."
    if f.reboot_required:
        return "Up to date, but a reboot is required."
    return "Up to date."


manager = JobManager()


# ---- scheduler ------------------------------------------------------------------
# "Once a day" reminders: at most one per 20 hours, so a check that runs a little earlier
# than yesterday's still counts as the next day
REMINDER_GAP = 20 * 3600


async def run_scheduled_checks(trigger: str = "schedule", server_ids: list[int] | None = None) -> None:
    rows = db.q("SELECT id, status, pending_count, docker_json, release_json FROM servers ORDER BY name")
    if server_ids is not None:
        rows = [r for r in rows if r["id"] in server_ids]
    before = {r["id"]: (r["status"], r["pending_count"]) for r in rows}
    release_before = {r["id"]: _release_target(r["release_json"]) for r in rows}
    containers_before = {r["id"]: _container_updates(r["docker_json"]) for r in rows}
    jobs = []
    for r in rows:
        try:
            jobs.append(manager.start(r["id"], "check", trigger))
        except RuntimeError:
            continue
    if not jobs:
        return
    await asyncio.gather(*(j.task for j in jobs if j.task), return_exceptions=True)
    if trigger != "schedule":
        return
    checked = {j.server_id for j in jobs}
    sys_lines, ct_lines, unreachable, new_releases = [], [], [], []
    sys_new = ct_new = False
    # list everything that is waiting, on every server, and note whether any of it is new
    for row in db.q("SELECT * FROM servers ORDER BY name COLLATE NOCASE"):
        prev_status, prev_count = before.get(row["id"], ("unknown", 0))
        if row["status"] == "updates":
            sec = f", {row['security_count']} security" if row["security_count"] else ""
            n = row["pending_count"]
            sys_lines.append(f"{row['name']}: {n} update{'' if n == 1 else 's'}{sec}")
            if row["id"] in checked and (prev_status != "updates" or n > prev_count):
                sys_new = True
        if row["id"] in checked and row["status"] == "unreachable" and prev_status != "unreachable":
            unreachable.append(row["name"])
        target = _release_target(row["release_json"])
        if row["id"] in checked and target and target != release_before.get(row["id"]):
            new_releases.append(f"{row['name']}: {target}")
        now_c = _container_updates(row["docker_json"])
        if now_c:
            ct_lines.append(f"{row['name']}: {', '.join(sorted(now_c))}")
            if row["id"] in checked and now_c - containers_before.get(row["id"], set()):
                ct_new = True
    await _announce("updates_found", sys_lines, sys_new, "Updates available", "Updates still waiting")
    await _announce("container_updates", ct_lines, ct_new, "Container updates available",
                    "Container updates still waiting")
    if unreachable:
        await notify.event("unreachable", "Servers unreachable", ", ".join(unreachable), 1)
    if new_releases:
        await notify.event("release_available", "New release available", "\n".join(new_releases))


def _release_target(release_json: str | None) -> str | None:
    if not release_json:
        return None
    info = json.loads(release_json)
    return info.get("target_name") if info.get("available") and not info.get("unsupported") else None


async def _announce(kind: str, lines: list[str], is_new: bool, title_new: str, title_waiting: str) -> None:
    """Report waiting updates: right away when something is new, otherwise as a reminder."""
    if not lines:
        return
    mode = db.get_setting("notify_reminder") or "daily"
    last = float(db.get_setting(f"last_notified_{kind}") or 0)
    if is_new:
        title = title_new
    elif mode == "every" or (mode == "daily" and time.time() - last >= REMINDER_GAP):
        title = title_waiting
    else:
        return
    await notify.event(kind, title, "\n".join(lines))
    db.set_setting(f"last_notified_{kind}", time.time())


def _container_updates(docker_json: str | None) -> set[str]:
    if not docker_json:
        return set()
    info = json.loads(docker_json)
    return {c["name"] for c in info.get("containers", []) if c.get("check") == "update"}


_scheduled: set[asyncio.Task] = set()


def _scheduled_done(task: asyncio.Task) -> None:
    _scheduled.discard(task)
    if not task.cancelled() and task.exception():
        log.error("Scheduled check failed", exc_info=task.exception())


async def scheduler_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            if db.get_setting("schedule_enabled"):
                hours = float(db.get_setting("check_interval_hours") or 6)
                last = float(db.get_setting("last_scheduled_check") or 0)
                if time.time() - last >= hours * 3600:
                    db.set_setting("last_scheduled_check", time.time())
                    log.info("Running scheduled update check")
                    task = asyncio.create_task(run_scheduled_checks("schedule"))
                    _scheduled.add(task)            # keep a reference until it finishes
                    task.add_done_callback(_scheduled_done)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(60)
