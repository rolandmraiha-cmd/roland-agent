"""Shared script settings, pinned base integrity and a synthetic CPU test model."""

from __future__ import annotations

import json
import os
from pathlib import Path

# Library telemetry and hub online fallback are disabled before importing HF libraries.
os.environ.update(HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1", WANDB_DISABLED="true")


def config():
    import yaml

    return yaml.safe_load(Path(__file__).with_name("config").joinpath("default.yaml").read_text())


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def save_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )


def pinned_base(destination: Path) -> Path:
    from huggingface_hub import snapshot_download

    from agent.training.files import sha256

    settings = config()
    lock = json.loads(Path(__file__).with_name("base_models.lock").read_text())
    if lock["repo"] != settings["base_model"] or lock["revision"] != settings["revision"]:
        raise ValueError("Base revision differs from the reviewed integrity lock")
    snapshot_download(
        lock["repo"],
        revision=lock["revision"],
        local_dir=destination,
        allow_patterns=list(lock["files"]),
        token=False,
    )
    for name, digest in lock["files"].items():
        if sha256(destination / name) != digest:
            raise ValueError("Base weights or tokenizer failed SHA-256 verification")
    return destination


def tiny_base(destination: Path) -> Path:
    import sentencepiece as spm
    import torch
    from transformers import LlamaConfig, LlamaForCausalLM, LlamaTokenizer

    destination.mkdir(parents=True, exist_ok=True)
    corpus = destination / "corpus.txt"
    # Committed synthetic seed content only. No personal data or downloaded model weights.
    corpus.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in load_jsonl(Path(__file__).with_name("seed") / "replay.jsonl")
        ),
        encoding="utf-8",
    )
    spm.SentencePieceTrainer.train(
        input=str(corpus),
        model_prefix=str(destination / "tokenizer"),
        vocab_size=320,
        model_type="bpe",
        byte_fallback=False,
        character_coverage=1.0,
        shuffle_input_sentence=False,
        num_threads=1,
        minloglevel=2,
    )
    tokenizer = LlamaTokenizer(vocab_file=str(destination / "tokenizer.model"))
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.chat_template = "{% for message in messages %}{{ message['role'] + ': ' + message['content'] + '\\n' }}{% endfor %}{% if add_generation_prompt %}{{ 'assistant: ' }}{% endif %}"
    tokenizer.save_pretrained(destination)
    torch.manual_seed(42)
    model = LlamaForCausalLM(
        LlamaConfig(
            vocab_size=len(tokenizer),
            hidden_size=256,
            intermediate_size=512,
            num_hidden_layers=1,
            num_attention_heads=4,
            num_key_value_heads=4,
            max_position_embeddings=512,
            bos_token_id=tokenizer.bos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    )
    model.save_pretrained(destination, safe_serialization=True)
    (destination / "LICENSE").write_text("Synthetic test weights created by this repository. Apache-2.0.\n")
    (destination / "NOTICE").write_text("Synthetic test model; never a production candidate.\n")
    corpus.unlink()
    return destination
