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


def test_tiny_context_fits_public_eval_and_output(tokenizer):
    from transformers import AutoConfig

    from agent.eval.runner import load_cases
    from agent.models.context import action_prompt
    from training.run_all import ROOT

    lengths = []
    for case in load_cases(ROOT / "agent/eval/cases"):
        messages = [dict(message) for message in case["messages"]]
        system = next(message for message in messages if message["role"] == "system")
        system["content"] += action_prompt(case["tools"])
        lengths.append(len(tokenizer.apply_chat_template(messages, add_generation_prompt=True)))
    # The old 512-token model could not run the full public suite on pinned llama.cpp.
    assert max(lengths) > 512
    context = AutoConfig.from_pretrained(tokenizer.name_or_path, local_files_only=True)
    assert max(lengths) + 256 < context.max_position_embeddings


def test_tiny_tokenizer_preserves_unseen_characters(tokenizer):
    assert all(
        tokenizer.convert_tokens_to_ids(f"<0x{value:02X}>") != tokenizer.unk_token_id for value in range(256)
    )
    text = "Ω🙂漢字"
    tokens = tokenizer.encode(text, add_special_tokens=False)
    assert tokenizer.unk_token_id not in tokens
    assert tokenizer.decode(tokens, skip_special_tokens=True) == text


def test_tiny_generation_prefix_has_no_artificial_space(tokenizer):
    messages = [{"role": "user", "content": "Reply ok."}]
    without = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
    with_prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    prefix = with_prompt[len(without) :]
    ids = tokenizer.encode(prefix, add_special_tokens=False)
    first_piece = tokenizer.convert_ids_to_tokens(ids[0]).replace("▁", " ")
    # Pinned llama.cpp drops a whitespace-prefixed first token for a nonspace prefix.
    assert prefix.startswith("assistant:")
    assert not first_piece.startswith(" ")


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
