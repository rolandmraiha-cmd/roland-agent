# Separate-machine training

Training runs on a separate CUDA machine with at least 16 GiB of VRAM. The VPS exports,
imports and evaluates; it never fine-tunes. Renting a machine or sending a dataset needs
Roland's explicit decision. The default launch mode is manual and no provider is configured.

1. Enable capture in Settings only for chats you want reviewed. A chat marked **Never** is
   excluded even while global capture is enabled. Screen and sign-in sessions suppress capture.
2. Label answers or approvals. Review Training examples and targets. Build a dataset after
   at least 50 new SFT examples remain after the private split; DPO needs at least 20 pairs.
   Download the bundle. It contains the scrubbed dataset and these scripts, with no credentials.
3. Inspect the data before transfer. Scrubbing reduces exposure; pattern detection cannot
   guarantee that arbitrary identifying prose is anonymous. Never transfer an unreviewed export.
4. Download the **current** verified GGUF from your own model registry to the GPU machine as
   `current.gguf`. Its exact version id is the evaluation baseline. Do not substitute another model.
5. On the GPU machine, using Python 3.12, a CUDA toolkit compatible with PyTorch 2.8/cu128,
   Git, CMake and a C++ compiler:

```bash
tar -xzf DATASET_ID.tar.gz
bash training/bootstrap.sh
. .training-venv/bin/activate
export LLAMA_CPP_DIR="$PWD/.llama.cpp"
bash training/run_all.sh --dataset dataset --current-model current.gguf \
  --current-version EXACT_CURRENT_VERSION --out result
```

The scripts download only the public pinned base model and its tokenizer. `base_models.lock`
verifies every file's SHA-256 at revision `cdbee75f17c01a7cc42f958dc650907174af0554`.
Dependencies are pinned with hashes. No hosted model inference, provider SDK or remote model
code is used. Training-library telemetry is disabled. GPU credentials stay on that machine.

The pipeline validates and deduplicates data, reserves private contexts before mixing replay,
uses at least 30% unique synthetic replay, and masks every token except the final assistant
action. It runs NF4 QLoRA SFT (r16/alpha32, all seven projection layers, seed42) and, when
there are enough pairs, DPO against the **SFT-merged** reference. Both use validation and stop
after two successive validation-loss increases. Conversion and Q4_K_M quantisation use the
exact commit in `docker/model/VERSION`. Scratch data and HF caches are deleted on exit.

`result/candidate.tar` contains the GGUF and checksum, manifest, both adapters when DPO runs,
model card, evaluation reports, training metrics, dependency lock, licence and notice. Evaluation
uses the production parser/schema, temperature 0/seed42, and mock tool results: no tool is
executed. It compares the current model and candidate on the same 284 public cases, including
60 gate cases (32 critical), 60 injection cases, tools and replies, in English and Finnish.
Private labelled holdouts are measured separately and never trained on. Negative replies
without corrections have no reliable reply target and are counted in the export, not scored.

Importing never switches models. A regression in gate compliance or injection refusal, any
critical failure, insufficient coverage, or a missed numeric floor rejects the candidate. A passing
report creates a pending human review request; see [the model runbook](../docs/MODEL.md).

## CPU CI

The `Training CPU and model switch` workflow installs `requirements-train-cpu.lock`, creates
tiny synthetic weights locally, runs one SFT and one DPO step, merges, converts and evaluates,
then loads the actual GGUF in the pinned production container. This is a wiring check, not
evidence that a fine-tuned Qwen model is good. Synthetic dry-run artifacts are refused by the
production importer. The swap test uses explicitly fabricated passing report fixtures to test
the supervisor independently of model quality; the real report remains in the CI artifact.

With a reviewed CPU llama.cpp build and the CPU lock installed, the CI command is:

```bash
LLAMA_CPP_DIR=/absolute/path/to/llama.cpp bash training/run_all.sh \
  --dry-run --current-version ci-tiny-base --out .ci-training
pytest training/tests -q
```

The Docker swap probe is restricted to disposable GitHub CI. No paid GPU is needed for CI.

## Optional remote launcher

After review, host smoke and Roland's decision, enable `COMPOSE_PROFILES=training` together
with `TRAINER_URL=http://10.77.7.70:7200`. They must be both enabled or both unset. Trainer
has no published port, requires the core peer **and** its bearer secret, reads training data
read-only and writes only models/runs. It has 128 MiB, 0.5 CPU and 64 processes. Core and model
mount model weights read-only. Never mount the browser profile or Docker socket in trainer.

For `TRAINING_LAUNCH_MODE=ssh`, mount a dedicated SSH private key as `training_ssh_key` and
a reviewed public `known_hosts` file with `docker-compose.training-ssh.yml`. Set
`TRAINING_SSH_TARGET=user@host`, `TRAINING_KNOWN_HOSTS_FILE=/absolute/path/known_hosts` and
use `TRAINING_COMPOSE_OVERRIDE=ssh APPLY=1 make deploy`. Strict host-key checking is required;
no trust-on-first-use, password login or agent forwarding. Always review host keys independently.

For hook mode, choose a provider explicitly and implement reviewed `provision`/`teardown` hooks
under `training/providers/NAME`. The example exits without provisioning anything. Provision
prints exactly `user@host` and a pinned known-hosts line. Teardown must be idempotent and identify
the rental from `TRAINING_RUN_ID`/`TRAINING_RUN_DIR`, even when provision did not return an address.
Mount hooks read-only and the provider token only in trainer with `docker-compose.training-hook.yml`;
use `TRAINING_COMPOSE_OVERRIDE=hook` for host Make targets. No provider secret is readable by core.

The default cap is four hours (configurable 1–24). Cancellation, timeout, provisioning failure,
training failure and process shutdown all attempt bounded remote cleanup and provider teardown.
A SIGKILL, host outage or unreachable provider cannot run cleanup: configure a provider-side
expiry before renting, and check for orphan rentals yourself after an outage. Automatic rental
is unavailable until Roland explicitly configures and approves a provider and its budget.

The system job is paused by default, can only be toggled by Roland, requires capture enabled,
waits for a completed backup and its shared lock, and skips insufficient data with a visible notice.
It may prepare/import a candidate; it cannot approve a model. Training-data backup inclusion
remains Roland's decision; existing database backups do not include the separate training volume.
