"""Public async CLI contracts and an opt-in real subprocess/Docker workflow."""

import json
import os
from pathlib import Path
import re
import subprocess
import time

from click.testing import CliRunner
import pytest

from agentic_harness import cli, execution
from agentic_harness.benchmarks import PROJECT_ROOT
from agentic_harness.db import Database
from agentic_harness.models import TaskResult

REPLAY = PROJECT_ROOT / "tests/fixtures/task_responses.json"


def invoke(path, command, *args):
    return CliRunner().invoke(cli.cli, [command, "--db", str(path), *args])


def submit(path, *args):
    result = invoke(path, "run", "--agent", "agents/mini-swe-agent", "--benchmark", "bash-operations",
                    "--replay", str(REPLAY), *args)
    assert result.exit_code == 0, result.output
    assert re.fullmatch(r"[0-9a-f]{32}\n", result.output)
    return result.output.strip()


def report(path, run_id, command="results"):
    result = invoke(path, command, "--run_id", run_id)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_submission_returns_without_execution_and_tasks_remain_durable(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(execution, "run_task", lambda *_: pytest.fail("Must not execute during submission"))
    monkeypatch.setattr(cli, "run_workers", lambda *_, **__: pytest.fail("Must not start a worker during submission"))
    run_id = submit(path, "--task_ids", "bash-001,bash-002")
    # Each CLI invocation opens and closes the database independently.
    status = report(path, run_id, "status")
    assert status["state"] == "queued" and status["counts"]["queued"] == 2
    assert status["progress"] == {"finished": 0, "total": 2, "fraction": 0.0}
    assert [task["state"] for task in status["tasks"]] == ["queued", "queued"]
    results = report(path, run_id)
    assert results["summary"]["failed"] == 0 and results["summary"]["graded_accuracy"] is None
    assert results["duration_seconds"] is None
    assert results["usage"] == {"model_calls": None, "cost": None}
    assert all(task["passed"] is None and task["score"] is None for task in results["tasks"])


@pytest.mark.parametrize("args", [
    ["--task_ids", "missing"], ["--task_ids", "bash-001,missing"],
    ["--task_ids", "bash-001,bash-001"], ["--task_ids", ""],
    ["--agent", "unknown"], ["--benchmark", "unknown"],
    ["--model", "openai/example"], ["--timeout", "nan"],
])
def test_invalid_submission_has_no_persisted_records(tmp_path, args):
    path = tmp_path / "queue.sqlite3"
    result = invoke(path, "run", "--agent", "agents/mini-swe-agent", "--benchmark", "bash-operations",
                    "--replay", str(REPLAY), *args)
    assert result.exit_code != 0
    with Database(path) as db:
        assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.connection.execute("SELECT count(*) FROM task_executions").fetchone()[0] == 0


def test_live_submission_needs_explicit_model_but_not_credentials(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = ["--agent", "agents/mini-swe-agent", "--benchmark", "bash-operations", "--task_ids", "bash-001"]
    missing = invoke(path, "run", *args)
    assert missing.exit_code != 0 and "exactly one" in missing.output
    result = invoke(path, "run", *args, "--model", "openai/example")
    assert result.exit_code == 0
    assert report(path, result.output.strip(), "status")["state"] == "queued"


def test_status_transitions_and_partial_usage_do_not_invent_measurements(tmp_path):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, "--task_ids", "bash-001,bash-002")
    with Database(path) as db:
        claim = db.claim_next("test-worker")
        assert report(path, run_id, "status")["state"] == "running"
        result = TaskResult("bash-001", "completed", "Submitted", True, 1.0, 0.25,
                            {"agent": {"model_calls": 3, "cost": 0.0}})
        db.finish_task(claim["task_execution_id"], claim["claim_token"], result, worker_id="test-worker")
    status = report(path, run_id, "status")
    assert status["state"] == "queued"  # No active worker; more work remains.
    assert status["counts"]["completed"] == 1 and status["counts"]["failed"] == 0
    assert status["progress"]["finished"] == 1
    partial = report(path, run_id)
    assert partial["summary"]["accuracy"] == 0.5 and partial["summary"]["graded_accuracy"] == 1.0
    assert partial["usage"] == {"model_calls": None, "cost": None}
    assert partial["tasks"][0]["model_calls"] == 3
    assert partial["tasks"][1]["model_calls"] is None
    assert "claim_token" not in json.dumps(partial)


@pytest.mark.parametrize("status,passed,score,overall", [
    ("completed", True, 1.0, "completed"), ("completed", False, 0.0, "completed"),
    ("error", None, None, "error"), ("timed_out", None, None, "timed_out"),
    ("interrupted", None, None, "interrupted"),
])
def test_formats_agree_on_terminal_outcomes(tmp_path, status, passed, score, overall):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, "--task_ids", "bash-001")
    with Database(path) as db:
        claim = db.claim_next("test-worker")
        result = TaskResult("bash-001", status, passed=passed, score=score, duration_seconds=0.25,
                            diagnostics={"error": "example failure"} if status != "completed" else {})
        db.finish_task(claim["task_execution_id"], claim["claim_token"], result, worker_id="test-worker")
    data = report(path, run_id)
    assert data["state"] == overall
    assert data["tasks"][0]["passed"] is passed and data["tasks"][0]["score"] == score
    assert data["counts"]["failed"] == (passed is False)
    assert data["summary"]["errors"] == (status == "error")
    assert data["usage"] == {"model_calls": None, "cost": None}
    for format in ("summary", "table", "JSON"):
        output = invoke(path, "results", "--run_id", run_id, "--format", format)
        assert output.exit_code == 0, output.output
        if format == "JSON":
            assert json.loads(output.output) == data
        else:
            assert f"State: {overall}" in output.output
            for key, value in data["summary"].items():
                assert f"{key}: {cli.display(value)}\n" in output.output
            assert "bash-001" in output.output and status in output.output
            assert cli.display(passed) in output.output and cli.display(score) in output.output


