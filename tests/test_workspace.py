"""A5.1 workspace normalisation, openat safety, quota and trash."""

from __future__ import annotations

import threading
import time

import pytest

from agent.memory import Memory
from agent.workspace import Workspace, WorkspaceError, normalize


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    mem = Memory(tmp_path / "m.db")
    space = Workspace(root, memory=mem, quota_mb=100, reserve_mb=1)
    space.ensure()
    return space


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("..", False),
        ("a/../../b", False),
        ("/etc/passwd", False),
        ("~", False),
        ("a\x00b", False),
        ("a\x1bb", False),
        ("a\\b", False),
        ("a" + "x" * 256, False),  # 257-byte? component of 256 bytes of 'x' = 256 > 255
        ("y" * 1025, False),
        ("%2e%2e", True),  # literal name, stays inside
        ("notes/todo.txt", True),
        (".", True),
        ("", True),
        ("a/b/c", True),
    ],
)
def test_normalize_rejects(raw, ok):
    # 256-byte component: use exactly 256 UTF-8 bytes
    if raw.startswith("a") and len(raw) > 200 and "x" in raw:
        raw = "x" * 256
    if ok:
        normalize(raw)
    else:
        with pytest.raises(WorkspaceError) as exc:
            normalize(raw)
        if raw in {"..", "a/../../b", "/etc/passwd"}:
            assert "outside" in str(exc.value)


def test_symlink_not_followed_even_inside(ws, tmp_path):
    target = ws.root / "real.txt"
    target.write_text("secret-inside")
    link = ws.root / "link.txt"
    link.symlink_to(target)
    with pytest.raises(WorkspaceError) as exc:
        ws.read_bytes("link.txt")
    assert "outside" in str(exc.value)
    # Inside-dir symlink to a file still refused.
    (ws.root / "dir").mkdir()
    (ws.root / "dir" / "x").write_text("x")
    (ws.root / "dirlink").symlink_to(ws.root / "dir")
    with pytest.raises(WorkspaceError):
        ws.read_bytes("dirlink/x")


def test_symlink_swap_race(ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE")
    victim_dir = ws.root / "race"
    victim_dir.mkdir()
    (victim_dir / "inside.txt").write_text("inside")
    stop = threading.Event()

    def swapper():
        while not stop.is_set():
            try:
                if victim_dir.is_symlink():
                    victim_dir.unlink()
                    victim_dir.mkdir()
                    (victim_dir / "inside.txt").write_text("inside")
                else:
                    # replace dir with symlink to outside parent
                    for child in victim_dir.iterdir():
                        child.unlink()
                    victim_dir.rmdir()
                    victim_dir.symlink_to(tmp_path)
            except OSError:
                pass

    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    try:
        for _ in range(1000):
            try:
                data = ws.read_bytes("race/inside.txt")
                assert b"OUTSIDE" not in data
                if data:
                    assert data == b"inside"
            except (WorkspaceError, FileNotFoundError, NotADirectoryError, OSError):
                pass
    finally:
        stop.set()
        thread.join(timeout=2)
    assert outside.read_text() == "OUTSIDE"


@pytest.mark.skip(reason="hard links on a single filesystem are outside M5 openat scope; documented")
def test_hardlink_impossible_note():
    pass


def test_quota_and_reserve_enforced(ws, monkeypatch):
    class Fake:
        f_blocks = 1000
        f_frsize = 1024 * 1024  # 1 MiB blocks → 1000 MiB total
        f_bavail = 10  # 10 MiB free

    monkeypatch.setattr(ws, "statvfs", lambda: Fake())
    # used = 990 MiB, quota 100 → full
    with pytest.raises(WorkspaceError) as exc:
        ws.check_space(1)
    assert "workspace is full" in str(exc.value)

    class Room:
        f_blocks = 100
        f_frsize = 1024 * 1024
        f_bavail = 50

    monkeypatch.setattr(ws, "statvfs", lambda: Room())
    # used=50, quota=100, reserve=1 → 1 MiB write ok; 60 MiB not (free-n < reserve? free=50, n=60)
    ws.check_space(1 * 1024 * 1024)
    with pytest.raises(WorkspaceError):
        ws.check_space(60 * 1024 * 1024)


def test_overwrite_moves_old_to_trash(ws):
    ws.write_text("notes/a.txt", "v1")
    ws.write_text("notes/a.txt", "v2")
    assert ws.read_text("notes/a.txt", max_chars=100) == "v2"
    entries = ws.memory.trash_entries()
    assert len(entries) == 1
    assert entries[0]["original_path"] == "notes/a.txt"
    trash_path = entries[0]["trash_path"]
    assert (ws.root.joinpath(*trash_path.split("/"))).read_text() == "v1"


def test_trash_purge_after_days(ws):
    ws.write_text("old.txt", "bye")
    trash_id = ws.move_to_trash("old.txt", deleted_by="roland")
    # Backdate the trash row.
    ws.memory._exec(
        "UPDATE trash SET deleted = ? WHERE id = ?",
        (time.time() - 10 * 86400, trash_id),
    )
    removed = ws.purge_trash(older_than_days=7)
    assert removed >= 1
    assert ws.memory.trash_entry(trash_id) is None
