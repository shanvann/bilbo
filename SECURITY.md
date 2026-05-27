# Security Policy

## Reporting a vulnerability

If you find a security vulnerability in BILBO, please report it privately:

- **GitHub Security Advisories** (preferred): https://github.com/shanvann/bilbo/security/advisories/new
- **Email**: shanitpv@gmail.com

Please do not open public GitHub issues for security problems.

This is a personal project with no SLA and no bug bounty. I'll do my best
to respond within a few days. If a vulnerability requires immediate action
and I haven't responded, treat your deployment as exposed and consider
disclosing publicly so other users can defend themselves.

## Scope

In scope:

- The BILBO codebase (`src/bilbo/`, `dashboard/`, `airgradient-logger/`,
  `deploy/`, scripts in `src/bilbo/scripts/`).
- The Docker Compose deployment as documented in the README.

Out of scope:

- Upstream dependencies (PyTorch, OpenCV, Flask, etc.) — report those to
  the respective projects.
- Misconfigurations in your network, your camera, or your Cloudflare
  Access setup.
- Issues that require physical access to the machine.

## Inherent risks of self-hosting

BILBO captures and processes video of a sleeping baby. Self-hosting a
camera-based monitor carries risks that exist regardless of this code:

- Your RTSP camera credentials are only as secure as your camera firmware
  and your LAN. Use a long, unique password and rotate it periodically.
- The dashboard exposes live video and historical frames. **Never expose
  it directly to the public internet.** Use Cloudflare Access, a VPN
  (Tailscale, WireGuard), or keep it LAN-only.
- The `.env` file holds high-value secrets (OpenAI API key, Telegram bot
  token, Cloudflare Access service token, RTSP credentials). Treat it as
  if it were a password file: `chmod 600`, do not commit, do not paste
  into shared chats or screen shares.

## Supported versions

Only the latest commit on `main` is supported. There is no LTS branch.
