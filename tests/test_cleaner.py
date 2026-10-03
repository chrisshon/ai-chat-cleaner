"""Synthetic fixtures only. Never read the operator's real history."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_chat_cleaner import core
from ai_chat_cleaner.cli import main
from ai_chat_cleaner.detection import candidate_reason
from ai_chat_cleaner.storage import Refusal, canonical, digest


def user(text, **extra):
    return {"type": "user", "uuid": "synthetic-uuid", "message": {"role": "user", "content": text}, **extra}


class DetectionTests(unittest.TestCase):
    def test_labelled_synthetic_examples(self):
        # Conservative discovery: each protected message has meaningful context.
        cases = [
            ("Fuck you.", True),
            ("You are a useless idiot.", True),
            ("I hate you.", True),
            ("Idiot.", True),
            ("You are a fucking idiot. Fix the tax column.", False),
            ("That is shit. Preserve the original logo.", False),
            ("You broke it, undo the change.", False),
            ("What the fuck does this error mean?", False),
            ('The character says "fuck you".', False),
            ("Quote the phrase 'you are useless' for a novel.", False),
            ("You did a fucking great job.", False),
            ("I love your shit.", False),
            ("Please do better.", False),
            ("Hello, I am bored.", False),
            ("The client called their system useless.", False),
            ("Use /tmp/sample.txt, you idiot.", False),
            ("You're wrong. Use port 8080.", False),
            ("> you are a useless idiot", False),
            ("<system>you are useless</system>", False),
        ]
        for text, expected in cases:
            with self.subTest(label=text):
                self.assertEqual(bool(candidate_reason(text)), expected)


class CleanerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "claude"
        (self.root / "projects" / "synthetic").mkdir(parents=True)
        self.source = self.root / "projects" / "synthetic" / "session.jsonl"
        self.plan_path, self.receipt_path = self.base / "plan.json", self.base / "receipt.json"

    def tearDown(self):
        self.temporary.cleanup()

    def put(self, path, records=None, raw=None, mode=0o640):
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is None:
            raw = b"".join(canonical(r) + b"\n" for r in records)
        path.write_bytes(raw)
        path.chmod(mode)
        old = time.time() - core.QUIET_SECONDS - 10
        os.utime(path, (old, old))
        return raw

    def prepare(self, text="You are a useless idiot."):
        self.put(self.source, [user(text), user("Keep the green logo.")])
        identity = core.scan(self.root)["messages"][0]["id"]
        result = core.make_plan(self.root, [identity], self.plan_path)
        return result["confirmation"]

    def apply(self, confirmation):
        return core.apply_plan(self.plan_path, confirmation, self.receipt_path)

    def test_scan_is_read_only_and_private_without_preview(self):
        raw = self.put(self.source, [user("You are a useless idiot.")])
        result = core.scan(self.root)
        self.assertEqual(result["candidates"], 1)
        self.assertNotIn("useless", json.dumps(result))
        self.assertNotIn("text", result["messages"][0])
        self.assertEqual(self.source.read_bytes(), raw)

    def test_explicit_preview_and_all_include_useful_record(self):
        self.put(self.source, [user("You are useless."), user("Fix that column.")])
        result = core.scan(self.root, preview=True, include_all=True)
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(result["messages"][0]["text"], "You are useless.")

    def test_copy_expansion_does_not_redact_assistant_or_tool_results(self):
        text = "You are useless."
        self.put(self.source, [user(text), {"type": "assistant", "message": {"role": "assistant", "content": text}},
                               user([{"type": "tool_result", "tool_use_id": "x", "content": text}])])
        copy = self.root / "projects" / "synthetic" / "subagents" / "agent.jsonl"
        self.put(copy, [user(text, isSidechain=True)])
        history = self.root / "history.jsonl"
        self.put(history, [{"display": text, "timestamp": 123, "pastedContents": {"x": text}}])
        identity = core.scan(self.root)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        self.assertEqual(plan["matching_records"], 3)
        self.apply(plan["confirmation"])
        records = [json.loads(line) for line in self.source.read_text().splitlines()]
        self.assertEqual(records[0]["message"]["content"], core.TOMBSTONE)
        self.assertEqual(records[1]["message"]["content"], text)
        self.assertEqual(records[2]["message"]["content"][0]["content"], text)
        self.assertEqual(json.loads(history.read_text())["pastedContents"]["x"], text)

    def test_replay_meta_and_internal_user_envelopes_are_excluded(self):
        text = "You are useless."
        self.put(self.source, [user(text, isMeta=True), user(text, isReplay=True),
                               user(text, userType="internal"), user(text, sourceToolAssistantUUID="x")])
        self.assertEqual(core.scan(self.root, include_all=True)["human_records"], 0)

    def test_subagent_generated_prompt_is_not_a_human_without_parent_match(self):
        self.put(self.source, [user("Keep the original requirements.")])
        subagent = self.source.parent / "subagents" / "generated.jsonl"
        self.put(subagent, [user("You are useless.", isSidechain=True)])
        self.assertEqual(core.scan(self.root)["candidates"], 0)
        self.assertEqual(core.scan(self.root, include_all=True)["human_records"], 1)

    def test_last_prompt_mirror_redacted_without_creating_a_human_source(self):
        self.put(self.source, [user("You are useless."), {"type": "last-prompt", "lastPrompt": "You are useless."},
                               {"type": "last-prompt", "lastPrompt": "You are an idiot."}])
        result = core.scan(self.root)
        self.assertEqual(result["human_records"], 2)
        plan = core.make_plan(self.root, [result["messages"][0]["id"]], self.plan_path)
        self.assertEqual(plan["matching_records"], 2)
        self.apply(plan["confirmation"])
        records = [json.loads(line) for line in self.source.read_text().splitlines()]
        self.assertEqual(records[1]["lastPrompt"], core.TOMBSTONE)
        self.assertEqual(records[2]["lastPrompt"], "You are an idiot.")

    def test_unknown_last_prompt_schema_refuses(self):
        self.put(self.source, [{"type": "last-prompt", "lastPrompt": {"unexpected": True}}])
        with self.assertRaises(Refusal):
            core.scan(self.root)

    def test_checked_path_normalizes_dotdot_after_symlink_check(self):
        self.put(self.source, [user("You are useless.")])
        nested = self.root / "nested"
        nested.mkdir()
        result = core.scan(nested / "..")
        self.assertEqual(result["root"], str(self.root))
        nested.rmdir()
        nested.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(Refusal):
            core.scan(nested / "..")

    def test_bom_and_utf16_sources_refuse_before_plan(self):
        for raw in (b'\xef\xbb\xbf' + canonical(user("You are useless.")),
                    json.dumps(user("You are useless.")).encode("utf-16")):
            with self.subTest(encoding=raw[:3]):
                self.put(self.source, raw=raw)
                with self.assertRaises(Refusal):
                    core.scan(self.root)
        self.assertFalse(self.plan_path.exists())

    def test_external_text_next_to_tool_result_redacts_only_text(self):
        content = [{"type": "text", "text": "You are useless."},
                   {"type": "tool_result", "content": "Synthetic tool output remains."}]
        self.put(self.source, [user(content, userType="external")])
        identity = core.scan(self.root)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        self.apply(plan["confirmation"])
        result = json.loads(self.source.read_text())
        self.assertEqual(result["message"]["content"][0]["text"], core.TOMBSTONE)
        self.assertEqual(result["message"]["content"][1], content[1])

    def test_exact_byte_preservation_unicode_escapes_and_crlf(self):
        selected = b'{ "type" : "user", "uuid":"keep", "message":{"role":"user", "content":"You are useless."}, "meta": "caf\\u00e9" }\r\n'
        untouched = b'{ "type":"assistant", "message":{"role":"assistant","content":"caf\\u00e9"} }\r\n'
        raw = self.put(self.source, raw=selected + untouched)
        identity = core.scan(self.root)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        self.apply(plan["confirmation"])
        expected = raw.replace(b'"You are useless."', json.dumps(core.TOMBSTONE).encode(), 1)
        self.assertEqual(self.source.read_bytes(), expected)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o640)

    def test_array_text_only_without_newline_at_eof(self):
        content = [{"type": "text", "text": "You are"}, {"type": "text", "text": "useless."},
                   {"type": "image", "source": {"type": "base64", "data": "synthetic"}}]
        self.put(self.source, raw=canonical(user(content)))
        identity = core.scan(self.root, include_all=True)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        self.apply(plan["confirmation"])
        after = json.loads(self.source.read_bytes())
        self.assertEqual(after["message"]["content"][0]["text"], core.TOMBSTONE)
        self.assertEqual(after["message"]["content"][1]["text"], core.TOMBSTONE)
        self.assertEqual(after["message"]["content"][2], content[2])
        self.assertFalse(self.source.read_bytes().endswith(b"\n"))
        self.assertEqual(core.scan(self.root, include_all=True)["human_records"], 0)

    def test_dry_run_has_no_changes_or_receipt(self):
        confirmation = self.prepare()
        before = self.source.read_bytes()
        result = core.apply_plan(self.plan_path, None, None, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(self.receipt_path.exists())

    def test_apply_requires_exact_confirmation(self):
        self.prepare()
        for confirmation in (None, "yes", "bad"):
            with self.subTest(confirmation=confirmation), self.assertRaises(Refusal):
                self.apply(confirmation)
        self.assertFalse(self.receipt_path.exists())

    def test_private_plan_receipt_and_no_plaintext_backup(self):
        confirmation = self.prepare()
        self.apply(confirmation)
        for path in (self.plan_path, self.receipt_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("useless idiot", path.read_text())
        self.assertEqual(list(self.source.parent.glob(".ai-chat-cleaner-*")), [])
        self.assertTrue(core.verify(self.receipt_path)["verified"])

    def test_stale_content_inventory_and_permissions_refuse(self):
        for mutation in ("content", "new_file", "mode"):
            with self.subTest(mutation=mutation):
                self.plan_path.unlink(missing_ok=True)
                confirmation = self.prepare()
                extra = self.source.parent / "other.jsonl"
                extra.unlink(missing_ok=True)
                # Regenerate after removing the prior subtest's inventory addition.
                self.plan_path.unlink()
                identity = core.scan(self.root)["messages"][0]["id"]
                confirmation = core.make_plan(self.root, [identity], self.plan_path)["confirmation"]
                if mutation == "content":
                    self.put(self.source, [user("Keep important changes.")])
                elif mutation == "new_file":
                    self.put(extra, [user("Keep this too.")])
                else:
                    self.source.chmod(0o600)
                with self.assertRaises(Refusal):
                    self.apply(confirmation)
                self.assertFalse(self.receipt_path.exists())

    def test_recent_source_refuses_plan(self):
        self.put(self.source, [user("You are useless.")])
        os.utime(self.source, None)
        identity = core.scan(self.root)["messages"][0]["id"]
        with self.assertRaisesRegex(Refusal, "last 120 seconds"):
            core.make_plan(self.root, [identity], self.plan_path)

    def test_unknown_and_malformed_records_fail_closed(self):
        cases = [b'{"type":"user",', b'[]\n', b'{"type":"future-format"}\n',
                 b'{"type":"user","type":"assistant"}\n', b'\n',
                 canonical(user([{"type": "future-block"}])) + b"\n",
                 canonical({"type": "user", "message": {"role": "assistant", "content": "x"}}),
                 b'{"type":"system","value":NaN}\n', b'\xff\n']
        for raw in cases:
            with self.subTest(raw=raw):
                self.put(self.source, raw=raw)
                with self.assertRaises(Refusal):
                    core.scan(self.root)

    def test_unsupported_history_schema_refuses(self):
        self.put(self.source, [user("You are useless.")])
        self.put(self.root / "history.jsonl", [{"display": 23, "timestamp": 1}])
        with self.assertRaises(Refusal):
            core.scan(self.root)

    def test_symlink_source_root_directory_and_hardlink_refuse(self):
        self.put(self.source, [user("You are useless.")])
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(Refusal):
            core.scan(alias)
        alias.unlink()
        alias = self.source.parent / "linked.jsonl"
        alias.symlink_to(self.source)
        with self.assertRaises(Refusal):
            core.scan(self.root)
        alias.unlink()
        folder = self.root / "projects" / "linked"
        folder.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(Refusal):
            core.scan(self.root)
        folder.unlink()
        os.link(self.source, alias)
        with self.assertRaises(Refusal):
            core.scan(self.root)

    def test_plan_cannot_write_inside_history_or_overwrite_file(self):
        confirmation = self.prepare()
        identity = core.scan(self.root)["messages"][0]["id"]
        with self.assertRaises(Refusal):
            core.make_plan(self.root, [identity], self.root / "plan.json")
        with self.assertRaises(FileExistsError):
            core.make_plan(self.root, [identity], self.plan_path)
        self.assertFalse(self.receipt_path.exists())

    def test_symlink_or_public_metadata_refuse(self):
        confirmation = self.prepare()
        self.plan_path.chmod(0o644)
        with self.assertRaises(Refusal):
            self.apply(confirmation)
        self.plan_path.chmod(0o600)
        self.receipt_path.symlink_to(self.base / "target")
        with self.assertRaises(Refusal):
            self.apply(confirmation)

    def test_tampered_and_forged_plan_selection_refuse(self):
        confirmation = self.prepare()
        plan = json.loads(self.plan_path.read_text())
        plan["selected"] = ["not-a-real-id"]
        self.plan_path.write_bytes(canonical(plan))
        with self.assertRaises(Refusal):
            self.apply(confirmation)
        plan["id"] = core._plan_id(plan)
        self.plan_path.write_bytes(canonical(plan))
        with self.assertRaises(Refusal):
            self.apply(plan["id"])

    def test_no_unselected_text_changed_when_identical_insult_inside_useful_prompt(self):
        self.put(self.source, [user("You are useless."), user("You are useless. Fix the totals.")])
        identity = core.scan(self.root)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        self.apply(plan["confirmation"])
        records = [json.loads(line) for line in self.source.read_text().splitlines()]
        self.assertEqual(records[1]["message"]["content"], "You are useless. Fix the totals.")

    def test_stage_failure_changes_no_source_and_cleans_temporary_files(self):
        confirmation = self.prepare()
        before = self.source.read_bytes()
        with patch("ai_chat_cleaner.core.stage", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["status"], "failed")
        self.assertEqual(failure.exception.result["files_changed"], 0)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(json.loads(self.receipt_path.read_text())["status"], "failed")

    def test_partial_failure_has_receipt_and_never_reports_success(self):
        text = "You are useless."
        self.put(self.source, [user(text)])
        second = self.source.parent / "two.jsonl"
        before_second = self.put(second, [user(text)])
        identity = core.scan(self.root)["messages"][0]["id"]
        plan = core.make_plan(self.root, [identity], self.plan_path)
        real_replace = os.replace
        def replacement(source, target):
            if Path(target) == second:
                raise OSError("synthetic replace failure")
            return real_replace(source, target)
        with patch("ai_chat_cleaner.core.os.replace", side_effect=replacement):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(plan["confirmation"])
        self.assertEqual(failure.exception.result["status"], "incomplete")
        self.assertEqual(failure.exception.result["files_changed"], 1)
        self.assertEqual(failure.exception.result["receipt_status"], "partial")
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(second.read_bytes(), before_second)
        self.assertEqual(list(self.source.parent.glob(".ai-chat-cleaner-*")), [])
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)

    def test_verify_refuses_changed_content_permissions_and_unexpected_path(self):
        confirmation = self.prepare()
        self.apply(confirmation)
        receipt = json.loads(self.receipt_path.read_text())
        receipt["files"]["../outside.jsonl"] = next(iter(receipt["files"].values()))
        self.receipt_path.write_bytes(canonical(receipt))
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)
        del receipt["files"]["../outside.jsonl"]
        self.receipt_path.write_bytes(canonical(receipt))
        self.source.chmod(0o600)
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)
        self.source.chmod(0o640)
        self.source.write_bytes(self.source.read_bytes() + b" ")
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)

    def test_cli_report_excludes_preview_and_stdout_requires_flag(self):
        self.put(self.source, [user("You are useless.")])
        report = self.base / "scan.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["scan", "--root", str(self.root), "--preview", "--report", str(report)]), 0)
        self.assertIn("You are useless.", out.getvalue())
        self.assertNotIn("You are useless.", report.read_text())
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

    def test_cli_subprocess_end_to_end(self):
        self.put(self.source, [user("You are useless.")])
        def run(*args):
            completed = subprocess.run([sys.executable, "-m", "ai_chat_cleaner", *args], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)
        result = run("scan", "--root", str(self.root))
        plan = run("plan", "--root", str(self.root), "--select", result["messages"][0]["id"], "--out", str(self.plan_path))
        run("apply", "--plan", str(self.plan_path), "--dry-run")
        applied = run("apply", "--plan", str(self.plan_path), "--confirm", plan["confirmation"], "--receipt", str(self.receipt_path))
        self.assertTrue(applied["verified"])
        self.assertTrue(run("verify", "--receipt", str(self.receipt_path))["verified"])

    def test_cli_error_contains_no_malformed_transcript_text(self):
        self.put(self.source, raw=b'{"type":"user","message":"synthetic-sensitive-marker"')
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(main(["scan", "--root", str(self.root)]), 2)
        self.assertNotIn("synthetic-sensitive-marker", errors.getvalue())

    def test_cli_unpaired_surrogate_is_safe_json_exit_two(self):
        raw = canonical(user("synthetic-sensitive-marker\ud800")) + b"\n"
        self.put(self.source, raw=raw)
        result = subprocess.run([sys.executable, "-m", "ai_chat_cleaner", "scan", "--root", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error["status"], "refused")
        self.assertIn("unsupported Unicode", error["error"])
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("synthetic-sensitive-marker", result.stderr + result.stdout)
        self.assertEqual(self.source.read_bytes(), raw)

    def test_cli_receipt_persistence_failure_reports_actual_incomplete_apply(self):
        confirmation = self.prepare()
        errors = io.StringIO()
        with patch("ai_chat_cleaner.core.replace_private", side_effect=OSError("synthetic-sensitive-error")):
            with contextlib.redirect_stderr(errors):
                code = main(["apply", "--plan", str(self.plan_path), "--confirm", confirmation,
                             "--receipt", str(self.receipt_path)])
        self.assertEqual(code, 2)
        result = json.loads(errors.getvalue())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["receipt"], str(self.receipt_path))
        self.assertEqual(result["receipt_status"], "pending")
        self.assertFalse(result["verified"])
        self.assertNotIn("synthetic-sensitive-error", errors.getvalue())
        self.assertEqual(json.loads(self.source.read_text().splitlines()[0])["message"]["content"], core.TOMBSTONE)
        self.assertEqual(json.loads(self.receipt_path.read_text())["status"], "pending")
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)

    def test_postwrite_verification_failure_never_returns_refused_or_success(self):
        confirmation = self.prepare()
        with patch("ai_chat_cleaner.core.verify", side_effect=Refusal("synthetic verification failure")):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["status"], "incomplete")
        self.assertEqual(failure.exception.result["files_changed"], 1)
        self.assertEqual(failure.exception.result["receipt_status"], "partial")

    def test_cli_partial_apply_cleanup_failure_preserves_incomplete_status(self):
        text = "You are useless."
        self.put(self.source, [user(text)])
        second = self.source.parent / "two.jsonl"
        self.put(second, [user(text)])
        identity = core.scan(self.root)["messages"][0]["id"]
        confirmation = core.make_plan(self.root, [identity], self.plan_path)["confirmation"]
        real_replace, real_unlink = os.replace, Path.unlink
        def replacing(source, target):
            if Path(target) == second:
                raise OSError("synthetic replace failure")
            return real_replace(source, target)
        def unlinking(path, *args, **kwargs):
            if path.parent == second.parent and path.name.startswith(".ai-chat-cleaner-") and path.exists():
                raise PermissionError("synthetic-sensitive-cleanup-error")
            return real_unlink(path, *args, **kwargs)
        errors = io.StringIO()
        with patch("ai_chat_cleaner.core.os.replace", side_effect=replacing), patch.object(Path, "unlink", unlinking):
            with contextlib.redirect_stderr(errors):
                code = main(["apply", "--plan", str(self.plan_path), "--confirm", confirmation,
                             "--receipt", str(self.receipt_path)])
        self.assertEqual(code, 2)
        result = json.loads(errors.getvalue())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["receipt_status"], "partial")
        self.assertFalse(result["verified"])
        self.assertEqual(len(result["temporary_files_to_check"]), 1)
        leftover = Path(result["temporary_files_to_check"][0])
        self.assertTrue(leftover.exists())
        self.assertIn(core.TOMBSTONE.encode(), leftover.read_bytes())
        self.assertNotIn(text.encode(), leftover.read_bytes())
        self.assertNotIn("synthetic-sensitive-cleanup-error", errors.getvalue())
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["temporary_files_to_check"], result["temporary_files_to_check"])
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)

    def test_cli_successful_write_cleanup_error_reports_incomplete_receipt(self):
        confirmation = self.prepare()
        real_unlink = Path.unlink
        def unlinking(path, *args, **kwargs):
            if path.parent == self.source.parent and path.name.startswith(".ai-chat-cleaner-"):
                # A permission error can prevent confirming even a moved temp's absence.
                raise PermissionError("synthetic cleanup failure")
            return real_unlink(path, *args, **kwargs)
        errors = io.StringIO()
        with patch.object(Path, "unlink", unlinking):
            with contextlib.redirect_stderr(errors):
                code = main(["apply", "--plan", str(self.plan_path), "--confirm", confirmation,
                             "--receipt", str(self.receipt_path)])
        self.assertEqual(code, 2)
        result = json.loads(errors.getvalue())
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["receipt_status"], "partial")
        self.assertFalse(result["verified"])
        self.assertEqual(len(result["temporary_files_to_check"]), 1)
        self.assertEqual(json.loads(self.source.read_text().splitlines()[0])["message"]["content"], core.TOMBSTONE)
        receipt = json.loads(self.receipt_path.read_text())
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["temporary_files_to_check"], result["temporary_files_to_check"])
        with self.assertRaises(Refusal):
            core.verify(self.receipt_path)


if __name__ == "__main__":
    unittest.main()
