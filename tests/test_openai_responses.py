from __future__ import annotations

from types import SimpleNamespace

from coomi.services.llm.openai import _convert_messages, _convert_tools, _parse_response


def test_openai_responses_converts_chat_history_and_tools():
    instructions, response_input = _convert_messages(
        [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "read x"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": '{"path":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ]
    )
    tools = _convert_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    )

    assert instructions == "system prompt"
    assert response_input[-2]["type"] == "function_call"
    assert response_input[-1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "ok",
    }
    assert tools == [
        {
            "type": "function",
            "name": "Read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]


def test_openai_responses_parses_text_tool_calls_and_usage():
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="done")],
            ),
            SimpleNamespace(
                type="function_call",
                call_id="call_2",
                id="item_2",
                name="Write",
                arguments='{"path":"x","content":"ok"}',
            ),
        ],
        output_text="done",
        usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
    )

    parsed = _parse_response(response)

    assert parsed.content == "done"
    assert parsed.tool_calls[0].id == "call_2"
    assert parsed.tool_calls[0].arguments == {"path": "x", "content": "ok"}
    assert parsed.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
    }
