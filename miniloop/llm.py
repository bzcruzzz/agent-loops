import anthropic
from miniloop.config import ANTHROPIC_API_KEY, MODEL


_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def complete(messages: list, tools: list, system: str) -> tuple[list, list, object]:
    """
    Call the Anthropic Messages API with tool use.
    Returns (text_blocks, tool_use_blocks, usage).
    """
    client = _get_client()

    kwargs = dict(
        model=MODEL,
        max_tokens=8096,
        system=system,
        messages=messages,
    )
    if tools:
        kwargs["tools"] = tools

    response = client.messages.create(**kwargs)

    text_blocks     = [b for b in response.content if b.type == "text"]
    tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

    return text_blocks, tool_use_blocks, response.usage
