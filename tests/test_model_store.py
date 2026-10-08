"""Verified installation, interrupted writes and supervisor process ownership."""

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("model_store", ROOT / "deploy/model_store.py")
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "models"
    root.mkdir(mode=0o700)
    source = tmp_path / "input.gguf"
    payload = b"GGUF" + b"synthetic-test-weights" * 8
    source.write_bytes(payload)
    entry = store.catalogue("test-tiny") | {
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    return root, source, entry


def test_install_is_atomic_private_and_preserves_existing_current(fixture):
    root, source, entry = fixture
    a = store.install(root, "test-tiny", entry, source=source)
    b = store.install(root, "test-tiny", entry, source=source, version_id="test-tiny-second")
    assert root.joinpath("current").readlink() == Path("versions") / a
    registry = json.loads(root.joinpath("registry.json").read_text())
    assert registry["current"] == a and registry["previous"] is None
    assert registry["versions"][a]["status"] == "active"
    assert registry["versions"][b]["status"] == "available"
    manifest = json.loads((root / "versions" / a / "manifest.json").read_text())
    assert manifest["sha256"] == entry["sha256"] and manifest["eval_summary"]["evaluated"] is False
    for version in (a, b):
        folder = root / "versions" / version
        assert folder.stat().st_mode & 0o777 == 0o700
        assert all(file.stat().st_mode & 0o777 == 0o400 for file in folder.iterdir())
        assert (folder / "LICENSE").read_text().startswith("MIT License")
    assert store.install(root, "test-tiny", entry, source=source) == a
    assert not list(root.glob(".install-*"))


@pytest.mark.parametrize("damage", ["hash", "size", "magic", "source_symlink", "source_fifo"])
def test_failed_copy_leaves_no_version_or_current(fixture, damage):
    root, source, entry = fixture
    if damage == "hash":
        entry["sha256"] = "0" * 64
    elif damage == "size":
        entry["size"] -= 1
    elif damage == "magic":
        payload = b"BAD!" + source.read_bytes()[4:]
        source.write_bytes(payload)
        entry["sha256"] = hashlib.sha256(payload).hexdigest()
    elif damage == "source_symlink":
        linked = source.with_suffix(".link")
        linked.symlink_to(source)
        source = linked
    else:
        source.unlink()
        os.mkfifo(source)
    with pytest.raises((store.InstallRefused, OSError)):
        store.install(root, "test-tiny", entry, source=source)
    assert not (root / "current").exists() and not (root / "registry.json").exists()
    assert not list(root.glob(".install-*")) and not list(root.joinpath("versions").iterdir())


def test_existing_tampered_model_is_refused(fixture):
    root, source, entry = fixture
    version = store.install(root, "test-tiny", entry, source=source)
    weights = root / "versions" / version / "model.gguf"
    weights.chmod(0o600)
    weights.write_bytes(b"tampered")
    with pytest.raises(store.InstallRefused):
        store.install(root, "test-tiny", entry, source=source)
    assert weights.read_bytes() == b"tampered"


def test_installer_refuses_storage_symlinks_and_incomplete_registry(fixture):
    root, source, entry = fixture
    (root / "registry.json").write_text('{"current": "unknown"}')
    with pytest.raises(store.InstallRefused):
        store.install(root, "test-tiny", entry, source=source)
    (root / "registry.json").unlink()
    (root / "versions").rmdir()
    (root / "versions").symlink_to(root.parent)
    with pytest.raises(store.InstallRefused):
        store.install(root, "test-tiny", entry, source=source)


def test_recovers_registry_without_current_symlink(fixture):
    root, source, entry = fixture
    version = store.install(root, "test-tiny", entry, source=source)
    (root / "current").unlink()
    assert not (root / "current").exists()
    second = store.install(root, "test-tiny", entry, source=source, version_id="test-tiny-second")
    assert (root / "current").resolve() == root / "versions" / version
    registry = json.loads((root / "registry.json").read_text())
    assert registry["current"] == version
    assert registry["versions"][second]["status"] == "available"



def test_refuses_dangling_current_without_version_dir(fixture):
    root, source, entry = fixture
    (root / "versions").mkdir(mode=0o700, exist_ok=True)
    (root / "current").symlink_to("versions/missing-base")
    with pytest.raises(store.InstallRefused, match="Incomplete model registry"):
        store.install(root, "test-tiny", entry, source=source)
    assert not (root / "registry.json").exists()


def test_recovers_current_without_registry(fixture):
    root, source, entry = fixture
    version = store.install(root, "test-tiny", entry, source=source)
    (root / "registry.json").unlink()
    second = store.install(root, "test-tiny", entry, source=source, version_id="test-tiny-second")
    registry = json.loads((root / "registry.json").read_text())
    assert registry["current"] == version
    assert (root / "current").resolve() == root / "versions" / version
    assert registry["versions"][second]["status"] == "available"


def test_first_install_symlink_survives_missing_registry(fixture):
    """Crash after symlink / before registry must be recoverable (new publish order)."""
    root, source, entry = fixture
    version = store.install(root, "test-tiny", entry, source=source)
    # Simulate the recoverable half-state the old order could leave, and the
    # new order intentionally prefers: current present, registry absent.
    (root / "registry.json").unlink()
    assert (root / "current").is_symlink()
    again = store.install(root, "test-tiny", entry, source=source)
    assert again == version
    registry = json.loads((root / "registry.json").read_text())
    assert registry["current"] == version and registry["versions"][version]["status"] == "active"


def test_model_sh_install_uses_network_none():
    script = Path(__file__).resolve().parents[1] / "deploy" / "model.sh"
    body = script.read_text()
    assert "--network none" in body
    assert 'if [[ $1 == fetch ]]; then' in body


@pytest.mark.parametrize("model", ["missing", "../test-tiny", "/test-tiny"])
def test_catalogue_refuses_unknown_ids(model):
    with pytest.raises(store.InstallRefused):
        store.catalogue(model)


def test_download_errors_do_not_print_signed_urls(monkeypatch, capsys, fixture):
    root, _, _ = fixture
    monkeypatch.setattr("sys.argv", ["model_store", "fetch", "--model", "qwen3-4b-q4km", "--root", str(root)])

    def fail(*args, **kwargs):
        raise OSError("https://download.test/?signature=synthetic-private-value")

    monkeypatch.setattr(store, "install", fail)
    assert store.main() == 1
    assert "signature" not in capsys.readouterr().err


def supervisor(tmp_path, root):
    token = tmp_path / "token"
    token.write_text("synthetic-token-for-unit-test-only")
    log = tmp_path / "starts.jsonl"
    server = tmp_path / "fake-server"
    server.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "with open(os.environ['MODEL_TEST_LOG'], 'a') as f: f.write(json.dumps({'pid':os.getpid(),'args':sys.argv[1:]})+'\\n')\n"
        "while True: time.sleep(0.05)\n"
    )
    server.chmod(0o700)
    process = subprocess.Popen(
        ["bash", str(ROOT / "docker/model/run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ
        | {
            "MODEL_ROOT": str(root),
            "MODEL_SERVER_BIN": str(server),
            "MODEL_SERVER_TOKEN_FILE": str(token),
            "MODEL_TEST_LOG": str(log),
        },
    )
    return process, log


def wait_for_starts(process, log, count, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists():
            rows = [json.loads(line) for line in log.read_text().splitlines()]
            if len(rows) >= count:
                return rows
        if process.poll() is not None:
            raise AssertionError("Supervisor exited before its verified server started")
        time.sleep(0.05)
    raise AssertionError("Supervisor did not restart within its polling window")


def test_supervisor_reloads_current_and_reaps_children(fixture, tmp_path):
    root, source, entry = fixture
    a = store.install(root, "test-tiny", entry, source=source)
    b = store.install(root, "test-tiny", entry, source=source, version_id="test-tiny-second")
    process, log = supervisor(tmp_path, root)
    try:
        rows = wait_for_starts(process, log, 1, 3)
        assert str(root / "versions" / a / "model.gguf") in rows[0]["args"]
        replacement = root / "next"
        replacement.symlink_to(f"versions/{b}")
        replacement.replace(root / "current")
        rows = wait_for_starts(process, log, 2, 20)
        assert str(root / "versions" / b / "model.gguf") in rows[1]["args"]
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == 0
        for row in rows:
            with pytest.raises(ProcessLookupError):
                os.kill(row["pid"], 0)
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3)


def test_supervisor_refuses_bad_sha_before_starting(fixture, tmp_path):
    root, source, entry = fixture
    version = store.install(root, "test-tiny", entry, source=source)
    sha = root / "versions" / version / "model.sha256"
    sha.chmod(0o600)
    sha.write_text("0" * 64)
    process, log = supervisor(tmp_path, root)
    _, error = process.communicate(timeout=3)
    assert process.returncode != 0 and "SHA-256 mismatch" in error and not log.exists()


def test_supervisor_passes_cache_ram_0_to_the_server(fixture, tmp_path):
    # llama-server's host-RAM prompt cache defaults to 8192 MiB and grows with
    # each new chat until the 3840m cgroup kills the model; it must stay off.
    root, source, entry = fixture
    store.install(root, "test-tiny", entry, source=source)
    process, log = supervisor(tmp_path, root)
    try:
        args = wait_for_starts(process, log, 1, 3)[0]["args"]
        assert args[args.index("--cache-ram") + 1] == "0"
        assert args.count("--cache-ram") == 1
        assert args[args.index("--cache-reuse") + 1] == "256"
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
