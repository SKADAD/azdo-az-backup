"""Tests for backup helpers."""
from azdo_backup.backup import (BackupStats, _attachment_relations,
                                _list_work_item_ids, _repo_backup_dirs,
                                _WIQL_PAGE)
from azdo_backup.client import AzDoClient


def make_client():
    return AzDoClient("https://dev.azure.com/myorg", pat="x")


def test_list_work_item_ids_pages_past_wiql_cap(monkeypatch):
    c = make_client()
    queries = []

    def fake_post_json(path, body, **kw):
        queries.append(body["query"])
        if len(queries) == 1:
            return {"workItems": [{"id": i} for i in range(1, _WIQL_PAGE + 1)]}
        return {"workItems": [{"id": _WIQL_PAGE + 1}, {"id": _WIQL_PAGE + 2}]}

    monkeypatch.setattr(c, "post_json", fake_post_json)
    ids = _list_work_item_ids(c, "P")
    assert len(ids) == _WIQL_PAGE + 2
    assert len(set(ids)) == len(ids), "no duplicates"
    assert f"[System.Id] > {_WIQL_PAGE}" in queries[1]


def test_repo_backup_dirs_no_collision():
    repos = [{"id": "aaaa1111-0", "name": "web"},
             {"id": "bbbb2222-0", "name": "api"}]
    dirs = _repo_backup_dirs(repos)
    assert dirs["aaaa1111-0"] == "web.git"
    assert dirs["bbbb2222-0"] == "api.git"


def test_repo_backup_dirs_collision_gets_id_suffix_for_all():
    # "a:b" and "a?b" both sanitize to "a_b" — every member must be suffixed
    # so the result is order-independent.
    repos = [{"id": "aaaa1111-0", "name": "a:b"},
             {"id": "bbbb2222-0", "name": "a?b"}]
    dirs = _repo_backup_dirs(repos)
    assert dirs["aaaa1111-0"] == "a_b_aaaa1111.git"
    assert dirs["bbbb2222-0"] == "a_b_bbbb2222.git"
    assert dirs == _repo_backup_dirs(list(reversed(repos)))


def test_attachment_relations_includes_historical():
    wi = {"relations": [{"rel": "AttachedFile", "url": "https://x/att/current"}]}
    revisions = [
        {"relations": [{"rel": "AttachedFile", "url": "https://x/att/removed"},
                       {"rel": "AttachedFile", "url": "https://x/att/current"}]},
        {"relations": [{"rel": "Hyperlink", "url": "https://example.com"}]},
    ]
    rels = _attachment_relations(wi, revisions)
    by_url = {r["url"]: r for r in rels}
    assert set(by_url) == {"https://x/att/current", "https://x/att/removed"}
    assert by_url["https://x/att/current"]["_historical"] is False
    assert by_url["https://x/att/removed"]["_historical"] is True


def test_backup_stats_tracks_errors():
    stats = BackupStats()
    stats.add("work_items", 3)
    stats.error("something failed")
    d = stats.as_dict()
    assert d["counts"]["work_items"] == 3
    assert d["error_count"] == 1


def test_work_item_unchanged_detection(tmp_path):
    import json as _json
    from azdo_backup.backup import _work_item_unchanged
    wi = {"id": 7, "rev": 3, "fields": {"System.CommentCount": 2}}
    assert _work_item_unchanged(tmp_path, wi) is False  # nothing saved yet
    (tmp_path / "7.json").write_text(_json.dumps(
        {"id": 7, "rev": 3, "fields": {"System.CommentCount": 2}}))
    assert _work_item_unchanged(tmp_path, wi) is True
    assert _work_item_unchanged(tmp_path, {**wi, "rev": 4}) is False
    newer_comments = {"id": 7, "rev": 3, "fields": {"System.CommentCount": 5}}
    assert _work_item_unchanged(tmp_path, newer_comments) is False
    (tmp_path / "7.json").write_text("{corrupt")
    assert _work_item_unchanged(tmp_path, wi) is False
