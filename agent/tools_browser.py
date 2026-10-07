"""Browser tools (§9.3, Appendix A): the agent drives one Chromium through browserd.

Two halves live here. The classifiers look at the live page element just before the gate
decides, and pin its fingerprint to the action. The handlers then act through browserd in
"safe" mode (browserd blocks form submissions) unless Roland approved that exact action.
Page text, titles and element names are untrusted and are only ever returned as tool output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import struct
import time
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import policy_browser
from .browser_client import BrowserClient, BrowserError, BrowserLocked, ElementChanged
from .gate import PIN_KEY, Decision, Risk
from .tools_files import workspace_from_ctx
from .workspace import WorkspaceError

TEXT_MAX = 5000             # characters in one browser_type call
SELECT_MAX_VALUES = 20
SELECT_VALUE_MAX = 200
SNAPSHOT_DEFAULT = 4000     # characters of page the model gets by default (§6.5)
SNAPSHOT_MAX = 20000
SNAPSHOT_MIN = 500
WAIT_MAX_S = 10
UPLOAD_MAX_BYTES = 25 * 1024 * 1024  # the file is copied through core's memory
MAX_LISTED = 50

OFF = "Error: the browser is turned off."
NOT_CHECKED = "Error: this action wasn't checked by the approval gate, so it wasn't done."
CHANGED_AFTER_APPROVAL = (
    "Error: the page changed after Roland approved this, so nothing was done. "
    "Take a new snapshot and ask again."
)
FILE_CHANGED_AFTER_APPROVAL = (
    "Error: the file changed after Roland approved the upload, so nothing was sent. Ask again."
)

# Every element fact the classifier or the approval card reads. browserd's own fingerprint
# covers only part of this (spec §6.5), so core keeps a digest of all of it with the approval
# and looks again just before an approved action runs.
_SEEN_KEYS = (
    "tag", "role", "name", "type", "href", "value", "aria_label", "title_attr", "in_form",
    "form_method", "form_action", "form_submit_name", "submits", "disabled", "sensitive",
    "aria_expanded", "aria_haspopup", "contenteditable", "inside_dialog_title",
)


def _browser(ctx) -> BrowserClient | None:
    return getattr(ctx, "browser", None)


def _one_line(value: object, limit: int = 200) -> str:
    """Page text as one plain line. Invisible and direction-changing characters are taken out:
    a page could use them to make an approval card read differently from what it says."""
    text = str(value if value is not None else "")[: limit * 8]
    if not text.isascii():
        text = "".join(
            char for char in text if unicodedata.category(char) not in {"Cf", "Cs", "Co", "Cn"}
        )
    text = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def valid_ref(ref: object) -> bool:
    """Element refs look like e12. Anything else never reaches browserd."""
    return (
        isinstance(ref, str)
        and 2 <= len(ref) <= 7
        and ref[0] == "e"
        and ref[1:].isascii()
        and ref[1:].isdigit()
    )


def valid_tab(tab_id: object) -> bool:
    """Tab ids go into a URL path, so only plain name characters are let through."""
    return (
        isinstance(tab_id, str)
        and 1 <= len(tab_id) <= 40
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in tab_id)
    )


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and 16 <= len(value) <= 128 and value.isascii() and value.isalnum()


def _where(url: object, *, path: bool = False) -> str:
    """The host (and path) of a page address, for approval cards and results."""
    try:
        parts = urlsplit(str(url or "")[: policy_browser.MAX_URL_CHARS])
    except ValueError:
        return "this page"
    host = parts.hostname or ""
    if not host:
        return "this page"
    return _one_line(f"{host}{parts.path}" if path else host, 200)


def _seen(element: dict | None, page_url: object = None) -> str:
    """A digest of what was classified and shown: the element's facts and the full address of
    the page it is on (a one-page app can change what a button means by changing the route).
    With no element (nothing has the focus) it covers the page address alone."""
    url = element.get("url") if element is not None else page_url
    url = str(url or "")[: policy_browser.MAX_URL_CHARS]
    facts = [url] + ([element.get(key) for key in _SEEN_KEYS] if element is not None else ["no element"])
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# What an element *is*, leaving out what an action on it changes (a field's value, a list's
# choice). The fingerprint moves when text is typed, so it can't be used to recognise "the
# same field again" after a blocked form submission; this can.
_TARGET_KEYS = ("tag", "role", "name", "type", "href", "in_form", "form_method", "form_action")


def _target(element: dict) -> str:
    try:
        parts = urlsplit(str(element.get("url") or "")[: policy_browser.MAX_URL_CHARS])
        page = f"{parts.scheme}://{parts.netloc}{parts.path}"
    except ValueError:
        page = ""
    facts = [page] + [element.get(key) for key in _TARGET_KEYS]
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _pins(element: dict) -> dict:
    """What every element classifier pins to the action."""
    return {"fingerprint": element["fingerprint"], "seen": _seen(element), "target": _target(element)}


async def _changed_since_approval(client: BrowserClient, pin: dict, ref: str | None) -> str | None:
    """Look at the element once more before an approved action. Returns the error to give
    back when it is no longer what Roland was shown, or None when it still is.
    `ref=None` means the focused element (key presses)."""
    try:
        fresh = await (client.describe(ref) if ref is not None else client.describe_focused())
        page_url = (await client.status()).get("url") if fresh is None else None
    except BrowserLocked as error:
        return _fail(error)
    except BrowserError:
        return CHANGED_AFTER_APPROVAL
    if not isinstance(pin.get("seen"), str) or _seen(fresh, page_url) != pin["seen"]:
        return CHANGED_AFTER_APPROVAL
    if fresh is not None and fresh.get("fingerprint") != pin.get("fingerprint"):
        return CHANGED_AFTER_APPROVAL
    return None


def _pin(args: dict) -> dict:
    value = args.get(PIN_KEY)
    return value if isinstance(value, dict) else {}


def _approved(ctx, tool: str) -> bool:
    """True only while call_tool runs this tool with an approval Roland gave for it."""
    run = getattr(ctx, "run", None)
    approval_id = getattr(run, "pending_approval_id", None) if run is not None else None
    if not approval_id:
        return False
    row = ctx.memory.approval(approval_id)
    return bool(row and row["tool"] == tool and row["status"] == "approved")


def _mode(ctx, tool: str) -> str:
    return "approved" if _approved(ctx, tool) else "safe"


def _audit(ctx, tool: str, **detail) -> None:
    run = getattr(ctx, "run", None)
    ctx.audit.write(
        "agent",
        "browser_action",
        run_id=getattr(run, "run_id", None) if run is not None else None,
        chat_id=getattr(run, "chat_id", None) if run is not None else None,
        tool=tool,
        detail=detail,
    )


def _fail(error: BrowserError) -> str:
    return f"Error: {error}"


def _forbid(reason: str) -> Decision:
    return Decision(Risk.FORBIDDEN, "other", reason=reason.rstrip("."))


# --- classifiers (run inside call_tool, before the gate) ---

async def _describe(ctx, args: dict) -> tuple[dict | None, Decision | None]:
    """Fresh facts about the element the model named, or the refusal to return instead."""
    client = _browser(ctx)
    if client is None:
        return None, _forbid("the browser is turned off")
    ref = args.get("ref")
    if not valid_ref(ref):
        return None, _forbid("give the ref of an element from the latest snapshot, like e3")
    try:
        element = await client.describe(ref)
    except BrowserError as error:
        return None, _forbid(str(error))
    if not _valid_fingerprint(element.get("fingerprint")):
        return None, _forbid("the browser gave no fingerprint for that element")
    return element, None


def _card_screenshot(ctx) -> Callable[[str], Awaitable[str | None]] | None:
    """What the gate calls to put a picture of the page on the approval card (Roland only)."""
    client = _browser(ctx)
    if client is None:
        return None

    async def take(approval_id: str) -> str | None:
        png = await client.screenshot(full_page=False)
        path = f"screenshots/approval-{approval_id}.png"
        run = getattr(ctx, "run", None)
        workspace_from_ctx(ctx).write_bytes(
            path, png, overwrite=True, origin="browser",
            chat_id=getattr(run, "chat_id", None) if run is not None else None,
        )
        return path

    return take


def _element_card(element: dict, verb: str, why: str) -> tuple[str, dict]:
    """Summary and details for an approval card. The quoted name comes from the web page, so
    quote marks are taken out of it: a page can't make its text look like part of our wording."""
    name = _one_line(element.get("name"), 80)
    for mark in "“”„‟\"«»":
        name = name.replace(mark, "'")
    name = name or "(no name)"
    kind = _label(element)
    host = _where(element.get("url"))
    summary = f"{verb} “{name}” ({kind}) on {host}"
    if why:
        summary += f" · {why}"
    details = {
        "element": name,
        "kind": kind,
        "page": _one_line(element.get("url"), 300),
        "why": why,
        "form": policy_browser.form_line(element),
        "inside dialog": _one_line(element.get("inside_dialog_title"), 80),
    }
    return summary, {key: value for key, value in details.items() if value}


