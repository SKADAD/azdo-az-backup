"""Command-line interface for azdo-az-backup."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

from .client import AzDoClient, AzDoError
from .util import get_logger, safe_filename

log = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--org", required=True,
                   help="Azure DevOps org URL, e.g. https://dev.azure.com/myorg")
    p.add_argument("--pat", default=None,
                   help="Personal Access Token (defaults to $AZURE_DEVOPS_EXT_PAT "
                        "or $AZDO_PAT).")


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
    r = sub.add_parser("restore", help="Restore a backed-up project (or a whole "
                                       "org backup) into new project(s).")
    _add_common_args(r)
    r.add_argument("--source", required=True,
                   help="Path to a project backup directory (containing "
                        "project.json), or with --all-projects the org backup "
                        "root (containing projects/).")
    r.add_argument("--project",
                   help="Target project name (single-project restore).")
    r.add_argument("--all-projects", action="store_true",
                   help="Restore every project from an org backup; target names "
                        "default to the original project names.")
    r.add_argument("--prefix", default="",
                   help="Prefix for target project names in --all-projects mode.")
    r.add_argument("--process", default=None,
                   help="Process template for the new project "
                        "(default: the source project's process, else Agile).")
    r.add_argument("--visibility", default="private", choices=["private", "public"])
    r.add_argument("--skip-work-items", action="store_true")
    r.add_argument("--skip-repos", action="store_true")
    r.add_argument("--skip-test-plans", action="store_true")

    # list-projects (handy utility)
    lp = sub.add_parser("list-projects", help="List projects in an org.")
    _add_common_args(lp)

    return parser


def _cmd_backup(client: AzDoClient, args: argparse.Namespace) -> int:
    from .backup import backup_org, backup_project
    out = Path(args.output)
    if args.all_projects:
        stats = backup_org(client, out)
    else:
        # Same layout as org backups so restore instructions are uniform.
        proj_dir = out / "projects" / safe_filename(args.project)
        stats = backup_project(client, args.project, proj_dir)
    print(json.dumps(stats.as_dict()["counts"], indent=2))
    if stats.errors:
        print(f"Backup finished with {len(stats.errors)} error(s) — "
              f"see summary.json under {out}", file=sys.stderr)
        return EXIT_PARTIAL
    print(f"Backup complete: {out}")
    return EXIT_OK


def _cmd_restore(client: AzDoClient, args: argparse.Namespace) -> int:
    from .restore import restore_project
    src = Path(args.source)
    common = dict(
        process_template=args.process,
        visibility=args.visibility,
        skip_work_items=args.skip_work_items,
        skip_repos=args.skip_repos,
        skip_test_plans=args.skip_test_plans,
    )

    if not args.all_projects:
        if not args.project:
            print("error: --project is required (or use --all-projects)",
                  file=sys.stderr)
            return EXIT_USAGE
        summary = restore_project(client, src, args.project, **common)
        print(json.dumps(summary, indent=2))
        return EXIT_OK

    projects_dir = src / "projects" if (src / "projects").is_dir() else src
    sources = sorted(p for p in projects_dir.iterdir()
                     if (p / "project.json").exists())
    if not sources:
        print(f"error: no project backups found under {projects_dir}",
              file=sys.stderr)
        return EXIT_USAGE

    summaries, failures = [], 0
    for proj_src in sources:
        original = json.loads((proj_src / "project.json").read_text(encoding="utf-8"))
        target = args.prefix + (original.get("name") or proj_src.name)
        try:
            summaries.append(restore_project(client, proj_src, target, **common))
        except AzDoError as exc:
            failures += 1
            log.error("restore of '%s' failed: %s", target, exc)
            summaries.append({"target_project": target, "error": str(exc)})
    print(json.dumps(summaries, indent=2))
    return EXIT_PARTIAL if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = AzDoClient(args.org, pat=args.pat)
    except AzDoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.cmd == "list-projects":
            for p in client.list_projects():
                print(f"{p['id']}\t{p['name']}\t{p.get('state')}")
            return EXIT_OK
        if args.cmd == "backup":
            return _cmd_backup(client, args)
        if args.cmd == "restore":
            return _cmd_restore(client, args)
    except (AzDoError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    return EXIT_OK
