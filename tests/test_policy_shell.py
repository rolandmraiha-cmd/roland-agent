"""A4.2: shell classifier table (≥40 commands) covering §9.4.2."""

from __future__ import annotations

import pytest

from agent.policy_shell import classify_shell

# (command, mode, tainted, expected_risk)
CASES: list[tuple[str, str, bool, str]] = [
    # SAFE: everyday read-only / build helpers
    ("uname -a", "tainted", False, "safe"),
    ("pwd", "tainted", False, "safe"),
    ("ls -la", "tainted", False, "safe"),
    ("cat notes.txt", "tainted", False, "safe"),
    ("head -n 20 report.csv", "tainted", False, "safe"),
    ("wc -l *.log", "tainted", False, "safe"),
    ("echo hello", "tainted", False, "safe"),
    ("date", "tainted", False, "safe"),
    ("python3 script.py", "tainted", False, "safe"),
    ("python3 -m pytest -q", "tainted", False, "safe"),
    ("git status", "tainted", False, "safe"),
    ("git log --oneline -5", "tainted", False, "safe"),
    ("git diff", "tainted", False, "safe"),
    ("pip install requests", "tainted", False, "safe"),
    ("npm install", "tainted", False, "safe"),
    ("jq . package.json", "tainted", False, "safe"),
    ("grep -R TODO .", "tainted", False, "safe"),
    ("stat notes.txt", "tainted", False, "safe"),
    ("sort data.csv", "tainted", False, "safe"),
    ("tee -a log.txt", "tainted", False, "safe"),  # >> not gated; tee alone is fine
    # GATED: destructive / network / interpreters with -c/-e
    ("rm -rf /", "tainted", False, "gated"),
    ("rmdir empty", "tainted", False, "gated"),
    ("unlink file", "tainted", False, "gated"),
    ("shred secret.bin", "tainted", False, "gated"),
    ("truncate -s 0 x", "tainted", False, "gated"),
    ("dd if=/dev/zero of=out bs=1M", "tainted", False, "gated"),
    ("mkfs /dev/sda", "tainted", False, "gated"),
    ("find . -name '*.py'", "tainted", False, "gated"),
    ("find . -delete", "tainted", False, "gated"),
    ("find . -exec rm {} +", "tainted", False, "gated"),
    ("mv a b", "tainted", False, "gated"),
    ("cp a b", "tainted", False, "gated"),
    ("chmod 777 x", "tainted", False, "gated"),
    ("chown root x", "tainted", False, "gated"),
    ("ln -s /etc/passwd link", "tainted", False, "gated"),
    ("git push origin main", "tainted", False, "gated"),
    ("git reset --hard", "tainted", False, "gated"),
    ("git clean -fd", "tainted", False, "gated"),
    ("git rebase main", "tainted", False, "gated"),
    ("git commit --amend", "tainted", False, "gated"),
    ("git rm tracked.txt", "tainted", False, "gated"),
    ("curl https://example.com", "tainted", False, "gated"),
    ("wget https://example.com", "tainted", False, "gated"),
    ("nc -l 1234", "tainted", False, "gated"),
    ("ncat -l 1234", "tainted", False, "gated"),
    ("socat - TCP:host:80", "tainted", False, "gated"),
    ("ssh host", "tainted", False, "gated"),
    ("scp file host:", "tainted", False, "gated"),
    ("sftp host", "tainted", False, "gated"),
    ("rsync -a a/ b/", "tainted", False, "gated"),
    ("ftp host", "tainted", False, "gated"),
    ("telnet host 80", "tainted", False, "gated"),
    ("python3 -c 'print(1)'", "tainted", False, "gated"),
    ("python -c 'print(1)'", "tainted", False, "gated"),
    ("node -e '1'", "tainted", False, "gated"),
    ("perl -e '1'", "tainted", False, "gated"),
    ("ruby -e '1'", "tainted", False, "gated"),
    ("php -r '1'", "tainted", False, "safe"),  # -r not in {-c,-e}; still listed for coverage of php without flags
    ("php -c /tmp/php.ini -r '1'", "tainted", False, "gated"),
    ("pip upload dist/*", "tainted", False, "gated"),
    ("twine upload dist/*", "tainted", False, "gated"),
    ("npm publish", "tainted", False, "gated"),
    ("sendmail user@x", "tainted", False, "gated"),
    ("mail -s hi user", "tainted", False, "gated"),
    ("mutt -s hi user", "tainted", False, "gated"),
    ("crontab -e", "tainted", False, "gated"),
    ("at now + 1 minute", "tainted", False, "gated"),
    ("kill 1", "tainted", False, "gated"),
    ("pkill sleep", "tainted", False, "gated"),
    ("killall sleep", "tainted", False, "gated"),
    ("echo hi > out.txt", "tainted", False, "gated"),
    ("sed -i 's/a/b/' file", "tainted", False, "gated"),
    # Length gate
    ("echo " + ("x" * 2001), "tainted", False, "gated"),
    # Tainted run → always GATED even for benign commands
    ("uname -a", "tainted", True, "gated"),
    ("pwd", "tainted", True, "gated"),
    ("ls", "tainted", True, "gated"),
    # always mode → every command GATED
    ("uname -a", "always", False, "gated"),
    ("pwd", "always", False, "gated"),
    ("echo hi", "always", True, "gated"),
]

assert len(CASES) >= 40, f"need ≥40 cases, got {len(CASES)}"


@pytest.mark.parametrize("command,mode,tainted,expected", CASES, ids=[
    f"{i}-{c[3]}-{c[0][:40]}" for i, c in enumerate(CASES)
])
def test_classify_shell_table(command: str, mode: str, tainted: bool, expected: str):
    risk, _reason = classify_shell(command, tainted=tainted, mode=mode)
    assert risk == expected


@pytest.mark.asyncio
async def test_classify_run_shell_uses_config_and_taint(tmp_path):
    from agent.gate import Risk
    from agent.memory import Memory
    from agent.policy_shell import classify_run_shell
    from agent.tools import ToolContext

    class Cfg:
        shell_approval = "tainted"
        shell_timeout_default = 60

    class Run:
        tainted = True

    ctx = ToolContext(Memory(tmp_path / "m.db"), tmp_path, "Europe/Helsinki", allow_shell=True)
    ctx.config = Cfg()
    ctx.run = Run()
    decision = await classify_run_shell(ctx, {"command": "uname -a"})
    assert decision.risk is Risk.GATED
    assert decision.category == "shell"
    assert decision.details and decision.details.get("internet") == "yes (sandbox)"
