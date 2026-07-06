"""Backup logic.

Layout produced::

    <output>/
      org.json
      projects/
        <project-name>/
          project.json
          manifest.json
          summary.json          # written LAST: completion marker + error list
          classification_nodes.json
          work_items/
            index.json          # ordered list of work item IDs
            <id>.json           # full work item with revisions/comments/relations
            attachments/<id>/<guid>_<filename>
          repos/
            index.json          # repo metadata incl. backup_dir per repo
            <repo-name>.git/    # bare mirror clone
          test_plans/
            index.json
            configurations.json
            variables.json
            <plan-id>/
              plan.json
              suites/<suite-id>.json   # includes test cases
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .client import AzDoAuthError, AzDoClient, AzDoError
from .util import chunks, ensure_dir, get_logger, run_git, safe_filename

log = get_logger(__name__)


class BackupStats:
    """Collects non-fatal errors so a partial backup is detectable."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.counts: dict[str, int] = {}

    def error(self, message: str) -> None:
        self.errors.append(message)
        log.error("%s", message)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def as_dict(self) -> dict:
        return {"counts": self.counts, "error_count": len(self.errors),
                "errors": self.errors}


def _write_json(path: Path, data) -> None:
    """Write JSON atomically (temp + rename) so interrupts can't truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(path)


# --------------------------------------------------------------------- top level


def backup_org(client: AzDoClient, output_dir: str | os.PathLike) -> BackupStats:
    out = ensure_dir(output_dir)
    stats = BackupStats()
    projects = client.list_projects()
    _write_json(out / "org.json", {"org": client.org_name, "projects": projects})
    log.info("Found %d projects in org '%s'", len(projects), client.org_name)
    projects_dir = ensure_dir(out / "projects")
    for proj in projects:
        if proj.get("state") != "wellFormed":
            log.info("Skipping project '%s' (state=%s)", proj["name"], proj.get("state"))
            stats.add("projects_skipped")
            continue
        try:
            backup_project(client, proj["name"],
                           projects_dir / safe_filename(proj["name"]), stats=stats)
            stats.add("projects_backed_up")
        except AzDoAuthError:
            raise  # credentials are gone; every further call would fail too
        except Exception as exc:  # keep going on a per-project basis
            stats.error(f"project '{proj['name']}' backup failed: {exc}")
    _write_json(out / "summary.json", stats.as_dict())
    return stats


def backup_project(client: AzDoClient, project: str,
                   output_dir: str | os.PathLike,
                   stats: BackupStats | None = None) -> BackupStats:
    out = ensure_dir(output_dir)
    stats = stats if stats is not None else BackupStats()
    log.info("Backing up project '%s' into %s", project, out)
    proj_meta = client.get_project(project)
    _write_json(out / "project.json", proj_meta)
    _write_json(out / "manifest.json", {
        "tool": "azdo-az-backup",
        "version": __version__,
        "org": client.org_name,
        "project": project,
        "backed_up_at_utc": datetime.now(timezone.utc).isoformat(),
    })

    vcs_type = ((proj_meta.get("capabilities") or {})
                .get("versioncontrol") or {}).get("sourceControlType", "")
    if vcs_type.lower() == "tfvc":
        stats.error(f"[{project}] project uses TFVC — source code is NOT "
                    "backed up by this tool (git repositories only)")

    _backup_classification_nodes(client, project, out, stats)
    _backup_work_items(client, project, out / "work_items", stats)
    _backup_repos(client, project, out / "repos", stats)
    _backup_test_plans(client, project, out / "test_plans", stats)

    _write_json(out / "summary.json", stats.as_dict())
    return stats


# --------------------------------------------------------------------- classification


def _backup_classification_nodes(client: AzDoClient, project: str,
                                 out: Path, stats: BackupStats) -> None:
    trees = {}
    for group in ("areas", "iterations"):
        try:
            trees[group] = client.get_json(
                f"_apis/wit/classificationnodes/{group}",
                project=project, params={"$depth": 14},
            )
        except AzDoError as exc:
            stats.error(f"[{project}] classification nodes ({group}) failed: {exc}")
    if trees:
        _write_json(out / "classification_nodes.json", trees)


# --------------------------------------------------------------------- work items


_WIQL_PAGE = 19999  # server maximum for a single WIQL result set is 20000


def _list_work_item_ids(client: AzDoClient, project: str) -> list[int]:
    """List every work item ID in the project, paging past the WIQL 20k cap."""
    ids: list[int] = []
    last_id = 0
    while True:
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = @project AND [System.Id] > {last_id} "
                "ORDER BY [System.Id] ASC"
            )
        }
        res = client.post_json("_apis/wit/wiql", wiql, project=project,
                               params={"$top": _WIQL_PAGE}, idempotent=True)
        page = [w["id"] for w in res.get("workItems", [])]
        ids.extend(page)
        if len(page) < _WIQL_PAGE:
            return ids
        last_id = page[-1]


def _backup_work_items(client: AzDoClient, project: str, out: Path,
                       stats: BackupStats) -> None:
    ensure_dir(out)
    log.info("[%s] Listing work items via WIQL", project)
    ids = _list_work_item_ids(client, project)
    _write_json(out / "index.json", {"project": project, "ids": ids, "count": len(ids)})
    log.info("[%s] %d work items to back up", project, len(ids))

    attachments_root = ensure_dir(out / "attachments")
    saved: set[int] = set()
    for batch in chunks(ids, 200):
        body = {"ids": batch, "$expand": "all", "errorPolicy": "omit"}
        try:
            resp = client.post_json("_apis/wit/workitemsbatch", body,
                                    project=project, idempotent=True)
        except AzDoAuthError:
            raise
        except Exception as exc:
            stats.error(f"[{project}] work item batch {batch[0]}..{batch[-1]} "
                        f"fetch failed: {exc}")
            continue
        items = resp.get("value", []) if isinstance(resp, dict) else resp
        for wi in items:
            try:
                if _work_item_unchanged(out, wi):
                    saved.add(wi["id"])
                    stats.add("work_items_unchanged")
                    continue
                _save_work_item(client, project, wi, out, attachments_root, stats)
                saved.add(wi["id"])
                stats.add("work_items")
            except AzDoAuthError:
                raise
            except Exception as exc:
                stats.error(f"[{project}] work item {wi.get('id')} failed: {exc}")

    missing = sorted(set(ids) - saved)
    if missing:
        stats.error(f"[{project}] {len(missing)} indexed work items were not "
                    f"saved (deleted mid-run or fetch failures): {missing[:50]}")


def _work_item_unchanged(out: Path, wi: dict) -> bool:
    """True when a previous run already saved this exact state.

    ``rev`` covers field/relation edits; ``System.CommentCount`` covers
    comments, which do not bump the revision.
    """
    existing = out / f"{wi['id']}.json"
    if not existing.exists():
        return False
    try:
        with open(existing, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return False
    old_fields = old.get("fields") or {}
    new_fields = wi.get("fields") or {}
    return (old.get("rev") == wi.get("rev")
            and old_fields.get("System.CommentCount") == new_fields.get("System.CommentCount"))


def _attachment_relations(wi: dict, revisions: list[dict]) -> list[dict]:
    """All AttachedFile relations, current and historical, deduped by URL.

    An attachment added in rev 3 and removed in rev 7 is absent from the
    current relations but still referenced by the revision history.
    """
    seen: dict[str, dict] = {}
    for rel in wi.get("relations", []) or []:
        if rel.get("rel") == "AttachedFile" and rel.get("url"):
            seen[rel["url"]] = dict(rel, _historical=False)
    for rev in revisions:
        for rel in rev.get("relations", []) or []:
            if rel.get("rel") == "AttachedFile" and rel.get("url") \
                    and rel["url"] not in seen:
                seen[rel["url"]] = dict(rel, _historical=True)
    return list(seen.values())


def _save_work_item(client: AzDoClient, project: str, wi: dict,
                    out: Path, attachments_root: Path, stats: BackupStats) -> None:
    wid = wi["id"]
    revisions = list(client.iter_paged(
        f"_apis/wit/workItems/{wid}/revisions",
        project=project, params={"$expand": "all"},
    ))
    # The comments API pages via a continuationToken in the response body.
    comments: list[dict] = []
    comment_params: dict = {"$expand": "all", "order": "asc", "$top": 200}
    while True:
        comments_resp = client.get_json(
            f"_apis/wit/workItems/{wid}/comments",
            project=project, params=comment_params,
            api_version="7.1-preview.4",
        )
        if not isinstance(comments_resp, dict):
            break
        comments.extend(comments_resp.get("comments", []))
        token = comments_resp.get("continuationToken")
        if not token:
            break
        comment_params["continuationToken"] = token

    attachments_meta = []
    for rel in _attachment_relations(wi, revisions):
        historical = rel.pop("_historical", False)
        att_url = rel.get("url", "")
        att_name = (rel.get("attributes") or {}).get("name") or att_url.rsplit("/", 1)[-1]
        att_id = att_url.rsplit("/", 1)[-1].split("?")[0]
        dest_dir = ensure_dir(attachments_root / str(wid))
        # Deterministic GUID-prefixed name: no collisions, and re-runs can
        # skip files that already exist (attachments are immutable).
        dest = dest_dir / safe_filename(f"{att_id}_{att_name}")
        try:
            if not dest.exists():
                client.download(att_url, dest)
            attachments_meta.append({
                "name": att_name,
                "attachment_id": att_id,
                "file": str(dest.relative_to(out.parent)),
                "historical": historical,
                "rel": rel,
            })
            stats.add("attachments")
        except AzDoAuthError:
            raise
        except Exception as exc:
            stats.error(f"[{project}] attachment {att_name} on WI {wid} failed: {exc}")

    record = {
        "id": wid,
        "rev": wi.get("rev"),
        "fields": wi.get("fields", {}),
        "relations": wi.get("relations", []),
        "revisions": revisions,
        "comments": comments,
        "attachments_local": attachments_meta,
        "_links": wi.get("_links"),
        "url": wi.get("url"),
    }
    _write_json(out / f"{wid}.json", record)


# --------------------------------------------------------------------- repos


def _repo_backup_dirs(repos: list[dict]) -> dict[str, str]:
    """Map repo id -> backup directory name, deterministically.

    Two repo names can sanitize to the same filename; every member of such a
    collision group gets a repo-id suffix (stable across runs regardless of
    listing order).
    """
    by_safe: dict[str, list[dict]] = {}
    for repo in repos:
        by_safe.setdefault(safe_filename(repo["name"]), []).append(repo)
    dirs: dict[str, str] = {}
    for base, group in by_safe.items():
        for repo in group:
            if len(group) == 1:
                dirs[repo["id"]] = f"{base}.git"
            else:
                dirs[repo["id"]] = f"{base}_{repo['id'][:8]}.git"
    return dirs


def _is_bare_repo(path: Path) -> bool:
    res = run_git(["git", "-C", str(path), "rev-parse", "--is-bare-repository"])
    return res.returncode == 0 and res.stdout.strip() == "true"


def _backup_repos(client: AzDoClient, project: str, out: Path,
                  stats: BackupStats) -> None:
    ensure_dir(out)
    repos = client.get_json("_apis/git/repositories", project=project).get("value", [])
    dirs = _repo_backup_dirs(repos)
    for repo in repos:
        repo["backup_dir"] = dirs.get(repo["id"])
    _write_json(out / "index.json", {"project": project, "repos": repos})
    log.info("[%s] %d git repos", project, len(repos))
    auth_env = client.git_auth_env()
    for repo in repos:
        name = repo["name"]
        if repo.get("isDisabled"):
            log.warning("[%s] repo '%s' is disabled — SKIPPED (not in backup)",
                        project, name)
            stats.add("repos_skipped_disabled")
            continue
        remote = repo.get("remoteUrl") or repo.get("webUrl")
        if not remote:
            stats.error(f"[{project}] repo '{name}' has no remoteUrl, skipping")
            continue
        remote = client.strip_url_credentials(remote)
        target = out / repo["backup_dir"]
        if target.exists() and not _is_bare_repo(target):
            log.warning("[%s] '%s' is not a valid bare repo (interrupted clone?) "
                        "— re-cloning", project, target.name)
            shutil.rmtree(target)
        if target.exists():
            log.info("[%s] repo '%s' already cloned, fetching updates", project, name)
            run_git(["git", "-C", str(target), "remote", "set-url", "origin", remote])
            cmd = ["git", "-C", str(target), "remote", "update", "--prune"]
        else:
            log.info("[%s] cloning '%s'", project, name)
            cmd = ["git", "clone", "--mirror", remote, str(target)]
        res = run_git(cmd, extra_env=auth_env)
        if res.returncode != 0:
            stats.error(f"[{project}] git for '{name}' failed (exit {res.returncode}): "
                        f"{(res.stderr or '').strip()[:500]}")
        else:
            stats.add("repos")


# --------------------------------------------------------------------- test plans


def _backup_test_plans(client: AzDoClient, project: str, out: Path,
                       stats: BackupStats) -> None:
    ensure_dir(out)
    try:
        plans = list(client.iter_continuation(
            "_apis/testplan/plans", project=project,
            api_version="7.1-preview.1",
        ))
    except AzDoError as exc:
        stats.error(f"[{project}] test plans listing failed: {exc}")
        return
    _write_json(out / "index.json", {"project": project, "plans": plans})
    log.info("[%s] %d test plans", project, len(plans))

    for kind, endpoint in (("configurations", "_apis/testplan/configurations"),
                           ("variables", "_apis/testplan/variables")):
        try:
            items = list(client.iter_continuation(
                endpoint, project=project, api_version="7.1-preview.1"))
            _write_json(out / f"{kind}.json", {kind: items})
        except AzDoError as exc:
            stats.error(f"[{project}] test {kind} listing failed: {exc}")

    for plan in plans:
        plan_id = plan["id"]
        plan_dir = ensure_dir(out / str(plan_id))
        _write_json(plan_dir / "plan.json", plan)
        suites_dir = ensure_dir(plan_dir / "suites")
        try:
            suites = list(client.iter_continuation(
                f"_apis/testplan/Plans/{plan_id}/suites", project=project,
                api_version="7.1-preview.1",
            ))
        except AzDoError as exc:
            stats.error(f"[{project}] plan {plan_id} suites failed: {exc}")
            continue
        for suite in suites:
            suite_id = suite["id"]
            try:
                cases = list(client.iter_continuation(
                    f"_apis/testplan/Plans/{plan_id}/Suites/{suite_id}/TestCase",
                    project=project, api_version="7.1-preview.3",
                ))
            except AzDoError as exc:
                stats.error(f"[{project}] suite {plan_id}/{suite_id} test cases "
                            f"failed: {exc}")
                cases = []
            _write_json(suites_dir / f"{suite_id}.json",
                        {"suite": suite, "test_cases": cases})
        stats.add("test_plans")
