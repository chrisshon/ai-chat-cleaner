"""Manufactured consumer exports only; never read accounts or real transcripts."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ai_chat_cleaner.cli import main
from ai_chat_cleaner import core, exports
from ai_chat_cleaner.exports import deletion_guide, review_export
from ai_chat_cleaner.storage import Refusal, canonical, digest


INSULT = "You are a useless idiot."
USEFUL = "Keep the synthetic green logo."
ASSISTANT = "Synthetic assistant context must remain private."
TITLE = "Synthetic private conversation title."
CONVERSATION = "12345678-1234-1234-1234-123456789abc"


def gpt_message(identity, text, role="user", **extra):
    return {"id": identity, "author": {"role": role}, "content": {"content_type": "text", "parts": [text]}, **extra}


def gpt_conversation():
    return {"id": CONVERSATION, "title": TITLE, "current_node": "u2", "mapping": {
        "root": {"id": "root", "parent": None, "children": ["u1"], "message": None},
        "u1": {"id": "u1", "parent": "root", "children": ["a1"], "message": gpt_message("m-u1", INSULT)},
        "a1": {"id": "a1", "parent": "u1", "children": ["u2", "alternate"], "message": gpt_message("m-a1", ASSISTANT, "assistant")},
        "u2": {"id": "u2", "parent": "a1", "children": [], "message": gpt_message("m-u2", USEFUL)},
        "alternate": {"id": "alternate", "parent": "a1", "children": [], "message": gpt_message("m-alt", "I hate you.")},
    }}


def claude_message(identity, text, sender="human", **extra):
    return {"uuid": identity, "sender": sender, "text": text, **extra}


def claude_conversation():
    return {"uuid": CONVERSATION, "name": TITLE, "chat_messages": [
        claude_message("m-u1", INSULT), claude_message("m-a1", ASSISTANT, "assistant"),
        claude_message("m-u2", USEFUL)]}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.input = self.base / "conversations.json"
        self.report = self.base / "report.json"
        self.guide = self.base / "guide.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, records=None, raw=None, path=None):
        path = path or self.input
        path.write_bytes(raw if raw is not None else canonical(records))
        path.chmod(0o640)
        return path.read_bytes()

    def review(self, provider="chatgpt", **options):
        return review_export(provider, self.input, self.report, **options)

    def assert_private(self, path):
        content = path.read_text()
        for text in (INSULT, USEFUL, ASSISTANT, TITLE):
            self.assertNotIn(text, content)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("text", json.loads(content).get("messages", [{}])[0] if json.loads(content).get("messages") else {})

    def test_chatgpt_review_validates_branches_and_keeps_original_bytes(self):
        original = self.put([gpt_conversation()])
        before = self.input.stat()
        result = self.review(include_all=True)
        self.assertEqual(result["reviewable_human_messages"], 3)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual([row["branch"] for row in result["messages"]], ["active", "active", "alternate"])
        self.assertEqual(result["message_counts"], {"user": 3, "assistant": 1})
        self.assertEqual(result["export_sha256"], digest(original))
        self.assertEqual(self.input.read_bytes(), original)
        after = self.input.stat()
        self.assertEqual((before.st_mode, before.st_mtime_ns, before.st_ctime_ns), (after.st_mode, after.st_mtime_ns, after.st_ctime_ns))
        self.assert_private(self.report)
        self.assertNotIn(INSULT, json.dumps(result))

    def test_explicit_preview_has_prompt_and_proven_local_context_only_in_stdout_result(self):
        self.put([gpt_conversation()])
        result = self.review(preview=True)
        first = result["messages"][0]
        self.assertEqual(first["text"], INSULT)
        self.assertEqual(first["conversation_title"], TITLE)
        self.assertEqual(first["context"], [{"role": "assistant", "text": ASSISTANT, "truncated": False}])
        self.assertEqual(first["context_relation"], "validated_tree_neighbours")
        self.assert_private(self.report)

    def test_hidden_generated_replay_assistant_context_is_not_previewed(self):
        for provider, maker in [("chatgpt", gpt_conversation), ("claude", claude_conversation)]:
            for flag in ("is_visually_hidden_from_conversation", "is_generated", "is_synthetic", "is_replay"):
                record = maker()
                assistant = record["mapping"]["a1"]["message"] if provider == "chatgpt" else record["chat_messages"][1]
                assistant["metadata"] = {flag: True}
                self.put([record])
                with self.subTest(provider=provider, flag=flag):
                    result = self.review(provider, preview=True)
                    self.assertEqual(result["message_counts"]["assistant"], 1)
                    self.assertNotIn(ASSISTANT, json.dumps(result["messages"]))
                    self.assertEqual(result["messages"][0]["context"], [])
                self.report.unlink()

    def test_assistant_tool_only_context_is_not_previewed(self):
        record = claude_conversation()
        record["chat_messages"][1]["content"] = [{"type": "tool_result", "content": ASSISTANT}]
        self.put([record])
        result = self.review("claude", preview=True)
        self.assertEqual(result["messages"][0]["context"], [])
        self.assertEqual(result["message_counts"]["assistant"], 1)

    def test_invalid_known_assistant_origin_flags_refuse(self):
        record = gpt_conversation()
        record["mapping"]["a1"]["message"]["metadata"] = {"is_generated": "false"}
        self.put([record])
        with self.assertRaisesRegex(Refusal, "origin flag"):
            self.review()

    def test_unknown_active_branch_is_labelled_without_guessing(self):
        record = gpt_conversation()
        record.pop("current_node")
        self.put([record])
        self.assertTrue(all(row["branch"] == "unknown" for row in self.review(include_all=True)["messages"]))

    def test_multimodal_direct_text_is_reviewed_without_attachment_or_tool_text(self):
        record = gpt_conversation()
        content = record["mapping"]["u1"]["message"]["content"]
        content["content_type"] = "multimodal_text"
        content["parts"].append({"content_type": "image_asset_pointer", "asset_pointer": INSULT})
        record["mapping"]["u2"]["message"]["author"] = {"role": "tool", "name": "synthetic-tool"}
        self.put([record])
        result = self.review(preview=True, include_all=True)
        self.assertEqual(result["reviewable_human_messages"], 2)
        self.assertEqual(result["messages"][0]["text"], INSULT)
        self.assertTrue(result["messages"][0]["mixed_nontext"])
        self.assertEqual(result["message_counts"]["tool"], 1)

    def test_generated_hidden_user_and_nonhuman_recipient_are_never_selectable(self):
        variants = [{"metadata": {"is_visually_hidden_from_conversation": True}},
                    {"metadata": {"is_user_system_message": True}}, {"is_generated": True},
                    {"metadata": {"user_context_message_data": {"text": INSULT}}},
                    {"recipient": "synthetic-tool"}, {"author": {"role": "user", "name": "synthetic-tool"}}]
        for variant in variants:
            with self.subTest(variant=variant):
                record = gpt_conversation()
                record["mapping"]["u1"]["message"].update(variant)
                self.put([record])
                result = self.review(include_all=True)
                self.assertEqual(result["reviewable_human_messages"], 2)
                self.assertEqual(result["message_counts"]["user"], 3)
                self.report.unlink()

    def test_claude_text_and_content_mirrors_are_not_concatenated(self):
        record = claude_conversation()
        record["chat_messages"][0]["content"] = [{"type": "text", "text": INSULT}, {"type": "image", "source": {"text": INSULT}}]
        self.put([record])
        result = self.review("claude", preview=True)
        self.assertEqual(result["reviewable_human_messages"], 2)
        self.assertEqual(result["messages"][0]["text"], INSULT)
        self.assertTrue(result["messages"][0]["mixed_nontext"])
        self.assertEqual(result["messages"][0]["branch"], "unknown")
        self.assert_private(self.report)

    def test_claude_content_only_text_and_attachments_are_supported(self):
        record = claude_conversation()
        message = record["chat_messages"][0]
        message.pop("text")
        message["content"] = [{"type": "text", "text": INSULT}]
        message["attachments"] = [{"file_name": INSULT, "extracted_content": INSULT}]
        self.put([record])
        result = self.review("claude", preview=True)
        self.assertEqual(result["messages"][0]["text"], INSULT)
        self.assertTrue(result["messages"][0]["mixed_nontext"])

    def test_claude_tool_only_user_envelope_never_becomes_human_text(self):
        for kind in ("tool_result", "tool_use", "thinking", "redacted_thinking"):
            record = claude_conversation()
            record["chat_messages"][0]["content"] = [{"type": kind, "content": "Synthetic tool body."}]
            raw = self.put([record])
            with self.subTest(kind=kind):
                result = self.review("claude", include_all=True)
                self.assertEqual(result["reviewable_human_messages"], 1)
                self.assertEqual(result["candidates"], 0)
                self.assertEqual(result["message_counts"]["user"], 2)
                identity = "export-" + digest(canonical(["claude", digest(raw), CONVERSATION, "m-u1"]))
                with self.assertRaises(Refusal):
                    deletion_guide("claude", self.input, [identity], self.guide)
                self.assertEqual(self.input.read_bytes(), raw)
            self.report.unlink()

    def test_chatgpt_text_attachment_metadata_marks_partial_text_coverage(self):
        for field in ("attachments", "files"):
            record = gpt_conversation()
            record["mapping"]["u1"]["message"]["metadata"] = {field: [{"extracted_text": INSULT}]}
            self.put([record])
            with self.subTest(field=field):
                row = self.review(preview=True)["messages"][0]
                self.assertTrue(row["mixed_nontext"])
                self.assertEqual(row["text"], INSULT)
            self.report.unlink()

    def test_shared_empty_graph_chains_have_linear_context_lookup(self):
        class CountedDict(dict):
            reads = 0
            def __getitem__(self, key):
                CountedDict.reads += 1
                return super().__getitem__(key)
        depth, width = 500, 500
        mapping = {"root": CountedDict(parent=None, children=["empty-0"], message=gpt_message("root-message", INSULT))}
        for index in range(depth):
            mapping["empty-" + str(index)] = CountedDict(
                parent="root" if index == 0 else "empty-" + str(index - 1),
                children=["empty-" + str(index + 1)] if index < depth - 1 else ["child-" + str(i) for i in range(width)],
                message=None)
        for index in range(width):
            mapping["child-" + str(index)] = CountedDict(parent="empty-" + str(depth - 1), children=[],
                message=gpt_message("child-message-" + str(index), "Synthetic child input."))
        _, messages = exports._chatgpt({"id": CONVERSATION, "mapping": mapping, "current_node": "child-0"})
        self.assertEqual(len(messages), width + 1)
        self.assertEqual(messages[-1]["context"][0]["text"], INSULT)
        self.assertLess(CountedDict.reads, len(mapping) * 50)

    def test_wide_graph_validates_child_membership_without_repeated_list_search(self):
        class CountedList(list):
            contains_calls = 0
            def __contains__(self, value):
                CountedList.contains_calls += 1
                return super().__contains__(value)
        children = CountedList("child-" + str(index) for index in range(2000))
        mapping = {"root": {"parent": None, "children": children, "message": None}}
        for child in children:
            mapping[child] = {"parent": "root", "children": [], "message": gpt_message("message-" + child, "Synthetic text.")}
        _, messages = exports._chatgpt({"id": CONVERSATION, "mapping": mapping})
        self.assertEqual(len(messages), 2000)
        self.assertEqual(CountedList.contains_calls, 0)

    def test_text_only_and_attachment_only_human_messages_are_counted_accurately(self):
        record = gpt_conversation()
        record["mapping"]["u2"]["message"]["content"] = {"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "synthetic-pointer"}]}
        self.put([record])
        result = self.review(include_all=True)
        self.assertEqual(result["reviewable_human_messages"], 2)
        self.assertEqual(result["message_counts"]["user"], 3)

    def test_conflicting_dual_text_mirrors_fail_closed(self):
        variants = [("chatgpt", gpt_conversation()), ("claude", claude_conversation())]
        variants[0][1]["mapping"]["u1"]["message"]["content"]["text"] = USEFUL
        variants[1][1]["chat_messages"][0]["content"] = [{"type": "text", "text": USEFUL}]
        for provider, record in variants:
            with self.subTest(provider=provider):
                original = self.put([record])
                with self.assertRaisesRegex(Refusal, "mirrors disagree"):
                    self.review(provider)
                self.assertEqual(self.input.read_bytes(), original)
                self.assertFalse(self.report.exists())

    def test_graph_corruption_fails_before_any_report(self):
        records = []
        value = gpt_conversation(); value["mapping"]["u1"]["parent"] = "missing"; records.append(value)
        value = gpt_conversation(); value["mapping"]["a1"]["children"] = ["u2", "u2"]; records.append(value)
        value = gpt_conversation(); value["mapping"]["u2"]["parent"] = "u1"; records.append(value)
        value = gpt_conversation(); value["current_node"] = "missing"; records.append(value)
        value = gpt_conversation(); value["mapping"]["root"]["parent"] = "u2"; value["mapping"]["u2"]["children"] = ["root"]; records.append(value)
        value = gpt_conversation(); value["mapping"]["orphan"] = {"parent": None, "children": [], "message": None}; records.append(value)
        value = gpt_conversation(); value["mapping"]["u1"].pop("message"); records.append(value)
        for record in records:
            with self.subTest(index=records.index(record)):
                original = self.put([record])
                with self.assertRaises(Refusal):
                    self.review()
                self.assertEqual(self.input.read_bytes(), original)
                self.assertFalse(self.report.exists())

    def test_duplicate_conversation_message_ids_and_json_keys_refuse(self):
        record = gpt_conversation()
        self.put([record, copy.deepcopy(record)])
        with self.assertRaisesRegex(Refusal, "duplicate conversation"):
            self.review()
        record["mapping"]["u2"]["message"]["id"] = "m-u1"
        self.put([record])
        with self.assertRaisesRegex(Refusal, "duplicate message"):
            self.review()
        self.put(raw=b'[{"id":"one","id":"two"}]')
        with self.assertRaisesRegex(Refusal, "duplicate JSON"):
            self.review()

    def test_unknown_schemas_roles_and_content_fail_closed(self):
        variants = []
        value = gpt_conversation(); value["mapping"]["u1"]["message"]["author"]["role"] = "future_human"; variants.append(("chatgpt", [value]))
        value = gpt_conversation(); value["mapping"]["u1"]["message"]["content"]["content_type"] = "future_text"; variants.append(("chatgpt", [value]))
        value = claude_conversation(); value["chat_messages"][0]["content"] = [{"type": "future_text", "text": INSULT}]; variants.append(("claude", [value]))
        value = claude_conversation(); value["chat_messages"][0]["parent"] = None; variants.append(("claude", [value]))
        value = claude_conversation(); value["chat_messages"][0]["is_generated"] = "false"; variants.append(("claude", [value]))
        variants += [("chatgpt", {"messages": [INSULT]}), ("claude", [gpt_conversation()])]
        for provider, data in variants:
            with self.subTest(provider=provider):
                self.put(data)
                with self.assertRaises(Refusal):
                    self.review(provider)
                self.assertFalse(self.report.exists())

    def test_unsafe_identifiers_and_export_urls_never_become_links(self):
        for identity in ["https://example.invalid/chat", "../other", "one?redirect=evil", "one\nheader", "javascript:alert(1)"]:
            record = gpt_conversation(); record["id"] = identity
            self.put([record])
            with self.subTest(identity=identity), self.assertRaises(Refusal):
                self.review()
        record = gpt_conversation(); record["url"] = "https://example.invalid/untrusted"
        self.put([record])
        result = self.review()
        guide = deletion_guide("chatgpt", self.input, [result["messages"][0]["id"]], self.guide)
        self.assertEqual(guide["checklist"][0]["review_url"], "https://chatgpt.com/c/" + CONVERSATION)
        self.assertNotIn("example.invalid", self.guide.read_text())

    def test_guide_deduplicates_conversation_and_warns_about_every_message(self):
        original = self.put([gpt_conversation()])
        result = self.review()
        guide = deletion_guide("chatgpt", self.input, [row["id"] for row in result["messages"]], self.guide)
        self.assertEqual(guide["conversations_to_review"], 1)
        row = guide["checklist"][0]
        self.assertEqual(row["selected_count"], 2)
        self.assertEqual(row["total_user_messages"], 3)
        self.assertEqual(row["total_assistant_messages"], 1)
        self.assertIn("useful", row["warning"])
        self.assertIn("unselected", row["warning"])
        self.assertEqual(guide["status"], "review_only")
        self.assertIn("neither approval", guide["notice"])
        self.assertEqual(self.input.read_bytes(), original)
        self.assert_private(self.guide)

    def test_non_uuid_conversation_gets_only_history_link_and_opaque_export_id(self):
        record = claude_conversation(); record["uuid"] = "synthetic-private-export-id"
        self.put([record])
        result = self.review("claude")
        guide = deletion_guide("claude", self.input, [result["messages"][0]["id"]], self.guide)
        self.assertEqual(guide["checklist"][0]["review_url"], "https://claude.ai/")
        self.assertNotIn("provider_conversation_id", guide["checklist"][0])
        self.assertNotIn("synthetic-private-export-id", self.guide.read_text())

    def test_stable_ids_bind_exact_input_provider_and_conversation(self):
        self.put([gpt_conversation()])
        first = self.review()["messages"][0]["id"]
        self.report.unlink()
        second = self.review()["messages"][0]["id"]
        self.assertEqual(first, second)
        self.input.write_bytes(self.input.read_bytes() + b"\n")
        with self.assertRaisesRegex(Refusal, "stale"):
            deletion_guide("chatgpt", self.input, [first], self.guide)
        self.put([claude_conversation()])
        with self.assertRaises(Refusal):
            deletion_guide("claude", self.input, [first], self.guide)
        self.assertFalse(self.guide.exists())

    def test_forged_duplicate_and_native_selection_are_refused(self):
        self.put([gpt_conversation()])
        identity = self.review()["messages"][0]["id"]
        for selection in (["export-" + "0" * 64], [identity, identity], ["f" * 24], []):
            with self.subTest(selection=selection), self.assertRaises(Refusal):
                deletion_guide("chatgpt", self.input, selection, self.guide)
        self.assertFalse(self.guide.exists())

    def test_manual_guide_is_never_accepted_as_native_apply_or_receipt(self):
        original = self.put([gpt_conversation()])
        identity = self.review()["messages"][0]["id"]
        deletion_guide("chatgpt", self.input, [identity], self.guide)
        with self.assertRaises(Refusal):
            core.apply_plan(self.guide, "0" * 64, self.base / "receipt.json")
        with self.assertRaises(Refusal):
            core.verify(self.guide)
        self.assertEqual(self.input.read_bytes(), original)
        self.assertFalse((self.base / "receipt.json").exists())

    def test_output_overwrite_input_symlinks_hardlinks_and_missing_parent_refuse(self):
        original = self.put([gpt_conversation()])
        with self.assertRaises(Refusal):
            review_export("chatgpt", self.input, self.input)
        self.report.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.review()
        self.assertEqual(self.report.read_bytes(), b"keep")
        self.report.unlink()
        link = self.base / "link.json"; link.symlink_to(self.input)
        with self.assertRaises(Refusal):
            review_export("chatgpt", link, self.report)
        self.report.symlink_to(self.input)
        with self.assertRaises(Refusal):
            self.review()
        self.report.unlink()
        hard = self.base / "hard.json"; os.link(self.input, hard)
        with self.assertRaises(Refusal):
            self.review()
        hard.unlink()
        with self.assertRaises(Refusal):
            review_export("chatgpt", self.input, self.base / "absent" / "report.json")
        self.assertEqual(self.input.read_bytes(), original)

    def test_source_change_during_review_refuses_no_report(self):
        self.put([gpt_conversation()])
        real = exports.candidate_reason
        changed = False
        def changing(text):
            nonlocal changed
            if not changed:
                changed = True
                self.input.write_bytes(self.input.read_bytes() + b"\n")
            return real(text)
        with patch("ai_chat_cleaner.exports.candidate_reason", side_effect=changing), self.assertRaisesRegex(Refusal, "changed"):
            self.review()
        self.assertFalse(self.report.exists())

    def zip_input(self, members):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, contents in members:
                archive.writestr(name, contents)
        return self.put(raw=data.getvalue())

    def test_zip_reads_exact_json_without_extraction_or_original_changes(self):
        original = self.zip_input([("conversations.json", canonical([claude_conversation()])),
                                   ("attachments/synthetic.txt", INSULT.encode())])
        before = {path.name for path in self.base.iterdir()}
        result = self.review("claude")
        self.assertEqual(result["input_container"], "zip")
        self.assertEqual(result["export_sha256"], digest(original))
        self.assertEqual({path.name for path in self.base.iterdir()} - before, {"report.json"})
        self.assertEqual(self.input.read_bytes(), original)
        self.assert_private(self.report)

    def test_zip_unsafe_ambiguous_nested_and_duplicate_members_refuse(self):
        payload = canonical([gpt_conversation()])
        variants = [[("../conversations.json", payload)], [("/conversations.json", payload)],
                    [("nested/conversations.json", payload)], [("conversations.json", payload), ("copy/conversations.json", payload)],
                    [("conversations.json", payload), ("CONVERSATIONS.JSON", payload)],
                    [("conversations.json", payload), ("other\\unsafe", b"x")],
                    [("conversations.json", payload), ("same", b"one"), ("same", b"two")]]
        for members in variants:
            with self.subTest(members=[name for name, _ in members]):
                with contextlib.redirect_stderr(io.StringIO()):
                    original = self.zip_input(members)
                with self.assertRaises(Refusal):
                    self.review()
                self.assertEqual(self.input.read_bytes(), original)
                self.assertFalse(self.report.exists())

    def test_zip_symlink_and_corrupt_member_refuse(self):
        link = zipfile.ZipInfo("conversations.json")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.zip_input([(link, canonical([gpt_conversation()]))])
        with self.assertRaises(Refusal):
            self.review()
        original = self.zip_input([("conversations.json", canonical([gpt_conversation()]))])
        damaged = bytearray(original)
        marker = damaged.find(b"Synthetic private")
        self.assertNotEqual(marker, -1)
        damaged[marker] ^= 1
        self.put(raw=bytes(damaged))
        with self.assertRaisesRegex(Refusal, "ZIP"):
            self.review()

    def test_zip_original_name_cannot_hide_nul_suffix(self):
        raw = self.zip_input([("conversations.jsonXevil", canonical([gpt_conversation()]))])
        raw = raw.replace(b"conversations.jsonXevil", b"conversations.json\x00evil")
        self.put(raw=raw)
        with self.assertRaisesRegex(Refusal, "unsafe"):
            self.review()
        self.assertEqual(self.input.read_bytes(), raw)
        self.assertFalse(self.report.exists())

    def test_cli_corrupt_deflate_returns_safe_refusal_without_traceback(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("conversations.json", canonical([gpt_conversation()]))
        raw = bytearray(data.getvalue())
        start = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
        raw[start] = (raw[start] & ~6) | 6  # Reserved DEFLATE block type.
        self.put(raw=bytes(raw))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(["review-export", "--provider", "chatgpt", "--input", str(self.input), "--report", str(self.report)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["status"], "refused")
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertFalse(self.report.exists())

    def test_bounded_input_json_zip_count_and_expanded_size(self):
        self.put([gpt_conversation()])
        with patch("ai_chat_cleaner.exports.MAX_INPUT_BYTES", 20), self.assertRaises(Refusal):
            self.review()
        self.zip_input([("conversations.json", canonical([gpt_conversation()])), ("other", b"data")])
        for constant, limit in [("MAX_JSON_BYTES", 20), ("MAX_ZIP_MEMBERS", 1), ("MAX_ZIP_EXPANDED_BYTES", 20)]:
            with self.subTest(limit=constant), patch("ai_chat_cleaner.exports." + constant, limit), self.assertRaises(Refusal):
                self.review()
        self.assertFalse(self.report.exists())

    def test_zip_actual_member_budget_cannot_be_bypassed_by_footer_count(self):
        raw = bytearray(self.zip_input([("conversations.json", canonical([gpt_conversation()])),
                                        ("one", b"x"), ("two", b"x")]))
        footer = raw.rfind(b"PK\x05\x06")
        raw[footer + 8:footer + 12] = b"\x01\x00\x01\x00"  # Lie about both entry counts.
        self.put(raw=bytes(raw))
        with patch("ai_chat_cleaner.exports.MAX_ZIP_MEMBERS", 2):
            with patch("ai_chat_cleaner.exports.zipfile.ZipFile") as opening:
                with self.assertRaisesRegex(Refusal, "actual members"):
                    self.review()
                opening.assert_not_called()
        self.assertFalse(self.report.exists())

    def test_zip64_and_multidisk_footers_are_explicitly_unsupported(self):
        original = self.zip_input([("conversations.json", canonical([gpt_conversation()]))])
        for field, replacement in [(4, b"\x01\x00"), (10, b"\xff\xff")]:
            raw = bytearray(original)
            footer = raw.rfind(b"PK\x05\x06")
            raw[footer + field:footer + field + 2] = replacement
            self.put(raw=bytes(raw))
            with self.subTest(field=field), self.assertRaisesRegex(Refusal, "unsupported"):
                self.review()

    def test_cli_errors_are_safe_json_without_transcript_or_traceback(self):
        samples = [b"not JSON synthetic-sensitive-marker", b'[{"x":NaN}]', b'[{"x":1e999}]',
                   b'[{"x":' + b"1" * 10000 + b"}]", b'[{"x":"synthetic-sensitive-marker\\ud800"}]', b"PKbroken"]
        for sample in samples:
            self.put(raw=sample)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = main(["review-export", "--provider", "chatgpt", "--input", str(self.input), "--report", str(self.report)])
            with self.subTest(sample_prefix=sample[:5]):
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(errors.getvalue())["status"], "refused")
                self.assertNotIn("synthetic-sensitive-marker", errors.getvalue())
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertFalse(self.report.exists())

    def test_subprocess_cli_preview_private_report_and_manual_guide(self):
        original = self.put([claude_conversation()])
        command = [sys.executable, "-m", "ai_chat_cleaner"]
        review = subprocess.run(command + ["review-export", "--provider", "claude", "--input", str(self.input),
                                          "--report", str(self.report), "--preview"], capture_output=True, text=True)
        self.assertEqual(review.returncode, 0, review.stderr)
        result = json.loads(review.stdout)
        self.assertEqual(result["messages"][0]["text"], INSULT)
        guide = subprocess.run(command + ["deletion-guide", "--provider", "claude", "--input", str(self.input),
                                         "--select", result["messages"][0]["id"], "--out", str(self.guide)], capture_output=True, text=True)
        self.assertEqual(guide.returncode, 0, guide.stderr)
        self.assertEqual(json.loads(guide.stdout)["action"], "manual_whole_conversation_deletion")
        self.assertNotIn(INSULT, guide.stdout)
        self.assert_private(self.report)
        self.assert_private(self.guide)
        self.assertEqual(self.input.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
