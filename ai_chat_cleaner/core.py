"""Claude Code adapters and fail-closed plan/apply/verify workflow."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

from .detection import candidate_reason
from .storage import (Refusal, canonical, checked_path, digest, fingerprint,
                      load_json, metadata_path, replace_private, safe_file,
                      stage, string_spans, write_private)

TOMBSTONE = "[User text removed locally by ai-chat-cleaner]"
QUIET_SECONDS = 120
KNOWN_TYPES = {
    "user", "assistant", "system", "summary", "progress", "queue-operation",
    "file-history-snapshot", "last-prompt", "custom-title", "agent-name",
    "agent-color", "session-name", "tag", "slug", "attachment", "pr-link",
    "atis-latch", "ai-title", "cost-state", "bridge-session", "mode",
    "file-history-delta", "agent-setting", "permission-mode", "frame-link",
    "worktree-state", "artifact-comment-monitor", "artifact-autoreact-ledger",
    "relocated", "started", "result", "launched",
}
COVERAGE = {
    "supported": ["projects/**/*.jsonl: direct user message text blocks",
                  "projects/**/*.jsonl: exact last-prompt.lastPrompt mirrors",
                  "history.jsonl: display strings; exact text matches only"],
    "excluded": ["assistant/tool output and summaries", "pastedContents and attachments",
                 "memory files, other local logs and caches", "exports, backups and cloud data"],
    "method": "replace selected string tokens with a tombstone; no secure disk erasure",
}


class ApplyFailure(Refusal):
    """An apply error whose result must not imply that nothing changed."""

    def __init__(self, receipt, changed, receipt_status, temporary_files=None):
        super().__init__("apply did not complete; inspect the receipt and current history before replanning")
        self.result = {"error": str(self), "status": "incomplete" if changed else "failed",
                       "receipt": str(receipt), "files_changed": changed,
                       "receipt_status": receipt_status, "verified": False,
                       "temporary_files_to_check": temporary_files or []}


@dataclass
class Prompt:
    id: str
    file: str
    line: int
    paths: list
    text: str
    sha256: str
    reason: str | None
    copy_only: bool = False

    def public(self, preview=False):
        value = {"id": self.id, "file": self.file, "line": self.line,
                 "candidate": bool(self.reason), "reason": self.reason}
        if preview:
            value["text"] = self.text
        return value


def discover(root):
    root = checked_path(root)
    if not root.is_dir():
        raise Refusal("history root must be an existing directory")
    paths = []
    history = root / "history.jsonl"
    if history.exists() or history.is_symlink():
        safe_file(history)
        paths.append(history)
    projects = root / "projects"
    if projects.is_symlink():
        raise Refusal("projects directory is a symlink")
    if projects.exists():
        if not projects.is_dir():
            raise Refusal("projects must be a directory")
        def failed(error):
            raise Refusal("cannot enumerate every project file") from error
        for parent, folders, files in os.walk(projects, followlinks=False, onerror=failed):
            for folder in folders:
                if (Path(parent) / folder).is_symlink():
                    raise Refusal("project directory contains a symlink")
            for filename in files:
                if filename.endswith(".jsonl"):
                    path = Path(parent) / filename
                    safe_file(path)
                    paths.append(path)
    if not paths:
        raise Refusal("no supported Claude Code history files found")
    return root, sorted(paths)


def _human_paths(record, history):
    if not isinstance(record, dict):
        raise Refusal("record must be a JSON object")
    if history:
        if not isinstance(record.get("display"), str) or not isinstance(record.get("timestamp"), (str, int)):
            raise Refusal("unknown history record schema")
        return [("display",)]
    kind = record.get("type")
    if kind not in KNOWN_TYPES:
        raise Refusal("unknown session record type")
    if kind == "last-prompt":
        if record.get("lastPrompt") is None:
            return []
        if not isinstance(record.get("lastPrompt"), str):
            raise Refusal("unknown last-prompt record schema")
        return [("lastPrompt",)]
    if kind != "user":
        return []
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        raise Refusal("unknown user message schema")
    content = message.get("content")
    if isinstance(content, str):
        paths = [("message", "content")]
    elif isinstance(content, list):
        paths = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                raise Refusal("unknown content block schema")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise Refusal("unknown text block schema")
                paths.append(("message", "content", index, "text"))
            elif block.get("type") not in {"tool_result", "image", "document"}:
                raise Refusal("unknown user content block type")
    else:
        raise Refusal("unknown user content shape")
    # A user envelope is also used for tool results, replay and injected messages.
    if (record.get("isMeta") or record.get("isReplay") or record.get("isReplayed")
            or record.get("isCompactSummary") or record.get("isSynthetic")
            or record.get("sourceToolAssistantUUID")
            or record.get("userType", "external") != "external"):
        return []
    return paths


def _at(record, path):
    for key in path:
        record = record[key]
    return record


def read_sources(root):
    root, files = discover(root)
    sources, prompts, raws = {}, [], {}
    for file in files:
        relative = file.relative_to(root).as_posix()
        raw, info = fingerprint(file)
        sources[relative], raws[relative] = info, raw
        for number, line in enumerate(raw.splitlines(keepends=True), 1):
            if not line.strip():
                raise Refusal(f"{relative}:{number}: blank records are unsupported")
            try:
                record = load_json(line)
                paths = _human_paths(record, relative == "history.jsonl")
                text = "\n".join(_at(record, path) for path in paths)
                if (not paths or not text or all(_at(record, p) == TOMBSTONE for p in paths)
                        or text.lstrip().startswith("<")):
                    continue
                try:
                    sha = digest(text.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise Refusal("user text contains unsupported Unicode surrogates") from exc
                identity = digest(canonical([relative, number, sha]))[:24]
                copy_only = ("/subagents/" in relative or bool(record.get("isSidechain"))
                             or record.get("type") == "last-prompt")
                prompts.append(Prompt(identity, relative, number, paths, text, sha,
                                      candidate_reason(text), copy_only))
            except Refusal as exc:
                raise Refusal(f"{relative}:{number}: {exc}") from exc
    originals = {p.sha256 for p in prompts if not p.copy_only}
    # Subagent inputs can be generated by another assistant. Only include known
    # exact mirrors of a supported parent/history human prompt.
    prompts = [p for p in prompts if not p.copy_only or p.sha256 in originals]
    return root, sources, prompts, raws


def scan(root, preview=False, include_all=False):
    root, sources, prompts, _ = read_sources(root)
    selected = prompts if include_all else [p for p in prompts if p.reason]
    return {"version": 1, "root": str(root), "coverage": COVERAGE,
            "files_checked": len(sources), "human_records": len(prompts),
            "candidates": sum(bool(p.reason) for p in prompts),
            "messages": [p.public(preview) for p in selected]}


def _quiet(sources):
    now = time.time_ns()
    if any(now - s["mtime_ns"] < QUIET_SECONDS * 1_000_000_000 for s in sources.values()):
        raise Refusal(f"history changed in the last {QUIET_SECONDS} seconds; close sessions and wait")


def _plan_id(plan):
    return digest(canonical({k: v for k, v in plan.items() if k != "id"}))


def make_plan(root, selected, output):
    root, sources, prompts, _ = read_sources(root)
    _quiet(sources)
    if not selected or len(selected) != len(set(selected)):
        raise Refusal("select at least one distinct message ID")
    by_id = {p.id: p for p in prompts}
    if any(identity not in by_id for identity in selected):
        raise Refusal("selected ID is missing or is not a supported human prompt")
    hashes = sorted({by_id[identity].sha256 for identity in selected})
    occurrences = [p for p in prompts if p.sha256 in hashes]
    plan = {"version": 1, "root": str(root), "created_ns": time.time_ns(),
            "coverage": COVERAGE, "sources": sources, "selected": sorted(selected),
            "hashes": hashes, "occurrences": [p.public() for p in occurrences]}
    plan["id"] = _plan_id(plan)
    path = metadata_path(output, root)
    write_private(path, plan)
    return {"plan": str(path), "confirmation": plan["id"],
            "selected_records": len(selected), "matching_records": len(occurrences),
            "files_to_change": len({p.file for p in occurrences}),
            "occurrences": plan["occurrences"], "coverage": COVERAGE}


def _read_plan(path):
    path = checked_path(path)
    raw, info = fingerprint(path)
    if info["mode"] & 0o077:
        raise Refusal("plan/receipt permissions must be private (0600)")
    plan = load_json(raw)
    if not isinstance(plan, dict) or set(plan) != {
        "version", "root", "created_ns", "coverage", "sources", "selected", "hashes", "occurrences", "id"
    } or plan.get("version") != 1:
        raise Refusal("unknown plan schema")
    if plan.get("id") != _plan_id(plan) or plan.get("coverage") != COVERAGE:
        raise Refusal("plan integrity check failed")
    if not isinstance(plan["root"], str) or not Path(plan["root"]).is_absolute():
        raise Refusal("invalid plan root")
    for key in ("selected", "hashes", "occurrences"):
        if not isinstance(plan[key], list):
            raise Refusal("invalid plan selection schema")
    return path, plan


def _validated(plan):
    root, sources, prompts, raws = read_sources(plan["root"])
    if sources != plan["sources"]:
        raise Refusal("stale plan: file inventory, contents or metadata changed; scan again")
    _quiet(sources)
    by_id = {p.id: p for p in prompts}
    selected = plan["selected"]
    if (not selected or any(not isinstance(s, str) or s not in by_id for s in selected)
            or len(selected) != len(set(selected))):
        raise Refusal("invalid plan message IDs")
    hashes = sorted({by_id[s].sha256 for s in selected})
    occurrences = [p for p in prompts if p.sha256 in hashes]
    if hashes != plan["hashes"] or [p.public() for p in occurrences] != plan["occurrences"]:
        raise Refusal("plan selection no longer matches supported human records")
    return root, sources, occurrences, raws


def _transform(raw, occurrences):
    lines = raw.splitlines(keepends=True)
    for prompt in occurrences:
        line = lines[prompt.line - 1].decode("utf-8")
        spans = string_spans(line)
        replacements = [spans[tuple(path)] for path in prompt.paths]
        token = json.dumps(TOMBSTONE)
        for start, end in sorted(replacements, reverse=True):
            line = line[:start] + token + line[end:]
        load_json(line)  # Every rewritten record remains valid JSON.
        lines[prompt.line - 1] = line.encode("utf-8")
    return b"".join(lines)


def apply_plan(plan_path, confirmation, receipt_path, dry_run=False):
    _, plan = _read_plan(plan_path)
    if not dry_run and confirmation != plan["id"]:
        raise Refusal("explicit --confirm must match the exact plan confirmation")
    root, sources, occurrences, raws = _validated(plan)
    groups = {}
    for prompt in occurrences:
        groups.setdefault(prompt.file, []).append(prompt)
    updates = {name: _transform(raws[name], records) for name, records in groups.items()}
    result = {"plan_id": plan["id"], "files_to_change": len(updates),
              "matching_records": len(occurrences), "coverage": COVERAGE}
    if dry_run:
        return {**result, "status": "dry_run", "occurrences": plan["occurrences"]}
    receipt_path = metadata_path(receipt_path, root)
    receipt = {"version": 1, "root": str(root), "plan_id": plan["id"],
               "coverage": COVERAGE, "status": "pending", "files": {
                   name: {"before": sources[name]["sha256"], "after": digest(data),
                          "mode": sources[name]["mode"], "applied": False}
                   for name, data in updates.items()},
               "records": len(occurrences), "temporary_files_to_check": []}
    # Reserve a private receipt before changing anything. It contains no prompt text.
    write_private(receipt_path, receipt)
    staged = {}
    persisted_status = "pending"
    failure = None
    interruption = None
    try:
        for name, data in updates.items():
            staged[name] = stage(root / name, data, sources[name]["mode"])
        # Check the complete inventory again after staging, before the first write.
        _validated(plan)
        for name, temporary in staged.items():
            path = root / name
            _, current = fingerprint(path)
            if current != sources[name]:
                raise Refusal("source changed immediately before replace")
            os.replace(temporary, path)
            receipt["files"][name]["applied"] = True
            replace_private(receipt_path, receipt)
        receipt["status"] = "applied"
        replace_private(receipt_path, receipt)
        persisted_status = "applied"
        verified = verify(receipt_path)["verified"]
    except BaseException as exc:
        receipt["status"] = "partial" if any(f["applied"] for f in receipt["files"].values()) else "failed"
        try:
            replace_private(receipt_path, receipt)
            persisted_status = receipt["status"]
        except (OSError, Refusal):
            pass
        changed = sum(f["applied"] for f in receipt["files"].values())
        if isinstance(exc, Exception):
            failure = ApplyFailure(receipt_path, changed, persisted_status)
        else:
            interruption = exc
    finally:
        cleanup_pending = []
        for temporary in staged.values():
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cleanup must never mask an error after source replacements.
                # The path may remain, or permissions may prevent confirming its absence.
                cleanup_pending.append(str(temporary))
    if cleanup_pending:
        changed = sum(f["applied"] for f in receipt["files"].values())
        receipt["temporary_files_to_check"] = cleanup_pending
        receipt["status"] = "partial" if changed else "failed"
        try:
            replace_private(receipt_path, receipt)
            persisted_status = receipt["status"]
        except (OSError, Refusal):
            pass
        failure = ApplyFailure(receipt_path, changed, persisted_status, cleanup_pending)
    if interruption is not None:
        raise interruption
    if failure is not None:
        raise failure
    return {**result, "status": "applied", "receipt": str(receipt_path),
            "verified": verified}


def verify(receipt_path):
    path = checked_path(receipt_path)
    raw, info = fingerprint(path)
    if info["mode"] & 0o077:
        raise Refusal("receipt permissions must be private (0600)")
    receipt = load_json(raw)
    if (not isinstance(receipt, dict) or set(receipt) != {
        "version", "root", "plan_id", "coverage", "status", "files", "records", "temporary_files_to_check"
    } or receipt.get("version") != 1 or receipt.get("coverage") != COVERAGE
            or receipt.get("status") != "applied" or not isinstance(receipt.get("files"), dict)
            or not receipt["files"] or receipt.get("temporary_files_to_check") != []):
        raise Refusal("receipt is invalid or the operation did not fully apply")
    root, files = discover(receipt["root"])
    allowed = {p.relative_to(root).as_posix() for p in files}
    for name, expected in receipt["files"].items():
        if name not in allowed or not isinstance(expected, dict) or expected.get("applied") is not True:
            raise Refusal("receipt references an unexpected file")
        raw, actual = fingerprint(root / name)
        if actual["sha256"] != expected.get("after") or actual["mode"] != expected.get("mode"):
            raise Refusal("verification failed: contents or permissions changed")
        for line in raw.splitlines():
            _human_paths(load_json(line), name == "history.jsonl")
    return {"verified": True, "files_verified": len(receipt["files"]),
            "records": receipt["records"], "coverage": COVERAGE}
