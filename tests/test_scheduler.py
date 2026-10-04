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
