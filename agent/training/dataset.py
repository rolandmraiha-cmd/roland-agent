"""Reviewed datasets with private evaluation, seed replay and immutable labels."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tarfile
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from .capture import validate_action
from .files import atomic_write, encode, sha256

ROOT = Path(__file__).resolve().parents[2]


class NotEnoughData(ValueError):
    def __init__(self, counts: dict):
        self.counts = counts
        super().__init__("Not enough new labelled data")


def sft_record(row: dict) -> dict:
    target = json.loads(row["target_json"] or row["output_json"])
    validate_action(target)
    return {
        "id": row["id"],
        "schema": 1,
        "source": row["source"],
        "weight": 1.0,
        "messages": json.loads(row["messages_json"]) + [{"role": "assistant", "content": encode(target)}],
        "tools": json.loads(row["tools_json"]),
        "meta": {
            "model_version": row["model_version_id"],
            "prompt_version": row["prompt_version_id"],
            "created": datetime.fromtimestamp(row["created"], UTC).isoformat(),
            "tainted": bool(row["tainted"]),
            "has_target": row["target_json"] is not None,
        },
    }


def identity(record: dict) -> str:
    # Context and target define an example, not its id, source or timestamp.
    return hashlib.sha256(encode({key: record[key] for key in ("messages", "tools")}).encode()).hexdigest()


def context_identity(record: dict) -> str:
    return hashlib.sha256(
        encode({"messages": record["messages"][:-1], "tools": record["tools"]}).encode()
    ).hexdigest()


class Datasets:
    def __init__(self, agent):
        self.agent = agent
        self.memory = agent.memory
        self.root = agent.capture.root / "datasets"

    def past_count(self) -> int:
        rows = self.memory._all(
            "SELECT f.message_id FROM feedback f JOIN chats c ON c.id=f.chat_id "
            "WHERE f.used_in_dataset IS NULL AND c.training_mode!='never' AND NOT EXISTS "
            "(SELECT 1 FROM training_examples e WHERE e.message_id=f.message_id)",
        )
        return sum(self.agent.capture.find_pending(row[0]) is not None for row in rows)

    def include_past(self) -> int:
        # Only actual saved model-step context can be linked. Never reconstruct model input
        # from a chat transcript, which would invent a context the model did not see.
        count = 0
        for row in self.memory._all("SELECT * FROM feedback WHERE used_in_dataset IS NULL"):
            if self.memory._all("SELECT 1 FROM training_examples WHERE message_id=?", (row["message_id"],)):
                continue
            step = self.agent.capture.find_pending(row["message_id"])
            if step and self.agent.capture.enabled(row["chat_id"]):
                self.agent.capture.feedback(
                    row["message_id"],
                    row["rating"],
                    row["correction"],
                    json.loads(row["correction_action"]) if row["correction_action"] else None,
                )
                count += 1
        return count

    def build(self, *, include_past: bool = False, since: float | None = None) -> dict:
        config = self.agent.config
        if not 0.3 <= config.training_seed_ratio < 1:
            raise ValueError("TRAINING_SEED_RATIO must be at least 0.3 and below 1")
        if self.agent.capture.blocked():
            raise ValueError("Training export waits until sign-in and screen sessions end")
        if include_past:
            self.include_past()
        with self.memory.transaction():
            rows = [
                dict(row)
                for row in self.memory._all(
                    "SELECT e.* FROM training_examples e JOIN chats c ON c.id=e.chat_id "
                    "WHERE e.used_in_dataset IS NULL AND (e.include=1 OR e.tainted=1) AND c.training_mode!='never' "
                    "AND (? IS NULL OR e.created>=?) ORDER BY e.created,e.id",
                    (since, since),
                )
            ]
            positives, private, pairs, seen, used, counts = [], [], [], set(), [], Counter()
            prepared, private_contexts = [], set()
            for row in rows:
                clean, more = self.agent.capture.scrubber.scrub(
                    {key: value for key, value in row.items() if not key.endswith("_json")}
                )
                # JSON strings must be decoded before scrubbing to catch password keys.
                for key in ("messages_json", "tools_json", "output_json", "target_json", "outcome_json"):
                    if row[key] is not None:
                        value, nested = self.agent.capture.scrubber.scrub(json.loads(row[key]))
                        self.agent.capture.scrubber.check(value)
                        clean[key] = encode(value)
                        more = Counter(more) + Counter(nested)
                self.agent.capture.scrubber.check(clean)
                counts += Counter(more) + Counter(json.loads(row["scrub_counts_json"]))
                negative = clean["source"] in {"thumbs_down", "rejected_call"} and not clean["target_json"]
                record = sft_record(clean)
                context = context_identity(record)
                if negative or clean["tainted"] and not clean["include"] or int(context[:8], 16) % 10 == 0:
                    private_contexts.add(context)
                prepared.append((row, record, context))
            for row, record, context in prepared:
                fingerprint = identity(record)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                used.append(row)
                # Private split happens BEFORE replay/mixing. A context cannot appear in both.
                holdout = context in private_contexts
                if holdout:
                    record["meta"]["private_eval"] = True
                    private.append(record)
                    continue
                positives.append(record)
                for pair in self.memory._all(
                    "SELECT * FROM preference_pairs WHERE example_id=? AND include=1 AND used_in_dataset IS NULL",
                    (row["id"],),
                ):
                    chosen, a = self.agent.capture.scrubber.scrub(json.loads(pair["chosen_json"]))
                    rejected, b = self.agent.capture.scrubber.scrub(json.loads(pair["rejected_json"]))
                    counts += Counter(a) + Counter(b)
                    validate_action(chosen)
                    validate_action(rejected)
                    pairs.append(
                        {
                            "id": pair["id"],
                            "schema": 1,
                            "source": pair["source"],
                            "prompt": record["messages"][:-1],
                            "chosen": encode(chosen),
                            "rejected": encode(rejected),
                            "tools": record["tools"],
                            "meta": record["meta"],
                        }
                    )
            summary = {
                "new_sft": len(positives),
                "new_pairs": len(pairs),
                "private_eval": len(private),
                "min_sft": config.training_min_new_sft,
                "min_pairs": config.training_min_new_pairs,
            }
            if len(positives) < config.training_min_new_sft:
                raise NotEnoughData(summary)
            if len(pairs) < config.training_min_new_pairs:
                pairs = []
            seeds = [
                json.loads(line)
                for path in sorted((ROOT / "training" / "seed").glob("*.jsonl"))
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            n_seed = math.ceil(len(positives) * config.training_seed_ratio / (1 - config.training_seed_ratio))
            safe_seeds = []
            for seed in seeds:
                validate_action(json.loads(seed["messages"][-1]["content"]))
                if identity(seed) not in seen and context_identity(seed) not in private_contexts:
                    seen.add(identity(seed))
                    safe_seeds.append(seed)
            if len(safe_seeds) < n_seed:
                raise ValueError("Not enough unique replay examples for the required seed ratio")
            sft = positives + safe_seeds[:n_seed]
            identifier = (
                datetime.now(UTC).strftime("%Y%m%d-%H%M-")
                + hashlib.sha256(encode([row["id"] for row in used]).encode()).hexdigest()[:8]
            )
            path = self.root / identifier
            if path.exists():
                raise ValueError("This dataset already exists")
            path.mkdir(mode=0o700, parents=True)
            try:
                hashes = {}
                for name, records in (
                    ("sft.jsonl", sft),
                    ("dpo.jsonl", pairs),
                    ("eval_private.jsonl", private),
                ):
                    self.agent.capture.scrubber.check(records)
                    atomic_write(path / name, "".join(encode(record) + "\n" for record in records))
                    hashes[name] = sha256(path / name)
                manifest = {
                    "id": identifier,
                    "schema": 1,
                    "created": time.time(),
                    "counts": {"sft": len(sft), "dpo": len(pairs), "eval": len(private), "seed": n_seed},
                    "scrub_counts": dict(counts),
                    "sources": dict(Counter(row["source"] for row in used)),
                    "date_range": [min(row["created"] for row in used), max(row["created"] for row in used)],
                    "base_model": "Qwen/Qwen3-4B-Instruct-2507",
                    "prompt_version_ids": sorted({row["prompt_version_id"] for row in used}),
                    "sha256": hashes,
                }
                atomic_write(path / "manifest.json", encode(manifest))
                self.memory._exec(
                    "INSERT INTO training_datasets VALUES (?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        time.time(),
                        len(sft),
                        len(pairs),
                        len(private),
                        n_seed,
                        sha256(path / "manifest.json"),
                        encode(dict(counts)),
                    ),
                )
                for row in used:
                    self.memory._exec(
                        "UPDATE training_examples SET used_in_dataset=? WHERE id=?", (identifier, row["id"])
                    )
                    self.memory._exec(
                        "UPDATE preference_pairs SET used_in_dataset=? WHERE example_id=?",
                        (identifier, row["id"]),
                    )
                    self.memory._exec(
                        "UPDATE feedback SET used_in_dataset=? WHERE message_id=?",
                        (identifier, row["message_id"]),
                    )
                self.memory.set_meta("last_dataset_id", identifier)
                self.agent.audit.write(
                    "roland",
                    "training_dataset_built",
                    detail={"dataset_id": identifier, **manifest["counts"]},
                )
            except BaseException:
                shutil.rmtree(path)
                raise
        return manifest

    def archive(self, identifier: str) -> Path:
        if not self.memory._all("SELECT 1 FROM training_datasets WHERE id=?", (identifier,)):
            raise KeyError(identifier)
        target = self.root / (identifier + ".tar.gz")
        descriptor, temporary = tempfile.mkstemp(prefix=".archive-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                    archive.add(self.root / identifier, arcname="dataset", filter=_private_member)
                    archive.add(ROOT / "training", arcname="training", filter=_private_member)
                    archive.add(ROOT / "agent", arcname="agent", filter=_private_member)
                    archive.add(
                        ROOT / "docker" / "model" / "VERSION",
                        arcname="docker/model/VERSION",
                        filter=_private_member,
                    )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target


def _private_member(member):
    if (
        "__pycache__" in member.name
        or member.name.endswith(".pyc")
        or not (member.isfile() or member.isdir())
    ):
        return None
    member.mode = 0o700 if member.isdir() else 0o600
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member
