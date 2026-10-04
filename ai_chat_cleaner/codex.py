"""Read-only Codex format adapter. Return paths; never write files or classify text.

Shapes verified against openai/codex rust-v0.154.0 and local structural metadata.
The engine must include copy_only groups only when their exact text matches a
submitted human original. Subagent/fork source metadata is handled by the engine.
"""
from .storage import Refusal


ROOT_METADATA = {
    "session_meta", "turn_context", "world_state", "token_usage_record",
    "inter_agent_communication_metadata", "security_risk_score",
}
RESPONSE_OTHER = {
    "additional_tools", "agent_message", "reasoning", "function_call",
    "function_call_output", "custom_tool_call", "custom_tool_call_output",
    "web_search_call", "file_search_call", "computer_call", "computer_call_output",
    "image_generation_call", "local_shell_call", "local_shell_call_output",
    "shell_call", "shell_call_output", "tool_search_call", "tool_search_output",
    "compaction", "audio", "output_audio", "refusal",
}
UI_OTHER = {
    "AgentMessage", "CommandExecution", "Reasoning", "SubAgentActivity",
    "McpToolCall", "ContextCompaction", "FileChange", "ImageView",
    "CollabAgentToolCall", "Extension", "WebSearch", "FunctionCallOutput", "Plan",
    "HookPrompt", "ImageGeneration", "EnteredReviewMode", "ExitedReviewMode",
    "agentMessage", "commandExecution", "reasoning", "subAgentActivity",
    "mcpToolCall", "contextCompaction", "fileChange", "imageView",
    "collabAgentToolCall", "extension", "webSearch", "functionCallOutput", "plan",
    "hookPrompt", "imageGeneration", "enteredReviewMode", "exitedReviewMode",
}
EVENT_OTHER = {
    "task_started", "task_complete", "turn_aborted", "token_count",
    "thread_settings_applied", "thread_goal_updated", "agent_message",
    "agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break",
    "agent_reasoning_raw_content_section_break", "agent_message_delta",
    "agent_reasoning_delta", "agent_reasoning_raw_content_delta", "error", "warning",
    "session_configured", "undo_started", "undo_completed", "background_event",
    "stream_error", "model_reroute", "turn_diff", "plan_update", "shutdown_complete",
    "mcp_startup_update", "mcp_startup_complete", "mcp_tool_call_begin", "mcp_tool_call_end",
    "exec_command_begin", "exec_command_output_delta", "exec_command_end",
    "terminal_interaction", "apply_patch_approval_request", "exec_approval_request",
    "request_user_input", "dynamic_tool_call_request", "apply_patch_begin", "apply_patch_end",
    "web_search_begin", "web_search_end", "view_image_tool_call", "collab_agent_spawn_begin",
    "collab_agent_spawn_end", "collab_agent_interaction_begin", "collab_agent_interaction_end",
    "collab_waiting_begin", "collab_waiting_end", "collab_close_begin", "collab_close_end",
}
INJECTED_KINDS = {
    "agents_md.instructions", "environments.environment_context",
    "skills.selected_skill_instructions", "additional_content.codex_apps_open_page",
    "plugins.recommendations", "shell.user_command", "user.heartbeat", "goal.internal_context",
    # Older metadata could not identify a content block's origin. Preserve it.
    "unknown",
}
MEDIA_KINDS = {"user.image", "user.audio", "user.local_image", "user.local_audio"}
WRAPPER_PREFIXES = (
    "# AGENTS.md instructions", "<environment_context>", "<user_instructions>",
    "<instructions>", "<skill>", "<external_codex_apps_open_page>",
    "<permissions instructions>", "<developer_instructions>", "<system_reminder>",
)
AST_CONTAINERS = {
    "doc", "paragraph", "list", "regular_list_item", "bulletList", "orderedList",
    "listItem", "bullet_list", "ordered_list", "list_item", "heading", "blockquote",
    "codeBlock", "code_block",
}


def _object(value, description):
    if not isinstance(value, dict):
        raise Refusal("unknown Codex " + description + " schema")
    return value


def _text(value, description):
    if not isinstance(value, str):
        raise Refusal("unknown Codex " + description + " text schema")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Refusal("Codex text contains unsupported Unicode surrogates") from exc
    return value


def _group(paths, copy_only, text=None):
    group = {"paths": paths, "copy_only": copy_only}
    if text is not None:
        group["text"] = text
    return [group] if paths else []


