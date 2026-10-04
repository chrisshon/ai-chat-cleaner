"""Read known Codex SQLite mirrors and stage redacted databases from RAM.

The source connection is read-only. This module never checkpoints, vacuums,
updates, or replaces a source database. The caller owns inventory, sidecar,
quiet-time, stale-plan, and atomic-replacement gates.
"""
import os
from pathlib import Path
import sqlite3
import tempfile

from .storage import Refusal, checked_path, load_json, safe_file

TOMBSTONE = "[User text removed locally by ai-chat-cleaner]"

# These are observed native schemas, not a generic database string search.
_MIGRATIONS = {"version", "description", "installed_on", "success", "checksum", "execution_time"}
_SCHEMAS = {
    "state_5.sqlite": {
        "_sqlx_migrations": _MIGRATIONS,
        "threads": set("id rollout_path created_at updated_at source model_provider cwd title sandbox_policy approval_mode tokens_used has_user_event archived archived_at git_sha git_branch git_origin_url cli_version first_user_message agent_nickname agent_role memory_mode model reasoning_effort agent_path created_at_ms updated_at_ms thread_source preview recency_at recency_at_ms history_mode name is_pinned thread_section_id section_position section_entered_at_ms project_id originator daybreak_enabled creator_user_id creator_account_id".split()),
        "backfill_state": set("id status last_watermark last_success_at updated_at".split()),
        "external_agent_config_imports": set("import_id completed_at_ms successes failures provider_id".split()),
        "project_idempotency_keys": set("key project_id created_at_ms".split()),
        "project_roots": set("project_id position path".split()),
        "projects": set("id name metadata position created_at_ms updated_at_ms".split()),
        "remote_control_enrollments": set("websocket_url account_id app_server_client_name server_id environment_id server_name updated_at remote_control_enabled".split()),
        "rollout_migration_skipped_rollouts": set("migration_id rollout_path rollout_size_bytes rollout_modified_at_ns skip_reason skipped_at".split()),
        "rollout_migration_state": set("migration_id last_checked_thread_created_at last_checked_thread_id updated_at".split()),
        "thread_attachments": set("id thread_id attachment_type identity_key payload created_at".split()),
        "thread_dynamic_tools": set("thread_id position name description input_schema defer_loading namespace".split()),
        "thread_sections": set("id name appearance".split()),
        "thread_spawn_edges": set("parent_thread_id child_thread_id status".split()),
    },
    "thread_history_1.sqlite": {
        "_sqlx_migrations": _MIGRATIONS,
        "thread_items": set("thread_id turn_id item_id rollout_ordinal created_at_ms item_json item_type updated_at_ordinal started_at_ms completed_at_ms".split()),
        "thread_realtime_items": set("thread_id item_id rollout_ordinal created_at_ms item_type item_json".split()),
        "thread_turns": set("thread_id turn_id rollout_ordinal status error_json started_at completed_at duration_ms first_user_item_id final_agent_item_id rollout_byte_offset rollout_end_ordinal rollout_end_byte_offset".split()),
        "thread_history_projection_state": set("thread_id next_rollout_byte_offset next_rollout_ordinal".split()),
    },
    "queue_1.sqlite": {
        "_sqlx_migrations": _MIGRATIONS,
        "queued_items": set("id thread_id payload_json queue_order created_at_ms updated_at_ms".split()),
        "queued_thread_revisions": {"revision", "thread_id"},
    },
}
_CELLS = {
    "state_5.sqlite": [("threads", ("id",), column, None)
                       for column in ("first_user_message", "title", "preview", "name")],
    "thread_history_1.sqlite": [
        ("thread_items", ("thread_id", "turn_id", "item_id"), "item_json", "item"),
        ("thread_realtime_items", ("thread_id", "item_id"), "item_json", "item"),
    ],
    "queue_1.sqlite": [("queued_items", ("id",), "payload_json", "queue")],
}
_REQUIRED = {
    "state_5.sqlite": {"threads": {"id", "title", "first_user_message"}},
    "thread_history_1.sqlite": {
        "thread_items": {"thread_id", "turn_id", "item_id", "item_json"},
        "thread_turns": {"thread_id", "turn_id", "rollout_ordinal", "status"},
        "thread_history_projection_state": {"thread_id", "next_rollout_byte_offset", "next_rollout_ordinal"},
    },
    "queue_1.sqlite": {"queued_items": {"id", "thread_id", "payload_json"}},
}


def _quote(name):
    # All identifier callers are schema-allowlisted, never user-controlled SQL.
    return '"' + name.replace('"', '""') + '"'


