"""M5.2 file tools: move fail-closed on raced destination."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.memory import Memory
from agent.tools import ToolContext
from agent.tools_files import move_file, workspace_from_ctx


def _ctx(tmp_path, **kw):
    memory = Memory(tmp_path / "agent.db")
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    base = ToolContext(memory, ws_root, "Europe/Helsinki", allow_shell=False)
    for key, value in kw.items():
        setattr(base, key, value)
    return base


@pytest.mark.asyncio
async def test_move_file_does_not_auto_overwrite_without_approval(tmp_path):
    """SAFE move + dest appears before execute → refuse, leave dest intact (B2)."""
    ctx = _ctx(tmp_path)
    ws = workspace_from_ctx(ctx)
    ws.ensure()
    ws.write_text("a.txt", "aaa")
    ws.write_text("b.txt", "bbb")
    # No pending approval → overwrite must stay False even if dest exists.
    ctx.run = SimpleNamespace(pending_approval_id=None, chat_id=None, run_id=None, tainted=False)
    out = await move_file(ctx, {"from": "a.txt", "to": "b.txt"})
    assert out.startswith("Error: destination already exists")
    assert ws.read_text("b.txt", max_chars=10) == "bbb"
    assert ws.exists("a.txt")


@pytest.mark.asyncio
async def test_move_file_overwrites_only_when_replace_approved(tmp_path):
    ctx = _ctx(tmp_path)
    ws = workspace_from_ctx(ctx)
    ws.ensure()
    ws.write_text("a.txt", "aaa")
    ws.write_text("b.txt", "bbb")
    ctx.run = SimpleNamespace(pending_approval_id="apr-test", chat_id=None, run_id=None, tainted=False)
    out = await move_file(ctx, {"from": "a.txt", "to": "b.txt"})
    assert out.startswith("Moved ")
    assert ws.read_text("b.txt", max_chars=10) == "aaa"
    assert not ws.exists("a.txt")
    trash = ws.memory.trash_entries()
    assert any(t["original_path"] == "b.txt" for t in trash)


@pytest.mark.asyncio
async def test_move_file_race_via_call_tool_safe_path(tmp_path):
    """Classifier saw no dest (SAFE); dest appears before execute → no overwrite."""
    ctx = _ctx(tmp_path)
    ws = workspace_from_ctx(ctx)
    ws.ensure()
    ws.write_text("src.txt", "from-src")
    assert not ws.exists("dst.txt")

    from agent.gate import POLICIES, Risk

    decision = await POLICIES["move_file"].classify(ctx, {"from": "src.txt", "to": "dst.txt"})
    assert decision.risk is Risk.SAFE
    ws.write_text("dst.txt", "raced-in")
    ctx.run = SimpleNamespace(pending_approval_id=None, chat_id=None, run_id=None, tainted=False)
    out = await move_file(ctx, {"from": "src.txt", "to": "dst.txt"})
    assert "already exists" in out
    assert ws.read_text("dst.txt", max_chars=20) == "raced-in"
    assert ws.read_text("src.txt", max_chars=20) == "from-src"


@pytest.mark.asyncio
async def test_move_refuses_symlink_dest_via_tool(tmp_path):
    ctx = _ctx(tmp_path)
    ws = workspace_from_ctx(ctx)
    ws.ensure()
    ws.write_text("src.txt", "data")
    (ws.root / "link.txt").symlink_to(ws.root / "src.txt")
    out = await move_file(ctx, {"from": "src.txt", "to": "link.txt"})
    assert out.startswith("Error:")
    assert "outside" in out or "symbolic" in out
    assert (ws.root / "link.txt").is_symlink()
