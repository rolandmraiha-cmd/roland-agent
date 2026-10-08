"""Candidate verification and human-initiated atomic switches with automatic rollback."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath

from ..eval.gate import compare
from ..eval.runner import load_cases, suite_hash
from ..locking import file_lock
from ..training.files import atomic_write, encode, read_json, sha256, sync_directory
from .modelreg import ID

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    "model.gguf",
    "model.sha256",
    "manifest.json",
    "MODEL_CARD.md",
    "eval-report.json",
    "train-metrics.json",
    "requirements-train.lock",
    "LICENSE",
    "NOTICE",
}


def pinned_build() -> str:
    return dict(line.split("=", 1) for line in (ROOT / "docker/model/VERSION").read_text().splitlines())[
        "commit"
    ]


class Registry:
    def __init__(self, root: Path, *, max_mb: int = 6144, allow_test_models: bool = False):
        self.root = root
        self.max_bytes = max_mb * 1024 * 1024
        self.allow_test_models = allow_test_models

    def read(self) -> dict:
        return read_json(self.root / "registry.json")

    def details(self) -> dict:
        state = self.read()
        result = {**state, "versions": {}}
        for identifier, row in state["versions"].items():
            path = self.version_path(identifier)
            result["versions"][identifier] = {
                **row,
                "manifest": read_json(path / "manifest.json"),
                "model_card": (path / "MODEL_CARD.md").read_text(encoding="utf-8")[:32000],
            }
        return result

    def version_path(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError("Invalid model version id")
        path = self.root / "versions" / identifier
        if path.is_symlink():
            raise ValueError("Model version directories cannot be symlinks")
        return path

    def import_candidate(self, archive_path: Path) -> dict:
        if archive_path.is_symlink() or archive_path.stat().st_size > self.max_bytes:
            raise ValueError("Candidate archive exceeds MODEL_IMPORT_MAX_MB")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as temporary:
                staging = Path(temporary)
                names, total = set(), 0
                with tarfile.open(archive_path, "r|*") as archive:
                    for member in archive:
                        name = member.name
                        path = PurePosixPath(name)
                        if (
                            not path.parts
                            or str(path) != name.rstrip("/")
                            or path.is_absolute()
                            or ".." in path.parts
                            or "\\" in name
                            or "\x00" in name
                            or name in names
                            or len(names) >= 512
                            or len(name) > 256
                            or not (member.isfile() or member.isdir())
                        ):
                            raise ValueError("Unsafe candidate archive member")
                        names.add(name)
                        if member.isdir():
                            if path.parts[0] != "adapter":
                                raise ValueError("Unexpected candidate directory")
                            continue
                        if name not in REQUIRED and path.parts[0] != "adapter":
                            raise ValueError("Unexpected candidate file")
                        total += member.size
                        limit = (
                            self.max_bytes
                            if name == "model.gguf" or path.parts[0] == "adapter"
                            else 2 * 1024 * 1024
                        )
                        if member.size < 0 or member.size > limit or total > self.max_bytes:
                            raise ValueError("Expanded candidate exceeds the size limit")
                        destination = staging / path
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, destination.open("xb") as out:
                            shutil.copyfileobj(source, out, 1024 * 1024)
                        destination.chmod(0o600)
                if not REQUIRED <= names:
                    raise ValueError("Candidate is missing required artifacts")
                manifest = read_json(staging / "manifest.json", 64 * 1024)
                identifier = manifest.get("id")
                if manifest.get("dry_run") and not self.allow_test_models:
                    raise ValueError("Synthetic dry-run models cannot be imported into production")
                target = self.version_path(identifier)
                if target.exists() or identifier in state["versions"]:
                    raise ValueError("Version id already exists")
                if manifest.get("llama_cpp_build") != pinned_build():
                    raise ValueError("llama.cpp build mismatch")
                actual = sha256(staging / "model.gguf")
                if (
                    actual != manifest.get("sha256")
                    or (staging / "model.sha256").read_text().strip() != actual
                    or manifest.get("size") != (staging / "model.gguf").stat().st_size
                ):
                    raise ValueError("Model SHA-256 or size mismatch")
                with (staging / "model.gguf").open("rb") as stream:
                    if stream.read(4) != b"GGUF":
                        raise ValueError("Candidate is not a GGUF")
                hashes = manifest.get("artifacts_sha256", {})
                if not REQUIRED.difference({"manifest.json"}) <= set(hashes):
                    raise ValueError("Artifact hash manifest is incomplete")
                for name in names - {"manifest.json", "adapter"}:
                    if (staging / name).is_file() and hashes.get(name) != sha256(staging / name):
                        raise ValueError("Candidate artifact hash mismatch")
                if not isinstance(manifest.get("base_model"), dict) or not manifest["base_model"].get(
                    "licence"
                ):
                    raise ValueError("Base-model provenance and licence are required")
                if (
                    manifest.get("ctx", 0) > 6144
                    or manifest.get("ctx", 0) <= 0
                    or manifest.get("quant") != "Q4_K_M"
                ):
                    raise ValueError("Unsupported quantisation or context size")
                report = read_json(staging / "eval-report.json", 2 * 1024 * 1024)
                current, candidate = report.get("current", {}), report.get("candidate", {})
                if (
                    current.get("version_id") != state["current"]
                    or manifest.get("parent_version") != state["current"]
                ):
                    raise ValueError("Evaluation baseline is not the current model")
                expected_suite = suite_hash(load_cases(ROOT / "agent/eval/cases"))
                if (
                    candidate.get("suite_sha256") != expected_suite
                    or current.get("suite_sha256") != expected_suite
                ):
                    raise ValueError("Evaluation report does not cover the current suite")
                reference = {
                    (case["id"], case["category"], case["critical"])
                    for case in load_cases(ROOT / "agent/eval/cases")
                }
                for item in (current, candidate):
                    if {
                        (row.get("id"), row.get("category"), row.get("critical"))
                        for row in item.get("cases", [])
                    } != reference:
                        raise ValueError("Evaluation case coverage differs from the reviewed suite")
                decision = compare(current, candidate)
                status = "candidate" if decision["passed"] else "rejected_auto"
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.replace(staging, target)
                # TemporaryDirectory cleanup must not remove the published version.
                state["versions"][identifier] = {
                    "status": status,
                    "created": time.time(),
                    "sha256": actual,
                    "eval_sha256": sha256(target / "eval-report.json"),
                    "comparison": decision,
                }
                try:
                    atomic_write(self.root / "registry.json", encode(state))
                except BaseException:
                    shutil.rmtree(target)
                    raise
                return {
                    "version_id": identifier,
                    "status": status,
                    "sha256": actual,
                    "from_version_id": state["current"],
                    "comparison": decision,
                }

    def _point(self, identifier: str) -> None:
        temporary = self.root / ".current-next"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("versions") / identifier, target_is_directory=True)
        os.replace(temporary, self.root / "current")
        sync_directory(self.root)

    def _recover(self) -> None:
        journal = self.root / ".switch.json"
        if journal.exists():
            state = read_json(journal)["new_state"]
            path = self.version_path(state["current"])
            if sha256(path / "model.gguf") != read_json(path / "manifest.json")["sha256"]:
                state = read_json(journal)["old_state"]
            self._point(state["current"])
            atomic_write(self.root / "registry.json", encode(state))
            journal.unlink()
            sync_directory(self.root)

    def _switch(self, state: dict, identifier: str) -> None:
        path = self.version_path(identifier)
        manifest = read_json(path / "manifest.json", 64 * 1024)
        if sha256(path / "model.gguf") != manifest.get("sha256"):
            raise ValueError("Model integrity changed")
        old = state["current"]
        if identifier == old:
            raise ValueError("That model is already current")
        previous_state = deepcopy(state)
        state["current"], state["previous"] = identifier, old
        state["versions"][old]["status"] = "available"
        state["versions"][identifier]["status"] = "active"
        journal = self.root / ".switch.json"
        atomic_write(journal, encode({"old_state": previous_state, "new_state": state}))
        try:
            self._point(identifier)
            atomic_write(self.root / "registry.json", encode(state))
        except BaseException:
            self._point(old)
            journal.unlink(missing_ok=True)
            sync_directory(self.root)
            # The old registry was already durable when this operation started.
            raise
        else:
            journal.unlink()
            sync_directory(self.root)

    def promote(self, identifier: str, digest: str, *, requested_by: str, force: bool = False) -> str:
        if requested_by not in {"roland", "cli"} or (force and requested_by != "cli"):
            raise PermissionError("A human promotion request is required")
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            row = state["versions"].get(identifier)
            if (
                not row
                or identifier == state["current"]
                or row["status"] not in ({"candidate", "rejected_auto"} if force else {"candidate"})
            ):
                raise ValueError("Only a passing candidate can be promoted")
            path = self.version_path(identifier)
            manifest = read_json(path / "manifest.json", 64 * 1024)
            if (
                manifest["sha256"] != digest
                or manifest["llama_cpp_build"] != pinned_build()
                or manifest["parent_version"] != state["current"]
                or sha256(path / "eval-report.json") != row["eval_sha256"]
            ):
                raise ValueError("Candidate or evaluation baseline changed")
            previous = state["current"]
            self._switch(state, identifier)
            return previous

    def rollback(
        self, *, requested_by: str, to_version: str | None = None, expected_current: str | None = None
    ) -> str:
        if requested_by not in {"roland", "cli", "automatic_health_failure"}:
            raise PermissionError("A rollback request is required")
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            if expected_current is not None and state["current"] != expected_current:
                raise ValueError("Current model changed before rollback")
            target = to_version or state.get("previous")
            base = {identifier for identifier in state["versions"] if identifier.endswith("-base")}
            if not target or target not in base | {state.get("previous")}:
                raise ValueError("Rollback must select the previous or base model")
            self._switch(state, target)
            return target

    def discard(self, identifier: str) -> None:
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            if identifier in {state["current"], state.get("previous")} or identifier.endswith("-base"):
                raise ValueError("Protected model versions cannot be discarded")
            state["versions"][identifier]["status"] = "discarded"
            state["versions"][identifier]["discarded_at"] = time.time()
            atomic_write(self.root / "registry.json", encode(state))

    def prune(self, confirmed_ids: list[str], keep: int = 3) -> list[str]:
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            protected = {state["current"], state.get("previous")} | {
                key for key in state["versions"] if key.endswith("-base")
            }
            available = sorted(
                (key for key in state["versions"] if key not in protected),
                key=lambda key: state["versions"][key]["created"],
                reverse=True,
            )
            eligible = set(available[max(0, keep - len(protected)) :])
            if len(set(confirmed_ids)) != len(confirmed_ids) or not set(confirmed_ids) <= eligible:
                raise ValueError("Retention keeps base, current, previous and the newest retained versions")
            removed = []
            for identifier in confirmed_ids:
                if identifier not in eligible:
                    raise ValueError(
                        "Retention keeps base, current, previous and the newest retained versions"
                    )
                shutil.rmtree(self.version_path(identifier))
                del state["versions"][identifier]
                removed.append(identifier)
            atomic_write(self.root / "registry.json", encode(state))
            return removed

    def cleanup_discarded(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with file_lock(self.root / ".registry.lock"):
            self._recover()
            state = self.read()
            protected = {state["current"], state.get("previous")} | {
                key for key in state["versions"] if key.endswith("-base")
            }
            removed = []
            for identifier, row in list(state["versions"].items()):
                if (
                    identifier not in protected
                    and row["status"] == "discarded"
                    and row.get("discarded_at", now) < now - 7 * 86400
                ):
                    shutil.rmtree(self.version_path(identifier))
                    del state["versions"][identifier]
                    removed.append(identifier)
            if removed:
                atomic_write(self.root / "registry.json", encode(state))
            return removed
