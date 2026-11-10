# Changelog

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
