"""Tools the agent can use. Each tool is a plain function plus a JSON schema for the model."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .audit import Audit, NullAudit
from .memory import Memory
from .schedule import next_run_after, valid_cron
from .tools_browser import SPECS as BROWSER_SPECS
from .tools_files import attach_file, delete_file, file_info, move_file

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
    audit: Audit | NullAudit = field(default_factory=NullAudit)
    gate: object | None = None
    run: object | None = None
    config: object | None = None
    shell: object | None = None  # ShellBackend; None → LocalShell when allow_shell
    browser: object | None = None  # BrowserClient; None → browser tools are off
    signins: object | None = None  # SignIns; None → request_signin is off


Handler = Callable[[ToolContext, dict], Awaitable[str]]


def _ws(ctx: ToolContext):
    from .tools_files import workspace_from_ctx

    return workspace_from_ctx(ctx)


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
def _shell_timeout(ctx: ToolContext, args: dict) -> int:
    config = getattr(ctx, "config", None)
    default = getattr(config, "shell_timeout_default", None) or SHELL_TIMEOUT
    maximum = getattr(config, "shell_timeout_max", None) or 300
    raw = args.get("timeout_s", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


async def run_shell(ctx: ToolContext, args: dict) -> str:
    if not ctx.allow_shell:
        return ("Error: shell commands are turned off. Roland can turn them on with "
                "ALLOW_SHELL=true.")
    command = str(args.get("command", ""))
    timeout_s = _shell_timeout(ctx, args)
    backend = ctx.shell
    if backend is None:
        from .local_shell import LocalShell

        backend = LocalShell(ctx.workspace, max_output=MAX_OUTPUT)
    result = await backend.run(command, timeout_s)
    # Audit every command (§6.2.5 shell_exec).
    run = ctx.run
    run_id = getattr(run, "run_id", None) if run is not None else None
    chat_id = getattr(run, "chat_id", None) if run is not None else None
    ctx.audit.write(
        "agent",
        "shell_exec",
        run_id=run_id,
        chat_id=chat_id,
        tool="run_shell",
        detail={
            "command": command[:4096],
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "output_sha256": hashlib.sha256(result.output.encode("utf-8", errors="replace")).hexdigest(),
            "output_preview": result.output[:2048],
            "truncated": result.truncated,
            "timed_out": result.timed_out,
            "timeout_s": timeout_s,
        },
    )
    if result.timed_out:
        return f"Error: the command took longer than {timeout_s} seconds and was stopped."
    text = clip(result.output)
    if result.truncated:
        return f"{text}\n[stopped: the command printed more than {MAX_OUTPUT} characters]"
    return f"exit code {result.exit_code}\n{text}"


# --- files ---
async def read_file(ctx: ToolContext, args: dict) -> str:
    path = str(args.get("path", ""))
    try:
        return _ws(ctx).read_text(path, max_chars=MAX_OUTPUT)
    except FileNotFoundError:
        shown = path.strip() or "(empty path)"
        return (
            f"Error: file not found: {shown}. It is not in the workspace. "
            "Do not retry this path; answer with what you know or ask Roland."
        )
    except (ValueError, OSError) as e:
        return f"Error: {e}"


async def write_file(ctx: ToolContext, args: dict) -> str:
    try:
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        append = bool(args.get("append"))
        run = ctx.run
        chat_id = getattr(run, "chat_id", None) if run is not None else None
        approval_id = getattr(run, "pending_approval_id", None) if run is not None else None
        result = _ws(ctx).write_text(
            path,
            content,
            append=append,
            origin="agent",
            chat_id=chat_id,
            deleted_by="agent",
            approval_id=approval_id,
        )
        return f"Saved {result['path']} ({len(content)} characters)."
    except (ValueError, OSError) as e:
        return f"Error: {e}"


async def list_files(ctx: ToolContext, args: dict) -> str:
    try:
        entries, _truncated = _ws(ctx).list_dir(str(args.get("path", ".") or "."))
        lines = []
        for entry in entries[:300]:
            name = entry["path"]
            if entry["type"] == "dir":
                lines.append(f"{name}/")
            elif entry["type"] == "symlink":
                lines.append(entry["name"] if entry["name"].endswith("@") else f"{name}@")
            else:
                lines.append(name)
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
    run = ctx.run
    chat_id = getattr(run, "chat_id", None) if run is not None else None
    tainted = bool(getattr(run, "tainted", False)) if run is not None else False
    fact_id = ctx.memory.remember(
        fact, limit=MAX_FACTS, origin="agent", chat_id=chat_id, tainted=tainted,
    )
    if fact_id is None:
        return (f"Error: {MAX_FACTS} facts are saved already. Forget one first, or ask Roland "
                "to delete some on the Jobs tab.")
    return f"Remembered as fact {fact_id}."


def prompt_facts(memory: Memory, *, max_chars: int = MAX_FACTS_PROMPT_CHARS) -> str:
    """Saved facts as prompt lines, newest first: one line each, shortened, and the whole block
    at most MAX_FACTS_PROMPT_CHARS. The system prompt is never trimmed, so this keeps it inside
    a small model's context however many facts are saved (older versions saved without limits)."""
    facts = memory.facts()
    room = max_chars - 100  # left for the "not shown" line
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
    with ctx.memory.transaction():
        job_id = ctx.memory.add_job(name, cron, prompt, nxt, approved=False, origin="agent")
        ctx.audit.write("agent", "job_created", tool="schedule_job",
                        detail={"job_id": job_id, "name": name, "cron": cron, "approved": False})
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
    "run_shell": (_fn("run_shell", "Run a shell command in the isolated sandbox workspace. "
                      "Optional timeout_s (seconds) and reason for gated commands.",
                      {"command": S, "timeout_s": INTEGER, "reason": S}, ["command"]), run_shell),
    "read_file": (_fn("read_file", "Read a text file from your workspace. Paths are relative to the workspace, e.g. 'notes/todo.txt'.",
                      {"path": S}, ["path"]), read_file),
    "write_file": (_fn("write_file", "Write (or append to) a text file in your workspace. Paths are relative to the workspace, e.g. 'notes/todo.txt'.",
                       {"path": S, "content": S, "append": {"type": "boolean"}},
                       ["path", "content"]), write_file),
    "list_files": (_fn("list_files", "List files in a workspace folder (relative path, default the workspace itself).",
                       {"path": S}, []), list_files),
    "delete_file": (_fn("delete_file", "Move a workspace file to trash. Requires Roland's approval.",
                        {"path": S, "reason": S}, ["path"]), delete_file),
    "move_file": (_fn("move_file", "Move or rename a workspace file. Replacing an existing file needs approval.",
                      {"from": S, "to": S, "reason": S}, ["from", "to"]), move_file),
    "file_info": (_fn("file_info", "Show size, modified time, sha256 and origin for a workspace file.",
                      {"path": S}, ["path"]), file_info),
    "attach_file": (_fn("attach_file", "Share a workspace file with Roland in the chat (download card). Nothing leaves the server.",
                        {"path": S, "note": S}, ["path"]), attach_file),
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
# Browser tools (M6) come last. They are only offered when BROWSER_ENABLED=true.
TOOLS.update({
    name: (_fn(name, description, properties, required), handler)
    for name, description, properties, required, handler in BROWSER_SPECS
})


