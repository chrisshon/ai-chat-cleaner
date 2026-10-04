"""Read consumer exports and make private, manual whole-chat deletion guides.

No export bytes are changed, no network is used, and guides are not native plans.
Only the explicitly supported layouts below are accepted; these are not a
provider guarantee that every current or future export has the same schema.
"""
from collections import Counter
import io
import math
import os
from pathlib import PurePosixPath
import re
import stat
import struct
import zipfile
import zlib

from .detection import candidate_reason
from .storage import Refusal, canonical, checked_path, digest, load_json, metadata_path, safe_file, write_private

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ZIP_MEMBERS = 2000
MAX_ZIP_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_ZIP_RATIO = 200
MAX_MESSAGES = 200000
MAX_VALUES = 2000000
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
ROLES = {"user", "assistant", "system", "developer", "tool"}
GENERATED_FLAGS = {"is_generated", "is_synthetic", "is_replay", "is_replayed", "is_tool_result",
                   "is_hidden", "is_visually_hidden_from_conversation", "is_user_system_message"}
GPT_MEDIA = {"image_asset_pointer", "audio_asset_pointer", "video_asset_pointer",
             "real_time_user_audio_video_asset_pointer", "input_image", "input_audio", "input_video",
             "image", "audio", "video", "file"}
CLAUDE_NON_TEXT = {"image", "document", "file", "tool_use", "tool_result", "thinking", "redacted_thinking"}
CONTROLS = {
    "chatgpt": {
        "history_url": "https://chatgpt.com/",
        "instructions_url": "https://help.openai.com/en/articles/8809935-deleting-and-archiving-chats-in-chatgpt",
        "steps": ["Open the verified conversation in your ChatGPT history.",
                  "Open its more-options menu, choose Delete, and review the confirmation.",
                  "Confirm only after checking that the entire conversation can be lost."],
    },
    "claude": {
        "history_url": "https://claude.ai/",
        "instructions_url": "https://support.claude.com/en/articles/8230524-delete-or-rename-a-conversation",
        "steps": ["Find the verified conversation in Claude's sidebar or Chats and tasks.",
                  "Open its more-options menu and choose Delete.",
                  "Review the confirmation and confirm only if the entire conversation can be lost."],
    },
}
LIMITATIONS = [
    "Review only: this tool does not delete or alter consumer exports or online conversations.",
    "Exports can be stale. Match the live account and complete conversation before using provider controls.",
    "Hints are conservative keyword discovery, not semantic judgments or deletion approval.",
    "Only direct human text in supported layouts is reviewed; attachments, tool content and generated messages are excluded.",
    "A whole-conversation deletion also loses useful user and assistant content, including alternate branches and attachments.",
    "Local exports, saved memories, separate files, backups and other copies are not removed by this guide.",
]


def _object(value, label):
    if not isinstance(value, dict):
        raise Refusal("unsupported export " + label + " schema")
    return value


def _string(value, label):
    if not isinstance(value, str):
        raise Refusal("unsupported export " + label + " text schema")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Refusal("export contains unsupported Unicode surrogates") from exc
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Refusal("export contains an unsupported or unsafe identifier")
    return value


def _validate_values(value):
    stack = [value]
    count = 0
    while stack:
        current = stack.pop()
        count += 1
        if count > MAX_VALUES:
            raise Refusal("export exceeds the supported structure size")
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            _string(current, "value")
        elif isinstance(current, float) and not math.isfinite(current):
            raise Refusal("export contains non-finite numbers")


def _read_input(value):
    path = checked_path(value)
    before = safe_file(path)
    if before.st_size > MAX_INPUT_BYTES:
        raise Refusal("export input exceeds the 64 MiB limit")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise Refusal("export changed during open")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
    if len(raw) > MAX_INPUT_BYTES or any(getattr(before, key) != getattr(after, key) for key in keys):
        raise Refusal("export changed during read or exceeds the supported size")
    return path, raw, tuple(getattr(after, key) for key in keys)


def _assert_unchanged(path, saved):
    info = safe_file(path)
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
    if tuple(getattr(info, key) for key in keys) != saved:
        raise Refusal("export changed during review; restart with the current file")


