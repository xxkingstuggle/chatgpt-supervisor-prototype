# Recovery and state rules

The task directory under the target project's `.agent-work/chatgpt-supervisor/<task_id>/` is the durable record. `state.json` is the only authority; reports, requests, replies, execution records, verification records and browser observations are immutable evidence files. State writes use a lock, state-version compare-and-swap, a temporary file and `os.replace`.

## Recovery matrix

| phase | recovery action |
| --- | --- |
| `SEND_PENDING` | Reopen the saved `conversation_url`, find the exact request by `request_id`, report hash and full text, then confirm whether it was sent. If uncertain, pause; never resend automatically. |
| `WAITING_REPLY` | Find the confirmed request and its corresponding assistant reply. Continue bounded observation if it is still generating; save a full reply and page evidence only after completion is independently visible. |
| `REPLY_ACCEPTED` | Use the saved immutable reply and hash. Check `goal_revision`; do not substitute a newer regenerated answer. |
| `EXECUTING` | Inspect real files, Git diff, command log and partial effects. Decide whether work is complete, partial, or unknown before continuing. |
| `VERIFYING` | Run only checks needed to establish the actual result, then record verification or prepare a new report. Do not replay changes solely because an execution record is missing. |
| `WAITING_USER` | Preserve the reply and evidence. After the user answers, call the goal-revision command, then prepare a new report. |
| `COMPLETED` | Read-only state; no new execution. |

The CLI refuses illegal phase transitions, stale `state_version` writes, a second executor, mismatched hashes and missing referenced files. An executor lock has no automatic timeout or stale takeover. Release it only with the owning token after the task is paused or complete; an orphaned lock requires an explicit, evidence-based operator decision outside the normal loop.

Network, refresh, timeout and browser errors are handled by reading the same conversation first. Three consecutive checks with the same connection/page error and no new state pause the task. Do not rotate accounts, change a proxy, restart CPA, create another conversation, or pay to bypass a block.

## User changes

`revise-goal` preserves the original goal and all prior work, appends the user answer, increments `goal_revision`, and invalidates unstarted instructions from the older revision. A status query does not increment the revision. New reports must carry the current revision and old accepted replies cannot start new actions.
