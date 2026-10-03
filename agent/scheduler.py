"""Runs scheduled background jobs while nobody is chatting."""

from __future__ import annotations

import asyncio
import logging
import time

from .core import Agent
from .memory import Job
from .schedule import next_run_after

log = logging.getLogger("agent.scheduler")


async def run_due_jobs(agent: Agent, now: float | None = None) -> int:
    now = time.time() if now is None else now
    count = 0
    for job in agent.memory.due_jobs(now):
        # Move the next run forward first, so a crash or long job never runs twice in a row.
        agent.memory.set_next_run(job.id, next_run_after(job.cron, agent.config.timezone, now))
        await execute(agent, job)
        count += 1
    return count


async def execute(agent: Agent, job: Job) -> None:
    """Runs one job now and saves its result for the jobs panel."""
    run_id = agent.memory.start_run(job)
    log.info("running job %s %s", job.id, job.name)
    try:
        ok, output = await agent.run_job(job)
    except Exception as e:
        ok, output = False, f"{type(e).__name__}: {e}"
    agent.memory.finish_run(run_id, ok, output)


async def scheduler_loop(agent: Agent, every: float = 20.0) -> None:
    while True:
        try:
            await run_due_jobs(agent)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(every)
