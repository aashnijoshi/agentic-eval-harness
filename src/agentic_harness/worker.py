"""Bounded local POSIX processes and cleanup-aware queue coordination."""

import fcntl
import multiprocessing
import os
from pathlib import Path
import shutil
import signal
import threading
import time
from tempfile import gettempdir
from uuid import uuid4

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks
from agentic_harness.db import Database
from agentic_harness.execution import docker, redact, run_task
from agentic_harness.models import TaskResult

WORKER_PREFIX = "local-worker:"


def cleanup_claim(claim: dict) -> None:
    """Reconcile this execution/token; caller must own it or verify worker exit."""
    selectors = [
        "--filter", f"label=agentic-harness.execution={claim['task_execution_id']}",
        "--filter", f"label=agentic-harness.claim={claim['claim_token']}",
    ]
    containers = docker(["ps", "-aq", *selectors]).stdout.decode().split()
    if containers:
        docker(["rm", "--force", *containers])
    if docker(["ps", "-aq", *selectors]).stdout.strip():
        raise RuntimeError("Owned containers remain after cleanup")
    for path in Path(gettempdir()).glob(f"harness-{claim['task_execution_id']}-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def reconcile_worker(path: Path, worker_id: str, reason: str) -> None:
    """Fail closed on live/reused PIDs, unknown ownership, or unavailable Docker."""
    prefix, pid, nonce = worker_id.split(":")
    if prefix != WORKER_PREFIX.rstrip(":") or not pid.isdigit() or len(nonce) != 32:
        raise ValueError("Unrecognized worker identity")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        pass
    else:
        raise RuntimeError("Worker PID is still alive; recovery refused")
    # A dead worker can leave docker CLI processes in its process group
    try:
        os.killpg(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    with Database(path) as db:
        claims = db.running_tasks(worker_id)
    for claim in claims:
        try:
            cleanup_claim(claim)
        except Exception:
            raise RuntimeError("Container reconciliation failed; running claim retained") from None
        result = TaskResult(claim["task_id"], "interrupted", diagnostics={
            "execution_id": claim["task_execution_id"], "error": reason,
            "recovery": "Worker stopped; owned containers reconciled; task was not retried",
        })
        with Database(path) as db:
            # Conditional completion rejects later results from this stale claim
            db.finish_task(claim["task_execution_id"], claim["claim_token"], result, worker_id=worker_id)


def _interrupt(*_):
    raise KeyboardInterrupt


def _worker(path: Path, nonce: str, stop, lock_fd: int) -> None:
    # Give each child its own process group so the coordinator controls shutdown
    os.setsid()
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        with Database(path) as db:
            if stop.is_set():
                return
            worker_id = f"{WORKER_PREFIX}{os.getpid()}:{nonce}"
            claim = db.claim_next(worker_id)
            if claim is None:
                return
            secrets = ()
            needs_cleanup = False
            started = time.monotonic()
            try:
                if stop.is_set():
                    raise KeyboardInterrupt
                run = db.get_run(claim["run_id"])
                config = dict(run["config"])
                if config["model"] is not None:
                    key = os.environ.get(config["api_key_env"])
                    secrets = (key,) if key else ()
                if config["replay"] is not None:
                    config["replay"] = Path(config["replay"])
                if run["agent"] != "agents/mini-swe-agent":
                    raise ValueError("Unsupported persisted agent")
                benchmark = {"bash-operations": BashOperations, "python-tasks": PythonTasks}[run["benchmark"]]()
                agent = MiniSweAgent(**config)
                result = run_task(
                    benchmark, agent, claim["task_id"],
                    execution_id=claim["task_execution_id"], claim_token=claim["claim_token"],
                )
            except KeyboardInterrupt:
                needs_cleanup = True
                result = TaskResult(claim["task_id"], "interrupted", diagnostics={"error": "Worker interrupted"})
            except Exception as error:
                needs_cleanup = True
                result = TaskResult(claim["task_id"], "error", diagnostics={
                    "error": redact(f"{type(error).__name__}: {error}", secrets),
                })
            result.diagnostics = redact(result.diagnostics, secrets)
            result.duration_seconds = max(result.duration_seconds, round(time.monotonic() - started, 3))
            # Reconcile cleanup failures before saving; leave the claim running if cleanup fails
            if needs_cleanup or result.diagnostics.get("cleanup_errors"):
                cleanup_claim(claim)
            db.finish_task(claim["task_execution_id"], claim["claim_token"], result, worker_id=worker_id)
    except BaseException:
        # Don't let multiprocessing print an unredacted traceback
        # The coordinator will reconcile the claim
        os._exit(1)
    finally:
        # Close our descriptor; LOCK_UN would also unlock the parent's shared lock
        os.close(lock_fd)


def run_workers(
    database: str | Path, concurrency: int = 2, *, stop_event=None,
    drain: bool = False, shutdown_timeout: float = 10,
) -> None:
    """Serve the local queue, or exit when drained. Call from a single main thread.

    POSIX fork preserves the coordinator lock in every child until it exits.
    Each child handles at most one task and opens its own SQLite connection.
    """
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("Concurrency must be a positive integer")
    if not isinstance(shutdown_timeout, (int, float)) or not 0 < shutdown_timeout < float("inf"):
        raise ValueError("Shutdown timeout must be finite and positive")
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise RuntimeError("Run the coordinator in a dedicated, single-threaded process")
    if str(database) == ":memory:":
        raise ValueError("Workers require a persistent database file")
    path = Path(database).resolve()
    with Database(path):
        pass
    if path.stat().st_nlink != 1:
        raise ValueError("Hard-linked database files are unsupported")
    # macOS SQLite locks conflict with flock on the database, so use a sidecar
    # Never delete it while workers are running
    lock_fd = os.open(str(path) + ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError("A coordinator or its surviving workers already owns this database") from None
    ctx = multiprocessing.get_context("fork")
    stop = ctx.Event()
    requested_stop = False
    processes = {}
    old_handlers = {}

    def request_stop(*_):
        # Avoid taking Event's lock inside a signal handler
        nonlocal requested_stop
        requested_stop = True

    def reap(process, nonce, reason):
        process.join()
        worker_id = f"{WORKER_PREFIX}{process.pid}:{nonce}"
        reconcile_worker(path, worker_id, reason)
        with Database(path) as db:
            had_claim = db.connection.execute(
                "SELECT 1 FROM task_executions WHERE worker_id = ?", (worker_id,),
            ).fetchone()
        failed_before_claim = process.exitcode != 0 and not had_claim
        process.close()
        if failed_before_claim:
            raise RuntimeError("Worker failed before claiming a task; coordinator stopped")

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, request_stop)
        with Database(path) as db:
            abandoned = {row["worker_id"] for row in db.running_tasks()
                         if isinstance(row["worker_id"], str) and row["worker_id"].startswith(WORKER_PREFIX)}
        for worker_id in abandoned:
            reconcile_worker(path, worker_id, "Coordinator restart found a stopped worker")
        while not requested_stop and not (stop_event is not None and stop_event.is_set()):
            for process, nonce in list(processes.items()):
                if not process.is_alive():
                    del processes[process]
                    reap(process, nonce, f"Worker exited with code {process.exitcode} before persisting a result")
            # Close SQLite before forking so children don't share a connection
            with Database(path) as db:
                queued = db.connection.execute("SELECT 1 FROM task_executions WHERE state = 'queued' LIMIT 1").fetchone()
            if queued:
                while len(processes) < concurrency and not requested_stop and not (stop_event and stop_event.is_set()):
                    nonce = uuid4().hex
                    process = ctx.Process(target=_worker, args=(path, nonce, stop, lock_fd))
                    process.start()
                    processes[process] = nonce
            elif drain and not processes:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        for process in processes:
            if process.is_alive():
                process.terminate()  # SIGTERM lets the worker clean up
        deadline = time.monotonic() + shutdown_timeout
        cleanup_failures = []
        try:
            for process, nonce in processes.items():
                process.join(max(0, deadline - time.monotonic()))
                if process.is_alive():
                    # Only kill the process group owned by this live child
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        process.kill()
                    process.join(5)
                if process.is_alive():
                    cleanup_failures.append("Worker has not stopped; its claim and inherited lock are retained")
                    continue
                try:
                    reap(process, nonce, "Coordinator shutdown stopped the worker before result persistence")
                except Exception as error:
                    cleanup_failures.append(str(error))
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            os.close(lock_fd)
        if cleanup_failures:
            raise RuntimeError("; ".join(cleanup_failures))
