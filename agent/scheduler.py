"""Runs scheduled background jobs while nobody is chatting."""

from __future__ import annotations

import asyncio
import logging
import time

from .core import Agent
from .memory import Job
from .schedule import next_run_after

log = logging.getLogger("agent.scheduler")

MAX_PARALLEL_JOBS = 2
running_jobs: set[int] = set()  # ids of jobs running right now


async def run_due_jobs(agent: Agent, now: float | None = None) -> int:
    now = time.time() if now is None else now
    count = 0
    for job in agent.memory.due_jobs(now):
        if job.id in running_jobs:
            continue
        if len(running_jobs) >= MAX_PARALLEL_JOBS:
            break  # Leave due times unchanged so these jobs can run on the next poll.
        # The list was read before earlier jobs ran, so Roland may have paused, deleted or
        # edited this one since. Read it again right before running.
        job = agent.memory.job(job.id)
        if job is None or not (job.enabled and job.approved) or job.next_run > now:
            continue  # resuming a job moves next_run forward, so a pause+resume also skips
        # Move the next run forward first, so a crash or long job never runs twice in a row.
        agent.memory.set_next_run(job.id, next_run_after(job.cron, agent.config.timezone, now))
        running_jobs.add(job.id)
        await execute(agent, job)
        count += 1
    return count


async def execute(agent: Agent, job: Job) -> None:
    """Runs one job now and saves its result for the jobs panel."""
    running_jobs.add(job.id)
    failed = False
    try:
        run_id = agent.memory.start_run(job)
        log.info("running job %s %s", job.id, job.name)
        try:
            ok, output = await agent.run_job(job)
        except asyncio.CancelledError:
            try:
                agent.memory.finish_run(run_id, False, "Stopped: the job was cancelled.")
            except Exception:
                log.exception("failed to record cancellation for job %s", job.id)
            raise
        except Exception as e:
            ok, output = False, f"{type(e).__name__}: {e}"
        agent.memory.finish_run(run_id, ok, output)
    except BaseException:
        failed = True
        raise
    finally:
        running_jobs.discard(job.id)
        try:
            finish_time = time.time()
            current = agent.memory.job(job.id)
            if current is not None and current.next_run <= finish_time:
                agent.memory.set_next_run(
                    job.id, next_run_after(current.cron, agent.config.timezone, finish_time),
                )
        except Exception:
            if not failed:
                raise
            log.exception("failed to reschedule job %s during cleanup", job.id)


async def scheduler_loop(agent: Agent, every: float = 20.0) -> None:
    while True:
        try:
            await run_due_jobs(agent)
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(every)