def test_measured_usage_is_aggregated_without_converting_null_cost_to_zero(tmp_path):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, "--task_ids", "bash-001,bash-002")
    with Database(path) as db:
        for task_id, cost in [("bash-001", 0.0), ("bash-002", None)]:
            claim = db.claim_next("test-worker")
            db.finish_task(claim["task_execution_id"], claim["claim_token"], TaskResult(
                task_id, "completed", "Submitted", True, 1.0,
                diagnostics={"agent": {"model_calls": 2, "cost": cost}},
            ), worker_id="test-worker")
    data = report(path, run_id)
    assert data["usage"] == {"model_calls": 4, "cost": None}


def test_worker_command_forwards_configuration_and_reports_conflicts(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    calls = []
    monkeypatch.setattr(cli, "run_workers", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = invoke(path, "worker", "--concurrency", "3", "--drain", "--shutdown-timeout", "2")
    assert result.exit_code == 0
    assert calls == [((path,), {"concurrency": 3, "drain": True, "shutdown_timeout": 2.0})]
    def conflict(*args, **kwargs):
        raise RuntimeError("A coordinator already owns this database")
    monkeypatch.setattr(cli, "run_workers", conflict)
    assert "already owns" in invoke(path, "worker").output
    assert invoke(path, "worker", "--concurrency", "0").exit_code != 0


@pytest.mark.parametrize("command", ["status", "results"])
def test_missing_or_unknown_run_id_has_clear_error(tmp_path, command):
    path = tmp_path / "queue.sqlite3"
    assert invoke(path, command).exit_code == 2  # Original contract required --run_id.
    result = invoke(path, command, "--run_id", "unknown")
    assert result.exit_code == 1 and "Unknown run" in result.output


@pytest.mark.parametrize("error,expected", [
    ("AuthenticationError: Incorrect API key provided: sk-proj-FAKE****TESTSUFFIX. Check your API key.",
     "AuthenticationError: Incorrect API key provided: [REDACTED]. Check your API key."),
    ("RateLimitError: quota exceeded for sk-FAKE...TESTSUFFIX; retry later (429).",
     "RateLimitError: quota exceeded for [REDACTED]; retry later (429)."),
    ("NotFoundError: model gpt-5-mini is unavailable to key 'sk-proj-FAKEPREFIX1234567890TESTSUFFIX'.",
     "NotFoundError: model gpt-5-mini is unavailable to key '[REDACTED]'."),
])
def test_historical_provider_errors_are_safe_to_display_without_rewriting_records(
    tmp_path, monkeypatch, error, expected,
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, "--task_ids", "bash-001")
    with Database(path) as db:
        claim = db.claim_next("test-worker")
        # Simulate a legacy record written before the provider redaction fix.
        db.finish_task(claim["task_execution_id"], claim["claim_token"], TaskResult(
            "bash-001", "error", "error", diagnostics={
                "error": error, "agent": {"error": error, "messages": [{"content": error}]},
            },
        ), worker_id="test-worker")
        original = tuple(db.connection.execute(
            "SELECT error, result_json FROM task_executions",
        ).fetchone())
    for command, args in [("status", ()), ("results", ()),
                          ("results", ("--format", "summary")), ("results", ("--format", "table"))]:
        output = invoke(path, command, "--run_id", run_id, *args)
        assert output.exit_code == 0, output.output
        assert "FAKE" not in output.output and "TESTSUFFIX" not in output.output
        if not args:
            displayed = json.loads(output.output)["tasks"][0]
            assert displayed["error"] == expected
            if command == "results":
                assert displayed["diagnostics"]["agent"]["error"] == displayed["error"]
                assert displayed["diagnostics"]["agent"]["messages"][0]["content"] == displayed["error"]
    with Database(path) as db:
        assert tuple(db.connection.execute(
            "SELECT error, result_json FROM task_executions",
        ).fetchone()) == original


@pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="Offline subprocess/Docker workflow")
def test_all_eight_tasks_through_public_cli_survive_worker_restart(tmp_path):
    path = tmp_path / "queue.sqlite3"
    def command(name, *args):
        return subprocess.run(["uv", "run", "harness", name, "--db", str(path), *args],
                              cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=150)
    runs = []
    for benchmark in ("bash-operations", "python-tasks"):
        response = command("run", "--agent", "agents/mini-swe-agent", "--benchmark", benchmark, "--replay", str(REPLAY))
        assert response.returncode == 0, response.stderr
        runs.append(response.stdout.strip())
    assert [json.loads(command("status", "--run_id", run_id).stdout)["state"] for run_id in runs] == ["queued", "queued"]
    coordinator = subprocess.Popen(["uv", "run", "harness", "worker", "--db", str(path), "--concurrency", "2", "--drain"],
                                   cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    saw_running = False
    maximum = 0
    deadline = time.monotonic() + 140
    try:
        while coordinator.poll() is None and time.monotonic() < deadline:
            with Database(path) as db:
                active = db.running_tasks()
            maximum = max(maximum, len(active))
            saw_running |= bool(active)
            assert len(active) <= 2
            time.sleep(0.05)
        stdout, stderr = coordinator.communicate(timeout=5)
        assert coordinator.returncode == 0, (stdout, stderr)
    finally:
        if coordinator.poll() is None:
            coordinator.terminate()
            coordinator.communicate(timeout=20)
    assert saw_running and maximum <= 2
    # Restarting an empty coordinator must not execute completed work again.
    restarted = command("worker", "--concurrency", "2", "--drain")
    assert restarted.returncode == 0, restarted.stderr
    for run_id, expected in zip(runs, (5, 3)):
        status = json.loads(command("status", "--run_id", run_id).stdout)
        assert status["state"] == "completed" and status["counts"]["completed"] == expected
        result = json.loads(command("results", "--run_id", run_id, "--format", "json").stdout)
        assert result["summary"]["passed"] == expected
        assert result["summary"]["accuracy"] == result["summary"]["graded_accuracy"] == 1.0
        assert result["usage"]["cost"] == 0.0 and result["usage"]["model_calls"] > 0
        for format in ("summary", "table"):
            output = command("results", "--run_id", run_id, "--format", format)
            assert output.returncode == 0, output.stderr
            assert f"passed: {expected}\n" in output.stdout
            assert all(task["task_id"] in output.stdout for task in result["tasks"])
