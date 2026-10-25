# Agentic Harness Take-Home

A system for running AI agents against benchmark tasks.

## Quick Start

### Installation

Use a Git checkout so `make init` can restore the submodule. If you have the
original starter ZIP without `.git` and with an empty `agents/mini-swe-agent/`,
run these commands once from the project root:

```bash
git init -b main
git clone --depth 1 --branch v1.17.5 https://github.com/SWE-agent/mini-swe-agent.git agents/mini-swe-agent
git submodule add https://github.com/SWE-agent/mini-swe-agent.git agents/mini-swe-agent
git submodule absorbgitdirs
```

The recovered setup pins mini-swe-agent v1.17.5 at
`68d367bf0ccf40ad59a07101a94c21b66e7798ff` through the Git submodule entry.
The ZIP did not preserve the original revision; this compatible v1 release
supports Python 3.11 and the flags used by the examples. Commit the submodule
entry along with the project files when preparing the GitHub submission.

```bash
make init       # Initialize git submodules (mini-swe-agent)
make install    # Install dependencies
source .venv/bin/activate
```

### Docker runtime prerequisite check

These commands build and check the agent runtime only. They do not run a
benchmark, grade a solution, or call a model API. Run them from the project root
with Docker running and the submodule initialized:

```bash
docker build --progress=plain -f docker/mini.Dockerfile -t harness-mini:python .
docker build --progress=plain -f docker/mini.Dockerfile --build-arg BASE_IMAGE=alpine:latest@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a -t harness-mini:alpine .

docker run --rm --network none harness-mini:python mini --help
docker run --rm --network none harness-mini:alpine mini --help
docker run --rm --network none harness-mini:python python /opt/runtime/smoke_test.py
docker run --rm --network none harness-mini:alpine python /opt/runtime/smoke_test.py
```

The default base is a digest-pinned `python:3.11-slim`. The Alpine build uses a
digest-pinned snapshot of the benchmark's `alpine:latest`, adding Python and Bash
through `apk`; it keeps Alpine and musl. Both builds use uv 0.12.5 pinned by digest
and the existing `uv.lock`. The Dockerfile-specific ignore file excludes the
host environment, credentials, Git metadata, harness source, and benchmark data
from the build context.

The smoke test uses mini's own `DefaultAgent`, `LocalEnvironment`, and
`DeterministicModel`: three scripted responses create a file with Bash, check it
with Python, and submit. It also imports the real LiteLLM adapter without calling
it. Networking is disabled, no credentials are injected, and `--rm` removes the
containers after each check. The workspace is temporary container storage.

Verified on Linux ARM64: Python 3.11.17 in the slim runtime and Python 3.14.8 on
Alpine 3.24.3. Both reported `Submitted`, three model calls, and zero cost. These
are scripted model calls, not requests to an API. The Alpine native dependencies
also imported successfully without installing a compiler toolchain.

The base images and Python dependencies are pinned, but Alpine's `apk`
repositories can publish newer system packages. Preserve a built image by its
image ID/digest when an exact runtime snapshot is required. Other CPU
architectures and live model-provider calls have not been tested.

### Execute benchmarks synchronously in Docker

All five `bash-operations` tasks and all three `python-tasks` are supported.
The runtime is selected from each task's declared `docker_image`: Alpine tasks
use `harness-mini:alpine`, and Python tasks use `harness-mini:python`.
These commands use mini's built-in deterministic model and need no API key:

```bash
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-001 --replay tests/fixtures/bash_001_responses.json

# Multiple selected tasks, in the requested order.
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --task_ids bash-002,bash-003 --replay tests/fixtures/task_responses.json

# Omit task IDs to execute the entire benchmark sequentially.
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --replay tests/fixtures/task_responses.json
uv run harness run --agent agents/mini-swe-agent --benchmark python-tasks --replay tests/fixtures/task_responses.json
```

Every response has the same shape, including single-task runs: `results` is a
list of per-task records, `summary` aggregates those records, and
`duration_seconds` measures the whole run. This replaces the earlier flat
single-task JSON response. Each task retains `task_id`, `execution_status`,
`agent_status`, `passed`, `score`, `duration_seconds`, and diagnostics. Diagnostics
include the dataset-loaded instructions, mini's messages/model-call count,
artifact previews/hashes, grader details, image IDs, and failure phase/error.

