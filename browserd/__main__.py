"""python -m browserd              start the screen and the service, and keep them running
python -m browserd serve        the service only (the launcher runs this)
python -m browserd healthcheck  exit 0 when the service and its browser are up"""

from __future__ import annotations

import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "healthcheck":
        from .server import healthcheck_cli

        raise SystemExit(0 if healthcheck_cli() else 1)
    if command == "serve":
        from .server import serve

        serve()
        return
    if command:
        raise SystemExit("Usage: python -m browserd [serve|healthcheck]")
    from .launcher import run

    raise SystemExit(run())


if __name__ == "__main__":
    main()
