# M0 security notes

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
