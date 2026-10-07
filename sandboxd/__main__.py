"""python -m sandboxd  |  python -m sandboxd healthcheck"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "healthcheck":
        from .server import healthcheck_cli

        raise SystemExit(0 if healthcheck_cli() else 1)
    from .server import serve

    serve()


if __name__ == "__main__":
    main()
