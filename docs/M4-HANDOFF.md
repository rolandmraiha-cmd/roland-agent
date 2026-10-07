# M4 handoff (PR #31 → `v2`)

## Landed on `v2-m4-sandbox`

- `sandboxd/` with peer+Bearer `/v1/exec`, output cap, timeout, concurrency 429, **container-gated** leftover reap (`/.dockerenv` or `SANDBOX_REAP_ALL`)
- `agent/{local_shell,sandbox_client,tools,core,policy_shell}` — ShellBackend + sandbox wiring, audit, approval cards
- `docker/sandbox/Dockerfile`, compose `sandbox` service, core `ALLOW_SHELL` / `SHELL_BACKEND=sandbox` / `sandbox_ctl` / secret
- `.env.example` shell defaults; `deploy/preflight_edge.py` validates caddy+core+model+sandbox and `sandbox_api_token`
- Unit + edge fixture create/chown `secrets/sandbox_api_token`; integration helpers (`isolation.sh`, `docker-compose.test.yml`, `test_sandbox_live.py`)

Tip: `git log origin/v2-m4-sandbox -1`.

## Safety

Never set `SANDBOX_REAP_ALL` on the host. Unit reap tests mock; real reap only in the dedicated sandbox container / compose integration.

## Remaining (Shipper / Contabo)

1. Contabo smoke after merge: `uname` without approval; `fetch_url` then shell gated; `make verify` / isolation.
2. Optional `make test-sandbox` (Docker + secrets). Do **not** raise `MODEL_MEM_LIMIT` / Contabo `MODEL_CTX=3072` without Roland.
