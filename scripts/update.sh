#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Kevin Villarreal (Homelab Club)
#
# Update an existing Patchrack installation to this release.
#
# Run the script from the NEW release you unpacked, and point it at your installation:
#
#   bash patchrack-update/patchrack/scripts/update.sh ~/patchrack
#
# What it does:
#   1. Backs up your current Patchrack files and its data into <installation>/backups
#   2. Copies the new files over. secrets/, backups/ and docker-compose.override.yml are never
#      touched. If you changed docker-compose.yml yourself, your version is kept and the new
#      one is saved next to it as docker-compose.yml.new.
#   3. Builds the new version while the current one keeps running, then restarts Patchrack
#   4. Checks that the new version starts and answers. If it doesn't, the previous files and
#      data are restored and the previous version is started again.
#
# Options:
#   -y, --yes    don't ask for confirmation
#   -h, --help   show this help
#
# Everything runs inside main(), so bash has read the whole script before anything changes.

set -Eeuo pipefail

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nError: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '4,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

version_of() { sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$1/app/main.py"; }

# Names in the release that are copied into the installation (everything except these)
skip_entry() {
  case "$1" in
    secrets|backups|data|.git|docker-compose.override.yml|docker-compose.yml) return 0 ;;
    *) return 1 ;;
  esac
}

