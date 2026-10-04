"""Run browser-state regressions with Node's built-in runner; no npm dependencies."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_chat_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for frontend state regression tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).parent / "frontend" / "chat.test.cjs")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