`summary.accuracy` is passed / selected tasks, so infrastructure errors do not
silently disappear from the denominator. `graded_accuracy` is passed / tasks
with a valid grade, or null when none were graded. The summary also counts
completed, graded, passed, failed, errored, timed-out, and interrupted tasks.
Scores remain the benchmarks' original binary scores. Unknown, empty, or
duplicate task selections are rejected before any task starts. A failed grade,
agent error, or timeout does not stop later selected tasks. Ctrl-C interrupts
the current task and marks remaining tasks interrupted without starting them.

Replay files may contain a response list (reused independently for each task)
or a mapping from task IDs to response lists. The all-task fixture uses the
mapping form. These are scripted responses to the real mini agent; they do not
bypass its loop or the grader. No solutions or task IDs are embedded in harness
production code. Each fixture task is expected to submit and score 1.0.

Execution proceeds in two separate containers:

1. A fresh trusted Python container receives the benchmark's scripts and dataset.
   Bash uses the digest-pinned Python base. Python grading reuses a separate
   instance of the already-built Python runtime, which supplies locked pytest.
   No package installation or networking is needed during execution.
   Runtime inspection uses the canonical `python@sha256:…` reference reported
   in Docker's `RepoDigests`. On Docker Desktop 29.9.2, the equivalent
   `python:3.11-slim@sha256:…` lookup intermittently returned "No such image"
   while the pinned image was present. The digest is unchanged; there is no
   automatic pull, delay, or retry. The daemon's intermittent alias behavior
   remains unexplained.
   Python's preparation script adds a backward-compatible `--skip-install`
   option; default standalone installation behavior is unchanged.
2. Only those initial workspace files and the public task prompt go into an
   isolated mini container using the declared image family. Bash setup files are
   preserved. Python preparation creates tests and a solution stub in the trusted
   container, but copies only the stub to the agent workspace. The grader,
   dataset, and Python tests are never copied into the agent container. Replay
   runs have networking disabled. Each task gets new containers and directories.
   Copy-in archives normalize ownership to container UID/GID 0 so initial files
   remain editable with all container capabilities dropped.
3. After mini finishes, its container is stopped before the declared artifact is
   copied out. Tar paths and file types are checked; links and special files are
   rejected. Nested artifact paths are preserved. Artifacts are limited to 1 MiB
   and 100 entries across the submission. Python accepts only its declared
   solution file, excluding agent-created tests and pytest configuration files.
4. The original evaluator runs against the copied submission in the trusted
   container's clean submission directory. Python tests are copied there from
   trusted preparation immediately before grading. Both benchmarks' JSON and
   exit-code conventions are preserved: Python's grader exits zero even for
   failed solutions. A wrong solution is a completed execution with a failed
   grade, not an infrastructure error. The supplied grader code is unchanged.
5. Context managers remove both containers and host temporary directories on
   success, exceptions, timeouts, and keyboard interruption. No volumes or host
   repository mounts are used. Cleanup errors are reported explicitly.

Exit codes are 0 when all selected tasks pass, 1 when at least one grade fails,
and 2 when any task has an execution error, timeout, or interruption.
Configuration errors also return a nonzero exit code. If a task obtained no
valid grade, its `passed` and `score` are null.

```bash
# Unit/contract tests; Docker is not required.
uv run pytest tests/test_execution.py -q

# Real Docker execution, still entirely offline and free of model API calls.
RUN_DOCKER_TESTS=1 uv run pytest tests/test_docker_execution.py -q

# Both suites; integration tests are skipped unless explicitly enabled.
uv run pytest tests -q
```

A live model path is also available, but has not been tested or called during
development. It requires `--model PROVIDER/MODEL` instead of `--replay` and a
provider key already in the environment. Use `--api-key-env ANTHROPIC_API_KEY`
for an Anthropic key, for example; the default is `OPENAI_API_KEY`. Only the named
key is forwarded, by variable name rather than its value in command arguments.
Known key values are redacted from returned diagnostics. Live runs enable the
agent container's outbound network access. The default limits are 60 seconds,
20 model calls, and an estimated $1 model cost; they can be changed with
`--timeout`, `--step-limit`, and `--cost-limit`. Cost checks happen between calls,
so one call can exceed the configured cost threshold.

