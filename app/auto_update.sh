#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
#
# patchrack-auto-update: installs updates on this server on a schedule, without Patchrack.
# Installed as /usr/local/sbin/patchrack-auto-update by Patchrack's setup command.
#
#   patchrack-auto-update version
#   patchrack-auto-update enable MINUTE HOUR DAYS MODE AUTOREMOVE CONTAINERS   (needs root)
#       DAYS: * for every day, or days of the week as cron numbers, e.g. 1,4 (0 = Sunday)
#       MODE: full (apt-get dist-upgrade) or safe (apt-get upgrade); AUTOREMOVE, CONTAINERS: 0 or 1
#   patchrack-auto-update disable      removes the cron job and the settings (needs root)
#   patchrack-auto-update status       settings and the result of the last run
#   patchrack-auto-update run          what cron runs (needs root)
#
# A run: apt-get update, then upgrade. When that worked and CONTAINERS is 1, it pulls new
# images for the running services of Docker Compose projects and recreates the ones that
# changed. Finally, when the system asks for it (/var/run/reboot-required), it reboots.
# The log of the last run is /var/log/patchrack-auto-update.log; the run before it is .log.1.
HELPER_VERSION=1

: "${PATCHRACK_CONF_DIR:=/etc/patchrack}"
: "${PATCHRACK_CRON_FILE:=/etc/cron.d/patchrack-auto-update}"
: "${PATCHRACK_STATE_DIR:=/var/lib/patchrack}"
: "${PATCHRACK_LOG:=/var/log/patchrack-auto-update.log}"
: "${PATCHRACK_REBOOT_FILE:=/var/run/reboot-required}"
: "${PATCHRACK_LOCK:=/run/lock/patchrack-auto-update.lock}"
CONF="$PATCHRACK_CONF_DIR/auto-update.conf"
STATE="$PATCHRACK_STATE_DIR/auto-update.state"
SELF=/usr/local/sbin/patchrack-auto-update
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# cron runs jobs in the system time zone (/etc/localtime), whatever a login session sets
unset TZ
export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a APT_LISTCHANGES_FRONTEND=none UCF_FORCE_CONFFOLD=1
APT_OPTS=(-o DPkg::Lock::Timeout=900 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

say() { printf '%s\n' "$*"; }
need_root() { [ "$(id -u)" -eq 0 ] || { say "Run this as root."; exit 1; }; }
conf_value() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | head -n1; }

write_state() {   # key=value pairs; written atomically, readable by everyone
  mkdir -p "$PATCHRACK_STATE_DIR"
  printf '%s\n' "$@" > "$STATE.tmp" && chmod 644 "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"
}

# --- turning it on and off ------------------------------------------------------------
ensure_cron() {   # cron must be installed and running for the schedule to work
  if ! command -v cron >/dev/null 2>&1; then
    say "cron isn't installed. Installing it."
    if ! { apt-get "${APT_OPTS[@]}" -q update >/dev/null && apt-get "${APT_OPTS[@]}" -y -q install cron >/dev/null; }; then
      say "Installing cron failed. Install it with: apt-get install cron"; return 1
    fi
  fi
  if [ -d /run/systemd/system ]; then
    systemctl enable --now cron >/dev/null 2>&1 || true
    systemctl is-active --quiet cron || { say "cron is installed but not running: systemctl start cron"; return 1; }
  else
    service cron status >/dev/null 2>&1 || service cron start >/dev/null 2>&1 || true
  fi
}

enable() {
  local minute="$1" hour="$2" days="$3" mode="$4" autoremove="$5" containers="$6"
  need_root
  [[ "$minute" =~ ^([0-9]|[1-5][0-9])$ ]] || { say "Invalid minute: $minute"; exit 2; }
  [[ "$hour" =~ ^([0-9]|1[0-9]|2[0-3])$ ]] || { say "Invalid hour: $hour"; exit 2; }
  [[ "$days" =~ ^(\*|[0-6](,[0-6]){0,6})$ ]] || { say "Invalid days: $days"; exit 2; }
  [[ "$mode" =~ ^(full|safe)$ ]] || { say "Invalid mode: $mode"; exit 2; }
  [[ "$autoremove" =~ ^[01]$ && "$containers" =~ ^[01]$ ]] || { say "Invalid option"; exit 2; }
  ensure_cron || exit 4
  mkdir -p "$PATCHRACK_CONF_DIR"
  printf '%s\n' "# Managed by Patchrack. Change these settings in Patchrack." \
    "minute=$minute" "hour=$hour" "days=$days" "mode=$mode" "autoremove=$autoremove" \
    "containers=$containers" "reboot=when-needed" "enabled_at=$(date +%s)" > "$CONF.tmp"
  chmod 644 "$CONF.tmp" && mv -f "$CONF.tmp" "$CONF"
  printf '%s\n' "# Managed by Patchrack: automatic updates. Turn them off in Patchrack." \
    "SHELL=/bin/bash" "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    "$minute $hour * * $days root $SELF run >/dev/null 2>&1" > "$PATCHRACK_CRON_FILE.tmp"
  chmod 644 "$PATCHRACK_CRON_FILE.tmp" && mv -f "$PATCHRACK_CRON_FILE.tmp" "$PATCHRACK_CRON_FILE"
  say "Automatic updates are on: $(describe_schedule) ($(date +%Z), the server's time zone)."
  say "Cron job: $PATCHRACK_CRON_FILE"
}

