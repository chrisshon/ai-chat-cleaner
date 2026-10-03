# AI Chat Cleaner

Find possible insults in local Claude Code history. Review them, choose exact messages, then remove their user text from supported local records.

The assistant skill performs the contextual review. The Python tool uses conservative discovery hints and never deletes something just because it contains profanity. Preserve useful corrections, quoted dialogue, praise and mixed requests. Discovery can miss insults; it is not a semantic classifier.

## Install

Use Python **3.10+** on **macOS or Linux**. Runtime dependencies are all in Python's standard library; installation uses setuptools.

```bash
git clone https://github.com/chrisshon/ai-chat-cleaner.git
cd ai-chat-cleaner
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
.venv/bin/ai-chat-cleaner --version
```

For Claude Code, copy `skills/ai-chat-cleaner` into `~/.claude/skills/`. For Codex, copy it into `~/.agents/skills/`. Give your assistant the absolute `.venv/bin/ai-chat-cleaner` path, or keep that executable on `PATH`. The [skill](skills/ai-chat-cleaner/SKILL.md) requires the companion CLI.

Windows and Codex/ChatGPT histories are unsupported in v1. Windows runs refuse because this version requires POSIX file permission protection.

## Review, then apply

Use returned IDs and confirmation hashes in place of the sample values. Create a private working directory outside the history root:

```bash
mkdir -m 700 "$HOME/ai-chat-cleaner-review"
```

1. Scan without changing history. `--preview` explicitly prints sensitive prompts into the terminal or assistant conversation. Omit it for metadata only. Reports never save prompt text.

   ```bash
   ai-chat-cleaner scan --root "$HOME/.claude" --preview \
     --report "$HOME/ai-chat-cleaner-review/scan.json"
   ```

2. Review candidates in their surrounding conversation. Keep useful instructions and mixed messages. Use `scan --all --preview` only for deliberate broader review.

3. Close **all Claude Code sessions** and wait two minutes after the last history write. Use a terminal or a different assistant such as Codex for the remaining commands. Reviewing your own live Claude history changes files and invalidates plans.

4. Create a plan for selected IDs, then inspect the dry run. It lists every exact matching supported copy and gives a confirmation hash. Any subsequent change to supported history invalidates it.

   ```bash
   ai-chat-cleaner plan --root "$HOME/.claude" --select MESSAGE_ID \
     --out "$HOME/ai-chat-cleaner-review/plan.json"
   ai-chat-cleaner apply --plan "$HOME/ai-chat-cleaner-review/plan.json" --dry-run
   ```

5. Approve the **complete copy list**, then apply. The tool makes no removed-text backup and cannot undo this change.

   ```bash
   ai-chat-cleaner apply --plan "$HOME/ai-chat-cleaner-review/plan.json" \
     --confirm FULL_CONFIRMATION_HASH \
     --receipt "$HOME/ai-chat-cleaner-review/receipt.json"
   ai-chat-cleaner verify --receipt "$HOME/ai-chat-cleaner-review/receipt.json"
   ```

Reports, plans and receipts contain only metadata and have permissions `0600`. Keep them outside the history root. Existing outputs are never overwritten by a new command; use new names for new runs. Results/errors are JSON. Refusals exit with code 2.

## Exact coverage

| Location | What can change |
| --- | --- |
| `projects/**/*.jsonl` | Direct human `user` content strings or `text` blocks. |
| Subagent/sidechain and `last-prompt.lastPrompt` records | Exact full-text mirrors of a supported parent/history human prompt, displayed for approval. Generated subagent inputs are excluded. |
| `history.jsonl` | Exact matching `display` strings. |

Selected strings become `[User text removed locally by ai-chat-cleaner]`. UUIDs, record order, non-text blocks and unselected bytes stay intact. This is **local text redaction**, not deletion of conversation records. All full-prompt matches appear in the plan, including identical messages in different sessions. An insult inside a longer useful prompt is not a full match.

Assistant replies, tool output, summaries/compaction, attachments, `pastedContents`, memory files, other logs/caches, exports, backups and cloud data are excluded. They can retain quotations or transformed copies. Previewing in another assistant can create another copy. The tool cannot erase AI memory, change training data, or guarantee secure SSD erasure.

## Failure behavior

Malformed JSON, unsupported types/schemas, unreadable files, symlinks, hardlinks, recent writes and stale plans refuse. Files are never silently skipped to make an apply succeed. Unknown metadata fields are preserved without deletion claims.

Replacement is atomic **per file**, preserving permission bits. Disk errors or interruptions can leave a partial multi-file result. Apply errors report `incomplete` after changes, with the receipt path and changed-file count; failed receipt updates can leave its status `pending`. Cleanup failures list `temporary_files_to_check`, the paths that may still contain redacted drafts. Inspect current files before replanning; verification refuses incomplete receipts. Temporary files contain proposed redacted results, never removed-text backups.

Keep every history writer closed until verification finishes. Age/digest checks detect changes but cannot prevent another program racing a write. Power-loss durability across files, extended attributes, ACLs and original timestamps are not guaranteed.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic histories only. They cover detection, preservation, copy expansion, tool/replay exclusion, bytes, stale/recent files, unsafe paths, failure reporting and the complete CLI workflow. GitHub Actions is configured for macOS/Linux with Python 3.10/3.13; configuration alone does not prove a remote run passed.