The CLI remains synchronous; background workers and the
`status`/`results` commands are not implemented. Images must already be available locally; execution
does not build or pull them. Full logs/artifacts are not retained on disk; the
result contains messages, artifact previews, and grader diagnostics. Docker
daemon failure or an uncatchable process kill can prevent immediate cleanup;
containers carry an `agentic-harness.execution` label for identification.

Known benchmark limitations are preserved: `bash-003` has five logical lines but
no trailing newline (`wc -l` alone returns four), and bash substring/existence
checks remain as supplied. Python grading executes submitted Python code in an
isolated container; withholding test files prevents editing them during agent
execution, but the provided pytest grader is not a hardened checker against
malicious code that manipulates pytest from inside its own Python process.

### SQLite persistence and durable queue (Python API)

`agentic_harness.db.Database` adds persistence independently of the CLI and
Docker execution. It uses standard-library SQLite with two tables:

| Table | Fields and purpose |
| --- | --- |
| `runs` | UUID `run_id`; canonical `agent` and `benchmark` identifiers; `task_ids_json` preserving the selected task list; `config_json` containing explicit execution settings; UTC `created_at` for submission order. |
| `task_executions` | UUID `task_execution_id`; foreign-key `run_id`; `task_id` and selection `position`; execution `state`; `worker_id` and unique per-claim `claim_token`; UTC `claimed_at`, `started_at`, and `finished_at`; serialized `TaskResult` in `result_json`; optional diagnostic `error`. |

The schema enforces valid states and unique `(run_id, task_id)` and
`(run_id, position)` pairs. A state index supports queued-task selection; the
unique indexes also support retrieving a run's tasks. Every connection enables
foreign keys. Initialization is idempotent. Use one `Database` instance per
caller/thread, with a context manager or explicit `close()`.

Submission validates the benchmark's real task definitions, the supported agent
and image families, a provider/model identifier or valid replay file, and the
execution limits. It inserts the run and every queued task in one transaction;
any insert failure rolls back the whole submission. Model submission does not
require credentials to be present yet. Configuration stores only `model`,
`replay` (an absolute file path), `api_key_env` (a variable name), `timeout`,
`step_limit`, and `cost_limit`. It never reads or stores environment values or
serializes agent objects. Arbitrary configuration fields are rejected.

For example, from the project root, this uses a temporary database and does not
run Docker or call a model. The successful `TaskResult` is illustrative; a
worker will eventually obtain it from the existing `run_task()` function.

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from agentic_harness.db import Database
from agentic_harness.models import TaskResult

with TemporaryDirectory() as directory, Database(Path(directory) / "queue.sqlite3") as db:
    run_id = db.submit_run(
        agent="agents/mini-swe-agent",
        benchmark="bash-operations",
        task_ids=["bash-001", "bash-002"],
        replay=Path("tests/fixtures/task_responses.json"),
    )
    claim = db.claim_next("worker-example")
    result = TaskResult("bash-001", "completed", "Submitted", True, 1.0)
    db.finish_task(
        claim["task_execution_id"], claim["claim_token"], result,
        worker_id=claim["worker_id"],
    )
    print([(task["task_id"], task["state"]) for task in db.get_run(run_id)["tasks"]])
    # [('bash-001', 'completed'), ('bash-002', 'queued')]
    print(db.get_results(run_id).summary)
    # total=2, completed=1, graded=1, passed=1, failed=0,
    # errors=0, timed_out=0, interrupted=0, accuracy=0.5, graded_accuracy=1.0
