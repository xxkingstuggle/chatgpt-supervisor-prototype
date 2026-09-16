---
name: chatgpt-supervisor
description: Use a real ChatGPT web conversation as the planning loop for a Codex task, while Codex audits local evidence, executes bounded work, verifies results, and persists recoverable state. Use when a user asks for a long-running goal to be supervised through ChatGPT web.
---

# ChatGPT Supervisor

Use this skill when the user wants a single long-term project goal coordinated through a real ChatGPT web conversation. Codex remains responsible for local truth, permissions, execution, tests, and evidence. The web conversation supplies the next bounded instruction or a completion judgement; it never grants permissions the user did not grant.

The workflow is persistent and one-goal-at-a-time:

`inspect goal and project → prepare report → send through the real browser → confirm the exact user message → wait for the matching complete assistant reply → validate and fix its protocol metadata → execute only bounded instructions → verify the real diff and tests → save a new report`

Start a new goal with the natural-language goal and this skill. Continue or inspect an existing goal from its project-local `.agent-work/chatgpt-supervisor/` state. User corrections increment `goal_revision` and invalidate not-yet-started instructions from the old revision. Paused, ambiguous, login, quota, and browser failures remain explicit local states; do not turn them into a fake `NEED_USER` or completion.

Use `scripts/supervisor.py --help` for the CLI. It owns task IDs, round IDs, request IDs, immutable report/reply files, atomic state writes, executor ownership, protocol validation, and recovery checks. Do not edit state JSON by hand. The CLI can persist browser observations, but `mcp__cua_repl` is a Codex tool, not a Python import or a `browser.send()` API.

Read the supporting references only for the operation at hand:

- [protocol.md](references/protocol.md): canonical request/reply/report fields and parser rules.
- [browser.md](references/browser.md): real `mcp__cua_repl` operations, completion evidence, association, and recovery.
- [recovery.md](references/recovery.md): phase transitions, interruption handling, user revisions, pause, and continue.

The templates in `assets/` are starting materials, not authority over the user's latest request. Never send credentials, cookies, unrelated private files, or unredacted logs. Simulated browser observations and replies are valid only for offline tests and must remain labelled `simulated`; they do not prove the real web loop.
