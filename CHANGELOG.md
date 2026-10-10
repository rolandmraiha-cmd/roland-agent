# Changelog

## 2.0.0 — Unreleased

Roland authorized the single-user release with model-reporting, phone and training-data
follow-ups retained. Full host acceptance and restart recovery passed at `bb76263` on
9 October. The final runtime candidate `7b3de9d` passed all seven CI jobs and the 10 October
Contabo deploy: verify 12 pass / 0 fail, schema 3 current, valid 591-row audit, and focused
browser/screen/tool smoke. Final polish #65 and build fix #67 are merged; their extra
branches and temporary cleanup workflows are removed. Release PR #66 records the
review and checks on this documentation-only finalization before the `v2` → `main` merge.
Final automated review confirmed a large-file download/hash memory blocker. The focused
streaming fix needs CI and a deployed smoke before the authorized main merge; the release
date remains pending. Training stays off. The acceptance note preserves both dated
checkpoints; NEXT retains follow-ups and the completed implementation plan as history.

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

- Workspace downloads stream bounded chunks from an openat/O_NOFOLLOW-validated regular
  file and close it on completion, cancellation or disconnect. File-info and append hashes
  no longer read the whole file into core memory. A 768 MiB sparse-file regression runs
  below the production memory cap. This final fix is pending deployed acceptance.

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

- Optional Ollama streamed-schema fallback (#68) and moved-directory descendant metadata
  (#69) are tracked after 2.0.0; the production model uses llama.cpp.

- Model reporting can claim actions or recovery without supporting tool results, especially
  in longer chats. Fresh explicit tools executed, but the final smoke labelled paragraph
  text as a heading. Accurate action/content reporting remains a follow-up; PR #65 does not fix it.
- Small CPU model speed/quality; 4096-token repo default has not been accepted on the VPS.
- Accepted browser GET/background-request and page-inspection limits (see SECURITY).
- Training-data setup/backup/capture/GPU smoke and phone screen/mobile-data faults wait
  until after 2.0.0 by Roland's decision on 9 October 2026. Training remains off.

## 0.1.0 — v1 baseline

Password-protected FastAPI chat with streamed replies, SQLite memory/facts/jobs, tools,
user-approved scheduled jobs and a daily model-call cap. The v2 specification records
baseline commit `4fb0950` and the preserved behaviour/tests.