def _zip_directory_budget(raw):
    """Bound central records before ZipFile allocates their Python objects.

    Standard single-disk ZIPs only. Count both declared and actual records;
    ZipFile itself does not enforce the footer's declared entry count.
    """
    footer = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if footer < 0 or footer + 22 > len(raw):
        raise Refusal("invalid export ZIP footer")
    _, disk, directory_disk, disk_count, total, size, offset, comment = struct.unpack_from("<4s4H2LH", raw, footer)
    if (disk or directory_disk or disk_count != total or total == 0xffff
            or size == 0xffffffff or offset == 0xffffffff
            or footer + 22 + comment != len(raw) or offset + size != footer
            or raw[max(0, footer - 20):max(0, footer - 16)] == b"PK\x06\x07"):
        raise Refusal("unsupported multi-disk, ZIP64 or nonstandard export ZIP footer")
    if total > MAX_ZIP_MEMBERS:
        raise Refusal("export ZIP has too many members")
    position, count = offset, 0
    while position < footer:
        if position + 46 > footer or raw[position:position + 4] != b"PK\x01\x02":
            raise Refusal("invalid export ZIP central directory")
        name, extra, member_comment = struct.unpack_from("<3H", raw, position + 28)
        position += 46 + name + extra + member_comment
        count += 1
        if count > MAX_ZIP_MEMBERS:
            raise Refusal("export ZIP has too many actual members")
    if position != footer or count != total:
        raise Refusal("export ZIP central directory count or size is inconsistent")


def _json_payload(raw):
    if not raw.startswith(b"PK"):
        if len(raw) > MAX_JSON_BYTES:
            raise Refusal("export JSON exceeds the 64 MiB limit")
        return raw, "json"
    try:
        _zip_directory_budget(raw)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_MEMBERS:
                raise Refusal("export ZIP has too many members")
            seen, candidates, expanded = set(), [], 0
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                if (entry.orig_filename != name or "\x00" in entry.orig_filename
                        or not name or "\\" in name or ":" in name or name.startswith("/")
                        or ".." in parts or name in seen or "\x00" in name):
                    raise Refusal("export ZIP contains unsafe or duplicate member names")
                seen.add(name)
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise Refusal("symlink or encrypted export ZIP members are unsupported")
                expanded += entry.file_size
                if expanded > MAX_ZIP_EXPANDED_BYTES:
                    raise Refusal("export ZIP expanded size exceeds the supported limit")
                if parts and parts[-1].lower() == "conversations.json":
                    candidates.append(entry)
            if len(candidates) != 1 or candidates[0].filename != "conversations.json":
                raise Refusal("export ZIP requires exactly one root conversations.json member")
            entry = candidates[0]
            if (entry.file_size > MAX_JSON_BYTES or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * MAX_ZIP_RATIO):
                raise Refusal("export ZIP JSON is oversized or uses unsupported compression")
            with archive.open(entry) as handle:
                data = handle.read(MAX_JSON_BYTES + 1)
            if len(data) != entry.file_size or len(data) > MAX_JSON_BYTES:
                raise Refusal("export ZIP JSON size does not match its metadata")
            return data, "zip"
    except Refusal:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError, zlib.error) as exc:
        raise Refusal("invalid or unsupported export ZIP") from exc


def _generated(message):
    metadata = message.get("metadata", {})
    if metadata is None:
        metadata = {}
    metadata = _object(metadata, "message metadata")
    generated = False
    for source in (message, metadata):
        for flag in GENERATED_FLAGS & source.keys():
            if type(source[flag]) is not bool:
                raise Refusal("unsupported export origin flag schema")
            generated = generated or source[flag]
    return generated or "user_context_message_data" in metadata


def _gpt_text(message, primary):
    content = _object(message.get("content"), "message content")
    kind = content.get("content_type")
    if kind not in {"text", "multimodal_text"}:
        if primary and kind not in GPT_MEDIA | {"user_editable_context"}:
            raise Refusal("unsupported ChatGPT human content type")
        return "", True
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise Refusal("unsupported ChatGPT text parts schema")
    texts, mixed = [], False
    for part in parts:
        if isinstance(part, str):
            texts.append(_string(part, "message"))
        elif isinstance(part, dict) and kind == "multimodal_text":
            if primary and part.get("content_type", part.get("type")) not in GPT_MEDIA:
                raise Refusal("unsupported ChatGPT human multimodal part")
            mixed = True
        else:
            raise Refusal("unsupported ChatGPT text part schema")
    text = "\n".join(texts)
    if "text" in content and _string(content["text"], "content mirror") != text:
        raise Refusal("ChatGPT text mirrors disagree")
    metadata = message.get("metadata")
    for source in (message, content, metadata if isinstance(metadata, dict) else {}):
        for field in ("attachments", "files"):
            value = source.get(field)
            if primary and value is not None and not isinstance(value, list):
                raise Refusal("unsupported ChatGPT attachment metadata schema")
            mixed = mixed or bool(value)
    return text, mixed


