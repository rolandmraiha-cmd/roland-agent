# roland-agent

Roland's private, always-on AI agent: password-protected web chat, local open-weight model only, tools, and approved background jobs. Target host is a Contabo VPS — **not deployed there yet**.

**Branch:** `v2` (tip includes #24 providers; host deploy scripts land via `v2-m2-deploy`).

## What works now

Web chat, SQLite memory/jobs/audit/backups, public web fetch and workspace file tools, Docker edge (**Caddy** → **core** → isolated **llama.cpp**), verified model installer. Shell, browser, screen, and training stay **off**.

## What does not

Local provider/grammar/context rewrite (M2.10–13; see open PR if any), Contabo model measurements, terminal sandbox, browser, screen/sign-in. Host deploy scripts exist (`make preflight` / `secrets` / `firewall` / `deploy` / `ship` / `verify`) but require `APPLY=1` and are **not** run against Contabo yet. `make model-bench` is deferred.

## Commands that exist

```bash
# Dev (unit tests need no model)
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
make lint && make test
python -m agent hash-password   # then put hash in .env / secret file
python -m agent                 # localhost:8080 when MODEL_BASE_URL is set

# Docker / model
make build && make compose-config && make preflight-edge
make test-integration           # needs Docker; disposable fixtures
make model-fetch MODEL=qwen3-4b-q4km
make model-install FILE=/abs/path.gguf ID=qwen3-4b-q4km

# Host deploy baseline (destructive steps need APPLY=1; no Contabo/DNS from here)
make secrets                    # create missing secrets/* (never prints values)
make hash-password              # writes secrets/agent_password_hash
make preflight                  # full read-only host checks
# APPLY=1 make firewall         # install iptables/nft rules + systemd unit
# APPLY=1 make deploy           # preflight → build → up → smoke
# APPLY=1 make ship HOST=deploy@host REF=v2
make verify                     # PASS/FAIL checklist (skips what is unavailable)
```

Also: `python -m agent` subcommands `chat`, `migrate --check`, `audit-verify`, `backup-now`, `restore`, `healthcheck`, `gen-token`.

## Full status and next steps

See **[docs/AGENT.md](docs/AGENT.md)** — living master doc (done vs not done, runbook, security, Contabo checklist, process).

Long target design remains [`docs/v2-spec.md`](docs/v2-spec.md); do not treat the spec as an implementation inventory.
