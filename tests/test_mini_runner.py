"""Exercise real LiteLLM error handling without contacting a model provider."""

import importlib
import io
import json
import runpy
import socket

import pytest

from agentic_harness.benchmarks import PROJECT_ROOT


@pytest.mark.parametrize("status_code,error_type", [
    (401, "AuthenticationError"),
    (404, "NotFoundError"),
])
def test_provider_error_preserves_runner_json(
    status_code, error_type, monkeypatch, capsys, caplog,
):
    def forbid_network(*_, **__):
        pytest.fail("This regression must never contact a model provider")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setenv("MSWEA_SILENT_STARTUP", "1")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-dummy-key")
    runner = runpy.run_path(str(PROJECT_ROOT / "src/agentic_harness/mini_runner.py"))
    import litellm
    from litellm.llms.openai.common_utils import OpenAIError

    # Keep mini and LiteLLM error mapping real; mock only the provider request
    calls = []

    def reject(**kwargs):
        calls.append(kwargs["model"])
        raise OpenAIError(status_code=status_code, message="Offline mocked provider rejection")

    provider = importlib.import_module("litellm.main").openai_chat_completions
    monkeypatch.setattr(provider, "completion", reject)
    monkeypatch.setattr(litellm, "suppress_debug_info", False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "task": "Offline diagnostic", "model": "openai/gpt-5-mini", "responses": None,
        "step_limit": 10, "cost_limit": 0.25,
    })))
    capsys.readouterr()
    runner["main"]()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert calls == ["gpt-5-mini"]
    assert result["status"] == "error"
    assert result["error"].startswith(f"{error_type}:")
    assert "Offline mocked provider rejection" in result["error"]
    assert result["model_calls"] == 0
    assert result["cost"] == 0.0
    assert len(result["messages"]) == 2
    assert "Give Feedback" not in captured.out + captured.err
    assert "offline-dummy-key" not in captured.out + captured.err + caplog.text
