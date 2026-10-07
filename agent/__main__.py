"""Start the agent.

python -m agent            serve the web chat page (default)
python -m agent chat       chat in the terminal, for quick local tests
python -m agent run-jobs   run any due background jobs once and exit
python -m agent hash-password   make the AGENT_PASSWORD_HASH line for .env
python -m agent migrate --check   inspect schema versions without upgrading
python -m agent audit-verify   check the stored audit chain without changing it
python -m agent backup-now   take private local database/workspace snapshots
python -m agent restore <file.db.gz>   restore while all database users are stopped
python -m agent healthcheck   test the local HTTP health endpoint
python -m agent gen-token   print a new private service token
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import getpass
import ipaddress
import json
import logging
import secrets
import sqlite3
import sys
from pathlib import Path

import httpx

from .brain import make_brain
from .config import Config
from .core import Agent
from .memory import Memory
from .scheduler import run_due_jobs


def harden_process() -> None:
    """Stops other processes running as the same user from reading this process's memory or
    environment (like /proc/<pid>/environ, which holds the API key)."""
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(4, 0, 0, 0, 0) != 0:  # 4 = PR_SET_DUMPABLE
                logging.warning("prctl(PR_SET_DUMPABLE) failed")
        except (OSError, AttributeError):
            logging.warning("could not call prctl")


def build(*, validate: bool = False, config: Config | None = None) -> Agent:
    config = Config.from_env() if config is None else config
    if validate:
        config.check()  # Refuse unsafe settings before opening the DB or model client.
    else:
        config.check_model()  # Terminal chat and background jobs also stay local.
    if config.browser_enabled or config.screen_enabled:
        raise SystemExit("Browser and screen services are not implemented yet; keep their flags false")
    if config.audit_detail_max_bytes < 64:
        raise SystemExit("AUDIT_DETAIL_MAX_BYTES must be at least 64")
    if config.backup_dir is not None:
        from .backup import validate_backup_config

        validate_backup_config(config)
    memory = Memory(config.db_path, backup_dir=config.backup_dir if validate else None)
    brain = make_brain(config)
    return Agent(config, memory, brain)


async def terminal_chat(agent: Agent) -> None:
    chat_id = agent.memory.new_chat("Terminal chat")
    print(f"{agent.config.agent_name} ({agent.config.model_name}). Empty line or Ctrl-D quits.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except EOFError:
            break
        if not text:
            break
        print("agent> ", end="", flush=True)
        async for event in agent.chat(chat_id, text):
            if event["type"] == "text":
                print(event["text"], end="", flush=True)
            elif event["type"] == "tool":
                print(f"\n  [tool] {event['text']}\n", end="", flush=True)
            elif event["type"] == "error":
                print(f"\n  [error] {event['message']}", end="", flush=True)
        print()


def make_hash() -> None:
    from .config import MIN_PASSWORD_LENGTH
    from .web.auth import hash_password

    pw = getpass.getpass("New password for the chat page: ")
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Same again: ") != pw:
        raise SystemExit("The passwords didn't match.")
    print("\nPut this line in your .env (keep the single quotes):\n")
    print(f"AGENT_PASSWORD_HASH='{hash_password(pw)}'")


def healthcheck(config: Config) -> bool:
    host = config.host
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
        if address.is_unspecified:
            host = "::1" if address.version == 6 else "127.0.0.1"
        host = f"[{host}]" if ":" in host else host
        allowed = next((name for name in config.allowed_hosts if name != "*"), "")
        header_host = config.agent_host or allowed.replace("*.", "healthcheck.") or host
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=3) as client:
            response = client.get(f"http://{host}:{config.port}/healthz", headers={"Host": header_host})
        payload = response.json()
        return (
            response.status_code == 200
            and isinstance(payload, dict)
            and set(payload) == {"ok"}
            and payload["ok"] is True
        )
    except (httpx.HTTPError, ValueError):
        return False


def serve(agent: Agent) -> None:
    log = logging.getLogger("agent")
    if not agent.config.allowed_hosts:
        log.warning(
            "ALLOWED_HOSTS is empty, so the page answers to any hostname. "
            "Set it before putting the agent online."
        )
    if agent.allow_shell and agent.config.shell_backend == "local":
        log.warning(
            "ALLOW_SHELL=true with SHELL_BACKEND=local: shell commands run as the "
            "agent's own user and can write its database. Prefer SHELL_BACKEND=sandbox."
        )
    elif agent.allow_shell:
        log.info("ALLOW_SHELL=true using sandbox backend at %s", agent.config.sandbox_url)
    import uvicorn

    from .web.app import create_app

    uvicorn.run(
        create_app(agent),
        host=agent.config.host,
        port=agent.config.port,
        # The app reads X-Forwarded-For itself, only from FORWARDED_ALLOW_IPS.
        proxy_headers=False,
        server_header=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=[
            "serve",
            "chat",
            "run-jobs",
            "hash-password",
            "migrate",
            "audit-verify",
            "backup-now",
            "restore",
            "healthcheck",
            "gen-token",
        ],
    )
    parser.add_argument("file", nargs="?", help="compressed SQLite backup to restore")
    parser.add_argument(
        "--check", action="store_true", help="inspect database versions without applying migrations"
    )
    args = parser.parse_args()
    if args.command == "restore" and not args.file:
        parser.error("restore needs a .db.gz backup file")
    if args.file and args.command != "restore":
        parser.error("Only restore accepts a file")
    if args.command == "migrate":
        if not args.check:
            parser.error("Use migrate --check; normal startup applies pending migrations")
        from .migrations import inspect_version, latest_version

        harden_process()  # Configuration may read mounted secret files, as in normal startup.
        current = inspect_version(Config.from_env().db_path)
        target = latest_version()
        if current == target:
            status = "up to date"
        elif current < target:
            status = "upgrade pending"
        else:
            status = "newer than supported"
        print(f"Database version: {current}; target: {target}; {status}")
        return
    if args.check:
        parser.error("--check is only valid with migrate")
    if args.command == "audit-verify":
        from .audit import verify_file

        harden_process()
        try:
            result = verify_file(Config.from_env().db_path)
        except sqlite3.Error as error:
            raise SystemExit(
                "Cannot inspect the audit log; check that the database exists and has been upgraded"
            ) from error
        print(json.dumps(result, sort_keys=True))
        if not result["ok"]:
            raise SystemExit(1)
        return
    if args.command == "hash-password":
        make_hash()
        return
    if args.command == "gen-token":
        print(secrets.token_urlsafe(32))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    harden_process()
    config = Config.from_env()
    if args.command == "healthcheck":
        raise SystemExit(0 if healthcheck(config) else 1)
    from .locking import LockBusy, database_lock

    try:
        if args.command == "restore":
            from .backup import restore

            preserved = restore(config, Path(args.file))
            print("Database restored; saved previous database: " + (str(preserved) if preserved else "none"))
            return
        if args.command == "serve":
            config.check()  # Validate before creating the lock file or opening the database.
        with database_lock(config.data_dir):
            if args.command == "backup-now":
                from .audit import Audit
                from .backup import backup_now, validate_backup_config

                validate_backup_config(config)
                if not config.db_path.is_file():
                    raise SystemExit("No existing database to back up")
                config.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                memory = Memory(config.db_path, backup_dir=config.backup_dir)
                try:
                    files = backup_now(config, memory, Audit.from_config(memory, config))
                    print(json.dumps({"ok": True, "files": [str(file) for file in files]}))
                finally:
                    memory.close()
                return
            agent = build(validate=args.command == "serve", config=config)
            try:
                if args.command == "chat":
                    asyncio.run(terminal_chat(agent))
                elif args.command == "run-jobs":
                    print(f"ran {asyncio.run(run_due_jobs(agent))} job(s)")
                else:
                    serve(agent)
            finally:
                agent.memory.close()
    except LockBusy as error:
        raise SystemExit(
            "Database or backup is in use; stop the server and active commands before restore"
        ) from error
    except (OSError, sqlite3.Error, ValueError) as error:
        if args.command in {"backup-now", "restore"}:
            raise SystemExit(
                f"{args.command} failed: {type(error).__name__}; existing database was preserved"
            ) from error
        raise


if __name__ == "__main__":
    main()
