"""Regression coverage for M0 proxy-trust review findings."""

import os
import subprocess
import sys

import pytest

from agent.web.app import ProxyHeaders


async def unused_app(scope, receive, send):
    pass


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "10.0.0.1/0", "2001:db8::1/0"])
def test_zero_prefix_rejected_by_config_and_middleware(make_agent, entry):
    agent = make_agent(trusted_proxies=(entry,))
    with pytest.raises(SystemExit) as config_error:
        agent.config.check()
    with pytest.raises(ValueError) as middleware_error:
        ProxyHeaders(unused_app, (entry,))
    message = f"FORWARDED_ALLOW_IPS entry {entry!r} has prefix length 0; list explicit proxy IPs or narrower CIDRs"
    assert str(config_error.value) == message
    assert str(middleware_error.value) == message


@pytest.mark.parametrize("entries", [("10.0.0.*",), ("127.0.0.1", "10.0.0.*")])
def test_each_proxy_entry_is_validated_at_startup(make_agent, entries):
    agent = make_agent(trusted_proxies=entries)
    with pytest.raises(SystemExit) as error:
        agent.config.check()
    assert str(error.value) == "Invalid FORWARDED_ALLOW_IPS entry '10.0.0.*'; use an IP address or CIDR"
    with pytest.raises(ValueError, match="Invalid FORWARDED_ALLOW_IPS entry"):
        ProxyHeaders(unused_app, entries)


@pytest.mark.parametrize("entries", [("127.0.0.1", "::1"), ("10.77.1.0/24", "2001:db8::/64")])
def test_explicit_proxy_addresses_and_narrow_networks_remain_valid(make_agent, entries):
    make_agent(trusted_proxies=entries).config.check()
    middleware = ProxyHeaders(unused_app, entries)
    assert len(middleware.nets) == len(entries)


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "10.0.0.*", "127.0.0.1,10.0.0.*"])
def test_invalid_proxy_configuration_fails_real_startup(tmp_path, entry):
    settings = dict(os.environ, FORWARDED_ALLOW_IPS=entry, AGENT_PASSWORD_HASH="",
                    DATA_DIR=str(tmp_path), MODEL_BASE_URL="http://127.0.0.1:9/v1")
    result = subprocess.run(
        [sys.executable, "-m", "agent"], env=settings, check=False,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "FORWARDED_ALLOW_IPS" in result.stderr
    expected = "prefix length 0" if "/0" in entry else "Invalid FORWARDED_ALLOW_IPS entry"
    assert expected in result.stderr
    assert "AGENT_PASSWORD_HASH is missing" not in result.stderr


@pytest.mark.asyncio
async def test_empty_forwarded_hops_do_not_consume_all_trusted_warning(caplog):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    middleware = ProxyHeaders(app, ("10.77.1.0/24",))

    async def request(hops):
        await middleware({"type": "http", "client": ("10.77.1.2", 1),
                          "headers": [(b"x-forwarded-for", hops)]}, None, None)

    with caplog.at_level("WARNING", logger="agent.web"):
        await request(b",")
        await request(b" , , ")
        assert not middleware.warned_all_trusted
        assert "Every X-Forwarded-For hop is trusted" not in caplog.text
        await request(b"10.77.1.5, 10.77.1.6")
        await request(b"10.77.1.5, 10.77.1.6")
    assert seen == ["10.77.1.2"] * 4
    assert middleware.warned_all_trusted
    assert caplog.text.count("Every X-Forwarded-For hop is trusted") == 1
