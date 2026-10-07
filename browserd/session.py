"""One persistent, headed Chromium driven for the agent (§6.5).

What this module guarantees, whatever the page or the model does:
  * only http and https addresses that aren't private are requested (navigation guard);
  * a form submission (a POST navigation) only goes out while Roland's approval for that exact
    action is being carried out, or while he is using the browser himself (POST guard);
  * dialogs are dismissed, never accepted;
  * there is no way to read cookies, storage or field values of secret fields, and no way to
    run script supplied from outside: the only script is the fixed snapshot.js next to this file.
Everything the page reports about itself is untrusted and is only passed on as data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import stat
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import guards
from .settings import (
    FULL_PAGE_MAX_HEIGHT,
    MAX_ANSWER_BYTES,
    MAX_DOWNLOAD_BYTES,
    MAX_ELEMENTS,
    MAX_SCREENSHOT_BYTES,
    MAX_SNAPSHOT_CHARS,
    MAX_UPLOAD_BYTES,
    MAX_URL_CHARS,
    Settings,
)

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout
except ImportError:  # the server's unit tests run without Playwright installed

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        pass

    class PlaywrightTimeout(PlaywrightError):  # type: ignore[no-redef]
        pass


log = logging.getLogger("browserd")

# Spec §6.5. No remote-debugging port is ever opened: Playwright talks to Chromium over a pipe.
CHROMIUM_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-features=AutofillServerCommunication,OptimizationHints,MediaRouter",
)
# Written into the profile before every start, in case managed policies aren't honoured.
PROFILE_PREFERENCES = {
    "credentials_enable_service": False,
    "credentials_enable_autosignin": False,
    "profile": {"password_manager_enabled": False, "password_manager_leak_detection": False},
    "autofill": {"enabled": False, "profile_enabled": False, "credit_card_enabled": False},
    "signin": {"allowed": False, "allowed_on_next_startup": False},
    "translate": {"enabled": False},
    "browser": {"check_default_browser": False},
    "download": {"prompt_for_download": False},
}

EVAL_TIMEOUT_S = 8.0          # one run of snapshot.js; a page that hogs its thread gives "timeout"
CLICK_TIMEOUT_MS = 10_000     # waiting for an element to be visible, still and not covered
SAFE_SETTLE = (0.4, 5.0)      # (quiet seconds, longest wait) after an action
APPROVED_SETTLE = (2.0, 10.0)  # §6.5: approved submissions may go out until 2 s quiet or 10 s
MAX_FRAMES = 10
MAX_NOTES = 20
TEXT_TYPES = frozenset({"", "text", "tel", "number", "email", "url", "search", "password"})

SCROLL_JS = "(dy) => window.scrollBy({ top: dy, left: 0, behavior: 'instant' })"
BLUR_JS = "() => { const el = document.activeElement; if (el && typeof el.blur === 'function') el.blur(); }"
HEIGHT_JS = (
    "() => Math.max(document.documentElement ? document.documentElement.scrollHeight : 0,"
    " document.body ? document.body.scrollHeight : 0)"
)


class BrowserdError(Exception):
    """A refusal or failure with a short code that core knows how to explain (§8.4)."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass
class Tab:
    id: str
    page: object
    next_ref: int = 1
    doc: str = field(default_factory=lambda: secrets.token_hex(8))
    navigations: int = 0


@dataclass
class _Action:
    tab: Tab
    url: str
    navigations: int
    approved: bool
    blocked: list[dict] = field(default_factory=list)


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def _text(value: object, limit: int) -> str:
    return guards.clean(value[:limit]) if isinstance(value, str) else ""