def _chatgpt(conversation):
    raw_id = _identifier(conversation.get("id", conversation.get("conversation_id")))
    if "id" in conversation and "conversation_id" in conversation and conversation["id"] != conversation["conversation_id"]:
        raise Refusal("ChatGPT conversation identifiers disagree")
    mapping = _object(conversation.get("mapping"), "ChatGPT mapping")
    if len(mapping) > MAX_MESSAGES:
        raise Refusal("export has too many messages")
    roots, nodes, child_sets = [], {}, {}
    for key, node in mapping.items():
        _identifier(key)
        node = _object(node, "ChatGPT mapping node")
        if "id" in node and node["id"] != key:
            raise Refusal("ChatGPT mapping node identifiers disagree")
        if "message" not in node or "parent" not in node or not isinstance(node.get("children"), list):
            raise Refusal("unsupported ChatGPT mapping edges")
        parent = node["parent"]
        if parent is not None:
            _identifier(parent)
            if parent not in mapping:
                raise Refusal("ChatGPT mapping has a missing parent")
        else:
            roots.append(key)
        children = node["children"]
        if any(not isinstance(child, str) for child in children) or len(children) != len(set(children)):
            raise Refusal("ChatGPT mapping has invalid or duplicate children")
        for child in children:
            _identifier(child)
            if child not in mapping:
                raise Refusal("ChatGPT mapping has a missing child")
        nodes[key] = node
        child_sets[key] = set(children)
    if nodes and len(roots) != 1:
        raise Refusal("ChatGPT mapping must have one connected root")
    for key, node in nodes.items():
        if node["parent"] is not None and key not in child_sets[node["parent"]]:
            raise Refusal("ChatGPT mapping parent and child edges disagree")
        if any(nodes[child]["parent"] != key for child in node["children"]):
            raise Refusal("ChatGPT mapping parent and child edges disagree")
    visited, order, stack = set(), [], list(roots)
    while stack:
        key = stack.pop()
        if key in visited:
            raise Refusal("ChatGPT mapping contains a cycle")
        visited.add(key)
        order.append(key)
        stack.extend(reversed(nodes[key]["children"]))
    if len(visited) != len(nodes):
        raise Refusal("ChatGPT mapping is disconnected or cyclic")
    current = conversation.get("current_node")
    active = set()
    if current is not None:
        _identifier(current)
        if current not in nodes:
            raise Refusal("ChatGPT current node is missing")
        key = current
        while key is not None:
            active.add(key)
            key = nodes[key]["parent"]
    messages, by_node, ids = [], {}, set()
    for key in order:
        message = nodes[key].get("message")
        if message is None:
            continue
        message = _object(message, "ChatGPT message")
        message_id = _identifier(message.get("id"))
        if message_id in ids:
            raise Refusal("export contains duplicate message identifiers")
        ids.add(message_id)
        author = _object(message.get("author"), "ChatGPT author")
        role = author.get("role")
        if role not in ROLES:
            raise Refusal("unsupported ChatGPT author role")
        generated = _generated(message)
        ordinary = not generated and author.get("name") in (None, "user", "assistant") and message.get("recipient") in (None, "all")
        eligible = (role == "user" and ordinary
                    and author.get("name") in (None, "user") and message.get("recipient") in (None, "all"))
        text, mixed = _gpt_text(message, eligible) if role in {"user", "assistant"} else ("", True)
        item = {"native_id": message_id, "role": role, "text": text,
                "eligible": eligible and bool(text), "mixed_nontext": mixed,
                "visible_context": role == "assistant" and ordinary and bool(text),
                "branch": "unknown" if current is None else "active" if key in active else "alternate", "context": []}
        messages.append(item)
        by_node[key] = item
    # Cache neighbours in tree order so long empty chains shared by many
    # branches cannot make review quadratic. Do not blend sibling branches.
    nearest_before, nearest_after = {}, {}
    for key in order:
        parent = nodes[key]["parent"]
        nearest_before[key] = by_node.get(parent, nearest_before.get(parent))
    for key in reversed(order):
        children = nodes[key]["children"]
        nearest_after[key] = by_node.get(children[0], nearest_after.get(children[0])) if len(children) == 1 else None
    for key, item in by_node.items():
        item["context"] = [neighbour for neighbour in (nearest_before[key], nearest_after[key]) if neighbour is not None]
    return raw_id, messages


