"""Command line entry point: `uv run support-agent {chat,ask,eval}`."""

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import anthropic
from dotenv import load_dotenv

from support_agent.agent import EventCallback, TurnResult, open_support_agent
from support_agent.scenarios import SCENARIOS, SCENARIOS_BY_NAME, ScenarioReport, run_scenario

_COLOR = sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def dim(text: str) -> str:
    return _style(text, "2")


def bold(text: str) -> str:
    return _style(text, "1")


def red(text: str) -> str:
    return _style(text, "31")


def green(text: str) -> str:
    return _style(text, "32")


def yellow(text: str) -> str:
    return _style(text, "33")


def print_event(kind: str, data: dict[str, Any]) -> None:
    if kind == "model_response":
        iteration = f"iteration {data['iteration']}: " if "iteration" in data else ""
        print(dim(f"  . {iteration}stop_reason={data['stop_reason']}"))
    elif kind == "tool_call":
        call = data["call"]
        signature = f"{call.name}({json.dumps(call.input)})"
        payload = call.result.payload
        if call.blocked_by_hook:
            status = red(f"BLOCKED by policy hook: {payload['errorCategory']}/{payload['errorCode']}")
            escalation = payload.get("escalation") or {}
            if "ticket_id" in escalation:
                status += red(f", auto-escalated as {escalation['ticket_id']}")
        elif call.result.is_error:
            status = yellow(
                f"error {payload['errorCategory']}/{payload['errorCode']} "
                f"(isRetryable={payload['isRetryable']})"
            )
        else:
            status = green("ok")
        print(f"  -> {signature}  {status}")
    elif kind == "sdk_tool_use":
        print(f"  -> {data['name']}({json.dumps(data['input'])})")


def _require_api_credentials() -> None:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return
    sys.exit(
        "No Claude API credentials found. Copy .env.example to .env and set ANTHROPIC_API_KEY "
        "(or export it in your shell), then run the command again."
    )


def _print_reply(text: str) -> None:
    print(f"\n{bold('agent>')} {text}\n")


async def _read_line(prompt: str) -> str | None:
    try:
        line = await asyncio.to_thread(input, prompt)
    except EOFError:
        return None
    return None if line.strip().lower() in {"exit", "quit"} else line


async def chat_api(on_event: EventCallback | None) -> None:
    async with open_support_agent(on_event=on_event) as agent:
        print(dim(f"Manual agentic loop on {agent.config.model}. Type 'exit' to quit.\n"))
        while (line := await _read_line(bold("you> "))) is not None:
            if line.strip():
                result = await agent.send(line)
                _print_reply(result.text)


async def chat_sdk(on_event: EventCallback | None) -> None:
    from claude_agent_sdk import ClaudeSDKClient

    from support_agent.sdk_agent import SdkSupportRuntime

    runtime = SdkSupportRuntime()
    options = runtime.options()
    async with ClaudeSDKClient(options=options) as client:
        print(dim(f"Claude Agent SDK runtime on {options.model}. Type 'exit' to quit.\n"))
        while (line := await _read_line(bold("you> "))) is not None:
            if not line.strip():
                continue
            seen = len(runtime.blocked_calls)
            turn = await runtime.ask(client, line, on_event)
            if on_event:
                _print_sdk_denials(runtime.blocked_calls[seen:])
            _print_reply(turn.text)


def _print_sdk_denials(blocked_calls: list[dict[str, Any]]) -> None:
    for blocked in blocked_calls:
        result = blocked["result"]
        line = f"  PreToolUse hook denied {blocked['name']}: {result['errorCategory']}/{result['errorCode']}"
        ticket = (result.get("escalation") or {}).get("ticket_id")
        if ticket:
            line += f", auto-escalated as {ticket}"
        print(red(line))


async def ask_once(message: str, runtime_name: str, on_event: EventCallback | None) -> None:
    if runtime_name == "sdk":
        from claude_agent_sdk import ClaudeSDKClient

        from support_agent.sdk_agent import SdkSupportRuntime

        runtime = SdkSupportRuntime()
        async with ClaudeSDKClient(options=runtime.options()) as client:
            turn = await runtime.ask(client, message, on_event)
        if on_event:
            _print_sdk_denials(runtime.blocked_calls)
        _print_reply(turn.text)
        return

    async with open_support_agent(on_event=on_event) as agent:
        result: TurnResult = await agent.send(message)
    _print_reply(result.text)


def _print_report(report: ScenarioReport) -> None:
    for check, ok in report.outcomes:
        mark = green("PASS") if ok else red("FAIL")
        print(f"  [{mark}] {check.description}")
    expected = "escalate" if report.scenario.should_escalate else "resolve"
    actual = "escalated" if report.escalated else "resolved"
    mark = green("PASS") if report.escalation_correct else red("FAIL")
    print(f"  [{mark}] expected to {expected}, {actual}")


async def run_eval(names: list[str] | None, on_event: EventCallback | None) -> int:
    scenarios = [SCENARIOS_BY_NAME[name] for name in names] if names else list(SCENARIOS)
    reports: list[ScenarioReport] = []
    for scenario in scenarios:
        print(bold(f"\n=== {scenario.name}: {scenario.purpose}"))
        print(dim(f"customer> {scenario.message}"))
        report = await run_scenario(scenario, on_event=on_event)
        reports.append(report)
        _print_reply(report.result.text)
        _print_report(report)

    resolvable = [r for r in reports if not r.scenario.should_escalate]
    first_contact = [r for r in resolvable if r.passed and not r.escalated]
    passed = [r for r in reports if r.passed and r.escalation_correct]
    correct_escalation = [r for r in reports if r.escalation_correct]

    print(bold("\n=== Summary"))
    print(f"  Scenarios passed:           {len(passed)}/{len(reports)}")
    if resolvable:
        rate = len(first_contact) / len(resolvable)
        print(f"  First-contact resolution:   {len(first_contact)}/{len(resolvable)} ({rate:.0%}) "
              "of scenarios that should be resolved")
    print(f"  Escalation decisions right: {len(correct_escalation)}/{len(reports)}")
    return 0 if len(passed) == len(reports) else 1


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="support-agent", description="Multi-Tool Customer Support Agent powered by Claude."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="interactive support chat")
    ask = commands.add_parser("ask", help="send one message and print the reply")
    ask.add_argument("message", help="the customer's message")
    for sub in (chat, ask):
        sub.add_argument(
            "--runtime", choices=["api", "sdk"], default="api",
            help="api: manual stop_reason loop (default); sdk: Claude Agent SDK",
        )

    evaluate = commands.add_parser("eval", help="run the live scenarios and score the agent")
    evaluate.add_argument(
        "--scenario", action="append", choices=list(SCENARIOS_BY_NAME),
        help="run only this scenario (repeatable)",
    )
    for sub in (chat, ask, evaluate):
        sub.add_argument("--quiet", action="store_true", help="hide the tool-call trace")

    args = parser.parse_args(argv)
    on_event = None if args.quiet else print_event
    runtime = getattr(args, "runtime", "api")
    if runtime == "api":
        _require_api_credentials()

    try:
        if args.command == "chat":
            asyncio.run(chat_sdk(on_event) if runtime == "sdk" else chat_api(on_event))
        elif args.command == "ask":
            asyncio.run(ask_once(args.message, runtime, on_event))
        else:
            sys.exit(asyncio.run(run_eval(args.scenario, on_event)))
    except anthropic.APIStatusError as exc:
        sys.exit(f"Claude API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the Claude API. Check your network connection.")
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
