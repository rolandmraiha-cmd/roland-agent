"""Start the agent.

    python -m agent            serve the web chat page (default)
    python -m agent chat       chat in the terminal, for quick local tests
    python -m agent run-jobs   run any due background jobs once and exit
    python -m agent hash-password   make the AGENT_PASSWORD_HASH line for .env
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os

from .brain import OpenAICompatibleBrain
from .config import Config
from .core import Agent
from .memory import Memory
from .scheduler import run_due_jobs


def build() -> Agent:
    config = Config.from_env()
    memory = Memory(config.db_path)
    brain = OpenAICompatibleBrain(config.model_base_url, config.model_name, config.model_api_key)
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
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "chat", "run-jobs", "hash-password"])
    args = parser.parse_args()
    if args.command == "hash-password":
        make_hash()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    agent = build()
    if args.command == "chat":
        asyncio.run(terminal_chat(agent))
    elif args.command == "run-jobs":
        print(f"ran {asyncio.run(run_due_jobs(agent))} job(s)")
    else:
        agent.config.check()
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(agent), host=os.getenv("HOST", "0.0.0.0"),
                    port=int(os.getenv("PORT", "8080")), proxy_headers=True)


if __name__ == "__main__":
    main()
