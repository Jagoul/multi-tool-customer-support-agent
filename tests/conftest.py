import asyncio
import itertools
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock, BetaUsage
from dotenv import load_dotenv

from support_agent.agent import SupportAgent, TurnResult, connect_mcp_tools
from support_agent.backend import SupportBackend

load_dotenv()

_ids = itertools.count(1)


def text(value: str) -> BetaTextBlock:
    return BetaTextBlock(type="text", text=value)


def tool_use(name: str, **tool_input: Any) -> BetaToolUseBlock:
    return BetaToolUseBlock(type="tool_use", id=f"toolu_{next(_ids):03d}", name=name, input=tool_input)


def reply(stop_reason: str, *content: Any) -> BetaMessage:
    return BetaMessage(
        id=f"msg_{next(_ids):03d}",
        type="message",
        role="assistant",
        model="claude-opus-5",
        content=list(content),
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=BetaUsage(input_tokens=10, output_tokens=10),
    )


class ScriptedClaude:
    """Stands in for AsyncAnthropic: plays back scripted responses and records each request."""

    def __init__(self, *responses: BetaMessage) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **params: Any) -> BetaMessage:
        self.requests.append({**params, "messages": list(params["messages"])})
        return self.responses.pop(0)


def run_turn(claude: ScriptedClaude, message: str) -> TurnResult:
    """One customer turn through the real loop and a real MCP server, with scripted Claude."""

    async def scenario() -> TurnResult:
        async with connect_mcp_tools() as executor:
            return await SupportAgent(claude, executor, executor.tools).send(message)

    return asyncio.run(scenario())


@pytest.fixture
def backend() -> SupportBackend:
    return SupportBackend()
