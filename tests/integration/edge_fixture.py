"""Fresh, private credentials for the disposable GitHub CI edge project only."""

import os
import secrets
from pathlib import Path

from agent.web.auth import hash_password

ROOT = Path(__file__).resolve().parents[2]


def main():
    if os.getenv("GITHUB_ACTIONS") != "true":
        raise SystemExit("The edge fixture is restricted to GitHub CI")
    env = ROOT / ".env"
    secret_dir = ROOT / "secrets"
    paths = [env, secret_dir / "agent_password_hash", secret_dir / "model_server_token"]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise SystemExit("Refusing to overwrite existing configuration or credentials")
    private = ROOT / ".ci-workspace"
    private.mkdir(mode=0o700, exist_ok=False)
    workspace = private / "workspace"
    workspace.mkdir(mode=0o700)
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    password = secrets.token_urlsafe(32)
    values = {
        secret_dir / "agent_password_hash": hash_password(password),
        secret_dir / "model_server_token": secrets.token_urlsafe(32),
        private / "login-password": password,
    }
    for path, value in values.items():
        with path.open("x") as stream:
            stream.write(value)
        path.chmod(0o400)
    overrides = {
        "AGENT_DOMAIN": "localhost",
        "CADDY_TLS": "internal",
        "ACME_EMAIL": "",
        "MODEL_CTX": "512",
        "MODEL_MAX_NEW_TOKENS": "128",
        "WORKSPACE_HOST_DIR": str(workspace),
    }
    lines = (ROOT / ".env.example").read_text().splitlines()
    lines = [line for line in lines if line.partition("=")[0] not in overrides]
    lines.extend(f"{name}={value}" for name, value in overrides.items())
    with env.open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    env.chmod(0o600)
    print("Prepared disposable edge fixtures; credential values are not printed.")


if __name__ == "__main__":
    main()
