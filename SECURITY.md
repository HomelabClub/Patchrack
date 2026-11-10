# Security policy

Patchrack holds SSH keys that can install software and reboot servers, so security reports are taken seriously.

## Reporting a problem

Please don't open a public issue for a security problem. Report it privately through GitHub instead: open the **Security** tab of this repository and choose **Report a vulnerability**.

Include what you found, how to reproduce it and which version you used. You'll get an answer within a week. Once a fix is released, you'll be credited in the release notes unless you'd rather not be.

## Supported versions

Only the latest release gets security fixes.

## What's in scope

- The Patchrack web app and its API
- How Patchrack stores and uses credentials (master secret, encrypted values, sessions)
- The server setup command and the sudo rule it installs
- How Patchrack connects to servers and to Docker

## What Patchrack assumes

- Patchrack runs on a trusted network (LAN or VPN), not exposed to the internet.
- Whoever can sign in to Patchrack, or holds its master secret and database, effectively has root access to the managed servers. The sudo rule limits accidents, not a determined attacker.
- The machine that runs Patchrack is kept secure and up to date.

Reports that depend on breaking these assumptions are still welcome. They may lead to better documentation rather than a code change.
