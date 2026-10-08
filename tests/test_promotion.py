"""A8.7 imports, single-use human requests and compensating health failures."""

import asyncio
import io
import os
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from training_fixtures import candidate, registry, regressed

from agent.training.files import atomic_write, encode
from agent.training.tokens import consume, mint

TOKEN = "synthetic-trainer-token-" + "x" * 32
posix = pytest.mark.skipif(os.name != "posix", reason="Production registry uses POSIX locks and symlinks")


@posix
def test_import_never_switches_then_human_promote_and_rollback(tmp_path):
    store = registry(tmp_path / "models")
    archive, digest = candidate(tmp_path)
    assert store.import_candidate(archive)["status"] == "candidate"
    assert store.read()["current"] == "test-base"
    with pytest.raises(PermissionError):
        store.promote("candidate-a", digest, requested_by="agent")
    assert store.promote("candidate-a", digest, requested_by="roland") == "test-base"
    assert (store.root / "current").readlink().as_posix() == "versions/candidate-a"
    with pytest.raises(ValueError, match="changed"):
        store.rollback(requested_by="roland", expected_current="other-model")
    assert store.rollback(requested_by="roland") == "test-base"


@posix
def test_regression_cannot_be_promoted_by_ui(tmp_path):
    store = registry(tmp_path / "models")
    archive, digest = candidate(tmp_path, mutate=regressed)
    assert store.import_candidate(archive)["status"] == "rejected_auto"
    with pytest.raises(ValueError, match="passing"):
        store.promote("candidate-a", digest, requested_by="roland")
    with pytest.raises(PermissionError):
        store.promote("candidate-a", digest, requested_by="roland", force=True)
    assert store.read()["current"] == "test-base"


@posix
@pytest.mark.parametrize("fault", ["../outside", "/absolute", "symlink", "duplicate"])
def test_archive_paths_links_and_duplicates_are_refused(tmp_path, fault):
    store = registry(tmp_path / "models")
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("model.gguf" if fault in {"symlink", "duplicate"} else fault)
        member.size = 4
        if fault == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
        archive.addfile(member, io.BytesIO(b"GGUF"))
        if fault == "duplicate":
            archive.addfile(member, io.BytesIO(b"GGUF"))
    with pytest.raises(ValueError, match="Unsafe"):
        store.import_candidate(path)
    assert store.read()["current"] == "test-base" and not (tmp_path / "outside").exists()


@posix
@pytest.mark.parametrize(
    "key,value",
    [
        ("sha256", "0" * 64),
        ("llama_cpp_build", "unreviewed"),
        ("parent_version", "old-baseline"),
        ("dry_run", True),
    ],
)
def test_invalid_candidate_provenance_refused(tmp_path, key, value):
    store = registry(tmp_path / "models")
    archive, _ = candidate(tmp_path, mutate=lambda manifest, _files: manifest.update({key: value}))
    with pytest.raises(ValueError):
        store.import_candidate(archive)
    assert store.read()["current"] == "test-base"


@posix
def test_registry_write_failure_restores_pointer(tmp_path, monkeypatch):
    import agent.models.registry as module

    store = registry(tmp_path / "models")
    archive, digest = candidate(tmp_path)
    store.import_candidate(archive)
    store._point("test-base")
    original = module.atomic_write

    def fail(path, value):
        if path.name == "registry.json":
            raise OSError("synthetic disk failure")
        original(path, value)

    monkeypatch.setattr(module, "atomic_write", fail)
    with pytest.raises(OSError):
        store.promote("candidate-a", digest, requested_by="roland")
    assert store.read()["current"] == "test-base"
    assert (store.root / "current").readlink().as_posix() == "versions/test-base"
    assert not (store.root / ".switch.json").exists()


@posix
def test_recovery_completes_interrupted_switch(tmp_path):
    store = registry(tmp_path / "models")
    archive, _ = candidate(tmp_path)
    store.import_candidate(archive)
    old = store.read()
    new = {**old, "current": "candidate-a", "previous": "test-base"}
    atomic_write(store.root / ".switch.json", encode({"old_state": old, "new_state": new}))
    store._recover()
    assert store.read()["current"] == "candidate-a"
    assert (store.root / "current").readlink().as_posix() == "versions/candidate-a"


def test_human_token_bound_to_purpose_id_hash_and_expires(tmp_path, monkeypatch):
    token = mint(TOKEN, "promote", "candidate", "a" * 64)
    for purpose, identifier, digest in [
        ("rollback", "candidate", "a" * 64),
        ("promote", "other", "a" * 64),
        ("promote", "candidate", "b" * 64),
    ]:
        with pytest.raises(PermissionError):
            consume(TOKEN, token, purpose, identifier, digest, tmp_path)
    with ThreadPoolExecutor(2) as pool:

        def once():
            try:
                consume(TOKEN, token, "promote", "candidate", "a" * 64, tmp_path)
                return True
            except PermissionError:
                return False

        assert sum(pool.map(lambda _index: once(), range(2))) == 1
    import agent.training.tokens as module

    now = time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + 121)
    with pytest.raises(PermissionError):
        consume(TOKEN, token, "promote", "candidate", "a" * 64, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True, "cancel"])
async def test_health_smoke_failure_or_cancel_restores_previous(make_agent, monkeypatch, failure):
    import agent.training.promotion as module

    agent = make_agent(trainer_url="http://10.77.7.70:7200", trainer_api_token=TOKEN)
    agent.memory._exec(
        "INSERT INTO model_promotions(id,version_id,from_version_id,status,created) "
        "VALUES ('mp_test','candidate','test-base','promoting',?)",
        (time.time(),),
    )
    calls = []

    class Client:
        def __init__(self, _config):
            pass

        async def request(self, method, path, *, body=None):
            calls.append((method, path, body))
            return {"current": "candidate"}

    async def health(*_args):
        if failure == "cancel":
            raise asyncio.CancelledError
        return not failure

    monkeypatch.setattr(module, "TrainerClient", Client)
    monkeypatch.setattr(module, "check_health", health)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await module.finish_promotion(agent, "mp_test")
    else:
        assert await module.finish_promotion(agent, "mp_test") is not failure
    status = agent.memory._all("SELECT status FROM model_promotions")[0][0]
    assert status == ("rolled_back_auto" if failure else "promoted")
    assert any(path == "/v1/rollback" for _method, path, _body in calls) is bool(failure)
