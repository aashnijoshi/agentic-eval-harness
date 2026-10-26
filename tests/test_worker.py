"""Real POSIX child processes, with offline runners and opt-in Docker coverage."""

import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import time
from uuid import uuid4

import pytest

from agentic_harness import worker
from agentic_harness.benchmarks import PROJECT_ROOT
from agentic_harness.db import Database
from agentic_harness.execution import Container
from agentic_harness.models import TaskResult

CTX = multiprocessing.get_context("fork")
REPLAY = PROJECT_ROOT / "tests/fixtures/task_responses.json"
REAL_CLEANUP = worker.cleanup_claim
DOCKER = pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="Opt-in offline Docker tests")


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for child process state")


def record(path, task_id):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (task_id + "\n").encode())
    finally:
        os.close(fd)


def submit(path, task_ids=None, **config):
    with Database(path) as db:
        return db.submit_run(**({
            "agent": "agents/mini-swe-agent", "benchmark": "bash-operations", "replay": REPLAY,
        } | config), task_ids=task_ids)


def states(path, run_id):
    with Database(path) as db:
        return [task["state"] for task in db.get_run(run_id)["tasks"]]


def launch(path, **kwargs):
    process = CTX.Process(target=worker.run_workers, args=(path,), kwargs=kwargs)
    process.start()
    return process


def join(process, timeout=15):
    process.join(timeout)
    if process.is_alive():
        process.kill()
        process.join(5)
        pytest.fail("Coordinator failed to stop")
    assert process.exitcode == 0


@pytest.fixture
def offline(monkeypatch, tmp_path):
    calls = tmp_path / "calls"

    def execute(benchmark, agent, task_id, **ownership):
        assert len(ownership["execution_id"]) == len(ownership["claim_token"]) == 32
        assert agent.network == "none" and agent.env_names == ()
        record(calls, task_id)
        return TaskResult(task_id, "completed", "Submitted", True, 1.0)

    monkeypatch.setattr(worker, "run_task", execute)
    monkeypatch.setattr(worker, "cleanup_claim", lambda claim: None)
    return calls


@pytest.mark.parametrize("count", [1, 2])
def test_queued_tasks_execute_and_survive_reopen(tmp_path, offline, monkeypatch, count):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "queue.sqlite3"
    ids = ["bash-001", "bash-002"][:count]
    run_id = submit(path, ids)
    worker.run_workers(path, concurrency=2, drain=True)
    assert states(path, run_id) == ["completed"] * count
    assert sorted(offline.read_text().splitlines()) == ids
    with Database(path) as db:
        assert db.get_results(run_id).summary["passed"] == count
    worker.run_workers(path, drain=True)
    assert len(offline.read_text().splitlines()) == count


@pytest.mark.parametrize("status,passed,score", [
    ("completed", False, 0.0), ("error", None, None), ("timed_out", None, None),
])
def test_failures_do_not_stop_other_tasks(tmp_path, offline, monkeypatch, status, passed, score):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001", "bash-002"])

    def execute(benchmark, agent, task_id, **ownership):
        if task_id == "bash-001":
            return TaskResult(task_id, status, passed=passed, score=score, diagnostics={"error": "test outcome"})
        return TaskResult(task_id, "completed", "Submitted", True, 1.0)

    monkeypatch.setattr(worker, "run_task", execute)
    worker.run_workers(path, drain=True)
    with Database(path) as db:
        results = db.get_results(run_id).results
    assert results[0].execution_status == status and results[0].passed is passed
    assert results[1].passed is True


