# Bash Operations

Five small tasks that test whether an agent can actually use a shell to get things done.

Instead of asking the agent to explain a command, we let it run commands in a workspace and check what it leaves behind.

## 1. What's being tested?

The five tasks cover:

- Creating a file with specific content (`bash-001`)
- Making a directory and three files (`bash-002`)
- Counting lines (`bash-003`)
- Listing `.txt` files (`bash-004`)
- Generating a sequence (`bash-005`)

Everything is defined in `dataset.json`, including task instructions, any starting files, and verification criteria. Tasks use an Alpine Linux environment.

## 2. How grading works

The benchmark checks the files the agent produces.

| Check | What it looks for |
|---|---|
| `file_content` | Exact text match, ignoring surrounding whitespace |
| `file_exists` | All required paths exist; contents aren't checked |
| `file_content_contains` | Every expected string occurs somewhere in the file; order and extra content don't matter |

For example, `bash-001` asks for `hello.txt` containing `Hello World`:

```json
{
  "type": "file_content",
  "file": "hello.txt",
  "expected_content": "Hello World"
}
```

A task receives **1.0 if it passes, and 0.0 if it fails**. The harness reports the fraction of tasks passed.

## 3. Running a task directly

From `benchmarks/bash-operations`, prepare a fresh workspace:

```bash
python setup.py --workspace-dir /tmp/bash-task --task bash-001
```

The agent can now execute shell commands in that workspace. After it finishes, run:

```bash
python evaluate.py --workspace-dir /tmp/bash-task --task bash-001
```

The evaluator returns JSON with the task ID, pass/fail result, score, and check details. Unlike the Python benchmark, it also uses its process exit code: `0` for a pass, `1` for a failure. The harness accounts for this difference.

## 4. Running it through the harness

From the repository root, after completing the [main README's setup](../../README.md):

```bash
RUN_ID=$(uv run harness run \
  --db demo.sqlite3 \
  --agent agents/mini-swe-agent \
  --benchmark bash-operations \
  --replay tests/fixtures/task_responses.json)

uv run harness worker --db demo.sqlite3 --concurrency 2 --drain

uv run harness results --db demo.sqlite3 --run_id "$RUN_ID" --format table
```

This submits all five tasks with saved model responses and no paid API calls. The worker processes all queued tasks in that database. Each task gets its own agent container, while grading happens separately.

See the [main README](../../README.md) for live-model runs and the architecture. The point is pretty simple: did the agent leave the right files behind? The verifier checks the result rather than trusting the agent's explanation.
