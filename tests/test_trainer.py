"""Peer authentication, bounded helpers and provider teardown on all exits."""

import asyncio
import sys

import httpx
import pytest
from training_fixtures import registry

from trainerd.runner import launch, process
from trainerd.server import create_app

TOKEN = "synthetic-trainer-token-" + "x" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer,bearer,expected",
    [
        ("10.77.7.10", TOKEN, 200),
        ("10.77.7.10", "bad", 403),
        ("10.77.13.10", TOKEN, 403),
        ("127.0.0.1", TOKEN, 403),
    ],
)
async def test_trainer_requires_peer_and_token(tmp_path, peer, bearer, expected):
    store = registry(tmp_path / "models")
    app = create_app({"models": str(store.root), "runs": str(tmp_path / "runs")}, TOKEN)
    transport = httpx.ASGITransport(app=app, client=(peer, 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://trainer") as client:
        response = await client.get("/healthz", headers={"Authorization": "Bearer " + bearer})
    assert response.status_code == expected


@pytest.mark.asyncio
async def test_helper_output_memory_and_timeout_are_bounded():
    result = await process([sys.executable, "-c", "import sys; sys.stdout.write('x'*5000000)"], timeout=10)
    assert len(result) == 64 * 1024
    with pytest.raises(TimeoutError):
        await process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["success", "provision", "train", "cancel", "cleanup"])
async def test_hook_teardown_runs_even_after_failure_and_cancellation(tmp_path, failure):
    source = tmp_path / "source"
    (source / "training/providers/example").mkdir(parents=True)
    key = tmp_path / "key"
    key.write_text("synthetic mounted private key")
    store = registry(tmp_path / "models")
    calls = []

    async def helper(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0].endswith("provision"):
            if failure == "provision":
                raise ValueError("synthetic provisioning failure")
            return b"gpu@public.example\npublic.example ssh-ed25519 AAAAtest\n"
        if "bash training/run_all.sh" in " ".join(argv):
            if failure == "train":
                raise ValueError("synthetic training failure")
            if failure == "cancel":
                raise asyncio.CancelledError
        if "rm -rf --" in " ".join(argv) and failure == "cleanup":
            raise ValueError("synthetic SSH cleanup failure")
        return b""

    config = {
        "source": str(source),
        "models": str(store.root),
        "datasets": str(tmp_path / "datasets"),
        "ssh_key_file": str(key),
        "provider": "example",
        "max_hours": 4,
    }
    run = {"id": "tr_" + "a" * 32, "mode": "hook", "dataset_id": "synthetic"}
    directory = tmp_path / "run"
    directory.mkdir()
    if failure in {"provision", "train"}:
        with pytest.raises(ValueError):
            await launch(config, run, directory, run_process=helper)
    elif failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await launch(config, run, directory, run_process=helper)
    else:
        await launch(config, run, directory, run_process=helper)
    teardown = [call for call in calls if call[0][0].endswith("teardown")]
    assert len(teardown) == 1
    assert teardown[0][1]["env"]["TRAINING_RUN_ID"] == run["id"]
    assert not (directory / "known_hosts").exists()
    for argv, _kwargs in calls:
        assert "StrictHostKeyChecking=no" not in " ".join(argv)


@pytest.mark.asyncio
async def test_ssh_without_pinned_host_key_never_connects(tmp_path):
    calls = []

    async def helper(argv, **_kwargs):
        calls.append(argv)
        return b""

    with pytest.raises(ValueError, match="pinned"):
        await launch(
            {"source": str(tmp_path), "target": "gpu@public.example", "max_hours": 4},
            {"id": "tr_" + "b" * 32, "mode": "ssh"},
            tmp_path,
            run_process=helper,
        )
    assert calls == []
