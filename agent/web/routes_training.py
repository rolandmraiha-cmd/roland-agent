"""CSRF-protected human persona, capture, export and model decisions."""

from __future__ import annotations

import json
import time
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..persona import OverBudget
from ..training.capture import validate_action
from ..training.client import TrainerClient
from ..training.dataset import Datasets, NotEnoughData
from ..training.files import encode
from ..training.tokens import mint


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonaBody(Body):
    agent_name: str = Field(min_length=1, max_length=60)
    persona: str = Field(max_length=2000)
    instructions: str = Field(max_length=4000)
    confirm_over_budget: StrictBool = False


class FeedbackBody(Body):
    rating: StrictInt
    correction: str | None = Field(default=None, max_length=8000)
    correction_action: dict | None = None


class TrainingBody(Body):
    capture: StrictBool
    loop_enabled: StrictBool


class ChatTrainingBody(Body):
    mode: Literal["follow", "on", "never"]


class ExampleBody(Body):
    include: StrictBool | None = None
    target_json: dict | None = None


class DatasetBody(Body):
    include_past: StrictBool = False
    confirmed_count: StrictInt | None = None


class RunBody(Body):
    dataset_id: str = Field(min_length=1, max_length=64)


class PromoteBody(Body):
    version_id: str
    sha256: str = Field(min_length=64, max_length=64)
    confirm: StrictBool = False


class RollbackBody(Body):
    to_version_id: str
    confirm: StrictBool = False


class PruneBody(Body):
    confirmed_ids: list[str] = Field(min_length=1, max_length=100)
    confirm: StrictBool = False


