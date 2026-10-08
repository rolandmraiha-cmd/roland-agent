"""Local-server evaluation entry point. Does not stop or modify the production service."""

import argparse
import asyncio
import os
from pathlib import Path

from ..models.llamacpp import LlamaCppBrain
from ..training.files import atomic_write, encode
from .runner import evaluate, load_cases


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--server", required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", default="candidate")
    parser.add_argument("--critical-only", action="store_true")
    args = parser.parse_args()
    token_path = os.getenv("MODEL_SERVER_TOKEN_FILE")
    token = Path(token_path).read_text().strip() if token_path else ""
    brain = LlamaCppBrain(args.server, "current", token, temperature=0, seed=42)
    try:
        cases = load_cases(args.cases)
        if args.critical_only:
            cases = [case for case in cases if case["critical"]][:20]
        atomic_write(args.out, encode(await evaluate(brain, cases, version_id=args.version)))
    finally:
        await brain.aclose()


if __name__ == "__main__":
    asyncio.run(main())
