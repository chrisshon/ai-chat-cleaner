"""Shared, fail-closed review workflow for Claude Code and Codex histories."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time

from .detection import candidate_reason
from . import codex, codex_state
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
CLAUDE_COVERAGE = {
    "supported": ["projects/**/*.jsonl: direct user message text blocks",
                  "projects/**/*.jsonl: exact last-prompt.lastPrompt mirrors",
                  "history.jsonl: display strings; exact text matches only"],
    "excluded": ["assistant/tool output and summaries", "pastedContents and attachments",
                 "memory files, other local logs and caches", "exports, backups and cloud data"],
    "method": "replace selected string tokens with a tombstone; no secure disk erasure",
}

COVERAGE = {
    "supported": {"claude": CLAUDE_COVERAGE["supported"], "codex": [
        "sessions/**/*.jsonl and archived_sessions/**/*.jsonl: labelled human input and exact replay mirrors",
        "history.jsonl, session_index.jsonl and known desktop prompt-history mirrors",
        "state_5.sqlite and thread_history_1.sqlite: known exact prompt mirrors; queue_1.sqlite must be empty",
    ]},
    "excluded": ["assistant/tool output, summaries and injected context", "attachments and unsubmitted drafts without an exact original match",
                 "memory, other logs/caches, derived or partial quotes", "exports, backups and cloud/server data"],
    "method": "local text replacement; JSONL positions and logical SQLite records preserved; no secure disk erasure",
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
    provider: str = "claude"
    cell: dict | None = None
    blocked: bool = False

    def public(self, preview=False):
        value = {"id": self.id, "file": self.file, "line": self.line,
                 "candidate": bool(self.reason), "reason": self.reason, "source": self.provider}
        if self.blocked:
            value["blocked"] = "unproven rich document reference"
        if self.cell:
            value["storage"] = {"table": self.cell["table"], "column": self.cell["column"]}
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


def _roots(root=None, source="claude", claude_root=None, codex_root=None):
    if source not in {"claude", "codex", "all"}:
        raise Refusal("unknown history source")
    if source == "all":
        if root is not None:
            raise Refusal("--root is ambiguous with --source all; use --claude-root and --codex-root")
        roots = {"claude": checked_path(claude_root or Path.home() / ".claude"),
                 "codex": checked_path(codex_root or Path.home() / ".codex")}
    else:
        if claude_root is not None or codex_root is not None:
            raise Refusal("provider-specific root flags require --source all")
        roots = {source: checked_path(root or Path.home() / (".claude" if source == "claude" else ".codex"))}
    values = list(roots.values())
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(values) for b in values[i+1:]):
        raise Refusal("history roots must be distinct and must not contain each other")
    return roots


def discover_codex(root):
    root = checked_path(root)
    if not root.is_dir():
        raise Refusal("Codex history root must be an existing directory")
    files = []
    for name in ("history.jsonl", "session_index.jsonl", ".codex-global-state.json"):
        path = root / name
        if path.exists() or path.is_symlink():
            safe_file(path)
            files.append(path)
    for name in ("sessions", "archived_sessions"):
        folder = root / name
        if folder.is_symlink():
            raise Refusal("Codex history directory is a symlink")
        if not folder.exists():
            continue
        if not folder.is_dir():
            raise Refusal("Codex history path must be a directory")
        def failed(error):
            raise Refusal("cannot enumerate every Codex history file") from error
        for parent, folders, names in os.walk(folder, followlinks=False, onerror=failed):
            if any((Path(parent) / n).is_symlink() for n in folders):
                raise Refusal("Codex history directory contains a symlink")
            for name in names:
                if name.endswith(".jsonl"):
                    path = Path(parent) / name
                    safe_file(path)
                    files.append(path)
    for path in root.iterdir():
        if any(path.name.startswith(prefix) for prefix in ("state_", "thread_history_", "queue_")) and ".sqlite" in path.name:
            if path.name not in {"state_5.sqlite", "thread_history_1.sqlite", "queue_1.sqlite"} and not any(
                    path.name == name + suffix for name in ("state_5.sqlite", "thread_history_1.sqlite", "queue_1.sqlite")
                    for suffix in ("-wal", "-shm", "-journal")):
                raise Refusal("unsupported Codex database version or sidecar; update the cleaner before continuing")
            safe_file(path)
            files.append(path)
    if not files:
        raise Refusal("no supported Codex history files found")
    return root, sorted(files)


def _collect(roots):
    sources, prompts, raws = {}, [], {}
    for provider, root in sorted(roots.items()):
        _, files = discover(root) if provider == "claude" else discover_codex(root)
        for file in files:
            relative = file.relative_to(root).as_posix()
            name = relative if len(roots) == 1 else provider + "/" + relative
            raw, info = fingerprint(file)
            fmt = ("sqlite" if relative.endswith(".sqlite") else "sidecar" if ".sqlite-" in relative
                   else "json" if relative == ".codex-global-state.json" else "jsonl")
            info.update(provider=provider, relative=relative, format=fmt)
            sources[name], raws[name] = info, raw
            if fmt == "sidecar":
                continue
            if fmt == "sqlite":
                groups = codex_state.read(file, codex.record_prompts)
                numbered = [(0, None, groups)]
            else:
                numbered = []
                secondary = False
                lines = [raw] if fmt == "json" else raw.splitlines(keepends=True)
                for number, line in enumerate(lines, 1):
                    if not line.strip():
                        raise Refusal("blank history records are unsupported")
                    record = load_json(line)
                    if not isinstance(record, dict):
                        raise Refusal("history record must be a JSON object")
                    if provider == "codex" and record.get("type") == "session_meta":
                        payload = record.get("payload")
                        if not isinstance(payload, dict):
                            raise Refusal("unsupported Codex session metadata")
                        origin = payload.get("source")
                        thread_source = payload.get("thread_source")
                        external_sources = {"cli", "vscode", "exec", "mcp", "appserver", "app-server", "app_server"}
                        external_threads = {None, "user", "realtime_voice", "voice_chat"}
                        source_known = origin is None or (isinstance(origin, str) and origin in external_sources)
                        thread_known = not isinstance(thread_source, (dict, list)) and thread_source in external_threads
                        secondary = secondary or not source_known or not thread_known
                    if provider == "claude":
                        paths = _human_paths(record, relative == "history.jsonl")
                        groups = [{"paths": paths, "copy_only": ("/subagents/" in relative or bool(record.get("isSidechain"))
                                   or record.get("type") == "last-prompt")}]
                    else:
                        kind = "global" if fmt == "json" else "history" if relative == "history.jsonl" else "index" if relative == "session_index.jsonl" else "session"
                        groups = codex.record_prompts(record, kind)
                        if secondary:
                            for group in groups:
                                group["copy_only"] = True
                    numbered.append((number, record, groups))
            for number, record, groups in numbered:
                for group in groups:
                    paths = group["paths"]
                    text = group.get("text")
                    if text is None:
                        text = "\n".join(_at(record, path) for path in paths)
                    if not isinstance(text, str):
                        raise Refusal("unsupported prompt text schema")
                    if not paths or not text or text == TOMBSTONE or text.lstrip().startswith("<"):
                        continue
                    # A previously redacted multi-block record must not become a new original.
                    if record is not None and all(_at(record, p) in {"", TOMBSTONE} for p in paths):
                        continue
                    try:
                        sha = digest(text.encode("utf-8"))
                    except UnicodeEncodeError as exc:
                        raise Refusal("user text contains unsupported Unicode surrogates") from exc
                    cell = group.get("cell")
                    identity = digest(canonical([provider, relative, number, paths, cell, sha]))[:24]
                    prompts.append(Prompt(identity, name, number, paths, text, sha, candidate_reason(text),
                                          group.get("copy_only", False), provider, cell, bool(group.get("reference_mismatch"))))
        # SQLite snapshots must agree with the complete file/sidecar inventory read above.
        if provider == "codex":
            _, current_files = discover_codex(root)
            if files != current_files:
                raise Refusal("Codex file inventory changed during scan")
            for name, info in sources.items():
                if info["provider"] == provider:
                    _, current = fingerprint(root / info["relative"])
                    if any(current[k] != info[k] for k in current):
                        raise Refusal("Codex history changed during scan; close clients and retry")
    originals = {p.sha256 for p in prompts if not p.copy_only}
    prompts = [p for p in prompts if not p.copy_only or p.sha256 in originals]
    return sources, prompts, raws


def read_sources(root):
    roots = _roots(root)
    sources, prompts, raws = _collect(roots)
    return roots["claude"], sources, prompts, raws


def _root_fields(roots):
    return {"root": str(next(iter(roots.values()))) if len(roots) == 1 else None,
            "roots": {p: str(r) for p, r in roots.items()}}


def _metadata(value, roots):
    path = None
    for root in roots.values():
        path = metadata_path(value, root)
    return path


def scan(root=None, preview=False, include_all=False, *, source="claude", claude_root=None, codex_root=None):
    roots = _roots(root, source, claude_root, codex_root)
    sources, prompts, _ = _collect(roots)
    selected = prompts if include_all else [p for p in prompts if p.reason]
    return {"version": 2, **_root_fields(roots), "coverage": COVERAGE,
            "files_checked": len(sources), "human_records": len(prompts),
            "candidates": sum(bool(p.reason) for p in prompts),
            "messages": [p.public(preview) for p in selected]}


def _quiet(sources):
    if any(s["format"] == "sidecar" and s["size"] for s in sources.values()):
        raise Refusal("Codex database sidecars are active; close every Codex client and wait for checkpointing")
    now = time.time_ns()
    if any(now - s["mtime_ns"] < QUIET_SECONDS * 1_000_000_000 for s in sources.values()):
        raise Refusal(f"history changed in the last {QUIET_SECONDS} seconds; close sessions and wait")


def _plan_id(plan):
    return digest(canonical({k: v for k, v in plan.items() if k != "id"}))


def make_plan(root, selected, output, *, source="claude", claude_root=None, codex_root=None):
    roots = _roots(root, source, claude_root, codex_root)
    sources, prompts, _ = _collect(roots)
    _quiet(sources)
    if not selected or len(selected) != len(set(selected)):
        raise Refusal("select at least one distinct message ID")
    by_id = {p.id: p for p in prompts}
    if any(identity not in by_id for identity in selected):
        raise Refusal("selected ID is missing or is not a supported human prompt")
    hashes = sorted({by_id[identity].sha256 for identity in selected})
    occurrences = [p for p in prompts if p.sha256 in hashes]
    if any(p.blocked for p in occurrences):
        raise Refusal("selected text has an unproven rich document copy; manual review is required before cleanup")
    plan = {"version": 2, **_root_fields(roots), "created_ns": time.time_ns(),
            "coverage": COVERAGE, "sources": sources, "selected": sorted(selected),
            "hashes": hashes, "occurrences": [p.public() for p in occurrences]}
    plan["id"] = _plan_id(plan)
    path = _metadata(output, roots)
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
        "version", "root", "roots", "created_ns", "coverage", "sources", "selected", "hashes", "occurrences", "id"
    } or plan.get("version") != 2:
        raise Refusal("unsupported plan schema; scan and create a new plan with this version")
    if plan.get("id") != _plan_id(plan) or plan.get("coverage") != COVERAGE:
        raise Refusal("plan integrity check failed")
    _saved_roots(plan)
    for key in ("selected", "hashes", "occurrences"):
        if not isinstance(plan[key], list):
            raise Refusal("invalid plan selection schema")
    return path, plan


def _saved_roots(value):
    saved = value.get("roots")
    if not isinstance(saved, dict) or not saved or not set(saved) <= {"claude", "codex"}:
        raise Refusal("invalid saved history roots")
    if any(not isinstance(p, str) or not Path(p).is_absolute() for p in saved.values()):
        raise Refusal("invalid saved root path")
    roots = {p: checked_path(r) for p, r in saved.items()}
    if _root_fields(roots) != {"root": value.get("root"), "roots": saved}:
        raise Refusal("inconsistent saved history roots")
    if len(roots) == 2:
        _roots(None, "all", roots["claude"], roots["codex"])
    return roots


def _validated(plan):
    roots = _saved_roots(plan)
    sources, prompts, raws = _collect(roots)
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
    if any(p.blocked for p in occurrences):
        raise Refusal("selected text has an unproven rich document copy")
    if hashes != plan["hashes"] or [p.public() for p in occurrences] != plan["occurrences"]:
        raise Refusal("plan selection no longer matches supported human records")
    return roots, sources, occurrences, raws


def _replace_tokens(line, paths, preserve_bytes=False):
    text = line.decode("utf-8")
    spans = string_spans(text)
    replacements = {spans[tuple(p)] for p in paths}
    for start, end in sorted(replacements, reverse=True):
        token = json.dumps(TOMBSTONE)
        if preserve_bytes:
            length = len(text[start:end].encode("utf-8"))
            if len(token.encode()) > length:
                token = '""'
            token += " " * (length - len(token.encode()))
        text = text[:start] + token + text[end:]
    load_json(text)
    result = text.encode("utf-8")
    if preserve_bytes and len(result) != len(line):
        raise Refusal("Codex transcript byte positions changed")
    return result


def _transform(raw, occurrences, preserve_bytes=False, whole_json=False):
    lines = [raw] if whole_json else raw.splitlines(keepends=True)
    groups = {}
    for prompt in occurrences:
        number = 0 if whole_json else prompt.line - 1
        groups.setdefault(number, []).extend(prompt.paths)
    for number, paths in groups.items():
        lines[number] = _replace_tokens(lines[number], paths, preserve_bytes)
    return b"".join(lines)


def apply_plan(plan_path, confirmation, receipt_path, dry_run=False):
    _, plan = _read_plan(plan_path)
    if not dry_run and confirmation != plan["id"]:
        raise Refusal("explicit --confirm must match the exact plan confirmation")
    roots, sources, occurrences, raws = _validated(plan)
    groups = {}
    for prompt in occurrences:
        groups.setdefault(prompt.file, []).append(prompt)
    updates = {name: _transform(raws[name], records, sources[name]["provider"] == "codex", sources[name]["format"] == "json")
               for name, records in groups.items() if sources[name]["format"] != "sqlite"}
    result = {"plan_id": plan["id"], "files_to_change": len(groups),
              "matching_records": len(occurrences), "coverage": COVERAGE}
    if dry_run:
        return {**result, "status": "dry_run", "occurrences": plan["occurrences"]}
    receipt_path = _metadata(receipt_path, roots)
    receipt = {"version": 2, **_root_fields(roots), "plan_id": plan["id"],
               "coverage": COVERAGE, "status": "pending", "files": {
                   name: {"before": sources[name]["sha256"], "after": digest(updates[name]) if name in updates else None,
                          "mode": sources[name]["mode"], "applied": False}
                   for name in groups},
               "records": len(occurrences), "temporary_files_to_check": []}
    # Reserve a private receipt before changing anything. It contains no prompt text.
    write_private(receipt_path, receipt)
    staged = {}
    persisted_status = "pending"
    failure = None
    interruption = None
    cleanup_pending = []
    try:
        for name, records in groups.items():
            info = sources[name]
            path = roots[info["provider"]] / info["relative"]
            if info["format"] == "sqlite":
                staged[name] = codex_state.stage(path, records, info["mode"],
                                                lambda raw, ps: _transform(raw, ps, whole_json=True))
                data, _ = fingerprint(staged[name])
                receipt["files"][name]["after"] = digest(data)
            else:
                staged[name] = stage(path, updates[name], info["mode"])
        # Check the complete inventory again after staging, before the first write.
        _validated(plan)
        for name, temporary in staged.items():
            info = sources[name]
            path = roots[info["provider"]] / info["relative"]
            _, current = fingerprint(path)
            if any(current[k] != sources[name][k] for k in current):
                raise Refusal("source changed immediately before replace")
            if info["format"] == "sqlite":
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = Path(str(path) + suffix)
                    if sidecar.exists() or sidecar.is_symlink():
                        if safe_file(sidecar).st_size:
                            raise Refusal("Codex database became active immediately before replace")
            os.replace(temporary, path)
            receipt["files"][name]["applied"] = True
            replace_private(receipt_path, receipt)
        receipt["status"] = "applied"
        replace_private(receipt_path, receipt)
        persisted_status = "applied"
        verified = verify(receipt_path)["verified"]
    except BaseException as exc:
        cleanup_pending.extend(getattr(exc, "temporary_files_to_check", []))
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
        "version", "root", "roots", "plan_id", "coverage", "status", "files", "records", "temporary_files_to_check"
    } or receipt.get("version") != 2 or receipt.get("coverage") != COVERAGE
            or receipt.get("status") != "applied" or not isinstance(receipt.get("files"), dict)
            or not receipt["files"] or receipt.get("temporary_files_to_check") != []):
        raise Refusal("receipt is invalid or the operation did not fully apply")
    roots = _saved_roots(receipt)
    sources, _, _ = _collect(roots)
    for name, expected in receipt["files"].items():
        if name not in sources or not isinstance(expected, dict) or expected.get("applied") is not True:
            raise Refusal("receipt references an unexpected file")
        actual = sources[name]
        if actual["sha256"] != expected.get("after") or actual["mode"] != expected.get("mode"):
            raise Refusal("verification failed: contents or permissions changed")
    return {"verified": True, "files_verified": len(receipt["files"]),
            "records": receipt["records"], "coverage": COVERAGE}