disable() {
  need_root
  rm -f "$PATCHRACK_CRON_FILE" "$CONF"
  say "Automatic updates are off. Removed $PATCHRACK_CRON_FILE and $CONF."
}

describe_schedule() {
  local m h d day_names=(Sun Mon Tue Wed Thu Fri Sat) out="" i
  m="$(conf_value minute)"; h="$(conf_value hour)"; d="$(conf_value days)"
  if [ "$d" = "*" ]; then out="every day"
  else for i in ${d//,/ }; do out="${out:+$out, }${day_names[$i]}"; done; out="on $out"; fi
  printf '%s at %02d:%02d' "$out" "$h" "$m"
}

# --- a run --------------------------------------------------------------------------------
release_upgrade_running() {
  local st
  st="$(/usr/local/sbin/patchrack-release-upgrade status 2>/dev/null)" || return 1
  grep -q '^state=running$' <<<"$st" && ! grep -q '^stale=1$' <<<"$st"
}

compose_cmd() {   # sets COMPOSE to the Compose command, or fails
  if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then COMPOSE=(docker-compose)
  else return 1; fi
}

changed_services() {   # $1: container ids. Prints the services whose image has a newer version pulled
  local id svc ref cur net new out="" changed_ids=""
  for id in $1; do
    read -r svc ref cur net < <(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.service"}} {{.Config.Image}} {{.Image}} {{.HostConfig.NetworkMode}}' "$id")
    new="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null)"
    if [ -n "$new" ] && [ "$new" != "$cur" ]; then out="$out $svc"; changed_ids="$changed_ids $id"; fi
  done
  # a container that uses the network of a recreated one (network_mode: service:...) goes with it
  for id in $1; do
    read -r svc net < <(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.service"}} {{.HostConfig.NetworkMode}}' "$id")
    case "$net" in container:*) [[ " $changed_ids " == *" ${net#container:} "* ]] && out="$out $svc" ;; esac
  done
  tr ' ' '\n' <<<"$out" | sed '/^$/d' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

update_containers() {   # prints the names of the recreated containers on fd 3
  if ! command -v docker >/dev/null 2>&1; then say "Docker isn't installed; nothing to do for containers."; return 0; fi
  if ! docker info >/dev/null 2>&1; then say "Docker isn't running; containers were skipped."; return 0; fi
  if ! compose_cmd; then say "Docker Compose isn't installed; containers were skipped."; return 0; fi
  local failed=0 project wd files services before after f id args changed
  local -a fargs
  while IFS= read -r project; do
    [ -n "$project" ] || continue
    wd="$(docker ps --filter "label=com.docker.compose.project=$project" \
          --format '{{.Label "com.docker.compose.project.working_dir"}}' | head -n1)"
    files="$(docker ps --filter "label=com.docker.compose.project=$project" \
          --format '{{.Label "com.docker.compose.project.config_files"}}' | head -n1)"
    services="$(docker ps --filter "label=com.docker.compose.project=$project" \
          --format '{{.Label "com.docker.compose.service"}}' | sort -u | tr '\n' ' ')"
    fargs=()
    for f in ${files//,/ }; do
      [ -f "$f" ] || { say "== $project: skipped, its Compose file $f no longer exists"; continue 2; }
      fargs+=(-f "$f")
    done
    [ -d "$wd" ] && [ ${#fargs[@]} -gt 0 ] || { say "== $project: skipped, its folder $wd wasn't found"; continue; }
    say "== Containers: Compose project $project ($wd), services: $services"
    args=(-p "$project" --project-directory "$wd" "${fargs[@]}")
    before="$(docker ps -q --no-trunc --filter "label=com.docker.compose.project=$project" | sort)"
    # only the services that are running now: a service you stopped stays stopped
    # shellcheck disable=SC2086
    if ! "${COMPOSE[@]}" "${args[@]}" pull --ignore-pull-failures $services; then
      say "Pulling images for $project failed."; failed=1; continue
    fi
    changed="$(changed_services "$before")"
    if [ -z "$changed" ]; then say "No new images for $project."; continue; fi
    say "New images for: $changed"
    # only the services with a new image are recreated, so unapplied edits to a Compose
    # file don't go live by surprise
    # shellcheck disable=SC2086
    if ! "${COMPOSE[@]}" "${args[@]}" up -d --no-deps $changed; then
      say "Starting the updated containers of $project failed."; failed=1
    fi
    after="$(docker ps -q --no-trunc --filter "label=com.docker.compose.project=$project")"
    for id in $after; do
      grep -qx "$id" <<<"$before" || docker inspect --format '{{.Name}}' "$id" | sed 's#^/##' >&3
    done
  done < <(docker ps --format '{{.Label "com.docker.compose.project"}}' | sort -u)
  say "== Removing unused old images (docker image prune)"
  docker image prune -f || true
  return $failed
}

do_reboot() { sync; systemctl reboot 2>/dev/null || shutdown -r now; }

run() {
  need_root
  [ -f "$CONF" ] || exit 0          # turned off
  mkdir -p "$(dirname "$PATCHRACK_LOCK")"
  exec 9>"$PATCHRACK_LOCK"
  flock -n 9 || exit 0               # a run is already going
  local mode autoremove containers started rc=0 ct_rc=0 packages=0 names="" reboot=0 tmp
  mode="$(conf_value mode)"; autoremove="$(conf_value autoremove)"; containers="$(conf_value containers)"
  [ "$mode" = safe ] && mode=upgrade || mode=dist-upgrade
  started="$(date +%s)"
  if release_upgrade_running; then
    write_state "state=skipped" "reason=A release upgrade is running" "started=$started" "finished=$(date +%s)"
    return 0
  fi
  [ -f "$PATCHRACK_LOG" ] && mv -f "$PATCHRACK_LOG" "$PATCHRACK_LOG.1"
  : >"$PATCHRACK_LOG"; chmod 644 "$PATCHRACK_LOG"
  write_state "state=running" "pid=$$" "started=$started"
  tmp="$(mktemp)"
  {
    say "Patchrack automatic update started $(date)"
    say "== dpkg --configure -a"
    dpkg --configure -a || true
    say "== apt-get update"
    if ! apt-get "${APT_OPTS[@]}" update; then
      say "Reading the package lists failed."; rc=1
    else
      packages="$(apt-get -s -o Debug::NoLocking=1 "$mode" 2>/dev/null | grep -c '^Inst ')"
      say "== apt-get $mode ($packages package(s))"
      if [ "$packages" -gt 0 ] && ! apt-get "${APT_OPTS[@]}" -y "$mode"; then rc=1; fi
      if [ "$rc" -eq 0 ] && [ "$autoremove" = 1 ]; then
        say "== apt-get autoremove"; apt-get "${APT_OPTS[@]}" -y autoremove || true
      fi
    fi
    if [ "$rc" -eq 0 ] && [ "$containers" = 1 ]; then
      update_containers 3>"$tmp" || ct_rc=1
    elif [ "$containers" = 1 ]; then
      say "Containers were skipped because the system update failed."
    fi
    [ -f "$PATCHRACK_REBOOT_FILE" ] && [ "$rc" -eq 0 ] && reboot=1
    say "Finished $(date): system $([ "$rc" -eq 0 ] && echo updated || echo FAILED)$([ "$reboot" = 1 ] && echo ", rebooting now")."
  } >>"$PATCHRACK_LOG" 2>&1
  names="$(sort -u "$tmp" | tr '\n' ' ' | sed 's/ $//')"; rm -f "$tmp"
  local result=success
  [ "$rc" -ne 0 ] && result=failed
  [ "$rc" -eq 0 ] && [ "$ct_rc" -ne 0 ] && result=partial
  write_state "state=$result" "exit=$rc" "packages=$packages" "containers=$names" \
    "container_errors=$ct_rc" "reboot=$reboot" "started=$started" "finished=$(date +%s)"
  if [ "$reboot" = 1 ]; then do_reboot; fi
}

status() {
  if [ -f "$PATCHRACK_CRON_FILE" ] && [ -f "$CONF" ]; then say "enabled=1"; else say "enabled=0"; fi
  [ -f "$CONF" ] && grep -v '^#' "$CONF" | sed 's/^/conf_/'
  if [ -f "$STATE" ]; then cat "$STATE"; else say "state=none"; fi
  say "tz=$(date +%Z) $(date +%z)"
  if command -v cron >/dev/null 2>&1; then say "cron=1"; else say "cron=0"; fi
}

main() {
  case "${1:-}" in
    version) say "patchrack-auto-update $HELPER_VERSION" ;;
    enable)  shift; enable "$@" ;;
    disable) disable ;;
    status)  status ;;
    run)     run ;;
    *)       sed -n '5,19p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
}

# Only run when executed, so the functions can be tested on their own
if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
