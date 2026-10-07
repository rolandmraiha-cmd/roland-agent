# Security notes (roland-agent)

Full write-up lands in M9. This file records accepted limits as they ship.

Update this file in the same PR, before squash-merge, whenever an accepted limit changes. Do not leave that note only in chat.

## Sandbox (M4)

- The shell classifier (`agent/policy_shell.py`) is a **usability filter**, not the security boundary. The boundary is the isolated `sandbox` container (§6.3), peer + Bearer auth on sandboxd, DOCKER-USER firewall rules, and gating every command once a run is tainted (or when `SHELL_APPROVAL=always`).
- **Known, accepted limit:** commands run as the same uid as sandboxd (1000), so a command can read the sandbox token file (`/run/secrets/sandbox_api_token`) or kill sandboxd. The token only authorises sandbox command execution (which the sandbox can already do). Killing sandboxd only causes a restart; healthcheck + `restart: unless-stopped` recover. Nothing in the sandbox reaches the core, its database, or core secrets.
- **Implementation note:** sandboxd's leftover-process reap (`/proc` SIGKILL except PID 1 and self) must run **only inside the container** (gate on `/.dockerenv` or `SANDBOX_REAP_ALL`). Running it on a host during in-process unit tests will kill the machine.
