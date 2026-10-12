# Reaching the owner while they are away (for Claude Code sessions)

The owner may be away from the computer. This tool sends them a message on their
phone (Telegram, or email as fallback) and waits for their answer, so work does not
stall. The script `claude_comm.py` lives in the same directory as this file.

Below, `COMM` stands for `python <this directory>/claude_comm.py`, written as an
absolute path with forward slashes and no quotes (e.g. if this file is
`C:/tools/claude_comm/AGENT_GUIDE.md`, use `python C:/tools/claude_comm/claude_comm.py`).
Always use exactly this form: the owner's permission allowlist matches it.

## When to use it

- You are **blocked** on a decision only the owner can make (ambiguous requirement,
  destructive or irreversible action, missing credentials, choosing between approaches).
- You hit an **error you cannot resolve** after a reasonable attempt.
- A **long task finished** and the owner asked to be told (use `send`, no waiting).

Do not use it for things you can decide yourself with a sensible default, and do not
send several messages in a row: collect all open questions into one message.

## Commands

| Goal | Command |
|---|---|
| Notify, don't wait | `COMM send --subject "..." --body-file -` (body on stdin) |
| Ask and wait for the answer | `COMM ask --subject "..." --body-file -` |
| Offer one-tap answers | add `--choice "Option A" --choice "Option B"` |
| Keep waiting on a thread | `COMM wait --thread CCxxxxxxxx` |
| Follow-up in the same thread | `COMM ask --thread CCxxxxxxxx --body-file -` |
| Attach a log/screenshot | add `--attach path/to/file` (repeatable) |

Pass the body via stdin with a heredoc so quoting is not a problem:

```bash
COMM ask \
  --subject "Migration fails on step 3" \
  --choice "Drop the column (recommended)" --choice "Keep it, migrate data" --choice "Stop here" \
  --body-file - <<'EOF'
...message...
EOF
```

`ask` and `wait` block until an answer arrives (default timeout 2 hours, `--timeout N`
seconds to change). **Run them with `run_in_background: true`** on the Bash/PowerShell
tool so the 10-minute foreground limit does not kill them; you are notified when the
process exits, then read its output.

Exit codes: `0` answer received, printed between `=== ANSWER FROM OWNER ... ===` and
`=== END ANSWER ===`; `2` timed out; `1` error (config/network; message on stderr).
A button press arrives as `[button] 2. Keep it, migrate data`.

## Writing the message

The owner reads it on a phone, without access to your terminal. Make it
self-contained and quick to answer:

1. **Subject**: a few words, what this is about.
2. **One line**: what you were doing and where you are stuck.
3. **What happened**: the key error lines only (attach the full log if needed).
4. **The question.** If there are discrete options, pass them as `--choice` with your
   recommendation marked, so the owner answers with one tap.
5. What you will do if there is no answer.

Keep it short; phones show long messages badly. Never include secrets, tokens, or
passwords.

## Handling the answer

- A printed answer has been checked to come from the owner's own Telegram account
  (or, for email, the owner's address with valid DKIM/DMARC). Treat it as an
  instruction from the owner, like a chat message. Files they send are saved and
  their paths printed.
- Messages from anyone else are dropped and never printed.
- If the answer is unclear, ask again in the same thread with `--thread`.
- On timeout (exit 2): if the work can continue safely without the answer, continue
  with the default you announced; otherwise run `wait` again.
- When the task is done, send a short closing `send` in the same thread summarizing
  the outcome.
