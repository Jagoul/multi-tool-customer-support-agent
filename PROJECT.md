# Project Brief: Multi-Tool Customer Support Agent

> The design and implementation are documented in [README.md](README.md).

## Scenario

Building a customer support resolution agent using the Claude Agent SDK. The agent handles
high-ambiguity requests like returns, billing disputes, and account issues. It has access to your
backend systems through custom Model Context Protocol (MCP) tools (`get_customer`, `lookup_order`,
`process_refund`, `escalate_to_human`). Your target is 80%+ first-contact resolution while knowing when
to escalate.


## Objective

Practice designing an agentic loop with tool integration, structured error handling, and
escalation patterns.

## Tasks

1. Define 3-4 MCP tools with detailed descriptions that clearly differentiate each tool's purpose,
   expected inputs, and boundary conditions. Include at least two tools with similar functionality that
   require careful description to avoid selection confusion.
2. Start with the design first, using Mermaid diagrams, and put it in the README. This folder ships to
   GitHub as a standalone project, so the design is the front page of the README, with clear,
   good-looking Mermaid diagrams.
3. Implement an agentic loop that checks `stop_reason` to determine whether to continue tool
   execution or present the final response. Handle both `"tool_use"` and `"end_turn"` stop reasons
   correctly.
4. Add structured error responses to your tools: include `errorCategory`
   (transient/validation/permission), `isRetryable` boolean, and human-readable descriptions. Test that
   the agent handles each error type appropriately (retrying transient errors, explaining business
   errors to the user).
5. Implement a programmatic hook that intercepts tool calls to enforce a business rule (e.g., blocking
   operations above a threshold amount), redirecting to an escalation workflow when triggered.
6. Test with multi-concern messages (e.g., requests involving multiple issues) and verify the agent
   decomposes the request, handles each concern, and synthesizes a unified response.
7. When done, review the README and make sure it explains everything that was built.

