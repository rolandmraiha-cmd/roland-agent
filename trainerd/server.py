"""Private trainer API: peer AND Bearer token, bounded uploads, human switch tokens."""

from __future__ import annotations

import asyncio
import hmac
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent.models.registry import Registry
from agent.training.files import atomic_write, encode, read_json
from agent.training.scrub import Scrubber
from agent.training.tokens import consume
from trainerd.runner import launch


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^tr_[a-f0-9]{32}$")
    dataset_id: str = Field(pattern=r"^[0-9]{8}-[0-9]{4}-[a-f0-9]{8}$")
    mode: str = Field(pattern=r"^(ssh|hook)$")


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    sha256: str = ""
    from_version_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    request_token: str = Field(max_length=2048)


class DiscardBody(BaseModel):
    version_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PruneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_ids: list[str] = Field(min_length=1, max_length=100)


def create_app(config: dict, token: str) -> FastAPI:
    if len(token) < 32:
        raise ValueError("Trainer API token is required")
    root = Path(config["runs"])
    registry = Registry(Path(config["models"]), max_mb=config.get("max_mb", 6144))
    active: dict[str, asyncio.Task] = {}
    scrubber = Scrubber([token])

    @asynccontextmanager
    async def lifespan(_app):
        async def housekeeping():
            while True:
                try:
                    await asyncio.to_thread(registry.cleanup_discarded)
                except (OSError, ValueError):
                    __import__("logging").getLogger(__name__).warning("Discard cleanup could not complete")
                await asyncio.sleep(3600)

        maintenance = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            tasks = [maintenance, *active.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def auth(request, call_next):
        from fastapi.responses import JSONResponse

        if (
            not request.client
            or request.client.host != "10.77.7.10"
            or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token)
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            response = await call_next(request)
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid training request"}, status_code=409)
        except PermissionError:
            return JSONResponse({"error": "human request token required"}, status_code=403)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def health():
        return {"ok": True}

    @app.get("/v1/registry")
    async def details():
        return registry.details()

    @app.post("/v1/import")
    async def import_model(request: Request):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".upload-", dir=root)
        try:
            count = 0
            with os.fdopen(descriptor, "wb") as stream:
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > registry.max_bytes:
                        raise HTTPException(413, "Candidate archive exceeds limit")
                    stream.write(chunk)
            return await asyncio.to_thread(registry.import_candidate, Path(name))
        finally:
            Path(name).unlink(missing_ok=True)

    @app.post("/v1/promote")
    async def promote(body: SwitchBody):
        consume(token, body.request_token, "promote", body.version_id, body.sha256, root / "used-tokens")
        previous = await asyncio.to_thread(
            registry.promote, body.version_id, body.sha256, requested_by="roland"
        )
        return {"ok": True, "previous": previous, "current": body.version_id}

    @app.post("/v1/rollback")
    async def rollback(body: SwitchBody):
        consume(
            token,
            body.request_token,
            "rollback",
            body.version_id,
            body.from_version_id or "",
            root / "used-tokens",
        )
        current = await asyncio.to_thread(
            registry.rollback,
            requested_by="roland",
            to_version=body.version_id,
            expected_current=body.from_version_id,
        )
        return {"ok": True, "current": current}

    @app.post("/v1/discard")
    async def discard(body: DiscardBody):
        registry.discard(body.version_id)
        return {"ok": True}

    @app.post("/v1/prune")
    async def prune(body: PruneBody):
        return {
            "removed": await asyncio.to_thread(
                registry.prune, body.confirmed_ids, keep=config.get("keep_versions", 3)
            )
        }

    @app.post("/v1/runs")
    async def start(body: RunBody):
        if active:
            raise HTTPException(409, "A run is already active")
        directory = root / body.id
        if directory.exists():
            raise HTTPException(409, "Run already exists")
        directory.mkdir(mode=0o700, parents=True)
        run = {**body.model_dump(), "status": "running"}
        atomic_write(directory / "status.json", encode(run))

        async def worker():
            try:
                archive = await launch(config, run, directory)
                result = await asyncio.to_thread(registry.import_candidate, archive)
                run.update(status="evaluated", candidate=result)
            except asyncio.CancelledError:
                run.update(status="cancelled")
                raise
            except Exception as error:
                detail, _ = scrubber.scrub(type(error).__name__ + ": " + str(error))
                run.update(status="failed", error=str(detail)[:2000])
            finally:
                atomic_write(directory / "status.json", encode(run))
                active.pop(body.id, None)

        active[body.id] = asyncio.create_task(worker())
        return run

    @app.get("/v1/runs/{identifier}")
    async def status(identifier: str):
        if not __import__("re").fullmatch(r"tr_[a-f0-9]{32}", identifier):
            raise HTTPException(404, "No such run")
        try:
            return read_json(root / identifier / "status.json")
        except (OSError, ValueError):
            raise HTTPException(404, "No such run") from None

    @app.post("/v1/runs/{identifier}/cancel")
    async def cancel(identifier: str):
        task = active.get(identifier)
        if not task:
            raise HTTPException(409, "No active run")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return {"ok": True}

    return app


def from_env():
    known = os.getenv("TRAINING_SSH_KNOWN_HOSTS", "")
    return create_app(
        {
            "models": "/models",
            "runs": "/training-runs",
            "datasets": "/training-data/datasets",
            "source": "/app",
            "target": os.getenv("TRAINING_SSH_TARGET", ""),
            "known_hosts": Path(known).read_text() if known else "",
            "provider": os.getenv("TRAINING_PROVIDER", ""),
            "max_hours": int(os.getenv("TRAINING_MAX_HOURS", "4")),
            "max_mb": int(os.getenv("MODEL_IMPORT_MAX_MB", "6144")),
            "keep_versions": int(os.getenv("MODEL_KEEP_VERSIONS", "3")),
            "ssh_key_file": os.getenv("TRAINING_SSH_KEY_FILE", "/run/secrets/training_ssh_key"),
        },
        Path(os.environ["TRAINER_API_TOKEN_FILE"]).read_text().strip(),
    )
