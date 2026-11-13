// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Kevin Villarreal (Homelab Club)
/* Patchrack frontend — no build step, no framework. */
"use strict";

const S = {
  session: null,
  servers: new Map(),
  jobs: new Map(),          // running/recent jobs by id
  settings: null,
  expanded: new Set(),
  dockerSel: new Map(),     // server id -> selected container ids
  groups: [],               // [{id, name, servers}]
  collapsed: loadCollapsed(),
  events: null,
  sheetCleanup: null,
};

/* ---------- helpers ---------- */
function loadCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem("patchrack.collapsed") || "[]")); } catch { return new Set(); }
}
function saveCollapsed() {
  try { localStorage.setItem("patchrack.collapsed", JSON.stringify([...S.collapsed])); } catch { /* private mode */ }
}
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + "s")}`;

async function api(method, path, body) {
  const opts = { method, headers: { "x-patchrack": "1" }, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["content-type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 401 && !path.startsWith("/api/login")) { S.session = { setup_required: false, user: null }; render(); throw new Error("Sign in to continue"); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = data && data.detail ? data.detail : `Request failed (${res.status})`;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 50) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
function fmtBytes(n) {
  n = Number(n || 0);
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < u.length - 1) { n /= 1000; i++; }
  return i < 2 ? `${Math.round(n)} ${u[i]}` : `${n.toFixed(1)} ${u[i]}`;
}
const JOB_VERB = { check: "Checking", update: "Updating", docker_update: "Updating containers on", docker_prune: "Cleaning up Docker on", release_upgrade: "Upgrading the release of" };
const JOB_NOUN = { check: "Check", update: "Update", docker_update: "Container update", docker_prune: "Docker clean-up", release_upgrade: "Release upgrade" };
function shortRelease(name) { return String(name || "").replace(/^(Ubuntu|Debian) /, ""); }
function groupName(id) { const g = S.groups.find((x) => x.id === id); return g ? g.name : ""; }
function containerUpdates(s) { return s.docker_status === "ok" && s.docker ? s.docker.updates || 0 : 0; }

function uptime(s) {
  if (!s) return "";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return d ? `${d} d ${h} h` : `${h} h ${Math.floor((s % 3600) / 60)} min`;
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<span class="led ${kind}"></span><span>${esc(msg)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "ok" ? 4200 : 8000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs HTTPS or localhost; fall back for plain-HTTP LAN access
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.append(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("Copied to clipboard");
}

const ICON = {
  plus: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M8 3v10M3 8h10"/></svg>',
  refresh: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2.5v3h-3"/></svg>',
  down: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M8 2.5v8M4.5 7 8 10.5 11.5 7M3 13.5h10"/></svg>',
  copy: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/></svg>',
  gear: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7 3.6 3.6"/></svg>',
};
const BRAND = `<img class="brand-mark" src="/static/logo-mark.png" alt="" width="51" height="30">`;

/* ---------- server status model ---------- */
function statusOf(s) {
  const job = s.busy && S.jobs.get(s.busy);
  if (s.busy) {
    const kind = job ? job.kind : "check";
    const label = { check: "Checking…", update: "Updating…", docker_update: "Updating containers…", docker_prune: "Cleaning up Docker…", release_upgrade: "Upgrading release…" }[kind] || "Working…";
    return { led: "busy", cls: "busy", label, sub: job ? job.phase : "" };
  }
  const cu = containerUpdates(s);
  const cuText = cu ? plural(cu, "container update") : "";
  switch (s.status) {
    case "updates": {
      return { led: "warn", cls: "warn", label: plural(s.pending_count, "update"),
        sub: [s.security_count ? `${s.security_count} security` : "", s.reboot_required ? "reboot required" : "", cuText].filter(Boolean).join(", ") || "ready to install" };
    }
    case "reboot": return { led: "warn", cls: "warn", label: "Reboot required", sub: cuText || "packages are current" };
    case "up_to_date":
      if (cu) return { led: "warn", cls: "warn", label: cuText, sub: "system packages are current" };
      return { led: "ok", cls: "ok", label: "Up to date", sub: s.kept_back_count ? `${s.kept_back_count} held back` : "" };
    case "unreachable": return { led: "bad", cls: "bad", label: "Unreachable", sub: "check network or SSH" };
    case "error": return { led: "bad", cls: "bad", label: "Needs attention", sub: "open for details" };
    default: return { led: "", cls: "", label: "Not checked yet", sub: "run a check" };
  }
}

/* ---------- routing ---------- */
function view() {
  const v = location.hash.replace(/^#\/?/, "") || "servers";
  return ["servers", "history", "settings"].includes(v) ? v : "servers";
}
window.addEventListener("hashchange", () => render());

/* ---------- boot ---------- */
async function boot() {
  try { S.session = await api("GET", "/api/session"); }
  catch (e) { $("#app").innerHTML = `<div class="auth"><p class="error-text">Could not reach Patchrack: ${esc(e.message)}</p></div>`; return; }
  if (S.session.user) await loadData();
  render();
}

async function loadData() {
  const [servers, jobs, groups] = await Promise.all([api("GET", "/api/servers"), api("GET", "/api/jobs"), api("GET", "/api/groups")]);
  S.servers = new Map(servers.map((s) => [s.id, s]));
  S.groups = groups;
  for (const j of jobs) S.jobs.set(j.id, j);
  connectEvents();
}

function connectEvents() {
  if (S.events) S.events.close();
  const es = new EventSource("/api/events");
  S.events = es;
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "server") { S.servers.set(msg.data.id, msg.data); refreshRack(msg.data.id); }
    else if (msg.type === "server_deleted") { S.servers.delete(msg.data.id); refreshRack(); }
    else if (msg.type === "groups") {
      S.groups = msg.data.groups;
      if (view() === "servers") renderServers();
      document.dispatchEvent(new CustomEvent("pd:groups"));
    }
    else if (msg.type === "job") {
      const prev = S.jobs.get(msg.data.id);
      S.jobs.set(msg.data.id, msg.data);
      const s = S.servers.get(msg.data.server_id);
      if (s) refreshRack(s.id);
      if (msg.data.status !== "running" && prev && prev.status === "running") onJobDone(msg.data);
      document.dispatchEvent(new CustomEvent("pd:job", { detail: msg.data }));
    }
  };
  es.onerror = () => { /* EventSource retries automatically */ };
}

function onJobDone(job) {
  if (job.kind !== "check") toast(`${job.server_name}: ${job.summary}`, job.status === "success" ? "ok" : "bad");
  else if (job.status !== "success" && job.trigger === "manual") toast(`${job.server_name}: ${job.summary}`, "bad");
}

/* ---------- top-level render ---------- */
function render() {
  const app = $("#app");
  if (!S.session) return;
  if (!S.session.user) { app.innerHTML = authView(S.session.setup_required); bindAuth(); return; }
  const v = view();
  app.innerHTML = `
    <header class="topbar">
      <a class="brand" href="#/servers" aria-label="Homelab Club Patchrack">${BRAND}<span class="brand-text">Patchrack<small>by Homelab Club</small></span></a>
      <nav class="nav" aria-label="Main">
        <a href="#/servers" ${v === "servers" ? 'aria-current="page"' : ""}>Servers</a>
        <a href="#/history" ${v === "history" ? 'aria-current="page"' : ""}>History</a>
        <a href="#/settings" ${v === "settings" ? 'aria-current="page"' : ""}>Settings</a>
      </nav>
      <div class="spacer"></div>
      <button class="btn ghost small" data-action="logout">Sign out</button>
    </header>
    <main id="main"></main>`;
  $('[data-action="logout"]').onclick = async () => { await api("POST", "/api/logout"); S.events?.close(); S.session.user = null; render(); };
  if (v === "servers") renderServers();
  else if (v === "history") renderHistory();
  else renderSettings();
}

function aboutLine() {
  const src = S.session.source_url;
  return `Homelab Club Patchrack ${esc(S.session.version)}. Free software under the
    <a href="https://www.gnu.org/licenses/agpl-3.0.html" target="_blank" rel="noopener">GNU AGPLv3</a>${src ? `.
    <a href="${esc(src)}" target="_blank" rel="noopener">Source code</a>` : ""}.`;
}

/* ---------- auth ---------- */
function authView(setup) {
  return `<div class="auth"><div class="auth-card">
    <img class="auth-logo" src="/static/logo-homelab-club.png" alt="Homelab Club" width="180" height="147">
    <h1>${setup ? "Set up Patchrack" : "Sign in to Patchrack"}</h1>
    <p>${setup ? "This account protects Patchrack and the SSH keys it holds. You only do this once." : "Keep your Linux servers patched from one place."}</p>
    <form id="authForm" autocomplete="on">
      <div class="field"><label for="u">Username</label><input id="u" type="text" name="username" autocomplete="username" required value="${setup ? "admin" : ""}"></div>
      <div class="field"><label for="p">Password</label><input id="p" type="password" name="password" autocomplete="${setup ? "new-password" : "current-password"}" required minlength="${setup ? 10 : 1}">
        ${setup ? '<span class="hint">At least 10 characters.</span>' : ""}</div>
      ${setup ? '<div class="field"><label for="p2">Repeat password</label><input id="p2" type="password" autocomplete="new-password" required></div>' : ""}
      <div class="error-text" id="authErr"></div>
      <button class="btn primary" type="submit">${setup ? "Create account" : "Sign in"}</button>
    </form><p class="about muted">${aboutLine()}</p></div></div>`;
}
function bindAuth() {
  const setup = S.session.setup_required;
  $("#authForm").onsubmit = async (e) => {
    e.preventDefault();
    const username = $("#u").value.trim(), password = $("#p").value;
    if (setup && password !== $("#p2").value) { $("#authErr").textContent = "The passwords don't match."; return; }
    try {
      await api("POST", setup ? "/api/setup" : "/api/login", { username, password });
      S.session = await api("GET", "/api/session");
      await loadData(); render();
    } catch (err) { $("#authErr").textContent = err.message; }
  };
  $("#u").value ? $("#p").focus() : $("#u").focus();
}

/* ---------- servers view ---------- */
function sortedServers() {
  return [...S.servers.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
}

function renderServers() {
  const main = $("#main");
  const list = sortedServers();
  if (!list.length) {
    main.innerHTML = `<div class="rack"><div class="empty">
      <h2>Add your first server</h2>
      <p>Patchrack connects over SSH with its own key. You'll get a one-time setup command to paste on each server.</p>
      <button class="btn primary" data-action="add">${ICON.plus}Add server</button></div></div>`;
    $('[data-action="add"]').onclick = () => openEditor(null);
    return;
  }
  main.innerHTML = `
    <div class="fleet-head">
      <div><h1>Servers</h1><div class="tally" id="tally"></div></div>
      <div class="fleet-actions">
        <button class="btn" data-action="add">${ICON.plus}Add server</button>
        <button class="btn" data-action="groups">Groups…</button>
        <button class="btn" data-action="check-all">${ICON.refresh}Check all</button>
        <button class="btn primary" data-action="update-all">${ICON.down}Update…</button>
      </div>
    </div>
    <div id="rack">${groupSections(list).map(sectionHTML).join("")}</div>`;
  $('[data-action="add"]').onclick = () => openEditor(null);
  $('[data-action="groups"]').onclick = () => openGroups();
  $('[data-action="check-all"]').onclick = async (e) => {
    e.currentTarget.disabled = true;
    try { await api("POST", "/api/check-all"); toast("Checking all servers"); } catch (err) { toast(err.message, "bad"); }
    setTimeout(() => { const b = $('[data-action="check-all"]'); if (b) b.disabled = false; }, 2500);
  };
  $('[data-action="update-all"]').onclick = () => openBulkUpdate();
  bindRack();
  renderTally();
}

/* ---------- groups in the server list ---------- */
const RACK_HEAD = `<div class="rack-head" aria-hidden="true"><span></span><span>Server</span><span>System</span><span>Status</span><span>Checked</span><span></span></div>`;

function sectionKey(s) { return S.groups.length ? String(s.group_id ?? "none") : "all"; }

function groupSections(list) {
  if (!S.groups.length) return [{ key: "all", name: null, servers: list }];
  const sections = S.groups.map((g) => ({ key: String(g.id), id: g.id, name: g.name, servers: list.filter((s) => s.group_id === g.id) }));
  const loose = list.filter((s) => !S.groups.some((g) => g.id === s.group_id));
  if (loose.length) sections.push({ key: "none", id: null, name: "No group", servers: loose });
  return sections;
}

function groupSummary(servers) {
  if (!servers.length) return { led: "", text: "No servers yet" };
  const n = (f) => servers.filter(f).length;
  const bad = n((s) => s.status === "unreachable" || s.status === "error");
  const upd = n((s) => s.status === "updates" || s.status === "reboot" || containerUpdates(s) > 0);
  const busy = n((s) => s.busy);
  const parts = [plural(servers.length, "server")];
  if (upd) parts.push(`${upd} with updates`);
  if (bad) parts.push(`${bad} unreachable or failing`);
  if (!upd && !bad) parts.push("all up to date");
  return { led: busy ? "busy" : bad ? "bad" : upd ? "warn" : "ok", text: parts.join(", ") };
}

function sectionHTML(sec) {
  if (sec.name === null) return `<div class="rack" role="list" data-group="all">${RACK_HEAD}${sec.servers.map(unitHTML).join("")}</div>`;
  const closed = S.collapsed.has(sec.key);
  const sum = groupSummary(sec.servers);
  return `<section class="group" data-group="${sec.key}">
    <div class="group-head">
      <button class="group-toggle" data-gact="toggle" aria-expanded="${!closed}">
        <span class="chev" aria-hidden="true"></span><h2>${esc(sec.name)}</h2>
        <span class="group-sum"><span class="led ${sum.led}"></span><span data-sum>${esc(sum.text)}</span></span></button>
      ${sec.servers.length ? `<div class="group-acts">
        <button class="btn small" data-gact="check">${ICON.refresh}Check group</button>
        <button class="btn small" data-gact="update">${ICON.down}Update group…</button></div>` : ""}
    </div>
    ${closed ? "" : sec.servers.length ? `<div class="rack" role="list">${RACK_HEAD}${sec.servers.map(unitHTML).join("")}</div>`
      : `<div class="group-empty">No servers in this group. Use <b>Groups…</b> or a server's settings to add some.</div>`}
  </section>`;
}

function refreshGroupSums() {
  for (const sec of groupSections(sortedServers())) {
    const el = $(`[data-group="${sec.key}"] .group-sum`);
    if (!el) continue;
    const sum = groupSummary(sec.servers);
    el.querySelector(".led").className = `led ${sum.led}`;
    el.querySelector("[data-sum]").textContent = sum.text;
  }
}

async function groupAction(act, key) {
  const servers = groupSections(sortedServers()).find((x) => x.key === key)?.servers || [];
  if (act === "toggle") {
    S.collapsed.has(key) ? S.collapsed.delete(key) : S.collapsed.add(key);
    saveCollapsed(); renderServers();
    $(`[data-group="${key}"] [data-gact="toggle"]`)?.focus();
  } else if (act === "check") {
    try {
      if (key === "none") for (const s of servers) { if (!s.busy) await api("POST", `/api/servers/${s.id}/check`); }
      else await api("POST", `/api/groups/${key}/check`);
      toast(`Checking ${plural(servers.length, "server")}`);
    } catch (err) { toast(err.message, "bad"); }
  } else if (act === "update") {
    openBulkUpdate(servers.map((s) => s.id));
  }
}

function renderTally() {
  const el = $("#tally");
  if (!el) return;
  const list = [...S.servers.values()];
  const n = (f) => list.filter(f).length;
  const updates = n((s) => s.status === "updates");
  const sec = list.reduce((a, s) => a + (s.status === "updates" ? s.security_count : 0), 0);
  const ok = n((s) => s.status === "up_to_date" && !containerUpdates(s));
  const bad = n((s) => s.status === "unreachable" || s.status === "error");
  const reboot = n((s) => s.reboot_required);
  const busy = n((s) => s.busy);
  const parts = [];
  parts.push(`<span><span class="led ok"></span><strong>${ok}</strong> up to date</span>`);
  parts.push(`<span><span class="led warn"></span><strong>${updates}</strong> with updates${sec ? ` (${sec} security)` : ""}</span>`);
  const cu = list.reduce((a, s) => a + containerUpdates(s), 0);
  if (cu) parts.push(`<span><span class="led warn"></span><strong>${cu}</strong> container ${cu === 1 ? "update" : "updates"}</span>`);
  if (reboot) parts.push(`<span><strong>${reboot}</strong> need a reboot</span>`);
  if (bad) parts.push(`<span><span class="led bad"></span><strong>${bad}</strong> need attention</span>`);
  if (busy) parts.push(`<span><span class="led busy"></span><strong>${busy}</strong> working</span>`);
  el.innerHTML = parts.join("");
}

function unitHTML(s) {
  const st = statusOf(s);
  const open = S.expanded.has(s.id);
  const canUpdate = !s.busy && (s.status === "updates" || s.status === "reboot");
  const onlyContainers = !s.busy && !canUpdate && containerUpdates(s) > 0;
  return `<div class="unit" role="listitem" data-id="${s.id}" aria-expanded="${open}">
    <div class="unit-main" data-toggle tabindex="0" aria-label="${esc(s.name)}, ${esc(st.label)}">
      <span class="led-cell"><span class="led ${st.led}"></span></span>
      <span class="name"><b>${esc(s.name)}</b><small>${esc(s.username)}@${esc(s.host)}${s.port !== 22 ? ":" + s.port : ""}</small></span>
      <span class="os"><span>${esc(s.os_name || "—")}</span><small>${s.kernel ? esc(s.kernel) : ""}</small>${releaseTag(s)}</span>
      <span class="state ${st.cls}"><b>${esc(st.label)}</b><span class="sub ${s.status === "updates" && s.security_count && !s.busy ? "sec" : ""}">${esc(st.sub)}</span></span>
      <span class="seen">${ago(s.last_check_at)}</span>
      <span class="acts">
        ${s.busy ? `<button class="btn small" data-act="watch">View progress</button>` : `
        <button class="btn small" data-act="check">Check</button>
        ${onlyContainers ? `<button class="btn small primary" data-act="containers">Containers…</button>`
          : `<button class="btn small ${canUpdate ? "primary" : ""}" data-act="update">${s.status === "reboot" ? "Reboot…" : "Update…"}</button>`}`}
      </span>
    </div>
    ${dockerStripHTML(s)}
    ${open ? detailHTML(s) : ""}
  </div>`;
}

function detailHTML(s) {
  const meta = [];
  if (s.uptime_s) meta.push(`<span>Uptime <b>${uptime(s.uptime_s)}</b></span>`);
  meta.push(`<span>Upgrade mode <b>${s.upgrade_mode === "full" ? "full (dist-upgrade)" : "safe (upgrade)"}</b></span>`);
  if (s.last_update_at) meta.push(`<span>Last updated <b>${ago(s.last_update_at)}</b></span>`);
  if (s.host_key_fp) meta.push(`<span>Host key <b class="mono">${esc(s.host_key_fp)}</b></span>`);
  let body = "";
  if (s.last_error && (s.status === "error" || s.status === "unreachable")) {
    body += `<div class="notice bad">${esc(s.last_error)}${/host key changed/i.test(s.last_error) ? `<br><button class="btn small" data-act="forget">Trust the new host key</button>` : ""}</div>`;
  }
  if (s.reboot_required) body += `<div class="notice warn">Reboot required${s.reboot_reason ? `: ${esc(s.reboot_reason)}` : ""}.</div>`;
  if (s.packages.length) {
    body += `<div class="pkg-scroll"><table class="pkg-table"><thead><tr><th>Package</th><th>Installed</th><th>Available</th><th>Source</th></tr></thead><tbody>
      ${s.packages.map((p) => `<tr><td class="name">${esc(p.name)}${p.security ? '<span class="tag sec">security</span>' : ""}${p.new ? '<span class="tag reb">new</span>' : ""}</td>
        <td class="mono">${esc(p.from || "—")}</td><td class="mono">${esc(p.to)}</td><td class="mono">${esc(p.origin.split(",")[0])}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  if (s.kept_back.length) body += `<div class="notice">Held back by apt: <span class="mono">${esc(s.kept_back.join(", "))}</span></div>`;
  if (s.status === "up_to_date" && !s.reboot_required) body += `<div class="notice ok">All packages are current. Last checked ${fmtTime(s.last_check_at)}.</div>`;
  if (s.notes) body += `<div class="muted">${esc(s.notes)}</div>`;
  body += releaseHTML(s);
  body += dockerHTML(s);
  return `<div class="unit-detail">
    <div class="detail-meta">${meta.join("")}</div>
    ${body}
    <div class="acts" style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn small" data-act="edit">${ICON.gear}Server settings</button>
      ${s.key_source === "app" ? `<button class="btn small" data-act="setup">Setup command</button>` : ""}
      <button class="btn small ghost" data-act="history">History</button>
    </div>
  </div>`;
}

function releaseTag(s) {
  const r = s.release;
  if (!r) return "";
  if ((r.last || {}).state === "running" && !s.busy) return `<span class="tag rel">release upgrade running</span>`;
  if (!r.available || r.unsupported) return "";
  return `<span class="tag rel">${esc(shortRelease(r.target_name))} available</span>`;
}

function releaseHTML(s) {
  const r = s.release;
  if (!r) return "";
  const last = r.last || {};
  let html = "";
  if (last.state === "running" && !s.busy) {
    html += `<div class="notice warn">A release upgrade${r.target_name ? ` to ${esc(r.target_name)}` : ""} is running on this server (started ${ago(+last.started)}).
      <br><button class="btn small" data-act="release-follow">Follow its progress</button></div>`;
  } else if (last.state === "failed" && r.available) {
    html += `<div class="notice bad">The last release upgrade failed${last.finished ? ` (${fmtTime(+last.finished)})` : ""}. The log is in History and in /var/log/patchrack-release-upgrade.log on the server. Check the server before trying again.</div>`;
  }
  if ((s.status === "error" || s.status === "unreachable") && r.available) html += `<div class="muted" style="font-size:13px">From the last check that worked (${ago(r.checked_at)}). Fix the error above and press Check to refresh this.</div>`;
  if (r.unsupported) html += `<div class="notice">${esc(r.unsupported)}</div>`;
  else if (r.available && last.state !== "running") {
    html += `<div class="release-box">
      <div><h3>${esc(r.target_name)} is available</h3><p class="muted">This server runs ${esc(r.current || s.os_name)}. A release upgrade installs the new version of the whole system.</p></div>
      <ul class="steps">${(r.checks || []).map((c) => `<li><span class="led ${c.ok ? "ok" : "bad"}"></span><span>${esc(c.text)}</span></li>`).join("")}</ul>
      <div><button class="btn small ${r.ready ? "primary" : ""}" data-act="release" ${r.ready && !s.busy ? "" : "disabled"}>Upgrade to ${esc(r.target_name)}…</button></div>
    </div>`;
  } else if (r.note) html += `<div class="muted" style="font-size:13px">${esc(r.note)}</div>`;
  return html ? `<section class="release" id="release-${s.id}">${html}</section>` : "";
}

const CHECK_TEXT = { excluded: "Excluded", pinned: "Pinned version", local: "Built locally", unknown: "Can't check", self: "Patchrack itself" };

/* one-line Docker status shown in every server row */
function dockerStripHTML(s) {
  const st = s.docker_status;
  if (!st || st === "none" || st === "unknown") return "";
  let led, text, list = "";
  if (st === "no_access") { led = "warn"; text = "<b>Docker installed</b><span>Patchrack has no access yet. Run this server's setup command again.</span>"; }
  else if (st === "stopped") { led = "bad"; text = "<b>Docker not running</b><span>The Docker service on this server is stopped.</span>"; }
  else if (st === "error" && !s.docker) { led = "bad"; text = "<b>Docker check failed</b><span>Open the server for details.</span>"; }
  else {
    const d = s.docker;
    if (!d) return "";
    const ups = d.containers.filter((c) => c.check === "update").map((c) => c.name);
    const stopped = d.containers.length - d.running;
    led = st === "error" ? "bad" : "ok";
    text = `<b>Docker active</b><span>${plural(d.containers.length, "container")}${stopped ? `, ${d.running} running` : ""}</span>`;
    if (ups.length) {
      const shown = ups.slice(0, 4).map(esc).join(", ");
      list = `<span class="dk-ups"><span class="lbl"><span class="led warn"></span>${ups.length} need${ups.length === 1 ? "s" : ""} an update:</span> <span class="names">${shown}${ups.length > 4 ? ` and ${ups.length - 4} more` : ""}</span></span>`;
    } else {
      list = `<span class="dk-ok">All containers up to date</span>`;
    }
  }
  return `<button class="dk-strip" data-act="containers" aria-label="Docker status for ${esc(s.name)}">
    <span class="led ${led}"></span><span class="dk-what">${text}</span>${list}</button>`;
}

function dockerHTML(s) {
  if (s.docker_status === "none" || s.docker_status === "unknown" || !s.docker_status) return "";
  if (s.docker_status === "stopped") {
    return `<section class="docker" id="docker-${s.id}"><div class="docker-head"><h3>Docker</h3></div>
      <div class="notice bad">Docker is installed, but the Docker service isn't running. Start it on the server with <span class="mono">sudo systemctl start docker</span>, then press Check.</div></section>`;
  }
  if (s.docker_status === "no_access") {
    return `<section class="docker" id="docker-${s.id}"><div class="docker-head"><h3>Docker</h3></div>
      <div class="notice warn">Docker is installed, but Patchrack doesn't have access to it yet. Choose <b>Setup command</b> below, run it on the server as root, then press Check.</div></section>`;
  }
  if (s.docker_status === "error" && !s.docker) {
    return `<section class="docker" id="docker-${s.id}"><div class="docker-head"><h3>Docker</h3></div>
      <div class="notice bad">${esc(s.docker_error || "The Docker check failed.")}</div></section>`;
  }
  const d = s.docker;
  if (!d) return "";
  const sel = S.dockerSel.get(s.id) || new Set();
  const ups = d.containers.filter((c) => c.check === "update");
  const other = d.containers.filter((c) => !["update", "current"].includes(c.check));
  const current = d.containers.filter((c) => c.check === "current").length;
  const busy = !!s.busy;
  let html = `<section class="docker" id="docker-${s.id}">
    <div class="docker-head"><div><h3>Docker</h3><p class="muted">Version ${esc(d.version)}, ${plural(d.containers.length, "container")}, ${d.running} running. Checked ${ago(d.checked_at)}.</p></div></div>`;
  if (s.docker_status === "error") html += `<div class="notice bad">The last Docker check failed: ${esc(s.docker_error)}</div>`;
  if (ups.length) {
    const allSel = ups.every((c) => sel.has(c.id));
    html += `<div class="pkg-scroll"><table class="pkg-table ctable"><thead><tr>
        <th class="cb"><input type="checkbox" data-dk="all" ${allSel ? "checked" : ""} aria-label="Select all containers with updates" ${busy ? "disabled" : ""}></th>
        <th>Container</th><th>Image</th><th>Stack</th></tr></thead><tbody>
      ${ups.map((c) => `<tr><td class="cb"><input type="checkbox" data-dk="one" value="${esc(c.id)}" ${sel.has(c.id) ? "checked" : ""} aria-label="Select ${esc(c.name)}" ${busy ? "disabled" : ""}></td>
        <td class="name">${esc(c.name)}${c.state !== "running" ? ` <span class="muted">(${esc(c.state)})</span>` : ""}</td>
        <td class="mono">${esc(c.image)}${/already downloaded/.test(c.note) ? '<span class="tag reb">downloaded</span>' : ""}</td>
        <td class="mono">${esc(c.project || "—")}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="dk-acts">
        <button class="btn small primary" data-act="dk-update-sel" ${sel.size && !busy ? "" : "disabled"}>${sel.size ? `Update ${plural(sel.size, "container")}` : "Select containers to update"}</button>
        <button class="btn small" data-act="dk-update-all" ${busy ? "disabled" : ""}>Update all ${ups.length}</button>
      </div>`;
  } else {
    html += `<div class="notice ok">${current ? `All ${plural(current, "container")} run the newest image.` : "No containers with registry images to compare."}</div>`;
  }
  if (other.length) {
    html += `<details class="disclose"><summary>${plural(other.length, "container")} not checked</summary><div>
      ${other.map((c) => `<div class="other-row"><b>${esc(c.name)}</b> <span class="muted">${esc(CHECK_TEXT[c.check] || c.check)}: ${esc(c.note)}</span></div>`).join("")}
      <span class="hint">Add the label <span class="mono">patchrack.enable=false</span> to a container to leave it out of updates.</span></div></details>`;
  }
  const r = d.reclaimable || {};
  html += `<div class="dk-clean"><div><b>Disk space</b><span class="muted">${d.free != null ? `${fmtBytes(d.free)} free on the Docker disk. ` : ""}Docker estimates about ${fmtBytes(r.total)} can be freed.</span></div>
    <button class="btn small" data-act="dk-prune" ${busy ? "disabled" : ""}>Clean up…</button></div>`;
  return html + "</section>";
}

function bindRack() {
  const rack = $("#rack");
  if (!rack || rack.dataset.bound) return;
  rack.dataset.bound = "1";
  rack.addEventListener("click", (e) => {
    const gbtn = e.target.closest("[data-gact]");
    if (gbtn) { groupAction(gbtn.dataset.gact, gbtn.closest("[data-group]").dataset.group); return; }
    const unit = e.target.closest(".unit");
    if (!unit) return;
    const id = +unit.dataset.id, s = S.servers.get(id);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act) { e.stopPropagation(); return unitAction(act, s); }
    if (e.target.closest("[data-toggle]")) toggleUnit(id);
  });
  rack.addEventListener("change", (e) => {
    const box = e.target.closest("[data-dk]");
    if (!box) return;
    const id = +box.closest(".unit").dataset.id, s = S.servers.get(id);
    const sel = S.dockerSel.get(id) || new Set();
    const ups = (s.docker?.containers || []).filter((c) => c.check === "update").map((c) => c.id);
    if (box.dataset.dk === "all") { sel.clear(); if (box.checked) ups.forEach((c) => sel.add(c)); }
    else box.checked ? sel.add(box.value) : sel.delete(box.value);
    S.dockerSel.set(id, sel);
    refreshRack(id);
    $(`.unit[data-id="${id}"] [data-dk="${box.dataset.dk}"]${box.dataset.dk === "one" ? `[value="${box.value}"]` : ""}`)?.focus();
  });
  rack.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-toggle]")) {
      e.preventDefault(); toggleUnit(+e.target.closest(".unit").dataset.id);
    }
  });
}
function toggleUnit(id) {
  S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id);
  refreshRack(id);
  $(`.unit[data-id="${id}"] [data-toggle]`)?.focus();
}

async function unitAction(act, s) {
  try {
    if (act === "check") { const j = await api("POST", `/api/servers/${s.id}/check`); S.jobs.set(j.id, j); }
    else if (act === "update") openUpdate(s);
    else if (act === "watch") openJob(s.busy);
    else if (act === "containers") {
      S.expanded.add(s.id); refreshRack(s.id);
      $(`#docker-${s.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    else if (act === "dk-update-sel") openDockerUpdate(s, [...(S.dockerSel.get(s.id) || [])]);
    else if (act === "dk-update-all") openDockerUpdate(s, s.docker.containers.filter((c) => c.check === "update").map((c) => c.id));
    else if (act === "dk-prune") openDockerPrune(s);
    else if (act === "release") openRelease(s);
    else if (act === "release-follow") { const j = await api("POST", `/api/servers/${s.id}/release-upgrade/follow`); S.jobs.set(j.id, j); openJob(j.id); }
    else if (act === "edit") openEditor(s);
    else if (act === "setup") openSetup(s);
    else if (act === "history") { location.hash = "#/history"; S.historyFilter = s.id; }
    else if (act === "forget") { await api("POST", `/api/servers/${s.id}/forget-host-key`); toast("Host key cleared. The next connection will trust the new key."); }
  } catch (err) { toast(err.message, "bad"); }
}

