"""Submit durable runs, operate a local worker, and inspect persisted outcomes."""

import json
from pathlib import Path
import sqlite3

import click

from agentic_harness.db import Database
from agentic_harness.worker import run_workers

database_option = click.option(
    "--db", type=click.Path(dir_okay=False, path_type=Path),
    default="harness.sqlite3", show_default=True, help="SQLite database; use the same path for every command",
)


@click.group()
def cli():
    """Run agents against benchmarks using a durable local task queue."""


@cli.command()
@database_option
@click.option("--agent", required=True, help="Agent identifier: agents/mini-swe-agent")
@click.option("--benchmark", required=True, type=click.Choice(["bash-operations", "python-tasks"]))
@click.option("--task_ids", default=None, help="Comma-separated task IDs; omit for all benchmark tasks")
@click.option("--model", default=None, help="Explicit provider/model identifier for a live run")
@click.option("--replay", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Deterministic responses; no paid API calls")
@click.option("--api-key-env", default="OPENAI_API_KEY", show_default=True)
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=60.0, show_default=True)
@click.option("--step-limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--cost-limit", type=click.FloatRange(min=0, min_open=True), default=1.0, show_default=True)
def run(db, agent, benchmark, task_ids, model, replay, api_key_env, timeout, step_limit, cost_limit):
    """Validate and queue a run; print its ID without executing any task."""
    selected = [item.strip() for item in task_ids.split(",")] if task_ids is not None else None
    try:
        with Database(db) as database:
            run_id = database.submit_run(
                agent=agent, benchmark=benchmark, task_ids=selected, model=model, replay=replay,
                api_key_env=api_key_env, timeout=timeout, step_limit=step_limit, cost_limit=cost_limit,
            )
    except (ValueError, OSError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error
    click.echo(run_id)


@cli.command()
@database_option
@click.option("--concurrency", type=click.IntRange(min=1), default=2, show_default=True)
@click.option("--drain", is_flag=True, help="Exit when no queued or active tasks remain")
@click.option("--shutdown-timeout", type=click.FloatRange(min=0, min_open=True), default=10.0, show_default=True)
def worker(db, concurrency, drain, shutdown_timeout):
    """Process queued tasks until interrupted, or until drained with --drain."""
    try:
        run_workers(db, concurrency=concurrency, drain=drain, shutdown_timeout=shutdown_timeout)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error


def read_report(path: Path, run_id: str) -> dict:
    """Read one short SQLite snapshot, then derive every output format from it."""
    with Database(path) as db, db.connection:
        db.connection.execute("BEGIN")
        run = db.get_run(run_id)
        results = db.get_results(run_id)
    by_id = {result.task_id: result for result in results.results}
    tasks = []
    for row in run["tasks"]:
        result = by_id.get(row["task_id"])
        diagnostics = result.diagnostics if result is not None else None
        agent = (diagnostics or {}).get("agent", {})
        tasks.append({
            **row,
            "agent_status": result.agent_status if result is not None else None,
            "passed": result.passed if result is not None else None,
            "score": result.score if result is not None else None,
            "duration_seconds": result.duration_seconds if result is not None else None,
            "model_calls": agent.get("model_calls"), "cost": agent.get("cost"),
            "diagnostics": diagnostics,
        })
    counts = {state: sum(task["state"] == state for task in tasks)
              for state in ("queued", "running", "completed", "error", "timed_out", "interrupted")}
    counts["failed"] = results.summary["failed"]  # Incorrect solutions, not execution errors.
    counts["passed"] = results.summary["passed"]
    if counts["running"]:
        state = "running"
    elif counts["queued"]:
        state = "queued"
    else:
        state = next((name for name in ("error", "timed_out", "interrupted") if counts[name]), "completed")
    finished = sum(counts[name] for name in ("completed", "error", "timed_out", "interrupted"))
    # A partial measured total would understate usage. Keep it null until every
    # selected task has a measurement; available per-task values remain visible.
    usage = {name: sum(task[name] for task in tasks) if all(task[name] is not None for task in tasks) else None
             for name in ("model_calls", "cost")}
    return {
        "run_id": run_id, "agent": run["agent"], "benchmark": run["benchmark"],
        "created_at": run["created_at"], "config": run["config"], "state": state,
        "progress": {"finished": finished, "total": len(tasks), "fraction": finished / len(tasks)},
        "counts": counts, "summary": results.summary, "tasks": tasks, "usage": usage,
        "duration_seconds": results.duration_seconds if results.results else None,
    }


@cli.command()
@database_option
@click.option("--run_id", required=True, help="Run ID returned by harness run")
def status(db, run_id):
    """Return JSON state, counts, progress, and per-task timestamps."""
    try:
        report = read_report(db, run_id)
    except (ValueError, OSError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error
    view = {key: report[key] for key in ("run_id", "agent", "benchmark", "created_at", "state", "progress", "counts")}
    fields = ("task_execution_id", "task_id", "state", "worker_id", "claimed_at", "started_at", "finished_at", "error")
    view["tasks"] = [{key: task[key] for key in fields} for task in report["tasks"]]
    click.echo(json.dumps(view, indent=2, allow_nan=False))


def display(value) -> str:
    if value is None or type(value) is bool:
        return json.dumps(value)
    return str(value)


def print_summary(report: dict) -> None:
    click.echo(f"Run: {report['run_id']}\nState: {report['state']}")
    click.echo(f"Progress: {report['progress']['finished']}/{report['progress']['total']}")
    for key, value in report["summary"].items():
        click.echo(f"{key}: {display(value)}")
    click.echo(f"cumulative_task_seconds: {display(report['duration_seconds'])}")
    for key, value in report["usage"].items():
        click.echo(f"{key}: {display(value)}")


@cli.command()
@database_option
@click.option("--run_id", required=True, help="Run ID returned by harness run")
@click.option("--format", type=click.Choice(["json", "table", "summary"], case_sensitive=False), default="json")
def results(db, run_id, format):
    """Read persisted outcomes, including partial runs, without executing work."""
    try:
        report = read_report(db, run_id)
    except (ValueError, OSError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error
    if format == "json":
        click.echo(json.dumps(report, indent=2, allow_nan=False))
        return
    print_summary(report)
    if format == "summary":
        for task in report["tasks"]:
            click.echo(f"{task['task_id']}: state={task['state']} passed={display(task['passed'])} "
                       f"score={display(task['score'])} seconds={display(task['duration_seconds'])} "
                       f"model_calls={display(task['model_calls'])} cost={display(task['cost'])}")
        return
    keys = ("task_id", "state", "passed", "score", "duration_seconds", "model_calls", "cost")
    rows = [list(keys)] + [[display(task[key]) for key in keys] for task in report["tasks"]]
    widths = [max(len(row[index]) for row in rows) for index in range(len(keys))]
    for row in rows:
        click.echo("  ".join(value.ljust(width) for value, width in zip(row, widths)))


def main():
    cli()


if __name__ == "__main__":
    main()
