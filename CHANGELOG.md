# Changelog

## 0.1.2 (2026-11-11)

A safe way to update Patchrack.

- New update script for Linux, `scripts/update.sh`. Run it from the new release and it backs up your files and data, copies the new files over (keeping `secrets/`, `backups/` and your own `docker-compose.override.yml`), builds the new version while the current one keeps running, and checks that it starts. If anything fails, it restores the previous version and data automatically.
- The README has a new "Updating Patchrack" section, including steps for Windows, keeping your own settings in `docker-compose.override.yml`, and restoring a backup by hand.
- Each GitHub release now includes a ready-to-download `Patchrack_vX.Y.Z.zip`.

## 0.1.1 (2026-11-11)

Fixes scheduled notifications that weren't arriving.

- Updates that are still waiting now get a reminder. Before, a scheduled check only sent a message for updates it hadn't seen before, so updates you had already found with Check were never reported. Choose under Settings how often to be reminded: once a day (default), after every scheduled check, or never.
- Send test now warns when that service is switched off. Before, the test worked even with the switch off, while real notifications were only sent to services that are switched on.
- New "Recent messages" list in Settings shows every message, the answer from Pushover and Prowl, and why a message wasn't sent.
- Settings shows when the next scheduled check runs.
- A failing scheduled run is now written to the log instead of failing silently.

## 0.1.0 (2026-11-11)

First public release.

- Check Debian and Ubuntu servers for apt updates on a schedule, with security updates highlighted
- Install updates with a per-run choice to not reboot, reboot only if required, or always reboot, and wait for the server to come back
- Live output while jobs run, plus a history of every check and update
- Docker status per server: whether Docker is active, the number of containers and which need an update
- Update all or selected containers, keeping their settings and volumes, with automatic rollback when a new container doesn't start
- Clean up unused Docker images, build cache and networks, showing the space freed
- Pushover and Prowl notifications
- SSH key login with host-key pinning, a setup command with a restricted sudo rule, and AES-256-GCM encryption of stored secrets