function refreshRack(id) {
  if (view() !== "servers" || !S.session?.user) return;
  const rack = $("#rack");
  if (!rack || id === undefined) { renderServers(); return; }
  const s = S.servers.get(id);
  const old = rack.querySelector(`.unit[data-id="${id}"]`);
  const section = old?.closest("[data-group]");
  if (!s || !old || !section || section.dataset.group !== sectionKey(s)) {
    // new server, deleted server or moved to another group: draw the list again
    if (!s || !(S.collapsed.has(sectionKey(s)))) renderServers(); else { renderTally(); refreshGroupSums(); }
    return;
  }
  const focusedAct = document.activeElement?.closest?.(`.unit[data-id="${id}"]`) ? document.activeElement.dataset.act : null;
  const tmp = document.createElement("div");
  tmp.innerHTML = unitHTML(s);
  old.replaceWith(tmp.firstElementChild);
  if (focusedAct) rack.querySelector(`.unit[data-id="${id}"] [data-act="${focusedAct}"]`)?.focus();
  renderTally();
  refreshGroupSums();
}
setInterval(() => { if (view() === "servers" && !$(".sheet")) $$(".unit").forEach((u) => { const s = S.servers.get(+u.dataset.id); const el = u.querySelector(".seen"); if (s && el) el.textContent = ago(s.last_check_at); }); }, 30000);

