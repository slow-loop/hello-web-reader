from __future__ import annotations

import pytest

from web_reader.transcribe import pipeline, refine


def test_refine_returns_empty_for_empty_input(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-not-used")
    assert refine.refine("") == ""
    assert refine.refine("   \n  ") == "   \n  "


def test_refine_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        refine.refine("some text")


def test_refine_calls_openrouter_with_defaults(monkeypatch):
    captured: dict[str, object] = {}

    class DummyMessage:
        content = "  polished output  "

    class DummyChoice:
        message = DummyMessage()

    class DummyResponse:
        choices = [DummyChoice()]

    class DummyChatCompletions:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return DummyResponse()

    class DummyChat:
        completions = DummyChatCompletions()

    class DummyClient:
        chat = DummyChat()

        def __init__(self, *, base_url=None, api_key=None):
            captured["base_url"] = base_url
            captured["api_key"] = api_key

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(refine, "OpenAI", DummyClient)

    out = refine.refine("raw text")

    assert out == "polished output"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["api_key"] == "router-key"

    kwargs = captured["kwargs"]
    assert kwargs["model"] == refine.DEFAULT_MODEL
    assert kwargs["temperature"] == 0.2
    messages = kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "NEVER answer questions" in messages[0]["content"]
    assert "<raw_transcript>" in messages[1]["content"]
    assert "raw text" in messages[1]["content"]
    assert "<domain_context>" not in messages[1]["content"]


def test_refine_honors_model_env_and_context(monkeypatch):
    captured: dict[str, object] = {}

    class DummyMessage:
        content = "ok"

    class DummyChoice:
        message = DummyMessage()

    class DummyResponse:
        choices = [DummyChoice()]

    class DummyChatCompletions:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return DummyResponse()

    class DummyChat:
        completions = DummyChatCompletions()

    class DummyClient:
        chat = DummyChat()

        def __init__(self, *, base_url=None, api_key=None):
            pass

    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    monkeypatch.setattr(refine, "OpenAI", DummyClient)

    refine.refine("hello", context="Mandarin finance podcast")

    kwargs = captured["kwargs"]
    assert kwargs["model"] == "anthropic/claude-haiku-4.5"
    user_content = kwargs["messages"][1]["content"]
    assert "<domain_context>\nMandarin finance podcast\n</domain_context>" in user_content
    assert "<raw_transcript>\nhello\n</raw_transcript>" in user_content


def test_pipeline_skips_refine_when_asr_empty(monkeypatch):
    monkeypatch.setattr(pipeline.asr, "transcribe", lambda path: "")

    def fail_refine(*args, **kwargs):
        raise AssertionError("refine should not be called on empty ASR output")

    monkeypatch.setattr(pipeline.refine, "refine", fail_refine)

    assert pipeline.transcribe_audio("/tmp/foo.m4a") == ""


def test_pipeline_forwards_context(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(pipeline.asr, "transcribe", lambda path: "raw transcript")

    def fake_refine(raw, context=None):
        captured["raw"] = raw
        captured["context"] = context
        return "polished"

    monkeypatch.setattr(pipeline.refine, "refine", fake_refine)

    out = pipeline.transcribe_audio("/tmp/foo.m4a", context="finance")

    assert out == "polished"
    assert captured == {"raw": "raw transcript", "context": "finance"}
