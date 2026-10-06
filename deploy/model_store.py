"""Install only hash-pinned GGUF data; never run downloaded code or replace current."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,119}\Z")


class InstallRefused(ValueError):
    """An installation cannot be safely completed."""


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise InstallRefused("Model downloads must remain on HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def catalogue(model: str, lock: Path = HERE / "models.lock") -> dict:
    data = json.loads(lock.read_text())
    if data.get("schema") != 1 or model not in data.get("models", {}) or not ID.fullmatch(model):
        raise InstallRefused("Model is not in the approved models.lock catalogue")
    entry = data["models"][model]
    if (
        not re.fullmatch(r"[a-f0-9]{40}", entry["revision"])
        or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
        or not 4 <= entry["size"] <= 10 * 1024**3
        or not 1 <= entry["ctx"] <= 6144
        or entry["capabilities"] != ["text"]
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repo"])
        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", entry["file"])
        or Path(entry["licence_file"]).name != entry["licence_file"]
    ):
        raise InstallRefused("Invalid models.lock entry")
    return entry


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
        or path.absolute() != path.resolve(strict=True)
    ):
        raise InstallRefused("Model storage must be a private owned directory without symlinks")


def copy_verified(source, destination: Path, entry: dict) -> None:
    digest = hashlib.sha256()
    total = 0
    magic = b""
    with destination.open("xb") as output:
        while block := source.read(1024 * 1024):
            if not magic:
                magic = block[:4]
            total += len(block)
            if total > entry["size"]:
                raise InstallRefused("Model exceeds its catalogue size")
            digest.update(block)
            output.write(block)
        if total != entry["size"] or digest.hexdigest() != entry["sha256"] or magic != b"GGUF":
            raise InstallRefused("Model size, SHA-256 or GGUF signature mismatch")
        os.fchmod(output.fileno(), 0o400)
        output.flush()
        os.fsync(output.fileno())


def verify_existing(path: Path, entry: dict) -> None:
    private_directory(path)
    descriptor = os.open(path / "model.gguf", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
            raise InstallRefused("Existing model integrity check failed")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != entry["sha256"] or read_regular(path / "model.sha256", 128).strip() != digest:
        raise InstallRefused("Existing model integrity check failed")
    manifest = json.loads(read_regular(path / "manifest.json", 64 * 1024))
    if manifest["sha256"] != digest or manifest["size"] != entry["size"] or manifest["id"] != path.name:
        raise InstallRefused("Existing model manifest disagrees with the catalogue")


def read_regular(path: Path, limit: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise InstallRefused("Invalid model metadata file")
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise InstallRefused("Model metadata exceeds its size limit")
        return value.decode()


def write_private(path: Path, content: str) -> None:
    with path.open("x") as stream:
        stream.write(content)
        os.fchmod(stream.fileno(), 0o400)
        stream.flush()
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def runtime_version() -> dict[str, str]:
    path = HERE / "VERSION"
    if not path.exists():
        path = HERE.parent / "docker/model/VERSION"
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def load_or_recover_registry(root: Path, versions: Path, current: Path, registry: Path) -> dict | None:
    """Return None for a fresh root; load or repair registry/current; refuse broken trees."""
    has_current = current.exists() or current.is_symlink()
    has_registry = registry.exists() or registry.is_symlink()
    if not has_current and not has_registry:
        return None
    if has_registry and registry.is_symlink():
        raise InstallRefused("Incomplete model registry; refusing to alter current")
    if has_current and not current.is_symlink():
        raise InstallRefused("Incomplete model registry; refusing to alter current")

    if has_registry:
        if not registry.is_file():
            raise InstallRefused("Incomplete model registry; refusing to alter current")
        state = json.loads(read_regular(registry, 1024 * 1024))
        current_id = state.get("current")
        if not isinstance(current_id, str) or not ID.fullmatch(current_id):
            raise InstallRefused("Incomplete model registry; refusing to alter current")
        if not (versions / current_id).is_dir():
            raise InstallRefused("Incomplete model registry; refusing to alter current")
        if not has_current:
            current.symlink_to(f"versions/{current_id}")
            sync_directory(root)
        elif current.resolve() != versions / current_id:
            raise InstallRefused("Model registry and current symlink disagree")
        return state

    # current without registry: rebuild from the symlink target
    link = current.readlink()
    if link.is_absolute() or len(link.parts) != 2 or link.parts[0] != "versions":
        raise InstallRefused("Incomplete model registry; refusing to alter current")
    current_id = link.parts[1]
    if not ID.fullmatch(current_id) or current.resolve() != versions / current_id:
        raise InstallRefused("Incomplete model registry; refusing to alter current")
    if not (versions / current_id).is_dir():
        raise InstallRefused("Incomplete model registry; refusing to alter current")
    created = datetime.now(UTC).isoformat()
    state = {
        "current": current_id,
        "previous": None,
        "versions": {current_id: {"status": "active", "created": created}},
    }
    with tempfile.NamedTemporaryFile(mode="w", prefix=".registry-", dir=root, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(state, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, registry)
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)
    return state


def install(
    root: Path, model: str, entry: dict, *, source: Path | None = None, version_id: str | None = None
) -> str:
    private_directory(root)
    version_id = version_id or f"{model}-base"
    if not ID.fullmatch(version_id):
        raise InstallRefused("Invalid model version id")
    lock_fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        lock_info = os.fstat(lock.fileno())
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_uid != os.getuid():
            raise InstallRefused("Invalid installation lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        versions = root / "versions"
        versions.mkdir(mode=0o700, exist_ok=True)
        private_directory(versions)
        current, registry = root / "current", root / "registry.json"
        recovered = load_or_recover_registry(root, versions, current, registry)
        initial = recovered is None
        target = versions / version_id
        created = datetime.now(UTC).isoformat()
        if target.exists() or target.is_symlink():
            verify_existing(target, entry)
        else:
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            try:
                with contextlib.ExitStack() as stack:
                    if source is None:
                        url = f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/{quote(entry['file'])}"
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSRedirect())
                        input_stream = stack.enter_context(opener.open(url, timeout=60))
                    else:
                        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        input_stream = stack.enter_context(os.fdopen(descriptor, "rb"))
                        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                            raise InstallRefused("Source model must be a regular file")
                    copy_verified(input_stream, stage / "model.gguf", entry)
                write_private(stage / "model.sha256", entry["sha256"] + "\n")
                manifest = {
                    "id": version_id,
                    "base_model": {key: entry[key] for key in ("repo", "revision", "licence", "licence_url")},
                    "parent_version": None,
                    "quant": entry["quant"],
                    "sha256": entry["sha256"],
                    "size": entry["size"],
                    "ctx": entry["ctx"],
                    "capabilities": entry["capabilities"],
                    "dataset_id": None,
                    "prompt_version_ids": [],
                    "eval_summary": {"evaluated": False, "test_only": entry.get("test_only", False)},
                    "llama_cpp_build": runtime_version()["build"],
                    "created": created,
                }
                write_private(stage / "manifest.json", json.dumps(manifest, indent=2) + "\n")
                write_private(
                    stage / "LICENSE", (HERE / "model_licenses" / entry["licence_file"]).read_text()
                )
                write_private(stage / "NOTICE", entry["notice"] + "\n")
                write_private(
                    stage / "MODEL_CARD.md",
                    f"# {version_id}\n\nSource: {entry['repo']} at {entry['revision']}.\n\n"
                    f"Licence: {entry['licence']}. Quantisation: {entry['quant']}. SHA-256: {entry['sha256']}.\n\n"
                    "Downloaded base; no project fine-tuning or personal training data. "
                    "Project evaluations have not been run. Model output is untrusted; "
                    "the application must enforce tool permissions in code.\n\n"
                    + (
                        "CI fixture only; unsuitable as an assistant.\n"
                        if entry.get("test_only")
                        else "Intended for Roland's private single-user agent; not evaluated for other uses.\n"
                    ),
                )
                sync_directory(stage)
                stage.rename(target)
                sync_directory(versions)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        if initial:
            state = {"current": version_id, "previous": None, "versions": {}}
        else:
            state = recovered if recovered is not None else json.loads(read_regular(registry, 1024 * 1024))
        state["versions"].setdefault(
            version_id,
            {"status": "active" if state["current"] == version_id else "available", "created": created},
        )
        with tempfile.NamedTemporaryFile(mode="w", prefix=".registry-", dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Symlink before registry on first install so a crash cannot leave
            # registry.json without current (previously unrecoverable).
            if initial and not current.exists() and not current.is_symlink():
                current.symlink_to(f"versions/{version_id}")
                sync_directory(root)
            os.replace(temporary, registry)
            sync_directory(root)
        finally:
            temporary.unlink(missing_ok=True)
    return version_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "install"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--file", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/models"))
    args = parser.parse_args()
    try:
        if (args.command == "install") != (args.file is not None):
            raise InstallRefused("install requires --file; fetch does not accept it")
        entry = catalogue(args.model)
        if entry.get("test_only") and os.environ.get("MODEL_INSTALL_TEST_ONLY") != "true":
            raise InstallRefused("Test-only models require an explicit CI fixture opt-in")
        version_id = install(args.root, args.model, entry, source=args.file)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Download errors can contain signed URLs. Print the class, never exception values.
        message = (
            str(error)
            if isinstance(error, InstallRefused)
            else f"Model installation failed ({type(error).__name__})"
        )
        print(message, file=sys.stderr)
        return 1
    print(f"Verified model installed: {version_id}. Existing current model was not replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
