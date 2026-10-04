# AI Chat Cleaner

Review possible insults in local **Claude Code and Codex** history, choose exact messages, then redact their text from supported records. One CLI and approval plan cover either provider or both.

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

For Claude Code, use `~/.claude/skills/ai-chat-cleaner/SKILL.md` instead. Install the same skill in both hosts if desired. Give it the CLI's absolute path or put the executable on `PATH`. The skill requires the companion CLI. Windows and ChatGPT history are unsupported.

Alternatively, clone into a new directory and install locally:

```bash
git clone https://github.com/chrisshon/ai-chat-cleaner.git
cd ai-chat-cleaner
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
```

## Review, then apply

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

Partial/derived quotes, assistant/tool text, summaries, pasted attachments, memory, other logs/caches, exports, backups and server/cloud data remain excluded. Exact supported human copies in compacted records are the exception. This cannot erase AI memory, change training data, or guarantee permanent/secure SSD erasure.

Malformed/unreadable files, unsafe links, recent writes and stale plans refuse. Replacement is atomic per file; interruptions can leave partial results. Inspect `incomplete`/`pending` receipts and `temporary_files_to_check` before replanning; verify refuses incomplete receipts. Age/digest checks cannot prevent concurrent writers. Cross-file power-loss durability, extended attributes, ACLs and original timestamps are not guaranteed.

Synthetic tests: `python3 -m unittest discover -s tests -v`.
