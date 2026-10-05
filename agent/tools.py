"""Tools the agent can use. Each tool is a plain function plus a JSON schema for the model."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .memory import Memory
from .schedule import next_run_after, valid_cron

MAX_OUTPUT = 8000          # characters of tool output the model sees
MAX_DOWNLOAD = 2_000_000   # bytes read from a web page
SHELL_TIMEOUT = 60         # seconds
LIST_PROMPT_CHARS = 300    # how much of each prompt list_jobs shows
MAX_LISTED_JOBS = 30       # list_jobs shows at most this many
MAX_JOB_PROMPT = 5000      # characters, same as the Jobs tab form
FETCH_DEADLINE = 45        # seconds for a whole web fetch, redirects included
MAX_FACT_CHARS = 200       # one saved fact; every fact goes into every prompt
MAX_FACTS = 50             # saved facts in total
MAX_FACTS_PROMPT_CHARS = 1500  # the whole facts block in the system prompt (~500 tokens)
# IPv6 ranges that can wrap an IPv4 address (NAT64, 6to4), so a private IPv4 could hide inside.
BLOCKED_NETS = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16")]
# Environment variables the shell never gets, so commands can't print the agent's secrets.
SECRET_ENV = {"AGENT_PASSWORD_HASH", "MODEL_API_KEY"}


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cut, {len(text) - limit} more characters]"


@dataclass
class ToolContext:
    memory: Memory
    workspace: Path
    timezone: str
    allow_shell: bool


Handler = Callable[[ToolContext, dict], Awaitable[str]]


def _workspace_path(ctx: ToolContext, path: str) -> Path:
    """Resolves a path inside the workspace and refuses anything that points outside it."""
    root = ctx.workspace.resolve()
    target = (root / (path or ".")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Path is outside the workspace.")
    return target


# --- web ---
def _public_ip(host: str) -> str | None:
    """Looks the host up once and returns an address to connect to, or None if any of its
    addresses is local or private."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return None
    if not infos:
        return None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped  # e.g. ::ffff:127.0.0.1
        # is_global is False for loopback, private, link-local (169.254.x, fe80::), unique-local
        # (fc00::/7), shared, reserved and other non-public ranges.
        if not ip.is_global or ip.is_multicast or any(ip in net for net in BLOCKED_NETS):
            return None
    return infos[0][4][0].split("%")[0]


def _pinned(parsed, ip: str) -> tuple[str, dict, dict]:
    """Rewrites the URL to connect to the address we just checked, so a second DNS lookup can't
    swap in a private one (DNS rebinding). The real hostname still goes in the Host header and,
    for https, in the TLS name the certificate is checked against."""
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        netloc_ip += f":{parsed.port}"
    url = parsed._replace(netloc=netloc_ip).geturl()
    host_header = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    ext = {"sni_hostname": parsed.hostname} if parsed.scheme == "https" else {}
    return url, {"Host": host_header}, ext


async def fetch_url(ctx: ToolContext, args: dict) -> str:
    try:
        return await asyncio.wait_for(_fetch(str(args.get("url", "")).strip()), FETCH_DEADLINE)
    except TimeoutError:
        return f"Error: the page took longer than {FETCH_DEADLINE} seconds."


async def _fetch(url: str) -> str:
    # trust_env=False: never send fetches through a proxy from the environment, which would
    # make the address check meaningless.
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False,
                                 headers={"User-Agent": "roland-agent/0.1"}) as client:
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "Error: only http and https URLs are allowed."
            # Blocks local and private addresses (the agent's own machine, home network,
            # cloud metadata), checked again on every redirect.
            ip = await asyncio.to_thread(_public_ip, parsed.hostname)
            if not ip:
                return "Error: that address is private or can't be resolved."
            target, headers, ext = _pinned(parsed, ip)
            async with client.stream("GET", target, headers=headers, extensions=ext) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    url = urljoin(url, resp.headers["location"])
                    continue
                body = b""
                async for part in resp.aiter_bytes():
                    body += part
                    if len(body) > MAX_DOWNLOAD:
                        break
                ctype = resp.headers.get("content-type", "")
                text = body.decode(resp.encoding or "utf-8", errors="replace")
                if "html" in ctype:
                    soup = BeautifulSoup(text, "html.parser")
                    for tag in soup(["script", "style", "noscript", "svg"]):
                        tag.decompose()
                    title = soup.title.get_text(strip=True) if soup.title else ""
                    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
                    text = (f"Title: {title}\n\n" if title else "") + "\n".join(lines)
                return clip(f"HTTP {resp.status_code} {url}\n\n{text}")
    return "Error: too many redirects."