def _blocked_before(ctx, element: dict) -> bool:
    """True when an unapproved action on this same element already tried to submit a form."""
    run = getattr(ctx, "run", None)
    return _target(element) in getattr(run, "blocked_submissions", ()) if run is not None else False


async def classify_open(ctx, args: dict) -> Decision:
    if _browser(ctx) is None:
        return _forbid("the browser is turned off")
    url = args.get("url")
    verdict = policy_browser.classify_open(url)
    if verdict.risk == "forbidden":
        return _forbid(verdict.why)
    if verdict.risk == "safe":
        return Decision(Risk.SAFE)
    shown = _one_line(str(url).strip(), 200)
    return Decision(
        Risk.GATED,
        verdict.category,
        reason=verdict.why,
        summary=f"Open {shown} · {verdict.why}",
        details={"url": _one_line(str(url).strip(), 500), "why": verdict.why},
    )


async def classify_click(ctx, args: dict) -> Decision:
    element, refusal = await _describe(ctx, args)
    if element is None:
        return refusal  # type: ignore[return-value]
    pinned = _pins(element)
    verdict = policy_browser.classify_click(element)
    if element.get("disabled"):
        pinned["disabled"] = True
    elif verdict.risk == "safe" and _blocked_before(ctx, element):
        verdict = policy_browser.Verdict(
            "gated", "form_submit", "an earlier click on this tried to submit a form",
        )
    if verdict.risk == "safe":
        return Decision(Risk.SAFE, pinned=pinned)
    summary, details = _element_card(element, "Click", verdict.why)
    return Decision(
        Risk.GATED, verdict.category, reason=verdict.why, summary=summary, details=details,
        pinned=pinned, card_screenshot=_card_screenshot(ctx),
    )


