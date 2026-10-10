"""Run the JavaScript regressions with Node's built-in runner; no npm dependencies."""
import shutil
import subprocess
from pathlib import Path

import pytest


def run_node_tests(name: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the JavaScript regression tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).parent / "frontend" / name)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_frontend():
    run_node_tests("chat.test.cjs")


def test_browser_page_script():
    """browserd/snapshot.js: secret fields, form facts, and nothing but its three jobs."""
    run_node_tests("snapshot.test.cjs")


def test_screen_page_script():
    """agent/web/static/screen.js: the session, the password's short life, keys and hand-back."""
    run_node_tests("screen.test.cjs")
