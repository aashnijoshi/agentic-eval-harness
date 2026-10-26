"""Real temporary SQLite files; no Docker, credentials, or model calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import sqlite3
from threading import Barrier

import pytest

from agentic_harness import execution
from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks, PROJECT_ROOT
from agentic_harness.db import Database
from agentic_harness.models import TaskResult

RESPONSES = PROJECT_ROOT / "tests/fixtures/task_responses.json"
CONFIG = {"agent": "agents/mini-swe-agent", "benchmark": "bash-operations", "replay": RESPONSES}


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "queue.sqlite3") as database:
        yield database


def submit(db, task_ids=None, **changes):
    return db.submit_run(**(CONFIG | changes), task_ids=task_ids)


def finish(db, claim, result):
    db.finish_task(claim["task_execution_id"], claim["claim_token"], result, worker_id=claim["worker_id"])


def success(task_id):
    return TaskResult(task_id, "completed", "Submitted", True, 1.0, 1.25, {"grading": {"actual": "ok"}})


def test_initialization_is_idempotent_and_enforces_constraints(db):
    db.initialize()
    db.initialize()
    tables = db.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert {row["name"] for row in tables} == {"runs", "task_executions"}
    assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert "task_queue_state" in {row["name"] for row in db.connection.execute("PRAGMA index_list(task_executions)")}
    with pytest.raises(sqlite3.IntegrityError), db.connection:
        db.connection.execute(
            """INSERT INTO task_executions (task_execution_id, run_id, task_id, position, state)
               VALUES ('orphan', 'unknown', 'bash-001', 0, 'queued')"""
        )
    run_id = submit(db, ["bash-001"])
    with pytest.raises(sqlite3.IntegrityError), db.connection:
        db.connection.execute(
            """INSERT INTO task_executions (task_execution_id, run_id, task_id, position, state)
               VALUES ('duplicate', ?, 'bash-001', 1, 'queued')""", (run_id,),
        )
    with pytest.raises(sqlite3.IntegrityError), db.connection:
        db.connection.execute("UPDATE task_executions SET state = 'invalid'")


@pytest.mark.parametrize("benchmark_type,name", [(BashOperations, "bash-operations"), (PythonTasks, "python-tasks")])
def test_submit_all_tasks_and_explicit_selection(db, benchmark_type, name):
    expected = benchmark_type().task_ids()
    run_id = submit(db, benchmark=name)
    run = db.get_run(run_id)
    assert run["agent"] == CONFIG["agent"] and run["benchmark"] == name
    assert run["task_ids"] == expected
    assert [task["task_id"] for task in run["tasks"]] == expected
    assert all(task["state"] == "queued" for task in run["tasks"])
    assert all(task["started_at"] is None and task["finished_at"] is None for task in run["tasks"])
    assert len({task["task_execution_id"] for task in run["tasks"]}) == len(expected)
    assert run["created_at"].endswith("+00:00")
    assert run["config"] == {
        "model": None, "replay": str(RESPONSES.resolve()), "api_key_env": "OPENAI_API_KEY",
        "timeout": 60, "step_limit": 20, "cost_limit": 1.0,
    }
    selected = list(reversed(expected[:2]))
    other = submit(db, selected, benchmark=name)
    assert other != run_id
    assert db.get_run(other)["task_ids"] == selected


def test_submission_rolls_back_run_and_preceding_tasks_when_second_insert_fails(db):
    existing = submit(db, ["bash-001"])
    db.connection.execute("""
        CREATE TRIGGER fail_second_task BEFORE INSERT ON task_executions
        WHEN NEW.position = 1 BEGIN SELECT RAISE(ABORT, 'injected task insert failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected task insert failure"):
        submit(db, ["bash-001", "bash-002"])
    assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert db.connection.execute("SELECT count(*) FROM task_executions").fetchone()[0] == 1
    assert db.get_run(existing)["tasks"][0]["state"] == "queued"
    assert not db.connection.in_transaction


@pytest.mark.parametrize("task_ids", [[], ["bash-001", "bash-001"], ["bash-001", "missing"], [""], [None], "bash-001"])
def test_invalid_task_selection_does_not_insert_a_run(db, task_ids):
    with pytest.raises(ValueError):
        submit(db, task_ids)
    assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("changes", [
    {"agent": "other-agent"}, {"benchmark": "unknown"}, {"model": "openai/example"},
    {"replay": None}, {"model": "no-provider", "replay": None},
    {"model": "https://user:password@example/model", "replay": None},
    {"api_key_env": "literal-secret-value"}, {"timeout": 0}, {"timeout": float("nan")},
    {"step_limit": 1.5}, {"step_limit": True}, {"cost_limit": float("inf")},
])
def test_invalid_configuration_is_rejected_before_submission(db, changes):
    with pytest.raises(ValueError):
        submit(db, ["bash-001"], **changes)
    assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_replay_must_exist_be_valid_and_cover_selected_tasks(db, tmp_path):
    replay = tmp_path / "replay.json"
    with pytest.raises(OSError):
        submit(db, ["bash-001"], replay=replay)
    for content in ["not-json", "[]", json.dumps({"other-task": ["response"]})]:
        replay.write_text(content)
        with pytest.raises(ValueError):
            submit(db, ["bash-001"], replay=replay)
    assert db.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.parametrize("task_count", [1, 2])
def test_competing_connections_claim_each_task_at_most_once(tmp_path, task_count):
    path = tmp_path / "race.sqlite3"
    tasks = ["bash-001", "bash-002"][:task_count]
    with Database(path) as db:
        submit(db, tasks)
    barrier = Barrier(2)

    def claim(worker):
        with Database(path) as database:
            barrier.wait(timeout=5)
            result = database.claim_next(worker)
            assert not database.connection.in_transaction
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ["worker-a", "worker-b"]))
    actual = [claim for claim in claims if claim is not None]
    assert len(actual) == task_count
    assert {claim["task_id"] for claim in actual} == set(tasks)
    assert len({claim["claim_token"] for claim in actual}) == task_count
    assert all(claim["state"] == "running" and claim["claimed_at"] == claim["started_at"] for claim in actual)
    with Database(path) as db:
        assert db.claim_next("worker-c") is None
        # A claim must not hold the write lock during execution
        db.connection.execute("BEGIN IMMEDIATE")
        db.connection.rollback()


