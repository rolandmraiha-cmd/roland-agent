"""Synthetic registry artifacts for validation tests; never usable model weights."""

import hashlib
import json
import tarfile
from pathlib import Path

from test_eval_gate import report

from agent.models.registry import REQUIRED, Registry, pinned_build
from agent.training.files import atomic_write, encode, sha256


def registry(root: Path):
    directory = root / "versions/test-base"
    directory.mkdir(parents=True)
    atomic_write(directory / "model.gguf", b"GGUFsynthetic base")
    digest = sha256(directory / "model.gguf")
    atomic_write(directory / "model.sha256", digest)
    atomic_write(directory / "manifest.json", encode({"id": "test-base", "sha256": digest}))
    atomic_write(directory / "MODEL_CARD.md", "Synthetic base")
    atomic_write(
        root / "registry.json",
        encode(
            {
                "schema": 1,
                "current": "test-base",
                "previous": None,
                "versions": {"test-base": {"status": "active", "created": 1, "sha256": digest}},
            }
        ),
    )
    return Registry(root)


def candidate(root: Path, *, identifier="candidate-a", mutate=None):
    files = {name: b"Synthetic test artifact" for name in REQUIRED - {"manifest.json"}}
    files["model.gguf"] = b"GGUFsynthetic candidate"
    digest = hashlib.sha256(files["model.gguf"]).hexdigest()
    files["model.sha256"] = digest.encode()
    files["eval-report.json"] = encode(
        {"current": report("test-base"), "candidate": report(identifier)}
    ).encode()
    manifest = {
        "id": identifier,
        "sha256": digest,
        "size": len(files["model.gguf"]),
        "parent_version": "test-base",
        "llama_cpp_build": pinned_build(),
        "base_model": {"repo": "synthetic", "licence": "Apache-2.0"},
        "ctx": 512,
        "quant": "Q4_K_M",
    }
    if mutate:
        mutate(manifest, files)
    manifest["artifacts_sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    files["manifest.json"] = encode(manifest).encode()
    directory = root / (identifier + "-artifacts")
    directory.mkdir()
    for name, value in files.items():
        atomic_write(directory / name, value)
    path = root / (identifier + ".tar")
    with tarfile.open(path, "w") as archive:
        for name in files:
            archive.add(directory / name, arcname=name)
    return path, digest


def regressed(_manifest, files):
    value = json.loads(files["eval-report.json"])
    value["candidate"]["gate_compliance"] = 0.5
    files["eval-report.json"] = encode(value).encode()
