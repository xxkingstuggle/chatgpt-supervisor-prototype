# Real browser procedure (mcp__cua_repl)

This reference is for Codex, not for `supervisor.py`. The CLI cannot import or call the browser tool. On the first browser call after reset, run exactly `await cua.getState()` and use the returned browser/tab IDs. The acceptance browser used in this prototype is the logged-in Chrome extension surface returned as `name: "Chrome"`; the in-app browser may have no tabs and is not a substitute for the real ChatGPT web session.

## New conversation and send

1. Select the supported Chrome browser from `cua.getState()`, then create a visible tab at `https://chatgpt.com/` or get the already-created tab. If login or CAPTCHA is shown, stop and ask the user to take over; never request or handle credentials.
2. Read the fresh AX state. Locate the new-chat composer and send controls from that state only. Paste the complete immutable request Markdown from the task's `current_request_path`, with no local file link as the only evidence. Re-read AX state after the click.
3. Confirm that the exact request text, including the current `request_id` and `report_sha256`, appears as a user message. Save the full relevant page observation to the evidence file and call `confirm-sent` with the actual conversation URL. `SEND_PENDING` is written before the click, so a click without confirmation is recoverable ambiguity, not permission to resend.

## Matching reply and completion

Re-read AX state after every UI action and derive element indices again. Follow the assistant message immediately associated with the confirmed user message; do not use the last page text, a regenerated answer, or an inserted human message. If the tool exposes a stable message identifier, save it; otherwise preserve the URL, request identity, message order, full request text, full reply text, and page observation.

Accept only when all four facts are visible: the full assistant body is readable, the page shows no active generation or thinking state for that message, no stop/error/interruption state is present, and the reply is associated with the confirmed request. A final marker in text, a temporarily missing stop button, fixed text, or a fixed wait duration alone is insufficient. Save the complete raw reply and the page observation, then call `accept-reply --generation-complete`; that flag records Codex's already-made page judgement and does not prove a browser run by itself.

## Recovery

After refresh, connection failure or timeout, reopen the saved conversation URL and use AX state to find the original request and its associated assistant reply. In `SEND_PENDING`, determine whether the request visibly exists before deciding; if the evidence cannot distinguish sent from unsent, call `pause` and do not resend. In `WAITING_REPLY`, continue bounded observation of the same reply. Three identical connection/page failures without new state are a pause condition. Do not create a second conversation, extract cookies, use an undocumented chat endpoint, change proxies, restart CPA or rotate accounts.

The task evidence must distinguish `real-browser` from `simulated`. A test-generated `generation_complete=true` or a fake HTML page can test local state logic only; it cannot be reported as real web acceptance.
