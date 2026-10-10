"""Build step of the noVNC image: get the pinned noVNC release, check it, unpack what is used.

    python fetch.py SOURCE SHA256 DEST

SOURCE is the https address of the release tarball (or a file already on disk, for a build
without network). Nothing is unpacked unless the SHA-256 matches. Only `core/` and `vendor/`
are kept, which is the library the screen page imports, plus the licence texts. noVNC's own
pages (vnc.html and friends, which take a password from the address bar) are left out.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

KEEP_FOLDERS = ("core", "vendor")
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024


def read_source(source: str) -> bytes:
    if source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=120) as response:  # noqa: S310 -- https only, checked above
            data = response.read(MAX_DOWNLOAD + 1)
    elif "://" in source:
        raise SystemExit("fetch: the source must be an https address or a file")
    else:
        data = Path(source).read_bytes()
    if len(data) > MAX_DOWNLOAD:
        raise SystemExit("fetch: the download is larger than expected")
    return data


def check(data: bytes, expected: str) -> None:
    found = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(found, expected.strip().lower()):
        raise SystemExit(f"fetch: SHA-256 mismatch\n  expected {expected}\n  found    {found}")


def wanted(name: str) -> PurePosixPath | None:
    """Where a member of the tarball goes under DEST, or None to leave it out.
    The first path part is the release's own folder (noVNC-1.7.0/)."""
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts) or name.startswith("/"):
        return None
    inner = parts[1:]
    if inner[0] in KEEP_FOLDERS and len(inner) > 1:
        return PurePosixPath(*inner)
    if inner in (("LICENSE.txt",), ("AUTHORS",)):
        return PurePosixPath(*inner)
    if len(inner) == 2 and inner[0] == "docs" and inner[1].startswith("LICENSE"):
        return PurePosixPath(*inner)
    return None


def unpack(data: bytes, dest: Path) -> int:
    """Write the wanted regular files, byte for byte. Links and special files are refused."""
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            target = wanted(member.name)
            if target is None or member.isdir():
                continue
            if not member.isreg() or member.size > MAX_FILE:
                raise SystemExit(f"fetch: unexpected entry in the release: {member.name}")
            source = archive.extractfile(member)
            if source is None:
                raise SystemExit(f"fetch: unreadable entry in the release: {member.name}")
            path = dest.joinpath(*target.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read())
            path.chmod(0o644)
            count += 1
    if not (dest / "core" / "rfb.js").is_file():
        raise SystemExit("fetch: core/rfb.js is not in the release")
    for folder in [dest, *(item for item in dest.rglob("*") if item.is_dir())]:
        folder.chmod(0o755)
    return count


def main(arguments: list[str]) -> int:
    if len(arguments) != 3:
        raise SystemExit("Usage: fetch.py SOURCE SHA256 DEST")
    source, expected, dest = arguments
    data = read_source(source)
    check(data, expected)
    count = unpack(data, Path(dest))
    print(f"fetch: SHA-256 matches; unpacked {count} files to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