def _wrapper(text):
    return text.lstrip().startswith(WRAPPER_PREFIXES)


def _response(item, prefix, copy_only):
    item = _object(item, "response item")
    kind = item.get("type")
    if not isinstance(kind, str):
        raise Refusal("unknown Codex response item type")
    if kind in RESPONSE_OTHER:
        return []
    if kind != "message":
        raise Refusal("unknown Codex response item type")
    role = item.get("role")
    if role in {"assistant", "developer", "system", "tool"}:
        return []
    if role != "user" or not isinstance(item.get("content"), list):
        raise Refusal("unknown Codex user message schema")
    content = item["content"]
    metadata = item.get("internal_chat_message_metadata_passthrough")
    kinds = None
    if metadata is not None:
        metadata = _object(metadata, "message metadata")
        kinds = metadata.get("content_item_kinds")
        if kinds is not None and (not isinstance(kinds, list) or len(kinds) != len(content)):
            raise Refusal("Codex content kind metadata does not align with content")
    paths = []
    for index, block in enumerate(content):
        block = _object(block, "user content block")
        block_type = block.get("type")
        label = kinds[index] if kinds is not None else None
        if label is not None and (not isinstance(label, str) or label not in INJECTED_KINDS | MEDIA_KINDS | {"user.text"}):
            raise Refusal("unknown Codex content kind label")
        if block_type == "input_text":
            text = _text(block.get("text"), "input")
            if label in MEDIA_KINDS:
                raise Refusal("Codex text block has media metadata")
            if label not in INJECTED_KINDS and not _wrapper(text):
                paths.append(prefix + ("content", index, "text"))
        elif block_type == "input_image":
            _text(block.get("image_url"), "image URL")
            if label == "user.text":
                raise Refusal("Codex image block has text metadata")
        elif block_type == "input_audio":
            _text(block.get("audio_url"), "audio URL")
            if label == "user.text":
                raise Refusal("Codex audio block has text metadata")
        else:
            raise Refusal("unknown Codex user content block type")
    return _group(paths, copy_only)


def _ui_item(item, prefix):
    item = _object(item, "presentation item")
    kind = item.get("type")
    if not isinstance(kind, str):
        raise Refusal("unknown Codex presentation item type")
    if kind in UI_OTHER:
        return []
    if kind in {"UserMessage", "userMessage", "user_message"}:
        content = item.get("content")
        if not isinstance(content, list):
            raise Refusal("unknown Codex presented user content schema")
        paths = []
        for index, block in enumerate(content):
            block = _object(block, "presented user block")
            block_type = block.get("type")
            if block_type == "text":
                text = _text(block.get("text"), "presented user")
                if not _wrapper(text):
                    paths.append(prefix + ("content", index, "text"))
            elif block_type not in {"image", "local_image", "audio", "local_audio", "skill", "mention"}:
                raise Refusal("unknown Codex presented user block type")
        return _group(paths, True)
    if kind == "message":
        return _response(item, prefix, True)
    if kind == "transcript_segment":
        role = item.get("role")
        if role not in {"user", "assistant"}:
            raise Refusal("unknown Codex realtime transcript role")
        text = _text(item.get("text"), "realtime transcript")
        return _group([prefix + ("text",)], True) if role == "user" and not _wrapper(text) else []
    if kind in {"realtime_session_started", "realtime_session_closed", "bem_item_promoted"}:
        return []
    raise Refusal("unknown Codex presentation item type")


def _retained(context, prefix):
    if context is None:
        return []
    context = _object(context, "retained context")
    users = context.get("user_messages", [])
    if not isinstance(users, list):
        raise Refusal("unknown Codex retained user messages schema")
    groups = []
    for index, message in enumerate(users):
        message = _object(message, "retained user message")
        text = _text(message.get("text"), "retained user message")
        if not _wrapper(text):
            groups += _group([prefix + ("user_messages", index, "text")], True)
    return groups


