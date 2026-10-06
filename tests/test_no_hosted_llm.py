"""Refuse hosted LLM SDKs, endpoints and legacy client identifiers (§13.6)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from agent.config import Config
from agent.models import factory
from tests.conftest import HASH

ROOT = Path(__file__).resolve().parents[1]
EXEMPT = {
    "docs/v2-spec.md",
    "tests/test_no_hosted_llm.py",
}

HOSTED_HOSTS = [
    "api.openai.com",
    "api.x.ai",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
    "api.mistral.ai",
    "api.groq.com",
    "openrouter.ai",
    "api.together.xyz",
    "api.deepseek.com",
    "api.cohere.",
    "api.fireworks.ai",
    "api-inference.huggingface.co",
    "router.huggingface.co",
    "inference.huggingface.co",
    "ollama.com/api",
    "openai.azure.com",
    "bedrock-runtime",
]

FORBIDDEN_IDS = ("OpenAICompatibleBrain", "MODEL_API_KEY_FILE", "secrets/model_api_key")
FORBIDDEN_PACKAGES = (
    "openai",
    "anthropic",
    "google-generativeai",
    "google-genai",
    "mistralai",
    "groq",
    "cohere",
    "together",
    "litellm",
)


def tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    return [line for line in out.splitlines() if line and line not in EXEMPT]


def test_no_hosted_hosts_or_identifiers_in_tracked_files():
    failures = []
    for rel in tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lower = text.lower()
        for host in HOSTED_HOSTS:
            if host.lower() in lower:
                failures.append(f"{rel}: hosted host {host}")
        for ident in FORBIDDEN_IDS:
            if ident in text:
                failures.append(f"{rel}: forbidden identifier {ident}")
    assert not failures, "\n".join(failures)


def test_no_hosted_packages_in_dependency_files():
    files = [
        "pyproject.toml",
        *sorted(p.name for p in ROOT.glob("requirements*.lock")),
        *sorted(p.name for p in ROOT.glob("requirements*.in")),
    ]
    failures = []
    for name in files:
        text = (ROOT / name).read_text(encoding="utf-8")
        for package in FORBIDDEN_PACKAGES:
            if re.search(rf"(?m)^\s*{re.escape(package)}\b", text) or f'"{package}' in text:
                failures.append(f"{name}: package {package}")
        if re.search(r"(?m)^\s*langchain", text) or '"langchain' in text:
            failures.append(f"{name}: package langchain*")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://api.openai.com/v1",
        "http://8.8.8.8:8080",
        "https://api.x.ai/v1",
        "http://openrouter.ai/api/v1",
    ],
)
def test_make_brain_refuses_public_endpoints(url, tmp_path):
    config = Config(
        model_base_url=url,
        model_name="x",
        model_server_token="t",
        password_hash=HASH,
        data_dir=tmp_path,
    )
    with pytest.raises(SystemExit):
        factory.make_brain(config)


def test_compose_model_only_on_internal_model_network():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    model = compose["services"]["model"]
    networks = model.get("networks")
    assert networks == {"model": {"ipv4_address": "10.77.6.60"}} or list(networks) == ["model"]
    assert compose["networks"]["model"].get("internal") is True
