# Security notes for current v2 foundations

## M1 configuration and local model guard

`serve` validates configuration before opening the database or model client. Production
requires secure cookies, an allowed web hostname, and a local model server token for
llama.cpp. It refuses same-user shell execution. Configuring sandbox, browser or screen
services requires their secrets; startup also refuses to enable those services until their
implementations arrive. No sidecar, screen access or deployment is added by this change.

`chat` and `run-jobs` also check model settings before opening resources. Every model URL
must be HTTP, on the configured hostname allowlist, and resolve **only** to loopback,
RFC1918 IPv4 or ULA IPv6 addresses. The HTTP transport resolves again for each request,
connects to the checked literal IP while retaining the Host header, ignores environment
proxies, and refuses redirects. Public, link-local, shared and reserved addresses remain
refused even when explicitly named in `MODEL_ALLOWED_HOSTS`.

Secret settings accept `NAME_FILE`; the file wins over `NAME`, with a value-free warning.
Unreadable files fail closed. Secret fields are excluded from the configuration repr.
`MODEL_API_KEY` is ignored, with a warning. Git and Docker build contexts exclude `secrets/`.
No secret files, credentials, telemetry or new dependencies are added.

This is M1.2, not completion of v2: the old model SDK and native completions protocol remain
temporarily behind the local guard. M2.10 removes that SDK and implements provider selection
and grammar mode. Most newly parsed settings belong to later milestones and do not activate
their features. The existing compose file is a development baseline, not the final isolated
server deployment. M1 audit writing and scheduled backups remain separate work.

## M1 database migrations and storage

Numbered migrations use one SQLite write transaction per version, including the version
update. They execute constant SQL without `executescript`'s implicit commit. Failures roll
back schema and data edits; future unsupported versions are refused. Concurrent initializers
re-read the version after obtaining the write lock. Existing database permissions stay 0600,
with foreign keys, a 10-second busy timeout, WAL for files and synchronous=NORMAL.

When an optional pre-upgrade snapshot is enabled, it uses SQLite's consistent online backup
API. Temporary and final files are private, integrity is checked, and publication is atomic.
This is the migration snapshot hook only; nightly backups and restore remain forthcoming.

New storage methods use bound parameters. Timeline metadata is limited to 16 KiB, and
canonical action arguments to 64 KiB (UTF-8 bytes). Approval decisions compare status,
expiry, argument hash and confirmation in one update; only one concurrent decision wins.
Sensitive action categories require confirmation. These methods do not execute actions;
the full tool gate and approval routes come in M3. The audit table has append-only triggers;
the redacting hash-chain writer is the next M1 task.

File/trash helpers change metadata only and reject absolute or traversal paths; actual
filesystem and symlink safety belong to M5. Sign-in URLs reject embedded credentials,
and screen records store a login-session hash with matching required for refresh/close.
Browser control, sign-in handling, screen authentication and cleanup scheduling arrive later.
No credentials, telemetry, new network requests or application dependencies are added.

## M0 review

M0 retains the v1 login, slowdown, lockout, sessions, CSRF/Origin checks, SSRF filtering,
DNS pinning, path checks, shell default, tool-output envelope, job approval and daily cap.
It adds no telemetry, model calls, outbound destinations or application packages.
Pydantic is already installed transitively; M0 declares its required major version explicitly.

## Proxy trust

Wildcard trust is rejected both by configuration checking and by `ProxyHeaders` construction.
Only listed direct peers may supply forwarded headers. The right-most untrusted hop is used;
invalid IP hops remain untrusted. A chain containing only trusted hops keeps the direct peer
and logs once instead of trusting a client-controlled left-most value.

## Regex audit

Every application Python `re` call was audited for overlapping unbounded quantifiers.
The repository contains two compiled patterns, both in `agent/core.py`:

| Pattern and calls | Result |
| --- | --- |
| `_MARKER = re.compile(r"tool_output", re.IGNORECASE)`; `_MARKER.sub` in `strip_markers` | Fixed literal, no quantifiers; linear scan. Existing output bound is preserved. |
| `_NOT_NAME = re.compile(r"[^A-Za-z0-9_.-]")`; `_NOT_NAME.sub` in `tool_name` | Single-character class, no quantifiers; linear scan with the resulting name capped to 80 characters. |

No nested or adjacent overlapping unbounded quantifiers were found. No regex rewrite was
needed. The new timing regression processes the specified whitespace input in under 0.5 s.

## Limits and persistence

The final permitted model call cannot start another tool round. Jobs that overrun their
interval are rescheduled after completion, using the current saved schedule so a later
user-set schedule is preserved. Deleted jobs are not recreated. Existing cancellation,
approval and daily-cap behavior remains in place.
