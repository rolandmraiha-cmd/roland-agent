"""BrowserClient: HTTP client for browserd (§8.4).

browserd runs Chromium, so a hostile page might take it over. Everything it sends back is
treated as untrusted: sizes are capped, types are checked, and the text only ever reaches
the model inside the <tool_output> envelope.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

MAX_JSON_BYTES = 1_000_000      # one browserd JSON answer
MAX_SCREENSHOT_BYTES = 5_000_000  # §8.4: image/png <= 5 MB
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# What core may still ask browserd while Roland has the browser (§8.4). Everything else is
# refused here too, before it reaches the network: browserd forgets user mode if it restarts.
OPEN_WHILE_LOCKED = frozenset({"/healthz", "/v1/status", "/v1/user-mode", "/v1/vnc/disconnect"})

# What the model is told for each browserd error code. Unknown codes get a general line.
ERROR_TEXT = {
    "user_mode": "Roland is using the browser right now.",
    "element_changed": "the page changed since you last looked at that element. Take a new snapshot.",
    "sensitive_field": "that is a password, code or card field, and you must never type into it.",
    "no_such_element": "there is no element with that ref on the page now. Take a new snapshot.",
    "no_focused_element": "no element has the keyboard focus.",
    "no_such_tab": "there is no tab with that id. Use browser_tabs to list the tabs.",
    "too_many_tabs": "too many tabs are open. Close one with browser_close_tab first.",
    "disabled": "that element is disabled.",
    "not_typeable": "that element isn't a text field.",
    "not_a_file_input": "that element isn't a file upload field.",
    "not_a_select": "that element isn't a drop-down list.",
    "no_such_file": "that file isn't in the browser's upload folder.",
    "file_changed": "the file to upload changed, so nothing was sent.",
    "bad_key": "that key isn't allowed.",
    "blocked_url": "the browser refused that address.",
    "timeout": "the page took too long to respond.",
    "load_failed": "the page couldn't be loaded. The address may be wrong, or the site is down.",
    "not_clickable": "that element is hidden or something covers it. Take a new snapshot.",
    "no_such_option": "that option isn't in the list. The snapshot shows the options.",
    "unavailable": "the browser is starting up. Try again in a moment.",
    "too_large": "the picture was too large to take.",
}


class BrowserError(Exception):
    """A browserd call failed. `str(error)` is safe to show to the model."""

    code = "error"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class BrowserLocked(BrowserError):
    """HTTP 423: Roland has taken control of the browser (user mode)."""

    code = "user_mode"


class ElementChanged(BrowserError):
    """HTTP 409: the element no longer matches the fingerprint that was classified or approved."""

    code = "element_changed"


def _clean(value):
    """Text from a web page can hold half of a character pair (a lone surrogate), which can't
    be stored or sent on as UTF-8. Replace those, everywhere in a browserd answer."""
    if isinstance(value, str):
        return value if value.isascii() else value.encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {_clean(key): _clean(item) for key, item in value.items()}
    return value


def _code(raw: object) -> str:
    """A short, harmless form of browserd's error code (letters, digits, underscore)."""
    text = str(raw or "")[:60]
    return "".join(char for char in text if char.isascii() and (char.isalnum() or char == "_"))


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    body = b""
    async for part in response.aiter_bytes():
        body += part
        if len(body) > limit:
            raise BrowserError("the browser sent back more data than allowed.", "too_large")
    return body


