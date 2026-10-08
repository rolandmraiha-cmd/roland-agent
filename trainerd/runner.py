"""SSH orchestration with pinned keys, bounded runtime and teardown on every exit."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from agent.models.modelreg import ID
from agent.training.files import atomic_write

TARGET = re.compile(r"^[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
RUN_ID = re.compile(r"^tr_[a-f0-9]{32}$")
LOG = logging.getLogger(__name__)


async def process(argv: list[str], *, timeout: float, cwd=None, env=None) -> bytes:
    child = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:

        async def collect():
            output = b""
            while block := await child.stdout.read(8192):
                output = (output + block)[-64 * 1024 :]
            await child.wait()
            return output

        output = await asyncio.wait_for(collect(), timeout)
        if child.returncode:
            raise ValueError("Training helper failed")
        return output
    except BaseException:
        if child.returncode is None:
            import signal

            if os.name == "posix":
                os.killpg(child.pid, signal.SIGTERM)
            else:
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 10)
            except TimeoutError:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
                await child.wait()
        raise


async def launch(config: dict, run: dict, directory: Path, *, run_process=process) -> Path:
    mode, target, provisioned = run["mode"], config.get("target", ""), False
    provider = config.get("provider", "")
    source = Path(config["source"])
    known = directory / "known_hosts"
    if not RUN_ID.fullmatch(run["id"]):
        raise ValueError("Invalid training run id")
    remote = "roland-training-" + run["id"]
    if not 1 <= config["max_hours"] <= 24:
        raise ValueError("TRAINING_MAX_HOURS must be 1..24")
    deadline = asyncio.get_running_loop().time() + config["max_hours"] * 3600
    env = dict(os.environ)
    env["TRAINING_PROVIDER_TOKEN_FILE"] = config.get(
        "provider_token_file", "/run/secrets/training_provider_token"
    )
    env["TRAINING_RUN_ID"] = run["id"]
    env["TRAINING_RUN_DIR"] = str(directory)

    async def call(argv, **kwargs):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Training time cap reached")
        return await run_process(argv, timeout=remaining, **kwargs)

    try:
        if mode == "hook":
            if not PROVIDER.fullmatch(provider):
                raise ValueError("An explicit provider hook is required")
            hook = source / "training/providers" / provider
            if hook.is_symlink() or not hook.resolve().is_relative_to(
                (source / "training/providers").resolve()
            ):
                raise ValueError("Invalid provider hook directory")
            # Once provision starts, teardown runs even if it exits without a usable address.
            provisioned = True
            output = (await call([str(hook / "provision")], env=env)).decode().splitlines()
            if len(output) != 2:
                raise ValueError("Provision must return target and a pinned host-key line")
            target = output[0]
            host_key = output[1]
        elif mode == "ssh":
            host_key = config.get("known_hosts", "")
        else:
            raise ValueError("Only ssh or hook launches remotely")
        if not TARGET.fullmatch(target):
            raise ValueError("SSH target must be a reviewed user@host")
        host = target.split("@", 1)[1]
        if not host_key or any(
            len(line.split()) < 3
            or line.split()[0] not in {host, "[" + host + "]:22"}
            or line.split()[1] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"}
            for line in host_key.splitlines()
        ):
            raise ValueError("A matching pinned SSH host key is required; no trust-on-first-use")
        atomic_write(known, host_key + "\n")
        key = config.get("ssh_key_file", "/run/secrets/training_ssh_key")
        if not Path(key).is_file() or Path(key).is_symlink():
            raise ValueError("Training SSH key must be a mounted regular secret file")
        ssh = [
            "ssh",
            "-i",
            key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UserKnownHostsFile=" + str(known),
            "-o",
            "ConnectTimeout=30",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "--",
        ]
        import shlex

        rsync_ssh = shlex.join(ssh[:-1])
        await call([*ssh, target, "mkdir -m 700 -- " + remote])
        for name in ("training", "agent", "docker/model/VERSION"):
            destination = remote + "/" + name.rsplit("/", 1)[0] if "/" in name else remote + "/"
            if "/" in name:
                await call([*ssh, target, "mkdir -p -- " + destination])
            await call(
                [
                    "rsync",
                    "-a",
                    "--safe-links",
                    "-e",
                    rsync_ssh,
                    "--",
                    str(source / name),
                    target + ":" + destination,
                ]
            )
        await call(
            [
                "rsync",
                "-a",
                "--safe-links",
                "-e",
                rsync_ssh,
                "--",
                str(Path(config["datasets"]) / run["dataset_id"]) + "/",
                target + ":" + remote + "/dataset/",
            ]
        )
        state = __import__("json").loads(Path(config["models"]).joinpath("registry.json").read_text())
        current = state["current"]
        if not ID.fullmatch(current):
            raise ValueError("Invalid current model version")
        await call(
            [
                "rsync",
                "-a",
                "--safe-links",
                "-e",
                rsync_ssh,
                "--",
                str(Path(config["models"]) / "versions" / current / "model.gguf"),
                target + ":" + remote + "/current.gguf",
            ]
        )
        # Constant bootstrap is shipped in the bundle. No provider-supplied shell source.
        await call(
            [
                *ssh,
                target,
                "cd "
                + remote
                + " && bash training/bootstrap.sh && . .training-venv/bin/activate && LLAMA_CPP_DIR=$PWD/.llama.cpp bash training/run_all.sh --dataset dataset --current-model current.gguf --current-version "
                + current
                + " --out result",
            ]
        )
        await call(
            [
                "rsync",
                "-a",
                "--safe-links",
                "-e",
                rsync_ssh,
                "--",
                target + ":" + remote + "/result/candidate.tar",
                str(directory / "candidate.tar"),
            ]
        )
        return directory / "candidate.tar"
    finally:
        # Cleanup is shielded from cancellation and has its own bounded allowance.
        try:
            if TARGET.fullmatch(target) and known.exists():
                cleanup_ssh = [
                    "ssh",
                    "-i",
                    config.get("ssh_key_file", "/run/secrets/training_ssh_key"),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    "UserKnownHostsFile=" + str(known),
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    target,
                    "rm -rf -- " + remote,
                ]
                task = asyncio.create_task(run_process(cleanup_ssh, timeout=60))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                except Exception as exc:
                    LOG.warning("Remote cleanup failed: %s", type(exc).__name__)
        finally:
            try:
                if provisioned:
                    task = asyncio.create_task(
                        run_process(
                            [str(source / "training/providers" / provider / "teardown"), target],
                            timeout=60,
                            env=env,
                        )
                    )
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
            finally:
                known.unlink(missing_ok=True)