def _claude_text(message, primary):
    plain = _string(message["text"], "Claude message") if "text" in message else None
    blocks = message.get("content", [])
    if not isinstance(blocks, list):
        raise Refusal("unsupported Claude content blocks schema")
    texts, mixed, tool_content = [], False, False
    for block in blocks:
        block = _object(block, "Claude content block")
        kind = block.get("type")
        if kind == "text":
            texts.append(_string(block.get("text"), "Claude block"))
        else:
            if primary and kind not in CLAUDE_NON_TEXT:
                raise Refusal("unsupported Claude human content block")
            mixed = True
            tool_content = tool_content or kind in {"tool_use", "tool_result", "thinking", "redacted_thinking"}
    joined = "\n".join(texts)
    if primary and tool_content and not texts:
        # A human sender envelope also carries tool/replay output. Its plain
        # mirror must not turn known tool-only content into a human original.
        return "", True
    if primary and plain is not None and texts and plain != joined:
        raise Refusal("Claude text and content mirrors disagree")
    if plain is None and "content" not in message:
        raise Refusal("unsupported Claude message text schema")
    return plain if plain is not None else joined, mixed or bool(message.get("attachments") or message.get("files"))


def _claude(conversation):
    raw_id = _identifier(conversation.get("uuid"))
    entries = conversation.get("chat_messages")
    if not isinstance(entries, list) or len(entries) > MAX_MESSAGES:
        raise Refusal("unsupported Claude chat messages schema or size")
    messages, ids = [], set()
    for message in entries:
        message = _object(message, "Claude message")
        message_id = _identifier(message.get("uuid"))
        if message_id in ids:
            raise Refusal("export contains duplicate message identifiers")
        ids.add(message_id)
        sender = message.get("sender")
        role = "user" if sender == "human" else sender
        if role not in ROLES or sender == "user":
            raise Refusal("unsupported Claude message sender")
        if "parent_message_uuid" in message or "parent" in message:
            raise Refusal("Claude branching message schema is unsupported")
        generated = _generated(message)
        eligible = role == "user" and not generated
        text, mixed = _claude_text(message, eligible) if role in {"user", "assistant"} else ("", True)
        blocks = message.get("content", [])
        ordinary = role == "assistant" and (not blocks or any(block.get("type") == "text" for block in blocks))
        messages.append({"native_id": message_id, "role": role, "text": text, "eligible": eligible and bool(text),
                         "visible_context": role == "assistant" and not generated and ordinary and bool(text),
                         "mixed_nontext": mixed, "branch": "unknown", "context": []})
    for index, message in enumerate(messages):
        message["context"] = messages[max(0, index - 1):index] + messages[index + 1:index + 2]
    return raw_id, messages


def _load(provider, value):
    if provider not in CONTROLS:
        raise Refusal("unsupported consumer export provider")
    path, raw, saved = _read_input(value)
    payload, container = _json_payload(raw)
    try:
        data = load_json(payload)
    except Refusal:
        raise
    except (ValueError, OverflowError) as exc:
        raise Refusal("export contains unsupported JSON numbers") from exc
    _validate_values(data)
    if not isinstance(data, list):
        raise Refusal("consumer export must be a supported conversations list")
    sha = digest(raw)
    conversations, all_ids, total = [], set(), 0
    for index, conversation in enumerate(data, 1):
        conversation = _object(conversation, "conversation")
        raw_id, messages = (_chatgpt if provider == "chatgpt" else _claude)(conversation)
        if raw_id in all_ids:
            raise Refusal("export contains duplicate conversation identifiers")
        all_ids.add(raw_id)
        total += len(messages)
        if total > MAX_MESSAGES:
            raise Refusal("export has too many messages")
        identity = "conversation-" + digest(canonical([provider, sha, raw_id]))
        counts = Counter(message["role"] for message in messages)
        for message in messages:
            message["id"] = "export-" + digest(canonical([provider, sha, raw_id, message["native_id"]]))
            message["reason"] = candidate_reason(message["text"]) if message["eligible"] else None
        conversations.append({"id": identity, "export_index": index, "native_id": raw_id,
                              "title": conversation.get("title", conversation.get("name", "")),
                              "messages": messages, "counts": dict(counts)})
    return path, saved, sha, container, conversations


