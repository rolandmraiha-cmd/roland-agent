# M4 handoff (incomplete — do not merge)

## What landed on `v2-m4-sandbox` via API (after box death)

- `sandboxd/` package (`__init__`, `__main__`, `paths`, `server`) with peer+Bearer `/v1/exec`, output cap, timeout, concurrency 429, **container-gated** `/proc` reap (`/.dockerenv` or `SANDBOX_REAP_ALL`).
- `agent/local_shell.py`, `agent/sandbox_client.py`
- `docker/sandbox/Dockerfile`
- `agent/__main__.py`: sandbox stub removed; local-backend warning only
- `docs/SECURITY.md` sandbox accepted limit

Tip at handoff time: check `git log origin/v2-m4-sandbox -1`.

## What was written on the box but not safely pushed (before reap killed the shell)

These may still exist under `/workspace/roland-agent` once the box is recovered — **re-read and fix reap before running `tests/test_sandboxd.py`**:

- Updated `agent/tools.py` (ShellBackend, timeout_s/reason schema, shell_exec audit)
- Updated `agent/core.py` (wire SandboxShell when backend=sandbox)
- Updated `agent/policy_shell.py` (approval card details)
- `docker-compose.yml` sandbox service + core ALLOW_SHELL/sandbox_ctl/secret
- `.env.example` ALLOW_SHELL=true SHELL_BACKEND=sandbox
- `deploy/preflight_edge.py` + `tests/test_compose_policy.py` updates
- `tests/test_sandboxd.py`, `tests/test_policy_shell.py`
- `tests/integration/isolation.sh`, `docker-compose.test.yml`, `tests/integration/test_sandbox_live.py`
- Makefile `test-integration` notes
- pyproject sandboxd package + integration marker

## Critical bug that killed the box

First `_reap_leftovers` implementation SIGKILL'd every `/proc` PID except 1 and self **on the host** during in-process A4.1 tests. Fixed version is on the branch (container-only). Local box copy may still have the dangerous version — replace from git before pytest.

## Remaining to finish M4

1. Recover box or re-clone branch; sync local files with remote; apply missing tools/core/compose/tests from this note / MEMORY.
2. Wire tools + core; flip compose/.env.example; policy card details; compose policy mem budget 5600m.
3. A4.1 leftover test: mock reap on host OR skipif not docker; real cover in integration.
4. `ruff check` + full pytest; optional `make test-integration` sandbox stack.
5. Open non-draft PR → v2 with A4.1–A4.5 checklist + Contabo smoke (uname no approval; after fetch_url shell gated).
6. Do **not** merge/deploy Contabo from Code Builder.
