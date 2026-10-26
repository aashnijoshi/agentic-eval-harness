"""SQLite persistence and atomic task claims. No execution or automatic retries."""

import json
import math
import re
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks, PROJECT_ROOT
from agentic_harness.models import RunResult, TaskResult, summarize_results

FINAL_STATES = ("completed", "error", "timed_out", "interrupted")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    agent TEXT NOT NULL,
    benchmark TEXT NOT NULL,
    task_ids_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_executions (
    task_execution_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    task_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'completed', 'error', 'timed_out', 'interrupted')
    ),
    worker_id TEXT,
    claim_token TEXT,
    claimed_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    result_json TEXT,
    error TEXT,
    UNIQUE (run_id, task_id),
    UNIQUE (run_id, position)
);
CREATE INDEX IF NOT EXISTS task_queue_state ON task_executions(state);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    """One connection per caller/thread; close explicitly or use a context manager."""

    def __init__(self, path: str | Path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)

    def submit_run(
        self, *, agent: str, benchmark: str, task_ids: list[str] | None = None,
        model: str | None = None, replay: Path | None = None,
        api_key_env: str = "OPENAI_API_KEY", timeout: float = 60,
        step_limit: int = 20, cost_limit: float = 1.0,
    ) -> str:
        # Store explicit fields, never an agent object or its credentials
        if agent != "agents/mini-swe-agent" and Path(agent).resolve() != PROJECT_ROOT / "agents/mini-swe-agent":
            raise ValueError("Only agents/mini-swe-agent is supported")
        adapters = {"bash-operations": BashOperations, "python-tasks": PythonTasks}
        if benchmark not in adapters:
            raise ValueError(f"Unknown benchmark: {benchmark!r}")
        adapter = adapters[benchmark]()
        selected = adapter.task_ids() if task_ids is None else task_ids
        if (
            not isinstance(selected, list) or not selected
            or not all(isinstance(task_id, str) and task_id for task_id in selected)
            or len(selected) != len(set(selected))
        ):
            raise ValueError("Select at least one task, with no duplicate task IDs")
        for task_id in selected:
            task = adapter.load_task(task_id)
            if task.docker_image not in MiniSweAgent.runtimes:
                raise ValueError(f"No mini runtime for declared image {task.docker_image}")
        if (model is None) == (replay is None):
            raise ValueError("Choose exactly one model or replay file")
        if model is not None and (
            not isinstance(model, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", model)
        ):
            raise ValueError("Model must be an explicit provider/model identifier")
        if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", api_key_env):
            raise ValueError("Invalid API key environment variable name")
        if (
            type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(step_limit) is not int or step_limit <= 0
            or type(cost_limit) not in (int, float) or not math.isfinite(cost_limit) or cost_limit <= 0
        ):
            raise ValueError("Timeout, step limit, and cost limit must be finite and positive")
        replay_path = str(Path(replay).resolve()) if replay is not None else None
        if replay_path is not None:
            # Validate replay files before queueing; this needs no key or Docker
            mini = MiniSweAgent(replay=Path(replay_path), timeout=timeout, step_limit=step_limit, cost_limit=cost_limit)
            if isinstance(mini.responses, dict) and any(task_id not in mini.responses for task_id in selected):
                raise ValueError("Replay mapping must contain every selected task")
        config = {
            "model": model, "replay": replay_path, "api_key_env": api_key_env,
            "timeout": timeout, "step_limit": step_limit, "cost_limit": cost_limit,
        }
        run_id = uuid4().hex
        # Commit the run and all tasks together, or roll everything back
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, "agents/mini-swe-agent", benchmark, json.dumps(selected),
                 json.dumps(config, allow_nan=False), utc_now()),
            )
            self.connection.executemany(
                """INSERT INTO task_executions
                   (task_execution_id, run_id, task_id, position, state)
                   VALUES (?, ?, ?, ?, 'queued')""",
                [(uuid4().hex, run_id, task_id, position) for position, task_id in enumerate(selected)],
            )
        return run_id

    def claim_next(self, worker_id: str) -> dict | None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Worker ID must be a nonempty string")
        with self.connection:
            # Take the write lock before selecting so another worker can't claim this task
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """SELECT t.task_execution_id FROM task_executions AS t
                   JOIN runs AS r ON r.run_id = t.run_id
                   WHERE t.state = 'queued'
                   ORDER BY r.created_at, r.run_id, t.position LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            self.connection.execute(
                """UPDATE task_executions
                   SET state = 'running', worker_id = ?, claim_token = ?,
                       claimed_at = ?, started_at = ?
                   WHERE task_execution_id = ? AND state = 'queued'""",
                (worker_id, uuid4().hex, now, now, row["task_execution_id"]),
            )
            claim = self.connection.execute(
                "SELECT * FROM task_executions WHERE task_execution_id = ?",
                (row["task_execution_id"],),
            ).fetchone()
        # Release the transaction before Docker or model work starts
        return dict(claim)

    def finish_task(
        self, task_execution_id: str, claim_token: str, result: TaskResult, *, worker_id: str,
    ) -> None:
        if not isinstance(result, TaskResult) or result.execution_status not in FINAL_STATES:
            raise ValueError("Completion requires a TaskResult with a final execution status")
        if (
            (result.passed is not None and type(result.passed) is not bool)
            or (result.passed is None and result.score is not None)
            or (result.passed is not None and (
                type(result.score) not in (int, float) or result.score != float(result.passed)
            ))
            or (result.execution_status == "completed" and result.passed is None)
            or type(result.duration_seconds) not in (int, float)
            or not math.isfinite(result.duration_seconds) or result.duration_seconds < 0
            or not isinstance(result.diagnostics, dict)
        ):
            raise ValueError("Invalid TaskResult grade, duration, or diagnostics")
        error = result.diagnostics.get("error")
        if error is not None and not isinstance(error, str):
            raise ValueError("TaskResult error must be a string or null")
        payload = json.dumps(asdict(result), allow_nan=False)
        with self.connection:
            updated = self.connection.execute(
                """UPDATE task_executions
                   SET state = ?, finished_at = ?, result_json = ?, error = ?
                   WHERE task_execution_id = ? AND task_id = ? AND state = 'running'
                     AND worker_id = ? AND claim_token = ?""",
                (result.execution_status, utc_now(), payload, error,
                 task_execution_id, result.task_id, worker_id, claim_token),
            )
            if updated.rowcount != 1:
                raise ValueError("Claim is not active, ownership is incorrect, or task ID does not match")

    def get_run(self, run_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id!r}")
        run = dict(row)
        run["task_ids"] = json.loads(run.pop("task_ids_json"))
        run["config"] = json.loads(run.pop("config_json"))
        run["tasks"] = [dict(task) for task in self.connection.execute(
            """SELECT task_execution_id, task_id, position, state, worker_id,
                      claimed_at, started_at, finished_at, error
               FROM task_executions WHERE run_id = ? ORDER BY position""", (run_id,),
        )]
        return run

    def running_tasks(self, worker_id: str | None = None) -> list[dict]:
        """Ownership records for reconciliation; claim tokens stay out of public status."""
        return [dict(row) for row in self.connection.execute(
            """SELECT * FROM task_executions WHERE state = 'running'
               AND (? IS NULL OR worker_id = ?)""", (worker_id, worker_id),
        )]

    def get_results(self, run_id: str) -> RunResult:
        """Return finished results only; duration is cumulative task execution time."""
        run = self.get_run(run_id)
        results = [TaskResult(**json.loads(row["result_json"])) for row in self.connection.execute(
            """SELECT result_json FROM task_executions
               WHERE run_id = ? AND result_json IS NOT NULL ORDER BY position""", (run_id,),
        )]
        return RunResult(
            results, summarize_results(results, len(run["task_ids"])),
            sum(result.duration_seconds for result in results),
        )