/* ---------- sheets & modals ---------- */
function openSheet(html, { onClose } = {}) {
  closeSheet();
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  const sheet = document.createElement("aside");
  sheet.className = "sheet";
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.innerHTML = html;
  document.body.append(scrim, sheet);
  const prevFocus = document.activeElement;
  const close = () => closeSheet();
  scrim.onclick = close;
  const onKey = (e) => { if (e.key === "Escape" && !$(".modal")) close(); };
  document.addEventListener("keydown", onKey);
  S.sheetCleanup = () => { document.removeEventListener("keydown", onKey); onClose && onClose(); scrim.remove(); sheet.remove(); prevFocus?.focus?.(); };
  $$("[data-close]", sheet).forEach((b) => (b.onclick = close));
  setTimeout(() => (sheet.querySelector("[autofocus]") || sheet.querySelector("input,button,select,textarea"))?.focus(), 30);
  return sheet;
}
function closeSheet() { if (S.sheetCleanup) { const f = S.sheetCleanup; S.sheetCleanup = null; f(); } }

function confirmBox(title, text, okLabel, danger = false) {
  return new Promise((resolve) => {
    const scrim = document.createElement("div"); scrim.className = "scrim"; scrim.style.zIndex = 50;
    const m = document.createElement("div"); m.className = "modal"; m.style.zIndex = 51; m.setAttribute("role", "alertdialog");
    m.innerHTML = `<h2>${esc(title)}</h2><p>${esc(text)}</p><div class="acts"><button class="btn" data-no>Cancel</button><button class="btn ${danger ? "danger" : "primary"}" data-yes>${esc(okLabel)}</button></div>`;
    document.body.append(scrim, m);
    const done = (v) => { scrim.remove(); m.remove(); document.removeEventListener("keydown", key); resolve(v); };
    const key = (e) => { if (e.key === "Escape") done(false); };
    document.addEventListener("keydown", key);
    scrim.onclick = () => done(false);
    m.querySelector("[data-no]").onclick = () => done(false);
    m.querySelector("[data-yes]").onclick = () => done(true);
    m.querySelector("[data-yes]").focus();
  });
}

