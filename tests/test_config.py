"""M1.2 settings and startup refusal rules (no model/network calls)."""

from dataclasses import fields, replace
from pathlib import Path

import pytest
from conftest import HASH

from agent import config as settings
from agent.config import Config

SECRET_FIELDS = {
    "MODEL_SERVER_TOKEN": "model_server_token",
    "AGENT_PASSWORD_HASH": "password_hash",
    "SANDBOX_API_TOKEN": "sandbox_api_token",
    "BROWSER_API_TOKEN": "browser_api_token",
    "TRAINER_API_TOKEN": "trainer_api_token",
    "VNC_PASSWORD": "vnc_password",
    "VNC_VIEW_PASSWORD": "vnc_view_password",
}


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(settings, "load_dotenv", lambda: None)
    for setting in fields(Config):
        name = setting.metadata.get("env", setting.name.upper())
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + "_FILE", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


@pytest.mark.parametrize("name,attribute", SECRET_FIELDS.items())
def test_file_secret_wins_over_env(clean_env, tmp_path, monkeypatch, caplog, name, attribute):
    path = tmp_path / "mounted-secret"
    path.write_text("file-test-value \n\t", encoding="utf-8")
    monkeypatch.setenv(name, "env-test-value")
    monkeypatch.setenv(name + "_FILE", str(path))
    config = Config.from_env()
    assert getattr(config, attribute) == "file-test-value"
    assert "using the file" in caplog.text
    assert "file-test-value" not in caplog.text + repr(config)
    assert "env-test-value" not in caplog.text + repr(config)
    assert str(path) not in caplog.text


def test_unreadable_secret_file_does_not_fall_back(clean_env, tmp_path, monkeypatch):
    path = tmp_path / "private-filename"
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "env-test-value")
    monkeypatch.setenv("MODEL_SERVER_TOKEN_FILE", str(path))
    with pytest.raises(SystemExit, match="Cannot read MODEL_SERVER_TOKEN_FILE") as error:
        Config.from_env()
    assert str(path) not in str(error.value)
    assert "env-test-value" not in str(error.value)


def test_password_hash_quotes_and_direct_secret(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PASSWORD_HASH", "'" + HASH + "'")
    monkeypatch.setenv("MODEL_SERVER_TOKEN", "development-test-token")
    assert Config.from_env().password_hash == HASH
    assert Config.from_env().model_server_token == "development-test-token"
    path = tmp_path / "hash"
    path.write_text('"' + HASH + '"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_PASSWORD_HASH_FILE", str(path))
    assert Config.from_env().password_hash == HASH


def test_deprecated_api_key_is_ignored(clean_env, monkeypatch, caplog):
    monkeypatch.setenv("MODEL_API_KEY", "unused-test-value")
    config = Config.from_env()
    assert not hasattr(config, "model_api_key")
    assert config.model_server_token == ""
    assert "removed and ignored" in caplog.text
    assert "unused-test-value" not in caplog.text + repr(config)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"trusted_proxies": ("*",)}, "FORWARDED_ALLOW_IPS"),
        ({"trusted_proxies": ("0.0.0.0/0",)}, "prefix length 0"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"agent_host": "", "allowed_hosts": ()}, "ALLOWED_HOSTS or AGENT_HOST"),
        ({"allow_shell": True, "shell_backend": "local"}, "SHELL_BACKEND=local"),
        ({"shell_backend": "sandbox"}, "SANDBOX_API_TOKEN"),
        ({"browser_enabled": True}, "BROWSER_API_TOKEN"),
        ({"screen_enabled": True, "vnc_password": "", "vnc_view_password": "view-test"}, "are required"),
        ({"screen_enabled": True, "vnc_password": "control-test", "vnc_view_password": ""}, "are required"),
        (
            {"screen_enabled": True, "vnc_password": "same-test", "vnc_view_password": "same-test"},
            "must differ",
        ),
        ({"model_base_url": "https://127.0.0.1:8080"}, "must be an http URL"),
        ({"model_base_url": "http://8.8.8.8", "model_allowed_hosts": ("8.8.8.8",)}, "must resolve only"),
        ({"model_base_url": "http://10.0.0.2"}, "MODEL_ALLOWED_HOSTS"),
        ({"model_provider": "other"}, "MODEL_PROVIDER"),
        ({"model_server_token": ""}, "MODEL_SERVER_TOKEN"),
    ],
)
def test_production_checks(changes, message):
    base = Config(
        agent_env="production",
        password_hash=HASH,
        allowed_hosts=("agent.test",),
        model_server_token="local-test-token",
        model_base_url="http://127.0.0.1:8080",
    )
    with pytest.raises(SystemExit, match=message):
        replace(base, **changes).check()


