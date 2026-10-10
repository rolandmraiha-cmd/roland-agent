"""Host UI for model operations; mutations require APPLY and typed version confirmation."""

import argparse
import asyncio
import os
import time
from pathlib import Path

import httpx

from ..audit import Audit
from ..eval.runner import evaluate, load_cases
from ..memory import Memory
from .llamacpp import LlamaCppBrain
from .registry import ROOT, Registry


async def check_health(identifier: str, server: str, token: str, *, timeout: float = 300) -> bool:
    brain = LlamaCppBrain(server, "current", token, temperature=0, seed=42, timeout=20)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = await brain.client.get(brain.server_root + "/props")
                response.raise_for_status()
                props = response.json()
                path = str(props.get("model_path") or props.get("model_alias") or "")
                # Never accept the old server merely because it still answers during reload.
                if identifier not in Path(path).parts:
                    await asyncio.sleep(2)
                    continue
                cases = [case for case in load_cases(ROOT / "agent/eval/cases") if case["critical"]][:20]
                result = await asyncio.wait_for(
                    evaluate(brain, cases, version_id=identifier),
                    timeout=max(0.1, deadline - time.monotonic()),
                )
                return result["critical_failures"] == 0
            except TimeoutError:
                return False
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(2)
        return False
    finally:
        await brain.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["list", "import", "promote", "rollback"])
    parser.add_argument("--root", type=Path, default=Path("/models"))
    args = parser.parse_args()
    registry = Registry(args.root)
    if args.command == "list":
        import json

        print(json.dumps(registry.read(), indent=2))
        return
    if os.getenv("APPLY") != "1":
        raise SystemExit("Model changes require APPLY=1")
    memory = Memory(Path(os.getenv("DATA_DIR", "/data")) / "agent.db")
    audit = Audit(memory, max_bytes=8192)
    if args.command == "import":
        result = registry.import_candidate(Path(os.environ["FILE"]))
        audit.write(
            "roland",
            "candidate_imported",
            detail={"version_id": result["version_id"], "status": result["status"]},
        )
        # Host import creates the same pending review record as a trainer upload.
        from types import SimpleNamespace

        from ..training.loop import record_candidate

        print(record_candidate(SimpleNamespace(memory=memory, audit=audit), result))
        return
    state = registry.read()
    identifier = os.getenv("ID") or state.get("previous")
    registry.version_path(identifier)
    if input("Type the full model version id to confirm: ") != identifier:
        raise SystemExit("Confirmation did not match; no model changed")
    force = os.getenv("FORCE") == "1"
    if args.command == "promote":
        manifest = __import__("json").loads((registry.version_path(identifier) / "manifest.json").read_text())
        registry.promote(identifier, manifest["sha256"], requested_by="cli", force=force)
        token = Path(os.environ["MODEL_SERVER_TOKEN_FILE"]).read_text().strip()
        healthy = asyncio.run(check_health(identifier, "http://10.77.6.60:8080", token))
        if not healthy:
            registry.rollback(requested_by="automatic_health_failure")
            audit.write("system", "model_rollback_auto", detail={"version_id": identifier})
            raise SystemExit("Health/smoke failed; the previous model was restored")
        audit.write(
            "roland", "model_promote_forced" if force else "model_promoted", detail={"version_id": identifier}
        )
    else:
        registry.rollback(requested_by="cli", to_version=identifier)
        audit.write("roland", "model_rollback", detail={"version_id": identifier})


if __name__ == "__main__":
    main()
