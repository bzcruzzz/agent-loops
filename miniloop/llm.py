"""
LLM client — calls any OpenAI-compatible /chat/completions endpoint.

Configure via .env:
    API_BASE_URL     endpoint base (default: Bob API)
    API_KEY          your key
    API_AUTH_SCHEME  "apikey" or "Bearer" (default: apikey)
    MINILOOP_MODEL   model name
"""
import json
import requests
from miniloop.config import API_BASE_URL, API_KEY, API_AUTH_SCHEME, MODEL


def complete(messages: list, tools: list, system: str) -> tuple[list, list, object]:
    """
    Call an OpenAI-compatible chat completions endpoint.
    Returns (text_blocks, tool_use_blocks, usage).
    """
    if not API_KEY:
        raise RuntimeError(
            "No API key set.\n"
            "Add to .env:\n"
            "  API_BASE_URL=<endpoint>   e.g. https://api.us-east.bob.ibm.com/inference/v1\n"
            "  API_KEY=<your key>\n"
            "  MINILOOP_MODEL=<model name>\n"
            "  API_AUTH_SCHEME=apikey    (or Bearer)"
        )

    payload = {
        "model": MODEL,
        "max_tokens": 8096,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    if tools:
        payload["tools"] = _convert_tools(tools)
        payload["tool_choice"] = "auto"

    resp = requests.post(
        f"{API_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"{API_AUTH_SCHEME} {API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )

    if not resp.ok:
        raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:500]}")

    data    = resp.json()
    message = data["choices"][0]["message"]
    usage   = data.get("usage", {})

    text_blocks, tool_use_blocks = [], []

    content = message.get("content") or ""
    if content:
        text_blocks.append(_TextBlock(content))

    for tc in message.get("tool_calls") or []:
        fn = tc["function"]
        try:
            parsed = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            parsed = {}
        tool_use_blocks.append(_ToolUseBlock(
            id=tc.get("id", ""),
            name=fn["name"],
            input=parsed,
        ))

    return text_blocks, tool_use_blocks, _Usage(
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


def _convert_tools(tools: list) -> list:
    """Anthropic tool schema → OpenAI function-calling schema."""
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
        },
    } for t in tools]


class _TextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _ToolUseBlock:
    def __init__(self, id: str, name: str, input: dict):
        self.type  = "tool_use"
        self.id    = id
        self.name  = name
        self.input = input


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens  = input_tokens
        self.output_tokens = output_tokens
