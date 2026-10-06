"""Read active local model metadata from /models (registry + manifest)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

ID = __import__("re").compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    model_id: str
    version_id: str
    ctx: int
    capabilities: set[str] = field(default_factory=set)
    licence: str = ""

    def public_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "version_id": self.version_id,
            "ctx": self.ctx,
            "capabilities": sorted(self.capabilities),
            "licence": self.licence,
        }


def _read_regular(path: Path, limit: int) -> str:
    flags = getattr(os, "O_NOFOLLOW", 0) | os.O_RDONLY
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid model metadata")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("model metadata too large")
        return data.decode()


def load_model_info(provider: str, models_root: Path | None = None) -> ModelInfo | None:
    """Return ModelInfo for the active version, or None when no registry is mounted."""
    root = models_root if models_root is not None else Path(os.environ.get("MODELS_DIR", "/models"))
    registry = root / "registry.json"
    current = root / "current"
    if not registry.is_file() or registry.is_symlink():
        return None
    try:
        state = json.loads(_read_regular(registry, 1024 * 1024))
        version_id = state.get("current")
        if not isinstance(version_id, str) or not ID.fullmatch(version_id):
            return None
        if current.is_symlink():
            target = (root / current.readlink()).resolve()
            if target != (root / "versions" / version_id).resolve():
                return None
        manifest_path = root / "versions" / version_id / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return None
        manifest = json.loads(_read_regular(manifest_path, 64 * 1024))
        caps = manifest.get("capabilities") or []
        if not isinstance(caps, list):
            caps = []
        return ModelInfo(
            provider=provider,
            model_id=str(manifest.get("id") or version_id),
            version_id=version_id,
            ctx=int(manifest.get("ctx") or 0),
            capabilities={str(item) for item in caps},
            licence=str(manifest.get("licence") or ""),
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
