"""Small JSON CLI. Sensitive previews require an explicit flag."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .core import ApplyFailure, apply_plan, make_plan, scan, verify
from .storage import Refusal, metadata_path, write_private


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review selected local Claude Code prompts before redaction.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    scanning = commands.add_parser("scan", help="read only; conservative candidate discovery")
    scanning.add_argument("--root", default=str(Path.home() / ".claude"))
    scanning.add_argument("--all", action="store_true", help="include all supported human prompts for manual review")
    scanning.add_argument("--preview", action="store_true", help="explicitly show full prompt text in stdout only")
    scanning.add_argument("--report", help="write private metadata only, never prompt text")
    planning = commands.add_parser("plan", help="prepare an exact selection and copy coverage")
    planning.add_argument("--root", default=str(Path.home() / ".claude"))
    planning.add_argument("--select", nargs="+", required=True, metavar="ID")
    planning.add_argument("--out", required=True)
    applying = commands.add_parser("apply", help="dry run first; explicit confirmation required for changes")
    applying.add_argument("--plan", required=True)
    applying.add_argument("--dry-run", action="store_true")
    applying.add_argument("--confirm")
    applying.add_argument("--receipt")
    checking = commands.add_parser("verify", help="verify exact changed files and permissions")
    checking.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            result = scan(args.root, args.preview, args.all)
            if args.report:
                private = scan(args.root, False, args.all) if args.preview else result
                path = metadata_path(args.report, Path(result["root"]))
                write_private(path, private)
        elif args.command == "plan":
            result = make_plan(args.root, args.select, args.out)
        elif args.command == "apply":
            if not args.dry_run and not args.receipt:
                raise Refusal("apply requires --receipt before changing files")
            result = apply_plan(args.plan, args.confirm, args.receipt, args.dry_run)
        else:
            result = verify(args.receipt)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except ApplyFailure as exc:
        print(json.dumps(exc.result), file=sys.stderr)
        return 2
    except (Refusal, OSError, TypeError, KeyError, IndexError, UnicodeError) as exc:
        # Do not echo malformed JSON or selected prompt text in failures.
        message = str(exc) if isinstance(exc, Refusal) else "file operation or schema validation failed"
        print(json.dumps({"error": message, "status": "refused"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
