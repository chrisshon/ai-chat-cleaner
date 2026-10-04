"""Path checks, private metadata, and exact JSON string-token replacement."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile


class Refusal(ValueError):
    """A validation failure, with no transcript text in its message."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def checked_path(value):
    if os.name != "posix":
        raise Refusal("requires macOS/Linux for private file permissions and atomic writes")
    path = Path(value).expanduser().absolute()
    # Check every ancestor, including a symlink passed as the root itself.
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise Refusal("symlink paths are unsupported")
    return Path(os.path.normpath(path))


def safe_file(path):
    checked_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refusal("source must be a regular file with one hard link")
    return info


def fingerprint(path):
    before = safe_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        actual = os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
            raise Refusal("source changed during open")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
        after = os.fstat(fd)
    finally:
        os.close(fd)
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
    if any(getattr(before, k) != getattr(after, k) for k in keys):
        raise Refusal("source changed during read")
    return data, {"sha256": digest(data), "size": after.st_size,
                  "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns,
                  "device": after.st_dev, "inode": after.st_ino,
                  "mode": stat.S_IMODE(after.st_mode)}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refusal("duplicate JSON keys are unsupported")
        result[key] = value
    return result


def load_json(data):
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Refusal("only UTF-8 JSON without BOM is supported") from exc
    if isinstance(data, str) and data.startswith("\ufeff"):
        raise Refusal("UTF-8 BOM is unsupported")
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refusal("non-finite JSON value")))
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise Refusal("malformed JSON or unsupported encoding") from exc


def string_spans(text):
    """Return value string spans by JSON path; key spelling/layout stays untouched."""
    decoder = json.JSONDecoder()
    spans = {}

    def ws(i):
        while i < len(text) and text[i] in " \t\r\n":
            i += 1
        return i

    def walk(i, path):
        i = ws(i)
        if text[i] == '"':
            _, end = decoder.raw_decode(text, i)
            spans[path] = (i, end)
            return end
        if text[i] == "{":
            i = ws(i + 1)
            while text[i] != "}":
                key, end = decoder.raw_decode(text, i)
                i = walk(ws(end) + 1, path + (key,))  # colon was validated by load_json
                i = ws(i)
                if text[i] == ",":
                    i = ws(i + 1)
            return i + 1
        if text[i] == "[":
            i, index = ws(i + 1), 0
            while text[i] != "]":
                i = ws(walk(i, path + (index,)))
                index += 1
                if text[i] == ",":
                    i = ws(i + 1)
            return i + 1
        return decoder.raw_decode(text, i)[1]

    walk(0, ())
    return spans


def metadata_path(value, root):
    path = checked_path(value)
    if path == root or root in path.parents:
        raise Refusal("reports, plans and receipts must be outside the history root")
    if not path.parent.is_dir():
        raise Refusal("metadata parent directory must already exist")
    return path


def write_private(path, value):
    """Create a new private metadata file, never overwrite an existing path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def stage(path, data, mode):
    fd, name = tempfile.mkstemp(prefix=".ai-chat-cleaner-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def replace_private(path, value):
    safe_file(path)
    temporary = stage(path, canonical(value) + b"\n", 0o600)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
