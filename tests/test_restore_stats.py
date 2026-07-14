"""Tests for restore error accountability and the type preflight."""
import json

from azdo_backup.restore import RestoreStats, _restore_work_items


class PermissiveFakeClient:
    """Just enough client for _restore_work_items: records creations,
    accepts everything else."""

    def __init__(self, target_types):
        self.target_types = target_types
        self.created = []
        self._next = 100

    def get_json(self, path, **kw):
        if "workitemtypes" in path:
            return {"value": [{"name": t} for t in self.target_types]}
        return {}

    def post_json(self, path, body, **kw):
        return {}

    def patch_json(self, path, body, **kw):
        if "/$" in path:
            self._next += 1
            wit = path.rsplit("/$", 1)[1]
            self.created.append(wit)
            return {"id": self._next}
        return {"id": 1}

    def _full_url(self, path, project=None):
        return f"https://dev.azure.com/o/{project}/{path}"


def _write_backup(src, items):
    src.mkdir(parents=True)
    ids = []
    for wid, wit in items:
        ids.append(wid)
        (src / f"{wid}.json").write_text(json.dumps({
            "id": wid, "rev": 1,
            "fields": {"System.WorkItemType": wit,
                       "System.Title": f"Item {wid}",
                       "System.AreaPath": "Old",
                       "System.IterationPath": "Old"},
            "relations": [], "comments": [], "attachments_local": [],
        }))
    (src / "index.json").write_text(json.dumps({"ids": ids}))


def test_missing_types_skipped_once_with_aggregated_error(tmp_path):
    src = tmp_path / "work_items"
    _write_backup(src, [(1, "Bug"), (2, "Product Backlog Item"),
                        (3, "Product Backlog Item")])
    client = PermissiveFakeClient(target_types=["Bug", "Task"])
    stats = RestoreStats()
    id_map = _restore_work_items(client, "New", src, old_project="Old",
                                 stats=stats)
    # Only the Bug was created; PBIs were skipped without doomed API calls.
    assert client.created == ["Bug"]
    assert set(id_map) == {1}
    assert stats.counts["work_items_created"] == 1
    pbi_errors = [e for e in stats.errors if "Product Backlog Item" in e]
    assert len(pbi_errors) == 1  # aggregated, not one per item
    assert "2 work item(s)" in pbi_errors[0]


def test_preflight_disabled_when_type_listing_fails(tmp_path):
    src = tmp_path / "work_items"
    _write_backup(src, [(1, "Bug")])

    class NoTypesClient(PermissiveFakeClient):
        def get_json(self, path, **kw):
            if "workitemtypes" in path:
                from azdo_backup.client import AzDoError
                raise AzDoError("nope", status_code=404)
            return {}

    client = NoTypesClient(target_types=[])
    id_map = _restore_work_items(client, "New", src, old_project="Old",
                                 stats=RestoreStats())
    assert set(id_map) == {1}  # creation still attempted


def test_restore_summary_carries_errors(tmp_path, monkeypatch):
    """restore_project surfaces error_count so the CLI can exit 3."""
    import azdo_backup.restore as restore_mod

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.json").write_text(json.dumps({"name": "Old"}))
    wi = proj / "work_items"
    _write_backup(wi, [(1, "Bug")])

    class FailingCreateClient(PermissiveFakeClient):
        org_name = "o"

        def patch_json(self, path, body, **kw):
            if "/$" in path:
                from azdo_backup.client import AzDoError
                raise AzDoError("field rejected", status_code=400)
            return {"id": 1}

    monkeypatch.setattr(restore_mod, "_ensure_project",
                        lambda *a, **kw: {"id": "p1"})
    summary = restore_mod.restore_project(
        FailingCreateClient(target_types=["Bug"]), proj, "New",
        skip_repos=True, skip_test_plans=True)
    assert summary["error_count"] >= 1
    assert any("could not create work item" in e for e in summary["errors"])
