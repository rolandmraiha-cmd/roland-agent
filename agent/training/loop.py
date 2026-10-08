"""System-owned schedule: prepare, launch and request review; never promote."""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

from ..schedule import next_run_after
from .client import TrainerClient
from .dataset import Datasets, NotEnoughData
from .files import encode


def record_candidate(agent, result: dict, *, run_id: str | None = None) -> dict:
    memory = agent.memory
    passing = result["status"] == "candidate" and result.get("comparison", {}).get("passed") is True
    with memory.transaction():
        if run_id:
            memory._exec(
                "UPDATE training_runs SET status=?,candidate_version_id=?,eval_summary_json=?,finished=? WHERE id=?",
                (
                    "awaiting_roland" if passing else "rejected_auto",
                    result["version_id"],
                    encode(result["comparison"]),
                    time.time(),
                    run_id,
                ),
            )
        identifier = None
        if passing:
            identifier = "mp_" + uuid4().hex
            memory._exec(
                "INSERT INTO model_promotions(id,run_id,version_id,from_version_id,status,created) "
                "VALUES (?,?,?,?,'pending',?)",
                (identifier, run_id, result["version_id"], result["from_version_id"], time.time()),
            )
        agent.audit.write(
            "system",
            "promotion_requested" if passing else "candidate_rejected_auto",
            detail={"version_id": result["version_id"], "promotion_id": identifier},
        )
    return {**result, "promotion_id": identifier}


async def start_run(agent, dataset_id: str) -> dict:
    memory, mode = agent.memory, agent.config.training_launch_mode
    if not memory._all("SELECT 1 FROM training_datasets WHERE id=?", (dataset_id,)):
        raise ValueError("No such dataset")
    identifier = "tr_" + uuid4().hex
    with memory.transaction():
        if memory._all("SELECT 1 FROM training_runs WHERE status IN ('launching','running','importing')"):
            raise LookupError("A training run is already active")
        memory._exec(
            "INSERT INTO training_runs(id,dataset_id,mode,status,created) VALUES (?,?,?,?,?)",
            (
                identifier,
                dataset_id,
                mode,
                "waiting_manual" if mode == "manual" else "launching",
                time.time(),
            ),
        )
        agent.audit.write(
            "roland",
            "training_launched",
            detail={"run_id": identifier, "mode": mode, "dataset_id": dataset_id},
        )
    if mode != "manual":
        if not agent.config.trainer_url:
            memory._exec(
                "UPDATE training_runs SET status='failed',error='Trainer is off',finished=? WHERE id=?",
                (time.time(), identifier),
            )
            raise ValueError("Trainer is off; use manual mode")
        try:
            await TrainerClient(agent.config).request(
                "POST", "/v1/runs", body={"id": identifier, "dataset_id": dataset_id, "mode": mode}
            )
            memory._exec("UPDATE training_runs SET status='running' WHERE id=?", (identifier,))
        except BaseException:
            memory._exec(
                "UPDATE training_runs SET status='failed',error='Trainer launch failed',finished=? WHERE id=?",
                (time.time(), identifier),
            )
            raise
    return {
        "id": identifier,
        "status": "waiting_manual" if mode == "manual" else "running",
        "dataset_id": dataset_id,
        "mode": mode,
    }


async def tick(agent, *, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    agent.capture.prune()
    # The scheduled path can import candidates and request review. It has no switch token.
    if agent.config.trainer_url:
        from .promotion import finish_promotion

        for row in agent.memory._all("SELECT id FROM model_promotions WHERE status='promoting'"):
            await finish_promotion(agent, row["id"])
        for row in agent.memory._all("SELECT id FROM training_runs WHERE status IN ('running','importing')"):
            progress = await TrainerClient(agent.config).request("GET", "/v1/runs/" + row["id"])
            if progress["status"] == "evaluated":
                record_candidate(agent, progress["candidate"], run_id=row["id"])
            elif progress["status"] in {"failed", "cancelled"}:
                error, _ = agent.capture.scrubber.scrub(progress.get("error", "Training failed"))
                agent.memory._exec(
                    "UPDATE training_runs SET status=?,error=?,finished=? WHERE id=?",
                    (progress["status"], str(error)[:2000], now, row["id"]),
                )
    if (
        agent.memory.get_meta("training_capture") != "1"
        or agent.memory.get_meta("training_loop_enabled") != "1"
    ):
        return None
    due = agent.memory.get_meta("training_next_run")
    if due is None:
        agent.memory.set_meta(
            "training_next_run",
            str(next_run_after(agent.config.training_schedule, agent.config.timezone, now)),
        )
        return None
    if now < float(due) or agent.capture.blocked():
        return None
    # Backup owns .backup.lock for its entire snapshot. Never overlap even across processes.
    from ..locking import LockBusy, file_lock

    if agent.config.backup_dir is None:
        return {
            "status": "waiting_backup",
            "notice": "Configure and complete a backup before scheduled training.",
        }
    try:
        with file_lock(agent.config.backup_dir / ".backup.lock"):
            if not agent.memory.get_meta("last_backup_ok"):
                return {"status": "waiting_backup"}
            with agent.memory.transaction():
                if float(agent.memory.get_meta("training_next_run") or "inf") > now:
                    return None
                agent.memory.set_meta(
                    "training_next_run",
                    str(next_run_after(agent.config.training_schedule, agent.config.timezone, now)),
                )
            try:
                manifest = Datasets(agent).build()
            except NotEnoughData as error:
                agent.audit.write(
                    "system", "training_skipped", detail={"reason": "not enough new data", **error.counts}
                )
                return {"status": "not_enough_data", "counts": error.counts}
    except LockBusy:
        return {"status": "waiting_backup"}
    return await start_run(agent, manifest["id"])


async def training_loop(agent) -> None:
    while True:
        try:
            notice = await tick(agent)
            if notice:
                agent.memory.set_meta("training_notice", encode(notice))
        except Exception as error:
            agent.audit.write("system", "training_loop_failed", detail={"error_type": type(error).__name__})
        await asyncio.sleep(30)
