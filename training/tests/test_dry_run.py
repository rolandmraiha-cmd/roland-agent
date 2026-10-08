"""Actual tokenizer preparation tests, including the final-target loss boundary."""

import json

import pytest

from agent.training.files import atomic_write, encode, sha256
from training.common import load_jsonl, save_jsonl, tiny_base
from training.prepare import prepare
from training.run_all import synthetic_dataset


@pytest.fixture(scope="module")
def tokenizer(tmp_path_factory):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        tiny_base(tmp_path_factory.mktemp("tiny-base")), local_files_only=True
    )


def test_preparation_preserves_only_final_target_boundary(tmp_path, tokenizer):
    dataset = tmp_path / "dataset"
    synthetic_dataset(dataset)
    result = prepare(dataset, tmp_path / "prepared", tokenizer, dry_run=True)
    assert result["seed_ratio"] >= 0.3 and result["pairs"] >= 20
    rows = load_jsonl(tmp_path / "prepared/train.jsonl")
    assert all(
        row["completion"].startswith('{"') and row["completion"].endswith(tokenizer.eos_token) for row in rows
    )
    assert all(row["prompt"].endswith("assistant: ") for row in rows)


def test_private_context_with_different_target_is_never_trained(tmp_path, tokenizer):
    dataset = tmp_path / "dataset"
    synthetic_dataset(dataset)
    row = load_jsonl(dataset / "sft.jsonl")[0]
    row["messages"][-1]["content"] = encode({"action": "reply", "text": "A different private target"})
    save_jsonl(dataset / "eval_private.jsonl", [row])
    manifest = json.loads((dataset / "manifest.json").read_text())
    manifest["sha256"]["eval_private.jsonl"] = sha256(dataset / "eval_private.jsonl")
    atomic_write(dataset / "manifest.json", encode(manifest))
    with pytest.raises(ValueError, match="Private"):
        prepare(dataset, tmp_path / "prepared", tokenizer, dry_run=True)


def test_gpu_required_full_run_fails_closed_without_gpu(monkeypatch):
    import torch

    from training.check_gpu import check

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(SystemExit) as error:
        check()
    assert error.value.code == 3
