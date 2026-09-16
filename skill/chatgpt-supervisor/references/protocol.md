# ChatGPT Supervisor protocol v1

The local CLI is the authority for `task_id`, `round_id`, `goal_revision`, `request_id`, and `report_sha256`. The web model must copy these values exactly; it must not invent or ask the user to fill them in.

## Request

Every request is an immutable UTF-8 Markdown file. The first request contains the user's original goal, current goal revision and constraints, acceptance criteria, a read-only project audit, relevant files or excerpts, commands and real results, unverified items, and the roles of the user, ChatGPT web, and Codex. Later requests contain the previous instruction's execution result, actual changes, commands and exit codes, failures, unverified items, current Git/field state, and the decision needed from ChatGPT. Only decision-relevant evidence is sent; secrets, cookies, tokens, passwords and unrelated private data are removed.

## Reply

The reply must have exactly one metadata fence at the beginning, then non-empty Markdown body, then the end marker as the final non-whitespace line:

```supervisor-meta
{"protocol_version":1,"task_id":"...","round_id":1,"goal_revision":1,"request_id":"...","report_sha256":"...","status":"CONTINUE"}
```

`protocol_version` and `round_id`/`goal_revision` are integers; all identity and hash fields are non-empty strings. `status` is exactly one of `CONTINUE`, `TASK_COMPLETE`, or `NEED_USER`. The body must describe the bounded objective, facts, steps, evidence and verification for `CONTINUE`; map each user acceptance item to evidence and list remaining limits for `TASK_COMPLETE`; or ask one necessary user question and preserve current work for `NEED_USER`.

The parser checks the expected task, round, revision, request, report hash and status. It rejects duplicate metadata, invalid types, missing or truncated JSON, empty body, missing final marker, stale identity, and status words found only in ordinary body text. A reply is not accepted merely because it contains the marker: the browser observation must also prove generation ended, no error/interruption is shown, and the full body was read.

## Local phase semantics

`INITIALIZED → REPORT_READY → SEND_PENDING → WAITING_REPLY → REPLY_ACCEPTED → EXECUTING → VERIFYING → REPORT_READY` is the normal loop. `SEND_PENDING` means the message may already have been sent; recovery must inspect the same conversation before any resend. `TASK_COMPLETE` enters completion verification and is marked complete only after local evidence review. `NEED_USER`, communication failures, login loss, quota, and unresolved send/reply ambiguity are distinct paused states.
