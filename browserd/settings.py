"""browserd settings, read once from the environment. Secrets come from files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_BODY = 64 * 1024               # one request body (§8.4)
MAX_URL_CHARS = 2048
MAX_TEXT_CHARS = 5000              # one browser_type call
MAX_SNAPSHOT_CHARS = 20000
MAX_ELEMENTS = 400                 # links, buttons and fields in one snapshot
MAX_ANSWER_BYTES = 900_000         # core refuses answers over 1 MB
MAX_SCREENSHOT_BYTES = 5_000_000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
FULL_PAGE_MAX_HEIGHT = 8000        # pixels in a full-page screenshot


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit(f"{name} must be a whole number") from error
    if not low <= value <= high:
        raise SystemExit(f"{name} must be between {low} and {high}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SystemExit(f"{name} must be true or false")


def _names(name: str) -> frozenset[str]:
    return frozenset(part.strip().lower() for part in os.environ.get(name, "").split(",") if part.strip())


def _token() -> str:
    """Prefer the mounted file. Never put the value or the path in an error."""
    path = os.environ.get("BROWSER_API_TOKEN_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise SystemExit("Cannot read BROWSER_API_TOKEN_FILE; check the file and its permissions") from error
    return os.environ.get("BROWSER_API_TOKEN", "").strip()


def _viewport() -> tuple[int, int]:
    raw = os.environ.get("BROWSER_VIEWPORT", "").strip().lower() or "1280x800"
    width, _, height = raw.partition("x")
    if not (width.isdigit() and height.isdigit()):
        raise SystemExit("BROWSER_VIEWPORT must look like 1280x800")
    size = int(width), int(height)
    if not (320 <= size[0] <= 3840 and 240 <= size[1] <= 2160):
        raise SystemExit("BROWSER_VIEWPORT is out of range")
    return size


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    host: str = "10.77.4.40"
    port: int = 7100
    peers: frozenset[str] = frozenset({"10.77.4.10"})
    profile_dir: Path = Path("/profile")
    files_dir: Path = Path("/files")
    display: str = ":99"
    viewport: tuple[int, int] = (1280, 800)
    max_tabs: int = 2
    action_timeout_s: int = 30
    nav_timeout_s: int = 45
    chromium_sandbox: bool = False
    # Test-only: host names the navigation guard lets through although they are private.
    allow_private_hosts: frozenset[str] = frozenset()
    # "word" (default) or "substring" (the spec's literal rule); see snapshot.js.
    sensitive_match: str = "word"
    # Also block fetch/XHR POSTs from actions Roland hasn't approved (stricter; off by default).
    block_background_posts: bool = False
    locale: str = "en-GB"
    timezone: str = "Europe/Helsinki"
    headless: bool = False  # development and tests only; production is headed on Xvfb

    @property
    def downloads_dir(self) -> Path:
        return self.files_dir / "downloads"

    @property
    def uploads_dir(self) -> Path:
        return self.files_dir / "uploads"

    @classmethod
    def from_env(cls) -> Settings:
        token = _token()
        if not token:
            raise SystemExit("BROWSER_API_TOKEN or BROWSER_API_TOKEN_FILE is required")
        peers = _names("BROWSERD_ALLOWED_PEERS") or frozenset({"10.77.4.10"})
        if "*" in peers:
            raise SystemExit("BROWSERD_ALLOWED_PEERS='*' is not allowed; list core's address")
        match = os.environ.get("BROWSER_SENSITIVE_MATCH", "").strip().lower() or "word"
        if match not in {"word", "substring"}:
            raise SystemExit("BROWSER_SENSITIVE_MATCH must be word or substring")
        return cls(
            token=token,
            host=os.environ.get("BROWSERD_HOST", "").strip() or "10.77.4.40",
            port=_int("BROWSERD_PORT", 7100, 1, 65535),
            peers=peers,
            profile_dir=Path(os.environ.get("BROWSERD_PROFILE_DIR", "").strip() or "/profile"),
            files_dir=Path(os.environ.get("BROWSERD_FILES_DIR", "").strip() or "/files"),
            display=os.environ.get("DISPLAY", "").strip() or ":99",
            viewport=_viewport(),
            max_tabs=_int("BROWSER_MAX_TABS", 2, 1, 8),
            action_timeout_s=_int("BROWSER_ACTION_TIMEOUT_S", 30, 1, 120),
            nav_timeout_s=_int("BROWSER_NAV_TIMEOUT_S", 45, 1, 180),
            chromium_sandbox=_bool("BROWSER_CHROMIUM_SANDBOX"),
            allow_private_hosts=_names("BROWSER_ALLOW_PRIVATE_HOSTS"),
            sensitive_match=match,
            block_background_posts=_bool("BROWSER_BLOCK_BACKGROUND_POSTS"),
            timezone=os.environ.get("TZ", "").strip() or "Europe/Helsinki",
            headless=_bool("BROWSERD_HEADLESS"),
        )
