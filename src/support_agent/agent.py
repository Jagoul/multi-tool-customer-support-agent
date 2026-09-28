"""Manual agentic loop on the Claude Messages API, with tools served by the stdio MCP server."""

import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

from support_agent.errors import ErrorCategory, ToolError
from support_agent.policy import SupportPolicy, blocked_result
from support_agent.prompts import SYSTEM_PROMPT
from support_agent.tools import ToolResult

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ITERATION_CAP_MESSAGE = (
    "I'm sorry, this is taking longer than it should. I've stopped here so I don't make a "
    "mistake on your account. Please reply and I'll pick it up, or ask for a human specialist."
)
REFUSAL_MESSAGE = (
    "I'm sorry, I can't help with that request. If you have a question about an order, a "
    "refund, or your account, I'm happy to help."
)
TRUNCATION_MESSAGE = "I'm sorry, my reply was cut off. Could you send your last message again?"

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class AgentConfig:
    model: str = field(default_factory=lambda: os.environ.get("SUPPORT_AGENT_MODEL", DEFAULT_MODEL))
    max_tokens: int = 16000
    max_iterations: int = 12
    use_fallbacks: bool = True


@dataclass
class ToolCall:
    tool_use_id: str
    name: str
    input: dict[str, Any]
    result: ToolResult
    iteration: int
    blocked_by_hook: bool = False


@dataclass
class TurnResult:
    text: str
    stop_reason: str
    iterations: int
    tool_calls: list[ToolCall]

    def calls_to(self, name: str) -> list[ToolCall]:
        return [call for call in self.tool_calls if call.name == name]

    @property
    def escalation_tickets(self) -> list[dict[str, Any]]:
        tickets = [
            call.result.payload
            for call in self.calls_to("escalate_to_human")
            if not call.result.is_error
        ]
        for call in self.tool_calls:
            escalation = call.result.payload.get("escalation")
            if call.blocked_by_hook and escalation and not escalation.get("isError"):
                tickets.append(escalation)
        return tickets


class ToolExecutor(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...


class McpToolExecutor:
    def __init__(self, session: ClientSession, tools: list[dict[str, Any]]) -> None:
        self.session = session
        self.tools = tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await self.session.call_tool(name, arguments)
        except MCPError as exc:
            return ToolResult.from_error(
                ToolError(
                    ErrorCategory.TRANSIENT,
                    "TOOL_UNAVAILABLE",
                    f"The {name} tool could not be reached ({exc}). Retry the same call.",
                )
            )
        if isinstance(result.structured_content, dict):
            return ToolResult(result.structured_content, result.is_error)
        text = "".join(block.text for block in result.content if block.type == "text")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"text": text}
        return ToolResult(payload, result.is_error)


@asynccontextmanager
async def connect_mcp_tools(
    server: StdioServerParameters | None = None,
) -> AsyncIterator[McpToolExecutor]:
    """Spawn the support MCP server over stdio and expose its tools in Messages API format."""
    server = server or StdioServerParameters(
        command=sys.executable, args=["-m", "support_agent.mcp_server"]
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [
                {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
                for tool in listed.tools
            ]
            yield McpToolExecutor(session, tools)


class SupportAgent:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        executor: ToolExecutor,
        tools: list[dict[str, Any]],
        *,
        config: AgentConfig | None = None,
        policy: SupportPolicy | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self.client = client
        self.executor = executor
        self.tools = tools
        self.config = config or AgentConfig()
        self.policy = policy or SupportPolicy()
        self.on_event = on_event
        self.messages: list[dict[str, Any]] = []

    async def send(self, user_message: str) -> TurnResult:
        """Run one customer turn to completion: loop until Claude stops asking for tools."""
        self.messages.append({"role": "user", "content": user_message})
        self.policy.observe_customer_message(user_message)
        tool_calls: list[ToolCall] = []

        for iteration in range(1, self.config.max_iterations + 1):
            response = await self._create_message()
            self._emit("model_response", iteration=iteration, stop_reason=response.stop_reason)
            if response.content:
                self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                tool_results = await self._run_tool_uses(response, iteration, tool_calls)
                self.messages.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "pause_turn":
                continue

            self._close_unexecuted_tool_uses(response)
            return TurnResult(
                text=_final_text(response),
                stop_reason=response.stop_reason,
                iterations=iteration,
                tool_calls=tool_calls,
            )

        return TurnResult(
            text=ITERATION_CAP_MESSAGE,
            stop_reason="max_iterations",
            iterations=self.config.max_iterations,
            tool_calls=tool_calls,
        )

    async def _create_message(self) -> Any:
        params: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": SYSTEM_PROMPT,
            "tools": self.tools,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},
        }
        if self.config.use_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return await self.client.beta.messages.create(**params)

    async def _run_tool_uses(
        self, response: Any, iteration: int, tool_calls: list[ToolCall]
    ) -> list[dict[str, Any]]:
        """Execute every tool_use block and return ALL results for a single user message."""
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result, blocked = await self._execute_with_policy(block.name, block.input)
            call = ToolCall(block.id, block.name, block.input, result, iteration, blocked)
            tool_calls.append(call)
            self._emit("tool_call", call=call)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result.to_text(),
                    "is_error": result.is_error,
                }
            )
        return tool_results

    async def _execute_with_policy(
        self, name: str, tool_input: dict[str, Any]
    ) -> tuple[ToolResult, bool]:
        decision = self.policy.pre_tool_use(name, tool_input)
        if not decision.allowed:
            escalation = None
            if decision.escalation_request is not None:
                escalation = await self.executor.call_tool(
                    "escalate_to_human", decision.escalation_request
                )
            return blocked_result(decision, escalation), True

        result = await self.executor.call_tool(name, tool_input)
        self.policy.post_tool_use(name, result)
        return result, False

    def _close_unexecuted_tool_uses(self, response: Any) -> None:
        """A turn cut off by max_tokens or refusal can hold tool_use blocks that must not run.

        Every tool_use still needs a tool_result, or the next request is rejected.
        """
        dangling = [block for block in response.content if block.type == "tool_use"]
        if not dangling:
            return
        error = ToolError(
            ErrorCategory.VALIDATION,
            "TOOL_CALL_NOT_EXECUTED",
            f"Not executed: the response ended with stop_reason={response.stop_reason} before "
            "this tool call was complete.",
        )
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(error.to_payload()),
                        "is_error": True,
                    }
                    for block in dangling
                ],
            }
        )

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event is not None:
            self.on_event(kind, data)


def _final_text(response: Any) -> str:
    if response.stop_reason == "refusal":
        return REFUSAL_MESSAGE
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if response.stop_reason == "max_tokens":
        return text or TRUNCATION_MESSAGE
    return text


@asynccontextmanager
async def open_support_agent(
    *,
    client: anthropic.AsyncAnthropic | None = None,
    config: AgentConfig | None = None,
    policy: SupportPolicy | None = None,
    on_event: EventCallback | None = None,
) -> AsyncIterator[SupportAgent]:
    """A ready-to-use agent wired to a fresh MCP server process (fresh backend state)."""
    async with connect_mcp_tools() as executor:
        yield SupportAgent(
            client or anthropic.AsyncAnthropic(),
            executor,
            executor.tools,
            config=config,
            policy=policy,
            on_event=on_event,
        )