def sane_element(raw: object) -> dict | None:
    """Keep only the fields we expect from snapshot.js, with their types and sizes enforced.
    The script runs inside the page, so its answer is treated like any other page data."""
    if not isinstance(raw, dict):
        return None
    ref = raw.get("ref")
    out: dict = {
        "ref": ref if guards.valid_ref(ref) else "",
        "tag": _text(raw.get("tag"), 30).lower(),
        "role": _text(raw.get("role"), 30).lower(),
        "name": _text(raw.get("name"), 200),
        "type": _text(raw.get("type"), 30).lower(),
        "href": _text(raw.get("href"), MAX_URL_CHARS),
        "value": _text(raw.get("value"), 200),
        "aria_label": _text(raw.get("aria_label"), 200),
        "title_attr": _text(raw.get("title_attr"), 200),
        "in_form": raw.get("in_form") is True,
        "form_method": _text(raw.get("form_method"), 10).lower(),
        "form_action": _text(raw.get("form_action"), MAX_URL_CHARS),
        "form_submit_name": _text(raw.get("form_submit_name"), 200),
        "submits": raw.get("submits") is True,
        "disabled": raw.get("disabled") is True,
        "sensitive": raw.get("sensitive") is True,
        "aria_expanded": raw.get("aria_expanded") if isinstance(raw.get("aria_expanded"), bool) else None,
        "aria_haspopup": raw.get("aria_haspopup") is True,
        "contenteditable": raw.get("contenteditable") is True,
        "inside_dialog_title": _text(raw.get("inside_dialog_title"), 80),
    }
    if out["sensitive"] or out["type"] == "password":
        out["sensitive"] = True
        out["value"] = ""  # never passed on, whatever the page script said
    if isinstance(raw.get("checked"), bool):
        out["checked"] = raw["checked"]
    options = raw.get("options")
    if isinstance(options, list):
        out["options"] = [_text(item, 60) for item in options[:20] if isinstance(item, str)]
        more = raw.get("more_options")
        out["more_options"] = more if isinstance(more, int) and 0 <= more < 100_000 else 0
    if raw.get("focused") is True:
        out["focused"] = True
    out["fingerprint"] = guards.fingerprint(out)
    return out


def sane_structure(raw: object) -> dict | None:
    """A heading or page region in a snapshot: no ref, nothing to act on."""
    if not isinstance(raw, dict):
        return None
    out: dict = {"role": _text(raw.get("role"), 30).lower() or "region", "name": _text(raw.get("name"), 200)}
    level = raw.get("level")
    if isinstance(level, int) and 1 <= level <= 6:
        out["level"] = level
    return out


def merge_preferences(existing: object, wanted: dict) -> dict:
    out = dict(existing) if isinstance(existing, dict) else {}
    for key, value in wanted.items():
        out[key] = merge_preferences(out.get(key), value) if isinstance(value, dict) else value
    return out


def read_upload(folder: Path, name: str) -> bytes:
    """Read one staged upload without following links. Raises BrowserdError if it can't be used."""
    if not guards.valid_upload_name(name):
        raise BrowserdError("no_such_file", 404)
    try:
        handle = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise BrowserdError("no_such_file", 404) from error
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_UPLOAD_BYTES:
            raise BrowserdError("no_such_file", 404)
        chunks = []
        left = MAX_UPLOAD_BYTES + 1
        while left > 0:
            part = os.read(handle, min(left, 1 << 20))
            if not part:
                break
            chunks.append(part)
            left -= len(part)
    finally:
        os.close(handle)
    data = b"".join(chunks)
    if len(data) > MAX_UPLOAD_BYTES:
        raise BrowserdError("no_such_file", 404)
    return data


