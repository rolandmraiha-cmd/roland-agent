import asyncio
import logging

import pytest

from agent.scheduler import execute, running_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("run_operation", ["start_run", "finish_run"])
@pytest.mark.parametrize("cleanup_operation", ["job", "set_next_run"])
async def test_cleanup_preserves_original_run_error(
    make_agent, monkeypatch, caplog, run_operation, cleanup_operation,
):
    agent = make_agent()
    job_id = agent.memory.add_job("Broken", "* * * * *", "work", 0)
    job = agent.memory.job(job_id)
    original_error = RuntimeError("original run failure")
    cleanup_error = RuntimeError("cleanup failure")

    async def run_job(_job):
        return True, "done"

    def fail_run(*_args):
        raise original_error

    def fail_cleanup(*_args):
        raise cleanup_error

    monkeypatch.setattr(agent, "run_job", run_job)
    monkeypatch.setattr(agent.memory, run_operation, fail_run)
    monkeypatch.setattr(agent.memory, cleanup_operation, fail_cleanup)
    with caplog.at_level(logging.ERROR, logger="agent.scheduler"):
        with pytest.raises(RuntimeError) as caught:
            await execute(agent, job)
    assert caught.value is original_error
    assert job_id not in running_jobs
    record = caplog.records[-1]
    assert record.getMessage() == f"failed to reschedule job {job_id} during cleanup"
    assert record.exc_info[1] is cleanup_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_operations",
    [("finish_run",), ("job",), ("set_next_run",), ("finish_run", "set_next_run")],
)
async def test_cleanup_preserves_original_cancellation(
    make_agent, monkeypatch, caplog, failed_operations,
):
    agent = make_agent()
    job_id = agent.memory.add_job("Cancelled", "* * * * *", "work", 0)
    job = agent.memory.job(job_id)
    cancellation = asyncio.CancelledError("original cancellation")
    cleanup_error = RuntimeError("cleanup failure")

    async def run_job(_job):
        raise cancellation

    def fail_cleanup(*_args):
        raise cleanup_error

    monkeypatch.setattr(agent, "run_job", run_job)
    for operation in failed_operations:
        monkeypatch.setattr(agent.memory, operation, fail_cleanup)
    with caplog.at_level(logging.ERROR, logger="agent.scheduler"):
        with pytest.raises(asyncio.CancelledError) as caught:
            await execute(agent, job)
    assert caught.value is cancellation
    assert job_id not in running_jobs
    assert len(caplog.records) == len(failed_operations)
    assert all(record.exc_info[1] is cleanup_error for record in caplog.records)
    if "finish_run" not in failed_operations:
        run = agent.memory.runs()[0]
        assert run["finished"] is not None and run["ok"] == 0
        assert "cancelled" in run["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_operation", ["job", "set_next_run"])
async def test_cleanup_error_after_success_still_propagates(
    make_agent, monkeypatch, cleanup_operation,
):
    agent = make_agent()
    job_id = agent.memory.add_job("Done", "* * * * *", "work", 0)
    job = agent.memory.job(job_id)
    cleanup_error = RuntimeError("cleanup failure")

    async def run_job(_job):
        return True, "done"

    def fail_cleanup(*_args):
        raise cleanup_error

    monkeypatch.setattr(agent, "run_job", run_job)
    monkeypatch.setattr(agent.memory, cleanup_operation, fail_cleanup)
    with pytest.raises(RuntimeError) as caught:
        await execute(agent, job)
    assert caught.value is cleanup_error
    assert job_id not in running_jobs
    run = agent.memory.runs()[0]
    assert run["ok"] == 1 and run["output"] == "done"
