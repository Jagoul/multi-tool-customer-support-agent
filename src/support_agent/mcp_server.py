"""Customer support MCP server (stdio).

Run standalone with `uv run support-mcp-server`, or register it with any MCP client
(Claude Code, Claude Desktop). The low-level `Server` is used instead of `MCPServer` so the
hand-written descriptions, JSON schemas, and annotations go over the wire exactly as defined.
"""

import asyncio
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from support_agent.backend import SupportBackend
from support_agent.tools import TOOL_DEFINITIONS, execute_tool

SERVER_NAME = "customer-support"
SERVER_INSTRUCTIONS = (
    "Customer support backend. Verify the customer with get_customer before acting on their "
    "account, inspect orders with lookup_order, refund with process_refund, and hand off to a "
    "human with escalate_to_human. Errors carry errorCategory and isRetryable."
)

MCP_TOOLS = [
    types.Tool(
        name=definition["name"],
        title=definition["title"],
        description=definition["description"],
        input_schema=definition["input_schema"],
        annotations=types.ToolAnnotations.model_validate(definition["annotations"]),
    )
    for definition in TOOL_DEFINITIONS
]


def build_server(backend: SupportBackend | None = None) -> Server:
    backend = backend or SupportBackend()

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=MCP_TOOLS)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = execute_tool(backend, params.name, params.arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.to_text())],
            structured_content=result.payload,
            is_error=result.is_error,
        )

    return Server(
        SERVER_NAME,
        version="1.0.0",
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