/* ---------- update (single server) ---------- */
function rebootChoices(name, def, reqd, reason) {
  const opt = (val, title, text) => `<label class="choice"><input type="radio" name="${name}" value="${val}" ${def === val ? "checked" : ""}><span><b>${title}</b><span>${text}</span></span></label>`;
  return `<div class="seg" role="radiogroup" aria-label="Reboot">
    ${opt("never", "Don't reboot", "Install updates only. You can reboot later.")}
    ${opt("if_required", "Reboot only if required", reqd ? `A reboot is required right now${reason ? ` (${esc(reason)})` : ""}.` : "Reboots only when the updates ask for it, for example a new kernel.")}
    ${opt("always", "Always reboot", "Reboot after updating, even if nothing asks for it.")}
  </div>`;
}

function openUpdate(s) {
  const n = s.pending_count;
  const def = s.reboot_policy || "if_required";
  const btnLabel = n ? `Install ${plural(n, "update")}` : (s.reboot_required ? "Run and reboot" : "Run update");
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led ${statusOf(s).led}"></span><div><h2>Update ${esc(s.name)}</h2>
      <p>${n ? `${plural(n, "package")} will be installed${s.security_count ? `, including ${s.security_count} security fixes` : ""}.` : "No package updates are pending."} The list is refreshed again right before installing.</p></div>
      <button class="btn ghost small" data-close aria-label="Close">Close</button></div>
    <div class="sheet-body">
      <div><div class="section-title">Reboot</div>${rebootChoices("reboot", n ? def : (s.reboot_required ? "always" : def), s.reboot_required, s.reboot_reason)}</div>
      <label class="check"><input type="checkbox" id="autoremove" ${s.autoremove ? "checked" : ""}> Remove packages that are no longer needed (autoremove)</label>
      ${n ? `<details class="disclose"><summary>${plural(n, "package")}</summary><div><div class="mono muted">${s.packages.map((p) => esc(p.name)).join(", ")}</div></div></details>` : ""}
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="go">${btnLabel}</button></div>`);
  $("#go", sheet).onclick = async () => {
    const reboot = $('input[name="reboot"]:checked', sheet).value;
    if (reboot === "always" || (reboot === "if_required" && s.reboot_required)) {
      if (!(await confirmBox(`Reboot ${s.name}?`, `${s.name} will restart after updating. Services on it will be briefly unavailable.`, "Update and reboot"))) return;
    }
    try {
      const j = await api("POST", `/api/servers/${s.id}/update`, { reboot, autoremove: $("#autoremove", sheet).checked });
      S.jobs.set(j.id, j);
      openJob(j.id);
    } catch (err) { toast(err.message, "bad"); }
  };
}

/* ---------- live job view ---------- */
function openJob(jobId) {
  const job = S.jobs.get(jobId);
  if (!job) return;
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led busy" id="jobLed"></span><div><h2 id="jobTitle">${JOB_VERB[job.kind] || "Working on"} ${esc(job.server_name)}</h2>
      <p id="jobPhase">${esc(job.phase)}</p></div><button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <div id="outcome"></div>
      <ul class="phases" id="phases"></ul>
      <div><div class="section-title">Output</div><div class="term" id="term" tabindex="0" aria-label="Command output"></div></div>
    </div>
    <div class="sheet-foot"><span class="left muted" id="jobFootNote" style="font-size:13px;align-self:center">You can close this panel; the job keeps running.</span><button class="btn" data-close>Close</button></div>`,
  { onClose: () => es.close() });
  const term = $("#term", sheet), phases = $("#phases", sheet);
  let lastPhase = null;
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  es.addEventListener("line", (ev) => {
    const l = JSON.parse(ev.data);
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    const span = document.createElement("span");
    span.className = l.lvl === "out" ? "" : l.lvl;
    span.textContent = (l.lvl === "phase" ? "▸ " : "") + l.msg + "\n";
    term.append(span);
    if (atBottom) term.scrollTop = term.scrollHeight;
    if (l.lvl === "phase") {
      if (lastPhase) { lastPhase.className = "done"; lastPhase.querySelector(".led").className = "led ok"; }
      const li = document.createElement("li");
      li.className = "current";
      li.innerHTML = `<span class="led busy"></span><span>${esc(l.msg)}</span>`;
      phases.append(li); lastPhase = li;
      $("#jobPhase", sheet).textContent = l.msg;
    }
  });
  es.addEventListener("done", (ev) => {
    es.close();
    const j = JSON.parse(ev.data);
    const ok = j.status === "success";
    if (lastPhase) { lastPhase.className = "done"; lastPhase.querySelector(".led").className = `led ${ok ? "ok" : "bad"}`; }
    $("#jobLed", sheet).className = `led ${ok ? "ok" : "bad"}`;
    $("#jobTitle", sheet).textContent = `${JOB_NOUN[j.kind] || "Job"} on ${j.server_name}`;
    $("#jobPhase", sheet).textContent = ok ? `Finished in ${Math.max(1, Math.round(j.finished_at - j.started_at))} s` : "Failed";
    $("#jobFootNote", sheet).textContent = "";
    const r = j.result || {};
    let title, good;
    if (j.kind === "update") {
      good = ok && !r.pending && !r.reboot_required;
      title = ok ? (good ? `${j.server_name} is up to date` : "Update finished") : "Update failed";
    } else if (j.kind === "docker_update") {
      good = ok && !r.remaining;
      title = ok ? (good ? "All containers are up to date" : "Containers updated") : (r.updated && r.updated.length ? "Some containers failed to update" : "Container update failed");
    } else if (j.kind === "release_upgrade") {
      good = ok; title = ok ? "Release upgrade finished" : "Release upgrade failed";
    } else if (j.kind === "docker_prune") {
      good = ok; title = ok ? `Freed ${fmtBytes(r.freed)}` : "Clean-up failed";
    } else { good = ok; title = ok ? "Check finished" : "Check failed"; }
    $("#outcome", sheet).innerHTML = `<div class="outcome ${ok ? (good ? "ok" : "") : "bad"}">
      <span class="led ${ok ? (good ? "ok" : "warn") : "bad"}"></span>
      <div><b>${esc(title)}</b>
      <span>${esc(j.kind === "docker_prune" && ok ? j.summary.replace(/^Freed [\d.]+ [KMGT]?B\.\s*/, "") : j.summary)}</span></div></div>`;
  });
}

/* ---------- setup command for one server ---------- */
async function openSetup(s) {
  let script = "";
  try { script = (await api("GET", `/api/ssh-key?username=${encodeURIComponent(s.username)}`)).setup_script; }
  catch (err) { toast(err.message, "bad"); return; }
  const sheet = openSheet(`
    <div class="sheet-head"><div><h2>Setup command for ${esc(s.name)}</h2>
      <p>For the user <span class="mono">${esc(s.username)}</span>, the user Patchrack signs in with on this server.</p></div>
      <button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <ol class="plain-steps">
        <li>Sign in to ${esc(s.name)} and become root: <span class="mono">sudo -i</span></li>
        <li>Paste the command below and press Enter. It should end with <span class="mono">Done: ${esc(s.username)} can now be managed by Patchrack</span>.</li>
        <li>Back here, press <b>Check</b> on the server.</li>
      </ol>
      <div class="codebox"><pre>${esc(script)}</pre><button type="button" class="btn small" id="copySetupOne">${ICON.copy}Copy</button></div>
      <div class="notice">The command only allows apt, dpkg repair, reboot, Docker and the release upgrade helper through sudo for this user. Running it again is safe.</div>
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Done</button></div>`);
  $("#copySetupOne", sheet).onclick = () => copyText(script);
}

