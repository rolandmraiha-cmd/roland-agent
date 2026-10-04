# M0 runbook

This document covers the v1 fixes on `v2`. The later container and deployment milestones
in `v2-spec.md` have not been implemented by M0.

From the repository root, with Python 3.12 and Node.js available:

```sh
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation --no-index -e .
.venv/bin/python -m pip install 'pytest>=8' 'pytest-asyncio>=0.23'
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

The M0 lint check covers added and changed lines using the rules in spec §13.4.
The repository-wide Ruff configuration and mechanical baseline fixes belong to M1.
There are 19 pre-existing diagnostics under those rules, including eight in existing tests;
M0 leaves them and the existing tests unchanged except the authorised forwarded-for assertion.
