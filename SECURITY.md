# Security Policy

## Supported versions

Security fixes are applied to the latest release on the `main` branch.

## Reporting a vulnerability

Please do **not** open a public issue. Report privately by emailing the maintainers
(GitHub security advisories or repository discussions).

Please include:

- The affected version and OS
- Steps to reproduce
- A description of the impact

## Safe defaults

AnyHardware AI is designed around conservative, local-only defaults:

- The inference server binds to `127.0.0.1` and uses a random per-process API key.
- `run_python` runs in an isolated `python -I` subprocess (no user site-packages) with a 30s timeout.
- File tools reject any path that escapes the configured workspace.
- No model data, chat history, or logs leave the machine by default.
