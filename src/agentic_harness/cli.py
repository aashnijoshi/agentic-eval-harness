"""CLI for the agentic harness system."""

import json
from dataclasses import asdict
from pathlib import Path

import click

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PythonTasks, PROJECT_ROOT
from agentic_harness.execution import run_benchmark


@click.group()
def cli():
    """Agentic Harness"""
    pass


@cli.command()
@click.option(
    "--agent",
    required=True,
    help="Path to the agent directory or configuration file",
)
@click.option(
    "--benchmark",
    required=True,
    type=click.Choice(["bash-operations", "python-tasks"]),
    help="Benchmark to execute",
)
@click.option(
    "--task_ids",
    default=None,
    help="Comma-separated task IDs; omit to run all tasks in the benchmark",
)
@click.option("--model", default=None, help="Explicit provider/model name for a live run")
@click.option("--replay", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON responses for mini's deterministic model; no API calls")
@click.option("--api-key-env", default="OPENAI_API_KEY", show_default=True)
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=60.0, show_default=True)
@click.option("--step-limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--cost-limit", type=click.FloatRange(min=0, min_open=True), default=1.0, show_default=True)
def run(agent, benchmark, task_ids, model, replay, api_key_env, timeout, step_limit, cost_limit):
    """Execute selected benchmark tasks sequentially, returning results and summary."""
    if Path(agent).resolve() != PROJECT_ROOT / "agents/mini-swe-agent":
        raise click.BadParameter("Only agents/mini-swe-agent is supported", param_hint="--agent")
    try:
        benchmark_adapter = BashOperations() if benchmark == "bash-operations" else PythonTasks()
        selected = [item.strip() for item in task_ids.split(",")] if task_ids is not None else None
        adapter = MiniSweAgent(
            model=model, replay=replay, api_key_env=api_key_env,
            timeout=timeout, step_limit=step_limit, cost_limit=cost_limit,
        )
        result = run_benchmark(benchmark_adapter, adapter, selected)
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(asdict(result), indent=2, allow_nan=False))
    if any(task.execution_status != "completed" for task in result.results):
        raise click.exceptions.Exit(2)
    if any(not task.passed for task in result.results):
        raise click.exceptions.Exit(1)


@cli.command()
@click.option(
    "--run_id",
    required=True,
    help="The unique identifier for the benchmark run",
)
def status(run_id: str):
    """Check the status of a benchmark run."""
    click.echo(f"Checking status for run: {run_id}")

    # TODO: Implement status checking logic

    click.echo("Status: Not implemented yet")


@cli.command()
@click.option(
    "--run_id",
    required=True,
    help="The unique identifier for the benchmark run",
)
@click.option(
    "--format",
    type=click.Choice(["json", "table", "summary"], case_sensitive=False),
    default="json",
    help="Output format for results (default: json)",
)
def results(run_id: str, format: str):
    """Get results from a completed run."""
    click.echo(f"Retrieving results for run: {run_id}")
    click.echo(f"Format: {format}")

    # TODO: Implement results retrieval logic

    click.echo("Results: Not implemented yet")


def main():
    """Main entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
