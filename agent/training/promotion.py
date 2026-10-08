"""Recoverable human promotions; health checks never authorize a new promotion."""

import asyncio

from ..models.cli import check_health
from .client import TrainerClient
from .tokens import mint


async def finish_promotion(agent, identifier: str) -> bool | None:
    active = getattr(agent, "_promotion_checks", None)
    if active is None:
        active = agent._promotion_checks = set()
    if identifier in active:
        return None
    rows = agent.memory._all(
        "SELECT * FROM model_promotions WHERE id=? AND status='promoting'", (identifier,)
    )
    if not rows:
        return None
    row = dict(rows[0])
    active.add(identifier)
    client = TrainerClient(agent.config)

    async def compensate():
        state = await client.request("GET", "/v1/registry")
        if state["current"] == row["version_id"]:
            token = mint(
                agent.config.trainer_api_token, "rollback", row["from_version_id"], row["version_id"]
            )
            await client.request(
                "POST",
                "/v1/rollback",
                body={
                    "version_id": row["from_version_id"],
                    "from_version_id": row["version_id"],
                    "request_token": token,
                },
            )
            agent.memory._exec(
                "UPDATE model_promotions SET status='rolled_back_auto' WHERE id=?", (identifier,)
            )
            agent.memory._exec(
                "UPDATE training_runs SET status='failed',error='Post-promotion smoke failed' WHERE id=?",
                (row["run_id"],),
            )
            agent.audit.write("system", "model_rollback_auto", detail={"version_id": row["version_id"]})
        elif state["current"] == row["from_version_id"]:
            agent.memory._exec(
                "UPDATE model_promotions SET status='pending',confirm_started=NULL WHERE id=?", (identifier,)
            )
        else:
            raise ValueError("Another model switch occurred; review the registry before retrying")

    try:
        healthy = await check_health(
            row["version_id"], agent.config.model_base_url, agent.config.model_server_token
        )
        if not healthy:
            raise ValueError("Post-promotion smoke failed")
        agent.memory._exec("UPDATE model_promotions SET status='promoted' WHERE id=?", (identifier,))
        agent.memory._exec("UPDATE training_runs SET status='promoted' WHERE id=?", (row["run_id"],))
        agent.audit.write("roland", "model_promoted", detail={"version_id": row["version_id"]})
        return True
    except BaseException as error:
        task = asyncio.create_task(compensate())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        if isinstance(error, asyncio.CancelledError):
            raise
        return False
    finally:
        active.discard(identifier)