class Session:
    def __init__(self, settings: Settings):
        self.s = settings
        self.mode = "agent"
        self.browser_ok = False
        self.on_dead = None  # called once if Chromium goes away, so the service can restart
        self._js = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
        self._lock = asyncio.Lock()
        self._pw = None
        self._context = None
        self._stopping = False
        self._tabs: dict[str, Tab] = {}
        self._active: str | None = None
        self._seq = 0
        self._action: _Action | None = None
        self._posts_open_until = 0.0
        self._last_request = 0.0
        self._refused: str | None = None
        self._dialogs: list[dict] = []
        self._background: list[dict] = []
        self._popups_closed = 0
        self._incoming: dict[int, dict] = {}
        self._download_seq = 0
        self._replacing = False
        self._tasks: set[asyncio.Task] = set()

    # --- lifecycle ---

    def prepare_profile(self) -> None:
        """Folders, stale locks and our preferences. Runs before every Chromium start."""
        profile = self.s.profile_dir
        for folder in (profile, profile / "Default", self.s.downloads_dir, self.s.uploads_dir, self._partial_dir):
            folder.mkdir(parents=True, exist_ok=True)
        # A lock left by a browser that was killed names the old container, and Chromium
        # then refuses to start. We are the only user of this profile.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (profile / name).unlink()
            except OSError:
                pass
        for leftover in self._partial_dir.iterdir():
            try:
                if not leftover.is_dir() or leftover.is_symlink():
                    leftover.unlink()
            except OSError:
                pass
        target = profile / "Default" / "Preferences"
        try:
            existing = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        except (OSError, ValueError):
            return  # leave a file we can't read for Chromium to deal with
        wanted = merge_preferences({}, PROFILE_PREFERENCES)
        wanted["download"]["default_directory"] = str(self.s.downloads_dir)
        temp = target.with_name(f".Preferences.{secrets.token_hex(4)}")
        temp.write_text(json.dumps(merge_preferences(existing, wanted)), encoding="utf-8")
        os.replace(temp, target)

    @property
    def _partial_dir(self) -> Path:
        return self.s.files_dir / ".incoming"

    def _chromium_env(self) -> dict[str, str]:
        """Chromium gets a small environment of its own: no token and nothing it doesn't need."""
        home = os.environ.get("HOME") or "/tmp/home"  # noqa: S108 -- the image's throwaway home
        env = {
            "HOME": home,
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "TZ": self.s.timezone,
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache",
        }
        if not self.s.headless:
            env["DISPLAY"] = self.s.display
        return env

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.prepare_profile()
        width, height = self.s.viewport
        args = [*CHROMIUM_ARGS, f"--host-resolver-rules={guards.resolver_rules(self.s.allow_private_hosts)}"]
        options: dict = {}
        if self.s.headless:
            options["viewport"] = {"width": width, "height": height}
        else:
            # The window fills the virtual screen, so what Roland will see there (M7) is
            # exactly what the agent's screenshots show.
            args += [f"--window-size={width},{height}", "--window-position=0,0"]
            options["no_viewport"] = True
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.s.profile_dir),
            headless=self.s.headless,
            args=args,
            env=self._chromium_env(),
            locale=self.s.locale,
            timezone_id=self.s.timezone,
            accept_downloads=True,
            downloads_path=str(self._partial_dir),
            chromium_sandbox=self.s.chromium_sandbox,
            service_workers="block",  # so every request passes the guards below
            handle_sigint=False,
            handle_sigterm=False,
            handle_sighup=False,
            **options,
        )
        context = self._context
        context.set_default_timeout(self.s.action_timeout_s * 1000)
        context.set_default_navigation_timeout(self.s.nav_timeout_s * 1000)
        await context.route("**/*", self._on_route)
        context.on("request", self._on_request)
        context.on("page", self._on_page)
        context.on("close", self._on_context_closed)
        for page in context.pages:
            self._on_page(page)
        if not self._tabs:
            self._on_page(await context.new_page())
        self.browser_ok = True

    async def stop(self) -> None:
        self._stopping = True
        self.browser_ok = False
        for task in list(self._tasks):
            task.cancel()
        try:
            if self._context is not None:
                await asyncio.wait_for(self._context.close(), timeout=15)  # flushes cookies to disk
        except Exception:  # noqa: S110 -- shutting down anyway
            pass
        try:
            if self._pw is not None:
                await asyncio.wait_for(self._pw.stop(), timeout=10)
        except Exception:  # noqa: S110
            pass

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_context_closed(self, *_args) -> None:
        self.browser_ok = False
        if not self._stopping and self.on_dead is not None:
            self.on_dead()

    # --- tabs ---

    def _on_page(self, page) -> None:
        if any(tab.page is page for tab in self._tabs.values()):
            return
        if len(self._tabs) >= self.s.max_tabs and not self._replacing:
            self._popups_closed += 1
            self._spawn(self._close_quietly(page))
            return
        self._seq += 1
        tab = Tab(f"t{self._seq}", page)
        self._tabs[tab.id] = tab
        self._active = tab.id
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)
        page.on("framenavigated", lambda frame, tab=tab: self._on_navigated(tab, frame))
        page.on("close", lambda _page, tab=tab: self._on_tab_closed(tab))
        page.on("crash", lambda _page, tab=tab: self._spawn(self._drop_crashed(tab)))

    async def _close_quietly(self, page) -> None:
        try:
            await page.close()
        except Exception:  # noqa: S110 -- already gone
            pass

    async def _blank_tab(self) -> None:
        """A new empty tab, even at the limit. Chromium quits when its last tab closes, so the
        last one is always replaced before it goes."""
        self._replacing = True
        try:
            self._on_page(await self._context.new_page())
        finally:
            self._replacing = False

    async def _drop_crashed(self, tab: Tab) -> None:
        try:
            async with self._lock:
                if tab.id in self._tabs and len(self._tabs) == 1:
                    await self._blank_tab()
        except Exception:  # noqa: S110 -- the whole browser is going; on_dead handles that
            pass
        await self._close_quietly(tab.page)

    def _on_tab_closed(self, tab: Tab) -> None:
        self._tabs.pop(tab.id, None)
        if self._active == tab.id:
            self._active = next(reversed(self._tabs), None)
        if not self._tabs and not self._stopping and self.browser_ok:
            self._spawn(self._open_blank())

    async def _open_blank(self) -> None:
        """A page closed itself and it was the last one. Try to give the next action
        somewhere to happen; if Chromium is already quitting, on_dead restarts the service."""
        try:
            async with self._lock:
                if not self._tabs:
                    await self._blank_tab()
        except Exception:  # noqa: S110
            pass

    def _on_navigated(self, tab: Tab, frame) -> None:
        try:
            if frame is not tab.page.main_frame:
                return
            tab.navigations += 1
            if not guards.main_frame_url_ok(frame.url):
                self._refused = "scheme"
                self._spawn(self._leave(tab))
        except Exception:  # noqa: S110 -- the tab closed under us
            pass

    async def _leave(self, tab: Tab) -> None:
        try:
            await tab.page.goto("about:blank", timeout=5000)
        except Exception:
            await self._close_quietly(tab.page)

    async def _on_dialog(self, dialog) -> None:
        """alert, confirm, prompt and "leave this page?" are all answered with No."""
        if self.mode == "user":
            return  # Roland is at the controls and answers them himself (M7)
        try:
            entry = {"type": _text(dialog.type, 20), "message": _text(dialog.message, 200)}
            if len(self._dialogs) < MAX_NOTES:
                self._dialogs.append(entry)
            await dialog.dismiss()
        except Exception:  # noqa: S110 -- the page closed the dialog first
            pass

    async def _on_download(self, download) -> None:
        self._download_seq += 1
        key = self._download_seq
        name = guards.safe_download_name(download.suggested_filename)
        entry = {"name": name, "size": 0, "finished": False}
        self._incoming[key] = entry
        try:
            folder = self.s.downloads_dir
            target = folder / name
            stem, dot, extension = name.rpartition(".")
            number = 1
            while target.exists() or target.is_symlink():
                number += 1
                target = folder / (f"{stem}-{number}{dot}{extension}" if dot and stem else f"{name}-{number}")
            entry["name"] = target.name
            await download.save_as(target)
            size = target.stat().st_size
            if size > MAX_DOWNLOAD_BYTES:
                target.unlink()
                entry["failed"] = True
            else:
                entry.update(size=size, finished=True)
        except Exception:
            entry["failed"] = True
        finally:
            try:
                await download.delete()
            except Exception:  # noqa: S110
                pass
            self._incoming.pop(key, None)

    def _tab(self) -> Tab:
        tab = self._tabs.get(self._active or "")
        if tab is None:
            raise BrowserdError("unavailable", 503)
        return tab

    # --- guards on every request the browser makes ---

    def _on_request(self, request) -> None:
        self._last_request = time.monotonic()
        try:
            # Playwright doesn't hand redirects to _on_route. Chromium's resolver rules stop a
            # redirect to a private address; this is only so the answer says why.
            if request.redirected_from is not None and request.is_navigation_request():
                reason = guards.url_block_reason(request.url, self.s.allow_private_hosts)
                if reason:
                    self._refused = reason
        except Exception:  # noqa: S110 -- the page went away
            pass

    def _stops_post(self, request) -> bool:
        if self.mode != "agent" or time.monotonic() < self._posts_open_until:
            return False
        if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return False
        kind = request.resource_type
        if kind == "document":
            return True  # a form being submitted, in the tab or in a frame
        return self.s.block_background_posts and kind in {"fetch", "xhr", "ping", "eventsource", "other"}

    async def _on_route(self, route) -> None:
        try:
            request = route.request
            reason = guards.url_block_reason(request.url, self.s.allow_private_hosts)
            # "aborted" stops a navigation the way the Stop button does: the tab stays on the
            # page it was on, so the agent can still see it and ask Roland properly.
            if reason:
                if request.is_navigation_request():
                    self._refused = reason
                await route.abort("aborted")
                return
            if self._stops_post(request):
                entry = {"method": _text(request.method.upper(), 10), "url": _text(request.url, MAX_URL_CHARS)}
                bucket = self._action.blocked if self._action is not None else self._background
                if len(bucket) < MAX_NOTES:
                    bucket.append(entry)
                await route.abort("aborted")
                return
            await route.continue_()
        except Exception:
            try:
                await route.abort("aborted")  # never let a request through undecided
            except Exception:  # noqa: S110 -- the page or tab went away
                pass

    # --- helpers for actions ---

    @asynccontextmanager
    async def _op(self):
        if not self.browser_ok:
            raise BrowserdError("unavailable", 503)
        async with self._lock:
            try:
                yield
            except BrowserdError:
                raise
            except PlaywrightTimeout as error:
                raise BrowserdError("timeout", 504) from error
            except PlaywrightError as error:
                # Messages can quote the page, so only the kind of failure is kept.
                closed = "closed" in str(error).lower()
                raise BrowserdError("unavailable" if closed else "failed", 503 if closed else 500) from error

    async def _eval(self, awaitable, timeout: float = EVAL_TIMEOUT_S):
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except TimeoutError as error:
            raise BrowserdError("timeout", 504) from error

    def _frames(self, page) -> list:
        """The tab's own frame and frames from the same site inside it (§6.5)."""
        main = page.main_frame
        origin = _origin(main.url)
        kept = [main]
        for frame in page.frames:
            if frame is main or len(kept) >= MAX_FRAMES:
                continue
            try:
                if frame.is_detached() or frame.parent_frame not in kept:
                    continue
                url = frame.url
            except Exception:  # noqa: S112 -- the frame went away
                continue
            if url.startswith("about:") or _origin(url) == origin:
                kept.append(frame)
        return kept

    async def _frame_name(self, frame) -> str:
        """What the page calls a frame: its title, like a screen reader would say it."""
        try:
            holder = await asyncio.wait_for(frame.frame_element(), timeout=3)
            for attribute in ("title", "aria-label", "name"):
                value = await asyncio.wait_for(holder.get_attribute(attribute), timeout=3)
                if value and value.strip():
                    return _text(value.strip(), 80)
        except Exception:  # noqa: S110 -- the frame went away; it just has no name then
            pass
        return ""

    async def _where(self, tab: Tab) -> tuple[str, str]:
        url = _text(tab.page.url, MAX_URL_CHARS)
        try:
            title = _text(await asyncio.wait_for(tab.page.title(), timeout=3), 300)
        except Exception:
            title = ""
        return url, title

    async def _settle(self, quiet: float, longest: float) -> None:
        """Wait until the page has stopped asking for things, or long enough."""
        started = time.monotonic()
        self._last_request = started
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - started >= longest or now - self._last_request >= quiet:
                return

    @asynccontextmanager
    async def _acting(self, tab: Tab, mode: str):
        act = _Action(tab, tab.page.url, tab.navigations, approved=mode == "approved")
        quiet, longest = APPROVED_SETTLE if act.approved else SAFE_SETTLE
        self._action = act
        if act.approved:
            self._posts_open_until = time.monotonic() + longest
        try:
            yield act
            await self._settle(quiet, longest)
        finally:
            self._posts_open_until = 0.0
            self._action = None

    async def _answer(self, act: _Action | None = None, **extra) -> dict:
        tab = self._tab()
        url, title = await self._where(tab)
        out: dict = {"ok": True, "mode": self.mode, "url": url, "title": title, **extra}
        if act is not None:
            out["navigated"] = tab is not act.tab or tab.navigations != act.navigations or url != act.url
            if act.blocked:
                out["ok"] = False
                out["blocked_submission"] = act.blocked[0]
        if self._dialogs:
            out["dialogs"], self._dialogs = self._dialogs, []
        if self._background:
            out["blocked_background"], self._background = self._background[:5], []
        if self._popups_closed:
            out["popup_closed"], self._popups_closed = True, 0
        return out

    async def _locate(self, tab: Tab, ref: object):
        if not guards.valid_ref(ref):
            raise BrowserdError("no_such_element", 404)
        found = []
        for frame in self._frames(tab.page):
            locator = frame.locator(f'[data-ra-ref="{ref}"]')
            try:
                count = await asyncio.wait_for(locator.count(), timeout=5)
            except Exception:  # noqa: S112 -- a frame that is navigating has nothing to find
                continue
            found.extend(locator.nth(index) for index in range(min(count, 2)))
        if len(found) != 1:  # none, or the page copied the label onto a second element
            raise BrowserdError("no_such_element", 404)
        return found[0]

    async def _facts(self, locator) -> dict:
        raw = await self._eval(
            locator.evaluate(self._js, {"op": "describe", "sensitive": self.s.sensitive_match}, timeout=5000)
        )
        element = sane_element(raw) if not (isinstance(raw, dict) and raw.get("error")) else None
        if element is None:
            raise BrowserdError("no_such_element", 404)
        return element

    async def _focused(self, tab: Tab) -> dict | None:
        for frame in reversed(self._frames(tab.page)):
            try:
                raw = await self._eval(
                    frame.evaluate(self._js, {"op": "focused", "sensitive": self.s.sensitive_match})
                )
            except BrowserdError:
                continue
            if isinstance(raw, dict) and not raw.get("error"):
                return sane_element(raw)
        return None

    async def _checked(self, tab: Tab, ref: object, fingerprint: object):
        """The element for this ref, if it is still exactly what core looked at."""
        if not isinstance(fingerprint, str) or not fingerprint:
            raise BrowserdError("bad_request", 400)
        locator = await self._locate(tab, ref)
        element = await self._facts(locator)
        if not secrets.compare_digest(element["fingerprint"], fingerprint):
            raise BrowserdError("element_changed", 409)
        return locator, element

    # --- what core can ask for (§8.4) ---

    def health(self) -> dict:
        screen = self.s.headless or Path(f"/tmp/.X11-unix/X{self.s.display.lstrip(':')}").exists()  # noqa: S108
        return {"ok": self.browser_ok, "xvfb": bool(screen), "vnc": False, "browser": self.browser_ok}

    async def status(self) -> dict:
        if not self.browser_ok:
            raise BrowserdError("unavailable", 503)
        tabs = []
        active_url = active_title = ""
        for tab in list(self._tabs.values()):
            url, title = await self._where(tab)
            tabs.append({"id": tab.id, "url": url, "title": title, "active": tab.id == self._active})
            if tab.id == self._active:
                active_url, active_title = url, title
        return {"mode": self.mode, "tabs": tabs, "url": active_url, "title": active_title}

    async def set_user_mode(self, on: bool) -> dict:
        """Roland takes the controls (the screen for that arrives in M7), or gives them back."""
        async with self._lock:  # an action that is under way finishes first
            was_user, self.mode = self.mode == "user", "user" if on else "agent"
            if was_user and not on:
                await self._hand_back()
        return {"mode": self.mode}

    async def _hand_back(self) -> None:
        """Take the focus off whatever Roland was typing in, and empty the screen's clipboard,
        so nothing of his is left where the next action could meet it. Nothing is read."""
        for tab in list(self._tabs.values()):
            for frame in self._frames(tab.page):
                try:
                    await asyncio.wait_for(frame.evaluate(BLUR_JS), timeout=3)
                except Exception:  # noqa: S110 -- a frame that is busy or gone
                    pass
        if self.s.headless:
            return
        for selection in ("--primary", "--clipboard"):
            try:
                process = await asyncio.create_subprocess_exec(
                    "xsel", "--clear", selection,
                    env={"DISPLAY": self.s.display, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(process.wait(), timeout=3)
            except (OSError, TimeoutError):
                pass

    async def navigate(self, url: object, new_tab: bool) -> dict:
        reason = guards.url_block_reason(url, self.s.allow_private_hosts)
        async with self._op():
            if reason:
                return await self._answer(status=0, blocked=reason)
            if new_tab:
                if len(self._tabs) >= self.s.max_tabs:
                    raise BrowserdError("too_many_tabs", 400)
                self._on_page(await self._context.new_page())
            tab = self._tab()
            self._refused = None
            downloads = self._download_seq
            status = 0
            failed = False
            async with self._acting(tab, "safe") as act:
                try:
                    response = await tab.page.goto(
                        url, timeout=self.s.nav_timeout_s * 1000, wait_until="domcontentloaded",
                    )
                    status = response.status if response is not None else 0
                except PlaywrightTimeout as error:
                    raise BrowserdError("timeout", 504) from error
                except PlaywrightError:
                    failed = True
            extra: dict = {"status": status}
            if failed and self._refused:
                extra["blocked"] = self._refused
            elif failed and any(note["type"] == "beforeunload" for note in self._dialogs):
                # The page asked "leave without saving?" and the answer is always no.
                extra["blocked"] = "leave_dialog"
            elif failed and self._download_seq != downloads:
                extra["download"] = True
            elif failed:
                raise BrowserdError("load_failed", 502)
            answer = await self._answer(act, **extra)
            answer.pop("navigated", None)
            if answer.pop("blocked_submission", None):  # a page that posts a form as it loads
                answer["ok"] = True
                answer.setdefault("blocked_background", []).extend(act.blocked[:5])
            return answer

    async def snapshot(self, max_chars: int) -> dict:
        max_chars = max(0, min(int(max_chars), MAX_SNAPSHOT_CHARS))
        async with self._op():
            tab = self._tab()
            elements: list[dict] = []
            texts: list[str] = []
            truncated = False
            login = False
            for index, frame in enumerate(self._frames(tab.page)):
                room = MAX_ELEMENTS - sum(1 for item in elements if item.get("ref"))
                args = {
                    "op": "snapshot", "sensitive": self.s.sensitive_match, "main": index == 0,
                    "doc": tab.doc, "next_ref": tab.next_ref, "max_elements": max(1, room),
                    "max_chars": max(0, max_chars - sum(len(text) for text in texts)),
                }
                try:
                    data = await self._eval(frame.evaluate(self._js, args))
                except (BrowserdError, PlaywrightError):
                    if index == 0:
                        raise
                    continue  # a frame that went away mid-snapshot
                if not isinstance(data, dict) or data.get("error"):
                    if index == 0:
                        raise BrowserdError("failed", 500)
                    continue
                shift = 0
                if index:
                    elements.append({"role": "frame", "name": await self._frame_name(frame), "depth": 0})
                    shift = 1
                raw_items = data.get("elements")
                for raw in raw_items[: MAX_ELEMENTS * 2] if isinstance(raw_items, list) else []:
                    item = sane_element(raw) if isinstance(raw, dict) and raw.get("ref") else sane_structure(raw)
                    if item is None or (item.get("ref") == "" and "tag" in item):
                        continue
                    depth = raw.get("depth") if isinstance(raw, dict) else 0
                    item["depth"] = min(depth, 6) + shift if isinstance(depth, int) and depth >= 0 else shift
                    elements.append(item)
                next_ref = data.get("next_ref")
                if isinstance(next_ref, int) and 1 <= next_ref <= 90_000:
                    tab.next_ref = next_ref
                else:  # the counter ran out, or the page meddled: start again with new labels
                    tab.next_ref, tab.doc = 1, secrets.token_hex(8)
                text = _text(data.get("text"), max_chars)
                if text:
                    texts.append(text)
                truncated = truncated or data.get("truncated") is True
                login = login or data.get("login_form_detected") is True
            answer = await self._answer(
                elements=elements, text="\n\n".join(texts)[:max_chars],
                login_form_detected=login, truncated=truncated,
            )
            while len(json.dumps(answer)) > MAX_ANSWER_BYTES and answer["elements"]:
                del answer["elements"][len(answer["elements"]) * 3 // 4 :]
                answer["truncated"] = True
            return answer

    async def describe(self, ref: object = None, focused: bool = False) -> dict:
        async with self._op():
            tab = self._tab()
            if focused:
                element = await self._focused(tab)
                if element is None:
                    raise BrowserdError("no_focused_element", 404)
            else:
                element = await self._facts(await self._locate(tab, ref))
                element["ref"] = ref
            url, title = await self._where(tab)
            return {**element, "focused": element.get("focused") is True, "mode": self.mode, "url": url, "title": title}

    async def click(self, ref: object, fingerprint: object, mode: str) -> dict:
        async with self._op():
            tab = self._tab()
            locator, element = await self._checked(tab, ref, fingerprint)
            if element["disabled"]:
                raise BrowserdError("disabled", 400)
            async with self._acting(tab, mode) as act:
                try:
                    await locator.click(timeout=CLICK_TIMEOUT_MS)
                except PlaywrightTimeout as error:
                    raise BrowserdError("not_clickable", 400) from error
            return await self._answer(act)

    async def type(self, ref: object, fingerprint: object, text: str, *, clear: bool, submit: bool, mode: str) -> dict:
        async with self._op():
            tab = self._tab()
            locator, element = await self._checked(tab, ref, fingerprint)
            if element["sensitive"]:
                raise BrowserdError("sensitive_field", 403)  # checked here as well as in core
            typeable = (
                element["tag"] == "textarea"
                or (element["tag"] == "input" and element["type"] in TEXT_TYPES)
                or element["contenteditable"]
                or element["role"] in {"textbox", "searchbox", "combobox"}
            )
            if not typeable:
                raise BrowserdError("not_typeable", 400)
            if element["disabled"]:
                raise BrowserdError("disabled", 400)
            async with self._acting(tab, mode) as act:
                try:
                    if clear:
                        await locator.fill(text, timeout=CLICK_TIMEOUT_MS)
                    else:
                        await locator.press("End", timeout=CLICK_TIMEOUT_MS)
                        await tab.page.keyboard.insert_text(text)
                    if submit:
                        await locator.press("Enter", timeout=CLICK_TIMEOUT_MS)
                except PlaywrightTimeout as error:
                    raise BrowserdError("not_clickable", 400) from error
                except PlaywrightError as error:
                    if "closed" in str(error).lower():
                        raise
                    raise BrowserdError("not_typeable", 400) from error
            return await self._answer(act)

    async def press(self, key: object, mode: str, fingerprint: object = None) -> dict:
        name = guards.KEYS.get(key) if isinstance(key, str) else None
        if name is None:
            raise BrowserdError("bad_key", 400)
        if key in guards.APPROVED_ONLY_KEYS and mode != "approved":
            raise BrowserdError("bad_key", 403)
        async with self._op():
            tab = self._tab()
            if fingerprint:
                element = await self._focused(tab)
                if element is None or not isinstance(fingerprint, str) or not secrets.compare_digest(
                    element["fingerprint"], fingerprint,
                ):
                    raise BrowserdError("element_changed", 409)
            async with self._acting(tab, mode) as act:
                await tab.page.keyboard.press(name)
            return await self._answer(act)

    async def select(self, ref: object, fingerprint: object, values: list[str], mode: str) -> dict:
        async with self._op():
            tab = self._tab()
            locator, element = await self._checked(tab, ref, fingerprint)
            if element["tag"] != "select":
                raise BrowserdError("not_a_select", 400)
            if element["disabled"]:
                raise BrowserdError("disabled", 400)
            async with self._acting(tab, mode) as act:
                try:
                    await locator.select_option(values, timeout=5000)
                except PlaywrightTimeout as error:
                    raise BrowserdError("no_such_option", 400) from error
            return await self._answer(act)

    async def scroll(self, direction: str, pages: int) -> dict:
        async with self._op():
            tab = self._tab()
            height = self.s.viewport[1]
            distance = int(height * 0.85) * pages * (-1 if direction == "up" else 1)
            await self._eval(tab.page.evaluate(SCROLL_JS, distance))
            return await self._answer()

    async def history(self, forward: bool) -> dict:
        async with self._op():
            tab = self._tab()
            async with self._acting(tab, "safe") as act:
                move = tab.page.go_forward if forward else tab.page.go_back
                try:
                    await move(timeout=self.s.nav_timeout_s * 1000, wait_until="commit")
                except PlaywrightTimeout as error:
                    raise BrowserdError("timeout", 504) from error
                except PlaywrightError as error:
                    if "closed" in str(error).lower():
                        raise
                    # A step that was refused or failed: the answer says where the tab is now.
            return await self._answer(act)

    async def activate_tab(self, tab_id: str) -> dict:
        async with self._op():
            tab = self._tabs.get(tab_id)
            if tab is None:
                raise BrowserdError("no_such_tab", 404)
            self._active = tab.id
            await tab.page.bring_to_front()
            return await self._answer()

    async def close_tab(self, tab_id: str) -> dict:
        async with self._op():
            tab = self._tabs.get(tab_id)
            if tab is None:
                raise BrowserdError("no_such_tab", 404)
            if len(self._tabs) == 1:
                await self._blank_tab()
            await tab.page.close()  # without asking the page: its "unsaved changes" box can't stop this
            self._on_tab_closed(tab)
            return await self._answer()

    async def screenshot(self, full_page: bool) -> bytes:
        async with self._op():
            page = self._tab().page
            options = {"type": "png", "timeout": self.s.action_timeout_s * 1000, "animations": "disabled"}
            data = b""
            if full_page:
                height = await self._eval(page.evaluate(HEIGHT_JS))
                if isinstance(height, (int, float)) and height <= FULL_PAGE_MAX_HEIGHT:
                    data = await page.screenshot(full_page=True, **options)
                else:
                    clip = {"x": 0, "y": 0, "width": self.s.viewport[0], "height": FULL_PAGE_MAX_HEIGHT}
                    data = await page.screenshot(full_page=True, clip=clip, **options)
            if not data or len(data) > MAX_SCREENSHOT_BYTES:
                data = await page.screenshot(**options)
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise BrowserdError("too_large", 502)
            return data

    async def upload(self, ref: object, fingerprint: object, name: object, sha256: object) -> dict:
        data = await asyncio.to_thread(read_upload, self.s.uploads_dir, name)
        # Core read and hashed the file Roland approved. The uploads folder is shared with the
        # sandbox, so the bytes are checked again here and then handed over from memory.
        if not isinstance(sha256, str) or not secrets.compare_digest(hashlib.sha256(data).hexdigest(), sha256.lower()):
            raise BrowserdError("file_changed", 409)
        async with self._op():
            tab = self._tab()
            locator, element = await self._checked(tab, ref, fingerprint)
            if element["tag"] != "input" or element["type"] != "file":
                raise BrowserdError("not_a_file_input", 400)
            async with self._acting(tab, "approved") as act:  # uploads only ever run approved
                kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
                await locator.set_input_files(
                    {"name": name, "mimeType": kind, "buffer": data}, timeout=CLICK_TIMEOUT_MS,
                )
            return await self._answer(act)

    async def downloads(self) -> list[dict]:
        def listing() -> list[dict]:
            found = []
            try:
                entries = sorted(self.s.downloads_dir.iterdir(), key=lambda path: path.name)
            except OSError:
                return found
            for path in entries:
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and not path.name.startswith("."):
                    found.append({"name": guards.clean(path.name), "size": info.st_size, "finished": True, "at": info.st_mtime})
            found.sort(key=lambda item: item.pop("at"), reverse=True)
            return found[:100]

        done = await asyncio.to_thread(listing)
        names = {item["name"] for item in done}
        pending = [
            {"name": entry["name"], "size": 0, "finished": False}
            for entry in self._incoming.values()
            if not entry.get("failed") and entry["name"] not in names
        ]
        return pending + done