async def classify_type(ctx, args: dict) -> Decision:
    text = args.get("text")
    if not isinstance(text, str):
        return _forbid("give the text to type")
    if len(text) > TEXT_MAX:
        return _forbid(f"the text is longer than {TEXT_MAX} characters")
    element, refusal = await _describe(ctx, args)
    if element is None:
        return refusal  # type: ignore[return-value]
    verdict = policy_browser.classify_type(element, submit=bool(args.get("submit")))
    if verdict.risk == "forbidden":
        return _forbid(verdict.why)
    pinned = _pins(element)
    if verdict.risk == "safe" and _blocked_before(ctx, element):
        # Some fields submit by themselves as soon as their text changes.
        verdict = policy_browser.Verdict(
            "gated", "form_submit", "typing here tried to submit a form before",
        )
        summary, details = _element_card(element, "Type into", verdict.why)
        details["text"] = _one_line(text, 500)
        return Decision(
            Risk.GATED, verdict.category, reason=verdict.why, summary=summary, details=details,
            pinned=pinned, card_screenshot=_card_screenshot(ctx),
        )
    if verdict.risk == "safe":
        return Decision(Risk.SAFE, pinned=pinned)
    summary, details = _element_card(element, "Type into", f"then submit · {verdict.why}")
    details["text"] = _one_line(text, 500)
    return Decision(
        Risk.GATED, verdict.category, reason=verdict.why, summary=summary, details=details,
        pinned=pinned, card_screenshot=_card_screenshot(ctx),
    )


async def classify_press(ctx, args: dict) -> Decision:
    client = _browser(ctx)
    if client is None:
        return _forbid("the browser is turned off")
    key = policy_browser.normal_key(args.get("key"))
    if key is None:
        return _forbid(policy_browser.KEY_NOT_ALLOWED)
    focused = None
    if key not in policy_browser.PLAIN_KEYS:
        # Enter, Space and Ctrl/Meta+Enter act on whatever has the focus, so look at it first.
        try:
            focused = await client.describe_focused()
        except BrowserError as error:
            return _forbid(str(error))
        if focused is not None and not _valid_fingerprint(focused.get("fingerprint")):
            focused = None
    verdict = policy_browser.classify_press(key, focused)
    page_url = None
    if focused is None and verdict.risk == "gated":
        try:
            page_url = (await client.status()).get("url")
        except BrowserError as error:
            return _forbid(str(error))
    pinned: dict = {"key": key, "seen": _seen(focused, page_url)}
    if focused is not None:
        pinned["fingerprint"] = focused["fingerprint"]
        pinned["target"] = _target(focused)
        if verdict.risk == "safe" and _blocked_before(ctx, focused):
            verdict = policy_browser.Verdict(
                "gated", "form_submit", "this key press tried to submit a form before",
            )
    if verdict.risk == "safe":
        return Decision(Risk.SAFE, pinned=pinned)
    if focused is not None:
        summary, details = _element_card(focused, f"Press {key} on", verdict.why)
    else:
        summary = f"Press {key} on {_where(page_url)} · {verdict.why}"
        details = {"why": verdict.why, "page": _one_line(page_url, 300)}
    details["key"] = key
    return Decision(
        Risk.GATED, verdict.category, reason=verdict.why, summary=summary, details=details,
        pinned=pinned, card_screenshot=_card_screenshot(ctx),
    )


async def classify_select(ctx, args: dict) -> Decision:
    """Choosing an option is harmless, unless this list already tried to submit a form."""
    element, refusal = await _describe(ctx, args)
    if element is None:
        return refusal  # type: ignore[return-value]
    pinned = _pins(element)
    if not _blocked_before(ctx, element):
        return Decision(Risk.SAFE, pinned=pinned)
    why = "choosing an option here tried to submit a form before"
    summary, details = _element_card(element, "Choose an option in", why)
    return Decision(
        Risk.GATED, "form_submit", reason=why, summary=summary, details=details,
        pinned=pinned, card_screenshot=_card_screenshot(ctx),
    )


