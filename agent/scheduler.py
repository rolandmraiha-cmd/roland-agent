"""Runs scheduled background jobs while nobody is chatting."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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
    backup_task = asyncio.create_task(backup_loop(agent)) if agent.config.backup_dir is not None else None
    try:
        while True:
            try:
                await run_due_jobs(agent)
            except Exception:
                log.exception("scheduler error")
            await asyncio.sleep(every)
    finally:
        if backup_task:
            backup_task.cancel()
            await asyncio.gather(backup_task, return_exceptions=True)


async def run_backup_if_due(agent: Agent, now: float | None = None) -> bool:
    """Run once per local day, retry failures hourly, and catch up after a restart."""
    from .backup import backup_now, validate_backup_config

    if agent.config.backup_dir is None:
        return False
    validate_backup_config(agent.config)
    now = time.time() if now is None else now
    zone = ZoneInfo(agent.config.timezone)
    local = datetime.fromtimestamp(now, zone)
    hour, minute = map(int, agent.config.backup_time.split(":"))
    if (local.hour, local.minute) < (hour, minute):
        return False
    success = agent.memory.get_meta("last_backup_ok")
    if success and datetime.fromtimestamp(float(success), zone).date() >= local.date():
        return False
    attempt = agent.memory.get_meta("last_backup_attempt")
    if attempt and now - float(attempt) < 3600:
        return False
    agent.memory.set_meta("last_backup_attempt", str(now))
    task = asyncio.create_task(asyncio.to_thread(backup_now, agent.config, agent.memory, agent.audit, now=now))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The thread cannot be cancelled. Keep Memory and the server's restore lock alive.
        await asyncio.gather(task, return_exceptions=True)
        raise
    return True


async def backup_loop(agent: Agent, every: float = 20.0) -> None:
    """Separate from model jobs so a long model reply cannot delay the nightly backup."""
    while True:
        try:
            await run_backup_if_due(agent)
        except Exception as error:
            log.error("scheduled backup failed: %s", type(error).__name__)
        await asyncio.sleep(every)