def test_empty_queue_and_invalid_worker_id(db):
    assert db.claim_next("worker") is None
    for worker in ["", "   ", None]:
        with pytest.raises(ValueError):
            db.claim_next(worker)
    assert not db.connection.in_transaction


def test_claims_follow_run_creation_and_task_selection_order(db):
    first = submit(db, ["bash-002", "bash-001"])
    second = submit(db, ["bash-003"])
    claims = [db.claim_next("worker") for _ in range(3)]
    assert [(claim["run_id"], claim["task_id"]) for claim in claims] == [
        (first, "bash-002"), (first, "bash-001"), (second, "bash-003"),
    ]


def test_valid_completion_persists_full_result_and_metadata(db):
    run_id = submit(db, ["bash-001", "bash-002"])
    claim = db.claim_next("worker")
    result = success("bash-001")
    finish(db, claim, result)
    status = db.get_run(run_id)
    assert [task["state"] for task in status["tasks"]] == ["completed", "queued"]
    task = status["tasks"][0]
    assert task["worker_id"] == "worker"
    assert task["claimed_at"] == claim["claimed_at"] and task["finished_at"] >= task["started_at"]
    assert "claim_token" not in task
    persisted = db.get_results(run_id)
    assert [asdict(item) for item in persisted.results] == [asdict(result)]
    assert persisted.duration_seconds == 1.25
    assert persisted.summary["accuracy"] == 0.5 and persisted.summary["graded_accuracy"] == 1.0


@pytest.mark.parametrize("field,value", [
    ("claim_token", "wrong"), ("worker_id", "other-worker"), ("task_execution_id", "unknown"),
])
def test_incorrect_ownership_cannot_complete_task(db, field, value):
    run_id = submit(db, ["bash-001"])
    claim = db.claim_next("worker")
    with pytest.raises(ValueError, match="Claim is not active"):
        finish(db, claim | {field: value}, success("bash-001"))
    assert db.get_run(run_id)["tasks"][0]["state"] == "running"
    assert db.get_results(run_id).results == []
    finish(db, claim, success("bash-001"))


def test_task_id_must_match_claim_and_completion_cannot_happen_twice(db):
    run_id = submit(db, ["bash-001"])
    claim = db.claim_next("worker")
    with pytest.raises(ValueError, match="task ID does not match"):
        finish(db, claim, success("bash-002"))
    finish(db, claim, success("bash-001"))
    with pytest.raises(ValueError, match="Claim is not active"):
        finish(db, claim, TaskResult("bash-001", "error", diagnostics={"error": "late failure"}))
    assert db.get_results(run_id).results[0].passed is True


def test_revoked_token_cannot_overwrite_a_new_claim_or_its_outcome(db):
    run_id = submit(db, ["bash-001"])
    old = db.claim_next("worker")
    # Simulate explicit recovery; the harness never requeues automatically
    with db.connection:
        db.connection.execute("UPDATE task_executions SET claim_token = 'new-token', worker_id = 'new-worker'")
    newer = old | {"claim_token": "new-token", "worker_id": "new-worker"}
    with pytest.raises(ValueError, match="Claim is not active"):
        finish(db, old, success("bash-001"))
    finish(db, newer, TaskResult("bash-001", "interrupted", diagnostics={"error": "explicit recovery"}))
    with pytest.raises(ValueError, match="Claim is not active"):
        finish(db, old, success("bash-001"))
    assert db.get_results(run_id).results[0].execution_status == "interrupted"