# --- shell ---
async def run_shell(ctx: ToolContext, args: dict) -> str:
    if not ctx.allow_shell:
        return ("Error: shell commands are turned off. Roland can turn them on with "
                "ALLOW_SHELL=true.")
    command = str(args.get("command", ""))
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    proc = await asyncio.create_subprocess_shell(
        command, cwd=ctx.workspace, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    def kill() -> None:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass

    async def read_capped() -> tuple[bytes, bool]:
        # Reads at most MAX_OUTPUT bytes; a command that prints more is stopped right there.
        out = b""
        while len(out) <= MAX_OUTPUT:
            part = await proc.stdout.read(4096)
            if not part:
                return out, False
            out += part
        kill()
        return out, True

    async def finish() -> None:
        # Drains what's left (the process is dead or done) so its pipes close cleanly.
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            pass

    try:
        out, cut = await asyncio.wait_for(read_capped(), timeout=SHELL_TIMEOUT)
        await finish()
    except asyncio.CancelledError:
        kill()
        await finish()
        raise
    except TimeoutError:
        kill()
        await finish()
        return f"Error: the command took longer than {SHELL_TIMEOUT} seconds and was stopped."
    text = clip(out.decode(errors="replace"))
    if cut:
        return f"{text}\n[stopped: the command printed more than {MAX_OUTPUT} characters]"
    return f"exit code {proc.returncode}\n{text}"


# --- files ---
async def read_file(ctx: ToolContext, args: dict) -> str:
    try:
        target = _workspace_path(ctx, str(args.get("path", "")))
        # Read one extra character to detect truncation without loading the whole file.
        with target.open(errors="replace") as f:
            text = f.read(MAX_OUTPUT + 1)
        if len(text) > MAX_OUTPUT:
            return text[:MAX_OUTPUT] + "\n... [cut, file continues]"
        return text
    except (ValueError, OSError) as e:
        return f"Error: {e}"


async def write_file(ctx: ToolContext, args: dict) -> str:
    try:
        target = _workspace_path(ctx, str(args.get("path", "")))
        if target == ctx.workspace.resolve():
            return "Error: give a file name."
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        if args.get("append"):
            with target.open("a") as f:
                f.write(content)
        else:
            target.write_text(content)
        return f"Saved {target.relative_to(ctx.workspace.resolve())} ({len(content)} characters)."
    except (ValueError, OSError) as e:
        return f"Error: {e}"


async def list_files(ctx: ToolContext, args: dict) -> str:
    try:
        ctx.workspace.mkdir(parents=True, exist_ok=True)
        target = _workspace_path(ctx, str(args.get("path", ".")))
        root = ctx.workspace.resolve()
        items = sorted(target.iterdir())
        lines = [f"{p.relative_to(root)}{'/' if p.is_dir() else ''}" for p in items[:300]]
        return "\n".join(lines) or "(empty)"
    except (ValueError, OSError) as e:
        return f"Error: {e}"


# --- memory ---
def one_line(text: str) -> str:
    """Joins all lines into one, so a fact can't start what looks like a new prompt line."""
    return " ".join(text.split())


async def remember(ctx: ToolContext, args: dict) -> str:
    fact = one_line(str(args.get("fact", "")))
    if not fact:
        return "Error: the fact is empty."
    if len(fact) > MAX_FACT_CHARS:
        return f"Error: a fact can be at most {MAX_FACT_CHARS} characters. Save a shorter one."
    fact_id = ctx.memory.remember(fact, limit=MAX_FACTS)
    if fact_id is None:
        return (f"Error: {MAX_FACTS} facts are saved already. Forget one first, or ask Roland "
                "to delete some on the Jobs tab.")
    return f"Remembered as fact {fact_id}."


def prompt_facts(memory: Memory) -> str:
    """Saved facts as prompt lines, newest first: one line each, shortened, and the whole block
    at most MAX_FACTS_PROMPT_CHARS. The system prompt is never trimmed, so this keeps it inside
    a small model's context however many facts are saved (older versions saved without limits)."""
    facts = memory.facts()
    room = MAX_FACTS_PROMPT_CHARS - 100  # left for the "not shown" line
    lines: list[str] = []
    used = 0
    for fact_id, text in reversed(facts):
        line = f"{fact_id}: {_short(text, MAX_FACT_CHARS)}"
        used += len(line) + 1
        if used > room:
            break
        lines.append(line)
    if len(facts) > len(lines):
        lines.append(f"({len(facts) - len(lines)} older saved facts not shown here; Roland can "
                     "see them on the Jobs tab.)")
    return "\n".join(lines)


async def forget(ctx: ToolContext, args: dict) -> str:
    ok = ctx.memory.forget(int(args.get("fact_id", 0)))
    return "Forgotten." if ok else "No fact with that id."


# --- background jobs ---
async def schedule_job(ctx: ToolContext, args: dict) -> str:
    cron = str(args.get("cron", "")).strip()
    if not valid_cron(cron):
        return "Error: that is not a valid 5-field cron schedule, e.g. '0 7 * * *'."
    name = str(args.get("name", "Job")).strip()[:80] or "Job"
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return "Error: the job needs a prompt."
    if len(prompt) > MAX_JOB_PROMPT:
        return f"Error: a job prompt can be at most {MAX_JOB_PROMPT} characters."
    nxt = next_run_after(cron, ctx.timezone)
    job_id = ctx.memory.add_job(name, cron, prompt, nxt, approved=False, origin="agent")
    return (f"Created job {job_id} '{name}' ({cron}, {ctx.timezone}). It is waiting for Roland's "
            "approval: tell him to press Approve on the Jobs tab. It won't run until then.")


def _short(text: str, limit: int) -> str:
    """One line, at most `limit` characters, with … when something was left out."""
    text = one_line(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def list_jobs(ctx: ToolContext, args: dict) -> str:
    # Jobs waiting for approval first, then newest first, so the ones the model just made
    # are always on the first page.
    jobs = sorted(ctx.memory.jobs(), key=lambda j: (j.approved, -j.id))
    if not jobs:
        return "No jobs."
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    total = len(jobs)
    jobs = jobs[offset:]
    if not jobs:
        return f"{total} job(s), none from offset {offset}."
    # Every field is put on one line (so a newline in a name can't fake an entry) and shortened
    # so the whole list fits under MAX_OUTPUT. A line is at most 160 characters plus the prompt.
    shown = jobs[:MAX_LISTED_JOBS]
    start = offset + 1
    room = max(40, min(LIST_PROMPT_CHARS, (MAX_OUTPUT - 200) // len(shown) - 160))
    lines = [
        f"{j.id}: {_short(j.name, 60)} [{_short(j.cron, 40)}] "
        f"{'waiting for approval' if not j.approved else 'on' if j.enabled else 'paused'}"
        f" - {_short(j.prompt, room)}" for j in shown
    ]
    rest = len(jobs) - len(shown)
    if rest:
        lines.append(f"... and {rest} more jobs not shown. Call list_jobs with offset "
                     f"{offset + len(shown)} to see them, or Roland can see all of them on the "
                     "Jobs tab.")
    head = f"{total} job(s), waiting for approval first, then newest first"
    if offset or rest:
        head += f" (showing {start}-{offset + len(shown)})"
    return head + ":\n" + "\n".join(lines)


async def cancel_job(ctx: ToolContext, args: dict) -> str:
    ok = ctx.memory.delete_job(int(args.get("job_id", 0)))
    return "Job deleted." if ok else "No job with that id."


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


S = {"type": "string"}
INTEGER = {"type": "integer"}

TOOLS: dict[str, tuple[dict, Handler]] = {
    "fetch_url": (_fn("fetch_url", "Download a public web page and return its text.",
                      {"url": S}, ["url"]), fetch_url),
    "run_shell": (_fn("run_shell", "Run a shell command in your own container, in your workspace "
                      "folder. 60 second limit.", {"command": S}, ["command"]), run_shell),
    "read_file": (_fn("read_file", "Read a text file from your workspace. Paths are relative to the workspace, e.g. 'notes/todo.txt'.",
                      {"path": S}, ["path"]), read_file),
    "write_file": (_fn("write_file", "Write (or append to) a text file in your workspace. Paths are relative to the workspace, e.g. 'notes/todo.txt'.",
                       {"path": S, "content": S, "append": {"type": "boolean"}},
                       ["path", "content"]), write_file),
    "list_files": (_fn("list_files", "List files in a workspace folder (relative path, default the workspace itself).",
                       {"path": S}, []), list_files),
    "remember": (_fn("remember", "Save a lasting fact about Roland or your work: one short line, "
                     f"at most {MAX_FACT_CHARS} characters.",
                     {"fact": S}, ["fact"]), remember),
    "forget": (_fn("forget", "Delete a saved fact by its id.", {"fact_id": INTEGER}, ["fact_id"]), forget),
    "schedule_job": (_fn("schedule_job", "Schedule a background job: a prompt you will run on a "
                         "5-field cron schedule in Roland's time zone, e.g. '0 7 * * *' for every "
                         "day at 07:00.", {"name": S, "cron": S, "prompt": S},
                         ["name", "cron", "prompt"]), schedule_job),
    "list_jobs": (_fn("list_jobs", "List scheduled background jobs, waiting for approval first, "
                      "then newest first, 30 at a time. Use offset to see more.",
                      {"offset": INTEGER}, []), list_jobs),
    "cancel_job": (_fn("cancel_job", "Delete a scheduled job by its id.",
                       {"job_id": INTEGER}, ["job_id"]), cancel_job),
}


def schemas(exclude: set[str] = frozenset()) -> list[dict]:
    return [schema for name, (schema, _) in TOOLS.items() if name not in exclude]


async def call_tool(ctx: ToolContext, name: str, args: dict) -> str:
    if name not in TOOLS:
        return f"Error: there is no tool called {name}."
    try:
        return await TOOLS[name][1](ctx, args)
    except Exception as e:  # a broken tool call should never crash the agent
        return f"Error: {type(e).__name__}: {e}"


def describe(name: str, args: dict) -> str:
    """A short line for the chat page showing which tool ran."""
    text = json.dumps(args, ensure_ascii=False)
    return f"{name} {clip(text, 160)}"
