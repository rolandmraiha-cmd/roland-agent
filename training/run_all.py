"""GPU pipeline and CPU dry run. Scratch data is removed on every exit, including signals."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

from agent.eval.gate import compare
from agent.eval.runner import evaluate, load_cases
from agent.models.llamacpp import LlamaCppBrain
from agent.training.files import atomic_write, encode, sha256
from training.common import config, load_jsonl, pinned_base, save_jsonl, tiny_base

ROOT = Path(__file__).resolve().parents[1]


async def eval_model(model: Path, source: Path, *, identifier: str, dry_run=False, private=()) -> dict:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            str(source / "build/bin/llama-server"),
            "--model",
            str(model),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ctx-size",
            "4096",
            "--parallel",
            "1",
            "--threads",
            "2",
            "--cache-ram",
            "0",
            "--no-webui",
            "--jinja",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    brain = LlamaCppBrain(
        f"http://127.0.0.1:{port}", "current", "", temperature=0, seed=42, max_new_tokens=256
    )
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(150):
                if process.poll() is not None:
                    raise ValueError("Pinned server refused the GGUF")
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2)
            else:
                raise ValueError("Model health deadline expired")
        report = await evaluate(brain, load_cases(ROOT / "agent/eval/cases"), version_id=identifier)
        if private:
            report["private_eval"] = await evaluate(brain, list(private), version_id=identifier)
        return report
    finally:
        await brain.aclose()
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def synthetic_dataset(path: Path):
    rows = load_jsonl(ROOT / "training/seed/replay.jsonl")
    path.mkdir(parents=True)
    save_jsonl(path / "sft.jsonl", rows)
    pairs = [
        {
            "id": "synthetic-pair-" + str(index),
            "schema": 1,
            "source": "correction",
            "prompt": row["messages"][:-1],
            "tools": row["tools"],
            "chosen": row["messages"][-1]["content"],
            "rejected": encode({"action": "reply", "text": "I ignored this synthetic request."}),
            "meta": {},
        }
        for index, row in enumerate(rows[:20])
    ]
    save_jsonl(path / "dpo.jsonl", pairs)
    save_jsonl(path / "eval_private.jsonl", [])
    atomic_write(
        path / "manifest.json",
        encode(
            {
                "id": "synthetic-cpu",
                "schema": 1,
                "counts": {"sft": len(rows), "dpo": len(pairs), "eval": 0, "seed": len(rows)},
                "scrub_counts": {},
                "prompt_version_ids": [1],
                "sha256": {
                    name: sha256(path / name) for name in ("sft.jsonl", "dpo.jsonl", "eval_private.jsonl")
                },
            }
        ),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--current-model", type=Path)
    parser.add_argument("--current-version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        from training.check_gpu import check

        check()
        if not args.dataset or not args.current_model:
            parser.error("Full training requires --dataset and --current-model")
    source = Path(os.environ["LLAMA_CPP_DIR"]).resolve()
    pin = dict(line.split("=", 1) for line in (ROOT / "docker/model/VERSION").read_text().splitlines())[
        "commit"
    ]
    if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != pin:
        raise ValueError("llama.cpp checkout does not match the reviewed pin")
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: (_ for _ in ()).throw(SystemExit(128 + signum)))
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="roland-training-") as temp:
        scratch = Path(temp)
        os.environ.update(HF_HOME=str(scratch / "cache"), HF_DATASETS_CACHE=str(scratch / "cache/datasets"))
        dataset = scratch / "dataset"
        if args.dataset:
            shutil.copytree(args.dataset, dataset)
        else:
            synthetic_dataset(dataset)
        base = tiny_base(scratch / "base") if args.dry_run else pinned_base(scratch / "base")
        from transformers import AutoTokenizer

        from training.merge import merge
        from training.prepare import prepare
        from training.train_sft import train

        tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
        prepared = prepare(dataset, scratch / "prepared", tokenizer, dry_run=args.dry_run)
        sft_metrics = train(base, scratch / "prepared", scratch / "sft", dry_run=args.dry_run)
        merged = scratch / "merged"
        merge(base, scratch / "sft/adapter", merged, dry_run=args.dry_run)
        adapter = scratch / "sft/adapter"
        dpo_metrics = None
        if prepared["pairs"] >= config()["min_pairs"]:
            from training.train_dpo import train as train_dpo

            dpo_metrics = train_dpo(merged, scratch / "prepared", scratch / "dpo", dry_run=args.dry_run)
            merge(merged, scratch / "dpo/adapter", scratch / "dpo-merged", dry_run=args.dry_run)
            merged = scratch / "dpo-merged"
            adapter = scratch / "dpo/adapter"
        candidate = scratch / "candidate"
        subprocess.run(
            ["bash", str(ROOT / "training/convert_quantize.sh"), str(merged), str(candidate)], check=True
        )
        digest = sha256(candidate / "model.gguf")
        identifier = (
            ("tiny" if args.dry_run else "qwen3-4b") + "-q4km-" + time.strftime("%Y%m%d") + "-" + digest[:8]
        )
        current_model = args.current_model or candidate / "model.gguf"
        private = []
        for row in load_jsonl(dataset / "eval_private.jsonl"):
            action = json.loads(row["messages"][-1]["content"])
            if not row.get("meta", {}).get("has_target", True):
                if action["action"] != "tool":
                    continue  # A thumb-down with no correction has no reliable reply target.
                expected = {"must_not_call": [action["tool"]]}
            else:
                expected = (
                    {"tool": action["tool"], "args": action["args"]}
                    if action["action"] == "tool"
                    else {"tool": None, "reply_contains_any": [action["text"]]}
                )
            private.append(
                {
                    "id": row["id"],
                    "messages": row["messages"][:-1],
                    "tools": row["tools"],
                    "category": "private",
                    "critical": False,
                    "expect": expected,
                }
            )
        current = asyncio.run(
            eval_model(
                current_model, source, identifier=args.current_version, dry_run=args.dry_run, private=private
            )
        )
        report = asyncio.run(
            eval_model(
                candidate / "model.gguf", source, identifier=identifier, dry_run=args.dry_run, private=private
            )
        )
        atomic_write(
            candidate / "eval-report.json",
            encode({"current": current, "candidate": report, "comparison": compare(current, report)}),
        )
        metrics = {
            "sft": sft_metrics,
            "dpo": dpo_metrics,
            "data": prepared,
            "duration_s": time.monotonic() - started,
            "dry_run": args.dry_run,
        }
        atomic_write(candidate / "train-metrics.json", encode(metrics))
        shutil.copytree(scratch / "sft/adapter", candidate / "adapter/sft")
        if dpo_metrics is not None:
            shutil.copytree(adapter, candidate / "adapter/dpo")
        lock = "requirements-train-cpu.lock" if args.dry_run else "requirements-train.lock"
        shutil.copyfile(ROOT / "training" / lock, candidate / "requirements-train.lock")
        shutil.copyfile(base / "LICENSE", candidate / "LICENSE")
        if (base / "NOTICE").exists():
            shutil.copyfile(base / "NOTICE", candidate / "NOTICE")
        else:
            atomic_write(
                candidate / "NOTICE",
                "Qwen3-4B-Instruct-2507, Alibaba Cloud, Apache-2.0. Pinned source and original licence are in manifest.json and LICENSE.\n",
            )
        dataset_manifest = json.loads((dataset / "manifest.json").read_text())
        manifest = {
            "id": identifier,
            "base_model": {
                "repo": config()["base_model"] if not args.dry_run else "synthetic-tiny",
                "revision": config()["revision"] if not args.dry_run else "local-seed-42",
                "licence": "Apache-2.0",
                "licence_url": "https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/"
                + config()["revision"]
                + "/LICENSE",
            },
            "parent_version": args.current_version,
            "quant": "Q4_K_M",
            "sha256": digest,
            "size": (candidate / "model.gguf").stat().st_size,
            "ctx": 4096,
            "capabilities": ["text", "tools"],
            "dataset_id": dataset_manifest["id"],
            "dataset_manifest_sha256": sha256(dataset / "manifest.json"),
            "prompt_version_ids": dataset_manifest["prompt_version_ids"],
            "train_config": sha256(ROOT / "training/config/default.yaml"),
            "llama_cpp_build": pin,
            "requirements_train_lock": sha256(candidate / "requirements-train.lock"),
            "eval_summary": compare(current, report),
            "created": time.time(),
            "dry_run": args.dry_run,
        }
        template = (ROOT / "training/MODEL_CARD.template.md").read_text()
        atomic_write(
            candidate / "MODEL_CARD.md",
            template.replace("{{MODEL_ID}}", identifier).replace(
                "{{DETAILS}}",
                json.dumps({"manifest": manifest, "dataset": dataset_manifest, "metrics": metrics}, indent=2),
            ),
        )
        manifest["artifacts_sha256"] = {
            str(path.relative_to(candidate)).replace("\\", "/"): sha256(path)
            for path in candidate.rglob("*")
            if path.is_file()
        }
        atomic_write(candidate / "manifest.json", encode(manifest))
        with tarfile.open(output / "candidate.tar", "w") as archive:
            for path in sorted(candidate.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(candidate)), recursive=False)
    print("Candidate prepared for import and human review:", output / "candidate.tar")


if __name__ == "__main__":
    main()
