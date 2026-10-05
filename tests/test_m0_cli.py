"""Check the rejected proxy configuration through the real entry point, without inference."""

import os
import subprocess
import sys


def test_star_rejected_at_startup(tmp_path):
    settings = dict(os.environ, FORWARDED_ALLOW_IPS="*", AGENT_PASSWORD_HASH="",
                    DATA_DIR=str(tmp_path), MODEL_BASE_URL="http://127.0.0.1:9/v1")
    result = subprocess.run(
        [sys.executable, "-m", "agent"], env=settings, check=False,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert result.stderr.strip() == "FORWARDED_ALLOW_IPS='*' is not allowed; list the proxy IP"