/* ---------- release upgrade ---------- */
function openRelease(s) {
  const r = s.release || {};
  const debian = r.family === "debian";
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led warn"></span><div><h2>Upgrade ${esc(s.name)} to ${esc(r.target_name)}</h2>
      <p>${esc(r.current || s.os_name)} becomes ${esc(r.target_name)}.</p></div>
      <button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <div class="notice">Patchrack installs the new release on the server. This usually takes 30 to 90 minutes, and services on the server may stop for a while. The upgrade keeps running on the server even if you close this panel or Patchrack restarts.</div>
      <div class="notice">${debian
        ? `Patchrack switches Debian's own package sources to ${esc(r.target)} and follows Debian's upgrade steps. Package sources from other providers are left as they are: check that they support ${esc(r.target)}. <a href="https://www.debian.org/releases/${encodeURIComponent(r.target)}/release-notes/" target="_blank" rel="noopener">Read Debian's release notes</a> first.`
        : `Ubuntu's own upgrader does the work. It switches off package sources from other providers, such as Docker's, during the upgrade. Patchrack lists them afterwards, so you can turn them back on once they support the new release.`}</div>
      <div><div class="section-title">Checks</div><ul class="steps">${(r.checks || []).map((c) => `<li><span class="led ${c.ok ? "ok" : "bad"}"></span><span>${esc(c.text)}</span></li>`).join("")}</ul></div>
      <div class="notice warn">A release upgrade can't be undone automatically. Make a backup or VM snapshot first, and try it on a test server before your important ones.</div>
      <label class="check"><input type="checkbox" id="rlBackup"> I have a backup or snapshot of ${esc(s.name)}</label>
      <label class="check"><input type="checkbox" id="rlReboot" checked> Reboot when the upgrade is done (recommended)</label>
      <div class="field"><label for="rlName">Type <b>${esc(s.name)}</b> to confirm</label><input id="rlName" type="text" autocomplete="off" spellcheck="false"></div>
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="rlGo" disabled>Start release upgrade</button></div>`);
  const sync = () => { $("#rlGo", sheet).disabled = !($("#rlBackup", sheet).checked && $("#rlName", sheet).value.trim() === s.name && r.ready); };
  sheet.addEventListener("input", sync); sheet.addEventListener("change", sync);
  $("#rlGo", sheet).onclick = async () => {
    try {
      const j = await api("POST", `/api/servers/${s.id}/release-upgrade`, { reboot: $("#rlReboot", sheet).checked, backup_confirmed: true, confirm_name: $("#rlName", sheet).value.trim() });
      S.jobs.set(j.id, j); openJob(j.id);
    } catch (err) { toast(err.message, "bad"); }
  };
}

/* ---------- groups management ---------- */
function openGroups() {
  const sheet = openSheet(`
    <div class="sheet-head"><div><h2>Groups</h2><p>Sort your servers into groups, for example by location or by what they run. Each server can be in one group.</p></div>
      <button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body" id="grBody"></div>
    <div class="sheet-foot"><button class="btn" data-close>Done</button></div>`, { onClose: () => document.removeEventListener("pd:groups", draw) });
  const body = $("#grBody", sheet);
  function draw() {
    const opts = (sel) => `<option value="">No group</option>${S.groups.map((g) => `<option value="${g.id}" ${sel === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}`;
    body.innerHTML = `
      <form class="gr-new" id="grNew"><div class="field" style="flex:1"><label for="grName">New group</label><input id="grName" type="text" maxlength="40" placeholder="Media, Network, Lab…"></div>
        <button class="btn" type="submit">${ICON.plus}Add group</button></form>
      ${S.groups.length ? `<div><div class="section-title">Your groups</div><div class="gr-list">${S.groups.map((g) => `
        <div class="gr-row" data-gid="${g.id}"><input type="text" value="${esc(g.name)}" maxlength="40" aria-label="Name of group ${esc(g.name)}">
          <span class="muted">${plural(g.servers, "server")}</span>
          <button class="btn small" data-gr="rename">Rename</button><button class="btn small danger" data-gr="delete">Delete</button></div>`).join("")}</div></div>` : ""}
      <div><div class="section-title">Servers</div>${S.servers.size ? `<div class="gr-list">${sortedServers().map((s) => `
        <div class="gr-row" data-sid="${s.id}"><b>${esc(s.name)}</b><select aria-label="Group for ${esc(s.name)}">${opts(s.group_id)}</select></div>`).join("")}</div>`
        : `<p class="muted">Add servers first.</p>`}</div>`;
    $("#grNew", body).onsubmit = async (e) => {
      e.preventDefault();
      const name = $("#grName", body).value.trim();
      if (!name) return;
      try { await api("POST", "/api/groups", { name }); S.groups = await api("GET", "/api/groups"); draw(); toast(`Group ${name} added`); $("#grName", body)?.focus(); }
      catch (err) { toast(err.message, "bad"); }
    };
    $$("[data-gr]", body).forEach((b) => (b.onclick = async () => {
      const row = b.closest("[data-gid]"), gid = +row.dataset.gid, g = S.groups.find((x) => x.id === gid);
      try {
        if (b.dataset.gr === "rename") {
          const name = row.querySelector("input").value.trim();
          if (!name || name === g.name) return;
          await api("PUT", `/api/groups/${gid}`, { name }); toast(`Renamed to ${name}`);
        } else {
          if (!(await confirmBox(`Delete the group ${g.name}?`, g.servers ? `Its ${plural(g.servers, "server")} stay in Patchrack, without a group.` : "The group is empty.", "Delete group", true))) return;
          await api("DELETE", `/api/groups/${gid}`); toast(`Group ${g.name} deleted`);
        }
        S.groups = await api("GET", "/api/groups"); draw();
      } catch (err) { toast(err.message, "bad"); }
    }));
    $$("[data-gid] input", body).forEach((inp) => (inp.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); inp.closest("[data-gid]").querySelector('[data-gr="rename"]').click(); } }));
    $$("[data-sid] select", body).forEach((sel) => (sel.onchange = async () => {
      const sid = +sel.closest("[data-sid]").dataset.sid;
      try { const saved = await api("PUT", `/api/servers/${sid}/group`, { group_id: sel.value ? +sel.value : null }); S.servers.set(sid, saved); }
      catch (err) { toast(err.message, "bad"); }
    }));
  }
  document.addEventListener("pd:groups", draw);
  draw();
}

/* ---------- docker: update & clean up ---------- */
function openDockerUpdate(s, ids) {
  const byId = new Map(s.docker.containers.map((c) => [c.id, c]));
  const list = ids.map((i) => byId.get(i)).filter(Boolean);
  if (!list.length) return;
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led warn"></span><div><h2>Update containers on ${esc(s.name)}</h2>
      <p>Each container gets the newest version of its image and keeps its settings, volumes and networks.</p></div>
      <button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <div><div class="section-title">${plural(list.length, "container")}</div>
        <ul class="steps">${list.map((c) => `<li><span class="led warn"></span><span><b>${esc(c.name)}</b> <span class="muted mono">${esc(c.image)}</span></span></li>`).join("")}</ul></div>
      <div class="notice">For each one, Patchrack downloads the new image, stops the container, starts a new one with the same configuration, and checks that it stays running. If it doesn't, the previous container is restored. Containers that share its network are reconnected. Volumes are never removed.</div>
      <label class="check"><input type="checkbox" id="dkRm" checked> Remove the old images afterwards to free disk space</label>
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="dkGo">Update ${plural(list.length, "container")}</button></div>`);
  $("#dkGo", sheet).onclick = async () => {
    try {
      const j = await api("POST", `/api/servers/${s.id}/docker/update`, { container_ids: list.map((c) => c.id), remove_old_images: $("#dkRm", sheet).checked });
      S.jobs.set(j.id, j); S.dockerSel.delete(s.id);
      openJob(j.id);
    } catch (err) { toast(err.message, "bad"); }
  };
}

function openDockerPrune(s) {
  const r = s.docker.reclaimable || {};
  const opt = (id, on, title, text) => `<label class="choice"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span><b>${title}</b><span>${text}</span></span></label>`;
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led"></span><div><h2>Clean up Docker on ${esc(s.name)}</h2>
      <p>${s.docker.free != null ? `${fmtBytes(s.docker.free)} is free on the Docker disk now. ` : ""}Docker estimates about ${fmtBytes(r.total)} can be freed.</p></div>
      <button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <div><div class="section-title">Remove</div><div class="seg">
        ${opt("pr-img", true, "Unused images", `Images no container uses${r.unused_images ? `, about ${fmtBytes(r.images)}` : ""}. They are downloaded again if you need them.`)}
        ${opt("pr-build", true, "Build cache", r.build_cache ? `About ${fmtBytes(r.build_cache)}.` : "Left over from building images on this server.")}
        ${opt("pr-net", true, "Unused networks", "Networks no container is connected to.")}
        ${opt("pr-cont", false, "Stopped containers", `${r.stopped_containers ? plural(r.stopped_containers, "stopped container") + ". " : ""}This also removes containers you stopped on purpose.`)}
      </div></div>
      <div class="notice">Volumes are never removed, so your data stays where it is.</div>
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="prGo">Clean up</button></div>`);
  const sync = () => { $("#prGo", sheet).disabled = !$$("#pr-img, #pr-build, #pr-net, #pr-cont", sheet).some((c) => c.checked); };
  sheet.addEventListener("change", sync);
  $("#prGo", sheet).onclick = async () => {
    if ($("#pr-cont", sheet).checked && !(await confirmBox("Remove stopped containers?", "Every container on this server that isn't running is deleted, including ones you stopped on purpose. Their volumes are kept.", "Remove them", true))) return;
    try {
      const j = await api("POST", `/api/servers/${s.id}/docker/prune`, {
        images: $("#pr-img", sheet).checked, build_cache: $("#pr-build", sheet).checked,
        networks: $("#pr-net", sheet).checked, containers: $("#pr-cont", sheet).checked });
      S.jobs.set(j.id, j); openJob(j.id);
    } catch (err) { toast(err.message, "bad"); }
  };
}

