"""Live scenarios against the real Claude API. Run with: uv run pytest -m live"""

import asyncio
import os

import pytest

from support_agent.scenarios import SCENARIOS, run_scenario

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
        reason="needs ANTHROPIC_API_KEY",
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario(scenario):
    report = asyncio.run(run_scenario(scenario))

    failed = [check.description for check, ok in report.outcomes if not ok]
    trace = [
        f"{c.name}({c.input}) -> {c.result.error_code or 'ok'}"
        + (" [blocked by hook]" if c.blocked_by_hook else "")
        for c in report.result.tool_calls
    ]
    assert not failed, f"failed: {failed}\ntrace: {trace}\nreply: {report.result.text}"
    assert report.escalation_correct, f"escalated={report.escalated}\ntrace: {trace}"
