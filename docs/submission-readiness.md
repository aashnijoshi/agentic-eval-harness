# Submission readiness and onsite notes

The root [README](../README.md) is the setup guide and contains the editable
[Mermaid architecture diagram](../README.md#architecture). This document maps
the assignment to the implementation and explains its operating boundaries.

## Requirements and evidence

All mandatory capabilities are implemented. Passing deterministic tasks verifies
the execution and grading infrastructure; it does not predict live model accuracy.
The optional second agent is not implemented.

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Python 3.11+, uv, Docker | `pyproject.toml`, `.python-version`, `uv.lock`, `docker/mini.Dockerfile` | Locked installation, both image builds, CLI help |
| Unified run/status/results CLI | `src/agentic_harness/cli.py`: `run`, `status`, `results` | `tests/test_cli.py`: submission, state transitions, JSON/table/summary |
| Small agent and benchmark contracts | `models.py`: `Agent`, `Benchmark`, `Task`, `AgentResult`, `Grade` | Concrete adapters and parameterized task tests |
| Pinned mini-swe-agent | `agents.py`: `MiniSweAgent`; `mini_runner.py`; Git submodule v1.17.5 | Real mini loop with replay; recorded live bash and Python runs |
| Both supplied benchmarks, all eight tasks | `benchmarks.py`: `BashOperations`, `PythonTasks` | `test_real_mini_passes_every_task` and `test_incorrect_submissions_fail_every_task` |
| Per-task isolated execution | `execution.py`: `Container`, `run_task` | Docker workspace isolation, resource limits, cleanup and timeout tests |
| Trusted preparation and grading | `benchmarks.py`: `prepare`, `evaluate`; separate trusted container | Grader replacement, hidden Python tests, pytest-configuration and symlink tests |
| Structured results and aggregation | `models.py`: result dataclasses, `summarize_results`; `cli.py`: `read_report` | Mixed outcomes, pending measurements, aggregation and grader-validation tests |
| Nonblocking durable submission | `db.py`: `Database.submit_run` | Submission cannot call execution; atomic insert rollback tests |
| Background workers and bounded concurrency | `worker.py`: `run_workers`, `_worker` | Real child-process concurrency, duplicate-coordinator and shutdown tests |
| Atomic claims and persistent results | `db.py`: `claim_next`, `finish_task`, `get_results` | Competing connections, stale-token rejection, close/reopen and CLI restart tests |
| Crash recovery without paid task retry | `worker.py`: `reconcile_worker`, `cleanup_claim` | Killed worker/coordinator, inherited lock, live PID refusal, both Docker ownership labels |
| Credential safety | `execution.py`: `redact`; worker persistence boundary; CLI report sanitation | Raw/masked fake provider keys, historical report immutability, secret-free database checks |
| GitHub repository and documentation | Root README, diagram, this checklist, pinned submodule | Fresh clone of committed state; final Git and tracked-file review |

Source files in the table live under `src/agentic_harness/` unless a different
path is shown. Docker tests require `RUN_DOCKER_TESTS=1`; default pytest skips
those cases rather than claiming they ran.

## Live Python finding

The supplied Python problem says to implement `add()`, while `solution_file`
metadata says where to submit it. The original agent prompt only contained the
problem text. The live run created a correct `add.py`, left `solution.py` as the
stub, and received a valid failing grade from the original grader. Its terminal
state was `completed`, agent status `LimitsExceeded`, score 0.0, ten calls,
89.712 seconds, and reported cost $0.0147626.

`MiniSweAgent.run` now appends missing artifact paths from `Task.artifacts`.
The original problem text remains intact. Complete-path matching avoids
mistaking another filename or a nested path for a required output. Already
explicit paths are not repeated. Only public paths are added; private tests,
expected answers, and grader implementation remain outside the prompt.

The shipped mini configuration already says to issue
`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` without another command. Mini's
`DefaultAgent.has_finished` checks the first output line after leading whitespace
and requires exit status zero. The live agent printed `4` before the marker, then
kept verifying `add.py` until its step limit. That completion failure is agent
behavior; mini's semantics and prompt configuration have not been modified.

Offline Docker tests reproduce this combination and show that agent status and
grade are separate: a limit-exhausted agent can receive a passing grade if it
leaves the correct required artifact. The final prompt clarification has not
been rerun against a paid model. The earlier live bash run passed with two
calls, 25.297 seconds, and reported cost $0.002525. Live results are examples,
not a benchmark-wide accuracy estimate.

## Verify a committed checkout without pushing

From the source repository root, after committing the intended state:

```bash
verification_root=$(mktemp -d)
git clone --no-local . "$verification_root/agentic-eval-harness"
cd "$verification_root/agentic-eval-harness"
git submodule update --init --recursive
uv python install 3.11
uv sync --locked
uv run harness --help

docker pull python@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377
docker build -f docker/mini.Dockerfile -t harness-mini:python .
docker build -f docker/mini.Dockerfile \
  --build-arg BASE_IMAGE=alpine:latest@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a \
  -t harness-mini:alpine .

bash_run=$(uv run harness run --db verification.sqlite3 \
  --agent agents/mini-swe-agent --benchmark bash-operations \
  --replay tests/fixtures/task_responses.json)
python_run=$(uv run harness run --db verification.sqlite3 \
  --agent agents/mini-swe-agent --benchmark python-tasks \
  --replay tests/fixtures/task_responses.json)
uv run harness status --db verification.sqlite3 --run_id "$bash_run"
uv run harness worker --db verification.sqlite3 --concurrency 2 --drain
uv run harness status --db verification.sqlite3 --run_id "$python_run"
uv run harness results --db verification.sqlite3 --run_id "$bash_run" --format summary
uv run harness results --db verification.sqlite3 --run_id "$python_run" --format table
uv run harness results --db verification.sqlite3 --run_id "$python_run" --format json

uv run pytest tests -q
RUN_DOCKER_TESTS=1 uv run pytest tests -q
git status --short
git submodule status
```

This clones committed Git objects and initializes the pinned submodule; it does
not copy a source checkout's uncommitted files, virtual environment, or local
databases. It can reuse the machine's uv/Python caches and Docker layers.
Successful verification on that machine is a clean-checkout test, not evidence
of a cache-free clean-machine install. The README's GitHub clone command becomes
equivalent after the final commit is pushed.

## Two-minute architecture walkthrough

The harness has a control plane and an execution plane. A user submits a run
through the CLI, selecting an agent, benchmark, task IDs, and either a live model
or deterministic replay. Submission validates those choices, inserts a run and
its task rows in one SQLite transaction, and returns a run ID immediately.

A separate worker coordinator starts at most the requested number of child
processes. Each child handles one task. It opens its own SQLite connection and
claims the next queued task inside a short `BEGIN IMMEDIATE` transaction. The
claim records a worker identity and a random token; no database lock is held
while Docker or the model is running.

The child builds the small agent and benchmark adapters and calls `run_task`.
A trusted container prepares the task and retains its grader and private tests.
Only public starting files reach a fresh agent container, where mini-swe-agent
runs its action/observation loop. Live execution forwards the named credential;
replay disables container networking.

After execution, the harness stops the agent and copies only declared artifacts.
It validates paths, links, types, and sizes before grading them in the separate
trusted container. Both containers and temporary files are cleaned up, including
on ordinary failure. The result records infrastructure status, agent status,
grade, duration, and available usage independently.

The worker redacts diagnostics and persists the result only if it still owns
the running claim. Status and results read SQLite after workers exit. If a worker
crashes, recovery verifies that it stopped, reconciles its owned containers, and
records interruption without automatically repeating a potentially paid task.

## Five design decisions and trade-offs

1. **SQLite and short transactions.** Durable atomic claims without another
   service. One writer at a time and a local filesystem fit this take-home;
   this is not a distributed scheduler.
2. **Small protocols and explicit adapters.** Existing benchmark scripts keep
   their scoring rules, and mini has one container entry point. Adding an
   adapter requires explicit registration; there is no plugin framework.
3. **Fresh Docker containers and trusted grading.** Agent-time access to tests
   and graders is prevented. Container startup and host-mediated file copying
   cost time, and running Python submissions in the grader is not adversarially
   secure against hostile code.
4. **Bounded one-task processes and inherited locking.** Process isolation and
   a coordinator lock survive partial crashes. This depends on POSIX fork/flock
   and requires a dedicated single-threaded coordinator.
5. **Ownership checks and no task retries.** State, task ID, worker ID and token
   guard completion; Docker cleanup requires both ownership labels. Interrupted
   work may require a deliberate new submission, and external model calls cannot
   be made exactly-once by a SQLite transaction.

## Five questions to rehearse

1. How does `BEGIN IMMEDIATE` prevent two workers from claiming the same task,
   and why are no model calls made inside that transaction?
2. What happens if the provider charges for a response immediately before the
   worker dies, or if the coordinator dies while a child keeps running?
3. Why check both claim ownership in SQLite and execution/token labels in
   Docker? What happens if a PID is reused or Docker is unavailable?
4. How are private tests protected during agent execution, and what attacks
   remain possible when submitted Python code runs during grading?
5. Why can `LimitsExceeded` coexist with a valid grade, and how would supporting
   partial-credit benchmarks change the current binary-grade validation?

## Failure categories and recovery

- **Infrastructure failure:** setup, Docker, malformed grader output, artifact
  validation, provider exceptions, or cleanup fail. Execution is `error` (or
  `timed_out`/`interrupted` where appropriate); usually no grade exists.
- **Agent failure:** mini cannot finish, for example `LimitsExceeded`. The
  harness can still grade whatever permitted artifacts exist. Provider failures
  are recorded as agent-phase errors rather than fabricated failing grades.
- **Graded failure:** the original grader returns a valid score 0.0. Execution
  is `completed` with `passed=false`; this does not mean the infrastructure broke.

The coordinator's sidecar lock is inherited by its children, so replacing a
killed coordinator cannot silently exceed the old concurrency bound. Recovery
refuses live or reused PIDs, confirms cleanup by both Docker labels, then uses
the same conditional completion operation to record interruption. Stale tokens
cannot overwrite the result. Queued work stays queued; no age-based requeue or
automatic task retry is implemented.

With more time, prioritize a hardened Python evaluation boundary, broader
platform verification, immutable task/configuration snapshots, explicit database
migrations and retention, and more complete provider-usage accounting. These are
future improvements, not implemented capabilities.

## Final handoff

Review `git status` and the final commits, push only after approval, check the
GitHub README and submodule link, and submit the repository URL through Ashby by
the assignment deadline. Keep live SQLite databases, credentials, worker lock
files, caches, logs, and temporary workspaces out of Git and private. No further
paid runs are needed for this submission-readiness review.
