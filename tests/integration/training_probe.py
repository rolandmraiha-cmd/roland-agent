"""Disposable CI: real tiny GGUF loading, supervisor swaps and corrupt-model rollback.

Passing reports below are explicit test fixtures for exercising the switch mechanism.
The pipeline's real tiny-model report remains in candidate.tar and is never promoted.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

from agent.eval.runner import load_cases, suite_hash
from agent.models.registry import ROOT, Registry
from agent.training.files import atomic_write, encode, sha256
from agent.training.tokens import mint
from trainerd.server import create_app


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180)


def passing(version):
    cases = load_cases(ROOT / "agent/eval/cases")
    return {
        "version_id": version,
        "suite_sha256": suite_hash(cases),
        "critical_failures": 0,
        **dict.fromkeys(
            (
                "protocol_validity",
                "args_validity",
                "tool_call_accuracy",
                "gate_compliance",
                "injection_refusal",
            ),
            1.0,
        ),
        "cases": [
            {
                "id": row["id"],
                "category": row["category"],
                "critical": row["critical"],
                "passed": True,
                "protocol": True,
                "args_valid": True,
            }
            for row in cases
        ],
    }


def main(archive_path):
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise SystemExit("Restricted to disposable CI; never runs against a production stack")
    name = "roland-training-ci-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    image = dict(line.split("=", 1) for line in (ROOT / "docker/model/VERSION").read_text().splitlines())[
        "image"
    ]
    with tempfile.TemporaryDirectory() as temporary:
        scratch = Path(temporary)
        scratch.chmod(0o755)
        unpacked = scratch / "unpacked"
        unpacked.mkdir()
        with tarfile.open(archive_path) as bundle:
            bundle.extractall(unpacked, filter="data")
        assert (unpacked / "model.gguf").read_bytes()[:4] == b"GGUF"
        assert (
            sha256(unpacked / "model.gguf") == json.loads((unpacked / "manifest.json").read_text())["sha256"]
        )
        metrics = json.loads((unpacked / "train-metrics.json").read_text())
        assert metrics["dry_run"] and metrics["sft"] and metrics["dpo"]
        models = scratch / "models"
        base = models / "versions/ci-tiny-base"
        shutil.copytree(unpacked, base)
        atomic_write(
            models / "registry.json",
            encode(
                {
                    "current": "ci-tiny-base",
                    "previous": None,
                    "versions": {"ci-tiny-base": {"status": "active", "created": 1}},
                }
            ),
        )
        store = Registry(models, allow_test_models=True)
        store._point("ci-tiny-base")
        token = "synthetic_ci_only_model_token_" + "x" * 32
        token_file = scratch / "token"
        token_file.write_text(token)
        token_file.chmod(0o644)
        trainer_token = "synthetic_ci_only_trainer_token_" + "x" * 32
        trainer_app = create_app(
            {"models": str(models), "runs": str(scratch / "runs"), "max_mb": 32}, trainer_token
        )

        def trainer_request(path, *, status=200, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=trainer_app, client=("10.77.7.10", 1234))
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://trainer",
                    headers={"Authorization": "Bearer " + trainer_token},
                ) as client:
                    return await client.post(path, **kwargs)

            response = asyncio.run(send())
            assert response.status_code == status, (path, response.status_code, response.text)
            return response.json()

        def promote(result, *, authorized=True):
            identifier, digest = result["version_id"], result["sha256"]
            return trainer_request(
                "/v1/promote",
                status=200 if authorized else 403,
                json={
                    "version_id": identifier,
                    "sha256": digest,
                    "request_token": mint(trainer_token, "promote", identifier, digest) if authorized else "",
                },
            )

        def share():
            # Synthetic fixtures only: readable by production uid 1000, writable by CI runner.
            for item in [models, *models.rglob("*")]:
                if not item.is_symlink():
                    item.chmod(0o755 if item.is_dir() else 0o644)

        def add(identifier, corrupt=False):
            directory = scratch / identifier
            shutil.copytree(unpacked, directory)
            if corrupt:
                atomic_write(directory / "model.gguf", b"GGUFinvalid payload for rollback test")
                atomic_write(directory / "model.sha256", sha256(directory / "model.gguf"))
            manifest = json.loads((directory / "manifest.json").read_text())
            manifest.update(
                id=identifier,
                parent_version="ci-tiny-base",
                dry_run=False,
                sha256=sha256(directory / "model.gguf"),
                size=(directory / "model.gguf").stat().st_size,
            )
            atomic_write(
                directory / "eval-report.json",
                encode({"current": passing("ci-tiny-base"), "candidate": passing(identifier)}),
            )
            manifest["artifacts_sha256"] = {
                str(item.relative_to(directory)): sha256(item)
                for item in directory.rglob("*")
                if item.is_file() and item.name != "manifest.json"
            }
            atomic_write(directory / "manifest.json", encode(manifest))
            output = scratch / (identifier + ".tar")
            with tarfile.open(output, "w") as bundle:
                for item in directory.rglob("*"):
                    if item.is_file():
                        bundle.add(item, arcname=str(item.relative_to(directory)))
            return trainer_request("/v1/import", content=output.read_bytes())

        def serving(identifier, timeout=180):
            deadline = time.monotonic() + timeout
            with httpx.Client(
                base_url="http://10.77.6.60:8080",
                trust_env=False,
                timeout=10,
                headers={"Authorization": "Bearer " + token},
            ) as client:
                while time.monotonic() < deadline:
                    try:
                        props = client.get("/props")
                        if (
                            props.status_code == 200
                            and identifier in Path(props.json().get("model_path", "")).parts
                        ):
                            schema = {
                                "type": "object",
                                "properties": {"action": {"const": "reply"}, "text": {"const": "ok"}},
                                "required": ["action", "text"],
                                "additionalProperties": False,
                            }
                            response = client.post(
                                "/v1/chat/completions",
                                json={
                                    "messages": [{"role": "user", "content": "Reply ok."}],
                                    "max_tokens": 128,
                                    "temperature": 0,
                                    "seed": 42,
                                    "response_format": {
                                        "type": "json_schema",
                                        "json_schema": {"name": "action", "schema": schema},
                                    },
                                },
                            )
                            return response.status_code == 200 and json.loads(
                                response.json()["choices"][0]["message"]["content"]
                            ) == {"action": "reply", "text": "ok"}
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(1)
            return False

        share()
        try:
            run("docker", "network", "create", "--internal", "--subnet", "10.77.6.0/24", name)
            run(
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                name,
                "--ip",
                "10.77.6.60",
                "--restart",
                "unless-stopped",
                "--user",
                "1000:1000",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "2",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:size=16m",
                "-v",
                str(models) + ":/models:ro",
                "-v",
                str(ROOT / "docker/model/run.sh") + ":/run-model.sh:ro",
                "-v",
                str(token_file) + ":/run/secrets/model_server_token:ro",
                "--entrypoint",
                "bash",
                image,
                "/run-model.sh",
            )
            assert serving("ci-tiny-base"), "Actual CPU-trained GGUF did not load in the production image"
            started = run("docker", "inspect", "--format", "{{.State.StartedAt}}", name).stdout
            result = add("ci-tiny-b")
            share()
            assert store.read()["current"] == "ci-tiny-base", "Import switched the model"
            promote(result, authorized=False)
            assert store.read()["current"] == "ci-tiny-base", "Missing human token switched the model"
            promote(result)
            share()
            assert serving("ci-tiny-b"), "Supervisor did not reload B"
            assert run("docker", "inspect", "--format", "{{.State.StartedAt}}", name).stdout == started
            trainer_request(
                "/v1/rollback",
                json={
                    "version_id": "ci-tiny-base",
                    "from_version_id": "ci-tiny-b",
                    "request_token": mint(trainer_token, "rollback", "ci-tiny-base", "ci-tiny-b"),
                },
            )
            share()
            assert serving("ci-tiny-base"), "Rollback did not restore A"
            assert run("docker", "inspect", "--format", "{{.State.StartedAt}}", name).stdout == started
            result = add("ci-corrupt-b", corrupt=True)
            share()
            promote(result)
            share()
            assert not serving("ci-corrupt-b", timeout=35), "Corrupt model unexpectedly served"
            store.rollback(requested_by="automatic_health_failure", expected_current="ci-corrupt-b")
            share()
            assert serving("ci-tiny-base"), "Failed-model rollback did not restore serving"
            print(
                "CPU SFT/DPO artifact loaded; authenticated API import, A/B swap, "
                "missing-human-token refusal and corrupt-B rollback passed."
            )
        except BaseException:
            diagnostics = subprocess.run(
                ["docker", "logs", "--tail", "80", name], capture_output=True, text=True, check=False
            )
            print(diagnostics.stdout + diagnostics.stderr, file=sys.stderr)
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            subprocess.run(["docker", "network", "rm", name], capture_output=True, check=False)


if __name__ == "__main__":
    main(sys.argv[1])
