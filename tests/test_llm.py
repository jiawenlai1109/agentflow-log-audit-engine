from pydantic import BaseModel, Field

from agentflow.core.llm import MockLLM, extract_json


class Dummy(BaseModel):
    name: str
    value: int = Field(ge=0)


def test_extract_json_from_fenced_code():
    text = '```json\n{"name": "a", "value": 1}\n```'
    assert extract_json(text) == {"name": "a", "value": 1}


def test_mock_llm_structured():
    llm = MockLLM(overrides={"default": '{"name": "x", "value": 2}'})
    result = llm.complete_structured(system="", messages=[], schema=Dummy)
    assert result.name == "x"
    assert result.value == 2


def test_structured_retry_on_invalid_output():
    calls = {"n": 0}

    class RetryLLM(MockLLM):
        def complete(self, system, messages, temperature=0.2, max_tokens=2000):
            calls["n"] += 1
            if calls["n"] == 1:
                return "这不是 JSON"
            return '{"name": "ok", "value": 3}'

    llm = RetryLLM()
    result = llm.complete_structured(system="", messages=[], schema=Dummy)
    assert result.value == 3
    assert calls["n"] == 2
