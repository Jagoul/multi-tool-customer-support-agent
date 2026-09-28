"""The same agent on the Claude Agent SDK.

The SDK (the Claude Code harness) runs the agentic loop. The support tools are served by an
in-process SDK MCP server, and the business policy runs as a `PreToolUse` hook that denies the
call before it executes.
"""

import os
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from support_agent.agent import DEFAULT_MODEL, EventCallback
from support_agent.backend import SupportBackend
from support_agent.policy import SupportPolicy, blocked_result
from support_agent.prompts import SYSTEM_PROMPT
from support_agent.tools import TOOL_DEFINITIONS, TOOL_NAMES, ToolResult, execute_tool

SERVER_NAME = "support"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"


def qualified(tool_name: str) -> str:
    return f"{TOOL_PREFIX}{tool_name}"


@dataclass
class SdkTurn:
    text: str
    tool_calls: list[dict[str, Any]]
    stop_reasons: list[str]
    result: ResultMessage | None


@dataclass
class SdkSupportRuntime:
    """Backend and policy shared by the in-process MCP tools and the hooks."""

    backend: SupportBackend = field(default_factory=SupportBackend)
    policy: SupportPolicy = field(default_factory=SupportPolicy)
    blocked_calls: list[dict[str, Any]] = field(default_factory=list)

    def run_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        result = execute_tool(self.backend, name, arguments)
        self.policy.post_tool_use(name, result)
        return result

    def build_mcp_server(self) -> Any:
        return create_sdk_mcp_server(
            name=SERVER_NAME,
            version="1.0.0",
            tools=[self._sdk_tool(definition) for definition in TOOL_DEFINITIONS],
        )

    def _sdk_tool(self, definition: dict[str, Any]) -> Any:
        name = definition["name"]

        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            result = self.run_tool(name, arguments)
            return {
                "content": [{"type": "text", "text": result.to_text()}],
                "is_error": result.is_error,
            }

        return tool(name, definition["description"], definition["input_schema"])(handler)

    async def pre_tool_use_hook(
        self, input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        name = input_data["tool_name"].removeprefix(TOOL_PREFIX)
        decision = self.policy.pre_tool_use(name, input_data["tool_input"])
        if decision.allowed:
            return {}

        escalation = None
        if decision.escalation_request is not None:
            escalation = execute_tool(self.backend, "escalate_to_human", decision.escalation_request)
        result = blocked_result(decision, escalation)
        self.blocked_calls.append(
            {"tool_use_id": tool_use_id, "name": name, "input": input_data["tool_input"],
             "result": result.payload}
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": result.to_text(),
            }
        }

    def options(self, model: str | None = None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT,
            model=model or os.environ.get("SUPPORT_AGENT_MODEL", DEFAULT_MODEL),
            mcp_servers={SERVER_NAME: self.build_mcp_server()},
            tools=[],
            allowed_tools=[qualified(name) for name in TOOL_NAMES],
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=f"{qualified('get_customer')}|{qualified('process_refund')}",
                        hooks=[self.pre_tool_use_hook],
                    )
                ]
            },
            permission_mode="dontAsk",
            setting_sources=[],
            strict_mcp_config=True,
            max_turns=15,
        )

    async def ask(
        self, client: ClaudeSDKClient, message: str, on_event: EventCallback | None = None
    ) -> SdkTurn:
        """Send one customer message; the SDK loops internally until the turn ends."""
        self.policy.observe_customer_message(message)
        return await _collect_turn(client, message, on_event)


async def _collect_turn(
    client: ClaudeSDKClient, message: str, on_event: EventCallback | None
) -> SdkTurn:
    await client.query(message)
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    stop_reasons: list[str] = []
    result: ResultMessage | None = None

    async for message_event in client.receive_response():
        if isinstance(message_event, AssistantMessage):
            if message_event.stop_reason:
                stop_reasons.append(message_event.stop_reason)
                if on_event:
                    on_event("model_response", {"stop_reason": message_event.stop_reason})
            for block in message_event.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    call = {"name": block.name.removeprefix(TOOL_PREFIX), "input": block.input}
                    tool_calls.append(call)
                    if on_event:
                        on_event("sdk_tool_use", call)
        elif isinstance(message_event, ResultMessage):
            result = message_event

    final_text = (result.result if result and result.result else "\n".join(texts)).strip()
    return SdkTurn(final_text, tool_calls, stop_reasons, result)
