"""Codex adapter tests use manufactured records, never operator transcripts."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from ai_chat_cleaner import core
from ai_chat_cleaner.codex import record_prompts
from ai_chat_cleaner.storage import Refusal, canonical, string_spans


def response(text="Synthetic human input.", role="user", metadata=None):
    item = {"type": "message", "id": "synthetic-message", "role": role,
            "content": [{"type": "input_text", "text": text}]}
    if metadata is not None:
        item["internal_chat_message_metadata_passthrough"] = metadata
    return item


def rollout(kind, payload):
    return {"timestamp": "2026-10-03T00:00:00Z", "ordinal": 12, "type": kind, "payload": payload}


def at(record, path):
    for key in path:
        record = record[key]
    return record


def document(*texts):
    return {"type": "doc", "content": [
        {"type": "paragraph", "attrs": {"synthetic": True},
         "content": [{"type": "text", "text": text, "marks": [{"type": "bold"}]}]}
        for text in texts]}


class CodexAdapterTests(unittest.TestCase):
    def test_direct_user_primary_and_input_text_groups(self):
        record = rollout("response_item", response())
        self.assertEqual(record_prompts(record, "session"), [
            {"paths": [("payload", "content", 0, "text")], "copy_only": False}])

    def test_multiple_text_blocks_are_one_prompt_preserving_media(self):
        item = response("First part.")
        item["content"] += [{"type": "input_image", "image_url": "data:image/png;synthetic"},
                            {"type": "input_audio", "audio_url": "data:audio/wav;synthetic"},
                            {"type": "input_text", "text": "Second part."}]
        record = rollout("response_item", item)
        groups = record_prompts(record, "session")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["paths"], [("payload", "content", 0, "text"),
                                              ("payload", "content", 3, "text")])

    def test_injected_metadata_kinds_are_not_human(self):
        kinds = ["agents_md.instructions", "environments.environment_context",
                 "skills.selected_skill_instructions", "additional_content.codex_apps_open_page",
                 "plugins.recommendations", "shell.user_command", "user.heartbeat", "goal.internal_context"]
        kinds.append("unknown")
        for label in kinds:
            with self.subTest(label=label):
                record = rollout("response_item", response(metadata={"content_item_kinds": [label]}))
                self.assertEqual(record_prompts(record, "session"), [])

    def test_mixed_injected_and_user_content_preserves_wrapper(self):
        item = response("# AGENTS.md instructions for synthetic-root")
        item["content"] += [{"type": "input_text", "text": "Actual synthetic human request."}]
        item["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": ["agents_md.instructions", "user.text"]}
        groups = record_prompts(rollout("response_item", item), "session")
        self.assertEqual(groups[0]["paths"], [("payload", "content", 1, "text")])

    def test_wrapper_fallback_preserves_legacy_injections(self):
        for text in ["# AGENTS.md instructions for synthetic-root", "<environment_context>synthetic</environment_context>",
                     "<skill>synthetic instructions</skill>", "<user_instructions>synthetic</user_instructions>"]:
            with self.subTest(wrapper=text):
                self.assertEqual(record_prompts(rollout("response_item", response(text)), "session"), [])

    def test_other_roles_and_tools_are_not_human(self):
        for role in ["assistant", "developer", "system", "tool"]:
            self.assertEqual(record_prompts(rollout("response_item", response(role=role)), "session"), [])
        for kind in ["function_call", "function_call_output", "custom_tool_call_output", "agent_message", "reasoning", "compaction"]:
            self.assertEqual(record_prompts(rollout("response_item", {"type": kind, "text": "Synthetic input."}), "session"), [])

    def test_unknown_human_shapes_and_metadata_fail_closed(self):
        variants = []
        item = response(); item["content"] = "unexpected string"; variants.append(item)
        item = response(); item["content"] = [{"type": "output_text", "text": "Synthetic"}]; variants.append(item)
        item = response(); item["content"] = [{"type": "future_text", "text": "Synthetic"}]; variants.append(item)
        item = response(); item["role"] = "future_human_role"; variants.append(item)
        variants += [response(metadata={"content_item_kinds": []}),
                     response(metadata={"content_item_kinds": "user.text"}),
                     response(metadata={"content_item_kinds": ["future.user_text"]}),
                     response(metadata={"content_item_kinds": ["user.image"]}),
                     response(metadata=[])]
        for item in variants:
            with self.subTest(schema=item):
                with self.assertRaises(Refusal):
                    record_prompts(rollout("response_item", item), "session")

    def test_image_text_metadata_mismatch_refuses(self):
        item = response(metadata={"content_item_kinds": ["user.text"]})
        item["content"] = [{"type": "input_image", "image_url": "synthetic"}]
        with self.assertRaises(Refusal):
            record_prompts(rollout("response_item", item), "session")

    def test_legacy_user_event_is_copy_only(self):
        record = rollout("event_msg", {"type": "user_message", "message": "Synthetic human input.", "images": []})
        self.assertEqual(record_prompts(record, "session"), [
            {"paths": [("payload", "message")], "copy_only": True}])

    def test_native_presentation_user_messages_are_copies(self):
        for kind in ["UserMessage", "userMessage", "user_message"]:
            item = {"type": kind, "id": "synthetic", "content": [
                {"type": "text", "text": "Synthetic human input.", "text_elements": []},
                {"type": "local_image", "path": "synthetic-image.png"}]}
            self.assertEqual(record_prompts(item, "item"), [
                {"paths": [("content", 0, "text")], "copy_only": True}])
            record = rollout("event_msg", {"type": "item_completed", "item": item})
            groups = record_prompts(record, "session")
            self.assertEqual(groups[0]["paths"], [("payload", "item", "content", 0, "text")])
            self.assertTrue(groups[0]["copy_only"])

    def test_presentation_tools_and_assistant_items_are_preserved(self):
        for kind in ["AgentMessage", "CommandExecution", "Reasoning", "McpToolCall", "ContextCompaction", "Extension"]:
            item = {"type": kind, "text": "Synthetic human input."}
            self.assertEqual(record_prompts(item, "item"), [])
            self.assertEqual(record_prompts(rollout("event_msg", {"type": "item_completed", "item": item}), "session"), [])

    def test_unknown_event_and_presentation_schema_refuse(self):
        for record, kind in [
            (rollout("event_msg", {"type": "future_user_event", "text": "Synthetic"}), "session"),
            (rollout("event_msg", {"type": "user_message", "message": {"text": "Synthetic"}}), "session"),
            ({"type": "userMessage", "content": [{"type": "future", "text": "Synthetic"}]}, "item"),
            ({"type": "futureUserMessage", "content": []}, "item"),
        ]:
            with self.subTest(kind=kind), self.assertRaises(Refusal):
                record_prompts(record, kind)

    def test_compacted_messages_are_separate_groups_on_same_line(self):
        record = rollout("compacted", {"message": "Synthetic summary remains.",
                                      "replacement_history": [response("One synthetic prompt."),
                                                              response("Two synthetic prompt."),
                                                              response("Synthetic assistant reply.", role="assistant")]})
        groups = record_prompts(record, "session")
        self.assertEqual(len(groups), 2)
        self.assertEqual([at(record, g["paths"][0]) for g in groups], ["One synthetic prompt.", "Two synthetic prompt."])
        self.assertTrue(all(g["copy_only"] for g in groups))
        self.assertEqual(groups[1]["paths"], [("payload", "replacement_history", 1, "content", 0, "text")])

    def test_nested_compacted_rollout_copies_are_supported(self):
        nested = rollout("compacted", {"message": "Synthetic summary.", "replacement_history": [response()]})
        record = rollout("compacted", {"message": "Outer summary.", "replacement_history": [nested]})
        groups = record_prompts(record, "session")
        self.assertEqual(groups[0]["paths"], [
            ("payload", "replacement_history", 0, "payload", "replacement_history", 0, "content", 0, "text")])
        self.assertTrue(groups[0]["copy_only"])

    def test_compacted_metadata_mismatch_unknown_variants_refuse(self):
        for payload in [
            {"replacement_history": {}},
            {"replacement_history": [response()], "replacement_history_metadata": []},
            {"replacement_history": [{"type": "future_response", "text": "Synthetic"}]},
            {"retained_context": {"user_messages": "Synthetic"}},
            {"retained_context": {"user_messages": [{"text": {"unexpected": True}}]}},
        ]:
            with self.subTest(payload=payload), self.assertRaises(Refusal):
                record_prompts(rollout("compacted", payload), "session")

    def test_retained_context_exact_copies_preserve_other_facts(self):
        retained = {"user_messages": [{"text": "Synthetic original.", "order": 4, "complete": True}],
                    "assistant_messages": [{"text": "Synthetic assistant remains."}],
                    "verified_answers": [{"text": "Synthetic verified answer remains."}]}
        record = rollout("compacted", {"message": "Synthetic summary.", "retained_context": retained})
        groups = record_prompts(record, "session")
        self.assertEqual(groups, [{"paths": [("payload", "retained_context", "user_messages", 0, "text")], "copy_only": True}])

    def test_native_retained_verified_answer_events_are_preserved(self):
        record = rollout("retained_context", {"type": "verified_answer", "text": "Synthetic verified answer remains."})
        self.assertEqual(record_prompts(record, "session"), [])
        record["payload"]["type"] = "future_user_fact"
        with self.assertRaises(Refusal):
            record_prompts(record, "session")

    def test_realtime_user_transcript_is_copy_not_new_human_source(self):
        item = {"type": "transcript_segment", "role": "user", "text": "Synthetic voice input."}
        self.assertTrue(record_prompts(item, "item")[0]["copy_only"])
        record = rollout("realtime_item", item)
        self.assertEqual(record_prompts(record, "session")[0]["paths"], [("payload", "text")])
        item["role"] = "assistant"
        self.assertEqual(record_prompts(item, "item"), [])

    def test_known_root_metadata_preserved_and_unknown_refuses(self):
        for kind in ["session_meta", "turn_context", "world_state", "token_usage_record", "inter_agent_communication_metadata"]:
            self.assertEqual(record_prompts(rollout(kind, {"text": "Synthetic stays."}), "session"), [])
        with self.assertRaises(Refusal):
            record_prompts(rollout("future_record", {}), "session")

    def test_history_original_index_copy(self):
        original = {"session_id": "synthetic", "ts": 123, "text": "Synthetic human input."}
        self.assertEqual(record_prompts(original, "history"), [{"paths": [("text",)], "copy_only": False}])
        index = {"id": "synthetic", "updated_at": "synthetic-time", "thread_name": original["text"]}
        self.assertEqual(record_prompts(index, "index"), [
            {"paths": [("thread_name",)], "copy_only": True, "text": original["text"]}])

    def test_malformed_history_index_identifiers_or_types_refuse(self):
        for record, kind in [({"text": "Synthetic"}, "history"),
                             ({"session_id": "synthetic", "ts": True, "text": "Synthetic"}, "history"),
                             ({"id": "synthetic", "thread_name": 123, "updated_at": "time"}, "index")]:
            with self.subTest(kind=kind), self.assertRaises(Refusal):
                record_prompts(record, kind)

    def test_surrogate_failures_are_sanitized(self):
        for record, kind in [(rollout("response_item", response("sensitive-synthetic\ud800")), "session"),
                             ({"type": "userMessage", "content": [{"type": "text", "text": "sensitive-synthetic\ud800"}]}, "item")]:
            with self.assertRaises(Refusal) as failure:
                record_prompts(record, kind)
            self.assertNotIn("sensitive-synthetic", str(failure.exception))

    def test_queue_unknown_nonempty_refuses_and_empty_has_no_prompts(self):
        self.assertEqual(record_prompts({}, "queue"), [])
        self.assertEqual(record_prompts([], "queue"), [])
        with self.assertRaises(Refusal):
            record_prompts({"unknown_operation": {"text": "Synthetic"}}, "queue")

    def test_global_plain_history_drafts_are_copy_only(self):
        record = {"electron-persisted-atom-state": {
            "prompt-history": {"synthetic-slot": ["Synthetic submitted."]},
            "composer-prompt-drafts-v1": {"synthetic-draft": "Synthetic unsent."},
            "composer-prompt-drafts-v2": {"synthetic-new": {"prompt": "Synthetic other.", "pullRequestChecks": []}},
            "unrelated-setting": "Synthetic submitted."}}
        groups = record_prompts(record, "global")
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(g["copy_only"] for g in groups))
        self.assertEqual([g["text"] for g in groups], ["Synthetic submitted.", "Synthetic unsent.", "Synthetic other."])

    def test_global_descriptions_are_exact_match_copy_only_metadata(self):
        record = {"electron-persisted-atom-state": {"thread-descriptions-v1": {
            "synthetic": "Synthetic submitted.", "derived": "A generated description."}}}
        groups = record_prompts(record, "global")
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["copy_only"] for group in groups))
        self.assertEqual(groups[0]["paths"], [("electron-persisted-atom-state", "thread-descriptions-v1", "synthetic")])
        record["electron-persisted-atom-state"]["thread-descriptions-v1"]["synthetic"] = {"future": "Synthetic"}
        with self.assertRaises(Refusal):
            record_prompts(record, "global")

    def test_global_rich_history_reference_and_all_document_leaves(self):
        doc = document("Synthetic", " input.")
        doc["content"][0]["content"].append(doc["content"][1]["content"][0])
        doc["content"].pop()
        record = {"electron-persisted-atom-state": {"prompt-history": {
            "synthetic": [{"markdown": "Synthetic input.", "document": doc}]}}}
        groups = record_prompts(record, "global")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["text"], "Synthetic input.")
        self.assertEqual(len(groups[0]["paths"]), 3)
        self.assertTrue(groups[0]["copy_only"])
        self.assertTrue(all(isinstance(at(record, path), str) for path in groups[0]["paths"]))

    def test_rich_history_mismatched_document_blocks_reference_without_leaf_paths(self):
        record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [
            {"markdown": "You are useless.", "document": document("Keep the useful synthetic logo.")}]}}}
        before = copy.deepcopy(record)
        group = record_prompts(record, "global")[0]
        self.assertTrue(group["reference_mismatch"])
        self.assertEqual(group["paths"], [("electron-persisted-atom-state", "prompt-history", "synthetic", 0, "markdown")])
        self.assertEqual(record, before)

    def test_retained_mismatched_document_blocks_reference_without_leaf_paths(self):
        record = {"electron-persisted-atom-state": {"composer-retained-documents-v1": {"synthetic": {
            "prompt": "You are useless.", "document": document("Preserve the useful synthetic original."),
            "plainTextMode": True}}}}
        group = record_prompts(record, "global")[0]
        self.assertTrue(group["reference_mismatch"])
        self.assertEqual(group["paths"], [("electron-persisted-atom-state", "composer-retained-documents-v1", "synthetic", "prompt")])

    def test_rich_markdown_rendering_is_not_guessed_from_substrings(self):
        record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [
            {"markdown": "**Synthetic** input.", "document": document("Synthetic input.")}]}}}
        group = record_prompts(record, "global")[0]
        self.assertTrue(group["reference_mismatch"])
        self.assertEqual(len(group["paths"]), 1)

    def test_exact_multiline_plain_projection_proves_all_leaves(self):
        record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [
            {"markdown": "First synthetic paragraph.\nSecond synthetic paragraph.",
             "document": document("First synthetic paragraph.", "Second synthetic paragraph.")}]}}}
        group = record_prompts(record, "global")[0]
        self.assertNotIn("reference_mismatch", group)
        self.assertEqual(len(group["paths"]), 3)

    def test_global_retained_document_mirrors_preserve_marks_and_settings(self):
        record = {"electron-persisted-atom-state": {"composer-retained-documents-v1": {
            "synthetic": {"prompt": "Synthetic input.", "document": document("Synthetic input."), "plainTextMode": False}}}}
        before = copy.deepcopy(record)
        groups = record_prompts(record, "global")
        self.assertEqual(groups[0]["text"], "Synthetic input.")
        self.assertEqual(len(groups[0]["paths"]), 2)
        self.assertEqual(record, before)

    def test_document_only_unsent_drafts_preserved_without_inferred_match(self):
        record = {"electron-persisted-atom-state": {"composer-prompt-drafts-v2": {
            "synthetic": {"prompt": {"document": document("Synthetic not submitted."), "plainTextMode": False}}}}}
        self.assertEqual(record_prompts(record, "global"), [])

    def test_known_global_prompt_unknown_shapes_fail_closed(self):
        variants = [
            {"prompt-history": {"synthetic": "unexpected scalar"}},
            {"prompt-history": {"synthetic": [{"markdown": "Synthetic", "document": {"type": "future_node"}}]}},
            {"composer-prompt-drafts-v1": {"synthetic": 123}},
            {"composer-prompt-drafts-v2": {"synthetic": {"prompt": {"unknown": "Synthetic"}}}},
            {"composer-prompt-drafts-v2": {"synthetic": {"prompt": "Synthetic", "future_prompt": "Other"}}},
            {"composer-prompt-drafts-v2": {"synthetic": {"prompt": "Synthetic", "pullRequestChecks": {}}}},
            {"composer-prompt-drafts-v2": {"synthetic": {"prompt": {"document": document("Synthetic"), "future_prompt": "Other"}}}},
            {"composer-retained-documents-v1": {"synthetic": {"prompt": "Synthetic"}}},
            {"composer-retained-documents-v1": {"synthetic": {"prompt": "Synthetic", "document": document("Synthetic"), "future_prompt": "Other"}}},
            {"composer-retained-documents-v1": {"synthetic": {"prompt": "Synthetic", "document": document("Synthetic"), "plainTextMode": "yes"}}},
        ]
        for atoms in variants:
            with self.subTest(atoms=atoms), self.assertRaises(Refusal):
                record_prompts({"electron-persisted-atom-state": atoms}, "global")

    def test_list_ast_support_and_unknown_leaf_refuses(self):
        doc = {"type": "doc", "content": [{"type": "list", "attrs": {"ordered": False}, "content": [
            {"type": "regular_list_item", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Synthetic list input."}]}]}]}]}
        record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [{"markdown": "Synthetic list input.", "document": doc}]}}}
        self.assertEqual(len(record_prompts(record, "global")[0]["paths"]), 2)
        doc["content"][0]["content"][0]["content"][0]["content"][0]["type"] = "future_text"
        with self.assertRaises(Refusal):
            record_prompts(record, "global")

    def test_ast_ignorable_variants_cannot_hide_unknown_text_fields(self):
        for node in [
            {"type": "paragraph", "text": "Useful synthetic text.", "content": []},
            {"type": "doc", "future_text": "Useful synthetic text.", "content": []},
            {"type": "hardBreak", "text": "Useful synthetic text."},
            {"type": "text", "text": "Synthetic", "future_text": "Useful synthetic text."},
        ]:
            record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [
                {"markdown": "Synthetic", "document": {"type": "doc", "content": [node]}}]}}}
            with self.subTest(node_type=node["type"]), self.assertRaises(Refusal):
                record_prompts(record, "global")

    def test_skill_mention_metadata_is_preserved_and_unknown_shape_refuses(self):
        mention = {"type": "skillMention", "attrs": {"name": "synthetic-skill", "path": "synthetic-skill.md"}}
        doc = document("Synthetic input.")
        doc["content"][0]["content"].append(mention)
        record = {"electron-persisted-atom-state": {"prompt-history": {"synthetic": [
            {"markdown": "Synthetic input.", "document": doc}]}}}
        before = copy.deepcopy(record)
        groups = record_prompts(record, "global")
        self.assertTrue(groups[0]["reference_mismatch"])
        self.assertEqual(len(groups[0]["paths"]), 1)
        self.assertEqual(record, before)
        mention["content"] = [{"type": "text", "text": "Useful synthetic text."}]
        with self.assertRaises(Refusal):
            record_prompts(record, "global")

    def test_every_returned_path_targets_a_string_with_no_record_mutation(self):
        records = [(rollout("response_item", response()), "session"),
                   (rollout("compacted", {"replacement_history": [response(), response("Other synthetic input.")]}), "session"),
                   ({"id": "synthetic", "updated_at": "time", "thread_name": "Synthetic input."}, "index")]
        for record, kind in records:
            before = copy.deepcopy(record)
            raw = json.dumps(record)
            spans = string_spans(raw)
            for group in record_prompts(record, kind):
                for path in group["paths"]:
                    self.assertIsInstance(at(record, path), str)
                    self.assertIn(path, spans)
                    start, end = spans[path]
                    self.assertEqual(json.loads(raw[start:end]), at(record, path))
            self.assertEqual(record, before)


class CodexEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "codex"
        self.source = self.root / "sessions" / "synthetic.jsonl"
        self.plan = self.base / "plan.json"
        self.receipt = self.base / "receipt.json"

    def tearDown(self):
        self.temp.cleanup()

    def put(self, path, records, *, ascii=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b"".join(json.dumps(record, ensure_ascii=ascii).encode("utf-8") + b"\r\n" for record in records)
        path.write_bytes(raw)
        path.chmod(0o640)
        old = time.time() - core.QUIET_SECONDS - 20
        os.utime(path, (old, old))
        return raw

    def select(self, texts):
        result = core.scan(self.root, source="codex", preview=True, include_all=True)
        identities = [message["id"] for message in result["messages"] if message["text"] in texts
                      and message["file"] == "sessions/synthetic.jsonl" and message["line"] <= len(texts)]
        return core.make_plan(self.root, identities, self.plan, source="codex")

    def test_two_selected_groups_on_one_compacted_line_both_change(self):
        first, second, keep = "You are a useless idiot.", "I hate you.", "Keep the synthetic green logo."
        records = [rollout("response_item", response(first)), rollout("response_item", response(second)),
                   rollout("compacted", {"message": "Synthetic summary remains.", "replacement_history": [
                       response(first), response(second), response(keep)]}),
                   rollout("turn_context", {"synthetic": "Position marker after compacted line."})]
        before = self.put(self.source, records)
        result = self.select({first, second})
        self.assertEqual(result["matching_records"], 4)
        self.assertTrue(core.apply_plan(self.plan, result["confirmation"], self.receipt)["verified"])
        after = self.source.read_bytes()
        decoded = [json.loads(line) for line in after.splitlines()]
        copies = decoded[2]["payload"]["replacement_history"]
        self.assertEqual(copies[0]["content"][0]["text"], "")
        self.assertEqual(copies[1]["content"][0]["text"], "")
        self.assertEqual(copies[2]["content"][0]["text"], keep)
        self.assertEqual(decoded[2]["payload"]["message"], records[2]["payload"]["message"])
        self.assertEqual(len(before), len(after))
        self.assertEqual(before.index(b'Position marker'), after.index(b'Position marker'))
        self.assertEqual(before.splitlines(keepends=True)[-1], after.splitlines(keepends=True)[-1])

    def test_unicode_and_escaped_tokens_preserve_native_byte_positions(self):
        for ascii in (False, True):
            with self.subTest(escaped=ascii):
                short = "Idiot."
                long = "Synthetic Unicode 🇳🇿 café " * 8
                before = self.put(self.source, [rollout("response_item", response(short)),
                                               rollout("response_item", response(long)),
                                               rollout("turn_context", {"marker": "Stable byte offset."})], ascii=ascii)
                plan = self.select({short, long})
                core.apply_plan(self.plan, plan["confirmation"], self.receipt)
                after = self.source.read_bytes()
                decoded = [json.loads(line) for line in after.splitlines()]
                self.assertEqual(decoded[0]["payload"]["content"][0]["text"], "")
                self.assertEqual(decoded[1]["payload"]["content"][0]["text"], core.TOMBSTONE)
                self.assertEqual(len(before), len(after))
                self.assertEqual(before.index(b'Stable byte offset'), after.index(b'Stable byte offset'))
                self.assertEqual(after.count(b"\r\n"), 3)
                self.assertTrue(core.verify(self.receipt)["verified"])
                self.plan.unlink()
                self.receipt.unlink()

    def test_subagent_generated_text_needs_exact_primary_human_match(self):
        self.put(self.source, [rollout("session_meta", {"source": "cli", "thread_source": "user"}),
                               rollout("response_item", response("Keep the synthetic original requirements."))])
        generated = self.root / "sessions" / "subagent.jsonl"
        self.put(generated, [rollout("session_meta", {"source": {"subagent": "review"}, "thread_source": "subagent"}),
                             rollout("response_item", response("You are useless."))])
        result = core.scan(self.root, source="codex", include_all=True)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["human_records"], 1)
        self.assertEqual(result["messages"][0]["file"], "sessions/synthetic.jsonl")


if __name__ == "__main__":
    unittest.main()