@dataclass
class BrowserClient:
    url: str
    token: str = field(repr=False)
    action_timeout: float = 30
    nav_timeout: float = 45
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)
    # Set by core (M7): True while Roland controls the browser or a sign-in is waiting. Then
    # no read, action or capture is sent at all, whatever browserd itself believes.
    locked: Callable[[], bool] | None = field(default=None, repr=False)

    async def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        timeout: float | None = None,
        limit: int = MAX_JSON_BYTES,
        raw: bool = False,
        auth: bool = True,
    ):
        if self.locked is not None and path not in OPEN_WHILE_LOCKED and self.locked():
            raise BrowserLocked(ERROR_TEXT["user_mode"])
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        wait = (timeout if timeout is not None else self.action_timeout) + 10
        try:
            # trust_env=False: never route control traffic through a proxy from the environment.
            async with httpx.AsyncClient(
                trust_env=False, timeout=wait, transport=self.transport, follow_redirects=False,
            ) as client:
                async with client.stream(
                    method, f"{self.url.rstrip('/')}{path}", json=body, headers=headers,
                ) as response:
                    status = response.status_code
                    data = await _read_capped(response, limit)
        except httpx.TimeoutException as error:
            raise BrowserError("the browser took too long to answer.", "timeout") from error
        except httpx.HTTPError as error:
            raise BrowserError("the browser isn't reachable right now.", "unreachable") from error
        if status >= 400:
            raise self._error(status, data)
        if raw:
            return data
        try:
            parsed = json.loads(data)
        except ValueError as error:
            raise BrowserError("the browser sent an answer that couldn't be read.", "bad_answer") from error
        if not isinstance(parsed, (dict, list)):
            raise BrowserError("the browser sent an answer that couldn't be read.", "bad_answer")
        return _clean(parsed)

    @staticmethod
    def _error(status: int, data: bytes) -> BrowserError:
        code = ""
        try:
            parsed = json.loads(data[:4000])
            if isinstance(parsed, dict):
                code = _code(parsed.get("error"))
        except ValueError:
            code = ""
        if status == 423 or code == "user_mode":
            return BrowserLocked(ERROR_TEXT["user_mode"])
        if code == "element_changed":
            return ElementChanged(ERROR_TEXT["element_changed"])
        if code in ERROR_TEXT:
            return BrowserError(ERROR_TEXT[code], code)
        if status in {401, 403}:
            return BrowserError("the browser service refused the request.", "refused")
        if status == 409:
            return ElementChanged(ERROR_TEXT["element_changed"])
        label = f" ({code})" if code else ""
        return BrowserError(f"the browser couldn't do that{label}.", code or f"http_{status}")

    async def _dict(self, method: str, path: str, body: dict | None = None, **kw) -> dict:
        answer = await self._call(method, path, body, **kw)
        if not isinstance(answer, dict):
            raise BrowserError("the browser sent an answer that couldn't be read.", "bad_answer")
        return answer

    async def healthy(self) -> bool:
        try:
            answer = await self._dict("GET", "/healthz", timeout=3, auth=False)
        except BrowserError:
            return False
        return answer.get("ok") is True

    async def status(self) -> dict:
        return await self._dict("GET", "/v1/status")

    async def navigate(self, url: str, new_tab: bool = False) -> dict:
        return await self._dict(
            "POST", "/v1/navigate", {"url": url, "new_tab": bool(new_tab)}, timeout=self.nav_timeout,
        )

    async def snapshot(self, max_chars: int) -> dict:
        return await self._dict("POST", "/v1/snapshot", {"max_chars": int(max_chars)})

    async def describe(self, ref: str) -> dict:
        return await self._dict("POST", "/v1/describe", {"ref": ref})

    async def describe_focused(self) -> dict | None:
        """The element that has the keyboard focus, or None when nothing has it."""
        try:
            return await self._dict("POST", "/v1/describe", {"focused": True})
        except BrowserError as error:
            if error.code == "no_focused_element":
                return None
            raise

    async def click(self, ref: str, fingerprint: str, mode: str) -> dict:
        return await self._dict("POST", "/v1/click", {"ref": ref, "fingerprint": fingerprint, "mode": mode})

    async def type(
        self, ref: str, fingerprint: str, text: str, *, clear: bool, submit: bool, mode: str,
    ) -> dict:
        body = {
            "ref": ref, "fingerprint": fingerprint, "text": text,
            "clear": bool(clear), "submit": bool(submit), "mode": mode,
        }
        return await self._dict("POST", "/v1/type", body)

    async def press(self, key: str, mode: str, fingerprint: str = "") -> dict:
        body = {"key": key, "mode": mode}
        if fingerprint:
            body["fingerprint"] = fingerprint  # the focused element must still be this one
        return await self._dict("POST", "/v1/press", body)

    async def select(self, ref: str, fingerprint: str, values: list[str], mode: str = "safe") -> dict:
        body = {"ref": ref, "fingerprint": fingerprint, "values": values, "mode": mode}
        return await self._dict("POST", "/v1/select", body)

    async def scroll(self, direction: str, pages: int) -> dict:
        return await self._dict("POST", "/v1/scroll", {"direction": direction, "pages": int(pages)})

    async def back(self) -> dict:
        return await self._dict("POST", "/v1/back", timeout=self.nav_timeout)

    async def forward(self) -> dict:
        return await self._dict("POST", "/v1/forward", timeout=self.nav_timeout)

    async def activate_tab(self, tab_id: str) -> dict:
        return await self._dict("POST", f"/v1/tabs/{tab_id}/activate")

    async def close_tab(self, tab_id: str) -> dict:
        return await self._dict("POST", f"/v1/tabs/{tab_id}/close")

    async def screenshot(self, full_page: bool = False) -> bytes:
        data = await self._call(
            "POST", "/v1/screenshot", {"full_page": bool(full_page)},
            limit=MAX_SCREENSHOT_BYTES, raw=True,
        )
        if not data.startswith(PNG_MAGIC):
            raise BrowserError("the browser sent a screenshot that isn't a PNG image.", "bad_answer")
        return data

    async def upload(self, ref: str, fingerprint: str, path: str, sha256: str = "") -> dict:
        body = {"ref": ref, "fingerprint": fingerprint, "path": path}
        if sha256:
            body["sha256"] = sha256  # browserd must refuse a staged file with another digest
        return await self._dict("POST", "/v1/upload", body)

    async def downloads(self) -> list:
        answer = await self._call("GET", "/v1/downloads")
        if not isinstance(answer, list):
            raise BrowserError("the browser sent an answer that couldn't be read.", "bad_answer")
        return answer

    async def user_mode(self, on: bool) -> str:
        """Hand the browser to Roland (True) or back to the agent (False). Returns the mode
        browserd reports afterwards; anything but the mode asked for is an error."""
        answer = await self._dict("POST", "/v1/user-mode", {"on": bool(on)})
        mode = answer.get("mode")
        if mode != ("user" if on else "agent"):
            raise BrowserError("the browser didn't change who controls it.", "bad_answer")
        return mode

    async def vnc_disconnect(self) -> None:
        """Drop every screen-sharing connection to the browser (§6.6)."""
        await self._dict("POST", "/v1/vnc/disconnect", timeout=10)