def _upload_file(ctx, path: object) -> tuple[dict | None, bytes, str]:
    """The workspace file to upload and its bytes, or why it can't be used. The returned
    info carries `sha256` of exactly those bytes."""
    if not isinstance(path, str) or not path.strip():
        return None, b"", "give the workspace path of the file to upload"
    too_big = f"the file is larger than {UPLOAD_MAX_BYTES // (1024 * 1024)} MB"
    try:
        workspace = workspace_from_ctx(ctx)
        info = workspace.info(path)
        if info["type"] != "file":
            return None, b"", "only regular files can be uploaded"
        if info["size"] > UPLOAD_MAX_BYTES:
            return None, b"", too_big
        data = workspace.read_bytes(info["path"], max_bytes=UPLOAD_MAX_BYTES + 1)
    except FileNotFoundError:
        return None, b"", f"file not found: {_one_line(path, 100)}"
    except (WorkspaceError, ValueError, OSError) as error:
        return None, b"", str(error)
    if len(data) > UPLOAD_MAX_BYTES:
        return None, b"", too_big
    return {**info, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, data, ""


async def classify_upload(ctx, args: dict) -> Decision:
    if _browser(ctx) is None:
        return _forbid("the browser is turned off")
    info, _data, problem = _upload_file(ctx, args.get("path"))
    if info is None:
        return _forbid(problem)
    element, refusal = await _describe(ctx, args)
    if element is None:
        return refusal  # type: ignore[return-value]
    if str(element.get("tag", "")).lower() != "input" or str(element.get("type", "")).lower() != "file":
        return _forbid("that element isn't a file upload field")
    why = f"sends {info['path']} ({info['size']} bytes) to the website"
    summary, details = _element_card(element, f"Upload {_one_line(info['path'], 80)} to", why)
    details["file"] = info["path"]
    details["size"] = info["size"]
    details["sha256"] = info["sha256"][:16]
    # The file's digest is pinned too: the bytes sent must be the bytes that were there when
    # Roland was asked, even if something rewrites the file while the card is waiting.
    return Decision(
        Risk.GATED, "upload", reason="uploading a file to a website", summary=summary,
        details=details, pinned={**_pins(element), "sha256": info["sha256"]},
        card_screenshot=_card_screenshot(ctx),
    )


CLASSIFIERS: dict[str, Callable[[object, dict], Awaitable[Decision]]] = {
    "browser_open": classify_open,
    "browser_click": classify_click,
    "browser_type": classify_type,
    "browser_press": classify_press,
    "browser_select": classify_select,
    "browser_upload": classify_upload,
}


# --- what the model reads ---

def _page_line(answer: dict) -> str:
    url = _one_line(answer.get("url"), 300) or "(no page)"
    title = _one_line(answer.get("title"), 120)
    return f"{url} — {title}" if title else url


def _action_result(ctx, tool: str, verb: str, answer: dict, target: object = "") -> str:
    """One short result for click/type/press/select/back/forward, from browserd's answer.
    With a target (see `_target`), a blocked form submission is remembered so the same call
    asks Roland next time; without one (history moves) there is nothing to approve, so it
    just stops."""
    target = target if isinstance(target, str) else ""
    blocked = answer.get("blocked_submission")
    if blocked:
        method, where = "POST", "a form"
        if isinstance(blocked, dict):
            method = _one_line(blocked.get("method"), 10).upper() or "POST"
            where = _where(blocked.get("url"), path=True)
        run = getattr(ctx, "run", None)
        if target and run is not None and hasattr(run, "blocked_submissions"):
            run.blocked_submissions.add(target)
        _audit(ctx, tool, ok=False, blocked_submission=f"{method} {where}")
        if not target:
            return (
                f"Not done: that would send a form again ({method} {where}), which isn't "
                "allowed here. Open the page you need with browser_open instead."
            )
        return (
            f"Not done: that tried to submit a form ({method} {where}), and submitting needs "
            f"Roland's approval. Call {tool} again in the same way to ask him."
        )
    if answer.get("ok") is False:
        _audit(ctx, tool, ok=False, url=_one_line(answer.get("url"), 300))
        return "Error: the browser couldn't do that. Take a new snapshot and look again."
    lines = [f"{verb}."]
    lines.append(("Now at " if answer.get("navigated") else "The page is ") + _page_line(answer))
    lines += _notes(answer)
    _audit(ctx, tool, ok=True, url=_one_line(answer.get("url"), 300), navigated=bool(answer.get("navigated")))
    return "\n".join(lines)


def _notes(answer: dict) -> list[str]:
    """Things browserd did on its own since the last call, in words for the model."""
    lines = []
    dialogs = answer.get("dialogs")
    for dialog in [item for item in dialogs if isinstance(item, dict)][:3] if isinstance(dialogs, list) else []:
        said = _one_line(dialog.get("message"), 160)
        kind = dialog.get("type")
        if kind == "confirm":
            lines.append(f"The page asked a yes/no question, and it was answered no: {said}")
        elif kind == "prompt":
            lines.append(f"The page asked for text in a box, and the box was cancelled: {said}")
        elif kind == "beforeunload":
            lines.append("The page asked whether to leave it, and it was answered no.")
        else:
            lines.append(f"The page showed a message box, and it was closed: {said}")
    background = answer.get("blocked_background")
    if isinstance(background, list) and background and isinstance(background[0], dict):
        method = _one_line(background[0].get("method"), 10).upper() or "POST"
        where = _where(background[0].get("url"), path=True)
        lines.append(f"The page tried to send a form by itself ({method} {where}). That was stopped.")
    if answer.get("popup_closed") is True:
        lines.append(
            "The page tried to open another tab, but too many are open, so it was closed. "
            "Close a tab with browser_close_tab if you need the new one."
        )
    return lines


def _label(element: dict) -> str:
    role = _one_line(element.get("role"), 20).lower()
    if role:
        return role
    tag = _one_line(element.get("tag"), 20).lower()
    kind = _one_line(element.get("type"), 20).lower()
    if tag == "a":
        return "link"
    if tag == "input":
        if kind in {"checkbox", "radio", "file"}:
            return kind
        if kind in {"submit", "button", "image", "reset"}:
            return "button"
        return "searchbox" if kind == "search" else "textbox"
    if tag == "textarea":
        return "textbox"
    if tag == "select":
        return "combobox"
    return tag or "element"


def _short_href(href: str, page_url: str) -> str:
    try:
        target, page = urlsplit(href[: policy_browser.MAX_URL_CHARS]), urlsplit(page_url)
    except ValueError:
        return _one_line(href, 80)
    if target.hostname and target.hostname == page.hostname:
        return _one_line(target.path + (f"?{target.query}" if target.query else ""), 80) or "/"
    return _one_line(href, 80)


def element_line(element: dict, page_url: str = "") -> str:
    """One snapshot line, e.g. `[e5] button "Place order" (submits form POST shop.example/checkout)`."""
    ref = element.get("ref")
    depth = element.get("depth")
    indent = "  " * min(depth, 6) if isinstance(depth, int) and depth > 0 else ""
    head = f"[{ref}] " if valid_ref(ref) else ""
    label = _label(element)
    level = element.get("level")
    if isinstance(level, int) and 1 <= level <= 6:
        label += f"({level})"
    name = _one_line(element.get("name"), 80)
    # Headings and page regions have no ref; an unnamed region is just its kind ("main").
    line = f'{indent}{head}{label} "{name}"' if head or name else f"{indent}{label}"
    href = element.get("href")
    if isinstance(href, str) and href and _label(element) == "link":
        line += f" -> {_short_href(href, page_url)}"
    if policy_browser.is_sensitive(element):
        line += " (sensitive, value hidden)"
    elif isinstance(element.get("value"), str) and element["value"]:
        line += f' value="{_one_line(element["value"], 60)}"'
    if element.get("checked") is True:
        line += " (checked)"
    options = element.get("options")
    if isinstance(options, list) and options:
        shown = " | ".join(_one_line(option, 30) for option in options[:12] if isinstance(option, str))
        extra = element.get("more_options")
        more = max(0, len(options) - 12) + (extra if isinstance(extra, int) and extra > 0 else 0)
        line += f" options: {_one_line(shown, 240)}" + (f" (+{more} more)" if more > 0 else "")
    if policy_browser.is_submit_control(element):
        form = policy_browser.form_line(element)
        line += f" (submits form {form})" if form else " (submits)"
    if element.get("disabled"):
        line += " (disabled)"
    return line


def format_snapshot(answer: dict, limit: int, start: int = 0) -> str:
    """The page as text for a text-only model: one line per element, then the visible text.
    Values of sensitive fields are dropped here as well, whatever browserd sent. A long page
    is read in pieces: `start` is the number of the first element line to show."""
    page_url = str(answer.get("url") or "")
    head = [f"URL: {_one_line(page_url, 300)}   Title: {_one_line(answer.get('title'), 120)}"]
    if answer.get("login_form_detected"):
        head.append(
            "This page has a sign-in form. Never type a password or code; tell Roland he has "
            "to sign in himself."
        )
    head += _notes(answer)
    elements = answer.get("elements")
    lines = [element_line(item, page_url) for item in elements if isinstance(item, dict)] \
        if isinstance(elements, list) else []
    start = max(0, min(start, len(lines)))
    # Later pieces are only the rest of the element list; the page text comes with the first.
    text = str(answer.get("text") or "").strip() if start == 0 else ""
    # The element list is what the model acts on, so it gets most of the room.
    room = max(200, int(limit * (0.6 if text else 0.9)))
    shown: list[str] = []
    used = 0
    for line in lines[start:]:
        if used + len(line) + 1 > room:
            break
        shown.append(line)
        used += len(line) + 1
    rest = len(lines) - start - len(shown)
    if start:
        head.append(f"(elements from number {start})")
    if rest > 0:
        shown.append(f"({rest} more elements not shown; call browser_snapshot with "
                     f"start={start + len(shown)} to see them)")
    parts = head + (shown or ["(no links, buttons or fields found)"])
    out = "\n".join(parts)
    if text:
        left = max(0, limit - len(out) - 40)
        cut = text[:left]
        out += "\n--- page text ---\n" + cut
        if len(cut) < len(text) or answer.get("truncated"):
            out += "\n[page text cut]"
    return out


def _snapshot_limit(ctx, args: dict) -> int:
    try:
        wanted = int(args.get("max_chars") or SNAPSHOT_DEFAULT)
    except (TypeError, ValueError):
        wanted = SNAPSHOT_DEFAULT
    wanted = max(SNAPSHOT_MIN, min(wanted, SNAPSHOT_MAX))
    # The loop cuts every tool output to MODEL_TOOL_OUTPUT_CHARS; plan for that, so the
    # element list is cut cleanly instead of losing its tail.
    cap = getattr(getattr(ctx, "config", None), "model_tool_output_chars", None)
    if isinstance(cap, int) and cap > 0:
        wanted = max(SNAPSHOT_MIN, min(wanted, cap - 150))
    return wanted


async def _share_file(ctx, path: str, size: int, note: str) -> None:
    """Show a saved picture to Roland in the chat (a `file` event), as attach_file does."""
    run = getattr(ctx, "run", None)
    if run is None or not hasattr(run, "events"):
        return
    name = PurePosixPath(path).name
    event = {"path": path, "name": name, "size": size, "mime": "image/png", "preview": True}
    await run.events.put({"type": "file", **event, "note": note})
    if getattr(run, "chat_id", None) is not None:
        try:
            ctx.memory.add_event(run.chat_id, "file", f"{note}: {path}", meta=event, run_id=run.run_id)
        except (TypeError, ValueError, OSError):
            pass  # the timeline row is best-effort; the card already went out


# --- handlers ---

async def browser_open(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    url = str(args.get("url") or "").strip()
    verdict = policy_browser.classify_open(url)
    if verdict.risk == "forbidden":
        return f"Error: {verdict.why}."
    if verdict.risk == "gated" and not _approved(ctx, "browser_open"):
        return NOT_CHECKED  # same rule as the classifier; only the gate can let this through
    try:
        answer = await client.navigate(url, bool(args.get("new_tab")))
    except BrowserError as error:
        return _fail(error)
    if answer.get("blocked"):
        _audit(ctx, "browser_open", ok=False, url=_one_line(url, 300), blocked=_one_line(answer["blocked"], 40))
        if answer["blocked"] == "leave_dialog":
            return (
                "Error: the page in this tab asked whether to leave it (it may hold unsaved "
                "changes), and that is never answered with yes. To leave it anyway, close the "
                "tab with browser_close_tab, or open the address with new_tab=true."
            )
        return "Error: the browser refused that address (private, local or not a web page)."
    if answer.get("download") is True:
        _audit(ctx, "browser_open", ok=True, url=_one_line(url, 300), download=True)
        return (
            "That address is a file, not a page, so the browser is downloading it. "
            "Use browser_downloads to see it; the tab still shows " + _page_line(answer)
        )
    _audit(ctx, "browser_open", ok=True, url=_one_line(answer.get("url") or url, 300))
    status = answer.get("status")
    lines = [f"Opened {_page_line(answer)}"]
    if isinstance(status, int) and status:
        lines.append(f"HTTP {status}")
    lines += _notes(answer)
    lines.append("Use browser_snapshot to read the page.")
    return "\n".join(lines)


async def browser_snapshot(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    limit = _snapshot_limit(ctx, args)
    try:
        start = max(0, int(args.get("start") or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        answer = await client.snapshot(limit)
    except BrowserError as error:
        return _fail(error)
    return format_snapshot(answer, limit, start)


async def browser_screenshot(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    try:
        png = await client.screenshot(bool(args.get("full_page")))
    except BrowserError as error:
        return _fail(error)
    width, height = struct.unpack(">II", png[16:24]) if len(png) >= 24 else (0, 0)
    path = f"screenshots/{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.png"
    run = getattr(ctx, "run", None)
    try:
        workspace_from_ctx(ctx).write_bytes(
            path, png, origin="browser",
            chat_id=getattr(run, "chat_id", None) if run is not None else None,
        )
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"
    await _share_file(ctx, path, len(png), "Browser screenshot")
    _audit(ctx, "browser_screenshot", ok=True, path=path, bytes=len(png))
    # Pictures are for Roland. Nothing here hands image data to the model (§6.5, M6.3).
    return (
        f"Saved {path} ({width}x{height}) and showed it to Roland. You can't see pictures; "
        "use browser_snapshot to read the page."
    )


async def browser_click(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    ref = args.get("ref")
    pin = _pin(args)
    fingerprint = pin.get("fingerprint")
    if not valid_ref(ref) or not _valid_fingerprint(fingerprint):
        return NOT_CHECKED
    if pin.get("disabled"):
        return "That element is disabled, so nothing was clicked."
    mode = _mode(ctx, "browser_click")
    if mode == "approved" and (changed := await _changed_since_approval(client, pin, ref)):
        return changed
    try:
        answer = await client.click(ref, fingerprint, mode)
    except ElementChanged as error:
        return CHANGED_AFTER_APPROVAL if mode == "approved" else _fail(error)
    except BrowserError as error:
        return _fail(error)
    return _action_result(ctx, "browser_click", "Clicked", answer, pin.get("target"))


async def browser_type(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    ref = args.get("ref")
    text = args.get("text")
    pin = _pin(args)
    fingerprint = pin.get("fingerprint")
    if not valid_ref(ref) or not _valid_fingerprint(fingerprint):
        return NOT_CHECKED
    if not isinstance(text, str) or len(text) > TEXT_MAX:
        return f"Error: give text of at most {TEXT_MAX} characters."
    submit = bool(args.get("submit"))
    mode = _mode(ctx, "browser_type")
    if submit and mode != "approved":
        return NOT_CHECKED  # submitting is only ever done with Roland's approval
    if mode == "approved" and (changed := await _changed_since_approval(client, pin, ref)):
        return changed
    try:
        answer = await client.type(
            ref, fingerprint, text, clear=args.get("clear") is not False, submit=submit, mode=mode,
        )
    except ElementChanged as error:
        return CHANGED_AFTER_APPROVAL if mode == "approved" else _fail(error)
    except BrowserError as error:
        return _fail(error)
    verb = f"Typed {len(text)} characters" + (" and submitted" if submit else "")
    return _action_result(ctx, "browser_type", verb, answer, pin.get("target"))


async def browser_press(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    pin = _pin(args)
    key = policy_browser.normal_key(args.get("key"))
    if key is None or pin.get("key") != key:
        return NOT_CHECKED
    fingerprint = pin.get("fingerprint") if _valid_fingerprint(pin.get("fingerprint")) else ""
    mode = _mode(ctx, "browser_press")
    if key in policy_browser.SUBMIT_COMBOS and mode != "approved":
        return NOT_CHECKED
    if mode == "approved" and (changed := await _changed_since_approval(client, pin, None)):
        return changed
    try:
        answer = await client.press(key, mode, fingerprint)
    except ElementChanged as error:
        return CHANGED_AFTER_APPROVAL if mode == "approved" else _fail(error)
    except BrowserError as error:
        return _fail(error)
    return _action_result(ctx, "browser_press", f"Pressed {key}", answer, pin.get("target"))


async def browser_select(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    ref = args.get("ref")
    values = args.get("values")
    if isinstance(values, str):
        values = [values]
    pin = _pin(args)
    fingerprint = pin.get("fingerprint")
    if not valid_ref(ref) or not _valid_fingerprint(fingerprint):
        return NOT_CHECKED
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= SELECT_MAX_VALUES
        or any(not isinstance(value, str) or len(value) > SELECT_VALUE_MAX for value in values)
    ):
        return f"Error: give 1 to {SELECT_MAX_VALUES} option names as text."
    mode = _mode(ctx, "browser_select")
    if mode == "approved" and (changed := await _changed_since_approval(client, pin, ref)):
        return changed
    try:
        answer = await client.select(ref, fingerprint, values, mode)
    except ElementChanged as error:
        return CHANGED_AFTER_APPROVAL if mode == "approved" else _fail(error)
    except BrowserError as error:
        return _fail(error)
    return _action_result(ctx, "browser_select", "Selected", answer, pin.get("target"))


async def browser_scroll(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    direction = str(args.get("direction") or "down").lower()
    if direction not in {"up", "down"}:
        return "Error: direction must be up or down."
    try:
        pages = max(1, min(int(args.get("pages") or 1), 10))
    except (TypeError, ValueError):
        pages = 1
    try:
        await client.scroll(direction, pages)
    except BrowserError as error:
        return _fail(error)
    return f"Scrolled {direction} {pages} screen(s). Take a new snapshot to see what is there."


async def _history(ctx, tool: str, verb: str) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    try:
        answer = await (client.back() if tool == "browser_back" else client.forward())
    except BrowserError as error:
        return _fail(error)
    return _action_result(ctx, tool, verb, answer)


async def browser_back(ctx, args: dict) -> str:
    return await _history(ctx, "browser_back", "Went back")


async def browser_forward(ctx, args: dict) -> str:
    return await _history(ctx, "browser_forward", "Went forward")


async def browser_tabs(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    try:
        answer = await client.status()
    except BrowserError as error:
        return _fail(error)
    tabs = answer.get("tabs")
    lines = []
    for tab in tabs[:MAX_LISTED] if isinstance(tabs, list) else []:
        if not isinstance(tab, dict) or not valid_tab(tab.get("id")):
            continue
        mark = " (active)" if tab.get("active") else ""
        lines.append(f"tab {tab['id']}{mark}: {_page_line(tab)}")
    return "\n".join(lines) or "No tabs are open. Use browser_open."


async def _tab(ctx, args: dict, tool: str) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    tab_id = args.get("tab_id")
    if not valid_tab(tab_id):
        return "Error: give a tab id from browser_tabs."
    try:
        if tool == "browser_close_tab":
            await client.close_tab(tab_id)
        else:
            await client.activate_tab(tab_id)
    except BrowserError as error:
        return _fail(error)
    _audit(ctx, tool, ok=True, tab=tab_id)
    return f"Closed tab {tab_id}." if tool == "browser_close_tab" else f"Switched to tab {tab_id}."


async def browser_switch_tab(ctx, args: dict) -> str:
    return await _tab(ctx, args, "browser_switch_tab")


async def browser_close_tab(ctx, args: dict) -> str:
    return await _tab(ctx, args, "browser_close_tab")


async def browser_wait(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    needle = args.get("text")
    if isinstance(needle, str) and needle.strip():
        needle = " ".join(needle.split()).lower()[:200]
        deadline = time.monotonic() + WAIT_MAX_S
        while True:
            try:
                answer = await client.snapshot(SNAPSHOT_MAX)
            except BrowserError as error:
                return _fail(error)
            if needle in " ".join(str(answer.get("text") or "").split()).lower():
                return "That text is on the page now."
            if time.monotonic() + 1 > deadline:
                return f"That text didn't show up within {WAIT_MAX_S} seconds."
            await asyncio.sleep(1)
    try:
        seconds = max(0.0, min(float(args.get("seconds") or 2), WAIT_MAX_S))
    except (TypeError, ValueError):
        seconds = 2.0
    await asyncio.sleep(seconds)
    return f"Waited {seconds:g} seconds."


async def browser_upload(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    ref = args.get("ref")
    pin = _pin(args)
    fingerprint = pin.get("fingerprint")
    if not valid_ref(ref) or not _valid_fingerprint(fingerprint) or not _approved(ctx, "browser_upload"):
        return NOT_CHECKED  # uploads only ever run with Roland's approval
    info, data, problem = _upload_file(ctx, args.get("path"))
    if info is None:
        return f"Error: {problem}."
    if not isinstance(pin.get("sha256"), str) or info["sha256"] != pin["sha256"]:
        return FILE_CHANGED_AFTER_APPROVAL
    if changed := await _changed_since_approval(client, pin, ref):
        return changed
    name = PurePosixPath(info["path"]).name
    try:
        # browserd can only read files under its own /files/uploads (workspace browser/uploads).
        # It gets the digest as well, so it can refuse a staged copy that was swapped.
        workspace_from_ctx(ctx).write_bytes(f"browser/uploads/{name}", data, overwrite=True, origin="agent")
        answer = await client.upload(ref, fingerprint, name, info["sha256"])
    except ElementChanged:
        return CHANGED_AFTER_APPROVAL
    except BrowserError as error:
        return _fail(error)
    except (WorkspaceError, ValueError, OSError) as error:
        return f"Error: {error}"
    return _action_result(ctx, "browser_upload", f"Uploaded {info['path']}", answer, pin.get("target"))


async def browser_downloads(ctx, args: dict) -> str:
    client = _browser(ctx)
    if client is None:
        return OFF
    try:
        items = await client.downloads()
    except BrowserError as error:
        return _fail(error)
    lines = []
    for item in items[:MAX_LISTED]:
        if not isinstance(item, dict):
            continue
        name = _one_line(item.get("name"), 120)
        if not name:
            continue
        size = item.get("size") if isinstance(item.get("size"), int) else 0
        state = "" if item.get("finished", True) else " (still downloading)"
        lines.append(f"browser/downloads/{name} ({size} bytes){state}")
    if len(items) > MAX_LISTED:
        lines.append(f"... and {len(items) - MAX_LISTED} more")
    return "\n".join(lines) or "Nothing has been downloaded."


S = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}

# (name, description for the model, parameters, required, handler). tools.py turns these into
# TOOLS entries. Descriptions stay under 160 characters: that is all the prompt keeps.
SPECS: tuple[tuple[str, str, dict, list[str], Callable[[object, dict], Awaitable[str]]], ...] = (
    ("browser_open", "Open a web page (http or https) in your browser. Then call browser_snapshot to read it.",
     {"url": S, "new_tab": BOOLEAN, "reason": S}, ["url"], browser_open),
    ("browser_snapshot", "Read the current page as text. Links, buttons and fields get a ref like e3 "
     "for browser_click, browser_type and browser_select.",
     {"max_chars": INTEGER, "start": INTEGER}, [], browser_snapshot),
    ("browser_screenshot", "Save a picture of the page for Roland. You can't see it yourself; use "
     "browser_snapshot to read the page.",
     {"full_page": BOOLEAN}, [], browser_screenshot),
    ("browser_click", "Click the element with this ref from the latest snapshot. Buying, sending, "
     "posting, deleting or submitting waits for Roland's approval.",
     {"ref": S, "reason": S}, ["ref"], browser_click),
    ("browser_type", "Type text into the field with this ref. Never passwords, codes or card numbers. "
     "To search: type, then browser_press Enter.",
     {"ref": S, "text": S, "clear": BOOLEAN, "submit": BOOLEAN, "reason": S}, ["ref", "text"], browser_type),
    ("browser_press", "Press one key: Enter, Tab, Shift+Tab, Escape, Space, Backspace, Delete, "
     "ArrowUp, ArrowDown, ArrowLeft, ArrowRight, PageUp, PageDown, Home, End.",
     {"key": S, "reason": S}, ["key"], browser_press),
    ("browser_select", "Choose one or more options in the drop-down list with this ref.",
     {"ref": S, "values": {"type": "array", "items": S}}, ["ref", "values"], browser_select),
    ("browser_scroll", "Scroll the page up or down by 1 to 10 screens.",
     {"direction": {"type": "string", "enum": ["up", "down"]}, "pages": INTEGER}, ["direction"], browser_scroll),
    ("browser_back", "Go back one page in the browser history.", {}, [], browser_back),
    ("browser_forward", "Go forward one page in the browser history.", {}, [], browser_forward),
    ("browser_tabs", "List the open browser tabs with their ids.", {}, [], browser_tabs),
    ("browser_switch_tab", "Switch to the browser tab with this id.", {"tab_id": S}, ["tab_id"], browser_switch_tab),
    ("browser_close_tab", "Close the browser tab with this id.", {"tab_id": S}, ["tab_id"], browser_close_tab),
    ("browser_wait", "Wait up to 10 seconds, or until some text shows up on the page.",
     {"seconds": INTEGER, "text": S}, [], browser_wait),
    ("browser_upload", "Put a workspace file into the file field with this ref. Always needs Roland's approval.",
     {"ref": S, "path": S, "reason": S}, ["ref", "path"], browser_upload),
    ("browser_downloads", "List files the browser downloaded. They are in browser/downloads/ in your workspace.",
     {}, [], browser_downloads),
)

BROWSER_TOOLS = frozenset(spec[0] for spec in SPECS)

__all__ = ["BROWSER_TOOLS", "CLASSIFIERS", "SPECS", "element_line", "format_snapshot"]