def _integrity(db):
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise Refusal("Codex SQLite integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise Refusal("Codex SQLite foreign-key integrity check failed")


def _schema(db, name):
    if name not in _SCHEMAS:
        raise Refusal("unsupported Codex SQLite version")
    columns = {}
    for kind, table in db.execute("SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')"):
        if table.startswith("sqlite_"):
            continue
        if kind != "table" or table not in _SCHEMAS[name]:
            raise Refusal("unsupported Codex SQLite table, view, or trigger")
        info = db.execute("PRAGMA table_info(" + _quote(table) + ")").fetchall()
        actual = {row[1] for row in info}
        if not actual or not actual <= _SCHEMAS[name][table]:
            raise Refusal("unsupported Codex SQLite columns")
        columns[table] = actual
        for cell_table, keys, column, _ in _CELLS[name]:
            if cell_table != table or column not in actual:
                continue
            if not set(keys) <= actual:
                raise Refusal("unsupported Codex SQLite row keys")
            primary = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
            if primary != keys:
                raise Refusal("unsupported Codex SQLite primary key")
            if any(row[2].upper() != "TEXT" for row in info if row[1] in (*keys, column)):
                raise Refusal("unsupported Codex SQLite text-column type")
    for table, required in _REQUIRED[name].items():
        if not required <= columns.get(table, set()):
            raise Refusal("incomplete Codex SQLite schema")
    return columns


def _snapshot(path):
    path = checked_path(path)
    safe_file(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            if safe_file(sidecar).st_size:
                raise Refusal("Codex SQLite sidecars remain; close all Codex clients before scanning")
    source = memory = None
    try:
        # Immutable reads never create or update WAL/shared-memory sidecars.
        # Only use them after rejecting every nonempty existing sidecar.
        with path.open("rb") as handle:
            header = handle.read(20)
        if len(header) != 20 or header[:16] != b"SQLite format 3\x00" or header[18:20] not in (b"\x01\x01", b"\x02\x02"):
            raise Refusal("unsupported Codex SQLite file header")
        source = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        source.execute("PRAGMA query_only=ON")
        # Immutable SQLite reports 'delete' even for a checkpointed WAL header.
        journal_mode = "wal" if header[18:20] == b"\x02\x02" else "delete"
        memory = sqlite3.connect(":memory:")
        memory.execute("PRAGMA temp_store=MEMORY")

        def progress(status, remaining, total):
            if status in (getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)):
                raise Refusal("Codex SQLite is busy; close Codex before retrying")

        source.backup(memory, pages=1024, progress=progress, sleep=0.01)
        source.close()
        source = None
        _integrity(memory)
        columns = _schema(memory, path.name)
        return memory, columns, journal_mode
    except (sqlite3.Error, OSError) as exc:
        if memory is not None:
            memory.close()
        raise Refusal("cannot read a supported Codex SQLite database") from exc
    except BaseException:
        if memory is not None:
            memory.close()
        raise
    finally:
        if source is not None:
            source.close()


def _at(value, path):
    try:
        for key in path:
            value = value[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise Refusal("invalid Codex SQLite JSON path") from exc
    if not isinstance(value, str):
        raise Refusal("Codex SQLite JSON path is not text")
    return value


def read(path, record_prompts):
    """Return copy-only text groups; never make SQLite rows new candidates."""
    path = checked_path(path)
    db, columns, _ = _snapshot(path)
    groups = []
    try:
        for table, keys, column, hook in _CELLS[path.name]:
            if column not in columns.get(table, set()):
                continue
            query = "SELECT " + ",".join(_quote(k) for k in (*keys, column)) + " FROM " + _quote(table)
            query += " ORDER BY " + ",".join(_quote(k) for k in keys)
            for row in db.execute(query):
                key_values = dict(zip(keys, row[:-1]))
                if any(not isinstance(v, str) or not v for v in key_values.values()):
                    raise Refusal("unsupported Codex SQLite row identity")
                value = row[-1]
                if value is None and hook is None:
                    continue
                if not isinstance(value, str):
                    raise Refusal("unsupported Codex SQLite text value")
                cell = {"table": table, "keys": key_values, "column": column}
                if hook is None:
                    if value:
                        groups.append({"text": value, "paths": [()], "cell": cell, "copy_only": True})
                    continue
                if hook == "queue":
                    raise Refusal("nonempty Codex queued operations must be drained before cleanup")
                decoded = load_json(value)
                for group in record_prompts(decoded, hook):
                    paths = [tuple(p) for p in group["paths"]]
                    text = group.get("text")
                    if text is None:
                        text = "\n".join(_at(decoded, p) for p in paths)
                    if not isinstance(text, str):
                        raise Refusal("unsupported Codex SQLite prompt group")
                    if text and paths:
                        groups.append({"text": text, "paths": paths, "cell": cell, "copy_only": True})
        return groups
    except (sqlite3.Error, UnicodeError) as exc:
        raise Refusal("cannot read supported Codex SQLite text") from exc
    finally:
        db.close()


def _field(occurrence, name):
    return occurrence[name] if isinstance(occurrence, dict) else getattr(occurrence, name)


def _validate_json_change(before, after, paths, current=()):
    if current in paths:
        if not isinstance(before, str) or after not in (TOMBSTONE, ""):
            raise Refusal("invalid Codex SQLite text replacement")
    elif isinstance(before, dict):
        if not isinstance(after, dict) or set(before) != set(after):
            raise Refusal("Codex SQLite JSON metadata changed")
        for key in before:
            _validate_json_change(before[key], after[key], paths, current + (key,))
    elif isinstance(before, list):
        if not isinstance(after, list) or len(before) != len(after):
            raise Refusal("Codex SQLite JSON item structure changed")
        for index, value in enumerate(before):
            _validate_json_change(value, after[index], paths, current + (index,))
    elif type(before) is not type(after) or before != after:
        raise Refusal("Codex SQLite unselected value changed")


def stage(path, occurrences, mode, transform_json):
    """Return a private staged database containing only the redacted snapshot.

    JSON transforms receive UTF-8 bytes and the occurrences for one cell. All
    identifiers are allowlisted and all values are bound SQL parameters.
    """
    path = checked_path(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            if safe_file(sidecar).st_size:
                raise Refusal("Codex SQLite sidecars remain; close Codex before staging")
    db, columns, journal_mode = _snapshot(path)
    temporary = target = None
    completed = False
    try:
        groups = {}
        for occurrence in occurrences:
            cell = _field(occurrence, "cell")
            if not isinstance(cell, dict) or set(cell) != {"table", "keys", "column"}:
                raise Refusal("invalid Codex SQLite cell selection")
            table, column, values = cell["table"], cell["column"], cell["keys"]
            allowed = next((entry for entry in _CELLS[path.name] if entry[0] == table and entry[2] == column), None)
            if allowed is None or column not in columns.get(table, set()):
                raise Refusal("unsupported Codex SQLite cell selection")
            keys = allowed[1]
            if not isinstance(values, dict) or set(values) != set(keys) or any(not isinstance(values[k], str) or not values[k] for k in keys):
                raise Refusal("invalid Codex SQLite cell identity")
            identity = (table, tuple((k, values[k]) for k in keys), column, allowed[3])
            groups.setdefault(identity, []).append(occurrence)
        if not groups:
            raise Refusal("no Codex SQLite cells selected")
        db.execute("PRAGMA secure_delete=ON")
        for (table, key_pairs, column, hook), selected in groups.items():
            where = " AND ".join(_quote(k) + "=?" for k, _ in key_pairs)
            params = tuple(v for _, v in key_pairs)
            row = db.execute("SELECT " + _quote(column) + " FROM " + _quote(table) + " WHERE " + where, params).fetchone()
            if row is None or not isinstance(row[0], str):
                raise Refusal("Codex SQLite selected cell disappeared")
            value = row[0]
            if hook is None:
                if any([tuple(p) for p in _field(o, "paths")] != [()] or _field(o, "text") != value for o in selected):
                    raise Refusal("stale Codex SQLite plain-text selection")
                redacted = TOMBSTONE
            else:
                before = load_json(value)
                paths = {tuple(p) for o in selected for p in _field(o, "paths")}
                for occurrence in selected:
                    if "\n".join(_at(before, tuple(p)) for p in _field(occurrence, "paths")) != _field(occurrence, "text"):
                        raise Refusal("stale Codex SQLite JSON selection")
                redacted_bytes = transform_json(value.encode("utf-8"), selected)
                redacted = redacted_bytes.decode("utf-8")
                _validate_json_change(before, load_json(redacted), paths)
            updated = db.execute("UPDATE " + _quote(table) + " SET " + _quote(column) + "=? WHERE " + where, (redacted, *params))
            if updated.rowcount != 1:
                raise Refusal("Codex SQLite selected row is not unique")
        db.commit()
        db.execute("VACUUM")
        _integrity(db)
        fd, name = tempfile.mkstemp(prefix=".ai-chat-cleaner-sqlite-", dir=path.parent)
        temporary = Path(name)
        os.close(fd)
        target = sqlite3.connect(str(temporary))
        db.backup(target)
        if journal_mode not in {"delete", "truncate", "persist", "memory", "wal", "off"}:
            raise Refusal("unsupported Codex SQLite journal mode")
        restored = target.execute("PRAGMA journal_mode=" + journal_mode).fetchone()[0]
        if restored != journal_mode:
            raise Refusal("cannot preserve Codex SQLite journal mode")
        _integrity(target)
        if journal_mode == "wal" and target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
            raise Refusal("cannot checkpoint the staged Codex SQLite database")
        target.close()
        target = None
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(temporary) + suffix)
            if sidecar.exists():
                if suffix != "-shm" and sidecar.stat().st_size:
                    raise Refusal("staged Codex SQLite sidecar was not checkpointed")
                sidecar.unlink()
        with temporary.open("rb") as handle:
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        completed = True
        return temporary
    except (sqlite3.Error, OSError, UnicodeError) as exc:
        raise Refusal("cannot stage the redacted Codex SQLite database") from exc
    finally:
        if target is not None:
            target.close()
        db.close()
        if temporary is not None and not completed:
            # Source databases have not changed. Cleanup errors must not mask
            # the stage error; name only leftover redacted paths for checking.
            pending = []
            for leftover in (temporary, *(Path(str(temporary) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pending.append(str(leftover))
            if pending:
                error = Refusal("redacted SQLite staging cleanup failed; check " + ", ".join(pending))
                error.temporary_files_to_check = pending
                raise error
