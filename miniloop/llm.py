"""
LLM client — supports two backends, auto-selected from .env:

  1. LiteLLM proxy  (when LITELLM_API_KEY is set)
     model="openai/chat"  api_base=https://pgx.blum.coffee/v1
     Must be on IBM network / VPN.

  2. Raw OpenAI-compatible endpoint  (any other case)
     Configure via API_BASE_URL / API_KEY / API_AUTH_SCHEME / MINILOOP_MODEL.
     Works with Bob API, Anthropic, OpenAI, Ollama, etc.

Both paths return the same (text_blocks, tool_use_blocks, usage) tuple.
"""
import json
import os
import re
import uuid
import requests

from miniloop.config import (
    API_BASE_URL, API_KEY, API_AUTH_SCHEME, MODEL,
    LITELLM_API_KEY, LITELLM_BASE_URL, LITELLM_MODEL,
)

# Populated on first call to _convert_tools(); used by the text fallback parser.
_KNOWN_TOOLS: set[str] = set()


def complete(messages: list, tools: list, system: str) -> tuple[list, list, object]:
    """
    Call the configured LLM backend.
    Returns (text_blocks, tool_use_blocks, usage).

    text_blocks     — list of objects with .text
    tool_use_blocks — list of objects with .id, .name, .input
    usage           — object with .input_tokens, .output_tokens
    """
    if LITELLM_API_KEY:
        return _complete_litellm(messages, tools, system)
    return _complete_openai(messages, tools, system)


# ── Backend 1: LiteLLM proxy ──────────────────────────────────────────────────

def _complete_litellm(messages: list, tools: list, system: str) -> tuple[list, list, object]:
    try:
        import litellm
        litellm.drop_params = True
    except ImportError:
        raise RuntimeError(
            "litellm not installed. Run: pip install litellm"
        )

    oai_tools = _convert_tools(tools) if tools else []

    kwargs = dict(
        model=LITELLM_MODEL,
        api_key=LITELLM_API_KEY,
        api_base=LITELLM_BASE_URL,
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=8096,
        temperature=0,
    )
    if oai_tools:
        kwargs["tools"]       = oai_tools
        kwargs["tool_choice"] = "auto"

    resp = litellm.completion(**kwargs)

    message = resp.choices[0].message
    usage   = resp.usage or _Usage(0, 0)

    text_blocks:     list = []
    tool_use_blocks: list = []
    content: str = message.content or ""

    # Native tool calls
    native = getattr(message, "tool_calls", None) or []
    for tc in native:
        fn = tc.function
        try:
            parsed = json.loads(fn.arguments or "{}")
        except json.JSONDecodeError:
            parsed = {}
        tool_use_blocks.append(_ToolUseBlock(
            id=tc.id or str(uuid.uuid4()),
            name=fn.name,
            input=parsed,
        ))

    # Text fallback
    if not native and content and _KNOWN_TOOLS:
        parsed_calls, remaining = _parse_text_tool_calls(content)
        tool_use_blocks.extend(parsed_calls)
        content = remaining

    if content.strip():
        text_blocks.append(_TextBlock(content))

    return text_blocks, tool_use_blocks, _Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0),
        output_tokens=getattr(usage, "completion_tokens", 0),
    )


# ── Backend 2: raw OpenAI-compatible requests ─────────────────────────────────

def _complete_openai(messages: list, tools: list, system: str) -> tuple[list, list, object]:
    if not API_KEY:
        raise RuntimeError(
            "No API key set.\n"
            "For the LiteLLM proxy add to .env:\n"
            "  LITELLM_API_KEY=sk-6vSzPoi24PyJA6UjGWfiUg\n"
            "  LITELLM_BASE_URL=https://pgx.blum.coffee/v1\n"
            "  LITELLM_MODEL=openai/chat\n\n"
            "For a direct endpoint add:\n"
            "  API_BASE_URL=<endpoint>\n"
            "  API_KEY=<your key>\n"
            "  MINILOOP_MODEL=<model name>\n"
            "  API_AUTH_SCHEME=apikey    (or Bearer)"
        )

    payload: dict = {
        "model": MODEL,
        "max_tokens": 8096,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    if tools:
        payload["tools"]       = _convert_tools(tools)
        payload["tool_choice"] = "auto"

    resp = requests.post(
        f"{API_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"{API_AUTH_SCHEME} {API_KEY}",
            "Content-Type":  "application/json",
        },
        json=payload,
        timeout=120,
    )

    if not resp.ok:
        raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:500]}")

    data    = resp.json()
    message = data["choices"][0]["message"]
    usage   = data.get("usage", {})

    text_blocks:     list = []
    tool_use_blocks: list = []
    content: str = message.get("content") or ""

    # Native tool calls
    native = message.get("tool_calls") or []
    for tc in native:
        fn = tc["function"]
        try:
            parsed = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            parsed = {}
        tool_use_blocks.append(_ToolUseBlock(
            id=tc.get("id") or str(uuid.uuid4()),
            name=fn["name"],
            input=parsed,
        ))

    # Text fallback
    if not native and content and _KNOWN_TOOLS:
        parsed_calls, remaining = _parse_text_tool_calls(content)
        tool_use_blocks.extend(parsed_calls)
        content = remaining

    if content.strip():
        text_blocks.append(_TextBlock(content))

    return text_blocks, tool_use_blocks, _Usage(
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


# ── shared helpers ────────────────────────────────────────────────────────────

def _convert_tools(tools: list) -> list:
    """
    Convert Anthropic-style tool schemas → OpenAI function-calling schema.
    Also registers each tool name in _KNOWN_TOOLS for the text fallback.
    """
    result = []
    for t in tools:
        _KNOWN_TOOLS.add(t["name"])
        result.append({
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def _parse_text_tool_calls(text: str) -> tuple[list, str]:
    """
    Fallback parser: find JSON objects in raw text that look like tool calls.
    Handles {"name": "bash", "arguments": {...}} and {"name": "read_file", "input": {...}}.
    """
    calls:     list = []
    remaining: str  = text

    pattern = re.compile(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', re.DOTALL)

    for match in pattern.finditer(text):
        raw = match.group(0)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        name = obj.get("name")
        if name not in _KNOWN_TOOLS:
            continue

        args = obj.get("arguments") or obj.get("input") or obj.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}

        calls.append(_ToolUseBlock(id=str(uuid.uuid4()), name=name, input=args))
        remaining = remaining.replace(raw, "", 1).strip()

    return calls, remaining


# ── value objects ─────────────────────────────────────────────────────────────

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
