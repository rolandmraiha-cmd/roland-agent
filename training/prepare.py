"""Validate exports, dedupe, preserve the private split and mask only the final target."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from agent.models.action import build_action_schema
from agent.models.parse import validate
from agent.training.files import sha256
from training.common import config, load_jsonl, save_jsonl


def validate_action(action, tools):
    if validate(build_action_schema(tools), action):
        raise ValueError("Invalid target action")


def identity(record):
    payload = json.dumps(
        {key: record[key] for key in ("messages", "tools")},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def context_identity(messages, tools):
    return hashlib.sha256(
        json.dumps(
            {"messages": messages, "tools": tools}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def prepare(dataset: Path, out: Path, tokenizer, *, dry_run=False):
    settings = config()
    manifest = json.loads((dataset / "manifest.json").read_text())
    if set(manifest["sha256"]) != {"sft.jsonl", "dpo.jsonl", "eval_private.jsonl"}:
        raise ValueError("Incomplete dataset checksum manifest")
    for filename, digest in manifest["sha256"].items():
        if (
            filename not in {"sft.jsonl", "dpo.jsonl", "eval_private.jsonl"}
            or sha256(dataset / filename) != digest
        ):
            raise ValueError("Dataset checksum mismatch")
    private = {
        context_identity(row["messages"][:-1], row["tools"])
        for row in load_jsonl(dataset / "eval_private.jsonl")
    }
    rows, seen, seed_count = [], set(), 0
    for row in load_jsonl(dataset / "sft.jsonl"):
        if row.get("schema") != 1 or not row.get("messages") or row["messages"][-1]["role"] != "assistant":
            raise ValueError("Invalid SFT schema")
        validate_action(json.loads(row["messages"][-1]["content"]), row["tools"])
        key = identity(row)
        if context_identity(row["messages"][:-1], row["tools"]) in private:
            raise ValueError("Private evaluation context appeared in training")
        if key in seen:
            continue
        seen.add(key)
        prompt = tokenizer.apply_chat_template(
            row["messages"][:-1], tokenize=False, add_generation_prompt=True
        )
        completion = row["messages"][-1]["content"] + tokenizer.eos_token
        if len(tokenizer(prompt + completion)["input_ids"]) > (512 if dry_run else settings["max_seq_len"]):
            continue
        rows.append({"prompt": prompt, "completion": completion, "source": row["source"]})
        seed_count += row["source"] == "seed"
    if not rows or seed_count / len(rows) < max(0.3, settings["seed_ratio"]):
        raise ValueError("Filtered data does not meet the seed replay ratio")
    if not dry_run and len(rows) - seed_count < settings["min_sft"]:
        raise ValueError("Filtered data does not meet the new SFT minimum")
    # A dry run still uses the same prompt/completion loss boundary.
    rows.sort(key=lambda row: hashlib.sha256((row["prompt"] + row["completion"]).encode()).hexdigest())
    split = max(1, min(len(rows) - 1, round(len(rows) * 0.9)))
    save_jsonl(out / "train.jsonl", rows[:split])
    save_jsonl(out / "val.jsonl", rows[split:])
    pairs = load_jsonl(dataset / "dpo.jsonl")
    for pair in pairs:
        validate_action(json.loads(pair["chosen"]), pair["tools"])
        validate_action(json.loads(pair["rejected"]), pair["tools"])
        if (
            pair.get("meta", {}).get("private_eval")
            or context_identity(pair["prompt"], pair["tools"]) in private
        ):
            raise ValueError("Private DPO context")
    if len(pairs) < settings["min_pairs"]:
        pairs = []
    save_jsonl(
        out / "dpo.jsonl",
        [
            {
                "prompt": tokenizer.apply_chat_template(
                    pair["prompt"], tokenize=False, add_generation_prompt=True
                ),
                "chosen": pair["chosen"] + tokenizer.eos_token,
                "rejected": pair["rejected"] + tokenizer.eos_token,
            }
            for pair in pairs
        ],
    )
    return {
        "train": split,
        "val": len(rows) - split,
        "pairs": len(pairs),
        "seed_ratio": seed_count / len(rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--base", type=Path, required=True)
    args = parser.parse_args()
    from transformers import AutoTokenizer

    print(prepare(args.dataset, args.out, AutoTokenizer.from_pretrained(args.base, local_files_only=True)))
