"""Command-line interface for azdo-az-backup."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import AzDoClient, AzDoError
from .util import get_logger, safe_filename

log = get_logger(__name__)


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--org", required=True,
                   help="Azure DevOps org URL, e.g. https://dev.azure.com/myorg")
    p.add_argument("--pat", default=None,
                   help="Personal Access Token (defaults to $AZURE_DEVOPS_EXT_PAT).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="azdo-backup",
        description="Backup and restore Azure DevOps projects/organizations.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # backup
    b = sub.add_parser("backup", help="Back up a project or entire organization.")
    _add_common_args(b)
    scope = b.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", help="Back up only this project (name or ID).")
    scope.add_argument("--all-projects", action="store_true",
                       help="Back up every project in the org.")
    b.add_argument("--output", "-o", required=True,
                   help="Output directory.")

    # restore
    r = sub.add_parser("restore", help="Restore a backed-up project into a (new) project.")
    _add_common_args(r)
    r.add_argument("--source", required=True,
                   help="Path to a backed-up project directory (the one containing project.json).")
    r.add_argument("--project", required=True,
                   help="Target project name to create / restore into.")
    r.add_argument("--process", default="Agile",
                   help="Process template for the new project (default: Agile).")
    r.add_argument("--visibility", default="private", choices=["private", "public"])
    r.add_argument("--skip-work-items", action="store_true")
    r.add_argument("--skip-repos", action="store_true")
    r.add_argument("--skip-test-plans", action="store_true")

    # list-projects (handy utility)
    lp = sub.add_parser("list-projects", help="List projects in an org.")
    _add_common_args(lp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = AzDoClient(args.org, pat=args.pat)
    except AzDoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "list-projects":
            for p in client.list_projects():
                print(f"{p['id']}\t{p['name']}\t{p.get('state')}")
            return 0

        if args.cmd == "backup":
            from .backup import backup_org, backup_project
            out = Path(args.output)
            if args.all_projects:
                backup_org(client, out)
            else:
                proj_dir = out / safe_filename(args.project)
                backup_project(client, args.project, proj_dir)
            print(f"Backup complete: {out}")
            return 0

        if args.cmd == "restore":
            from .restore import restore_project
            summary = restore_project(
                client, args.source, args.project,
                process_template=args.process,
                visibility=args.visibility,
                skip_work_items=args.skip_work_items,
                skip_repos=args.skip_repos,
                skip_test_plans=args.skip_test_plans,
            )
            print(json.dumps(summary, indent=2))
            return 0
    except AzDoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    return 0
