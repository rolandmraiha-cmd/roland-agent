# v2 foundations runbook

This document covers the v1 fixes, CI and M1.2 configuration on `v2`. The final Linux
deployment, isolated shell, browser and live screen milestones remain unimplemented.

From the repository root, with Python 3.12 and Node.js available:

```sh
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation --no-index -e .
.venv/bin/python -m pytest -q
node --test tests/frontend/chat.test.cjs
```

Tests use a fake model; no model server or model API request is needed.

Configure `FORWARDED_ALLOW_IPS` with your reverse proxy's explicit IP or CIDR.
Starting `python -m agent` with `FORWARDED_ALLOW_IPS=*` must exit non-zero with:

```text
FORWARDED_ALLOW_IPS='*' is not allowed; list the proxy IP
```

`tests/test_m0_cli.py` exercises this startup path with a temporary data directory.
Do not use an existing production database to test a rejected configuration.

Run `.venv/bin/ruff check agent tests` for the repository-wide lint check.

## Configuration stage (M1.2)

`.env.example` lists v2 settings and their code defaults. Many are parsed foundations for
later milestones; configuring them does not create those services. Do not deploy this
development baseline as the final 24/7 server setup.

For development with a local llama.cpp server supporting native completions:

```dotenv
MODEL_PROVIDER=llamacpp
MODEL_BASE_URL=http://127.0.0.1:8080
MODEL_TOOL_MODE=native
DATA_DIR=./data
ALLOW_SHELL=false
BROWSER_ENABLED=false
SCREEN_ENABLED=false
```

The existing development compose file still uses its `ollama` service. To use that file,
override `MODEL_PROVIDER=ollama`, `MODEL_BASE_URL=http://ollama:11434/v1`, set
`MODEL_ALLOWED_HOSTS=ollama,127.0.0.1,localhost,::1`, and set `MODEL_NAME` to a locally
installed model tag. Full provider implementations replace this compatibility path in M2.
The static `10.77.6.60` code default belongs to the future v2 deployment, not this old compose.

Make the login hash with `python -m agent hash-password`. Put the printed hash in a private
file and set `AGENT_PASSWORD_HASH_FILE` to its path, or set `AGENT_PASSWORD_HASH` directly
for local development. Apply the same pattern to `MODEL_SERVER_TOKEN` when the model server
requires authentication. Files win over environment values; failed file reads stop startup.
No secret value belongs in the repository. Secret files are not automatically mounted by
the old compose file; host file paths work only when they also exist inside the container.

`AGENT_ENV=production` requires `COOKIE_SECURE=true`, `AGENT_HOST` or `ALLOWED_HOSTS`,
and `MODEL_SERVER_TOKEN` for llama.cpp. It refuses `ALLOW_SHELL=true` with the local backend.
The configured model must stay local in every environment, including terminal chat and jobs.
Setting an unavailable browser, screen, or sandbox feature causes a clear startup refusal;
those flags should remain off until their milestones are implemented.
