#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
#
# patchrack-release-upgrade: upgrades this server to the next Ubuntu or Debian release.
# Installed as /usr/local/sbin/patchrack-release-upgrade by Patchrack's setup command.
#
#   patchrack-release-upgrade version
#   patchrack-release-upgrade start ubuntu -          (needs root)
#   patchrack-release-upgrade start debian <codename> (needs root)
#   patchrack-release-upgrade status
#
# The upgrade runs as a background service, so it continues when the SSH connection
# drops. Progress goes to /var/log/patchrack-release-upgrade.log and the result to
# /var/lib/patchrack/release-upgrade.state; both are readable without root.
HELPER_VERSION=1

: "${PATCHRACK_STATE_DIR:=/var/lib/patchrack}"
: "${PATCHRACK_LOG:=/var/log/patchrack-release-upgrade.log}"
: "${PATCHRACK_APT_DIR:=/etc/apt}"
: "${PATCHRACK_OS_RELEASE:=/etc/os-release}"
STATE="$PATCHRACK_STATE_DIR/release-upgrade.state"
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a APT_LISTCHANGES_FRONTEND=none UCF_FORCE_CONFFOLD=1
APT_OPTS=(-o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

say() { printf '%s\n' "$*"; }
# shellcheck source=/dev/null
os_value() { (. "$PATCHRACK_OS_RELEASE" 2>/dev/null; eval "printf '%s' \"\${$1:-}\""); }

write_state() {   # key=value pairs; written atomically, readable by everyone
  mkdir -p "$PATCHRACK_STATE_DIR"
  printf '%s\n' "$@" > "$STATE.tmp" && chmod 644 "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"
}

is_running() {
  [ -f "$STATE" ] && grep -q '^state=running$' "$STATE" || return 1
  local pid
  pid="$(sed -n 's/^pid=//p' "$STATE")"
  [ -n "$pid" ] && [ -d "/proc/$pid" ] && return 0
  systemctl is-active --quiet patchrack-release-upgrade 2>/dev/null
}

# --- Debian: point Debian's own package sources at the new release -------------------
# Only entries that use a debian.org address are changed; third-party sources are left
# alone and listed, so you can check whether they support the new release.
is_official() { grep -qE '(^|[/.])debian\.org(/|[[:space:]]|$)|//deb\.debian\.org|//security\.debian\.org' <<<"$1"; }

switch_list_file() {   # one-line format: deb http://deb.debian.org/debian bookworm main
  local file="$1" old="$2" new="$3" line changed=0 tmp
  tmp="$(mktemp)"
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^[[:space:]]*deb(-src)?[[:space:]] ]] && is_official "$line" \
       && [[ " $line " =~ [[:space:]]$old(-[a-z]+)?[[:space:]] ]]; then
      line="$(sed -E "s/([[:space:]])$old(-[a-z]+)?([[:space:]]|$)/\\1$new\\2\\3/" <<<"$line")"
      changed=$((changed + 1))
    elif [[ "$line" =~ ^[[:space:]]*deb(-src)?[[:space:]] ]] && [[ " $line " =~ [[:space:]]$old(-[a-z]+)?[[:space:]] ]]; then
      say "Left unchanged (not a Debian source): $file: $line" >&2
    fi
    printf '%s\n' "$line"
  done <"$file" >"$tmp"
  cat "$tmp" >"$file"
  rm -f "$tmp"
  echo "$changed"
}

switch_sources_file() {   # deb822 format: stanzas with URIs: and Suites: lines
  local file="$1" old="$2" new="$3"
  awk -v old="$old" -v new="$new" -v file="$file" '
    function flush(   i, n, parts, j, s, out, official) {
      official = (uris ~ /(^|[\/.])debian\.org(\/|[ \t]|$)/)
      for (i = 1; i <= count; i++) {
        line = buf[i]
        if (line ~ /^Suites:/) {
          n = split(substr(line, 8), parts, /[ \t]+/); out = "Suites:"; hit = 0
          for (j = 1; j <= n; j++) {
            s = parts[j]; if (s == "") continue
            if (s == old || index(s, old "-") == 1) { if (official) { s = new substr(s, length(old) + 1) }; hit = 1 }
            out = out " " s
          }
          if (hit && official) { changed++; line = out }
          else if (hit) { print "Left unchanged (not a Debian source): " file ": " uris > "/dev/stderr" }
        }
        print line
      }
      count = 0; uris = ""
    }
    /^[ \t]*$/ { flush(); print; next }
    { buf[++count] = $0; if ($0 ~ /^URIs:/) uris = uris " " substr($0, 6) }
    END { flush(); print changed + 0 > "/dev/fd/3" }
  ' "$file" 3>"$file.count" >"$file.new" && cat "$file.new" >"$file"
  rm -f "$file.new"
  cat "$file.count"; rm -f "$file.count"
}

