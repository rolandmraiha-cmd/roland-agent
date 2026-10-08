"""Typed v2 settings, read from environment variables or a .env file."""

from __future__ import annotations

import ipaddress
import logging
import math
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import load_dotenv

from .models.endpoint_guard import DEFAULT_MODEL_HOSTS, ModelEndpointRefused, validate_endpoint

MIN_PASSWORD_LENGTH = 16
log = logging.getLogger("agent.config")


def _explicit_networks(
    entries: tuple[str, ...], name: str,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Validate explicit peer networks without allowing trust of every address."""
    if "*" in entries:
        label = "proxy IP" if name == "FORWARDED_ALLOW_IPS" else "allowed peer IP"
        raise ValueError(f"{name}='*' is not allowed; list the {label}")
    networks = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise ValueError(
                f"Invalid {name} entry {entry!r}; use an IP address or CIDR",
            ) from error
        if network.prefixlen == 0:
            label = "proxy IPs" if name == "FORWARDED_ALLOW_IPS" else "peer IPs"
            raise ValueError(
                f"{name} entry {entry!r} has prefix length 0; "
                f"list explicit {label} or narrower CIDRs",
            )
        networks.append(network)
    return networks


def trusted_proxy_networks(entries: tuple[str, ...]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return _explicit_networks(entries, "FORWARDED_ALLOW_IPS")


def core_peer_networks(entries: tuple[str, ...]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return _explicit_networks(entries, "CORE_ALLOWED_PEERS")


def validate_agent_host(host: str) -> None:
    """AGENT_HOST is a served DNS hostname, never a URL or CSP fragment."""
    if not host:
        return
    labels = host.split(".")
    if len(host) > 253 or any(
        not 1 <= len(label) <= 63
        or not label[0].isascii()
        or not label[0].isalnum()
        or not label[-1].isascii()
        or not label[-1].isalnum()
        or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
        for label in labels
    ):
        raise ValueError("AGENT_HOST must be a DNS hostname or IPv4 address without a scheme, port or path")


def _secret(name: str, default: str = "") -> str:
    """Prefer mounted files; never include secret values or paths in errors/logs."""
    filename = os.getenv(f"{name}_FILE")
    if filename:
        if name in os.environ:
            log.warning("%s and %s_FILE are both set; using the file", name, name)
        try:
            return Path(filename).read_text(encoding="utf-8").rstrip()
        except (OSError, UnicodeError) as error:
            raise SystemExit(f"Cannot read {name}_FILE; check the file and its permissions") from error
    return os.getenv(name, default)


def _bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected a boolean")


def _vnc_usable(value: str) -> bool:
    """A password x11vnc can read from one line of its password file."""
    return (
        0 < len(value) <= 64
        and value.isascii()
        and value.isprintable()
        and " " not in value
        and not value.startswith("#")
        and "__" not in value
    )


@dataclass(frozen=True)
class Config:
    # All defaults are code defaults from v2-spec §11.1, not deployment settings.
    agent_env: str = "development"
    agent_domain: str = ""
    agent_fallback_host: str = "37-60-226-214.sslip.io"
    agent_host: str = ""
    acme_email: str = ""
    caddy_tls: str = "acme"
    model_provider: str = "llamacpp"
    model_base_url: str = "http://10.77.6.60:8080"
    model_allowed_hosts: tuple[str, ...] = DEFAULT_MODEL_HOSTS
    model_name: str = "current"
    model_server_token: str = field(default="", repr=False, metadata={"secret": True})
    model_tool_mode: str = "grammar"
    model_ctx: int = 4096
    model_max_new_tokens: int = 768
    model_temperature: float = 0.2
    model_timeout_s: float = 600.0
    model_max_concurrency: int = 3
    model_parse_retries: int = 2
    model_history_messages: int = 12
    model_tool_output_chars: int = 3000
    model_system_prompt_budget: int = 1500
    model_threads: int = 3
    model_mem_limit: str = "3840m"
    model_cpus: float = 3.0
    model_vision: bool = False
    model_keep_versions: int = 3
    model_import_max_mb: int = 6144
    training_capture: bool = False
    training_loop_enabled: bool = False
    training_schedule: str = "0 3 * * 0"
    training_launch_mode: str = "manual"
    training_ssh_target: str = ""
    training_ssh_known_hosts: str = ""
    training_provider: str = ""
    training_max_hours: int = 4
    training_min_new_sft: int = 50
    training_min_new_pairs: int = 20
    training_seed_ratio: float = 0.3
    training_keep_emails: tuple[str, ...] = ()
    trainer_url: str = ""
    training_data_dir: Path | None = None
    trainer_api_token: str = field(default="", repr=False, metadata={"secret": True})
    password_hash: str = field(
        default="", repr=False, metadata={"env": "AGENT_PASSWORD_HASH", "secret": True}
    )
    cookie_secure: bool = True
    session_days: int = 14
    idle_hours: float = field(default=72.0, metadata={"env": "SESSION_IDLE_HOURS"})
    daily_call_limit: int = 300
    max_tool_steps: int = 8
    agent_name: str = "Agent"
    timezone: str = "Europe/Helsinki"
    allowed_hosts: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = field(
        default=("127.0.0.1", "::1"), metadata={"env": "FORWARDED_ALLOW_IPS"}
    )
    core_allowed_peers: tuple[str, ...] = ()
    host: str = "0.0.0.0"  # noqa: S104 -- binding is an explicit deployment setting
    port: int = 8080
    data_dir: Path = Path("./data")
    workspace_dir: Path | None = None
    workspace_host_dir: Path | None = None
    workspace_quota_mb: int = 8192
    workspace_reserve_mb: int = 256
    workspace_max_files: int = 50000
    upload_max_mb: int = 100
    trash_keep_days: int = 7
    allow_shell: bool = False
    shell_backend: str = "local"
    shell_approval: str = "tainted"
    shell_timeout_default: int = 60
    shell_timeout_max: int = 300
    sandbox_url: str = "http://10.77.3.20:7000"
    sandbox_api_token: str = field(default="", repr=False, metadata={"secret": True})
    sandbox_max_output_bytes: int = 65536
    sandbox_max_concurrent: int = 2
    sandbox_timeout_max: int = 300
    browser_enabled: bool = False
    browser_url: str = "http://10.77.4.40:7100"
    browser_api_token: str = field(default="", repr=False, metadata={"secret": True})
    browser_max_tabs: int = 2
    browser_action_timeout_s: int = 30
    browser_nav_timeout_s: int = 45
    browser_viewport: str = "1280x800"
    browser_chromium_sandbox: bool = False
    browser_allow_private_hosts: tuple[str, ...] = ()
    screen_enabled: bool = False
    vnc_password: str = field(default="", repr=False, metadata={"secret": True})
    vnc_view_password: str = field(default="", repr=False, metadata={"secret": True})
    screen_session_idle_min: int = 30
    approval_timeout_min: int = 15
    job_approval_timeout_min: int = 120
    max_pending_approvals: int = 10
    signin_timeout_min: int = 30
    backup_dir: Path | None = None
    backup_time: str = "03:30"
    backup_keep_daily: int = 14
    backup_keep_weekly: int = 8
    backup_workspace: bool = True
    backup_workspace_keep: int = 3
    backup_workspace_max_mb: int = 2048
    audit_detail_max_bytes: int = 8192
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not self.allowed_hosts and self.agent_host:
            object.__setattr__(self, "allowed_hosts", (self.agent_host,))
        if self.agent_env == "production" and not self.core_allowed_peers:
            object.__setattr__(self, "core_allowed_peers", ("10.77.1.2",))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def workspace(self) -> Path:
        return self.workspace_dir if self.workspace_dir is not None else self.data_dir / "workspace"

    def check_model(self) -> None:
        """Model restrictions also apply to terminal chat and background jobs."""
        if self.model_provider not in {"llamacpp", "ollama"}:
            raise SystemExit("MODEL_PROVIDER must be llamacpp or ollama")
        if (
            self.agent_env == "production"
            and self.model_provider == "llamacpp"
            and not self.model_server_token
        ):
            raise SystemExit("MODEL_SERVER_TOKEN is required for llamacpp in production")
        try:
            validate_endpoint(self.model_base_url, self.model_allowed_hosts)
        except ModelEndpointRefused as error:
            raise SystemExit(str(error)) from error

    def check(self) -> None:
        """Refuse unsafe server settings before opening the DB or model client."""
        try:
            trusted_proxy_networks(self.trusted_proxies)
            core_peer_networks(self.core_allowed_peers)
            validate_agent_host(self.agent_host)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        if not self.password_hash.startswith("$argon2"):
            raise SystemExit(
                "AGENT_PASSWORD_HASH is missing. Run `python -m agent hash-password` "
                "and set AGENT_PASSWORD_HASH or AGENT_PASSWORD_HASH_FILE."
            )
        if self.agent_env not in {"development", "production"}:
            raise SystemExit("AGENT_ENV must be development or production")
        if self.shell_backend not in {"local", "sandbox"}:
            raise SystemExit("SHELL_BACKEND must be local or sandbox")
        if self.agent_env == "production":
            if not self.cookie_secure:
                raise SystemExit("COOKIE_SECURE must be true in production")
            if not self.allowed_hosts:
                raise SystemExit("Set ALLOWED_HOSTS or AGENT_HOST in production")
            if self.allow_shell and self.shell_backend == "local":
                raise SystemExit("ALLOW_SHELL with SHELL_BACKEND=local is refused in production")
            if self.browser_allow_private_hosts:
                raise SystemExit("BROWSER_ALLOW_PRIVATE_HOSTS is for tests only; leave it empty in production")
        if self.shell_backend == "sandbox" and not self.sandbox_api_token:
            raise SystemExit("SANDBOX_API_TOKEN is required for SHELL_BACKEND=sandbox")
        if self.browser_enabled and not self.browser_api_token:
            raise SystemExit("BROWSER_API_TOKEN is required when BROWSER_ENABLED=true")
        if self.screen_enabled:
            if not self.vnc_password or not self.vnc_view_password:
                raise SystemExit("VNC_PASSWORD and VNC_VIEW_PASSWORD are required when SCREEN_ENABLED=true")
            # VNC looks at the first eight characters only: alike there, and the view-only
            # password would open the screen for typing too.
            if self.vnc_password[:8] == self.vnc_view_password[:8]:
                raise SystemExit("VNC_PASSWORD and VNC_VIEW_PASSWORD must differ in their first 8 characters")
            if not (_vnc_usable(self.vnc_password) and _vnc_usable(self.vnc_view_password)):
                # The same rule as browserd/vnc.py, which writes them into x11vnc's password file.
                raise SystemExit(
                    "VNC_PASSWORD and VNC_VIEW_PASSWORD must be plain letters, digits or punctuation, without "
                    "spaces, a leading # or a double underscore (make secrets creates suitable ones)"
                )
            if not self.browser_enabled:
                raise SystemExit("SCREEN_ENABLED=true needs BROWSER_ENABLED=true: the screen shows the agent's browser")
        self.check_model()

        from .schedule import valid_cron

        if not .3 <= self.training_seed_ratio < 1:
            raise SystemExit("TRAINING_SEED_RATIO must be at least 0.3 and below 1")
        if self.training_launch_mode not in {"manual", "ssh", "hook"}:
            raise SystemExit("TRAINING_LAUNCH_MODE must be manual, ssh or hook")
        if not 1 <= self.training_max_hours <= 24 or not valid_cron(self.training_schedule):
            raise SystemExit("Invalid training time cap or schedule")
        if self.trainer_url:
            from .training.client import TrainerClient
            try:
                TrainerClient(self)
            except ValueError as error:
                raise SystemExit(str(error)) from error
            if len(self.trainer_api_token) < 32:
                raise SystemExit("TRAINER_API_TOKEN is required when TRAINER_URL is set")

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv()
        if "MODEL_API_KEY" in os.environ:
            log.warning("MODEL_API_KEY is removed and ignored; use MODEL_SERVER_TOKEN for the local server")
        defaults = cls()
        values = {}
        path_fields = {"data_dir", "workspace_dir", "workspace_host_dir", "backup_dir", "training_data_dir"}
        for setting in fields(cls):
            name = setting.metadata.get("env", setting.name.upper())
            default = getattr(defaults, setting.name)
            if setting.metadata.get("secret", False):
                values[setting.name] = _secret(name)
                continue
            value = os.getenv(name)
            if value is None or (not value.strip() and not isinstance(default, str)):
                continue
            try:
                if setting.name in path_fields:
                    values[setting.name] = Path(value).resolve() if value.strip() else default
                elif isinstance(default, bool):
                    values[setting.name] = _bool(value)
                elif isinstance(default, int):
                    values[setting.name] = int(value)
                elif isinstance(default, float):
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("expected a finite number")
                    values[setting.name] = number
                elif isinstance(default, tuple):
                    values[setting.name] = tuple(item.strip() for item in value.split(",") if item.strip())
                else:
                    values[setting.name] = value.strip()
            except ValueError as error:
                raise SystemExit(f"Invalid {name}; check its setting type") from error
        values["password_hash"] = values["password_hash"].strip().strip("'\"")
        values["data_dir"] = Path(values.get("data_dir", defaults.data_dir)).resolve()
        if "model_base_url" not in values and values.get("model_provider") == "ollama":
            values["model_base_url"] = "http://10.77.6.60:11434"
        return cls(**values)
