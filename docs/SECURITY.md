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
server deployment. Audit writing and scheduled backups are described below.

## M1 database migrations and storage

Numbered migrations use one SQLite write transaction per version, including the version
update. They execute constant SQL without `executescript`'s implicit commit. Failures roll
back schema and data edits; future unsupported versions are refused. Concurrent initializers
re-read the version after obtaining the write lock. Existing database permissions stay 0600,
with foreign keys, a 10-second busy timeout, WAL for files and synchronous=NORMAL.

When an optional pre-upgrade snapshot is enabled, it uses SQLite's consistent online backup
API. Temporary and final files are private, integrity is checked, and publication is atomic.
It now also snapshots unversioned v1 before its first upgrade; fresh empty databases are skipped.

New storage methods use bound parameters. Timeline metadata is limited to 16 KiB, and
canonical action arguments to 64 KiB (UTF-8 bytes). Approval decisions compare status,
expiry, argument hash and confirmation in one update; only one concurrent decision wins.
Sensitive action categories require confirmation. These methods do not execute actions;
the full tool gate and approval routes come in M3. The audit table has append-only triggers;
the redacting hash-chain writer records the existing endpoints described below.

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
The current patterns are:

| Pattern and calls | Result |
| --- | --- |
| `_MARKER = re.compile(r"tool_output", re.IGNORECASE)`; `_MARKER.sub` in `strip_markers` | Fixed literal, no quantifiers; linear scan. Existing output bound is preserved. |
| `_NOT_NAME = re.compile(r"[^A-Za-z0-9_.-]")`; `_NOT_NAME.sub` in `tool_name` | Single-character class, no quantifiers; linear scan with the resulting name capped to 80 characters. |
| Audit redaction in `agent/audit.py` | An alternation of `re.escape`d loaded secret literals, sorted longest first. No quantified subexpressions or interpretation of secret text as a regex. |
| Backup filename patterns and HH:MM check in `agent/backup.py` | Fixed-length digit groups and escaped extensions; only generated filename shapes are accepted for retention. |

No nested or adjacent overlapping unbounded quantifiers were found. No regex rewrite was
needed. The new timing regression processes the specified whitespace input in under 0.5 s.

## Limits and persistence

The final permitted model call cannot start another tool round. Jobs that overrun their
interval are rescheduled after completion, using the current saved schedule so a later
user-set schedule is preserved. Deleted jobs are not recreated. Existing cancellation,
approval and daily-cap behavior remains in place.

### Audit records (M1.4)

Audit details and labels redact the loaded Config secret values using literal matches, before
truncation or serialization. Empty values are ignored. Extra secrets loaded by future services
must be passed to their audit writer; encoded/changed spellings of a secret are not matched.
Login passwords and raw session/CSRF tokens must never be supplied to the writer at all.

SQLite triggers block audit updates/deletes. BEGIN IMMEDIATE serializes writers across core
and CLI connections; nested savepoints let audit entries commit with their state change.
SHA-256 chaining detects edited rows and missing interior rows. It is not a signed log:
someone who controls the database and can remove triggers can recompute the chain, truncate
its tail, or replace the whole database. There is no external anchor or off-site copy.
Verification reports the first bad row without printing stored details. The app does not
prune audit entries. This stage records existing account/job/fact endpoints; later milestones
add action gate and browser events.

### Local backup and restore (M1.5–M1.6)

Only the core creates backups; no upload or external service is used. SQLite snapshots are
consistent under concurrent writes and published from private temporary files. Workspace
archives use open directory descriptors and O_NOFOLLOW, so a swapped symlink cannot cause
an outside file to be copied. Symlinks remain links, hard links are refused, and device/FIFO/
socket contents are not opened. The destination is refused if it lies inside the workspace.

Restore requires an exclusive Linux file lock held throughout verification and installation.
Serve/chat/jobs/backup hold shared access, allowing their existing concurrency while blocking
restore. Locks remain named on disk after release to avoid inode-replacement races. This
protects cooperating application commands; an unrelated SQLite editor ignores these locks.
The command checks integrity, foreign keys, supported migrations and audit hashes before
moving current files. It preserves old database sidecars and clears restored login sessions.
Caught rename failures roll back, but a host crash during multiple file renames can leave
pre-restore files needing manual recovery. Keep those files until restore is confirmed.

Backups contain private conversations and workspace data; the audit redactor does not scrub
SQLite or archive contents. They stay local, unencrypted, mode 0600. Retention acts only on
the generated regular snapshot filenames, preserving migration backups and symlinks. The
read-only audit chain check, local HTTP healthcheck and token generator require no model
request. No network request is added except healthcheck to the configured local bind address.
