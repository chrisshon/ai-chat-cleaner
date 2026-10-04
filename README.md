# AI Chat Cleaner

Review possible insults in **Claude Code, Codex, ChatGPT and Claude** history with one CLI and companion skill. The cleanup options differ:

| History | What this tool does |
| --- | --- |
| Local Claude Code and Codex | Redact selected human message text from supported local records after exact approval. |
| ChatGPT and regular Claude exports | Read supported downloaded history, suggest messages to review, and prepare a manual checklist for deleting whole online conversations. It changes neither the export nor your account. |

Discovery uses conservative hints, not an AI classifier/API. The assistant skill reviews context. Preserve useful corrections, quotes, praise and mixed requests. Discovery can miss insults; nothing is automatically deleted.

## Install

Use **Python 3.10+ on macOS/Linux**. Check `python3 --version`; some Macs ship Python 3.9, so substitute an installed `python3.12` or `python3.13`. Runtime is standard-library only; installation uses setuptools and Git.

Install the CLI in a virtual environment and the portable skill for Codex:

```bash
python3 -m venv "$HOME/ai-chat-cleaner-venv"
"$HOME/ai-chat-cleaner-venv/bin/python" -m pip install --no-deps \
  'git+https://github.com/chrisshon/ai-chat-cleaner.git'
mkdir -p "$HOME/.agents/skills/ai-chat-cleaner"
curl -fsSL https://raw.githubusercontent.com/chrisshon/ai-chat-cleaner/main/skills/ai-chat-cleaner/SKILL.md \
  -o "$HOME/.agents/skills/ai-chat-cleaner/SKILL.md"
"$HOME/ai-chat-cleaner-venv/bin/ai-chat-cleaner" --version
export PATH="$HOME/ai-chat-cleaner-venv/bin:$PATH"
```

For Claude Code, use `~/.claude/skills/ai-chat-cleaner/SKILL.md` instead. Install the same skill in both hosts if desired. Give it the CLI's absolute path or put the executable on `PATH`. The skill runs inside Codex or Claude Code and requires the companion CLI; it does not install into the ChatGPT or regular Claude chat app. Windows is unsupported.

Alternatively, clone into a new directory and install locally:

```bash
git clone https://github.com/chrisshon/ai-chat-cleaner.git
cd ai-chat-cleaner
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
```

## ChatGPT and regular Claude: review an export

