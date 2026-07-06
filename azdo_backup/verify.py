"""Offline verification of backups and archives.

For archival use a backup must be checkable years later, without network
access and without attempting a restore. ``verify_backup`` validates:

- archive integrity (zip CRCs are checked during extraction),
- the sha256 manifest (``checksums.json``, written by ``backup --archive``),
- the completion marker (``summary.json`` written last, error_count == 0),
- work item completeness (every indexed ID has its JSON, parseable),
- attachment completeness (every recorded binary exists, with size > 0),
- git mirrors (bare-repository check plus ref listing),
- test plan indexes.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from .util import get_logger, run_git

log = get_logger(__name__)

CHECKSUM_FILE = "checksums.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(root: Path) -> Path:
    """Write a sha256 manifest of every file under ``root``."""
    sums = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != CHECKSUM_FILE:
            sums[path.relative_to(root).as_posix()] = sha256_file(path)
    out = root / CHECKSUM_FILE
    with open(out, "w", encoding="utf-8") as f:
        json.dump(sums, f, indent=2)
    return out


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract with zip-slip protection. Zip CRCs are validated on read."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            target = (dest / member).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise ValueError(f"unsafe path in archive: {member}")
        zf.extractall(dest)


class VerifyReport:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.counts: dict[str, int] = {}

    def problem(self, msg: str) -> None:
        self.problems.append(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {"ok": self.ok, "counts": self.counts,
                "problems": self.problems, "warnings": self.warnings}


def _read_json(path: Path, report: VerifyReport):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        report.problem(f"{path}: unreadable JSON ({exc})")
        return None


def verify_backup(source: str | Path) -> VerifyReport:
    """Verify a backup directory or a ``backup --archive`` zip."""
    source = Path(source)
    report = VerifyReport()
    tmp: Path | None = None
    try:
        if source.is_file() and source.suffix.lower() == ".zip":
            tmp = Path(tempfile.mkdtemp(prefix="azdo-verify-"))
            try:
                safe_extract_zip(source, tmp)  # CRC-checked by zipfile
            except (zipfile.BadZipFile, ValueError, OSError) as exc:
                report.problem(f"archive is not extractable: {exc}")
                return report
            root = tmp
        elif source.is_dir():
            root = source
        else:
            report.problem(f"{source} is neither a backup directory nor a .zip")
            return report

        _verify_checksums(root, report)

        project_dirs = []
        if (root / "project.json").exists():
            project_dirs = [root]
        elif (root / "projects").is_dir():
            project_dirs = sorted(p for p in (root / "projects").iterdir()
                                  if p.is_dir())
        if not project_dirs:
            report.problem(f"no project backups found under {source}")
            return report

        for proj in project_dirs:
            _verify_project(proj, report)
        return report
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def _verify_checksums(root: Path, report: VerifyReport) -> None:
    manifest_path = root / CHECKSUM_FILE
    if not manifest_path.exists():
        report.warning("no checksums.json manifest (created by backup --archive)")
        return
    manifest = _read_json(manifest_path, report)
    if manifest is None:
        return
    for rel, expected in manifest.items():
        path = root / rel
        if not path.is_file():
            report.problem(f"missing file listed in manifest: {rel}")
            continue
        actual = sha256_file(path)
        if actual != expected:
            report.problem(f"checksum mismatch: {rel}")
        report.add("files_checksummed")


def _verify_project(proj: Path, report: VerifyReport) -> None:
    name = proj.name
    if _read_json(proj / "project.json", report) is None:
        report.problem(f"[{name}] missing/invalid project.json")
        return
    report.add("projects")

    summary_path = proj / "summary.json"
    if not summary_path.exists():
        report.problem(f"[{name}] no summary.json — the backup never completed")
    else:
        summary = _read_json(summary_path, report)
        if summary and summary.get("error_count", 0) > 0:
            report.problem(f"[{name}] backup finished with "
                           f"{summary['error_count']} error(s) — see summary.json")

    _verify_work_items(proj, name, report)
    _verify_repos(proj, name, report)

    tp_index = proj / "test_plans" / "index.json"
    if tp_index.exists():
        data = _read_json(tp_index, report)
        for plan in (data or {}).get("plans", []):
            if not (proj / "test_plans" / str(plan.get("id"))).is_dir():
                report.problem(f"[{name}] test plan {plan.get('id')} indexed "
                               "but its directory is missing")
            else:
                report.add("test_plans")


def _verify_work_items(proj: Path, name: str, report: VerifyReport) -> None:
    wi_dir = proj / "work_items"
    index_path = wi_dir / "index.json"
    if not index_path.exists():
        report.warning(f"[{name}] no work_items/index.json")
        return
    index = _read_json(index_path, report)
    if index is None:
        return
    for wid in index.get("ids", []):
        wi_path = wi_dir / f"{wid}.json"
        if not wi_path.exists():
            report.problem(f"[{name}] work item {wid} indexed but not saved")
            continue
        wi = _read_json(wi_path, report)
        if wi is None:
            continue
        report.add("work_items")
        if not wi.get("revisions"):
            report.problem(f"[{name}] work item {wid} has no revision history")
        for att in wi.get("attachments_local", []) or []:
            att_path = proj / att["file"]
            if not att_path.is_file():
                report.problem(f"[{name}] work item {wid}: attachment file "
                               f"missing: {att['file']}")
            elif att_path.stat().st_size == 0:
                report.problem(f"[{name}] work item {wid}: attachment file "
                               f"empty: {att['file']}")
            else:
                report.add("attachments")


def _verify_repos(proj: Path, name: str, report: VerifyReport) -> None:
    repos_index = proj / "repos" / "index.json"
    if not repos_index.exists():
        return
    data = _read_json(repos_index, report)
    for repo in (data or {}).get("repos", []):
        if repo.get("isDisabled"):
            continue
        backup_dir = repo.get("backup_dir")
        if not backup_dir:
            report.warning(f"[{name}] repo '{repo.get('name')}' has no "
                           "backup_dir recorded")
            continue
        target = proj / "repos" / backup_dir
        if not target.is_dir():
            report.problem(f"[{name}] repo '{repo.get('name')}' indexed but "
                           f"mirror {backup_dir} is missing")
            continue
        res = run_git(["git", "-C", str(target), "rev-parse",
                       "--is-bare-repository"])
        if res.returncode != 0 or res.stdout.strip() != "true":
            report.problem(f"[{name}] repo mirror {backup_dir} is not a valid "
                           "bare repository")
            continue
        refs = run_git(["git", "-C", str(target), "for-each-ref",
                        "--format=%(refname)"])
        if refs.returncode != 0:
            report.problem(f"[{name}] repo mirror {backup_dir}: cannot list refs")
        elif repo.get("defaultBranch") and \
                repo["defaultBranch"] not in refs.stdout.split():
            report.problem(f"[{name}] repo mirror {backup_dir}: default branch "
                           f"{repo['defaultBranch']} not present in mirror")
        else:
            report.add("repos")


def dry_run_summary(project_dir: Path) -> dict:
    """What a restore of this backup would create (no network access)."""
    wi_index = project_dir / "work_items" / "index.json"
    wi_count = 0
    attachment_count = 0
    if wi_index.exists():
        with open(wi_index, encoding="utf-8") as f:
            ids = json.load(f).get("ids", [])
        for wid in ids:
            path = project_dir / "work_items" / f"{wid}.json"
            if path.exists():
                wi_count += 1
                with open(path, encoding="utf-8") as f:
                    attachment_count += len(json.load(f).get("attachments_local") or [])
    repos = []
    repos_index = project_dir / "repos" / "index.json"
    if repos_index.exists():
        with open(repos_index, encoding="utf-8") as f:
            repos = [r["name"] for r in json.load(f).get("repos", [])
                     if not r.get("isDisabled")]
    plans = []
    tp_index = project_dir / "test_plans" / "index.json"
    if tp_index.exists():
        with open(tp_index, encoding="utf-8") as f:
            plans = [p["name"] for p in json.load(f).get("plans", [])]
    with open(project_dir / "project.json", encoding="utf-8") as f:
        original = json.load(f)
    return {
        "source_project": original.get("name"),
        "process_template": ((original.get("capabilities") or {})
                             .get("processTemplate") or {}).get("templateName"),
        "work_items": wi_count,
        "attachments": attachment_count,
        "repos": repos,
        "test_plans": plans,
    }