/* ---------- bulk update ---------- */
function openBulkUpdate(onlyIds = null) {
  const list = sortedServers();
  const eligible = list.filter((s) => !s.busy && (s.status === "updates" || s.status === "reboot") && (!onlyIds || onlyIds.includes(s.id)));
  const anyReboot = eligible.some((s) => s.reboot_required);
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led warn"></span><div><h2>Update servers</h2>
      <p>Choose which servers to update. Up to ${S.settings?.max_parallel ?? 3} run at the same time.</p></div><button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body">
      <div><div class="section-title">Servers</div><div class="seg" id="pick">
        ${list.length ? groupSections(list).filter((g) => g.servers.length).map((g) => `
          ${g.name !== null ? `<label class="check pick-group"><input type="checkbox" data-pg="${g.key}"> <b>${esc(g.name)}</b></label>` : ""}
          ${g.servers.map((s) => {
            const st = statusOf(s);
            const sel = eligible.includes(s);
            return `<label class="choice"><input type="checkbox" value="${s.id}" data-in="${g.key}" ${sel ? "checked" : ""} ${s.busy ? "disabled" : ""}>
              <span><b>${esc(s.name)}</b><span>${esc(st.label)}${st.sub ? ", " + esc(st.sub) : ""}</span></span></label>`;
          }).join("")}`).join("") : '<p class="muted">No servers yet.</p>'}
      </div></div>
      <div><div class="section-title">Reboot</div>${rebootChoices("breboot", "if_required", anyReboot, anyReboot ? "on at least one selected server" : "")}</div>
      <label class="check"><input type="checkbox" id="bauto" checked> Remove packages that are no longer needed (autoremove)</label>
      <div id="bulkProgress"></div>
    </div>
    <div class="sheet-foot"><button class="btn" data-close>Cancel</button><button class="btn primary" id="bgo">Update selected</button></div>`,
  { onClose: () => document.removeEventListener("pd:job", onJob) });
  const ids = () => $$("#pick input[value]:checked", sheet).map((i) => +i.value);
  const syncGroups = () => $$("[data-pg]", sheet).forEach((g) => {
    const boxes = $$(`[data-in="${g.dataset.pg}"]:not(:disabled)`, sheet);
    g.checked = boxes.length > 0 && boxes.every((b) => b.checked);
    g.indeterminate = !g.checked && boxes.some((b) => b.checked);
  });
  const sync = () => { const n = ids().length; $("#bgo", sheet).disabled = !n; $("#bgo", sheet).textContent = n ? `Update ${plural(n, "server")}` : "Select servers"; syncGroups(); };
  $("#pick", sheet).addEventListener("change", (e) => {
    if (e.target.dataset.pg) $$(`[data-in="${e.target.dataset.pg}"]:not(:disabled)`, sheet).forEach((b) => (b.checked = e.target.checked));
    sync();
  });
  sync();
  let started = [];
  const onJob = () => paint();
  const paint = () => {
    if (!started.length) return;
    $("#bulkProgress", sheet).innerHTML = `<div class="section-title">Progress</div><div class="list">${started.map((j) => {
      const cur = S.jobs.get(j.id) || j;
      const led = cur.status === "running" ? "busy" : cur.status === "success" ? "ok" : "bad";
      return `<div class="hist" data-job="${cur.id}" style="grid-template-columns:18px 1fr auto"><span class="led ${led}"></span><span><b>${esc(cur.server_name)}</b><br><span class="muted" style="font-size:13px">${esc(cur.status === "running" ? cur.phase : cur.summary)}</span></span><button class="btn small">Log</button></div>`;
    }).join("")}</div>`;
    $$("[data-job]", sheet).forEach((r) => (r.onclick = () => openJob(r.dataset.job)));
  };
  document.addEventListener("pd:job", onJob);
  $("#bgo", sheet).onclick = async () => {
    const reboot = $('input[name="breboot"]:checked', sheet).value;
    if (reboot !== "never" && !(await confirmBox("Reboot servers?", reboot === "always" ? "Every selected server restarts after updating." : "Servers that need it restart after updating.", "Start updates"))) return;
    try {
      const res = await api("POST", "/api/update-many", { server_ids: ids(), reboot, autoremove: $("#bauto", sheet).checked });
      started = res.started; started.forEach((j) => S.jobs.set(j.id, j));
      res.skipped.forEach((s) => toast(s.reason, "bad"));
      $("#bgo", sheet).disabled = true; $("#bgo", sheet).textContent = "Running…";
      $$("#pick input, input[name=breboot], #bauto", sheet).forEach((i) => (i.disabled = true));
      paint();
    } catch (err) { toast(err.message, "bad"); }
  };
}

/* ---------- add / edit server ---------- */
async function openEditor(s) {
  const isNew = !s;
  s = s || { name: "", host: "", port: 22, username: "patchrack", key_source: "app", become: "sudo", upgrade_mode: "full", autoremove: true, reboot_policy: "if_required", notes: "" };
  let keyInfo = null;
  try { keyInfo = await api("GET", `/api/ssh-key?username=${encodeURIComponent(s.username || "patchrack")}`); } catch {}
  const sheet = openSheet(`
    <div class="sheet-head"><div><h2>${isNew ? "Add server" : `${esc(s.name)} settings`}</h2>
      <p>${isNew ? "Patchrack signs in with an SSH key. Passwords are never used to log in." : "Leave secret fields empty to keep what's stored."}</p></div><button class="btn ghost small" data-close>Close</button></div>
    <form class="sheet-body" id="ed" autocomplete="off">
      <div class="row2 row-group">
        <div class="field"><label for="f-name">Name</label><input id="f-name" type="text" value="${esc(s.name)}" placeholder="pve-node-01" required autofocus></div>
        <div class="field"><label for="f-group">Group</label><select id="f-group">
          <option value="">No group</option>${S.groups.map((g) => `<option value="${g.id}" ${s.group_id === g.id ? "selected" : ""}>${esc(g.name)}</option>`).join("")}
          <option value="new">New group…</option></select></div>
      </div>
      <div class="field hidden" id="f-newgroup-box"><label for="f-newgroup">Name of the new group</label><input id="f-newgroup" type="text" maxlength="40" placeholder="Media, Network, Lab…"></div>
      <div class="row2">
        <div class="field"><label for="f-host">Hostname or IP address</label><input id="f-host" type="text" value="${esc(s.host)}" placeholder="192.168.1.20" required></div>
        <div class="field"><label for="f-port">SSH port</label><input id="f-port" type="number" min="1" max="65535" value="${s.port}"></div>
      </div>
      <div class="field"><label for="f-user">SSH user</label><input id="f-user" type="text" value="${esc(s.username)}" required>
        <span class="hint">The setup command below creates this user for you.</span></div>

      <div class="field"><span class="label">SSH key</span><div class="seg">
        <label class="choice"><input type="radio" name="ks" value="app" ${s.key_source === "app" ? "checked" : ""}><span><b>Use the Patchrack key</b><span>Recommended. Run the setup command on the server once.</span></span></label>
        <label class="choice"><input type="radio" name="ks" value="custom" ${s.key_source === "custom" ? "checked" : ""}><span><b>Use my own private key</b><span>Paste an existing key. It's encrypted before it's stored.</span></span></label>
      </div></div>

      <div id="appKeyBox" class="subcard">
        <div><h3>Setup command</h3><p class="muted" style="margin:4px 0 0;font-size:13.5px">Run this once on the server as root. It creates the <span class="mono">${esc(s.username || "patchrack")}</span> user, installs the Patchrack public key, and allows only apt, dpkg repair, reboot, Docker and the release upgrade helper through sudo. Already set up? Run it again after updating Patchrack to install the newest helper.</p></div>
        <div class="codebox"><pre id="script">${esc(keyInfo?.setup_script || "Could not load the key")}</pre><button type="button" class="btn small" id="copyScript">${ICON.copy}Copy</button></div>
      </div>

      <div id="customKeyBox" class="subcard">
        <div class="field"><label for="f-key">Private key (OpenSSH format)</label>
          <textarea id="f-key" placeholder="${s.has_custom_key ? "A key is stored. Paste a new one to replace it." : "-----BEGIN OPENSSH PRIVATE KEY-----"}" spellcheck="false"></textarea></div>
        <div class="field"><label for="f-pass">Key passphrase</label><input id="f-pass" type="password" autocomplete="new-password" placeholder="${s.has_passphrase ? "Stored. Leave empty to keep." : "Only if the key has one"}"></div>
      </div>

      <div class="field"><span class="label">Running updates as root</span><div class="seg">
        <label class="choice"><input type="radio" name="bc" value="sudo" ${s.become === "sudo" ? "checked" : ""}><span><b>sudo without a password</b><span>Set up by the setup command.</span></span></label>
        <label class="choice"><input type="radio" name="bc" value="sudo_password" ${s.become === "sudo_password" ? "checked" : ""}><span><b>sudo with a password</b><span>The password is encrypted and only used for sudo.</span></span></label>
        <label class="choice"><input type="radio" name="bc" value="root" ${s.become === "root" ? "checked" : ""}><span><b>The SSH user is root</b><span>No sudo needed.</span></span></label>
      </div></div>
      <div class="field" id="sudoBox"><label for="f-sudo">sudo password</label><input id="f-sudo" type="password" autocomplete="new-password" placeholder="${s.has_sudo_password ? "Stored. Leave empty to keep." : ""}"></div>

      <details class="disclose"><summary>Update defaults</summary><div>
        <div class="row-even">
          <div class="field"><label for="f-mode">Upgrade mode</label><select id="f-mode">
            <option value="full" ${s.upgrade_mode === "full" ? "selected" : ""}>Full (apt-get dist-upgrade)</option>
            <option value="safe" ${s.upgrade_mode === "safe" ? "selected" : ""}>Safe (apt-get upgrade)</option></select>
            <span class="hint">Full can install new dependencies, such as new kernels.</span></div>
          <div class="field"><label for="f-reboot">Default reboot choice</label><select id="f-reboot">
            <option value="never" ${s.reboot_policy === "never" ? "selected" : ""}>Don't reboot</option>
            <option value="if_required" ${s.reboot_policy === "if_required" ? "selected" : ""}>Reboot only if required</option>
            <option value="always" ${s.reboot_policy === "always" ? "selected" : ""}>Always reboot</option></select>
            <span class="hint">Pre-selected in the update panel. You always confirm.</span></div>
        </div>
        <label class="check"><input type="checkbox" id="f-auto" ${s.autoremove ? "checked" : ""}> Autoremove by default</label>
        <div class="field"><label for="f-notes">Notes</label><input id="f-notes" type="text" value="${esc(s.notes || "")}" placeholder="Runs Pi-hole, update last"></div>
      </div></details>

      ${!isNew && s.host_key_fp ? `<div class="field"><span class="label">Trusted host key</span><div class="keyline">${esc(s.host_key_fp)}</div>
        <div><button type="button" class="btn small" id="forget">Forget trusted key</button></div>
        <span class="hint">Only needed if you reinstalled the server. Patchrack refuses to connect when the key changes.</span></div>` : ""}

      <div id="testOut"></div>
      <div class="error-text" id="edErr"></div>
    </form>
    <div class="sheet-foot">
      ${isNew ? "" : `<button class="btn danger left" id="del">Delete</button>`}
      <button class="btn" data-close>Cancel</button>
      <button class="btn primary" id="save">${isNew ? "Add and test connection" : "Save and test"}</button>
    </div>`);

  const syncBoxes = () => {
    const ks = $('input[name="ks"]:checked', sheet).value, bc = $('input[name="bc"]:checked', sheet).value;
    $("#appKeyBox", sheet).classList.toggle("hidden", ks !== "app");
    $("#customKeyBox", sheet).classList.toggle("hidden", ks !== "custom");
    $("#sudoBox", sheet).classList.toggle("hidden", bc !== "sudo_password");
  };
  sheet.addEventListener("change", syncBoxes); syncBoxes();
  $("#f-group", sheet).addEventListener("change", (e) => {
    $("#f-newgroup-box", sheet).classList.toggle("hidden", e.target.value !== "new");
    if (e.target.value === "new") $("#f-newgroup", sheet).focus();
  });
  $("#copyScript", sheet).onclick = () => copyText($("#script", sheet).textContent);
  let userTimer;
  $("#f-user", sheet).addEventListener("input", () => {
    clearTimeout(userTimer);
    userTimer = setTimeout(async () => {
      const u = $("#f-user", sheet).value.trim();
      if (!/^[a-z_][a-z0-9_.-]*$/.test(u)) return;
      try { const k = await api("GET", `/api/ssh-key?username=${encodeURIComponent(u)}`); $("#script", sheet).textContent = k.setup_script; } catch {}
    }, 350);
  });
  $("#forget", sheet)?.addEventListener("click", async () => {
    if (!(await confirmBox("Forget the trusted host key?", "The next connection will trust whatever key the server presents. Only do this if you reinstalled or rekeyed the server.", "Forget key", true))) return;
    await api("POST", `/api/servers/${s.id}/forget-host-key`); toast("Trusted host key cleared");
    closeSheet();
  });
  $("#del", sheet)?.addEventListener("click", async () => {
    if (!(await confirmBox(`Delete ${s.name}?`, "Its stored credentials are removed from Patchrack. The server itself is not changed.", "Delete server", true))) return;
    try { await api("DELETE", `/api/servers/${s.id}`); S.servers.delete(s.id); closeSheet(); renderServers(); toast(`${s.name} deleted`); }
    catch (err) { toast(err.message, "bad"); }
  });
  // once a new server is saved, retries update it instead of creating a duplicate
  let savedId = isNew ? null : s.id;
  const save = async () => {
    let groupId = $("#f-group", sheet).value;
    if (groupId === "new") {
      const name = $("#f-newgroup", sheet).value.trim();
      if (!name) { $("#edErr", sheet).textContent = "Give the new group a name, or choose another group."; return; }
      try { groupId = (await api("POST", "/api/groups", { name })).id; S.groups = await api("GET", "/api/groups"); }
      catch (err) { $("#edErr", sheet).textContent = err.message; return; }
    }
    const body = {
      name: $("#f-name", sheet).value.trim(), host: $("#f-host", sheet).value.trim(), port: +$("#f-port", sheet).value || 22,
      username: $("#f-user", sheet).value.trim(), key_source: $('input[name="ks"]:checked', sheet).value,
      private_key: $("#f-key", sheet).value.trim() || null, passphrase: $("#f-pass", sheet).value || null,
      become: $('input[name="bc"]:checked', sheet).value, sudo_password: $("#f-sudo", sheet).value || null,
      upgrade_mode: $("#f-mode", sheet).value, reboot_policy: $("#f-reboot", sheet).value,
      autoremove: $("#f-auto", sheet).checked, notes: $("#f-notes", sheet).value.trim() || null,
      group_id: groupId ? +groupId : null,
    };
    const btn = $("#save", sheet);
    $("#edErr", sheet).textContent = "";
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const saved = await api(savedId ? "PUT" : "POST", savedId ? `/api/servers/${savedId}` : "/api/servers", body);
      savedId = saved.id;
      S.servers.set(saved.id, saved);
      if (view() === "servers") renderServers();
      btn.textContent = "Testing connection…";
      $("#testOut", sheet).innerHTML = "";
      const t = await api("POST", `/api/servers/${saved.id}/test`);
      $("#testOut", sheet).innerHTML = `<div class="section-title">Connection test</div><ul class="steps">${t.steps.map((st) => `<li><span class="led ${st.ok ? "ok" : "bad"}"></span><span>${esc(st.msg)}</span></li>`).join("")}</ul>`;
      if (t.ok) {
        btn.textContent = "Check for updates now"; btn.disabled = false; btn.classList.replace("primary", "go");
        btn.onclick = async () => { try { const j = await api("POST", `/api/servers/${saved.id}/check`); S.jobs.set(j.id, j); } catch (e) { toast(e.message, "bad"); } closeSheet(); };
        $("#testOut", sheet).scrollIntoView({ behavior: "smooth", block: "nearest" });
      } else {
        btn.textContent = "Save and test again"; btn.disabled = false;
      }
    } catch (err) {
      $("#edErr", sheet).textContent = err.message; btn.disabled = false; btn.textContent = savedId ? "Save and test" : "Add and test connection";
    }
  };
  $("#save", sheet).onclick = save;
  $("#ed", sheet).onsubmit = (e) => { e.preventDefault(); $("#save", sheet).click(); };
}

/* ---------- history ---------- */
async function renderHistory() {
  const main = $("#main");
  main.innerHTML = `<div class="page-head"><div><h1>History</h1><p>Every check and update, newest first.</p></div>
    <div class="fleet-actions"><select id="hf" aria-label="Filter by server" style="width:200px"><option value="">All servers</option>
      ${sortedServers().map((s) => `<option value="${s.id}" ${S.historyFilter === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>
      <select id="hk" aria-label="Filter by kind" style="width:150px"><option value="">Everything</option><option value="update">System updates</option><option value="docker_update">Container updates</option><option value="docker_prune">Docker clean-ups</option><option value="release_upgrade">Release upgrades</option><option value="check">Checks</option></select></div></div>
    <div class="list" id="hl"><div class="empty"><p>Loading…</p></div></div>`;
  const load = async () => {
    const sid = $("#hf").value;
    S.historyFilter = sid ? +sid : null;
    let rows = await api("GET", `/api/history?limit=300${sid ? `&server_id=${sid}` : ""}`);
    const kind = $("#hk").value;
    if (kind) rows = rows.filter((r) => r.kind === kind);
    $("#hl").innerHTML = rows.length ? rows.map((r) => `
      <div class="hist" data-h="${r.id}" tabindex="0"><span class="led ${r.result === "success" ? "ok" : "bad"}"></span>
        <span><b>${esc(r.server_name)}</b></span><span>${esc(r.summary || (r.result === "success" ? "Done" : "Failed"))}</span>
        <span class="kind">${JOB_NOUN[r.kind] || r.kind}${r.trigger === "schedule" ? ", scheduled" : ""}</span>
        <span class="when" title="${fmtTime(r.started_at)}">${ago(r.started_at)}</span></div>`).join("")
      : `<div class="empty"><h2>Nothing here yet</h2><p>Checks and updates appear here once they've run.</p></div>`;
    $$("[data-h]").forEach((el) => {
      const open = () => openLog(rows.find((r) => r.id === +el.dataset.h));
      el.onclick = open; el.onkeydown = (e) => { if (e.key === "Enter") open(); };
    });
  };
  $("#hf").onchange = load; $("#hk").onchange = load;
  try { await load(); } catch (err) { $("#hl").innerHTML = `<div class="empty"><p class="error-text">${esc(err.message)}</p></div>`; }
}

async function openLog(r) {
  const sheet = openSheet(`
    <div class="sheet-head"><span class="led ${r.result === "success" ? "ok" : "bad"}"></span><div><h2>${JOB_NOUN[r.kind] || "Job"} on ${esc(r.server_name)}</h2>
      <p>${fmtTime(r.started_at)}${r.finished_at ? `, took ${Math.max(1, Math.round(r.finished_at - r.started_at))} s` : ""}</p></div><button class="btn ghost small" data-close>Close</button></div>
    <div class="sheet-body"><div class="outcome ${r.result === "success" ? "ok" : "bad"}"><span class="led ${r.result === "success" ? "ok" : "bad"}"></span><div><b>${r.result === "success" ? "Succeeded" : "Failed"}</b><span>${esc(r.summary)}</span></div></div>
      <div><div class="section-title">Output</div><div class="term" id="logTerm" style="height:calc(100vh - 340px);min-height:260px">Loading…</div></div></div>
    <div class="sheet-foot"><button class="btn" id="copyLog">${ICON.copy}Copy log</button><button class="btn" data-close>Close</button></div>`);
  try {
    const res = await fetch(`/api/history/${r.id}/log`, { credentials: "same-origin" });
    const text = await res.text();
    const term = $("#logTerm", sheet);
    term.textContent = "";
    for (const line of text.split("\n")) {
      const m = line.match(/^\[(\w+)\] (.*)$/);
      const span = document.createElement("span");
      if (m) { span.className = m[1] === "out" ? "" : m[1]; span.textContent = (m[1] === "phase" ? "▸ " : "") + m[2] + "\n"; }
      else span.textContent = line + "\n";
      term.append(span);
    }
    $("#copyLog", sheet).onclick = () => copyText(text);
  } catch (err) { $("#logTerm", sheet).textContent = err.message; }
}

/* ---------- settings ---------- */
async function loadNotificationLog() {
  const box = $("#nlog");
  if (!box) return;
  let rows;
  try { rows = await api("GET", "/api/notifications"); } catch (err) { box.innerHTML = `<p class="error-text">${esc(err.message)}</p>`; return; }
  if (!rows.length) { box.innerHTML = `<p class="muted">No messages yet. Use Send test, or wait for the next scheduled check.</p>`; return; }
  const LABEL = { pushover: "Pushover", prowl: "Prowl" };
  box.innerHTML = `<div class="nlog">${rows.map((r) => {
    const res = Object.entries(r.results || {});
    const allOk = res.length && res.every(([, v]) => v[0]);
    const led = r.note ? "" : allOk ? "ok" : res.some(([, v]) => v[0]) ? "warn" : "bad";
    const detail = r.note ? esc(r.note) : res.map(([k, v]) => `${LABEL[k] || k}: ${v[0] ? "sent" : `<span class="bad-text">${esc(v[1])}</span>`}`).join(", ");
    return `<div class="nrow"><span class="led ${led}"></span><div><b>${esc(r.title)}</b> <span class="muted">${esc(fmtTime(r.at))}</span>
      ${r.message ? `<div class="nmsg">${esc(r.message.split("\n").join(", "))}</div>` : ""}<div class="ndetail">${detail}</div></div></div>`;
  }).join("")}</div>`;
}

async function renderSettings() {
  const main = $("#main");
  main.innerHTML = `<div class="page-head"><div><h1>Settings</h1></div></div><div class="settings" id="st"><div class="card"><p class="muted">Loading…</p></div></div>`;
  let st, key;
  try { [st, key] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/ssh-key")]); }
  catch (err) { $("#st").innerHTML = `<div class="card"><p class="error-text">${esc(err.message)}</p></div>`; return; }
  S.settings = st;
  const sw = (id, on, title, text) => `<div class="split"><div><b>${title}</b>${text ? `<p>${text}</p>` : ""}</div>
    <label class="switch"><input type="checkbox" id="${id}" ${on ? "checked" : ""} aria-label="${esc(title)}"><span class="track"></span></label></div>`;
  const secret = (id, has, label, hint) => `<div class="field"><label for="${id}">${label}</label>
    <input id="${id}" type="password" autocomplete="off" placeholder="${has ? "Saved and encrypted. Leave empty to keep." : ""}">
    ${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
  const hours = [1, 3, 6, 12, 24, 168];
  const when = (ts) => {
    const d = new Date(ts * 1000), today = new Date();
    const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === today.toDateString() ? time : d.toLocaleDateString(undefined, { weekday: "long" }) + " " + time;
  };
  const schedText = !st.schedule_enabled ? "Switched off. Patchrack only checks when you press Check."
    : `${st.last_scheduled_check ? `Last check ${ago(st.last_scheduled_check)}.` : "Hasn't run yet."} Next check around ${when(st.next_scheduled_check)}.`;
  const remind = [["daily", "Once a day"], ["every", "After every scheduled check"], ["never", "Don't remind me"]];
  $("#st").innerHTML = `
    <section class="card">
      <header><h2>Automatic checks</h2><p>Patchrack looks for updates on a schedule. It never installs anything without you pressing Update. Checks only run while the computer running Patchrack is switched on and awake.</p></header>
      ${sw("s-sched", st.schedule_enabled, "Check on a schedule", schedText)}
      <div class="row-even">
        <div class="field"><label for="s-int">Check every</label><select id="s-int">
          ${hours.map((h) => `<option value="${h}" ${+st.check_interval_hours === h ? "selected" : ""}>${h === 168 ? "week" : h === 24 ? "day" : plural(h, "hour")}</option>`).join("")}
          ${hours.includes(+st.check_interval_hours) ? "" : `<option value="${st.check_interval_hours}" selected>${st.check_interval_hours} hours</option>`}</select></div>
        <div class="field"><label for="s-par">Servers at the same time</label><input id="s-par" type="number" min="1" max="20" value="${st.max_parallel}"></div>
      </div>
      <div class="field" style="max-width:calc(50% - 6px)"><label for="s-rto">Wait for reboot (minutes)</label><input id="s-rto" type="number" min="2" max="60" value="${st.reboot_timeout_min}">
        <span class="hint">How long to wait for a server to come back before marking the update failed.</span></div>
    </section>

    <section class="card">
      <header><h2>Notifications</h2><p>Get a push message on your phone. Keys are encrypted before they're stored.</p></header>
      <div class="subcard">
        ${sw("n-found", st.notify_updates_found, "System updates available", "From scheduled checks, one message for all servers.")}
        ${sw("n-done", st.notify_update_done, "Update finished", "")}
        ${sw("n-fail", st.notify_update_failed, "Update failed", "Sent with high priority.")}
        ${sw("n-cont", st.notify_container_updates, "Container updates available", "From scheduled checks, when containers have newer images.")}
        ${sw("n-rel", st.notify_release_available, "New release available", "When a scheduled check finds a new Ubuntu or Debian release.")}
        ${sw("n-unr", st.notify_unreachable, "Server unreachable", "When a scheduled check can't connect.")}
      </div>
      <div class="field" style="max-width:calc(50% - 6px)"><label for="n-rem">Remind me about waiting updates</label><select id="n-rem">
        ${remind.map(([v, t]) => `<option value="${v}" ${(st.notify_reminder || "daily") === v ? "selected" : ""}>${t}</option>`).join("")}</select>
        <span class="hint">New updates are always reported at the next scheduled check. Reminders repeat the list while updates are waiting.</span></div>
      <div class="subcard">
        ${sw("po-on", st.pushover_enabled, "Pushover", "Create an application at pushover.net to get a token.")}
        <div class="row-even">${secret("po-token", st.has_pushover_token, "Application API token")}${secret("po-user", st.has_pushover_user, "User key")}</div>
        <div class="acts"><button class="btn small" data-test="pushover">Send test</button>${st.has_pushover_token ? '<button class="btn small ghost" data-clear="pushover_token">Remove token</button>' : ""}</div>
      </div>
      <div class="subcard">
        ${sw("pr-on", st.prowl_enabled, "Prowl", "Find your API key at prowlapp.com under API Keys.")}
        ${secret("pr-key", st.has_prowl_apikey, "API key")}
        <div class="acts"><button class="btn small" data-test="prowl">Send test</button>${st.has_prowl_apikey ? '<button class="btn small ghost" data-clear="prowl_apikey">Remove key</button>' : ""}</div>
      </div>
      <div class="field"><label for="s-url">Patchrack address (optional)</label><input id="s-url" type="url" value="${esc(st.app_url)}" placeholder="http://192.168.1.10:8080">
        <span class="hint">Added as a link to notifications.</span></div>
    </section>
    <div class="acts" style="display:flex;justify-content:flex-end"><button class="btn primary" id="saveSettings">Save settings</button></div>

    <section class="card">
      <header><h2>Recent messages</h2><p>What Patchrack sent, and the answer from Pushover and Prowl. If something wasn't sent, the reason is shown here.</p></header>
      <div id="nlog"><p class="muted">Loading…</p></div>
    </section>

    <section class="card">
      <header><h2>Patchrack SSH key</h2><p>The key Patchrack uses to sign in to your servers. The private half never leaves Patchrack and is stored encrypted.</p></header>
      <div class="field"><span class="label">Public key</span><div class="keyline">${esc(key.public_key)}</div><span class="hint">Fingerprint ${esc(key.fingerprint)}</span></div>
      <div class="field"><label for="setupUser">Setup command for the SSH user</label><input id="setupUser" type="text" value="patchrack" spellcheck="false" style="max-width:260px">
        <span class="hint">The command sets up only the user named here. A server that signs in with another user needs the command for that user: open the server and choose <b>Setup command</b>.</span></div>
      <div class="codebox"><pre id="setupPre">${esc(key.setup_script)}</pre><button class="btn small" id="copySetup">${ICON.copy}Copy</button></div>
      <div class="acts"><button class="btn small" id="copyPub">${ICON.copy}Copy public key</button><button class="btn small danger" id="rotate">Replace key</button></div>
    </section>

    <section class="card">
      <header><h2>Account</h2><p>Signed in as <b>${esc(S.session.user)}</b>.</p></header>
      <form id="pwForm" class="row-even" autocomplete="off">
        <div class="field"><label for="pw-cur">Current password</label><input id="pw-cur" type="password" autocomplete="current-password" required></div>
        <div class="field"><label for="pw-new">New password</label><input id="pw-new" type="password" autocomplete="new-password" minlength="10" required></div>
        <div><button class="btn" type="submit">Change password</button></div>
      </form>
    </section>
    <p class="muted" style="font-size:13px">${aboutLine()}</p>`;

  const collect = () => ({
    schedule_enabled: $("#s-sched").checked, check_interval_hours: +$("#s-int").value, max_parallel: +$("#s-par").value || 3,
    reboot_timeout_min: +$("#s-rto").value || 10,
    notify_updates_found: $("#n-found").checked, notify_update_done: $("#n-done").checked,
    notify_update_failed: $("#n-fail").checked, notify_unreachable: $("#n-unr").checked, notify_release_available: $("#n-rel").checked, notify_container_updates: $("#n-cont").checked,
    notify_reminder: $("#n-rem").value,
    pushover_enabled: $("#po-on").checked, pushover_token: $("#po-token").value.trim() || null, pushover_user: $("#po-user").value.trim() || null,
    prowl_enabled: $("#pr-on").checked, prowl_apikey: $("#pr-key").value.trim() || null, app_url: $("#s-url").value.trim(),
  });
  const saveAll = async (quiet) => { S.settings = await api("PUT", "/api/settings", collect()); if (!quiet) toast("Settings saved"); };
  $("#saveSettings").onclick = async () => { try { await saveAll(); renderSettings(); } catch (err) { toast(err.message, "bad"); } };
  $$("[data-test]").forEach((b) => (b.onclick = async () => {
    b.disabled = true; b.textContent = "Sending…";
    const name = b.dataset.test === "pushover" ? "Pushover" : "Prowl";
    try {
      await saveAll(true);
      const r = await api("POST", "/api/notify/test", { channel: b.dataset.test });
      if (!r.ok) toast(r.message, "bad");
      else if (!r.enabled) toast(`Test sent, but ${name} is switched off, so Patchrack won't send it real notifications. Turn on the ${name} switch and save.`, "warn");
      else toast(`Test sent through ${name}`);
    } catch (err) { toast(err.message, "bad"); }
    b.disabled = false; b.textContent = "Send test";
    loadNotificationLog();
  }));
  loadNotificationLog();
  $$("[data-clear]").forEach((b) => (b.onclick = async () => { await api("POST", "/api/settings/clear-secret", { key: b.dataset.clear }); toast("Removed"); renderSettings(); }));
  $("#copySetup").onclick = () => copyText($("#setupPre").textContent);
  let setupTimer;
  $("#setupUser").oninput = (e) => {
    clearTimeout(setupTimer);
    setupTimer = setTimeout(async () => {
      const u = e.target.value.trim() || "patchrack";
      try { $("#setupPre").textContent = (await api("GET", `/api/ssh-key?username=${encodeURIComponent(u)}`)).setup_script; }
      catch { $("#setupPre").textContent = "That isn't a valid Linux user name."; }
    }, 300);
  };
  $("#copyPub").onclick = () => copyText(key.public_key);
  $("#rotate").onclick = async () => {
    if (!(await confirmBox("Replace the Patchrack key?", "Servers using the current key stop working until you run the new setup command on each one. Only do this if the key may be compromised.", "Replace key", true))) return;
    try { await api("POST", "/api/ssh-key/rotate"); toast("New key created. Run the setup command on each server."); renderSettings(); } catch (err) { toast(err.message, "bad"); }
  };
  $("#pwForm").onsubmit = async (e) => {
    e.preventDefault();
    try { await api("PUT", "/api/account/password", { current: $("#pw-cur").value, new: $("#pw-new").value }); toast("Password changed"); e.target.reset(); }
    catch (err) { toast(err.message, "bad"); }
  };
}

boot();