switch_debian_sources() {   # prints how many entries were switched
  local old="$1" new="$2" total=0 n f
  for f in "$PATCHRACK_APT_DIR/sources.list" "$PATCHRACK_APT_DIR"/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    n="$(switch_list_file "$f" "$old" "$new")"; total=$((total + n))
  done
  for f in "$PATCHRACK_APT_DIR"/sources.list.d/*.sources; do
    [ -f "$f" ] || continue
    n="$(switch_sources_file "$f" "$old" "$new")"; total=$((total + n))
  done
  echo "$total"
}

run_debian() {
  local new="$1" old backup n
  old="$(os_value VERSION_CODENAME)"
  [ -n "$old" ] || { say "Couldn't read the current Debian codename."; return 1; }
  [ "$old" != "$new" ] || { say "This server already runs Debian $new."; return 1; }
  backup="/var/backups/patchrack-apt-sources-$(date +%Y%m%d-%H%M%S).tar.gz"
  say "== Saving the current package sources to $backup"
  tar -czf "$backup" -C "$PATCHRACK_APT_DIR" sources.list sources.list.d 2>/dev/null \
    || tar -czf "$backup" -C "$PATCHRACK_APT_DIR" sources.list.d
  say "== Switching Debian's package sources from $old to $new"
  n="$(switch_debian_sources "$old" "$new")"
  if [ "${n:-0}" -eq 0 ]; then
    say "No Debian package sources (debian.org) were found to switch. Nothing was changed."
    return 1
  fi
  say "Switched $n source entries."
  say "== apt-get update"
  if ! apt-get "${APT_OPTS[@]}" update; then
    say "Reading the new package lists failed. Restoring the previous package sources."
    tar -xzf "$backup" -C "$PATCHRACK_APT_DIR" && apt-get update -qq || true
    say "Nothing else was changed."
    return 1
  fi
  say "== Minimal upgrade (apt-get upgrade --without-new-pkgs)"
  apt-get "${APT_OPTS[@]}" -y upgrade --without-new-pkgs || return 1
  say "== Full upgrade (apt-get full-upgrade)"
  apt-get "${APT_OPTS[@]}" -y full-upgrade || return 1
  say "== Removing packages that are no longer needed"
  apt-get "${APT_OPTS[@]}" -y autoremove || true
}

run_ubuntu() {
  say "== do-release-upgrade (non-interactive)"
  do-release-upgrade -f DistUpgradeViewNonInteractive </dev/null
  local rc=$?
  local off
  off="$(grep -lisE 'disabled on upgrade|^Enabled: no' "$PATCHRACK_APT_DIR"/sources.list.d/* 2>/dev/null | xargs -r -n1 basename | tr '\n' ' ')"
  [ -n "$off" ] && say "SWITCHED_OFF_SOURCES=$off"
  return $rc
}

do_run() {   # the actual upgrade; runs in the background
  local family="$1" target="$2" before after rc started
  started="$(date +%s)"
  before="$(os_value VERSION_ID)"
  write_state "state=running" "pid=$$" "family=$family" "target=$target" "from=$(os_value PRETTY_NAME)" "started=$started"
  {
    say "Patchrack release upgrade started $(date)"
    say "From: $(os_value PRETTY_NAME)"
    if [ "$family" = ubuntu ]; then run_ubuntu; else run_debian "$target"; fi
    rc=$?
    after="$(os_value VERSION_ID)"
    if [ "$rc" -eq 0 ] && [ "$before" = "$after" ]; then
      say "The upgrade finished, but the system still reports version $after."
      rc=1
    fi
    say "Finished $(date) with exit code $rc. Now: $(os_value PRETTY_NAME)"
    write_state "state=$([ "$rc" -eq 0 ] && echo success || echo failed)" "exit=$rc" "family=$family" \
      "target=$target" "from=$(sed -n 's/^from=//p' "$STATE")" "to=$(os_value PRETTY_NAME)" \
      "started=$started" "finished=$(date +%s)"
  } >>"$PATCHRACK_LOG" 2>&1
}

start() {
  local family="$1" target="${2:--}"
  [ "$(id -u)" -eq 0 ] || { say "Run this as root."; exit 1; }
  case "$family" in ubuntu|debian) ;; *) say "Unknown system: $family"; exit 2 ;; esac
  [[ "$target" =~ ^([a-z]+|-)$ ]] || { say "Invalid release name: $target"; exit 2; }
  [ "$family" = debian ] && [ "$target" = - ] && { say "Give the Debian release name, for example trixie."; exit 2; }
  if is_running; then say "A release upgrade is already running."; exit 3; fi
  [ "$(os_value ID)" = "$family" ] || { say "This server isn't $family."; exit 2; }
  command -v pveversion >/dev/null 2>&1 && { say "This is a Proxmox VE host. Use Proxmox's own upgrade guide."; exit 2; }
  : >"$PATCHRACK_LOG"; chmod 644 "$PATCHRACK_LOG"
  write_state "state=running" "family=$family" "target=$target" "from=$(os_value PRETTY_NAME)" "started=$(date +%s)"
  if [ -d /run/systemd/system ] && command -v systemd-run >/dev/null 2>&1; then
    systemctl reset-failed patchrack-release-upgrade >/dev/null 2>&1 || true
    systemd-run --quiet --unit=patchrack-release-upgrade --description="Patchrack release upgrade" \
      --collect "$(readlink -f "$0")" _run "$family" "$target"
  else
    setsid nohup "$(readlink -f "$0")" _run "$family" "$target" </dev/null >/dev/null 2>&1 &
    write_state "state=running" "pid=$!" "family=$family" "target=$target" "from=$(os_value PRETTY_NAME)" "started=$(date +%s)"
  fi
  say "started"
}

main() {
  case "${1:-}" in
    version) say "patchrack-release-upgrade $HELPER_VERSION" ;;
    start)   shift; start "$@" ;;
    status)  if [ -f "$STATE" ]; then cat "$STATE"; else say "state=none"; fi
             if grep -q '^state=running$' "$STATE" 2>/dev/null && ! is_running; then say "stale=1"; fi ;;
    _run)    [ "$(id -u)" -eq 0 ] || exit 1; shift; do_run "$@" ;;
    *)       sed -n '5,16p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
}

# Only run when executed, so the functions can be tested on their own
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