def schemas(exclude: set[str] = frozenset()) -> list[dict]:
    return [schema for name, (schema, _) in TOOLS.items() if name not in exclude]


async def call_tool(ctx: ToolContext, name: str, args: dict) -> str:
    from .gate import PIN_KEY, POLICIES, Decision, NoApproverGate, Risk, mark_executed

    if name not in TOOLS:
        return f"Error: there is no tool called {name}."
    policy = POLICIES.get(name)
    if policy is None:
        return f"Error: there is no tool called {name}."
    if PIN_KEY in args:
        # Reserved for values a classifier pins. The model can never supply them.
        args = {key: value for key, value in args.items() if key != PIN_KEY}
    try:
        decision = await policy.classify(ctx, args)
    except Exception:
        decision = Decision(Risk.FORBIDDEN, "other", reason="classifier error")
    if decision.pinned and decision.risk is not Risk.FORBIDDEN:
        # Stored with the approval, so what Roland approves is bound to what was classified.
        args = {**args, PIN_KEY: dict(decision.pinned)}
    run = ctx.run
    run_id = getattr(run, "run_id", None) if run is not None else None
    chat_id = getattr(run, "chat_id", None) if run is not None else None
    ctx.audit.write(
        "agent",
        "gate_decision",
        run_id=run_id,
        chat_id=chat_id,
        tool=name,
        decision=decision.risk.value,
        detail={"category": decision.category, "reason": decision.reason, "args_keys": sorted(args)},
    )
    if decision.risk is Risk.FORBIDDEN:
        return f"Error: {name} isn't allowed: {decision.reason or 'forbidden'}"
    approval_id = None
    run_args = args
    if decision.risk is Risk.GATED:
        gate = ctx.gate or NoApproverGate()
        outcome = await gate.request(ctx, name, args, decision)
        if not outcome.approved:
            return f"Not done: {outcome.message}"
        # Fail closed: never run with freshly supplied/model args when stored args are missing.
        if outcome.args is None:
            return "Not done: approved action is missing stored args."
        run_args = outcome.args
        approval_id = outcome.approval_id
    # Gate clears pending_approval_id in request()'s finally; restore for the tool
    # so overwrite/trash paths can see that replace was already approved.
    if approval_id is not None and run is not None:
        run.pending_approval_id = approval_id
    try:
        result = await TOOLS[name][1](ctx, run_args)
    except Exception as e:  # a broken tool call should never crash the agent
        result = f"Error: {type(e).__name__}: {e}"
    finally:
        if approval_id is not None and run is not None:
            run.pending_approval_id = None
    if approval_id is not None and ctx.gate is not None:
        mark_executed(ctx.memory, ctx.audit, approval_id, result)
    else:
        digest_detail = {"preview": result[:500]}
        ctx.audit.write(
            "agent",
            "tool_result",
            run_id=run_id,
            chat_id=chat_id,
            tool=name,
            decision=decision.risk.value,
            detail=digest_detail,
        )
    if policy.taints and run is not None:
        run.tainted = True
        if run_id:
            ctx.memory.set_run_tainted(run_id)
    return result


def describe(name: str, args: dict) -> str:
    """A short line for the chat page showing which tool ran."""
    text = json.dumps(args, ensure_ascii=False)
    return f"{name} {clip(text, 160)}"


# Import-time assertion: every registered tool must have a policy (§6.2.4).
from .gate import POLICIES as _POLICIES  # noqa: E402

assert set(TOOLS) == set(_POLICIES), (
    f"TOOLS/POLICIES mismatch: only in TOOLS={set(TOOLS)-set(_POLICIES)} "
    f"only in POLICIES={set(_POLICIES)-set(TOOLS)}"
)
