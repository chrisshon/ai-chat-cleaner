"""Synthetic desktop and cross-provider acceptance tests; no real histories."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_chat_cleaner import core, codex_state
from ai_chat_cleaner.cli import main
from ai_chat_cleaner.storage import Refusal, canonical


INSULT = "You are a useless idiot."
USEFUL = "Keep the original green logo."


def response(text):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]}}}


def ui(text, role="userMessage"):
    return {"type": role, "id": "item-id", "content": [{"type": "text", "text": text, "text_elements": []}]}


def document(text):
    return {"type": "doc", "content": [{"type": "paragraph", "attrs": {"id": "keep"},
            "content": [{"type": "text", "text": text, "marks": [{"type": "bold"}]}]}]}


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.codex = self.base / "codex"
        self.claude = self.base / "claude"
        self.source = self.codex / "sessions" / "2026" / "rollout.jsonl"
        self.source.parent.mkdir(parents=True)
        self.claude_source = self.claude / "projects" / "synthetic" / "session.jsonl"
        self.claude_source.parent.mkdir(parents=True)
        self.plan = self.base / "plan.json"
        self.receipt = self.base / "receipt.json"

    def tearDown(self):
        self.tmp.cleanup()

    def old(self, path):
        old = time.time() - core.QUIET_SECONDS - 20
        os.utime(path, (old, old))

    def put(self, path, records=None, raw=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = raw if raw is not None else b"".join(canonical(r) + b"\n" for r in records)
        path.write_bytes(data)
        path.chmod(0o640)
        self.old(path)
        return data

    def scan(self, **options):
        return core.scan(self.codex, source="codex", **options)

    def prepare(self, identity=None):
        identity = identity or self.scan()["messages"][0]["id"]
        return core.make_plan(self.codex, [identity], self.plan, source="codex")["confirmation"]

    def apply(self, confirmation):
        return core.apply_plan(self.plan, confirmation, self.receipt)

    def state(self):
        path = self.codex / "state_5.sqlite"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,title TEXT NOT NULL,first_user_message TEXT NOT NULL,preview TEXT,name TEXT,history_mode TEXT)")
        db.executemany("INSERT INTO threads VALUES(?,?,?,?,?,?)", [
            ("selected", INSULT, INSULT, INSULT, INSULT, "paginated"),
            ("keep", USEFUL, USEFUL, USEFUL, None, "paginated"),
            ("derived", "A summary about frustration", "unmatched original", "You are", "custom name", "legacy"),
        ])
        db.commit()
        db.close()
        path.chmod(0o640)
        self.old(path)
        return path

    def history(self):
        path = self.codex / "thread_history_1.sqlite"
        db = sqlite3.connect(path)
        db.executescript("""
        CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,rollout_ordinal INTEGER, PRIMARY KEY(thread_id,turn_id,item_id));
        CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,item_json TEXT,item_type TEXT, PRIMARY KEY(thread_id,item_id));
        CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER,PRIMARY KEY(thread_id,turn_id));
        CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER);
        """)
        db.executemany("INSERT INTO thread_items VALUES(?,?,?,?,?)", [
            ("thread", "turn", "user", canonical(ui(INSULT)).decode(), 1),
            ("thread", "turn", "assistant", canonical({"type": "agentMessage", "text": INSULT, "id": "assistant"}).decode(), 2),
            ("thread", "turn2", "keep", canonical(ui(USEFUL)).decode(), 3),
        ])
        db.execute("INSERT INTO thread_realtime_items VALUES(?,?,?,?)", (
            "thread", "realtime", canonical({"type": "transcript_segment", "role": "user", "text": INSULT, "id": "segment"}).decode(), "transcript_segment"))
        db.execute("INSERT INTO thread_turns VALUES('thread','turn',1,'completed',0,99)")
        db.execute("INSERT INTO thread_history_projection_state VALUES('thread',99,3)")
        db.commit()
        db.close()
        path.chmod(0o640)
        self.old(path)
        return path

    def rows(self, path, table):
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db, db:
            return db.execute('SELECT * FROM "' + table + '" ORDER BY 1,2').fetchall()

    def test_combined_plan_expands_only_exact_copies_across_both_apps(self):
        self.put(self.source, [response(INSULT), response(USEFUL)])
        self.put(self.claude_source, [{"type": "user", "message": {"role": "user", "content": INSULT}},
                                     {"type": "user", "message": {"role": "user", "content": INSULT + " Fix the totals."}}])
        result = core.scan(source="all", claude_root=self.claude, codex_root=self.codex, preview=True)
        self.assertEqual({m["source"] for m in result["messages"]}, {"claude", "codex"})
        self.assertEqual(len({m["id"] for m in result["messages"]}), 2)
        plan = core.make_plan(None, [result["messages"][0]["id"]], self.plan,
                              source="all", claude_root=self.claude, codex_root=self.codex)
        self.assertEqual(plan["matching_records"], 2)
        self.assertEqual(plan["files_to_change"], 2)
        self.assertNotIn(INSULT, self.plan.read_text())
        self.apply(plan["confirmation"])
        self.assertTrue(core.verify(self.receipt)["verified"])
        self.assertNotIn(INSULT, json.loads(self.source.read_text().splitlines()[0])["payload"]["content"][0]["text"])
        records = list(map(json.loads, self.claude_source.read_text().splitlines()))
        self.assertEqual(records[0]["message"]["content"], core.TOMBSTONE)
        self.assertEqual(records[1]["message"]["content"], INSULT + " Fix the totals.")

    def test_desktop_sqlite_text_and_item_mirrors_preserve_every_other_value(self):
        self.put(self.source, [response(INSULT), response(USEFUL)])
        state, history = self.state(), self.history()
        before_state = self.rows(state, "threads")
        protected = {name: self.rows(history, name) for name in ("thread_turns", "thread_history_projection_state")}
        before_items = self.rows(history, "thread_items")
        confirmation = self.prepare()
        dry = core.apply_plan(self.plan, None, None, dry_run=True)
        self.assertEqual(dry["files_to_change"], 3)
        self.assertEqual(self.rows(state, "threads"), before_state)
        self.assertTrue(self.apply(confirmation)["verified"])
        after = self.rows(state, "threads")
        for old, new in zip(before_state, after):
            if old[0] == "selected":
                self.assertEqual(new, ("selected", *(core.TOMBSTONE for _ in range(4)), "paginated"))
            else:
                self.assertEqual(old, new)
        items = self.rows(history, "thread_items")
        for old, new in zip(before_items, items):
            if old[2] == "user":
                self.assertEqual(new[:3], old[:3])
                item = json.loads(new[3])
                self.assertEqual(item["id"], "item-id")
                self.assertEqual(item["content"][0]["text_elements"], [])
                self.assertEqual(item["content"][0]["text"], core.TOMBSTONE)
                self.assertEqual(new[4:], old[4:])
            else:
                self.assertEqual(old, new)
        for table, rows in protected.items():
            self.assertEqual(self.rows(history, table), rows)
        with contextlib.closing(sqlite3.connect(history)) as db, db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(json.loads(db.execute("SELECT item_json FROM thread_realtime_items").fetchone()[0])["text"], core.TOMBSTONE)
        self.assertEqual(list(self.codex.glob(".ai-chat-cleaner-*")), [])
        self.assertEqual(state.stat().st_mode & 0o777, 0o640)

    def test_global_rich_history_and_matching_drafts_keep_document_metadata(self):
        self.put(self.source, [response(INSULT)])
        path = self.codex / ".codex-global-state.json"
        record = {"unrelated": {"title": INSULT}, "electron-persisted-atom-state": {
            "theme": "dark", "prompt-history": {"workspace": [INSULT, {"markdown": INSULT, "document": document(INSULT)}, USEFUL]},
            "composer-prompt-drafts-v1": {"thread": INSULT, "other": USEFUL},
            "composer-retained-documents-v1": {"thread": {"prompt": INSULT, "document": document(INSULT), "plainTextMode": False}},
        }}
        raw = self.put(path, raw=canonical(record) + b"\n")
        self.apply(self.prepare())
        self.assertEqual(len(path.read_bytes()), len(raw))
        after = json.loads(path.read_bytes())
        atoms = after["electron-persisted-atom-state"]
        self.assertEqual(after["unrelated"], record["unrelated"])
        self.assertEqual(atoms["theme"], "dark")
        self.assertEqual(atoms["prompt-history"]["workspace"][2], USEFUL)
        for slot in (atoms["prompt-history"]["workspace"][1], atoms["composer-retained-documents-v1"]["thread"]):
            paragraph = slot["document"]["content"][0]
            self.assertEqual(paragraph["attrs"], {"id": "keep"})
            self.assertEqual(paragraph["content"][0]["marks"], [{"type": "bold"}])
            self.assertNotEqual(paragraph["content"][0]["text"], INSULT)
        self.assertEqual(atoms["composer-prompt-drafts-v1"]["other"], USEFUL)

    def test_wal_database_scan_does_not_create_source_sidecars(self):
        self.put(self.source, [response(INSULT)])
        path = self.state()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
        self.old(path)
        before = {p.name: p.read_bytes() for p in self.codex.iterdir() if p.is_file()}
        self.scan()
        after = {p.name: p.read_bytes() for p in self.codex.iterdir() if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(self.apply(self.prepare())["verified"])

    def test_sqlite_backup_works_without_python311_result_constants(self):
        self.put(self.source, [response(INSULT)])
        self.state()
        removed = {key: sqlite3.__dict__.pop(key) for key in ("SQLITE_BUSY", "SQLITE_LOCKED") if key in sqlite3.__dict__}
        try:
            self.assertTrue(self.scan()["messages"])
            self.assertTrue(self.apply(self.prepare())["verified"])
        finally:
            sqlite3.__dict__.update(removed)

    def test_nonempty_database_sidecars_block_without_changing_sources(self):
        self.put(self.source, [response(INSULT)])
        path = self.state()
        original = path.read_bytes()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            self.put(sidecar, raw=b"synthetic-active-sidecar")
            with self.subTest(suffix=suffix), self.assertRaises(Refusal):
                self.prepare()
            self.assertFalse(self.plan.exists())
            self.assertEqual(path.read_bytes(), original)
            sidecar.unlink()

    def test_unknown_database_version_and_schema_refuse_private_cli(self):
        self.put(self.source, [response(INSULT)])
        unknown = self.codex / "state_6.sqlite"
        self.put(unknown, raw=b"unsupported")
        with self.assertRaises(Refusal):
            self.scan()
        unknown.unlink()
        path = self.state()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE threads ADD COLUMN future_prompt TEXT DEFAULT 'synthetic-sensitive-marker'")
        self.old(path)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(["scan", "--source", "codex", "--root", str(self.codex)])
        self.assertEqual(code, 2)
        self.assertNotIn("synthetic-sensitive-marker", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_database_change_after_plan_refuses_before_any_write(self):
        raw = self.put(self.source, [response(INSULT)])
        path = self.state()
        confirmation = self.prepare()
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE threads SET title='updated title' WHERE id='keep'")
        self.old(path)
        with self.assertRaisesRegex(Refusal, "stale plan"):
            self.apply(confirmation)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertFalse(self.receipt.exists())

    def test_new_sidecar_after_plan_refuses_before_any_write(self):
        raw = self.put(self.source, [response(INSULT)])
        path = self.state()
        confirmation = self.prepare()
        self.put(Path(str(path) + "-wal"), raw=b"active")
        with self.assertRaises(Refusal):
            self.apply(confirmation)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertFalse(self.receipt.exists())

    def test_inventory_race_during_sqlite_staging_cleans_redacted_temp(self):
        raw = self.put(self.source, [response(INSULT)])
        self.state()
        confirmation = self.prepare()
        real_stage = codex_state.stage
        def change(*args):
            temporary = real_stage(*args)
            self.put(self.codex / "archived_sessions" / "new.jsonl", [response(USEFUL)])
            return temporary
        with patch("ai_chat_cleaner.codex_state.stage", side_effect=change):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["files_changed"], 0)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(list(self.codex.glob(".ai-chat-cleaner-*")), [])

    def test_partial_sqlite_replace_failure_reports_actual_changed_count(self):
        self.put(self.source, [response(INSULT)])
        path = self.state()
        confirmation = self.prepare()
        real_replace = os.replace
        def fail(source, target):
            if Path(target) == path:
                raise OSError("synthetic failure")
            return real_replace(source, target)
        with patch("ai_chat_cleaner.core.os.replace", side_effect=fail):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["status"], "incomplete")
        self.assertEqual(failure.exception.result["files_changed"], 1)
        with self.assertRaises(Refusal):
            core.verify(self.receipt)

    def test_unmatched_database_mirrors_are_not_new_human_candidates(self):
        self.put(self.source, [response(USEFUL)])
        self.state()
        self.assertEqual(self.scan()["candidates"], 0)

    def test_internal_and_unrecognized_codex_origins_are_copy_only(self):
        origins = [
            {"source": {"internal": "guardian"}},
            {"source": {"internal": "memory_consolidation"}},
            {"source": {"custom": "future"}},
            {"source": {"subagent": "review"}},
            {"source": "cli", "thread_source": "memory_consolidation"},
            {"source": "mcp", "thread_source": "unknown-feature"},
            {"source": "unknown-future-origin"},
        ]
        for payload in origins:
            with self.subTest(payload=payload):
                self.put(self.source, [{"type": "session_meta", "payload": payload}, response(INSULT)])
                self.assertEqual(self.scan()["candidates"], 0)
        self.put(self.source, [{"type": "session_meta", "payload": {"source": "mcp", "thread_source": "voice_chat"}}, response(INSULT)])
        self.assertEqual(self.scan()["candidates"], 1)

    def test_mismatched_rich_document_blocks_only_its_selected_prompt(self):
        self.put(self.source, [response(INSULT), response(USEFUL)])
        path = self.codex / ".codex-global-state.json"
        record = {"electron-persisted-atom-state": {"prompt-history": {
            "workspace": [{"markdown": INSULT, "document": document(USEFUL)}]}}}
        raw = self.put(path, raw=canonical(record))
        before = self.source.read_bytes()
        with self.assertRaisesRegex(Refusal, "unproven rich document"):
            self.prepare()
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(path.read_bytes(), raw)
        selected = next(m["id"] for m in self.scan(include_all=True, preview=True)["messages"] if m["text"] == USEFUL)
        confirmation = self.prepare(selected)
        self.assertTrue(self.apply(confirmation)["verified"])
        self.assertEqual(path.read_bytes(), raw)

    def test_mismatched_retained_document_refuses_without_erasing_useful_text(self):
        self.put(self.source, [response(INSULT)])
        path = self.codex / ".codex-global-state.json"
        record = {"electron-persisted-atom-state": {"composer-retained-documents-v1": {
            "thread": {"prompt": INSULT, "document": document(USEFUL), "plainTextMode": False}}}}
        raw = self.put(path, raw=canonical(record))
        with self.assertRaisesRegex(Refusal, "unproven rich document"):
            self.prepare()
        self.assertEqual(path.read_bytes(), raw)

    def test_sqlite_staging_cleanup_paths_are_preserved_in_failure_receipt(self):
        self.put(self.source, [response(INSULT)])
        self.state()
        confirmation = self.prepare()
        leftover = self.codex / ".ai-chat-cleaner-sqlite-leftover"
        self.put(leftover, raw=b"redacted synthetic data")
        error = Refusal("synthetic staging cleanup failed")
        error.temporary_files_to_check = [str(leftover)]
        with patch("ai_chat_cleaner.codex_state.stage", side_effect=error):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["temporary_files_to_check"], [str(leftover)])
        self.assertEqual(failure.exception.result["files_changed"], 0)
        self.assertEqual(json.loads(self.receipt.read_bytes())["temporary_files_to_check"], [str(leftover)])

    def test_sqlite_activated_after_first_replace_reports_partial_apply(self):
        self.put(self.source, [response(INSULT)])
        state = self.state()
        before = state.read_bytes()
        confirmation = self.prepare()
        real_replace = os.replace
        def activate(source, target):
            result = real_replace(source, target)
            if Path(target) == self.source:
                self.put(Path(str(state) + "-wal"), raw=b"new writer")
            return result
        with patch("ai_chat_cleaner.core.os.replace", side_effect=activate):
            with self.assertRaises(core.ApplyFailure) as failure:
                self.apply(confirmation)
        self.assertEqual(failure.exception.result["files_changed"], 1)
        self.assertEqual(failure.exception.result["status"], "incomplete")
        self.assertEqual(state.read_bytes(), before)

    def test_unknown_nonempty_queue_refuses_instead_of_skipping(self):
        self.put(self.source, [response(INSULT)])
        path = self.codex / "queue_1.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE queued_items(id TEXT PRIMARY KEY,thread_id TEXT,payload_json TEXT)")
            db.execute("INSERT INTO queued_items VALUES('queue','thread',?)", (canonical({"future_type": INSULT}).decode(),))
        self.old(path)
        with self.assertRaises(Refusal):
            self.scan()

    def test_both_roots_and_outputs_cannot_overlap(self):
        self.put(self.source, [response(INSULT)])
        with self.assertRaises(Refusal):
            core.scan(source="all", claude_root=self.codex, codex_root=self.codex)
        with self.assertRaises(Refusal):
            core.scan(self.codex, source="all")
        self.put(self.claude_source, [{"type": "user", "message": {"role": "user", "content": INSULT}}])
        result = core.scan(source="all", claude_root=self.claude, codex_root=self.codex)
        for root in (self.codex, self.claude):
            with self.subTest(root=root), self.assertRaises(Refusal):
                core.make_plan(None, [result["messages"][0]["id"]], root / "plan.json",
                               source="all", claude_root=self.claude, codex_root=self.codex)

    def test_cli_combined_report_is_private_and_without_prompt_text(self):
        self.put(self.source, [response(INSULT)])
        self.put(self.claude_source, [{"type": "user", "message": {"role": "user", "content": INSULT}}])
        out, report = io.StringIO(), self.base / "report.json"
        with contextlib.redirect_stdout(out):
            code = main(["scan", "--source", "all", "--claude-root", str(self.claude), "--codex-root", str(self.codex),
                         "--preview", "--report", str(report)])
        self.assertEqual(code, 0)
        self.assertIn(INSULT, out.getvalue())
        self.assertNotIn(INSULT, report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_old_plan_version_requires_rescan(self):
        self.put(self.source, [response(INSULT)])
        self.prepare()
        value = json.loads(self.plan.read_bytes())
        value["version"] = 1
        self.plan.write_bytes(canonical(value))
        with self.assertRaisesRegex(Refusal, "create a new plan"):
            self.apply(value["id"])

    def test_cannot_follow_codex_archive_or_database_symlinks(self):
        self.put(self.source, [response(INSULT)])
        archive = self.codex / "archived_sessions"
        archive.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(Refusal):
            self.scan()
        archive.unlink()
        state = self.state()
        alias = self.codex / "queue_1.sqlite"
        alias.symlink_to(state)
        with self.assertRaises(Refusal):
            self.scan()


if __name__ == "__main__":
    unittest.main()
