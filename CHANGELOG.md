# Changelog

## 2.0.0 — 2026-10-10

Accepted for Roland's single-user Contabo deployment. Full M9 host acceptance passed
at `bb76263`, browser/screen/tool smoke at `7b3de9d`, and the final streaming-fix deploy
at `7ce7c2c`: verify 12/0/2, schema 3 current, valid 604-row audit, all seven exact-head
CI jobs passed. The complete 768 MiB download passed full size/SHA-256/CRC validation;
its 180-second memory watch recorded core sampled peak 87.99/640 MiB, minimum host available
3817 MiB, and zero OOM kills or restarts. Release PR #66 records final documentation-head
checks/review and the authorized main merge. Extra feature branches and cleanup workflows
are removed. NEXT preserves the approved implementation plan as history and retains the
agreed model-reporting, phone, training-data and lower-priority follow-ups. Training stays off.

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
  file and close it on completion, cancellation or disconnect. Mutable files use chunked
  HTTP framing, avoiding stale-length errors during truncation. File-info and append hashes
  no longer read the whole file into core memory. A 768 MiB sparse-file regression runs
  below the production memory cap. The fix is deployed and the complete 768 MiB download and memory watch passed.

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
