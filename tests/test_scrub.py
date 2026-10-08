"""A8.3 checksum-specific identifier redaction and bounded adversarial patterns."""

import time

import pytest

from agent.training.scrub import Scrubber


@pytest.mark.parametrize(
    "text,marker",
    [
        ("4111 1111 1111 1111", "[CARD]"),
        ("FI2112345600000785", "[IBAN]"),
        ("131052-308T", "[HETU]"),
        ("+358401234567", "[PHONE]"),
        ("040 123 4567", "[PHONE]"),
        ("test@example.com", "[EMAIL]"),
        ("Authorization: Bearer example", "[REDACTED]"),
        ("https://example.com/?token=private", "[REDACTED]"),
        ("sk-" + "a" * 32, "[CREDENTIAL]"),
        ("a" * 10 + "." + "b" * 10 + "." + "c" * 10, "[CREDENTIAL]"),
        ("-----BEGIN RSA PRIVATE KEY-----\nexample\n-----END RSA PRIVATE KEY-----", "[PRIVATE_KEY]"),
    ],
)
def test_rules(text, marker):
    clean, counts = Scrubber().scrub(text)
    assert marker in clean and counts


@pytest.mark.parametrize(
    "text", ["4111111111111112", "FI2212345600000785", "131052-308U", "hello", "article-123"]
)
def test_negative_checks_not_redacted(text):
    assert Scrubber().scrub(text)[0] == text


def test_password_keys_and_email_allowlist():
    clean, counts = Scrubber(keep_emails=["keep@example.com"]).scrub(
        {"password": "typed", "salasana": "other", "text": "keep@example.com"}
    )
    assert clean == {"password": "[PASSWORD]", "salasana": "[PASSWORD]", "text": "keep@example.com"}
    assert counts["password"] == 2


def test_loaded_secret_values_removed():
    secret = "synthetic confidential multiline\nvalue"
    scrubber = Scrubber([secret])
    clean, _ = scrubber.scrub({"messages": [{"content": secret}]})
    scrubber.check(clean)
    assert clean["messages"][0]["content"] == "[SECRET]"


def test_self_check_aborts_export(monkeypatch):
    scrubber = Scrubber(["synthetic confidential value"])
    monkeypatch.setattr(scrubber, "text", lambda text, counts: text)
    clean, _ = scrubber.scrub("synthetic confidential value")
    with pytest.raises(ValueError, match="survived"):
        scrubber.check(clean)


@pytest.mark.parametrize(
    "character", ["x", ".", "-", " ", "0"], ids=["letters", "dots", "dashes", "spaces", "digits"]
)
def test_scrub_patterns_linear(character):
    text = character * 300000
    start = time.perf_counter()
    Scrubber().scrub(text)
    assert time.perf_counter() - start < 0.5
