"""Command-line interface for azdo-az-backup."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
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
    b.add_argument("--archive", action="store_true",
                   help="Also produce a single self-contained zip of the whole "
                        "backup, with a sha256 manifest (checksums.json).")
    b.add_argument("--archive-path",
                   help="Where to write the archive (default: <output>.zip). "
                        "Refuses to overwrite an existing file.")
    b.add_argument("--workers", type=int, default=4,
                   help="Concurrent work item fetches (default: 4). Raise for "
                        "large projects; lower if you hit rate limits.")
    b.add_argument("--exclude-projects", default="",
                   help="Comma-separated project names to skip in "
                        "--all-projects mode.")

    # restore
    r = sub.add_parser("restore", help="Restore a backed-up project (or a whole "
                                       "org backup) into new project(s).")
    _add_common_args(r)
    r.add_argument("--source", required=True,
                   help="Path to a project backup directory (containing "
                        "project.json), an org backup root (containing "
                        "projects/), or a .zip produced by backup --archive.")
    r.add_argument("--project",
                   help="Target project name (single-project restore).")
    r.add_argument("--source-project",
                   help="Which project to restore when the source contains "
                        "several (org backup or archive).")
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
    r.add_argument("--dry-run", action="store_true",
                   help="Verify the source and print what would be created, "
                        "without touching the target organization.")

    # verify (offline, no credentials needed)
    v = sub.add_parser("verify", help="Verify a backup directory or archive "
                                      "offline (integrity, completeness).")
    v.add_argument("--source", required=True,
                   help="Backup directory, org backup root, or .zip archive.")

    # list-projects (handy utility)
    lp = sub.add_parser("list-projects", help="List projects in an org.")
    _add_common_args(lp)

    return parser


def _cmd_backup(client: AzDoClient, args: argparse.Namespace) -> int:
    from .backup import backup_org, backup_project
    out = Path(args.output)
    if args.all_projects:
        excluded = {n for n in args.exclude_projects.split(",") if n.strip()}
        stats = backup_org(client, out, workers=args.workers,
                           exclude_projects=excluded)
    else:
        # Same layout as org backups so restore instructions are uniform.
        proj_dir = out / "projects" / safe_filename(args.project)
        stats = backup_project(client, args.project, proj_dir,
                               workers=args.workers)
    if args.archive or args.archive_path:
        from .verify import write_checksums
        write_checksums(out)
        if args.archive_path:
            zip_target = Path(args.archive_path)
            if zip_target.exists():
                print(f"error: {zip_target} already exists — refusing to "
                      "overwrite an archive", file=sys.stderr)
                return EXIT_ERROR
            base = str(zip_target)[:-4] if zip_target.suffix.lower() == ".zip" \
                else str(zip_target)
        else:
            base = str(out)
        zip_path = shutil.make_archive(base, "zip", root_dir=out)
        print(f"Archive: {zip_path}")
    print(json.dumps(stats.as_dict()["counts"], indent=2))
    if stats.errors:
        print(f"Backup finished with {len(stats.errors)} error(s) — "
              f"see summary.json under {out}", file=sys.stderr)
        return EXIT_PARTIAL
    print(f"Backup complete: {out}")
    return EXIT_OK


def _resolve_project_source(src: Path, source_project: str | None) -> Path:
    """Find the single project dir to restore inside an org backup root."""
    if (src / "project.json").exists():
        return src
    projects_dir = src / "projects"
    candidates = []
    if projects_dir.is_dir():
        candidates = sorted(p for p in projects_dir.iterdir()
                            if (p / "project.json").exists())
    if source_project:
        for p in candidates:
            original = json.loads((p / "project.json").read_text(encoding="utf-8"))
            if source_project.lower() in (p.name.lower(),
                                          (original.get("name") or "").lower()):
                return p
        raise AzDoError(f"project '{source_project}' not found in {src}")
    if len(candidates) == 1:
        return candidates[0]
    names = [p.name for p in candidates]
    raise AzDoError(
        f"{src} contains {len(candidates)} project backups {names} — "
        "pick one with --source-project or use --all-projects")


def _cmd_restore(client: AzDoClient | None, args: argparse.Namespace) -> int:
    from .restore import restore_project
    from .verify import dry_run_summary, safe_extract_zip, verify_backup
    src = Path(args.source)
    common = dict(
        process_template=args.process,
        visibility=args.visibility,
        skip_work_items=args.skip_work_items,
        skip_repos=args.skip_repos,
        skip_test_plans=args.skip_test_plans,
    )

    id_map_dir = None
    extracted: Path | None = None
    try:
        if src.is_file() and src.suffix.lower() == ".zip":
            extracted = Path(tempfile.mkdtemp(prefix="azdo-restore-"))
            log.info("Extracting archive %s", src)
            safe_extract_zip(src, extracted)
            # The extraction dir is ephemeral; keep the resume id-map next
            # to the archive instead.
            id_map_dir = src.parent
            src = extracted
        common["id_map_dir"] = id_map_dir

        if args.dry_run:
            report = verify_backup(args.source)
            if not args.all_projects:
                proj_src = _resolve_project_source(src, args.source_project)
                plan = [dry_run_summary(proj_src)]
            else:
                projects_dir = src / "projects" if (src / "projects").is_dir() else src
                plan = [dry_run_summary(p) for p in sorted(projects_dir.iterdir())
                        if (p / "project.json").exists()]
            print(json.dumps({"verify": report.as_dict(),
                              "would_restore": plan}, indent=2))
            return EXIT_OK if report.ok else EXIT_PARTIAL

        if not args.all_projects:
            if not args.project:
                print("error: --project is required (or use --all-projects)",
                      file=sys.stderr)
                return EXIT_USAGE
            proj_src = _resolve_project_source(src, args.source_project)
            summary = restore_project(client, proj_src, args.project, **common)
            print(json.dumps(summary, indent=2))
            if summary.get("error_count"):
                print(f"Restore finished with {summary['error_count']} "
                      "error(s) — see the summary above", file=sys.stderr)
                return EXIT_PARTIAL
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
                summary = restore_project(client, proj_src, target, **common)
                failures += 1 if summary.get("error_count") else 0
                summaries.append(summary)
            except AzDoError as exc:
                failures += 1
                log.error("restore of '%s' failed: %s", target, exc)
                summaries.append({"target_project": target, "error": str(exc)})
        print(json.dumps(summaries, indent=2))
        return EXIT_PARTIAL if failures else EXIT_OK
    finally:
        if extracted is not None:
            shutil.rmtree(extracted, ignore_errors=True)


def _cmd_verify(args: argparse.Namespace) -> int:
    from .verify import verify_backup
    report = verify_backup(args.source)
    print(json.dumps(report.as_dict(), indent=2))
    if not report.ok:
        print(f"error: verification found {len(report.problems)} problem(s)",
              file=sys.stderr)
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        # Offline commands never need credentials.
        if args.cmd == "verify":
            return _cmd_verify(args)
        if args.cmd == "restore" and args.dry_run:
            return _cmd_restore(None, args)

        try:
            client = AzDoClient(args.org, pat=args.pat)
        except AzDoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_USAGE

        if args.cmd == "list-projects":
            for p in client.list_projects():
                print(f"{p['id']}\t{p['name']}\t{p.get('state')}")
            return EXIT_OK
        if args.cmd == "backup":
            return _cmd_backup(client, args)
        if args.cmd == "restore":
            return _cmd_restore(client, args)
    except (AzDoError, requests.RequestException, zipfile.BadZipFile,
            ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    return EXIT_OK
