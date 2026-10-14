import json

import httpx
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from sapbot.agent.llm import ClaudeLLM, OpenAICompatLLM


def test_claude_adapter_roundtrip(monkeypatch):
    llm = ClaudeLLM("claude-sonnet-5-5", api_key="sk-test", exclude_tools={"task_begin"})
    assert all(t["name"] != "task_begin" for t in llm.tools)
    msg = Message(id="m1", type="message", role="assistant", model="claude-sonnet-5-5", stop_reason="tool_use",
                  content=[TextBlock(type="text", text="Look first."),
                           ToolUseBlock(type="tool_use", id="tu1", name="sap_observe_screen",
                                        input={"reasoning": "see screen"})],
                  usage=Usage(input_tokens=10, output_tokens=5))
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        return msg
    monkeypatch.setattr(llm.client.messages, "create", fake_create)
    llm.start("sys", "task")
    t = llm.step()
    assert t.text == "Look first." and t.tool_calls[0].name == "sap_observe_screen"
    llm.add_tool_results([("tu1", "{}", False)])
    assert llm.messages[-1]["content"][0]["type"] == "tool_result"
    assert seen["model"] == "claude-sonnet-5-5" and seen["system"] == "sys"


def test_openai_compat_adapter(monkeypatch):
    llm = OpenAICompatLLM("http://hub:8000/v1", "qwen2.5-14b-instruct", api_key="x")
    payload = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
               "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "sap_act",
                                "arguments": json.dumps({"ok_code": "/nVA03", "reasoning": "r"})}}]}}],
               "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/chat/completions") and json["tools"]
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", fake_post)
    llm.start("sys", "task")
    t = llm.step()
    assert t.tool_calls[0].args["ok_code"] == "/nVA03" and t.input_tokens == 3