Request your own export in [ChatGPT Settings > Data controls](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data), or [Claude Settings > Privacy](https://support.claude.com/en/articles/9450526-export-your-claude-data). Export availability depends on your account. Downloaded history is sensitive; keep it private.

Use the extracted `conversations.json`, or a ZIP containing exactly one root `conversations.json`. Input and JSON payloads are limited to 64 MiB; archives also have member, expanded-size and compression limits. For a large media archive, extract just the JSON yourself and use it if within the limit. Only the known JSON layouts described below are accepted. This is read-only and does not require closing the apps.

```bash
mkdir -m 700 "$HOME/ai-chat-cleaner-review"
ai-chat-cleaner review-export --provider chatgpt \
  --input /absolute/path/conversations.json \
  --report "$HOME/ai-chat-cleaner-review/chatgpt-review.json"
```

Use `--provider claude` for a regular Claude export. Add `--preview` only if you accept showing private messages and their context in the terminal or reviewing assistant. Saved reports omit message text and titles. Hints can miss insults; `--all` includes all supported human text for deliberate contextual review.

After reviewing the conversation and keeping useful corrections, choose returned message IDs:

```bash
ai-chat-cleaner deletion-guide --provider chatgpt \
  --input /absolute/path/conversations.json --select MESSAGE_ID \
  --out "$HOME/ai-chat-cleaner-review/chatgpt-guide.json"
```

The guide groups selections by conversation and shows how many other messages would be lost. It is a checklist, not an executable deletion plan or proof of deletion. Changed export bytes invalidate selection IDs. Recheck the same signed-in account and current conversation before acting: an export can omit newer messages. Review all useful content before approving an entire conversation's removal.

Delete each chosen conversation yourself using [ChatGPT's controls](https://help.openai.com/en/articles/8809935-deleting-and-archiving-chats-in-chatgpt) or [Claude's controls](https://support.claude.com/en/articles/8230524-delete-or-rename-a-conversation). Their documented controls delete whole conversations; this tool cannot remove one online message while keeping the rest. No browser automation, account access or online deletion runs. Deleting online does not remove your downloaded copies. Native `apply` rejects export guides.

## Claude Code and Codex: review, then apply

Root choices apply to both `scan` and `plan`:

| Source | Defaults and optional custom roots |
| --- | --- |
| Omit `--source`, or `--source claude` | `~/.claude`; `--root /custom/claude` |
| `--source codex` | `~/.codex`; `--root /custom/codex` |
| `--source all` | Both defaults; `--claude-root /custom/claude --codex-root /custom/codex` |

Both roots must be distinct and non-nested. With `all`, use provider-specific flags, not `--root`.

1. Close every targeted desktop/CLI client and wait **120 seconds after the last history write**, including Codex before full scanning. If hosted in a targeted app, hand off to an offline terminal, then close that host. Keep clients closed through verification. Nonempty SQLite WAL/SHM/journal sidecars block scans/cleanup; never remove them to bypass the gate.

2. Create a private review directory outside both roots. Scan is read-only. `--preview` prints sensitive prompts into the terminal/conversation and can create another copy; omit it for metadata only. Reports never contain prompt text.

   ```bash
   mkdir -m 700 "$HOME/ai-chat-cleaner-review"
   ai-chat-cleaner scan --source all --preview \
     --report "$HOME/ai-chat-cleaner-review/scan.json"
   ```

3. Review surrounding context and keep mixed messages. Use `scan --all --preview` only for deliberate broader review, retaining source/root flags. Select returned IDs, then inspect the complete copy list:

   ```bash
   ai-chat-cleaner plan --source all --select MESSAGE_ID \
     --out "$HOME/ai-chat-cleaner-review/plan.json"
   ai-chat-cleaner apply --plan "$HOME/ai-chat-cleaner-review/plan.json" --dry-run
   ```

4. Approve every expanded copy. Exact full-prompt hashes expand across selected providers and sessions. There is no removed-text backup or undo. Version 2 rejects older plans: rescan and approve a new plan. Supported-history changes invalidate approval.

   ```bash
   ai-chat-cleaner apply --plan "$HOME/ai-chat-cleaner-review/plan.json" \
     --confirm FULL_CONFIRMATION_HASH \
     --receipt "$HOME/ai-chat-cleaner-review/receipt.json"
   ai-chat-cleaner verify --receipt "$HOME/ai-chat-cleaner-review/receipt.json"
   ```

Metadata uses `0600`; outputs are never overwritten. Use new filenames. Results/errors are JSON; refusals exit 2.

## Coverage and limits

Only known native formats are supported; unknown schemas/versions fail closed. Rich desktop documents require proof that their complete text matches the saved reference; otherwise selecting that prompt refuses without changing history.

| Provider | Supported text |
| --- | --- |
| Claude Code | Human text in `projects/**/*.jsonl`, `history.jsonl` display strings, and exact sidechain/subagent/`lastPrompt` mirrors. Generated subagent inputs remain. |
| Codex | Human inputs in `sessions/**/*.jsonl` and `archived_sessions/**/*.jsonl`; exact replay/compacted copies, `history.jsonl`, `session_index.jsonl`, and known desktop prompt-history/`thread-descriptions-v1` mirrors. |
| Codex SQLite | Exact mirrors in `state_5.sqlite` threads' `first_user_message`/`title`/`preview`/`name` and known item JSON in `thread_history_1.sqlite`. Known `queue_1.sqlite` schemas are validated; nonempty queued operations refuse and must be drained before cleanup. |

Claude strings become `[User text removed locally by ai-chat-cleaner]`. Codex JSON uses that marker when it fits; otherwise an empty string plus JSON whitespace preserves byte positions. Records and unselected transcript bytes remain. SQLite preserves logical records, not database bytes. Mirrors require an exact full-prompt match; arbitrary unsubmitted drafts are not covered.

Local redaction excludes partial/derived quotes, assistant/tool text, summaries, pasted attachments, memory, other logs/caches, exports, backups and server/cloud data. Exact supported human copies in compacted records are the exception. This cannot erase AI memory, change training data, or guarantee permanent/secure SSD erasure.

Export review accepts ChatGPT conversation lists with validated `mapping` message trees, and Claude conversation lists with `uuid`/`chat_messages`. Only supported direct human text is selectable; attachments and nontext content are excluded. These layouts are tested with synthetic fixtures, not a real account export. Unknown or malformed formats refuse rather than imply complete coverage. Export files remain unchanged.

Native redaction refuses malformed/unreadable files, unsafe links, recent writes and stale plans. Replacement is atomic per file; interruptions can leave partial results. Inspect `incomplete`/`pending` receipts and `temporary_files_to_check` before replanning; verify refuses incomplete receipts. Age/digest checks cannot prevent concurrent writers. Cross-file power-loss durability, extended attributes, ACLs and original timestamps are not guaranteed.

Synthetic tests: `python3 -m unittest discover -s tests -v`.