def test_real_process_concurrency_is_bounded_and_claims_are_unique(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path)
    active, maximum = CTX.Value("i", 0), CTX.Value("i", 0)
    ready, release = CTX.Event(), CTX.Event()

    def execute(benchmark, agent, task_id, **ownership):
        with active.get_lock():
            active.value += 1
            maximum.value = max(maximum.value, active.value)
            if active.value == 2:
                ready.set()
        try:
            record(offline, task_id)
            assert release.wait(10)
            return TaskResult(task_id, "completed", "Submitted", True, 1.0)
        finally:
            with active.get_lock():
                active.value -= 1

    monkeypatch.setattr(worker, "run_task", execute)
    coordinator = launch(path, concurrency=2, drain=True)
    try:
        assert ready.wait(10)
        time.sleep(0.1)
        assert active.value == maximum.value == 2
    finally:
        release.set()
        join(coordinator)
    assert len(set(offline.read_text().splitlines())) == 5
    assert len(offline.read_text().splitlines()) == 5
    assert states(path, run_id) == ["completed"] * 5
    assert maximum.value == 2


def test_second_coordinator_and_symlink_alias_are_rejected(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    submit(path, ["bash-001"])
    ready, release, stop = CTX.Event(), CTX.Event(), CTX.Event()

    def execute(*args, **kwargs):
        ready.set()
        release.wait(10)
        return TaskResult("bash-001", "completed", "Submitted", True, 1.0)

    monkeypatch.setattr(worker, "run_task", execute)
    coordinator = launch(path, stop_event=stop)
    try:
        assert ready.wait(10)
        alias = tmp_path / "alias.sqlite3"
        alias.symlink_to(path)
        for candidate in (path, alias):
            with pytest.raises(RuntimeError, match="already owns"):
                worker.run_workers(candidate, drain=True)
    finally:
        release.set()
        stop.set()
        join(coordinator)


@pytest.mark.parametrize("how", ["event", "sigint", "sigterm"])
def test_shutdown_stops_new_work_and_interrupts_current_task(tmp_path, offline, monkeypatch, how):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001", "bash-002"])
    ready, stop = CTX.Event(), CTX.Event()

    def execute(benchmark, agent, task_id, **ownership):
        record(offline, task_id)
        ready.set()
        time.sleep(30)

    monkeypatch.setattr(worker, "run_task", execute)
    coordinator = launch(path, concurrency=1, stop_event=stop)
    assert ready.wait(10)
    if how == "event":
        stop.set()
    else:
        os.kill(coordinator.pid, signal.SIGINT if how == "sigint" else signal.SIGTERM)
    join(coordinator)
    assert states(path, run_id) == ["interrupted", "queued"]
    assert offline.read_text().splitlines() == ["bash-001"]


def test_worker_crash_is_reconciled_without_paid_retry_and_stale_finish_is_rejected(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-no-paid-calls")
    run_id = submit(path, ["bash-001"], replay=None, model="openai/example")
    captured = tmp_path / "claim.json"

    def crash(benchmark, agent, task_id, **ownership):
        record(offline, task_id)
        with Database(path) as db:
            captured.write_text(json.dumps(db.running_tasks()[0]))
        os._exit(7)

    monkeypatch.setattr(worker, "run_task", crash)
    worker.run_workers(path, drain=True)
    worker.run_workers(path, drain=True)
    assert offline.read_text().splitlines() == ["bash-001"]
    claim = json.loads(captured.read_text())
    with Database(path) as db:
        assert db.get_results(run_id).results[0].execution_status == "interrupted"
        with pytest.raises(ValueError, match="Claim is not active"):
            db.finish_task(claim["task_execution_id"], claim["claim_token"], TaskResult("bash-001", "completed", passed=True, score=1), worker_id=claim["worker_id"])
    assert b"dummy-key-no-paid-calls" not in path.read_bytes()


@pytest.mark.parametrize("key,message,expected", [
    ("dummy-secret-from-worker-environment", "failure dummy-secret-from-worker-environment",
     "RuntimeError: failure [REDACTED]"),
    ("sk-proj-FAKEPREFIX1234567890TESTSUFFIX",
     "AuthenticationError: Incorrect API key provided: sk-proj-FAKE****TESTSUFFIX. Check your API key.",
     "RuntimeError: AuthenticationError: Incorrect API key provided: [REDACTED]. Check your API key."),
])
def test_worker_exception_is_redacted_and_other_work_continues(
    tmp_path, offline, monkeypatch, key, message, expected,
):
    path = tmp_path / "queue.sqlite3"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    run_id = submit(path, ["bash-001", "bash-002"], replay=None, model="openai/example")

    def execute(benchmark, agent, task_id, **ownership):
        if task_id == "bash-001":
            raise RuntimeError(message)
        return TaskResult(task_id, "completed", "Submitted", True, 1.0)

    monkeypatch.setattr(worker, "run_task", execute)
    worker.run_workers(path, drain=True)
    with Database(path) as db:
        results = db.get_results(run_id).results
    assert results[0].diagnostics["error"] == expected
    assert results[1].passed is True
    assert key.encode() not in path.read_bytes()
    assert b"FAKE" not in path.read_bytes() and b"TESTSUFFIX" not in path.read_bytes()


def test_surviving_child_keeps_lock_after_coordinator_is_killed(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    ready, release = CTX.Event(), CTX.Event()

    def execute(*args, **kwargs):
        ready.set()
        assert release.wait(10)
        return TaskResult("bash-001", "completed", "Submitted", True, 1.0)

    monkeypatch.setattr(worker, "run_task", execute)
    coordinator = launch(path)
    try:
        assert ready.wait(10)
        coordinator.kill()
        coordinator.join(5)
        with pytest.raises(RuntimeError, match="already owns"):
            worker.run_workers(path, drain=True)
    finally:
        release.set()
    wait_for(lambda: states(path, run_id) == ["completed"])
    # Wait for the orphan to close its inherited lock
    def released():
        with open(str(path) + ".worker.lock", "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True
    wait_for(released)
    worker.run_workers(path, drain=True)


def test_restart_refuses_live_worker_then_recovers_after_verified_exit(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    ready, release = CTX.Event(), CTX.Event()

    def abandoned():
        os.setsid()
        with Database(path) as db:
            db.claim_next(f"{worker.WORKER_PREFIX}{os.getpid()}:{uuid4().hex}")
        ready.set()
        release.wait(10)

    process = CTX.Process(target=abandoned)
    process.start()
    assert ready.wait(10)
    try:
        with pytest.raises(RuntimeError, match="still alive"):
            worker.run_workers(path, drain=True)
        assert states(path, run_id) == ["running"]
    finally:
        release.set()
        join(process)
    cleaned = []
    def cleanup(claim):
        with pytest.raises(ProcessLookupError):
            os.kill(int(claim["worker_id"].split(":")[1]), 0)
        cleaned.append(claim["task_execution_id"])
    monkeypatch.setattr(worker, "cleanup_claim", cleanup)
    worker.run_workers(path, drain=True)
    assert len(cleaned) == 1 and states(path, run_id) == ["interrupted"]


def test_cleanup_failure_retains_claim_and_restart_retries_cleanup_only(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    def crash(*args, **kwargs):
        record(offline, "called")
        os._exit(7)
    def unavailable(claim):
        raise RuntimeError("Docker unavailable")
    monkeypatch.setattr(worker, "run_task", crash)
    monkeypatch.setattr(worker, "cleanup_claim", unavailable)
    with pytest.raises(RuntimeError, match="running claim retained"):
        worker.run_workers(path, drain=True)
    assert states(path, run_id) == ["running"]
    monkeypatch.setattr(worker, "cleanup_claim", lambda claim: None)
    worker.run_workers(path, drain=True)
    assert states(path, run_id) == ["interrupted"]
    assert offline.read_text().splitlines() == ["called"]


def test_forced_shutdown_waits_for_termination_before_cleanup(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    ready, stop = CTX.Event(), CTX.Event()
    def stuck(*args, **kwargs):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ready.set()
        time.sleep(30)
    monkeypatch.setattr(worker, "run_task", stuck)
    coordinator = launch(path, stop_event=stop, shutdown_timeout=0.1)
    assert ready.wait(10)
    stop.set()
    join(coordinator)
    assert states(path, run_id) == ["interrupted"]


def test_cleanup_requires_both_durable_labels(tmp_path, monkeypatch):
    calls = []
    def docker(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"owned-id\n" if len(calls) == 1 else b"", b"")
    monkeypatch.setattr(worker, "docker", docker)
    REAL_CLEANUP({"task_execution_id": uuid4().hex, "claim_token": uuid4().hex})
    assert calls[1] == ["rm", "--force", "owned-id"]
    assert all(any("agentic-harness." + label in argument for argument in calls[0]) for label in ("execution=", "claim="))
    assert calls[0] == calls[2]


def test_failure_before_claim_does_not_create_an_endless_respawn_loop(tmp_path, offline, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    def unavailable(*args):
        raise RuntimeError("Cannot claim")
    monkeypatch.setattr(Database, "claim_next", unavailable)
    with pytest.raises(RuntimeError, match="before claiming"):
        worker.run_workers(path, drain=True)
    assert states(path, run_id) == ["queued"]
    assert not offline.exists()


def test_hard_linked_database_is_rejected(tmp_path):
    path = tmp_path / "queue.sqlite3"
    submit(path, ["bash-001"])
    os.link(path, tmp_path / "alias.sqlite3")
    with pytest.raises(ValueError, match="Hard-linked"):
        worker.run_workers(path, drain=True)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_shutdown_timeout_is_rejected(tmp_path, timeout):
    with pytest.raises(ValueError, match="Shutdown timeout"):
        worker.run_workers(tmp_path / "queue.sqlite3", shutdown_timeout=timeout)


@pytest.mark.parametrize("concurrency", [0, -1, True, 1.5])
def test_invalid_concurrency_is_rejected(tmp_path, concurrency):
    with pytest.raises(ValueError, match="Concurrency"):
        worker.run_workers(tmp_path / "queue.sqlite3", concurrency=concurrency)


@DOCKER
def test_docker_queue_worker_grading_and_persistence(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / "queue.sqlite3"
    runs = [submit(path, ["bash-001"]), submit(path, ["py-001"], benchmark="python-tasks")]
    worker.run_workers(path, concurrency=2, drain=True)
    with Database(path) as db:
        for run_id in runs:
            result = db.get_results(run_id).results[0]
            assert result.passed is True, result.diagnostics
            assert result.diagnostics["agent"]["cost"] == 0.0
            assert result.diagnostics["execution_id"] == db.get_run(run_id)["tasks"][0]["task_execution_id"]
            assert not worker.docker(["ps", "-aq", "--filter", f"label=agentic-harness.execution={result.diagnostics['execution_id']}"]).stdout.strip()


@DOCKER
def test_docker_crash_cleanup_does_not_remove_another_active_container(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    run_id = submit(path, ["bash-001"])
    def crash(benchmark, agent, task_id, **ownership):
        Container("harness-mini:alpine", ownership["execution_id"], "agent", claim_token=ownership["claim_token"]).__enter__()
        os._exit(7)
    monkeypatch.setattr(worker, "run_task", crash)
    with Container("harness-mini:alpine", uuid4().hex, "agent", claim_token=uuid4().hex) as other:
        worker.run_workers(path, drain=True)
        assert states(path, run_id) == ["interrupted"]
        assert worker.docker(["inspect", "--format", "{{.State.Running}}", other.name]).stdout.strip() == b"true"
    with Database(path) as db:
        task = db.get_run(run_id)["tasks"][0]
    assert not worker.docker(["ps", "-aq", "--filter", f"label=agentic-harness.execution={task['task_execution_id']}"]).stdout.strip()
