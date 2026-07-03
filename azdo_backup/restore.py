"""Restore an Azure DevOps project from a backup directory into a new project.

Restore caveats (Azure DevOps limitations, not bugs in this tool):

- Work item IDs cannot be preserved. We create new items and write an
  ``id_map.json`` (old_id -> new_id) into the restore destination.
- Revision history cannot be replayed verbatim; only the latest field values
  are restored. The original revision payload is preserved in the backup and
  a synthetic comment referencing the original ID is added on each new item.
- Author / CreatedBy / CreatedDate are set via the bypass-rules flag where
  permitted but may still be rewritten by the server in some collections.
- Test run results / test points are not restored — only plans/suites/cases.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .client import AzDoClient, AzDoError
from .util import ensure_dir, get_logger, safe_filename

log = get_logger(__name__)


# Fields that the server manages and that we shouldn't try to set directly.
_SKIP_FIELDS = {
    "System.Id",
    "System.Rev",
    "System.AreaId",
    "System.NodeName",
    "System.AreaLevel1",
    "System.AreaLevel2",
    "System.AreaLevel3",
    "System.AreaLevel4",
    "System.IterationId",
    "System.IterationLevel1",
    "System.IterationLevel2",
    "System.IterationLevel3",
    "System.IterationLevel4",
    "System.AuthorizedDate",
    "System.RevisedDate",
    "System.ChangedDate",
    "System.Watermark",
    "System.BoardColumn",
    "System.BoardColumnDone",
    "System.BoardLane",
    "System.TeamProject",
    "System.WorkItemType",
    "System.CommentCount",
    "System.AttachedFileCount",
    "System.HyperLinkCount",
    "System.ExternalLinkCount",
    "System.RelatedLinkCount",
}


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- top level


def restore_project(
    client: AzDoClient,
    source_dir: str | os.PathLike,
    new_project_name: str,
    *,
    process_template: str = "Agile",
    source_control_type: str = "Git",
    visibility: str = "private",
    skip_work_items: bool = False,
    skip_repos: bool = False,
    skip_test_plans: bool = False,
) -> dict:
    src = Path(source_dir)
    if not (src / "project.json").exists():
        raise AzDoError(f"{src} does not look like a project backup (missing project.json)")
    original = _read_json(src / "project.json")
    log.info("Restoring '%s' -> '%s' in org '%s'",
             original.get("name"), new_project_name, client.org_name)

    new_project = _ensure_project(client, new_project_name,
                                  process_template=process_template,
                                  source_control_type=source_control_type,
                                  visibility=visibility)
    summary = {"source": str(src), "target_project": new_project_name,
               "target_project_id": new_project.get("id")}

    id_map: dict[int, int] = {}
    if not skip_work_items and (src / "work_items").exists():
        id_map = _restore_work_items(client, new_project_name, src / "work_items")
        with open(src / "id_map.json", "w", encoding="utf-8") as f:
            json.dump(id_map, f, indent=2)
        summary["work_items_mapped"] = len(id_map)

    if not skip_repos and (src / "repos").exists():
        summary["repos_pushed"] = _restore_repos(client, new_project_name, src / "repos")

    if not skip_test_plans and (src / "test_plans").exists():
        summary["test_plans_restored"] = _restore_test_plans(
            client, new_project_name, src / "test_plans", id_map,
        )

    return summary


# --------------------------------------------------------------------- project


def _ensure_project(client: AzDoClient, name: str,
                    *, process_template: str,
                    source_control_type: str, visibility: str) -> dict:
    try:
        existing = client.get_project(name)
        log.info("Project '%s' already exists, reusing", name)
        return existing
    except AzDoError:
        pass

    processes = client.get_json("_apis/process/processes").get("value", [])
    process = next((p for p in processes if p["name"].lower() == process_template.lower()), None)
    if not process:
        raise AzDoError(
            f"Process template '{process_template}' not found. "
            f"Available: {[p['name'] for p in processes]}"
        )

    body = {
        "name": name,
        "description": f"Restored from backup",
        "visibility": visibility,
        "capabilities": {
            "versioncontrol": {"sourceControlType": source_control_type},
            "processTemplate": {"templateTypeId": process["id"]},
        },
    }
    log.info("Creating project '%s' (process=%s)", name, process_template)
    op = client.post_json("_apis/projects", body)
    # Poll the operation until completion.
    op_id = op.get("id") if isinstance(op, dict) else None
    if op_id:
        for _ in range(60):
            status = client.get_json(f"_apis/operations/{op_id}")
            if status.get("status") in ("succeeded", "failed", "cancelled"):
                if status["status"] != "succeeded":
                    raise AzDoError(f"Project creation {status['status']}: {status}")
                break
            time.sleep(2)
    return client.get_project(name)


# --------------------------------------------------------------------- work items


def _restore_work_items(client: AzDoClient, project: str, src: Path) -> dict[int, int]:
    idx = _read_json(src / "index.json")
    ids = idx["ids"]
    log.info("[%s] restoring %d work items", project, len(ids))

    id_map: dict[int, int] = {}

    # Pass 1: create items with primitive fields only.
    for old_id in ids:
        path = src / f"{old_id}.json"
        if not path.exists():
            continue
        wi = _read_json(path)
        fields = wi.get("fields") or {}
        wit = fields.get("System.WorkItemType")
        if not wit:
            log.warning("[%s] WI %s missing System.WorkItemType, skipping", project, old_id)
            continue
        patch = _fields_to_patch(fields)
        try:
            created = client.patch_json(
                f"_apis/wit/workitems/${wit}",
                patch, project=project,
                params={"bypassRules": "true", "suppressNotifications": "true"},
            )
            id_map[old_id] = created["id"]
        except Exception as exc:
            log.error("[%s] create WI (old %s, type %s) failed: %s",
                      project, old_id, wit, exc)

    # Pass 2: attachments + relations + links.
    for old_id, new_id in id_map.items():
        wi = _read_json(src / f"{old_id}.json")
        try:
            _restore_relations(client, project, src.parent, wi, new_id, id_map)
        except Exception as exc:
            log.error("[%s] relations for WI %s->%s failed: %s",
                      project, old_id, new_id, exc)
        # Provenance comment.
        try:
            client.post_json(
                f"_apis/wit/workItems/{new_id}/comments",
                {"text": f"Restored from work item #{old_id} (rev {wi.get('rev')})."},
                project=project, api_version="7.1-preview.4",
            )
        except Exception as exc:
            log.warning("[%s] provenance comment on WI %s failed: %s",
                        project, new_id, exc)
        # Original comments.
        for c in wi.get("comments", []) or []:
            text = c.get("text") or ""
            if not text:
                continue
            try:
                client.post_json(
                    f"_apis/wit/workItems/{new_id}/comments",
                    {"text": f"[original by {(c.get('createdBy') or {}).get('displayName','?')} at {c.get('createdDate','?')}]\n\n{text}"},
                    project=project, api_version="7.1-preview.4",
                )
            except Exception as exc:
                log.warning("[%s] comment on WI %s failed: %s", project, new_id, exc)

    return id_map


def _fields_to_patch(fields: dict) -> list[dict]:
    patch: list[dict] = []
    for name, value in fields.items():
        if name in _SKIP_FIELDS:
            continue
        # Server-managed identity objects need their displayName/uniqueName form.
        if isinstance(value, dict) and "uniqueName" in value:
            value = value.get("uniqueName") or value.get("displayName")
        patch.append({"op": "add", "path": f"/fields/{name}", "value": value})
    return patch


def _find_attachment_file(backup_root: Path, wi: dict, rel: dict) -> Path | None:
    """Locate the on-disk file for an AttachedFile relation.

    Prefers the exact path recorded in ``attachments_local`` at backup time;
    falls back to reconstructing it from the sanitized attachment name.
    """
    old_id = wi["id"]
    rel_url = rel.get("url")
    for meta in wi.get("attachments_local", []) or []:
        if (meta.get("rel") or {}).get("url") == rel_url:
            candidate = backup_root / meta["file"]
            if candidate.exists():
                return candidate
    name = (rel.get("attributes") or {}).get("name") or ""
    attachments_dir = backup_root / "work_items" / "attachments" / str(old_id)
    candidate = attachments_dir / safe_filename(name)
    if candidate.exists():
        return candidate
    if attachments_dir.exists():
        candidates = list(attachments_dir.iterdir())
        if len(candidates) == 1:
            return candidates[0]
    return None


def _restore_relations(client: AzDoClient, project: str, backup_root: Path,
                       wi: dict, new_id: int, id_map: dict[int, int]) -> None:
    patches: list[dict] = []
    for rel in wi.get("relations", []) or []:
        rel_type = rel.get("rel")
        if rel_type == "AttachedFile":
            old_id = wi["id"]
            name = (rel.get("attributes") or {}).get("name") or ""
            local = _find_attachment_file(backup_root, wi, rel)
            if local is None:
                log.warning("[%s] attachment file for WI %s not found locally: %s",
                            project, old_id, name)
                continue
            try:
                with open(local, "rb") as f:
                    resp = client.request(
                        "POST", "_apis/wit/attachments",
                        project=project,
                        params={"fileName": name or local.name},
                        data=f.read(),
                        headers={"Content-Type": "application/octet-stream"},
                    )
                new_url = resp.json()["url"]
            except Exception as exc:
                log.error("[%s] upload attachment %s failed: %s", project, name, exc)
                continue
            patches.append({
                "op": "add", "path": "/relations/-",
                "value": {
                    "rel": "AttachedFile", "url": new_url,
                    "attributes": {"name": name},
                },
            })
        elif rel_type and rel_type.startswith("System.LinkTypes."):
            # work-item to work-item link — remap via id_map
            url = rel.get("url", "")
            try:
                target_old = int(url.rsplit("/", 1)[-1])
            except ValueError:
                continue
            target_new = id_map.get(target_old)
            if not target_new:
                continue
            patches.append({
                "op": "add", "path": "/relations/-",
                "value": {
                    "rel": rel_type,
                    "url": client._full_url(f"_apis/wit/workItems/{target_new}", project=project),
                    "attributes": rel.get("attributes") or {},
                },
            })
        elif rel_type == "Hyperlink":
            patches.append({
                "op": "add", "path": "/relations/-",
                "value": {"rel": "Hyperlink", "url": rel.get("url"),
                          "attributes": rel.get("attributes") or {}},
            })
    if patches:
        client.patch_json(
            f"_apis/wit/workitems/{new_id}", patches, project=project,
            params={"bypassRules": "true", "suppressNotifications": "true"},
        )


# --------------------------------------------------------------------- repos


def _restore_repos(client: AzDoClient, project: str, src: Path) -> int:
    pushed = 0
    for repo_dir in sorted(src.iterdir()):
        if not repo_dir.is_dir() or not repo_dir.name.endswith(".git"):
            continue
        name = repo_dir.name[:-4]
        # Create repo in target project (idempotent).
        try:
            new_repo = client.post_json(
                "_apis/git/repositories", {"name": name},
                project=project,
            )
        except AzDoError as exc:
            if "already exists" in str(exc).lower() or "TF400948" in str(exc):
                new_repo = client.get_json(f"_apis/git/repositories/{name}", project=project)
            else:
                log.error("[%s] create repo '%s' failed: %s", project, name, exc)
                continue
        remote = new_repo.get("remoteUrl")
        if not remote:
            log.error("[%s] repo '%s' has no remoteUrl after creation", project, name)
            continue
        push_url = client.strip_url_credentials(remote)
        cmd = ["git", *client.git_auth_args(), "-C", str(repo_dir),
               "push", "--mirror", push_url]
        log.info("[%s] pushing mirror for '%s'", project, name)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            pushed += 1
        except subprocess.CalledProcessError as exc:
            log.error("[%s] push '%s' failed: %s", project, name,
                      exc.stderr.strip()[:500] if exc.stderr else exc)
    return pushed


# --------------------------------------------------------------------- test plans


def _restore_test_plans(client: AzDoClient, project: str, src: Path,
                        wi_id_map: dict[int, int]) -> int:
    if not (src / "index.json").exists():
        return 0
    plans = _read_json(src / "index.json").get("plans", [])
    restored = 0
    for plan in plans:
        plan_dir = src / str(plan["id"])
        if not plan_dir.exists():
            continue
        body = {
            "name": plan.get("name"),
            "description": plan.get("description"),
            "areaPath": plan.get("areaPath"),
            "iteration": plan.get("iteration"),
            "startDate": plan.get("startDate"),
            "endDate": plan.get("endDate"),
        }
        body = {k: v for k, v in body.items() if v is not None}
        try:
            new_plan = client.post_json(
                "_apis/testplan/plans", body, project=project,
                api_version="7.1-preview.1",
            )
        except AzDoError as exc:
            log.error("[%s] create test plan '%s' failed: %s",
                      project, plan.get("name"), exc)
            continue
        new_plan_id = new_plan["id"]
        suites_dir = plan_dir / "suites"
        # Map old suite id -> new suite id (root suite is auto-created).
        root_suite = (new_plan.get("rootSuite") or {}).get("id")
        old_root = (plan.get("rootSuite") or {}).get("id")
        suite_id_map: dict[int, int] = {}
        if old_root and root_suite:
            suite_id_map[old_root] = root_suite

        if suites_dir.exists():
            # Recreate suites by parent ordering: BFS using parentSuite linkage.
            suites = []
            for f in suites_dir.glob("*.json"):
                suites.append(_read_json(f).get("suite") or {})
            # Sort so parents come before children.
            suites_by_id = {s["id"]: s for s in suites if "id" in s}
            order: list[dict] = []
            seen: set[int] = set()
            def visit(s: dict) -> None:
                sid = s.get("id")
                if not sid or sid in seen:
                    return
                parent = (s.get("parentSuite") or {}).get("id")
                if parent and parent in suites_by_id and parent not in seen:
                    visit(suites_by_id[parent])
                seen.add(sid)
                order.append(s)
            for s in suites:
                visit(s)

            for s in order:
                old_id = s["id"]
                if old_id in suite_id_map:
                    continue  # root
                parent_old = (s.get("parentSuite") or {}).get("id")
                parent_new = suite_id_map.get(parent_old) or root_suite
                suite_body = {
                    "name": s.get("name"),
                    "suiteType": s.get("suiteType", "StaticTestSuite"),
                    "parentSuite": {"id": parent_new},
                }
                if s.get("queryString"):
                    suite_body["queryString"] = s["queryString"]
                try:
                    new_s = client.post_json(
                        f"_apis/testplan/Plans/{new_plan_id}/suites",
                        suite_body, project=project,
                        api_version="7.1-preview.1",
                    )
                    suite_id_map[old_id] = new_s["id"]
                except AzDoError as exc:
                    log.error("[%s] create suite '%s' under plan %s failed: %s",
                              project, s.get("name"), new_plan_id, exc)

            # Add test cases (work item ids must be remapped).
            for f in suites_dir.glob("*.json"):
                data = _read_json(f)
                old_suite_id = (data.get("suite") or {}).get("id")
                new_suite_id = suite_id_map.get(old_suite_id)
                if not new_suite_id:
                    continue
                tc_ids = []
                for tc in data.get("test_cases", []) or []:
                    wi_obj = tc.get("workItem") or {}
                    old_wi = wi_obj.get("id")
                    new_wi = wi_id_map.get(old_wi)
                    if new_wi:
                        tc_ids.append(new_wi)
                if not tc_ids:
                    continue
                body = [{"workItem": {"id": wid}} for wid in tc_ids]
                try:
                    client.post_json(
                        f"_apis/testplan/Plans/{new_plan_id}/Suites/{new_suite_id}/TestCase",
                        body, project=project, api_version="7.1-preview.3",
                    )
                except AzDoError as exc:
                    log.error("[%s] add test cases to suite %s failed: %s",
                              project, new_suite_id, exc)
        restored += 1
    return restored
