"""Server-side tools are refused with a usable error, not a 500.

Bedrock's Converse API executes only client tools: a `toolSpec` with a name and an
input schema that the caller invokes. Anthropic's server-side tools — web search
first among them — carry a versioned `type` and no input schema, because Anthropic
runs them. Translating one produced `inputSchema: {json: {}}`, Bedrock rejected
that, and the caller received a bare `500 Internal Server Error`.

The status code is the whole point: a 500 tells the user "the gateway is broken,
retry later", when the truth is "this route cannot do that, turn it off".
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from mvp.anthropic import _reject_server_side_tools


def test_web_search_is_refused_by_name_with_a_remedy():
    with pytest.raises(HTTPException) as exc:
        _reject_server_side_tools(
            [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        )
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert detail["type"] == "unsupported_tool"
    assert detail["tools"] == ["web_search_20250305"]
    # The message has to say what to do, since nothing about the request is fixable
    # server-side.
    assert "WebSearch" in detail["message"]


@pytest.mark.parametrize(
    "tool_type",
    [
        "web_search_20250305",
        "web_fetch_20250910",
        "code_execution_20250522",
        "computer_20250124",
        "bash_20250124",
        "text_editor_20250728",
    ],
)
def test_every_server_side_family_is_refused(tool_type: str):
    with pytest.raises(HTTPException):
        _reject_server_side_tools([{"type": tool_type, "name": "t"}])


def test_client_tools_pass_through_untouched():
    """The common case must stay untouched: agents send dozens of client tools."""
    tools = [
        {"name": "Read", "description": "read a file", "input_schema": {"type": "object"}},
        {"name": "Bash", "description": "run a command", "input_schema": {"type": "object"}},
        # A client tool that also carries a `type` is still a client tool: it has a
        # schema, so it translates cleanly.
        {
            "type": "custom",
            "name": "Grep",
            "description": "search",
            "input_schema": {"type": "object"},
        },
    ]
    _reject_server_side_tools(tools)


def test_unknown_tool_shapes_are_not_rejected():
    """Only the known server-side families are refused.

    Rejecting anything unfamiliar would break callers whose tools Bedrock happens
    to accept, and the failure would be ours rather than the provider's.
    """
    _reject_server_side_tools([{"type": "something_new_20991231", "name": "x"}])
    _reject_server_side_tools(["not-a-dict", None])