def test_valid_production_configuration():
    Config(
        agent_env="production",
        agent_host="agent.test",
        password_hash=HASH,
        model_server_token="local-test-token",
        model_base_url="http://127.0.0.1:8080",
        shell_backend="sandbox",
        sandbox_api_token="sandbox-test-token",
        browser_enabled=True,
        browser_api_token="browser-test-token",
        screen_enabled=True,
        vnc_password="control-test",
        vnc_view_password="view-test",
    ).check()  # Settings are valid; build separately refuses unimplemented services.


def test_allowed_hosts_defaults_to_agent_host(clean_env, monkeypatch):
    monkeypatch.setenv("AGENT_HOST", "agent.test")
    assert Config.from_env().allowed_hosts == ("agent.test",)
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    assert Config.from_env().allowed_hosts == ("agent.test",)
    monkeypatch.setenv("ALLOWED_HOSTS", " custom.test, second.test ")
    assert Config.from_env().allowed_hosts == ("custom.test", "second.test")


def test_workspace_override_and_disabled_backup(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert Config.from_env().workspace == tmp_path / "data" / "workspace"
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("BACKUP_DIR", "")
    assert Config.from_env().workspace == tmp_path / "files"
    assert Config.from_env().backup_dir is None


def test_ollama_default_url(clean_env, monkeypatch):
    assert Config.from_env().model_base_url == "http://10.77.6.60:8080"
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    assert Config.from_env().model_base_url == "http://10.77.6.60:11434"


@pytest.mark.parametrize(
    "name,value",
    [
        ("COOKIE_SECURE", "tru"),
        ("PORT", "abc"),
        ("MODEL_TIMEOUT_S", "NaN"),
    ],
)
def test_invalid_typed_values_have_clear_errors(clean_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=f"Invalid {name}"):
        Config.from_env()


def test_every_setting_can_be_loaded(clean_env, tmp_path, monkeypatch):
    """Exercise every §11.1 field, not only defaults or hand-picked parser types."""
    expected = {}
    for setting in fields(Config):
        name = setting.metadata.get("env", setting.name.upper())
        default = getattr(Config(), setting.name)
        if setting.name in {"data_dir", "workspace_dir", "workspace_host_dir", "backup_dir"}:
            value = str(tmp_path / setting.name)
            parsed = Path(value)
        elif isinstance(default, bool):
            value, parsed = "true", True
        elif isinstance(default, int):
            value, parsed = "17", 17
        elif isinstance(default, float):
            value, parsed = "17.5", 17.5
        elif isinstance(default, tuple):
            value, parsed = "alpha,beta", ("alpha", "beta")
        else:
            value, parsed = "setting-test", "setting-test"
        monkeypatch.setenv(name, value)
        expected[setting.name] = parsed
    actual = Config.from_env()
    assert {setting.name: getattr(actual, setting.name) for setting in fields(Config)} == expected


@pytest.mark.parametrize("validate", [False, True])
def test_model_refusal_precedes_resource_creation(make_agent, monkeypatch, validate):
    from agent import __main__ as cli

    config = replace(make_agent().config, model_base_url="https://127.0.0.1:8080")
    monkeypatch.setattr(cli.Config, "from_env", lambda: config)

    def forbidden(*args, **kwargs):
        pytest.fail("Refused configuration must not open the DB or model client")

    monkeypatch.setattr(cli, "Memory", forbidden)
    monkeypatch.setattr(cli, "make_brain", forbidden)
    with pytest.raises(SystemExit, match="MODEL_BASE_URL"):
        cli.build(validate=validate)


@pytest.mark.parametrize(
    "changes",
    [
        {"allow_shell": True, "shell_backend": "sandbox", "sandbox_api_token": "test-token"},
        {"browser_enabled": True, "browser_api_token": "test-token"},
        {"screen_enabled": True, "vnc_password": "control-test", "vnc_view_password": "view-test"},
    ],
)
def test_unimplemented_services_fail_closed(make_agent, monkeypatch, changes):
    from agent import __main__ as cli

    config = replace(make_agent().config, **changes)
    monkeypatch.setattr(cli.Config, "from_env", lambda: config)
    with pytest.raises(SystemExit, match="not implemented yet"):
        cli.build(validate=True)


def test_shell_uses_validated_config(clean_env, monkeypatch, make_agent):
    monkeypatch.setenv("ALLOW_SHELL", "yes")
    parsed = Config.from_env()
    assert parsed.allow_shell is True
    assert make_agent(allow_shell=parsed.allow_shell).allow_shell is True
    assert make_agent(allow_shell=False).allow_shell is False


def test_model_ctx_default_is_4096():
    from agent.config import Config

    assert Config.__dataclass_fields__["model_ctx"].default == 4096
