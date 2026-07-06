from azdo_backup.restore import _fields_to_patch, _find_attachment_file


def test_fields_to_patch_skips_server_managed_fields():
    fields = {
        "System.Id": 12,
        "System.Rev": 4,
        "System.TeamProject": "Old",
        "System.WorkItemType": "Bug",
        "System.Title": "A bug",
        "Custom.Field": 7,
    }
    patch = _fields_to_patch(fields)
    paths = {p["path"] for p in patch}
    assert paths == {"/fields/System.Title", "/fields/Custom.Field"}
    assert all(p["op"] == "add" for p in patch)


def test_fields_to_patch_flattens_identity_objects():
    fields = {"System.AssignedTo": {
        "displayName": "Jane Doe", "uniqueName": "jane@example.com",
    }}
    patch = _fields_to_patch(fields)
    assert patch == [{"op": "add", "path": "/fields/System.AssignedTo",
                      "value": "jane@example.com"}]


def test_find_attachment_prefers_recorded_metadata(tmp_path):
    att_dir = tmp_path / "work_items" / "attachments" / "42"
    att_dir.mkdir(parents=True)
    recorded = att_dir / "guid_report.pdf"
    recorded.write_bytes(b"pdf")
    rel = {"rel": "AttachedFile",
           "url": "https://dev.azure.com/o/_apis/wit/attachments/guid",
           "attributes": {"name": "report.pdf"}}
    wi = {"id": 42, "relations": [rel],
          "attachments_local": [{"name": "report.pdf",
                                 "file": "work_items/attachments/42/guid_report.pdf",
                                 "rel": rel}]}
    assert _find_attachment_file(tmp_path, wi, rel) == recorded


def test_find_attachment_falls_back_to_sanitized_name(tmp_path):
    att_dir = tmp_path / "work_items" / "attachments" / "42"
    att_dir.mkdir(parents=True)
    (att_dir / "report.pdf").write_bytes(b"pdf")
    rel = {"rel": "AttachedFile", "url": "https://x/att/guid",
           "attributes": {"name": "report.pdf"}}
    wi = {"id": 42, "attachments_local": []}
    assert _find_attachment_file(tmp_path, wi, rel) == att_dir / "report.pdf"


def test_find_attachment_missing_returns_none(tmp_path):
    rel = {"rel": "AttachedFile", "url": "https://x/att/guid",
           "attributes": {"name": "report.pdf"}}
    wi = {"id": 42, "attachments_local": []}
    assert _find_attachment_file(tmp_path, wi, rel) is None


def test_remap_classification_path():
    from azdo_backup.restore import remap_classification_path
    assert remap_classification_path("Old\\Team A\\Sub", "Old", "New") == "New\\Team A\\Sub"
    assert remap_classification_path("Old", "Old", "New") == "New"
    assert remap_classification_path("old\\X", "OLD", "New") == "New\\X"
    assert remap_classification_path("Other\\X", "Old", "New") == "Other\\X"
    assert remap_classification_path(None, "Old", "New") is None
    assert remap_classification_path("", "Old", "New") == ""


def test_fields_to_patch_remaps_paths():
    fields = {
        "System.AreaPath": "Old\\Team A",
        "System.IterationPath": "Old\\Sprint 1",
        "System.Title": "t",
    }
    patch = _fields_to_patch(fields, old_project="Old", new_project="New")
    by_path = {p["path"]: p["value"] for p in patch}
    assert by_path["/fields/System.AreaPath"] == "New\\Team A"
    assert by_path["/fields/System.IterationPath"] == "New\\Sprint 1"
