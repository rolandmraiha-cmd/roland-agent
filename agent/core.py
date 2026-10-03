"""The agent loop: build the prompt, stream the model's reply, run tools, repeat."""

from __future__ import annotations

import json
from typing import AsyncIterator

from .brain import Brain, Step
from .config import Config
from .memory import Job, Memory
from .schedule import now_text, today
from .tools import ToolContext, call_tool, describe, in_container, schemas

HISTORY = 30  # earlier messages from the same chat sent to the model

SYSTEM = """You are {name}, Roland's personal AI agent. You run around the clock on his own server.
Current time: {now} ({tz}).

You have tools: read web pages, run shell commands in your own container, read and write files
in your workspace (file paths are relative to it, like 'notes/todo.txt'), save facts, and schedule background jobs. Use them when they help; don't
pretend you used a tool when you didn't. Keep answers short and plain unless asked for detail.
{shell_note}
Things you remember (fact id: fact):
{facts}"""


class LimitReached(Exception):
    pass


class Agent:
    def __init__(self, config: Config, memory: Memory, brain: Brain):
        self.config = config
        self.memory = memory
        self.brain = brain
        self.allow_shell = in_container() or __import__("os").getenv("ALLOW_SHELL") == "true"
        self.ctx = ToolContext(memory, config.workspace, config.timezone, self.allow_shell)
        config.workspace.mkdir(parents=True, exist_ok=True)

    def system_prompt(self, extra: str = "") -> str:
        facts = "\n".join(f"{i}: {t}" for i, t in self.memory.facts()) or "(nothing yet)"
        shell_note = "" if self.allow_shell else "Shell commands are turned off right now.\n"
        return SYSTEM.format(
            name=self.config.agent_name, now=now_text(self.config.timezone),
            tz=self.config.timezone, facts=facts, shell_note=shell_note,
        ) + extra

    def calls_left(self) -> int:
        return max(0, self.config.daily_call_limit - self.memory.calls_today(today(self.config.timezone)))

    def _count_call(self) -> None:
        day = today(self.config.timezone)
        if self.memory.calls_today(day) >= self.config.daily_call_limit:
            raise LimitReached(
                f"The daily limit of {self.config.daily_call_limit} model calls is used up. "
                "It resets at midnight, or raise DAILY_CALL_LIMIT in .env."
            )
        self.memory.count_call(day)

    async def run(self, messages: list[dict], tool_exclude: set[str] = frozenset()
                  ) -> AsyncIterator[dict]:
        """Runs the tool loop. Yields events: text, tool, done (with the full reply) or error."""
        tools = schemas(tool_exclude)
        reply = ""
        for _ in range(self.config.max_tool_steps + 1):
            try:
                self._count_call()
            except LimitReached as e:
                yield {"type": "error", "message": str(e)}
                return
            step = Step()
            try:
                async for item in self.brain.stream(messages, tools):
                    if isinstance(item, Step):
                        step = item
                    else:
                        yield {"type": "text", "text": item}
            except Exception as e:
                yield {"type": "error", "message": f"The model didn't answer: {type(e).__name__}: {e}"}
                return
            reply += step.text
            if not step.tool_calls:
                yield {"type": "done", "reply": reply}
                return
            messages.append({
                "role": "assistant", "content": step.text or None,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments or "{}"}}
                               for c in step.tool_calls],
            })
            for call in step.tool_calls:
                args = call.args()
                if call.name in tool_exclude:
                    result = f"Error: {call.name} isn't available here."
                else:
                    yield {"type": "tool", "text": describe(call.name, args)}
                    result = await call_tool(self.ctx, call.name, args)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if reply and not reply.endswith("\n"):
                reply += "\n"
        yield {"type": "error",
               "message": f"Stopped after {self.config.max_tool_steps} tool steps (MAX_TOOL_STEPS)."}

    async def chat(self, chat_id: int, text: str) -> AsyncIterator[dict]:
        """Answers one message in a saved chat, streaming events, and saves both sides."""
        history = self.memory.messages(chat_id, limit=HISTORY)
        if not history:
            self.memory.rename_chat(chat_id, text.strip().splitlines()[0][:60] or "New chat")
        self.memory.add_message(chat_id, "user", text)
        messages = [{"role": "system", "content": self.system_prompt()}]
        messages += [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": text})
        reply = ""
        async for event in self.run(messages):
            if event["type"] == "text":
                reply += event["text"]
            if event["type"] == "done":
                reply = event["reply"]
            if event["type"] == "error":
                reply = (reply + "\n\n" if reply else "") + f"[{event['message']}]"
            yield event
        if reply.strip():
            self.memory.add_message(chat_id, "assistant", reply.strip())

    async def run_job(self, job: Job) -> tuple[bool, str]:
        """Runs one background job with no chat history. Returns (ok, output)."""
        extra = (f"\n\nYou are running the scheduled background job '{job.name}' ({job.cron}). "
                 "Nobody is watching live; your final answer is saved as the job's result.")
        messages = [{"role": "system", "content": self.system_prompt(extra)},
                    {"role": "user", "content": job.prompt}]
        out, tools_used, ok = "", [], True
        # A job can't create more jobs, so a bad prompt can't multiply itself.
        async for event in self.run(messages, tool_exclude={"schedule_job"}):
            if event["type"] == "done":
                out = event["reply"]
            elif event["type"] == "tool":
                tools_used.append(event["text"])
            elif event["type"] == "error":
                ok = False
                out = (out + "\n" if out else "") + event["message"]
        if tools_used:
            out = out.strip() + "\n\nTools used:\n" + "\n".join(f"- {t}" for t in tools_used)
        return ok, out.strip()


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