def build_router(agent) -> APIRouter:
    router, memory, datasets = APIRouter(), agent.memory, Datasets(agent)

    async def trainer(method, path, body=None, content=None):
        if not agent.config.trainer_url:
            raise HTTPException(
                503, "Trainer is off. Use make model-import, model-promote or model-rollback on the host."
            )
        try:
            return await TrainerClient(agent.config).request(method, path, body=body, content=content)
        except httpx.HTTPStatusError as error:
            raise HTTPException(error.response.status_code, "Trainer refused the request") from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Trainer is unavailable") from None

    async def measured(version):
        try:
            return await agent.persona.preview(agent, version)
        except (httpx.HTTPError, ValueError, RuntimeError):
            raise HTTPException(
                503, "The local model tokenizer is unavailable; no persona was changed."
            ) from None

    @router.get("/api/settings/persona")
    async def get_persona():
        version = agent.persona.active()
        return {
            "active": version,
            **(await measured(version)),
            "history_ids": [row["id"] for row in agent.persona.history()],
        }

    @router.post("/api/settings/persona/preview")
    async def preview_persona(body: PersonaBody):
        return await measured(body.model_dump())

    async def save_persona(version, confirm):
        try:
            return await agent.persona.save(agent, version, confirm=confirm)
        except OverBudget as error:
            raise HTTPException(
                409, {"message": str(error), "token_count": error.count, "confirm_over_budget": True}
            ) from None
        except (httpx.HTTPError, RuntimeError):
            raise HTTPException(503, "The local tokenizer is unavailable") from None

    @router.put("/api/settings/persona")
    async def put_persona(body: PersonaBody):
        return await save_persona(body.model_dump(), body.confirm_over_budget)

    @router.get("/api/settings/persona/versions")
    async def persona_versions():
        return agent.persona.history()

    @router.get("/api/settings/persona/versions/{identifier}")
    async def persona_version(identifier: int):
        try:
            return {**agent.persona.version(identifier), "diff": agent.persona.diff(identifier)}
        except KeyError:
            raise HTTPException(404, "No such prompt version") from None

    @router.post("/api/settings/persona/versions/{identifier}/restore")
    async def restore_persona(identifier: int, request: Request):
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        if set(body) - {"confirm_over_budget"} or not isinstance(
            body.get("confirm_over_budget", False), bool
        ):
            raise HTTPException(422, "Invalid restore request")
        try:
            return await save_persona(
                agent.persona.version(identifier), body.get("confirm_over_budget", False)
            )
        except KeyError:
            raise HTTPException(404, "No such prompt version") from None

    @router.get("/api/messages/{identifier}/feedback")
    async def get_feedback(identifier: int):
        empty = {"message_id": identifier, "rating": None, "used_in_dataset": None, "captured": False}
        return agent.capture.saved_feedback(identifier) or empty

    @router.get("/api/feedback/tools")
    async def feedback_tools():
        from ..tools import schemas

        return schemas()

    @router.post("/api/messages/{identifier}/feedback")
    async def feedback(identifier: int, body: FeedbackBody):
        try:
            return agent.capture.feedback(identifier, **body.model_dump())
        except KeyError:
            raise HTTPException(404, "No such assistant message") from None
        except LookupError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.delete("/api/messages/{identifier}/feedback")
    async def delete_feedback(identifier: int):
        try:
            with memory.transaction():
                agent.capture.check_feedback_unlocked(identifier)
                memory._exec("DELETE FROM feedback WHERE message_id=?", (identifier,))
                memory._exec(
                    "DELETE FROM training_examples WHERE message_id=? AND source IN "
                    "('thumbs_up','thumbs_down','correction')",
                    (identifier,),
                )
                agent.audit.write(
                    "roland", "feedback_given", detail={"message_id": identifier, "removed": True}
                )
        except LookupError as error:
            raise HTTPException(409, str(error)) from None
        return {"ok": True}

    @router.get("/api/settings/training")
    async def get_training():
        return {
            "capture": memory.get_meta("training_capture") == "1",
            "loop_enabled": memory.get_meta("training_loop_enabled") == "1",
            "schedule": agent.config.training_schedule,
            "mode": agent.config.training_launch_mode,
            "notice": json.loads(memory.get_meta("training_notice") or "null"),
            "past_count": datasets.past_count(),
            "blocked": agent.capture.blocked(),
        }

    @router.get("/api/jobs/training")
    async def system_training_job():
        settings = await get_training()
        return {
            **settings,
            "owner": "system",
            "name": "Prepare training candidate",
            "editable": False,
            "next_run": memory.get_meta("training_next_run"),
        }

    @router.post("/api/jobs/training/toggle")
    async def toggle_training_job():
        if memory.get_meta("training_capture") != "1":
            raise HTTPException(422, "Enable training capture in Settings first")
        memory.set_meta(
            "training_loop_enabled", "0" if memory.get_meta("training_loop_enabled") == "1" else "1"
        )
        agent.audit.write(
            "roland",
            "training_settings_changed",
            detail={"loop_enabled": memory.get_meta("training_loop_enabled") == "1"},
        )
        return await system_training_job()

    @router.put("/api/settings/training")
    async def set_training(body: TrainingBody):
        if body.loop_enabled and not body.capture:
            raise HTTPException(422, "The training loop needs capture enabled")
        with memory.transaction():
            memory.set_meta("training_capture", str(int(body.capture)))
            memory.set_meta("training_loop_enabled", str(int(body.loop_enabled)))
            agent.audit.write("roland", "training_settings_changed", detail=body.model_dump())
        return await get_training()

    @router.get("/api/chats/{identifier}/training")
    async def chat_training(identifier: int):
        rows = memory._all("SELECT training_mode FROM chats WHERE id=?", (identifier,))
        if not rows:
            raise HTTPException(404, "No such chat")
        return {"mode": rows[0][0], "capture": agent.capture.enabled(identifier)}

    @router.post("/api/chats/{identifier}/training")
    async def set_chat_training(identifier: int, body: ChatTrainingBody):
        await chat_training(identifier)
        with memory.transaction():
            memory._exec("UPDATE chats SET training_mode=? WHERE id=?", (body.mode, identifier))
            if body.mode == "never":
                memory._exec(
                    "UPDATE training_examples SET include=0 WHERE chat_id=? AND used_in_dataset IS NULL",
                    (identifier,),
                )
                agent.capture.purge_chat(identifier)
            agent.audit.write(
                "roland", "training_settings_changed", chat_id=identifier, detail={"mode": body.mode}
            )
        return await chat_training(identifier)

    @router.get("/api/training/examples")
    async def examples(
        source: str | None = None,
        included: bool | None = None,
        tainted: bool | None = None,
        since: float | None = None,
        before: float | None = None,
        limit: int = 100,
    ):
        rows = memory._all(
            "SELECT * FROM training_examples WHERE (? IS NULL OR source=?) AND (? IS NULL OR include=?) "
            "AND (? IS NULL OR tainted=?) AND (? IS NULL OR created>=?) AND (? IS NULL OR created<?) "
            "ORDER BY created DESC,id DESC LIMIT ?",
            (
                source,
                source,
                included,
                included,
                tainted,
                tainted,
                since,
                since,
                before,
                before,
                max(1, min(limit, 200)),
            ),
        )
        return [dict(row) for row in rows]

    def need_unused(identifier):
        rows = memory._all("SELECT * FROM training_examples WHERE id=?", (identifier,))
        if not rows:
            raise HTTPException(404, "No such example")
        if rows[0]["used_in_dataset"]:
            raise HTTPException(409, "This example has been used in a dataset")
        return dict(rows[0])

    @router.patch("/api/training/examples/{identifier}")
    async def edit_example(identifier: str, body: ExampleBody):
        with memory.transaction():
            need_unused(identifier)
            if body.include is not None:
                memory._exec(
                    "UPDATE training_examples SET include=? WHERE id=?", (int(body.include), identifier)
                )
                memory._exec(
                    "UPDATE preference_pairs SET include=? WHERE example_id=?",
                    (int(body.include), identifier),
                )
            if body.target_json is not None:
                try:
                    validate_action(body.target_json)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                target, _ = agent.capture.scrubber.scrub(body.target_json)
                memory._exec(
                    "UPDATE training_examples SET target_json=? WHERE id=?", (encode(target), identifier)
                )
                memory._exec(
                    "UPDATE preference_pairs SET chosen_json=? WHERE example_id=?",
                    (encode(target), identifier),
                )
        return need_unused(identifier)

    @router.delete("/api/training/examples/{identifier}")
    async def delete_example(identifier: str):
        with memory.transaction():
            need_unused(identifier)
            memory._exec("DELETE FROM training_examples WHERE id=?", (identifier,))
        return {"ok": True}

    @router.get("/api/training/past-count")
    async def past_count():
        return {
            "count": datasets.past_count(),
            "note": "Only retained, actual model context can be included. Older transcripts cannot reconstruct it.",
        }

    @router.post("/api/training/datasets")
    async def build_dataset(body: DatasetBody):
        if body.include_past and body.confirmed_count != datasets.past_count():
            raise HTTPException(409, "Review the current past-chat count before including it")
        try:
            return datasets.build(include_past=body.include_past)
        except NotEnoughData as error:
            raise HTTPException(422, {"message": str(error), "counts": error.counts}) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/api/training/datasets")
    async def list_datasets():
        return [dict(row) for row in memory._all("SELECT * FROM training_datasets ORDER BY created DESC")]

    @router.get("/api/training/datasets/{identifier}/download")
    async def download_dataset(identifier: str):
        try:
            return FileResponse(
                datasets.archive(identifier), media_type="application/gzip", filename=identifier + ".tar.gz"
            )
        except KeyError:
            raise HTTPException(404, "No such dataset") from None

    @router.get("/api/training/export")
    async def export(since: str | None = None):
        try:
            from datetime import datetime

            start = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() if since else None
            result = datasets.build(since=start)
            return await download_dataset(result["id"])
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/api/training/runs")
    async def runs():
        return [
            dict(row) for row in memory._all("SELECT * FROM training_runs ORDER BY created DESC LIMIT 100")
        ]

    @router.get("/api/training/runs/{identifier}")
    async def run(identifier: str):
        rows = memory._all("SELECT * FROM training_runs WHERE id=?", (identifier,))
        if not rows:
            raise HTTPException(404, "No such training run")
        result = dict(rows[0])
        if result["mode"] != "manual":
            result["progress"] = await trainer("GET", "/v1/runs/" + identifier)
        return result

    @router.post("/api/training/runs")
    async def start_run(body: RunBody):
        from ..training.loop import start_run

        try:
            return await start_run(agent, body.dataset_id)
        except (LookupError, ValueError) as error:
            raise HTTPException(409, str(error)) from None

    @router.post("/api/training/runs/{identifier}/cancel")
    async def cancel_run(identifier: str):
        rows = memory._all("SELECT * FROM training_runs WHERE id=?", (identifier,))
        if not rows:
            raise HTTPException(404, "No such run")
        if rows[0]["status"] in {"promoted", "discarded", "failed", "cancelled", "rejected_auto"}:
            raise HTTPException(409, "This run has finished")
        if rows[0]["mode"] != "manual":
            await trainer("POST", "/v1/runs/" + identifier + "/cancel")
        memory._exec(
            "UPDATE training_runs SET status='cancelled',finished=? WHERE id=?", (time.time(), identifier)
        )
        return {"ok": True}

    @router.get("/api/models")
    async def models():
        if agent.config.trainer_url:
            return await trainer("GET", "/v1/registry")
        from pathlib import Path

        from ..models.registry import Registry

        try:
            return Registry(Path("/models")).details()
        except (OSError, ValueError):
            return {"current": None, "previous": None, "versions": {}, "trainer_off": True}

    @router.put("/api/models/import")
    async def import_model(request: Request):
        count = 0

        async def chunks():
            nonlocal count
            async for chunk in request.stream():
                count += len(chunk)
                if count > agent.config.model_import_max_mb * 1024 * 1024:
                    raise HTTPException(413, "Candidate exceeds MODEL_IMPORT_MAX_MB")
                yield chunk

        result = await trainer("POST", "/v1/import", content=chunks())
        from ..training.loop import record_candidate

        return record_candidate(agent, result)

    @router.get("/api/models/promotions")
    async def promotions(status: str = "pending"):
        memory._exec(
            "UPDATE model_promotions SET status='expired' WHERE status='pending' AND created<?",
            (time.time() - 14 * 86400,),
        )
        return [
            dict(row)
            for row in memory._all(
                "SELECT * FROM model_promotions WHERE status=? ORDER BY created DESC", (status,)
            )
        ]

    @router.post("/api/models/promotions/{identifier}/promote")
    async def promote(identifier: str, body: PromoteBody):
        rows = memory._all("SELECT * FROM model_promotions WHERE id=? AND status='pending'", (identifier,))
        if not rows or rows[0]["created"] < time.time() - 14 * 86400:
            raise HTTPException(409, "No active promotion request")
        row = rows[0]
        state = await models()
        version = state["versions"].get(body.version_id)
        if (
            body.version_id != row["version_id"]
            or row["from_version_id"] != state["current"]
            or not version
            or version["status"] != "candidate"
            or version["sha256"] != body.sha256
        ):
            raise HTTPException(409, "Candidate or current model changed; review again")
        if not body.confirm:
            memory._exec(
                "UPDATE model_promotions SET confirm_started=? WHERE id=?", (time.time(), identifier)
            )
            return {"confirm_required": True, "version_id": body.version_id}
        if row["confirm_started"] is None or not 1 <= time.time() - row["confirm_started"] <= 300:
            raise HTTPException(409, "Start and confirm this promotion in the UI")
        request_token = mint(agent.config.trainer_api_token, "promote", body.version_id, body.sha256)
        # Claim before crossing the network so simultaneous clicks cannot launch two switches.
        with memory.transaction():
            changed = memory._exec(
                "UPDATE model_promotions SET status='promoting',decided=?,decided_by='roland' "
                "WHERE id=? AND status='pending'",
                (time.time(), identifier),
            ).rowcount
            if not changed:
                raise HTTPException(409, "Already decided")
        try:
            await trainer(
                "POST",
                "/v1/promote",
                {"version_id": body.version_id, "sha256": body.sha256, "request_token": request_token},
            )
        finally:
            # This also compensates an ambiguous network failure or cancellation after a switch.
            from ..training.promotion import finish_promotion

            healthy = await finish_promotion(agent, identifier)
        if not healthy:
            raise HTTPException(
                503, "Promotion did not pass its serving checks. Check the model registry before retrying."
            )
        return {"ok": True, "version_id": body.version_id}

    @router.post("/api/models/promotions/{identifier}/discard")
    async def discard(identifier: str):
        rows = memory._all("SELECT * FROM model_promotions WHERE id=? AND status='pending'", (identifier,))
        if not rows:
            raise HTTPException(409, "No pending promotion")
        await trainer("POST", "/v1/discard", {"version_id": rows[0]["version_id"]})
        memory._exec(
            "UPDATE model_promotions SET status='discarded',decided=?,decided_by='roland' WHERE id=?",
            (time.time(), identifier),
        )
        memory._exec("UPDATE training_runs SET status='discarded' WHERE id=?", (rows[0]["run_id"],))
        agent.audit.write("roland", "model_discarded", detail={"version_id": rows[0]["version_id"]})
        return {"ok": True}

    @router.post("/api/models/rollback")
    async def rollback(body: RollbackBody):
        state = await models()
        if body.to_version_id not in {state.get("previous")} | {
            key for key in state["versions"] if key.endswith("-base")
        }:
            raise HTTPException(409, "Choose the previous or base model")
        if not body.confirm:
            memory.set_meta(
                "rollback_confirmation",
                encode({"id": body.to_version_id, "current": state["current"], "ts": time.time()}),
            )
            return {"confirm_required": True}
        with memory.transaction():
            confirmation = json.loads(memory.get_meta("rollback_confirmation") or "{}")
            if (
                confirmation.get("id") != body.to_version_id
                or confirmation.get("current") != state["current"]
                or not 1 <= time.time() - confirmation.get("ts", 0) <= 300
            ):
                raise HTTPException(409, "Start and confirm rollback in the UI")
            memory.set_meta("rollback_confirmation", "{}")
        token = mint(agent.config.trainer_api_token, "rollback", body.to_version_id, state["current"])
        result = await trainer(
            "POST",
            "/v1/rollback",
            {"version_id": body.to_version_id, "from_version_id": state["current"], "request_token": token},
        )
        agent.audit.write("roland", "model_rollback", detail={"version_id": body.to_version_id})
        return result

    @router.post("/api/models/prune")
    async def prune_models(body: PruneBody):
        state = await models()
        if not set(body.confirmed_ids) <= set(state["versions"]):
            raise HTTPException(409, "A selected model version is no longer available")
        if not body.confirm:
            memory.set_meta(
                "prune_confirmation",
                encode({"ids": sorted(body.confirmed_ids), "current": state["current"], "ts": time.time()}),
            )
            return {"confirm_required": True}
        with memory.transaction():
            confirmation = json.loads(memory.get_meta("prune_confirmation") or "{}")
            if (
                confirmation.get("ids") != sorted(body.confirmed_ids)
                or confirmation.get("current") != state["current"]
                or not 1 <= time.time() - confirmation.get("ts", 0) <= 300
            ):
                raise HTTPException(409, "Review and confirm the selected versions first")
            memory.set_meta("prune_confirmation", "{}")
        result = await trainer("POST", "/v1/prune", {"confirmed_ids": body.confirmed_ids})
        for version in result["removed"]:
            memory._exec(
                "UPDATE model_promotions SET status='discarded' WHERE version_id=? AND status='pending'",
                (version,),
            )
        agent.audit.write("roland", "model_pruned", detail=result)
        return result

    return router
