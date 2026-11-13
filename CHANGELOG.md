# Changelog

## 0.2.1 (2026-11-13)

Clearer setup commands for servers that sign in with their own user.

- Each server now has a **Setup command** button that gives the command for the user Patchrack signs in with on that server. Before, the Docker notice pointed to the command in Settings, which is always for the user `patchrack`, so servers using another user stayed without sudo and Docker access.
- Settings has a field for the SSH user, so you can make the setup command for any user.
- "sudo refused" and "Upgrade helper missing" messages now name the user and point to the server's setup command.
- When a check fails, the Release upgrade section says it shows the result of the last check that worked.

## 0.2.0 (2026-11-13)

Groups and release upgrades.

- Sort servers into groups. Create, rename and delete groups under **Groups…**, or pick a group in a server's settings. Each group gets its own section with a summary, Check group and Update group…, and can be folded away. The Update… panel lets you select a whole group.
- Patchrack now spots new Ubuntu and Debian releases, such as Ubuntu 26.04 LTS and Debian 13 (trixie), and can upgrade a server when you choose to. Before starting it checks for pending updates, a waiting reboot, free disk space and the upgrade script, and asks you to confirm a backup and type the server's name. The upgrade runs on the server in the background, survives a Patchrack restart, and can be followed live.
- New notification when a new release is found, which you can turn off under Settings.
- The setup command now installs `/usr/local/sbin/patchrack-release-upgrade` and allows it in sudoers. Run the setup command again on servers you added before 0.2.0.
- Proxmox VE hosts and Raspberry Pi OS aren't offered release upgrades.
- README: the ready-made image name is now lowercase, `ghcr.io/homelabclub/patchrack`, as GitHub requires.

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