@pytest.mark.parametrize("changes", [
    {"execution_status": "queued"}, {"passed": None}, {"score": 0.5}, {"passed": 1},
    {"duration_seconds": -1}, {"duration_seconds": float("nan")},
    {"diagnostics": {"error": {"invalid": True}}}, {"diagnostics": {"value": float("nan")}},
])
def test_invalid_result_does_not_consume_claim(db, changes):
    run_id = submit(db, ["bash-001"])
    claim = db.claim_next("worker")
    with pytest.raises(ValueError):
        finish(db, claim, replace(success("bash-001"), **changes))
    assert db.get_run(run_id)["tasks"][0]["state"] == "running"
    finish(db, claim, success("bash-001"))


def test_results_and_old_running_claims_survive_reopen_without_retry(tmp_path):
    path = tmp_path / "durable.sqlite3"
    with Database(path) as db:
        run_id = submit(db, ["bash-001", "bash-002"])
        finish(db, db.claim_next("worker-a"), success("bash-001"))
        running = db.claim_next("worker-b")
        with db.connection:
            db.connection.execute("UPDATE task_executions SET claimed_at = '2000-01-01T00:00:00+00:00' WHERE state = 'running'")
    with Database(path) as db:
        assert db.get_results(run_id).results == [success("bash-001")]
        assert [task["state"] for task in db.get_run(run_id)["tasks"]] == ["completed", "running"]
        assert db.claim_next("worker-c") is None
        # The original token can still finish its task after reopening the database
        finish(db, running, success("bash-002"))


def test_aggregation_matches_synchronous_execution_and_preserves_error_grades(db, monkeypatch):
    tasks = BashOperations().task_ids()
    outcomes = [
        success(tasks[0]), TaskResult(tasks[1], "completed", "Submitted", False, 0.0),
        TaskResult(tasks[2], "timed_out", diagnostics={"error": "agent timed out"}),
        TaskResult(tasks[3], "error", diagnostics={"error": "setup failed"}),
        TaskResult(tasks[4], "interrupted", diagnostics={"error": "interrupted"}),
    ]
    monkeypatch.setattr(execution, "run_task", lambda benchmark, agent, task_id: outcomes[tasks.index(task_id)])
    synchronous = execution.run_benchmark(BashOperations(), MiniSweAgent(replay=RESPONSES))
    run_id = submit(db)
    claims = [db.claim_next("worker") for _ in tasks]
    for claim, outcome in reversed(list(zip(claims, outcomes))):
        finish(db, claim, outcome)
    persisted = db.get_results(run_id)
    assert persisted.results == outcomes
    assert persisted.summary == synchronous.summary == {
        "total": 5, "completed": 2, "graded": 2, "passed": 1, "failed": 1,
        "errors": 1, "timed_out": 1, "interrupted": 1, "accuracy": 0.2, "graded_accuracy": 0.5,
    }
    assert [task["error"] for task in db.get_run(run_id)["tasks"]] == [None, None, "agent timed out", "setup failed", "interrupted"]
    # A cleanup error can coexist with a valid grade
    other = submit(db, ["bash-001"])
    finish(db, db.claim_next("worker"), replace(success("bash-001"), execution_status="error"))
    summary = db.get_results(other).summary
    assert summary["errors"] == 1 and summary["passed"] == 1 and summary["graded"] == 1


def test_pending_tasks_have_no_results_and_do_not_count_as_failed_grades(db):
    run_id = submit(db, ["bash-001", "bash-002"])
    db.claim_next("worker")
    persisted = db.get_results(run_id)
    assert persisted.results == []
    assert persisted.summary == {
        "total": 2, "completed": 0, "graded": 0, "passed": 0, "failed": 0,
        "errors": 0, "timed_out": 0, "interrupted": 0, "accuracy": 0.0, "graded_accuracy": None,
    }


def test_secrets_are_not_copied_from_environment_or_arbitrary_config(tmp_path, monkeypatch):
    path = tmp_path / "secrets.sqlite3"
    secret = "fake-provider-credential-DO-NOT-PERSIST"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    with Database(path) as db:
        run_id = submit(db, ["bash-001"], replay=None, model="anthropic/example", api_key_env="ANTHROPIC_API_KEY")
        config = db.get_run(run_id)["config"]
        assert config["model"] == "anthropic/example" and config["api_key_env"] == "ANTHROPIC_API_KEY"
        assert secret not in json.dumps(config)
        for extra in [{"api_key": secret}, {"config": {"api_key": secret}}, {"secrets": [secret]}]:
            with pytest.raises(TypeError):
                submit(db, ["bash-001"], **extra)
        monkeypatch.delenv("OPENAI_API_KEY")
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        # Queueing must not read worker credentials
        submit(db, ["bash-002"], replay=None, model="openai/example")
    assert secret.encode() not in path.read_bytes()


@pytest.mark.parametrize("method", ["get_run", "get_results"])
def test_unknown_runs_are_reported(db, method):
    with pytest.raises(ValueError, match="Unknown run"):
        getattr(db, method)("unknown")
