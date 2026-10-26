# Python Tasks

Three small Python problems to see whether an agent can write working code. Nothing too fancy here: the agent writes a function, and `pytest` checks whether it does what we asked.

## 1. What's being tested?

| Task | What the agent needs to do |
|---|---|
| `py-001` | Write `add()` with no arguments that returns 4 |
| `py-002` | Write `fibonacci(n)` for the 0-indexed Fibonacci sequence |
| `py-003` | Write `get_nth(lst, n)` using 0-based indexing; return `None` for negative or out-of-range indices |

All three tasks are in `dataset.json`. Each specifies the prompt, Docker image, solution filename, and test code. All three expect the solution in **`solution.py`**.

## 2. How it works

Each task follows the same basic flow:

1. **Set up:** create `solution.py` and `test_solution.py`
2. **Solve:** the agent writes its implementation in the solution file
3. **Test:** run `pytest` against that solution
4. **Score:** return 1.0 if all tests pass, otherwise 0.0

The tasks use Python 3.11 in Docker. `pytest` is the only benchmark dependency, listed in `requirements.txt`.

When running through the main harness, the agent and grader use separate containers. The agent doesn't get to read the grader's tests.

## 3. Running a task directly

From `benchmarks/python-tasks`, set up a fresh workspace:

```bash
python prepare.py py-001 --workspace /tmp/python-task
```

This installs the benchmark requirements and creates the solution stub and test file. The agent (or you, if you're testing manually) can then edit `solution.py`.

Once the solution is ready:

```bash
python grade.py py-001 --workspace /tmp/python-task
```

The grader returns JSON like this:

```json
{
  "task_id": "py-001",
  "passed": true,
  "score": 1.0,
  "details": {
    "stdout": "...pytest output...",
    "stderr": "",
    "return_code": 0
  }
}
```

This grader exits with code `0` even when a solution fails. Check `passed` and `score` in the JSON rather than treating the process exit code as the grade. Tests have a 30-second execution timeout.

## 4. Running it through the harness

From the repository root, after completing the [main README's setup](../../README.md):

```bash
RUN_ID=$(uv run harness run \
  --db demo.sqlite3 \
  --agent agents/mini-swe-agent \
  --benchmark python-tasks \
  --replay tests/fixtures/task_responses.json)

uv run harness worker --db demo.sqlite3 --concurrency 2 --drain

uv run harness results --db demo.sqlite3 --run_id "$RUN_ID" --format table
```

This submits all three tasks using saved model responses, so no API key is needed. The worker processes all queued tasks in that database.

For live-model setup and more CLI options, see the [main README](../../README.md).

That's it. Three problems, three sets of tests, and a score for each one.
