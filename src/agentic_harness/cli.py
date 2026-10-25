"""CLI for the agentic harness system."""

import json
from dataclasses import asdict
from pathlib import Path

import click

from agentic_harness.agents import MiniSweAgent
from agentic_harness.benchmarks import BashOperations, PROJECT_ROOT
from agentic_harness.execution import run_task


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
    help="Name of the benchmark to run (e.g., swebench)",
)
@click.option(
    "--task_ids",
    default=None,
    help="Task IDs (this implementation step requires bash-001)",
)
@click.option("--model", default=None, help="Explicit provider/model name for a live run")
@click.option("--replay", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON responses for mini's deterministic model; no API calls")
@click.option("--api-key-env", default="OPENAI_API_KEY", show_default=True)
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True), default=60.0, show_default=True)
@click.option("--step-limit", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--cost-limit", type=click.FloatRange(min=0, min_open=True), default=1.0, show_default=True)
def run(agent, benchmark, task_ids, model, replay, api_key_env, timeout, step_limit, cost_limit):
    """Execute and grade bash-001 synchronously, returning JSON."""
    if Path(agent).resolve() != PROJECT_ROOT / "agents/mini-swe-agent":
        raise click.BadParameter("Only agents/mini-swe-agent is supported", param_hint="--agent")
    if benchmark != "bash-operations":
        raise click.BadParameter("Only bash-operations is supported", param_hint="--benchmark")
    if not task_ids or task_ids.strip() != "bash-001":
        raise click.BadParameter("Specify bash-001 for this implementation step", param_hint="--task_ids")
    try:
        adapter = MiniSweAgent(
            model=model, replay=replay, api_key_env=api_key_env,
            timeout=timeout, step_limit=step_limit, cost_limit=cost_limit,
        )
        result = run_task(BashOperations(), adapter, "bash-001")
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(asdict(result), indent=2, allow_nan=False))
    if result.execution_status != "completed":
        raise click.exceptions.Exit(2)
    if not result.passed:
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
