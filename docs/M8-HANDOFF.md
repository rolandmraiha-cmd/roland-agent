# M8 handoff for the next AI — 9 October 2026

Clearing feedback removes the saved vote, but the page still marks the old thumb as pressed until it is refreshed. Roland requested this written handoff only; implementation is for the AI he assigns next. This draft adds only the handoff document; implementation is not included.

### Baseline and deployment evidence

- M8 implementation PR #56 is merged. The verified remote `v2` tip is `e208bbd53e42ce4d6d88110e14060dfbed6f7e0b`. This draft carries the handoff notes. Recheck `origin/v2` before starting a fix and target `v2`.
- Roland's supplied Contabo deployment log records that same commit, a successful rebuild/deploy, and `make verify`: **13 pass / 0 fail / 1 skip**, with **3393 MiB available** at that measurement. The skip is the human login/chat/approval checklist. This is evidence from his supplied log, not a new SSH check.
- The earlier deployment preflight failure came from the existing `.env` retaining a nonempty `TRAINER_URL` while the training profile was off. Roland corrected that configuration and the next deploy passed. Do not treat that resolved configuration mismatch as an unfixed app bug.
- Live Settings currently shows persona **Version 6**, blank Standing instructions, **815 / 1500 tokens**, both capture and weekly self-improvement unchecked, and serving model **`qwen3-4b-q4km-base`**, previous model **none**. The temporary `M8TEST` instruction has already been removed.

### Confirmed bug to fix: Clear vote leaves stale pressed state

Reproduced on the live agent:

1. Open the test chat `What is 2 + 2?` and select either thumb. For the negative case, enter a harmless correction and save it.
2. Refresh and reopen the chat: the negative vote and correction are correctly retained.
3. Click **Clear vote**. The page says **Vote cleared** and hides the correction editor, but the old thumb still has `aria-pressed="true"`.
4. Refresh and reopen: neither thumb is selected. The stored vote was removed; the failure is the immediate UI state.

Expected: after a successful clear, both thumbs immediately report an unpressed state without refreshing. A failed clear must retain the saved selection and display its error.

Source pointer: [`agent/web/static/settings.js`, lines 50–54](https://github.com/rolandmraiha-cmd/roland-agent/blob/e208bbd53e42ce4d6d88110e14060dfbed6f7e0b/agent/web/static/settings.js#L50-L54). The DELETE handler updates the status and hides the editor, but does not reset the two `aria-pressed` attributes. The successful POST path updates them at line 33.

For the next implementation PR:

- [ ] Fix the immediate state after a successful clear.
- [ ] Add focused frontend regression coverage in [`tests/frontend/settings.test.cjs`](https://github.com/rolandmraiha-cmd/roland-agent/blob/e208bbd53e42ce4d6d88110e14060dfbed6f7e0b/tests/frontend/settings.test.cjs): clear after both positive and negative ratings, and failure preserving the prior state.
- [ ] Run the relevant frontend checks and required CI; repeat the live clear test after Roland deploys the fix.

### Tests already completed

- **Thumbs-up:** saves and survives refresh.
- **Thumbs-down:** saves and survives refresh.
- **Text correction:** saves and is populated again when reopening the negative feedback editor after refresh.
- **Clear vote:** persistence works; immediate pressed-state display fails as described above.
- Throughout those tests, the chat showed **Capture is off / Not captured**. The original thumbs-up was restored at the end. No capture setting or serving model was changed.
- Persona edit/preview/save worked. The original wording `Start ordinary answers with M8TEST.` was ignored in a fresh arithmetic chat. Clearer wording with an example, saved as Version 3, produced the real live answer **M8TEST 4**. A separate local mock-transport probe verified that the saved instruction reaches the model request and that the reply prefix is preserved. That probe was not real model inference; no application defect causing the original ignored instruction was established.

### Documentation follow-up and remaining M8 checks

The repository status is now stale: [`README.md`](https://github.com/rolandmraiha-cmd/roland-agent/blob/e208bbd53e42ce4d6d88110e14060dfbed6f7e0b/README.md#L6) and [`docs/AGENT.md`](https://github.com/rolandmraiha-cmd/roland-agent/blob/e208bbd53e42ce4d6d88110e14060dfbed6f7e0b/docs/AGENT.md#L3) still name the older deployed baseline and say M8 deployment is next. Update README, AGENT and NEXT in the eventual implementation PR with the actual evidence and unresolved checks; do not mark M8 fully accepted yet.

- [ ] Finish persona restore and fixed-safety-block UI acceptance. The present Version 6/blank instructions is verified, but the exact restore interaction was not independently observed.
- [ ] Explicitly confirm schema version 3 (`docker compose exec -T core python -m agent migrate --check`) and the base/current model (`make model-list`). The model UI already shows the base serving model.
- [ ] Verify that feedback while capture is off creates no `training_examples` row. The UI's **Not captured** label was checked; the live database was not queried.
- [ ] Repeat normal chat, chat refresh, browser screen and human sign-in after the M8 deployment. Earlier M7 acceptance is recorded; a fresh post-M8 human sign-in test is still pending.
- [ ] If Roland chooses the capture/export smoke, first agree the training-data backup policy, then use a disposable test chat with fake data and check scrubber counts/removal of a planted fake secret. Capture/export was not tested live. A rented GPU run is optional.

Keep capture, weekly training, `TRAINER_URL` and the training Compose profile off until Roland chooses otherwise. Roland runs the host commands himself.

### After M8 acceptance: M9

Follow [`docs/NEXT.md`, M9](https://github.com/rolandmraiha-cmd/roland-agent/blob/e208bbd53e42ce4d6d88110e14060dfbed6f7e0b/docs/NEXT.md#L602-L637): security review and residual-risk documentation; backup restore drill on a copy; measured memory, speed, soak/restart recovery; final runbooks and status docs; version `2.0.0` and changelog; required CI and host acceptance. The final release PR is **`v2` → `main`, merged by Roland**.

This draft adds only this handoff document. No app code, tests, dependencies, workflows, deployment settings, training settings, or serving-model changes are proposed.