```

`claim_next(worker_id)` uses `BEGIN IMMEDIATE` before selecting the oldest
queued run and its first selected task. SQLite serializes writers, so a second
claimer waits for the first transaction to commit, then sees the task as
running. An empty queue returns `None`. A fresh token is generated per claim,
and claim/start times are initially equal. The transaction commits before the
claim is returned: Docker execution must happen outside a database transaction.
Lock contention waits up to five seconds, then raises a SQLite error to the
caller without silently retrying work.

`finish_task()` conditionally updates only a running row matching the execution
ID, task ID, worker ID, and claim token. Invalid results, mismatched ownership,
stale tokens, and duplicate completion are rejected. Final states are
`completed`, `error`, `timed_out`, and `interrupted`. An incorrect solution is
`completed` with `passed=False` and score `0.0`. A grade obtained before a later
cleanup error is retained, matching synchronous execution. Results from the
execution layer already redact its known API key values; callers must not add
secrets to arbitrary result diagnostics.

`get_run()` returns configuration and ordered task states/timestamps without
exposing claim tokens. `get_results()` returns a `RunResult` with finished
results in selection order and the same summary calculation as synchronous
execution. Queued/running tasks have no fabricated result and do not count as
failed solutions; they remain in the overall accuracy denominator. Graded
accuracy includes only available grades and is null when none exist. Database
`duration_seconds` is the sum of persisted task durations, not queue waiting
time or a run's wall-clock duration.

Crash/recovery policy for Chunk 2B: committed claims survive process exit as
`running`, with their owner, token, and timestamps intact. Claim age alone never
requeues work, and there is no retry, lease expiry, heartbeat, or recovery daemon.
The worker must associate Docker container labels with the durable execution ID
before launch, handle normal completion/interruption, and recover orphaned work
only after confirming its worker has stopped and its containers are cleaned up.
Any explicit recovery must compare the current state/token and revoke ownership
atomically before recording an interrupted/error outcome, so a late worker cannot
overwrite it. No recovery or requeue API is exposed in this chunk.

The queue assumes one local SQLite file and the same checkout/replay files are
available to the future worker. It records configuration references, not copies
of datasets, source, or replay contents; preserve those files for reproducibility.
The existing ignore rules exclude SQLite files and their journal sidecars.
There are no migrations or database/CLI integration yet.

```bash
uv run pytest tests/test_db.py -q
RUN_DOCKER_TESTS=1 uv run pytest tests -q
```

### Configuring mini-swe-agent

Run the setup wizard to configure your default model and API key:

```bash
mini-extra config setup
```

Read more about how to configure mini-swe-agent: https://mini-swe-agent.com/latest/usage/config/

### Run Examples

```bash
make bash-example
make py-example
```

These commands demonstrate the full workflow: setup → run agent → evaluate.

## Project Structure

```
.
├── benchmarks/
│   ├── bash-operations/      # Bash command benchmark
│   │   ├── dataset.json      # 5 bash tasks
│   │   ├── setup.py          # Setup workspace
│   │   ├── evaluate.py       # Evaluate results
│   │   └── README.md
│   └── python-tasks/         # Python coding benchmark
│       ├── dataset.json      # 3 Python tasks
│       ├── prepare.py        # Setup workspace (different name!)
│       ├── grade.py          # Evaluate results (different name!)
│       └── README.md
├── agents/
│   └── mini-swe-agent/       # Example agent (git submodule)
├── examples/                 # Example scripts
├── src/
│   └── agentic_harness/      # Your harness implementation
└── Makefile
```

## Workflow

Each benchmark follows a three-step process:

1. **Setup**: Prepare workspace with initial files
2. **Execute**: Run agent to solve the task
3. **Evaluate**: Check if task was completed correctly

## Benchmarks

### bash-operations
- 5 simple bash tasks
- Uses `alpine:latest` Docker image
- CLI: `--workspace-dir <path> --task <id>`
- Exit codes: 0 for pass, 1 for fail

[Full documentation](benchmarks/bash-operations/README.md)

### python-tasks
- 3 simple Python tasks
- Uses `python:3.11-slim` Docker image
- CLI: `<task_id> --workspace <path>` (positional argument!)
- Exit code: Always 0 (check JSON for pass/fail)

[Full documentation](benchmarks/python-tasks/README.md)

## mini-swe-agent

Important flags:
- `--exit-immediately`: Required to prevent hanging
- `-y/--yolo`: Skip action confirmations
- `-t/--task`: Task description

For more information, run `mini --help` or read the [mini-swe-agent quickstart](https://mini-swe-agent.com/latest/quickstart/).

## Notes

- Benchmarks have intentionally different interfaces to test flexibility
- Tasks specify `docker_image` field for container execution
- Each benchmark has its own evaluation logic
