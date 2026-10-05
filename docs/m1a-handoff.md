# M1a handoff: CI and lint

Copy the **Instructions for the next environment** section below into a new Codex task that was started from an authenticated Codex cloud environment connected to `rolandmraiha-cmd/roland-agent`.

## Instructions for the next environment

You are continuing M1a (M1.1: CI and lint) for Roland Agent. Treat this document as a handoff, not as proof that the work has been completed. Work in the authenticated repository and open a real GitHub pull request into `v2` when all requirements below pass.

### Non-negotiable starting point

1. Run:

   ```bash
   git fetch origin v2
   git checkout -b v2-m1a-ci-lint origin/v2
   git rev-parse --short HEAD
   pytest -q
   node --test tests/frontend/chat.test.cjs
   ```

2. The base must be `v2` at `76a0830` or later. Before changing anything, confirm that the baseline contains **99 Python tests** and **15 JavaScript tests**. M0, the fact caps, and the spec update are already merged there.
3. Do not start from `main`, do not push to `main`, and do not merge the PR. Roland decides merges.
4. Read the current `docs/v2-spec.md`, especially §§3, 4.4, and 13.4, before editing. Follow its PR template exactly.

### Why this redo is necessary

A prior attempt was made in an unauthenticated checkout of `main` at `4fb0950`. That checkout had only 61 Python and 13 JavaScript tests. The local attempt ended at commit `7a59309` (an earlier report called the same work `e9d7f1c`), but it was never pushed and no real GitHub PR was opened. The available `make_pr` helper only recorded proposed metadata; it did not return a GitHub URL. Do **not** treat that attempt as correctly based work. Reimplement or carefully port only the intended M1a changes onto the real `v2` base.

### Required M1a scope

Implement M1.1 only:

- Add `.github/workflows/ci.yml`.
- Add `requirements-dev.in` and a hash-checked `requirements-dev.lock`.
- Add the exact Ruff configuration below.
- Add Makefile targets `lint`, `test`, `test-integration` (a stub), and `fmt-check`.
- Make only the minimal mechanical source/test edits required for `ruff check` to pass.
- Do not implement M1b or M1c in this PR.

### Exact Ruff configuration (§13.4)

Use this configuration exactly:

```toml
[tool.ruff]
target-version = "py311"
line-length = 110

[tool.ruff.lint]
select = ["E", "F", "W", "B", "S", "UP", "I"]
ignore = ["E501", "S101", "S603", "S607"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S"]
"agent/local_shell.py" = ["S602", "S604"]
```

Do **not** run `ruff format` across the repository. Section 13.4 requires v1 code to pass after mechanical fixes only, and a mass reformat would bury review. Keep the `fmt-check` Make target if the current spec still requires it, but do not add `fmt-check` to CI yet.

### Exact CI requirements (§13.4)

The workflow must have:

- Triggers: `pull_request`, plus `push` to `v2`, `v2-*`, and `main`.
- Top-level `permissions: contents: read`.
- Concurrency group `ci-${{ github.ref }}` with `cancel-in-progress: true`.
- Exactly the M1a jobs `lint` and `unit`, both on `ubuntu-24.04`.
- GitHub Actions pinned by full commit SHA, not tags.
- Hash-verified installation from the runtime and development lock files.
- The Python and frontend unit suites in the `unit` job.
- No formatting job/check for now.

Jobs that depend on later milestones may be omitted, but the PR must list these follow-ups explicitly:

- compose policy
- `build`
- `integration`
- `no-hosted-llm`
- `training-dry-run`

### Dependencies and Make targets

Development requirements should include pytest, pytest-asyncio, and Ruff, with resolved packages and hashes in `requirements-dev.lock`. State why each direct development dependency is needed. Do not add unrelated dependencies.

The Makefile must expose:

- `make lint`: run Ruff checks.
- `make test`: run all Python tests and `node --test tests/frontend/chat.test.cjs`.
- `make test-integration`: a clearly identified placeholder for the later integration milestone.
- `make fmt-check`: available locally but not invoked by M1a CI.

### Existing tests (§4.4)

All existing tests must continue to pass. If Ruff requires edits in existing test files, make only mechanical lint fixes. In the PR description, list **every edited line in every existing test file**, with:

- file path and line number;
- the exact change;
- the Ruff diagnostic that required it; and
- confirmation that no assertion, parameter, skip, or behavior changed.

The previous, wrong-base attempt encountered examples such as import-group blank lines, one unused import, and one unused loop-variable rename. Do not blindly reproduce those line numbers or edits: run Ruff against current `v2` and list the actual edits on that base.

### Timing-sensitive test

Run `test_wrong_guesses_dont_hold_up_roland` without changing or skipping it. In the prior sandbox, Argon2 verification took about 1.15–1.19 seconds and caused its unchanged one-second timing assertion to fail. If that happens again, report the exact command and timing in the PR, but do not weaken, alter, or skip the test.

### Required validation

Before committing and opening the PR, run at least:

```bash
pytest -q
node --test tests/frontend/chat.test.cjs
make lint
make test-integration
git diff --check
```

Also verify the lock installation in a clean environment using hash checking. The expected complete baseline is 99 passing Python tests and 15 passing JavaScript tests unless the unchanged Argon2 timing test is demonstrably slow in the sandbox.

Every commit must leave `pytest` and `node --test tests/frontend/chat.test.cjs` green, subject only to the documented unchanged Argon2 sandbox limitation.

### Git and pull-request procedure

1. Review the complete diff and confirm there was no mass formatting.
2. Commit on `v2-m1a-ci-lint`.
3. Push the branch to `origin`.
4. Open a real GitHub PR **into `v2`**, never `main`.
5. Use the §3 PR template from the current spec and tick every acceptance criterion only after verification.
6. Include the complete existing-test edit list described above.
7. Include the omitted later-milestone CI jobs as follow-ups.
8. Mention any Argon2 timing limitation without modifying the test.
9. Ask Claude to review the PR (for example, add `@claude Please review this M1a implementation.` where appropriate for the repository's workflow).
10. Return the actual GitHub PR URL. If workflow-file push permissions are unavailable, say so explicitly rather than silently omitting CI.

Claude reviews each PR on GitHub. Address findings with new commits or explain disagreements in replies. Stop after 10 review rounds and leave the final merge decision to Roland.

### Relevant historical context

The original M1 request allows up to three PRs:

1. **M1a:** CI and lint (this task).
2. **M1b:** configuration and persistence.
3. **M1c:** audit, backups, and CLI.

Changes already merged into `v2` must be preserved:

- Fact limits: `Memory.remember(text, limit=None)` checks the count inside the INSERT.
- `agent/tools.py` has `prompt_facts()` with `MAX_FACTS_PROMPT_CHARS = 1500`.
- `facts()` still returns `(id, text)`.
- The current model is Qwen3-4B-Instruct-2507.
- Current defaults include `MODEL_CTX=6144`, `MODEL_MEM_LIMIT=3840m`, and `BROWSER_MAX_TABS=2`.

M1a should not alter those behaviors. They are noted so that a wrong-base patch does not accidentally overwrite newer `v2` work.

### Authentication setup for the human operator

If the new Codex task still lacks repository access, connect GitHub at:

- https://chatgpt.com/codex/settings/environments

Create or update a Codex cloud environment, choose **Connect GitHub**, authorize `rolandmraiha-cmd/roland-agent`, publish the environment, and start a new task from it. Connecting GitHub does not retroactively add credentials to an already-running container. Do not paste personal access tokens into chat or commit them to the repository.
