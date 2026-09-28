"""The stdio MCP server, tested through a real MCP client session."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client

from support_agent.tools import TOOLS_BY_NAME

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "support_agent.mcp_server"])


async def with_session(action):
    async with stdio_client(SERVER) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await action(session)


def test_lists_tools_with_descriptions_schemas_and_annotations():
    async def action(session):
        return (await session.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(with_session(action))}

    assert set(tools) == set(TOOLS_BY_NAME)
    for name, tool in tools.items():
        assert tool.description == TOOLS_BY_NAME[name]["description"]
        assert tool.input_schema == TOOLS_BY_NAME[name]["input_schema"]
    assert tools["lookup_order"].annotations.read_only_hint is True
    assert tools["process_refund"].annotations.destructive_hint is True
    assert tools["process_refund"].annotations.idempotent_hint is False


def test_tool_errors_travel_as_structured_mcp_errors():
    async def action(session):
        return await session.call_tool("lookup_order", {"order_id": "ORD-1004"})

    result = asyncio.run(with_session(action))

    assert result.is_error is True
    assert result.structured_content["errorCategory"] == "transient"
    assert result.structured_content["isRetryable"] is True
    assert json.loads(result.content[0].text) == result.structured_content


def test_successful_call_returns_structured_content():
    async def action(session):
        return await session.call_tool("get_customer", {"email": "bob@example.com"})

    result = asyncio.run(with_session(action))

    assert result.is_error is False
    assert result.structured_content["customer_id"] == "CUST-1002"
