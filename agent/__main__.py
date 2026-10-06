"""Start the agent.

    python -m agent            serve the web chat page (default)
    python -m agent chat       chat in the terminal, for quick local tests
    python -m agent run-jobs   run any due background jobs once and exit
    python -m agent hash-password   make the AGENT_PASSWORD_HASH line for .env
    python -m agent migrate --check   inspect schema versions without upgrading
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import getpass
import logging
import sys

from .brain import OpenAICompatibleBrain
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


def build(*, validate: bool = False) -> Agent:
    config = Config.from_env()
    if validate:
        config.check()  # Refuse unsafe settings before opening the DB or model client.
    else:
        config.check_model()  # Terminal chat and background jobs also stay local.
    if config.allow_shell and config.shell_backend != "local":
        raise SystemExit("The sandbox shell backend is not implemented yet; keep ALLOW_SHELL=false")
    if config.browser_enabled or config.screen_enabled:
        raise SystemExit("Browser and screen services are not implemented yet; keep their flags false")
    memory = Memory(config.db_path, backup_dir=config.backup_dir if validate else None)
    brain = OpenAICompatibleBrain(
        config.model_base_url, config.model_name, config.model_server_token,
        allowed_hosts=config.model_allowed_hosts, timeout=config.model_timeout_s,
    )
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "chat", "run-jobs", "hash-password", "migrate"])
    parser.add_argument("--check", action="store_true", help="inspect database versions without applying migrations")
    args = parser.parse_args()
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
    if args.command == "hash-password":
        make_hash()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    harden_process()
    agent = build(validate=args.command == "serve")
    if args.command == "chat":
        asyncio.run(terminal_chat(agent))
    elif args.command == "run-jobs":
        print(f"ran {asyncio.run(run_due_jobs(agent))} job(s)")
    else:
        log = logging.getLogger("agent")
        if not agent.config.allowed_hosts:
            log.warning("ALLOWED_HOSTS is empty, so the page answers to any hostname. "
                        "Set it before putting the agent online.")
        if agent.allow_shell:
            log.warning("ALLOW_SHELL=true: shell commands run as the agent's own user and can "
                        "write its database. A web page that tricks the model into one command "
                        "could create a login for an attacker or approve its own jobs. Only turn "
                        "this on if you accept that risk.")
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(agent), host=agent.config.host,
                    port=agent.config.port,
                    # The app reads X-Forwarded-For itself, only from FORWARDED_ALLOW_IPS.
                    proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
