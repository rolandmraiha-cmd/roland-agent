"""M9 acceptance checks must fail for missing or misleading evidence."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent.config import Config
from agent.models.benchmark import benchmark, rates, sample
from deploy.verify_state import expected_services, logs, services
from tests.test_deploy_scripts import fake_commands, fake_deploy_host, run_fake_script, update_fake_state


def service_rows():
    return [
        {"Service": name, "State": "running", "Health": "healthy"}
        for name in ("caddy", "core", "model", "sandbox")
    ]


@pytest.mark.parametrize("array", [False, True])
def test_compose_inventory_accepts_both_docker_json_formats(array):
    rows = service_rows()
    rows[1]["Publishers"] = [{"TargetPort": 8080, "PublishedPort": 0}]
    rows[1]["Ports"] = "8080/tcp"  # EXPOSE is not a host publication.
    services(json.dumps(rows) if array else "\n".join(json.dumps(row) for row in rows))


def test_expected_services_reads_resolved_data_without_shell_execution(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    config = {
        "services": {
            "core": {
                "environment": {
                    "TRAINER_URL": f"$(touch {sentinel})",
                }
            }
        }
    }
    assert "trainer" in expected_services(config)
    assert not sentinel.exists()


@pytest.mark.parametrize("value", [" true ", "\tON\t", True])
def test_expected_services_matches_runtime_boolean_normalisation(value):
    config = {"services": {"core": {"environment": {"BROWSER_ENABLED": value}}}}
    assert "browser" in expected_services(config)


@pytest.mark.parametrize("observation", ["", "[]", "{}", "null", "not JSON"])
def test_compose_inventory_refuses_missing_or_broken_observations(observation):
    with pytest.raises(ValueError):
        services(observation)


@pytest.mark.parametrize("change", ["missing", "stopped", "unhealthy", "unknown-health", "loopback-port"])
def test_compose_inventory_cannot_pass_an_incomplete_or_exposed_stack(change):
    rows = service_rows()
    if change == "missing":
        rows.pop()
    elif change == "stopped":
        rows[1]["State"] = "exited"
    elif change == "unhealthy":
        rows[1]["Health"] = "unhealthy"
    elif change == "unknown-health":
        rows[1].pop("Health")
    else:
        rows[1]["Publishers"] = [{"URL": "127.0.0.1", "PublishedPort": 8080}]
    with pytest.raises(ValueError):
        services(json.dumps(rows))


@pytest.mark.parametrize(
    "options",
    [{"max-size": "10m"}, {"max-size": "10m", "max-file": "30"}, {"max-size": "110m", "max-file": "3"}, {}],
)
def test_log_rotation_checks_exact_limits(options):
    with pytest.raises(ValueError):
        logs(json.dumps({"Type": "json-file", "Config": options}))


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("backup_rc", 1, "no nonempty database backup"),
        ("model_health_rc", 1, "model healthcheck"),
        ("model_egress_rc", 0, "model container reached the internet"),
        ("model_egress_rc", 127, "model egress probe failed"),
        ("log_config", {"Type": "json-file", "Config": {"max-size": "10m"}}, "required log rotation"),
    ],
)
def test_verify_reports_release_blockers_as_failures(tmp_path, setting, value, message):
    repo, env, state = fake_deploy_host(tmp_path)
    state[setting] = value
    update_fake_state(env, state)
    result = run_fake_script(repo, env, "verify.sh")
    assert result.returncode != 0
    assert "[FAIL] " + message in result.stdout or message in result.stdout


def test_verify_requires_isolation_script(tmp_path):
    repo, env, _ = fake_deploy_host(tmp_path)
    (repo / "tests/integration/isolation.sh").unlink()
    result = run_fake_script(repo, env, "verify.sh")
    assert result.returncode != 0 and "[FAIL] isolation.sh --server missing" in result.stdout


@pytest.mark.parametrize(
    ("setting", "service"),
    [
        ("BROWSER_ENABLED=true", "browser"),
        ("SCREEN_ENABLED='true' # enabled", "novnc"),
        ("TRAINER_URL=http://10.77.7.70:7200", "trainer"),
    ],
)
def test_verify_cannot_skip_an_enabled_service_that_is_missing(tmp_path, setting, service):
    repo, env, _ = fake_deploy_host(tmp_path)
    with (repo / ".env").open("a") as stream:
        stream.write(setting + "\n")
    result = run_fake_script(repo, env, "verify.sh")
    assert result.returncode != 0 and "[FAIL] missing services: " + service in result.stdout


@pytest.mark.parametrize("restore_code", [0, 1])
def test_restore_drill_only_mounts_disposable_data_and_cleans_up(tmp_path, restore_code):
    repo, env, state = fake_deploy_host(tmp_path)
    state["restore_rc"] = restore_code
    update_fake_state(env, state)
    backup = tmp_path / "private backup.db.gz"
    backup.write_bytes(b"synthetic backup fixture")
    env.update(APPLY="1", FILE=str(backup))
    result = run_fake_script(repo, env, "restore.sh", "--test")
    assert (result.returncode == 0) is (restore_code == 0), result.stdout + result.stderr
    commands = [item["args"] for item in fake_commands(env) if item["command"] == "docker"]
    runs = [args for args in commands if args[0] == "run"]
    assert len(runs) == (2 if restore_code == 0 else 1)
    for args in runs:
        assert args[args.index("--network") + 1] == "none"
        mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "-v"]
        assert len(mounts) == 3
        assert all("/secrets/" not in mount and "browser-profile" not in mount for mount in mounts)
        assert all("-restore-test-" in mount for mount in mounts[:2])
        assert mounts[-1].endswith(":/input/backup.db.gz:ro")
        assert not Path(mounts[-1].split(":")[0]).exists()  # private staging copy removed
    created = {args[-1] for args in commands if args[:2] == ["volume", "create"]}
    removed = {args[-1] for args in commands if args[:2] == ["volume", "rm"]}
    assert created == removed and len(created) == 2
    assert backup.read_bytes() == b"synthetic backup fixture"
    assert not any(args[0] == "compose" for args in commands)


def test_restore_drill_does_not_claim_success_when_cleanup_fails(tmp_path):
    repo, env, state = fake_deploy_host(tmp_path)
    state["volume_rm_rc"] = 1
    update_fake_state(env, state)
    backup = tmp_path / "backup.db.gz"
    backup.write_bytes(b"fixture")
    env.update(APPLY="1", FILE=str(backup))
    result = run_fake_script(repo, env, "restore.sh", "--test")
    assert result.returncode != 0 and "restore-test PASS" not in result.stdout


async def test_benchmark_measures_first_text_and_server_token_rates_without_logging_content():
    payload = {
        "stop": True,
        "content": "",
        "timings": {
            "prompt_n": 40,
            "predicted_n": 128,
            "prompt_per_second": 12,
            "predicted_per_second": 3,
        },
    }
    body = 'data: {"content":""}\n\ndata: {"content":"private-looking test text"}\n\n'
    body += "data: " + json.dumps(payload) + "\n\n"
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text=body)

    times = iter([10, 12, 15])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await sample(client, "http://model:8080", clock=lambda: next(times))
    assert result["time_to_first_token_s"] == 2 and result["elapsed_s"] == 5
    assert result["predicted_per_second"] == 3
    assert "private-looking" not in json.dumps(result)
    assert requests[0]["cache_prompt"] is False and requests[0]["n_predict"] == 128


@pytest.mark.parametrize("timing", [None, {}, {"prompt_n": float("nan")}, {"prompt_n": True}])
def test_benchmark_refuses_missing_or_nonfinite_metrics(timing):
    with pytest.raises(ValueError):
        rates({"timings": timing})


async def test_benchmark_refuses_a_truncated_stream_without_metrics():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='data: {"content":"hello"}\n\n')
        )
    ) as client:
        with pytest.raises(ValueError, match="ended without"):
            await sample(client, "http://model:8080")


async def test_benchmark_refuses_public_model_even_when_its_host_is_allowlisted():
    config = Config(model_base_url="http://8.8.8.8:8080", model_allowed_hosts=("8.8.8.8",))
    with pytest.raises(ValueError, match="loopback"):
        await benchmark(config, 1)
