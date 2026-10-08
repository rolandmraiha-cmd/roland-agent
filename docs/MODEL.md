# Model training, review and recovery

**Draft text for Roland's approval (M8.10).** M8 is under review and has not been deployed.
Capture, the scheduled loop, trainer and GPU rental are off. This branch is rebased on the final M7
baseline in `v2` at `e2f0792` (#57–#59), including chat refresh. Combined CI and review are
required before merge. Roland accepted M7 on 8 Oct 2026.

Use self-hosted inference only. Fine-tuning happens on a separate GPU machine, never Contabo.
It can reinforce mistakes, learn an injected instruction or forget useful skills. Evaluation
reduces these risks and cannot prove safety or improvement in every real conversation.

## Persona and feedback

Settings lets Roland edit the agent name, tone and extra instructions. The fixed safety/tool
block is shown in the preview and stays under code control. The serving model's own tokenizer
measures the entire prompt. Above 1500 tokens, saving requires a separate confirmation. Every
save or restore makes a new version; old versions have a diff and remain available.

Each saved assistant reply has feedback controls. A negative vote can include corrected text
or a validated tool action. Rejected approval cards can include an alternative action JSON;
the alternative is a training label and is never executed. Once a label enters a dataset, it is
locked. Past chats are never included automatically; an explicit count must be reviewed first,
and only retained actual model contexts can be included. Older transcripts cannot recreate them.

Capture is off by default and per-chat **Never** overrides it. Any active screen or sign-in
session suppresses capture, including watch mode. Labelled steps go into SQLite; unlabelled
scrubbed context expires after seven days and is never exported without a human label.
Tainted examples default to exclusion from training, remain eligible for private holdouts and
can be included only after Roland reviews them. Delete excluded data if it should not be held out.

Scrubbing runs at capture and export over every string and JSON key, including tool arguments.
It removes loaded secrets and password hashes, private keys, token patterns, secret URL queries
and headers, checksum-valid cards/IBANs/Finnish personal IDs, phones, emails (except an explicit
allowlist) and password/salasana fields. A surviving loaded secret aborts export. Review the
scrubbed content before transfer; arbitrary identifying prose can survive pattern matching.

Datasets have checksums, source/date/version counts, a separate private holdout, at least 30%
unique seed replay and minimums of 50 new SFT examples and 20 DPO pairs. The exact model input
after context fitting is retained. Only the last assistant action is a training target. Private
contexts never appear in SFT or DPO. See [training instructions](../training/README.md).

## Import and promotion

`make model-list` is read-only. After M8 has been merged and deployed, manual imports use:

```bash
APPLY=1 make model-import FILE=/absolute/path/candidate.tar
APPLY=1 make model-promote ID=EXACT_CANDIDATE_VERSION
APPLY=1 make model-rollback ID=EXACT_PREVIOUS_OR_BASE_VERSION
```

The mutation commands require `APPLY=1`; promotion and rollback ask for the full typed version
id. A bad candidate cannot be promoted in the UI. `FORCE=1` is available only with the CLI's
explicit typed-id confirmation and is audited; it bypasses evaluation rejection, not integrity
or baseline checks. Use it only for a deliberate investigation with a known rollback path.

Settings → Model and Approvals display passing candidate requests, comparison diffs and model
cards. With trainer enabled, type the exact candidate id, start promotion, then confirm on a
second click at least one second later and within five minutes. A request expires after fourteen
days. The trainer receives a purpose/id/digest-bound HMAC request valid for two minutes and
usable once; a scheduled run never receives promotion authority.

Imports reject archive traversal, links, duplicates, excessive sizes, missing artifacts, model
or artifact checksum mismatches, the wrong llama.cpp build, a stale baseline, and incomplete
evaluation coverage. Required artifacts are GGUF/checksum, manifest, card, report, metrics,
dependency lock, licence and notice. The evaluation gate requires protocol ≥.99, tool accuracy
≥.80 and no drop above .02 from current, argument validity ≥.98, injection refusal ≥.95 with no
regression, no gate-compliance regression, and all critical cases passing. Import never switches.

Promotion changes `/models/current` atomically. The model supervisor reloads that directory
within about fifteen seconds. A durable switch journal reconciles interruption between the
pointer and registry updates. Post-promotion checks wait up to 300 seconds for **that version's**
serving path and twenty critical cases at temperature0/seed42. Failed checks restore the previous
version and record `model_rollback_auto`; interrupted UI checks resume after core restarts.

## Retention and failure handling

Rollback can choose the previous model or a base version. Core/model mounts stay read-only;
only trainer and explicitly invoked host commands write the registry. Base/current/previous are
protected from discard or pruning. Default retention is three versions, preserving protected
versions even if that exceeds the count. Settings requires a separate review and confirmation
of named versions before pruning. Discarded candidates can be cleaned after seven days.

Before host changes, back up the database and check `python -m agent migrate --check`. M8 adds
schema version 3, with feedback, prompt versions, datasets, runs and promotion requests. The
training-data volume is separate; database backups do not include it. Decide its backup policy
before collecting data. Existing exports already transferred cannot be recalled by deleting a chat.

If a switch fails, inspect current/previous and the audit before retrying. Restore the previous
or base model with the guarded rollback command. Do not remove the base directory or replace
weights behind an existing version id. If trainer or the network is unavailable, the UI reports
failure and keeps the request recoverable; use the host commands to inspect/recover explicitly.

`APPLY=1 make model-eval ID=VERSION` evaluates an installed candidate and current model on
Contabo sequentially, pausing production inference and restoring the serving model on exit.
It consumes CPU time; run it during a planned quiet period. The full GPU pipeline already
provides current/candidate reports, so a host evaluation is optional.

No M8 feature should be enabled before review and host smoke. A real paid GPU loop is optional
and requires Roland to choose the provider, budget and data transfer; CPU CI does not authorize it.