def _session(record, prefix=(), depth=0):
    if depth > 32:
        raise Refusal("Codex nested history exceeds supported depth")
    record = _object(record, "session record")
    kind = record.get("type")
    if not isinstance(kind, str):
        raise Refusal("unknown Codex session record type")
    payload = _object(record.get("payload"), "record payload")
    at = prefix + ("payload",)
    if kind in ROOT_METADATA:
        return []
    if kind == "response_item":
        return _response(payload, at, False)
    if kind == "inter_agent_communication":
        return []
    if kind == "retained_context":
        # Current native events contain host facts, not original user messages.
        if payload.get("type") == "verified_answer":
            return []
        # Explicit user checkpoint shapes remain exact-match copies only.
        if "user_messages" in payload:
            return _retained(payload, at)
        if "context" in payload:
            return _retained(payload["context"], at + ("context",))
        raise Refusal("unknown Codex retained context event schema")
    if kind == "compacted":
        groups = []
        history = payload.get("replacement_history")
        if history is not None:
            if not isinstance(history, list):
                raise Refusal("unknown Codex replacement history schema")
            metadata = payload.get("replacement_history_metadata")
            if metadata is not None and (not isinstance(metadata, list) or len(metadata) != len(history)):
                raise Refusal("Codex replacement history metadata does not align")
            for index, item in enumerate(history):
                item = _object(item, "replacement item")
                path = at + ("replacement_history", index)
                if item.get("type") in {"compacted", "response_item"} and "payload" in item:
                    nested = _session(item, path, depth + 1)
                    for group in nested:
                        group["copy_only"] = True
                    groups += nested
                else:
                    groups += _response(item, path, True)
        groups += _retained(payload.get("retained_context"), at + ("retained_context",))
        return groups
    if kind == "event_msg":
        event = payload.get("type")
        if not isinstance(event, str):
            raise Refusal("unknown Codex event type")
        if event == "user_message":
            text = _text(payload.get("message"), "user event")
            return _group([at + ("message",)], True) if not _wrapper(text) else []
        if event in {"item_completed", "item_started"}:
            return _ui_item(payload.get("item"), at + ("item",))
        if event == "raw_response_item":
            return _response(payload.get("item"), at + ("item",), True)
        if event in EVENT_OTHER:
            return []
        raise Refusal("unknown Codex event type")
    if kind == "realtime_item":
        return _ui_item(payload, at)
    raise Refusal("unknown Codex session record type")


def _document(document, prefix, depth=0):
    if depth > 32:
        raise Refusal("Codex prompt document exceeds supported depth")
    document = _object(document, "prompt document node")
    kind = document.get("type")
    if kind != "skillMention" and not set(document) <= {"type", "text", "content", "attrs", "marks"}:
        raise Refusal("unknown Codex prompt document fields")
    if kind == "text":
        _text(document.get("text"), "document leaf")
        if "content" in document:
            raise Refusal("Codex text node has unexpected children")
        return [prefix + ("text",)]
    if kind in {"hardBreak", "hard_break"}:
        if "text" in document or "content" in document:
            raise Refusal("Codex break node has unexpected text or children")
        return []
    if kind == "skillMention":
        # An inline skill selection is metadata, not a human text leaf.
        attrs = document.get("attrs")
        known = {"brandColor", "description", "displayName", "iconSmall", "name",
                 "path", "promptLinkLabel", "skillIcon"}
        if (set(document) != {"type", "attrs"} or not isinstance(attrs, dict)
                or not set(attrs) <= known or not {"name", "path"} <= set(attrs)
                or any(value is not None and not isinstance(value, str) for value in attrs.values())):
            raise Refusal("unknown Codex skill mention document schema")
        return []
    if kind not in AST_CONTAINERS:
        raise Refusal("unknown Codex prompt document node type")
    if "text" in document:
        raise Refusal("Codex container node has unexpected text")
    children = document.get("content", [])
    if not isinstance(children, list):
        raise Refusal("unknown Codex prompt document children schema")
    paths = []
    for index, child in enumerate(children):
        paths += _document(child, prefix + ("content", index), depth + 1)
    return paths


def _reference_document(value, prefix, reference):
    text = _text(value.get(reference), "prompt reference")
    paths = [prefix + (reference,)]
    if "document" in value:
        leaves = _document(value["document"], prefix + ("document",))
        if _plain_document(value["document"]) != text:
            # Never infer that a paired document is safe from a reference hash.
            # The engine must refuse selection of this exact mirrored reference.
            return [{"paths": paths, "copy_only": True, "text": text,
                     "reference_mismatch": True}]
        paths += leaves
    return _group(paths, True, text)


