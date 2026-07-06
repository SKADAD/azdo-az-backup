"""Restore an Azure DevOps project from a backup directory into a new project.

Restore caveats (Azure DevOps limitations, not bugs in this tool):

- Work item IDs cannot be preserved. We create new items and write an
  ``id_map.<target>.json`` (old_id -> new_id) next to the backup; re-running
  a restore loads it and skips already-restored items.
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
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .client import AzDoClient, AzDoError
from .util import get_logger, run_git, safe_filename

log = get_logger(__name__)


# Fields that the server manages and that we shouldn't try to set directly.
_SKIP_FIELDS = {
    "System.Id",
    "System.Rev",
    "System.Parent",           # read-only; parents are Hierarchy relations
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
    "System.AuthorizedAs",
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
    "System.RemoteLinkCount",
}

# Work item types that are managed through the Test Plans API and cannot be
# created via the work item tracking API.
_TEST_MANAGED_TYPES = {"test plan", "test suite"}

# Relation attribute keys the server accepts on newly added relations.
_RELATION_ATTR_WHITELIST = {"comment", "name"}


def _read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------- top level


def restore_project(
    client: AzDoClient,
    source_dir: str | os.PathLike,
    new_project_name: str,
    *,
    process_template: str | None = None,
    source_control_type: str = "Git",
    visibility: str = "private",
    skip_work_items: bool = False,
    skip_repos: bool = False,
    skip_test_plans: bool = False,
    id_map_dir: str | os.PathLike | None = None,
) -> dict:
    src = Path(source_dir)
    if not (src / "project.json").exists():
        raise AzDoError(f"{src} does not look like a project backup (missing project.json)")
    original = _read_json(src / "project.json")
    log.info("Restoring '%s' -> '%s' in org '%s'",
             original.get("name"), new_project_name, client.org_name)

    if not process_template:
        caps = ((original.get("capabilities") or {}).get("processTemplate") or {})
        process_template = caps.get("templateName") or "Agile"
        log.info("Using process template '%s' (from source project)", process_template)

    new_project = _ensure_project(client, new_project_name,
                                  process_template=process_template,
                                  source_control_type=source_control_type,
                                  visibility=visibility)
    summary = {"source": str(src), "target_project": new_project_name,
               "target_project_id": new_project.get("id")}

    old_project_name = original.get("name") or new_project_name
    # When restoring from an extracted archive the source dir is ephemeral —
    # callers pass a durable id_map_dir so resume still works.
    map_dir = Path(id_map_dir) if id_map_dir else src
    id_map_path = map_dir / f"id_map.{safe_filename(new_project_name)}.json"
    id_map = _load_id_map(id_map_path)
    if id_map:
        log.info("Loaded existing id map (%d entries) — restore will resume",
                 len(id_map))

    if not skip_work_items and (src / "work_items").exists():
        id_map = _restore_work_items(client, new_project_name, src / "work_items",
                                     old_project=old_project_name, id_map=id_map,
                                     id_map_path=id_map_path)
        summary["work_items_mapped"] = len(id_map)
        summary["id_map_file"] = str(id_map_path)

    if not skip_repos and (src / "repos").exists():
        summary["repos_pushed"] = _restore_repos(client, new_project_name, src / "repos")

    if not skip_test_plans and (src / "test_plans").exists():
        if skip_work_items and not id_map:
            log.warning("Restoring test plans without an id map — test case "
                        "associations will be dropped (run the work item "
                        "restore first, or keep its id_map file)")
        summary["test_plans_restored"] = _restore_test_plans(
            client, new_project_name, src / "test_plans", id_map,
            old_project=old_project_name,
        )

    return summary


def _load_id_map(path: Path) -> dict[int, int]:
    if not path.exists():
        return {}
    raw = _read_json(path)
    return {int(k): int(v) for k, v in raw.items()}


def _write_id_map(path: Path, id_map: dict[int, int]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(id_map, f, indent=2)


# --------------------------------------------------------------------- project


def _ensure_project(client: AzDoClient, name: str,
                    *, process_template: str,
                    source_control_type: str, visibility: str) -> dict:
    try:
        existing = client.get_project(name)
        log.info("Project '%s' already exists, reusing", name)
        return existing
    except AzDoError as exc:
        # Only a 404 means "project doesn't exist"; auth or permission
        # failures must not silently fall through to project creation.
        if exc.status_code != 404:
            raise

    processes = client.get_json("_apis/process/processes").get("value", [])
    process = next((p for p in processes if p["name"].lower() == process_template.lower()), None)
    if not process:
        raise AzDoError(
            f"Process template '{process_template}' not found. "
            f"Available: {[p['name'] for p in processes]}"
        )

    body = {
        "name": name,
        "description": "Restored from backup",
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
        for _ in range(90):
            status = client.get_json(f"_apis/operations/{op_id}")
            if status.get("status") in ("succeeded", "failed", "cancelled"):
                if status["status"] != "succeeded":
                    raise AzDoError(f"Project creation {status['status']}: {status}")
                break
            time.sleep(2)
        else:
            raise AzDoError(f"Project creation still running after 180s (operation {op_id})")
    return client.get_project(name)


# --------------------------------------------------------------------- classification


def remap_classification_path(path: str | None, old_project: str,
                              new_project: str) -> str | None:
    """Re-root ``OldProject\\Area\\Sub`` under the new project name."""
    if not path:
        return path
    parts = path.split("\\")
    if parts and parts[0].strip().lower() == old_project.strip().lower():
        parts[0] = new_project
        return "\\".join(parts)
    return path


def remap_query_string(query: str | None, old_project: str,
                       new_project: str) -> str | None:
    """Rewrite quoted references to the old project inside a WIQL query
    (TeamProject literals and path prefixes like ``'Old\\Area'``)."""
    if not query:
        return query
    pattern = re.compile("'" + re.escape(old_project) + r"(\\|')", re.IGNORECASE)
    return pattern.sub(lambda m: "'" + new_project + m.group(1), query)


def _create_classification_node(client: AzDoClient, project: str, group: str,
                                parent_segments: list[str], name: str,
                                attributes: dict | None = None) -> None:
    """Create one node under areas/iterations; 'already exists' is fine."""
    url = f"_apis/wit/classificationnodes/{group}"
    if parent_segments:
        url += "/" + "/".join(quote(seg, safe="") for seg in parent_segments)
    body: dict = {"name": name}
    if attributes:
        body["attributes"] = attributes
    try:
        client.post_json(url, body, project=project)
    except AzDoError as exc:
        text = str(exc).lower()
        if exc.status_code != 409 and "already exists" not in text \
                and "vs402371" not in text:
            log.warning("[%s] create %s node '%s' failed: %s",
                        project, group, "\\".join(parent_segments + [name]), exc)


def _restore_classification_tree(client: AzDoClient, project: str, group: str,
                                 node: dict, parent_segments: list[str]) -> None:
    """Recreate a backed-up classification tree, keeping iteration dates."""
    for child in node.get("children", []) or []:
        name = child.get("name")
        if not name:
            continue
        attrs = child.get("attributes") or {}
        dates = {k: v for k, v in attrs.items() if k in ("startDate", "finishDate")}
        _create_classification_node(client, project, group, parent_segments,
                                    name, dates or None)
        _restore_classification_tree(client, project, group, child,
                                     parent_segments + [name])


def _ensure_classification_nodes(client: AzDoClient, project: str,
                                 group: str, paths: Iterable[str]) -> None:
    """Create area/iteration nodes so remapped paths validate on the server.

    Paths must already be re-rooted under the target project name; the root
    node always exists.
    """
    created: set[str] = set()
    for path in sorted(set(p for p in paths if p)):
        parts = path.split("\\")
        for depth in range(1, len(parts)):
            node_path = "\\".join(parts[: depth + 1])
            if node_path in created:
                continue
            _create_classification_node(client, project, group,
                                        parts[1:depth], parts[depth])
            created.add(node_path)


# --------------------------------------------------------------------- work items


def _restore_work_items(client: AzDoClient, project: str, src: Path,
                        *, old_project: str,
                        id_map: dict[int, int] | None = None,
                        id_map_path: Path | None = None) -> dict[int, int]:
    idx = _read_json(src / "index.json")
    ids = idx["ids"]
    id_map = dict(id_map or {})
    log.info("[%s] restoring %d work items (%d already mapped)",
             project, len(ids), len(id_map))

    # Pass 0: recreate the classification tree. Prefer the full backed-up
    # tree (keeps iteration dates and empty nodes); infer from work item
    # fields as a fallback / gap-filler.
    tree_file = src.parent / "classification_nodes.json"
    if tree_file.exists():
        trees = _read_json(tree_file)
        for group in ("areas", "iterations"):
            if trees.get(group):
                _restore_classification_tree(client, project, group,
                                             trees[group], [])
    area_paths: set[str] = set()
    iteration_paths: set[str] = set()
    for old_id in ids:
        path = src / f"{old_id}.json"
        if not path.exists():
            continue
        fields = _read_json(path).get("fields") or {}
        ap = remap_classification_path(fields.get("System.AreaPath"), old_project, project)
        ip = remap_classification_path(fields.get("System.IterationPath"), old_project, project)
        if ap:
            area_paths.add(ap)
        if ip:
            iteration_paths.add(ip)
    _ensure_classification_nodes(client, project, "areas", area_paths)
    _ensure_classification_nodes(client, project, "iterations", iteration_paths)

    # Pass 1: create items with primitive fields only.
    newly_created: list[int] = []
    for old_id in ids:
        if old_id in id_map:
            continue  # already restored by a previous (partial) run
        path = src / f"{old_id}.json"
        if not path.exists():
            continue
        wi = _read_json(path)
        fields = wi.get("fields") or {}
        wit = fields.get("System.WorkItemType")
        if not wit:
            log.warning("[%s] WI %s missing System.WorkItemType, skipping", project, old_id)
            continue
        if wit.strip().lower() in _TEST_MANAGED_TYPES:
            # Created by the Test Plans restore, not the WIT API.
            continue
        created = _create_work_item(client, project, wit, fields,
                                    old_project=old_project, old_id=old_id)
        if created:
            id_map[old_id] = created["id"]
            newly_created.append(old_id)

    # Persist the map before pass 2: a crash there must not cause the next
    # run to recreate (duplicate) all the items pass 1 already made.
    if id_map_path is not None:
        _write_id_map(id_map_path, id_map)

    # Pass 2: attachments + relations + comments (only for new items).
    dropped_relations: Counter = Counter()
    for old_id in newly_created:
        new_id = id_map[old_id]
        wi = _read_json(src / f"{old_id}.json")
        try:
            _restore_relations(client, project, src.parent, wi, new_id, id_map,
                               dropped_relations)
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

    if dropped_relations:
        log.warning("[%s] relations not restored (unsupported types): %s",
                    project, dict(dropped_relations))
    return id_map


def _create_work_item(client: AzDoClient, project: str, wit: str, fields: dict,
                      *, old_project: str, old_id: int) -> dict | None:
    """Create one work item, degrading gracefully when the server rejects
    fields (invalid state for the target process, unknown custom fields...)."""
    params = {"bypassRules": "true", "suppressNotifications": "true"}
    url = f"_apis/wit/workitems/${wit}"

    full = _fields_to_patch(fields, old_project=old_project, new_project=project)
    try:
        return client.patch_json(url, full, project=project, params=params)
    except AzDoError as exc:
        log.warning("[%s] full-field create failed for WI %s (%s): %s — "
                    "retrying without state fields", project, old_id, wit, exc)

    no_state = [p for p in full if p["path"] not in
                ("/fields/System.State", "/fields/System.Reason")]
    try:
        return client.patch_json(url, no_state, project=project, params=params)
    except AzDoError:
        log.warning("[%s] stateless create failed for WI %s — retrying with "
                    "minimal fields", project, old_id)

    keep = {"/fields/System.Title", "/fields/System.AreaPath",
            "/fields/System.IterationPath", "/fields/System.Description",
            "/fields/System.Tags"}
    minimal = [p for p in full if p["path"] in keep]
    try:
        return client.patch_json(url, minimal, project=project, params=params)
    except AzDoError as exc:
        log.error("[%s] create WI (old %s, type %s) failed even with minimal "
                  "fields: %s", project, old_id, wit, exc)
        return None


def _fields_to_patch(fields: dict, *, old_project: str | None = None,
                     new_project: str | None = None) -> list[dict]:
    patch: list[dict] = []
    for name, value in fields.items():
        if name in _SKIP_FIELDS:
            continue
        if name.startswith("WEF_"):
            continue  # board fields bound to the source project's boards
        # Server-managed identity objects need their displayName/uniqueName form.
        if isinstance(value, dict) and "uniqueName" in value:
            value = value.get("uniqueName") or value.get("displayName")
        if name in ("System.AreaPath", "System.IterationPath") \
                and old_project and new_project:
            value = remap_classification_path(value, old_project, new_project)
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
    att_id = (rel_url or "").rsplit("/", 1)[-1].split("?")[0]
    attachments_dir = backup_root / "work_items" / "attachments" / str(old_id)
    for candidate_name in (safe_filename(f"{att_id}_{name}"), safe_filename(name)):
        candidate = attachments_dir / candidate_name
        if candidate.exists():
            return candidate
    if attachments_dir.exists():
        candidates = list(attachments_dir.iterdir())
        if len(candidates) == 1:
            return candidates[0]
    return None


def _relation_attributes(rel: dict) -> dict:
    return {k: v for k, v in (rel.get("attributes") or {}).items()
            if k in _RELATION_ATTR_WHITELIST}


def _restore_relations(client: AzDoClient, project: str, backup_root: Path,
                       wi: dict, new_id: int, id_map: dict[int, int],
                       dropped: Counter | None = None) -> None:
    dropped = dropped if dropped is not None else Counter()
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
                new_url = client.upload_attachment(project, local, name or local.name)
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
                dropped[f"{rel_type} (target not restored)"] += 1
                continue
            # Each link appears on both endpoints; adding it twice makes the
            # second PATCH fail. Recreate directional links only from the
            # Forward side, and symmetric links (e.g. Related) only from the
            # endpoint with the lower original ID.
            if rel_type.endswith("-Reverse"):
                continue
            if not rel_type.endswith("-Forward") and wi["id"] > target_old:
                continue
            patches.append({
                "op": "add", "path": "/relations/-",
                "value": {
                    "rel": rel_type,
                    "url": client._full_url(f"_apis/wit/workItems/{target_new}", project=project),
                    "attributes": _relation_attributes(rel),
                },
            })
        elif rel_type == "Hyperlink":
            patches.append({
                "op": "add", "path": "/relations/-",
                "value": {"rel": "Hyperlink", "url": rel.get("url"),
                          "attributes": _relation_attributes(rel)},
            })
        elif rel_type:
            # ArtifactLink (commits, builds, PRs), Remote.* cross-org links...
            dropped[rel_type] += 1
    if not patches:
        return
    params = {"bypassRules": "true", "suppressNotifications": "true"}
    try:
        client.patch_json(f"_apis/wit/workitems/{new_id}", patches,
                          project=project, params=params)
    except AzDoError:
        # One bad relation must not take the rest down — apply individually.
        for p in patches:
            try:
                client.patch_json(f"_apis/wit/workitems/{new_id}", [p],
                                  project=project, params=params)
            except AzDoError as exc:
                log.error("[%s] relation %s on WI %s failed: %s",
                          project, (p.get("value") or {}).get("rel"), new_id, exc)


# --------------------------------------------------------------------- repos


def _restore_repos(client: AzDoClient, project: str, src: Path) -> int:
    # index.json maps backup dirs to original names and default branches.
    repo_meta: dict[str, dict] = {}
    index_file = src / "index.json"
    if index_file.exists():
        for repo in _read_json(index_file).get("repos", []):
            key = repo.get("backup_dir") or f"{safe_filename(repo['name'])}.git"
            repo_meta[key] = repo

    pushed = 0
    auth_env = client.git_auth_env()
    for repo_dir in sorted(src.iterdir()):
        if not repo_dir.is_dir() or not repo_dir.name.endswith(".git"):
            continue
        meta = repo_meta.get(repo_dir.name, {})
        name = meta.get("name") or repo_dir.name[:-4]
        # Create repo in target project (idempotent).
        try:
            new_repo = client.post_json(
                "_apis/git/repositories", {"name": name},
                project=project,
            )
        except AzDoError as exc:
            if "already exists" in str(exc).lower() or "TF400948" in str(exc):
                new_repo = client.get_json(
                    f"_apis/git/repositories/{quote(name, safe='')}", project=project)
            else:
                log.error("[%s] create repo '%s' failed: %s", project, name, exc)
                continue
        remote = new_repo.get("remoteUrl")
        if not remote:
            log.error("[%s] repo '%s' has no remoteUrl after creation", project, name)
            continue
        push_url = client.strip_url_credentials(remote)
        # Push branches and tags explicitly rather than --mirror: mirrors of
        # Azure DevOps repos carry server-managed hidden refs (refs/pull/*)
        # that the target server rejects, failing the whole push.
        cmd = ["git", "-C", str(repo_dir), "push", push_url,
               "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
        log.info("[%s] pushing branches/tags for '%s'", project, name)
        res = run_git(cmd, extra_env=auth_env)
        if res.returncode != 0:
            log.error("[%s] push '%s' failed (exit %d): %s", project, name,
                      res.returncode, (res.stderr or "").strip()[:500])
            continue
        pushed += 1
        default_branch = meta.get("defaultBranch")
        if default_branch:
            try:
                client.patch_json(
                    f"_apis/git/repositories/{new_repo['id']}",
                    {"defaultBranch": default_branch},
                    content_type="application/json", project=project,
                )
            except AzDoError as exc:
                log.warning("[%s] set default branch on '%s' failed: %s",
                            project, name, exc)
    return pushed


# --------------------------------------------------------------------- test plans


def _restore_test_configurations(client: AzDoClient, project: str, src: Path) -> None:
    """Recreate test variables and configurations by name (skip existing)."""
    try:
        existing_vars = {v["name"] for v in client.iter_continuation(
            "_apis/testplan/variables", project=project,
            api_version="7.1-preview.1")}
        existing_cfgs = {c["name"] for c in client.iter_continuation(
            "_apis/testplan/configurations", project=project,
            api_version="7.1-preview.1")}
    except AzDoError as exc:
        log.warning("[%s] listing test configurations failed: %s", project, exc)
        return

    var_file = src / "variables.json"
    if var_file.exists():
        for var in _read_json(var_file).get("variables", []):
            if var.get("name") in existing_vars:
                continue
            body = {"name": var.get("name"),
                    "description": var.get("description"),
                    "values": var.get("values") or []}
            try:
                client.post_json("_apis/testplan/variables", body,
                                 project=project, api_version="7.1-preview.1")
            except AzDoError as exc:
                log.warning("[%s] create test variable '%s' failed: %s",
                            project, var.get("name"), exc)

    cfg_file = src / "configurations.json"
    if cfg_file.exists():
        for cfg in _read_json(cfg_file).get("configurations", []):
            if cfg.get("name") in existing_cfgs:
                continue
            body = {"name": cfg.get("name"),
                    "description": cfg.get("description"),
                    "values": cfg.get("values") or [],
                    "isDefault": cfg.get("isDefault", False),
                    "state": cfg.get("state", "active")}
            try:
                client.post_json("_apis/testplan/configurations", body,
                                 project=project, api_version="7.1-preview.1")
            except AzDoError as exc:
                log.warning("[%s] create test configuration '%s' failed: %s",
                            project, cfg.get("name"), exc)


def _order_suites_parents_first(suites: list[dict]) -> list[dict]:
    """Order suites so every parent precedes its children."""
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
    return order


def _restore_test_plans(client: AzDoClient, project: str, src: Path,
                        wi_id_map: dict[int, int], *, old_project: str) -> int:
    if not (src / "index.json").exists():
        return 0
    plans = _read_json(src / "index.json").get("plans", [])

    _restore_test_configurations(client, project, src)

    try:
        existing_plans = {p["name"] for p in client.iter_continuation(
            "_apis/testplan/plans", project=project,
            api_version="7.1-preview.1")}
    except AzDoError:
        existing_plans = set()

    # Plans may reference area/iteration paths no work item uses.
    plan_paths_area = {remap_classification_path(p.get("areaPath"), old_project, project)
                       for p in plans if p.get("areaPath")}
    plan_paths_iter = {remap_classification_path(p.get("iteration"), old_project, project)
                       for p in plans if p.get("iteration")}
    _ensure_classification_nodes(client, project, "areas", plan_paths_area)
    _ensure_classification_nodes(client, project, "iterations", plan_paths_iter)

    restored = 0
    for plan in plans:
        if plan.get("name") in existing_plans:
            log.info("[%s] test plan '%s' already exists — skipping (resume)",
                     project, plan.get("name"))
            continue
        plan_dir = src / str(plan["id"])
        if not plan_dir.exists():
            continue
        body = {
            "name": plan.get("name"),
            "description": plan.get("description"),
            "areaPath": remap_classification_path(plan.get("areaPath"),
                                                  old_project, project),
            "iteration": remap_classification_path(plan.get("iteration"),
                                                   old_project, project),
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
        # Suites whose restored form accepts direct test case adds.
        static_suites: set[int] = set()
        if old_root and root_suite:
            suite_id_map[old_root] = root_suite
            static_suites.add(root_suite)

        if suites_dir.exists():
            suites = [(_read_json(f).get("suite") or {})
                      for f in suites_dir.glob("*.json")]
            for s in _order_suites_parents_first(suites):
                old_id = s["id"]
                if old_id in suite_id_map:
                    continue  # root
                parent_old = (s.get("parentSuite") or {}).get("id")
                parent_new = suite_id_map.get(parent_old)
                if parent_new is None:
                    log.warning("[%s] suite '%s': parent %s missing — attaching "
                                "to root", project, s.get("name"), parent_old)
                    parent_new = root_suite
                suite_type = s.get("suiteType", "staticTestSuite")
                effective_type = suite_type
                suite_body = {
                    "name": s.get("name"),
                    "suiteType": suite_type,
                    "parentSuite": {"id": parent_new},
                }
                if s.get("queryString"):
                    suite_body["queryString"] = remap_query_string(
                        s["queryString"], old_project, project)
                if suite_type.lower() == "requirementtestsuite":
                    req_old = s.get("requirementId")
                    req_new = wi_id_map.get(req_old)
                    if not req_new:
                        log.warning(
                            "[%s] suite '%s': requirement WI %s not in id map, "
                            "recreating as a static suite", project, s.get("name"), req_old)
                        suite_body["suiteType"] = effective_type = "staticTestSuite"
                    else:
                        suite_body["requirementId"] = req_new
                try:
                    new_s = client.post_json(
                        f"_apis/testplan/Plans/{new_plan_id}/suites",
                        suite_body, project=project,
                        api_version="7.1-preview.1",
                    )
                    suite_id_map[old_id] = new_s["id"]
                    if effective_type.lower() == "statictestsuite":
                        static_suites.add(new_s["id"])
                except AzDoError as exc:
                    log.error("[%s] create suite '%s' under plan %s failed: %s",
                              project, s.get("name"), new_plan_id, exc)

            # Add test cases — only to static suites (query suites are
            # query-driven and requirement suites populate from links).
            for f in suites_dir.glob("*.json"):
                data = _read_json(f)
                old_suite_id = (data.get("suite") or {}).get("id")
                new_suite_id = suite_id_map.get(old_suite_id)
                if not new_suite_id or new_suite_id not in static_suites:
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
