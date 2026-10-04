import asyncio

import pytest

from agent.scheduler import MAX_PARALLEL_JOBS, execute, run_due_jobs, running_jobs


@pytest.mark.asyncio
async def test_cancelled_job_records_failure_and_releases_slot(make_agent, monkeypatch):
    agent = make_agent()
    job_id = agent.memory.add_job("Long job", "* * * * *", "work", 0)
    started = asyncio.Event()

    async def run_job(job):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(agent, "run_job", run_job)
    task = asyncio.create_task(execute(agent, agent.memory.job(job_id)))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = agent.memory.runs()[0]
    assert run["finished"] is not None and run["ok"] == 0
    assert "cancelled" in run["output"]
    assert job_id not in running_jobs


@pytest.mark.asyncio
async def test_due_jobs_wait_when_all_slots_reserved(make_agent):
    agent = make_agent(["done"])
    job_id = agent.memory.add_job("Due", "* * * * *", "work", 0)
    reserved = {-i - 1 for i in range(MAX_PARALLEL_JOBS)}
    running_jobs.update(reserved)
    try:
        assert await run_due_jobs(agent, now=100) == 0
        assert agent.memory.job(job_id).next_run == 0
        assert agent.memory.runs() == []
    finally:
        running_jobs.difference_update(reserved)
    assert await run_due_jobs(agent, now=100) == 1
    assert agent.memory.runs()[0]["ok"] == 1


@pytest.mark.asyncio
async def test_job_exception_records_failure_and_releases_slot(make_agent, monkeypatch):
    agent = make_agent()
    job_id = agent.memory.add_job("Broken", "* * * * *", "work", 0)

    async def run_job(job):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(agent, "run_job", run_job)
    await execute(agent, agent.memory.job(job_id))
    run = agent.memory.runs()[0]
    assert run["ok"] == 0 and "model unavailable" in run["output"]
    assert job_id not in running_jobs


@pytest.mark.asyncio
async def test_job_paused_or_deleted_during_earlier_job_does_not_run(make_agent, monkeypatch):
    agent = make_agent()
    first = agent.memory.add_job("First", "* * * * *", "one", 0)
    paused = agent.memory.add_job("Paused", "* * * * *", "two", 0)
    deleted = agent.memory.add_job("Deleted", "* * * * *", "three", 0)
    ran = []

    async def run_job(job):
        ran.append(job.id)
        if job.id == first:  # Roland changes the others while the first one runs
            agent.memory.set_job_enabled(paused, False)
            agent.memory.delete_job(deleted)
        return True, "ok"

    monkeypatch.setattr(agent, "run_job", run_job)
    assert await run_due_jobs(agent, now=100) == 1
    assert ran == [first]


@pytest.mark.asyncio
async def test_job_paused_and_resumed_during_earlier_job_waits_for_next_time(make_agent, monkeypatch):
    from agent.schedule import next_run_after
    agent = make_agent()
    first = agent.memory.add_job("First", "* * * * *", "one", 0)
    second = agent.memory.add_job("Second", "* * * * *", "two", 0)
    ran = []

    async def run_job(job):
        ran.append(job.id)
        if job.id == first:  # pause and resume like the Jobs tab does
            agent.memory.set_job_enabled(second, False)
            agent.memory.set_next_run(second, next_run_after("* * * * *", agent.config.timezone))
            agent.memory.set_job_enabled(second, True)
        return True, "ok"

    monkeypatch.setattr(agent, "run_job", run_job)
    assert await run_due_jobs(agent, now=100) == 1
    assert ran == [first]