restore_files() {
  say "Putting the previous Patchrack files back"
  local entry name
  for entry in "$RELEASE"/* "$RELEASE"/.[!.]*; do
    [ -e "$entry" ] || continue
    name="$(basename "$entry")"
    skip_entry "$name" && continue
    rm -rf "${INSTALL:?}/$name"
  done
  rm -f "$INSTALL/docker-compose.yml.new"
  tar -xzf "$FILES_BAK" -C "$INSTALL"
}

restore_data() {
  say "Putting the previous data back"
  docker run --rm --user 0 -v "$MOUNT_SRC:/data" -v "$BACKUPS:/backup:ro" "$PREV_TAG" \
    sh -c "find /data -mindepth 1 -delete && tar -xzf /backup/$(basename "$DATA_BAK") -C /data"
}

# Start the previous version again from its saved image (no rebuild needed)
start_previous() {
  say "Starting Patchrack $OLD_VERSION again"
  docker tag "$PREV_TAG" "$OLD_REF"
  docker compose up -d
}

# Called for any unexpected error: undo whatever was already changed
on_error() {
  local code=$?
  trap - ERR
  say ""
  say "Something went wrong (line $1, exit code $code)."
  case "$STAGE" in
    files)   restore_files ;;
    stopped) restore_files; start_previous ;;
    started) docker compose stop patchrack || true; restore_files; restore_data; start_previous ;;
  esac
  if [ "$STAGE" != "none" ]; then
    say ""
    say "Patchrack $OLD_VERSION has been restored. Your backups are in $BACKUPS"
  fi
  exit "$code"
}

main() {
  local assume_yes=0 arg
  INSTALL=""
  for arg in "$@"; do
    case "$arg" in
      -y|--yes) assume_yes=1 ;;
      -h|--help) usage; return 0 ;;
      -*) fail "Unknown option: $arg (see --help)" ;;
      *) [ -z "$INSTALL" ] || fail "Give only one installation folder"; INSTALL="$arg" ;;
    esac
  done

  RELEASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  INSTALL="${INSTALL:-$PWD}"
  [ -d "$INSTALL" ] || fail "Folder not found: $INSTALL"
  INSTALL="$(cd "$INSTALL" && pwd)"
  STAGE="none"

  [ -f "$RELEASE/app/main.py" ] && [ -f "$RELEASE/docker-compose.yml" ] \
    || fail "Run this script from an unpacked Patchrack release."
  [ -f "$INSTALL/app/main.py" ] && [ -f "$INSTALL/docker-compose.yml" ] \
    || fail "$INSTALL doesn't look like a Patchrack installation. Give the folder that contains docker-compose.yml, for example: bash $0 ~/patchrack"
  [ "$RELEASE" != "$INSTALL" ] \
    || fail "This is the copy of the script inside your installation. Run the one from the new release you unpacked instead (see the README)."
  command -v docker >/dev/null || fail "docker was not found."
  docker compose version >/dev/null 2>&1 || fail "The Docker Compose plugin was not found."
  docker info >/dev/null 2>&1 || fail "Can't reach Docker. Run the script with sudo, or add your user to the docker group."

  OLD_VERSION="$(version_of "$INSTALL")"
  NEW_VERSION="$(version_of "$RELEASE")"
  [ -n "$OLD_VERSION" ] && [ -n "$NEW_VERSION" ] || fail "Couldn't read the version numbers."

  cd "$INSTALL"
  local cid
  cid="$(docker compose ps -a -q patchrack 2>/dev/null || true)"
  [ -n "$cid" ] || fail "No Patchrack container was found for $INSTALL. Start Patchrack once with: docker compose up -d --build"
  # the image the current version runs; kept under a second name until the update has worked
  OLD_REF="$(docker inspect -f '{{.Config.Image}}' "$cid")"
  PREV_TAG="patchrack-previous:latest"
  local mount_type
  mount_type="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}{{end}}{{end}}' "$cid")"
  case "$mount_type" in
    volume) MOUNT_SRC="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$cid")" ;;
    bind)   MOUNT_SRC="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$cid")" ;;
    *)      fail "Couldn't find where Patchrack keeps its data (/data in the container)." ;;
  esac

  local what newest
  newest="$(printf '%s\n%s\n' "$OLD_VERSION" "$NEW_VERSION" | sort -V | tail -n 1)"
  if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    what="reinstall Patchrack $NEW_VERSION"
  elif [ "$newest" = "$OLD_VERSION" ]; then
    what="go back from Patchrack $OLD_VERSION to the older version $NEW_VERSION"
  else
    what="update Patchrack from $OLD_VERSION to $NEW_VERSION"
  fi
  say "This will $what in $INSTALL."
  say "Patchrack stops for about a minute. Your files and data are backed up first."
  if [ -d "$INSTALL/.git" ]; then
    say "Note: this folder is a git clone. You can also update it with 'git pull'. If you use this script, git will show the new files as changes."
  fi
  if [ "$assume_yes" -eq 0 ]; then
    local answer=""
    if [ -r /dev/tty ]; then read -r -p "Continue? [y/N] " answer </dev/tty || true; fi
    case "$answer" in
      y|Y|yes|Yes|YES) ;;
      *) say "Nothing was changed."; return 0 ;;
    esac
  fi

  local stamp owner
  stamp="$(date +%Y%m%d-%H%M%S)"
  owner="${SUDO_UID:-$(id -u)}:${SUDO_GID:-$(id -g)}"
  BACKUPS="$INSTALL/backups"
  FILES_BAK="$BACKUPS/patchrack-$OLD_VERSION-$stamp-files.tgz"
  DATA_BAK="$BACKUPS/patchrack-$OLD_VERSION-$stamp-data.tgz"
  mkdir -p "$BACKUPS"
  chmod 700 "$BACKUPS"
  trap 'on_error $LINENO' ERR

  step "Backing up the current files"
  docker tag "$OLD_REF" "$PREV_TAG"
  tar -czf "$FILES_BAK" -C "$INSTALL" --exclude=./backups --exclude=./secrets --exclude=./data --exclude=./.git .
  chmod 600 "$FILES_BAK"
  say "Saved $FILES_BAK"

  step "Copying the new files"
  STAGE="files"
  local entry name
  for entry in "$RELEASE"/* "$RELEASE"/.[!.]*; do
    [ -e "$entry" ] || continue
    name="$(basename "$entry")"
    skip_entry "$name" && continue
    rm -rf "${INSTALL:?}/$name"
    cp -a "$entry" "$INSTALL/$name"
  done
  # docker-compose.yml: replace it only if you didn't change it yourself
  local shipped="$BACKUPS/.shipped-docker-compose.yml" compose_kept=0
  if cmp -s "$RELEASE/docker-compose.yml" "$INSTALL/docker-compose.yml"; then
    :
  elif [ -f "$shipped" ] && cmp -s "$shipped" "$INSTALL/docker-compose.yml"; then
    cp "$RELEASE/docker-compose.yml" "$INSTALL/docker-compose.yml"
  else
    cp "$RELEASE/docker-compose.yml" "$INSTALL/docker-compose.yml.new"
    compose_kept=1
  fi

  step "Building Patchrack $NEW_VERSION (the current version keeps running)"
  if ! docker compose build; then
    restore_files
    docker rmi "$PREV_TAG" >/dev/null 2>&1 || true
    STAGE="none"
    fail "The build failed. Nothing else was changed, and Patchrack $OLD_VERSION is still running."
  fi

  step "Stopping Patchrack $OLD_VERSION"
  STAGE="stopped"
  docker compose stop patchrack

  step "Backing up the data"
  docker run --rm --user 0 -v "$MOUNT_SRC:/data:ro" -v "$BACKUPS:/backup" "$PREV_TAG" \
    sh -c "tar -czf /backup/$(basename "$DATA_BAK") -C /data . && chown $owner /backup/$(basename "$DATA_BAK") && chmod 600 /backup/$(basename "$DATA_BAK")"
  say "Saved $DATA_BAK"

  step "Starting Patchrack $NEW_VERSION"
  STAGE="started"
  docker compose up -d

  step "Checking that Patchrack $NEW_VERSION answers"
  local got=""
  for _ in $(seq 1 45); do
    got="$(docker compose exec -T patchrack python -c 'import json,urllib.request;print(json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/session",timeout=3))["version"])' 2>/dev/null || true)"
    [ "$got" = "$NEW_VERSION" ] && break
    sleep 2
  done
  if [ "$got" != "$NEW_VERSION" ]; then
    trap - ERR
    say "Patchrack $NEW_VERSION didn't start correctly. Its last log lines:"
    docker compose logs --tail 25 patchrack || true
    step "Restoring Patchrack $OLD_VERSION"
    docker compose stop patchrack || true
    restore_files
    restore_data
    start_previous
    fail "The update didn't work, so Patchrack $OLD_VERSION is running again with your data as it was. Backups are in $BACKUPS"
  fi
  trap - ERR
  STAGE="done"

  cp "$RELEASE/docker-compose.yml" "$shipped"
  docker rmi "$PREV_TAG" >/dev/null 2>&1 || true
  # keep the five most recent backups
  { ls -1t "$BACKUPS"/patchrack-*-files.tgz 2>/dev/null || true; } | tail -n +6 | while read -r prev; do
    rm -f "$prev" "${prev%-files.tgz}-data.tgz"
  done
  chown -R "$owner" "$BACKUPS" 2>/dev/null || true

  printf '\nDone: Patchrack %s is running.\n' "$NEW_VERSION"
  say "Backups of version $OLD_VERSION are in $BACKUPS"
  if [ "$compose_kept" -eq 1 ]; then
    say ""
    say "Your docker-compose.yml differs from the one in this release, so it was kept."
    say "The release version is saved as docker-compose.yml.new. Compare the two, and move your"
    say "own changes to docker-compose.override.yml so future updates can replace the file."
  fi
  say ""
  say "Tip: remove old Patchrack images with: docker image prune -f"
}

main "$@"
exit $?
