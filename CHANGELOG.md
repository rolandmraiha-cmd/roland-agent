# Changelog

## 2.0.0 — Unreleased

M9 release candidate. Host acceptance and restart recovery passed at `bb76263` on
9 October 2026. Final polish integration, release review and the `v2` → `main` merge
remain pending. Training stays off. See the approved M9 acceptance note and known
model-reporting, phone and training-data follow-ups in the project records.

### Added

- Self-hosted Qwen/llama.cpp inference with local-only endpoint validation, constrained
  tool actions, context budgeting and verified model installation.
- Code-enforced, single-use approvals; isolated terminal sandbox; workspace files,
  uploads/downloads and trash/restore.
- Persistent Chromium, browser action policies, private view/control screen and user-only sign-in.
- Persona versions, feedback and opt-in scrubbed training/evaluation on a separate machine;
  imports do not switch models and promotion/rollback require a human request.
- Versioned SQLite migrations, hash-chained audit, online backups and guarded recovery.
- M9 operational/security documentation, fixed-public-prompt CPU benchmark and explicit
  release acceptance evidence table.

### Hardened

- Proxy trust, login/session/CSRF/websocket controls, tool-step limits and scheduled-job overruns.
- Read-only non-root containers, network/firewall boundaries, resource limits and supply-chain pins.
- Release verification now rejects missing/stopped services, loopback publications,
  absent backups, incomplete log rotation and broken model egress probes.
- Restore drill uses only disposable volumes and a private backup copy, without production
  secrets or network; validates schema/audit/integrity and expired sessions before success.
- Dedicated no-hosted-llm CI and live browser/screen/sandbox plus real-container restore checks.
- The screen server disables both x11vnc and LibVNCServer IPv6 listeners; CI checks the
  actual IPv4/IPv6 socket tables before accepting the container.

### Known limitations and deferred work

- Small CPU model speed/quality; 4096-token repo default has not been accepted on the VPS.
- Accepted browser GET/background-request and page-inspection limits (see SECURITY).
- Training-data setup/backup/capture/GPU smoke and phone screen/mobile-data faults wait
  until after 2.0.0 by Roland's decision on 9 October 2026. Training remains off.

## 0.1.0 — v1 baseline

Password-protected FastAPI chat with streamed replies, SQLite memory/facts/jobs, tools,
user-approved scheduled jobs and a daily model-call cap. The v2 specification records
baseline commit `4fb0950` and the preserved behaviour/tests.
