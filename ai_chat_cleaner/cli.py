"""Small JSON CLI. Sensitive previews require an explicit flag."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .core import ApplyFailure, apply_plan, make_plan, scan, verify
from .exports import deletion_guide, review_export
from .storage import Refusal, metadata_path, write_private


def _source_args(parser):
    parser.add_argument("--source", choices=("claude", "codex", "all"), default="claude",
                        help="history provider; legacy commands default to Claude Code")
    parser.add_argument("--root", help="custom root for one provider")
    parser.add_argument("--claude-root", help="Claude root when --source all")
    parser.add_argument("--codex-root", help="Codex root when --source all")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Review native AI prompts before local redaction, or consumer exports before manual chat deletion.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    scanning = commands.add_parser("scan", help="read only; conservative candidate discovery")
    _source_args(scanning)
    scanning.add_argument("--all", action="store_true", help="include all supported human prompts for manual review")
    scanning.add_argument("--preview", action="store_true", help="explicitly show full prompt text in stdout only")
    scanning.add_argument("--report", help="write private metadata only, never prompt text")
    planning = commands.add_parser("plan", help="prepare an exact selection and copy coverage")
    _source_args(planning)
    planning.add_argument("--select", nargs="+", required=True, metavar="ID")
    planning.add_argument("--out", required=True)
    applying = commands.add_parser("apply", help="dry run first; explicit confirmation required for changes")
    applying.add_argument("--plan", required=True)
    applying.add_argument("--dry-run", action="store_true")
    applying.add_argument("--confirm")
    applying.add_argument("--receipt")
    checking = commands.add_parser("verify", help="verify exact changed files and permissions")
    checking.add_argument("--receipt", required=True)
    reviewing = commands.add_parser("review-export", help="read consumer exports; write private review metadata")
    reviewing.add_argument("--provider", choices=("chatgpt", "claude"), required=True)
    reviewing.add_argument("--input", required=True, help="extracted JSON or bounded ZIP with root conversations.json")
    reviewing.add_argument("--report", required=True, help="new private metadata file; never contains chat text")
    reviewing.add_argument("--preview", action="store_true", help="explicitly show prompt text and local context in stdout only")
    reviewing.add_argument("--all", action="store_true", help="include all supported human text for manual review")
    guiding = commands.add_parser("deletion-guide", help="make a manual whole-conversation checklist; never deletes chats")
    guiding.add_argument("--provider", choices=("chatgpt", "claude"), required=True)
    guiding.add_argument("--input", required=True)
    guiding.add_argument("--select", nargs="+", required=True, metavar="ID")
    guiding.add_argument("--out", required=True, help="new private metadata checklist, not a native plan")
    args = parser.parse_args(argv)
    try:
        if args.command in {"scan", "plan"}:
            options = {"source": args.source, "claude_root": args.claude_root, "codex_root": args.codex_root}
        if args.command == "scan":
            result = scan(args.root, args.preview, args.all, **options)
            if args.report:
                private = scan(args.root, False, args.all, **options) if args.preview else result
                path = None
                for root in result["roots"].values():
                    path = metadata_path(args.report, Path(root))
                write_private(path, private)
        elif args.command == "plan":
            result = make_plan(args.root, args.select, args.out, **options)
        elif args.command == "apply":
            if not args.dry_run and not args.receipt:
                raise Refusal("apply requires --receipt before changing files")
            result = apply_plan(args.plan, args.confirm, args.receipt, args.dry_run)
        elif args.command == "verify":
            result = verify(args.receipt)
        elif args.command == "review-export":
            result = review_export(args.provider, args.input, args.report, args.preview, args.all)
        else:
            result = deletion_guide(args.provider, args.input, args.select, args.out)
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
