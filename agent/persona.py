"""Human-owned prompt versions. Safety and tool protocol remain code-owned."""

from __future__ import annotations

import difflib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .memory import Memory

DEFAULT_PERSONA = (
    "Roland's personal AI agent. You run around the clock on his own server. "
    "Keep answers short and plain unless asked for detail."
)


class OverBudget(ValueError):
    def __init__(self, count: int):
        self.count = count
        super().__init__(f"The full prompt uses {count} tokens. Confirm the extra time and context cost.")


class Persona:
    def __init__(self, memory: Memory):
        self.memory = memory

    def active(self) -> dict:
        return dict(self.memory._all("SELECT * FROM prompt_versions WHERE active=1")[0])

    def version(self, identifier: int) -> dict:
        rows = self.memory._all("SELECT * FROM prompt_versions WHERE id=?", (identifier,))
        if not rows:
            raise KeyError(identifier)
        return dict(rows[0])

    def history(self) -> list[dict]:
        return [dict(row) for row in self.memory._all("SELECT * FROM prompt_versions ORDER BY id DESC")]

    @staticmethod
    def block(version: dict) -> str:
        name, persona, instructions = (version[key] for key in ("agent_name", "persona", "instructions"))
        return f"You are {name}. {persona}\n{instructions}\n\n"

    async def preview(self, agent, version: dict) -> dict:
        prompt = agent.system_prompt(persona_version=version)
        # Save requires the serving model's own tokenizer, never a guessed count.
        count = await agent._counter(prompt)
        return {
            "preview": prompt,
            "token_count": count,
            "over_budget": count > agent.config.model_system_prompt_budget,
            "budget": agent.config.model_system_prompt_budget,
        }

    async def save(self, agent, version: dict, *, confirm: bool = False) -> dict:
        measured = await self.preview(agent, version)
        if measured["over_budget"] and not confirm:
            raise OverBudget(measured["token_count"])
        with self.memory.transaction():
            self.memory._db.execute("UPDATE prompt_versions SET active=0 WHERE active=1")
            cursor = self.memory._db.execute(
                "INSERT INTO prompt_versions(agent_name,persona,instructions,token_count,created,active) "
                "VALUES (?,?,?,?,?,1)",
                (
                    version["agent_name"],
                    version["persona"],
                    version["instructions"],
                    measured["token_count"],
                    time.time(),
                ),
            )
            identifier = cursor.lastrowid
            agent.audit.write("roland", "prompt_changed", detail={"version_id": identifier})
        return {**self.version(identifier), **measured}

    def diff(self, identifier: int) -> str:
        return "\n".join(
            difflib.unified_diff(
                self.block(self.version(identifier)).splitlines(),
                self.block(self.active()).splitlines(),
                fromfile=f"version {identifier}",
                tofile="active",
                lineterm="",
            )
        )