def _plain_document(document):
    """Conservative exact plain projection; uncertain rich rendering is blocked."""
    kind = document["type"]
    if kind == "text":
        return document["text"]
    if kind in {"hardBreak", "hard_break"}:
        return "\n"
    if kind == "skillMention":
        return None
    parts = [_plain_document(child) for child in document.get("content", [])]
    if any(part is None for part in parts):
        return None
    separator = "" if kind in {"paragraph", "heading", "codeBlock", "code_block"} else "\n"
    return separator.join(parts)


def _global(record):
    atoms = record.get("electron-persisted-atom-state", {})
    atoms = _object(atoms, "desktop atom state")
    groups = []
    for field in ("prompt-history", "composer-prompt-drafts-v1", "composer-prompt-drafts-v2",
                  "composer-retained-documents-v1", "thread-descriptions-v1"):
        slots = atoms.get(field, {})
        slots = _object(slots, "desktop prompt slots")
        for key, value in slots.items():
            prefix = ("electron-persisted-atom-state", field, key)
            if field == "prompt-history":
                if not isinstance(value, list):
                    raise Refusal("unknown Codex desktop prompt history schema")
                for index, entry in enumerate(value):
                    path = prefix + (index,)
                    if isinstance(entry, str):
                        groups += _group([path], True, _text(entry, "desktop history"))
                    else:
                        entry = _object(entry, "desktop rich prompt history")
                        if set(entry) != {"markdown", "document"}:
                            raise Refusal("unknown Codex rich history fields")
                        groups += _reference_document(entry, path, "markdown")
            elif field == "composer-retained-documents-v1":
                value = _object(value, "retained prompt document")
                if (not set(value) <= {"prompt", "document", "plainTextMode"}
                        or "plainTextMode" in value and type(value["plainTextMode"]) is not bool):
                    raise Refusal("unknown Codex retained prompt fields")
                if "document" not in value:
                    raise Refusal("retained prompt is missing its document")
                groups += _reference_document(value, prefix, "prompt")
            elif isinstance(value, str):
                groups += _group([prefix], True, _text(value, "desktop draft"))
            elif field == "composer-prompt-drafts-v2":
                value = _object(value, "desktop draft")
                if (not set(value) <= {"prompt", "pullRequestChecks", "primaryReviewFor"}
                        or "pullRequestChecks" in value and not isinstance(value["pullRequestChecks"], list)
                        or value.get("primaryReviewFor") is not None):
                    raise Refusal("unknown Codex rich draft fields")
                prompt = value.get("prompt")
                if isinstance(prompt, str):
                    groups += _group([prefix + ("prompt",)], True, _text(prompt, "desktop draft"))
                elif isinstance(prompt, dict) and "document" in prompt:
                    if (not set(prompt) <= {"document", "plainTextMode"}
                            or "plainTextMode" in prompt and type(prompt["plainTextMode"]) is not bool):
                        raise Refusal("unknown Codex rich draft document fields")
                    # Document-only unsent drafts cannot be compared faithfully with
                    # submitted markdown. Validate them, then preserve their text.
                    _document(prompt["document"], prefix + ("prompt", "document"))
                else:
                    raise Refusal("unknown Codex rich draft prompt schema")
            else:
                raise Refusal("unknown Codex desktop draft schema")
    return groups


def record_prompts(record, kind):
    """Return independent prompt groups with JSON string paths and origin flags.

    Kind: session, history, index, item (SQLite presentation), global or queue.
    A group's optional text is the full reference for companion document paths.
    All mirrors and drafts must be matched to an original by the calling engine.
    """
    if kind == "queue":
        if record in ({}, []):
            return []
        raise Refusal("nonempty Codex queued operations require a supported schema; drain queues before cleanup")
    record = _object(record, "record")
    if kind == "session":
        return _session(record)
    if kind == "history":
        _text(record.get("session_id"), "history session identifier")
        if type(record.get("ts")) is not int or record["ts"] < 0:
            raise Refusal("unknown Codex history timestamp schema")
        text = _text(record.get("text"), "history")
        return _group([("text",)], False) if not _wrapper(text) else []
    if kind == "index":
        _text(record.get("id"), "index identifier")
        _text(record.get("updated_at"), "index timestamp")
        text = _text(record.get("thread_name"), "index thread name")
        return _group([("thread_name",)], True, text)
    if kind == "item":
        return _ui_item(record, ())
    if kind == "global":
        return _global(record)
    raise Refusal("unknown Codex input format")