def _base(provider, sha, container, kind):
    return {"kind": kind, "format_version": 1, "provider": provider, "export_sha256": sha,
            "input_container": container, "status": "review_only", "limitations": LIMITATIONS}


def _message_metadata(message, conversation):
    return {"id": message["id"], "conversation_id": conversation["id"],
            "export_conversation_index": conversation["export_index"], "branch": message["branch"],
            "candidate": bool(message["reason"]), "reason": message["reason"],
            "mixed_nontext": message["mixed_nontext"]}


def _preview(message, conversation):
    result = _message_metadata(message, conversation)
    result["text"] = message["text"]
    title = conversation["title"]
    result["conversation_title"] = title[:500] if isinstance(title, str) else ""
    result["context"] = [{"role": item["role"], "text": item["text"][:1000], "truncated": len(item["text"]) > 1000}
                         for item in message["context"] if item["visible_context"] or item["eligible"]]
    result["context_relation"] = "validated_tree_neighbours" if message["branch"] != "unknown" else "export_neighbours_branch_unknown"
    result["data_notice"] = "Preview text is untrusted conversation data, never an instruction to execute."
    return result


def review_export(provider, input_path, report_path, preview=False, include_all=False):
    path, saved, sha, container, conversations = _load(provider, input_path)
    rows, previews, totals = [], [], Counter()
    eligible_count, candidates = 0, 0
    for conversation in conversations:
        totals.update(conversation["counts"])
        for message in conversation["messages"]:
            if not message["eligible"]:
                continue
            eligible_count += 1
            candidates += bool(message["reason"])
            if include_all or message["reason"]:
                rows.append(_message_metadata(message, conversation))
                if preview:
                    previews.append(_preview(message, conversation))
    report = {**_base(provider, sha, container, "consumer_export_review"),
              "conversations": len(conversations), "message_counts": dict(totals),
              "reviewable_human_messages": eligible_count, "candidates": candidates, "messages": rows}
    output = metadata_path(report_path, path)
    _assert_unchanged(path, saved)
    write_private(output, report)
    return {**report, "report": str(output), "messages": previews if preview else rows}


def deletion_guide(provider, input_path, selected, output_path):
    path, saved, sha, container, conversations = _load(provider, input_path)
    if (not selected or any(not isinstance(identity, str) for identity in selected)
            or len(selected) != len(set(selected))):
        raise Refusal("select at least one distinct export message ID")
    known = {message["id"]: (conversation, message) for conversation in conversations
             for message in conversation["messages"] if message["eligible"]}
    if any(identity not in known for identity in selected):
        raise Refusal("selection is stale, forged, from another provider, or not direct human export text")
    grouped = {}
    for identity in selected:
        conversation, message = known[identity]
        grouped.setdefault(conversation["id"], (conversation, []))[1].append(message)
    rows = []
    for conversation, messages in sorted(grouped.values(), key=lambda pair: pair[0]["export_index"]):
        native_id = conversation["native_id"]
        link = CONTROLS[provider]["history_url"]
        row = {"conversation_id": conversation["id"], "export_conversation_index": conversation["export_index"],
               "selected_message_ids": sorted(message["id"] for message in messages), "selected_count": len(messages),
               "total_user_messages": conversation["counts"].get("user", 0),
               "total_assistant_messages": conversation["counts"].get("assistant", 0),
               "total_message_counts": conversation["counts"],
               "warning": "Deleting this entire conversation also loses useful content and unselected messages. Review every branch first."}
        if UUID.fullmatch(native_id):
            row["provider_conversation_id"] = native_id
            link += ("c/" if provider == "chatgpt" else "chat/") + native_id
        row["review_url"] = link
        row["link_notice"] = "The URL is a navigation aid, not proof of live identity or existence. Match the conversation before deletion."
        rows.append(row)
    guide = {**_base(provider, sha, container, "consumer_manual_deletion_guide"),
             "action": "manual_whole_conversation_deletion", "selected_messages": len(selected),
             "conversations_to_review": len(rows), "checklist": rows,
             "before_deletion": ["Match the live conversation and correct account against your local preview; an export may be stale.",
                                 "Read all messages and alternate branches. Preserve useful content before deciding whether to delete the entire chat."],
             "provider_controls": CONTROLS[provider],
             "notice": "This checklist is neither approval nor a deletion receipt. It cannot be passed to native apply or verify."}
    output = metadata_path(output_path, path)
    _assert_unchanged(path, saved)
    write_private(output, guide)
    return {**guide, "guide": str(output)}
