"""Backup logic.

Layout produced::

    <output>/
      org.json
      projects/
        <project-name>/
          project.json
          work_items/
            index.json          # ordered list of work item IDs
            <id>.json           # full work item with revisions/comments/relations
            attachments/<id>/<filename>
          repos/
            index.json
            <repo-name>.git/    # bare mirror clone
          test_plans/
            index.json
            <plan-id>/
              plan.json
              suites/<suite-id>.json   # includes test cases
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .client import AzDoClient, AzDoError
from .util import chunks, ensure_dir, get_logger, run_git, safe_filename

log = get_logger(__name__)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# --------------------------------------------------------------------- top level


def backup_org(client: AzDoClient, output_dir: str | os.PathLike) -> Path:
    out = ensure_dir(output_dir)
    projects = client.list_projects()
    _write_json(out / "org.json", {"org": client.org_name, "projects": projects})
    log.info("Found %d projects in org '%s'", len(projects), client.org_name)
    projects_dir = ensure_dir(out / "projects")
    for proj in projects:
        try:
            backup_project(client, proj["name"], projects_dir / safe_filename(proj["name"]))
        except Exception as exc:  # keep going on a per-project basis
            log.error("Project '%s' backup failed: %s", proj["name"], exc)
    return out


def backup_project(client: AzDoClient, project: str, output_dir: str | os.PathLike) -> Path:
    out = ensure_dir(output_dir)
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

    _backup_work_items(client, project, out / "work_items")
    _backup_repos(client, project, out / "repos")
    _backup_test_plans(client, project, out / "test_plans")

    return out


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
                               params={"$top": _WIQL_PAGE})
        page = [w["id"] for w in res.get("workItems", [])]
        ids.extend(page)
        if len(page) < _WIQL_PAGE:
            return ids
        last_id = page[-1]


def _backup_work_items(client: AzDoClient, project: str, out: Path) -> None:
    ensure_dir(out)
    log.info("[%s] Listing work items via WIQL", project)
    ids = _list_work_item_ids(client, project)
    _write_json(out / "index.json", {"project": project, "ids": ids, "count": len(ids)})
    log.info("[%s] %d work items to back up", project, len(ids))

    attachments_root = ensure_dir(out / "attachments")
    for batch in chunks(ids, 200):
        body = {"ids": batch, "$expand": "all", "errorPolicy": "omit"}
        try:
            resp = client.post_json("_apis/wit/workitemsbatch", body, project=project)
        except Exception as exc:
            log.error("[%s] batch fetch %s..%s failed: %s",
                      project, batch[0], batch[-1], exc)
            continue
        items = resp.get("value", []) if isinstance(resp, dict) else resp
        for wi in items:
            try:
                _save_work_item(client, project, wi, out, attachments_root)
            except Exception as exc:
                log.error("[%s] work item %s failed: %s", project, wi.get("id"), exc)


def _save_work_item(client: AzDoClient, project: str, wi: dict,
                    out: Path, attachments_root: Path) -> None:
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
    for rel in wi.get("relations", []) or []:
        if rel.get("rel") == "AttachedFile":
            att_url = rel.get("url", "")
            att_name = (rel.get("attributes") or {}).get("name") or att_url.rsplit("/", 1)[-1]
            att_id = att_url.rsplit("/", 1)[-1].split("?")[0]
            dest_dir = ensure_dir(attachments_root / str(wid))
            dest = dest_dir / safe_filename(att_name)
            if dest.exists():
                # Two attachments on the same work item share a sanitized
                # name — disambiguate with the attachment GUID.
                dest = dest_dir / safe_filename(f"{att_id}_{att_name}")
            try:
                client.download(att_url, dest)
                attachments_meta.append({
                    "name": att_name,
                    "attachment_id": att_id,
                    "file": str(dest.relative_to(out.parent)),
                    "rel": rel,
                })
            except Exception as exc:
                log.error("[%s] attachment %s on WI %s failed: %s",
                          project, att_name, wid, exc)

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


def _backup_repos(client: AzDoClient, project: str, out: Path) -> None:
    ensure_dir(out)
    repos = client.get_json("_apis/git/repositories", project=project).get("value", [])
    _write_json(out / "index.json", {"project": project, "repos": repos})
    log.info("[%s] %d git repos", project, len(repos))
    auth = client.git_auth_args()
    for repo in repos:
        name = repo["name"]
        if repo.get("isDisabled"):
            log.info("[%s] repo '%s' is disabled, skipping", project, name)
            continue
        remote = repo.get("remoteUrl") or repo.get("webUrl")
        if not remote:
            log.warning("[%s] repo '%s' has no remoteUrl, skipping", project, name)
            continue
        remote = client.strip_url_credentials(remote)
        target = out / f"{safe_filename(name)}.git"
        if target.exists():
            log.info("[%s] repo '%s' already cloned, fetching updates", project, name)
            # Repair remotes written by older versions that embedded the PAT.
            run_git(["git", "-C", str(target), "remote", "set-url", "origin", remote])
            cmd = ["git", *auth, "-C", str(target), "remote", "update", "--prune"]
        else:
            log.info("[%s] cloning '%s'", project, name)
            cmd = ["git", *auth, "clone", "--mirror", remote, str(target)]
        try:
            run_git(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            log.error("[%s] git for '%s' failed: %s", project, name,
                      exc.stderr.strip()[:500] if exc.stderr else exc)


# --------------------------------------------------------------------- test plans


def _backup_test_plans(client: AzDoClient, project: str, out: Path) -> None:
    ensure_dir(out)
    try:
        plans = list(client.iter_continuation(
            "_apis/testplan/plans", project=project,
            api_version="7.1-preview.1",
        ))
    except AzDoError as exc:
        log.warning("[%s] test plans listing failed: %s", project, exc)
        return
    _write_json(out / "index.json", {"project": project, "plans": plans})
    log.info("[%s] %d test plans", project, len(plans))
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
            log.error("[%s] plan %s suites failed: %s", project, plan_id, exc)
            continue
        for suite in suites:
            suite_id = suite["id"]
            try:
                cases = list(client.iter_continuation(
                    f"_apis/testplan/Plans/{plan_id}/Suites/{suite_id}/TestCase",
                    project=project, api_version="7.1-preview.3",
                ))
            except AzDoError as exc:
                log.error("[%s] suite %s/%s test cases failed: %s",
                          project, plan_id, suite_id, exc)
                cases = []
            _write_json(suites_dir / f"{suite_id}.json",
                        {"suite": suite, "test_cases": cases})
