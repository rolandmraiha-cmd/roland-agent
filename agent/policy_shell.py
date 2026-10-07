"""Shell command classifier (§9.4.2). Usability filter; the sandbox is the security boundary."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .tools import ToolContext

# Split on whitespace and shell metacharacters so risky tokens are found without a full parse.
_SPLIT = re.compile(r"[\s;|&()<>$\\`'\"]+")

_RISKY_ALWAYS = frozenset({
    "rm", "rmdir", "unlink", "shred", "truncate", "dd", "mkfs", "find",
    "-delete", "-exec", "mv", "cp",
    "chmod", "chown", "ln",
    "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet",
    "sendmail", "mail", "mutt", "crontab", "at", "kill", "pkill", "killall",
    "twine",
})

_INTERPRETERS = frozenset({"python", "python3", "node", "perl", "ruby", "php"})
_INTERPRETER_FLAGS = frozenset({"-c", "-e"})
_GIT_RISKY = frozenset({"push", "reset", "clean", "rebase", "rm"})
_PIP_RISKY = frozenset({"upload"})
_NPM_RISKY = frozenset({"publish"})


def classify_shell(command: str, *, tainted: bool, mode: str) -> tuple[str, str]:
    """Return (risk_name, reason). risk_name is 'safe' or 'gated'."""
    if mode == "always":
        return "gated", "SHELL_APPROVAL=always"
    if tainted:
        return "gated", "run is tainted"
    text = command if len(command) <= 16000 else command[:16000]
    if len(command) > 2000:
        return "gated", "command longer than 2000 characters"
    tokens = [t for t in _SPLIT.split(text) if t]
    lower = [t.lower() for t in tokens]
    for word in lower:
        if word in _RISKY_ALWAYS:
            return "gated", f"risky token {word!r}"
    if ">" in text and ">>" not in text.replace(">>", ""):
        # Bare '>' redirection (not '>>'). Cheap signal; false positives only add an approval.
        if re.search(r"(^|[^>])>(?!>)", text):
            return "gated", "output redirection"
    if "sed" in lower:
        for i, word in enumerate(lower):
            if word == "sed" and i + 1 < len(lower) and lower[i + 1] == "-i":
                return "gated", "sed -i"
            if word.startswith("sed") and "-i" in word:
                return "gated", "sed -i"
    for i, word in enumerate(lower):
        if word in _INTERPRETERS:
            rest = lower[i + 1 : i + 4]
            if any(flag in rest for flag in _INTERPRETER_FLAGS):
                return "gated", f"{word} with -c/-e"
        if word == "git":
            rest = lower[i + 1 : i + 6]
            if any(sub in rest for sub in _GIT_RISKY):
                return "gated", "git risky subcommand"
            if "commit" in rest and "--amend" in rest:
                return "gated", "git commit --amend"
        if word == "pip" and any(sub in lower[i + 1 : i + 4] for sub in _PIP_RISKY):
            return "gated", "pip upload"
        if word == "npm" and any(sub in lower[i + 1 : i + 4] for sub in _NPM_RISKY):
            return "gated", "npm publish"
    return "safe", ""


async def classify_run_shell(ctx: ToolContext, args: dict):
    from .gate import Decision, Risk

    command = str(args.get("command", ""))
    tainted = bool(ctx.run and ctx.run.tainted)
    mode = getattr(getattr(ctx, "config", None), "shell_approval", None) or "tainted"
    if hasattr(ctx, "shell_approval") and ctx.shell_approval:
        mode = ctx.shell_approval
    # Prefer config from the agent when ToolContext carries it.
    config = getattr(ctx, "config", None)
    if config is not None:
        mode = config.shell_approval
    risk_name, reason = classify_shell(command, tainted=tainted, mode=mode)
    if risk_name == "safe":
        return Decision(Risk.SAFE)
    return Decision(Risk.GATED, "shell", reason=reason or "shell command needs approval")
