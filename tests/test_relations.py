"""Tests for relation deduplication during restore."""
from azdo_backup.client import AzDoClient
from azdo_backup.restore import _restore_relations


class RecordingClient:
    """Stands in for AzDoClient: records patches, uploads nothing."""

    def __init__(self):
        self.patches = []

    def _full_url(self, path, project=None):
        return f"https://dev.azure.com/o/{project}/{path}"

    def patch_json(self, path, body, **kw):
        self.patches.extend(body)

    def request(self, *a, **kw):  # attachments not exercised here
        raise AssertionError("unexpected request")


def _link(rel_type, target_id):
    return {"rel": rel_type,
            "url": f"https://dev.azure.com/o/_apis/wit/workItems/{target_id}"}


def test_reverse_links_are_skipped():
    c = RecordingClient()
    wi = {"id": 2, "relations": [_link("System.LinkTypes.Hierarchy-Reverse", 1)]}
    _restore_relations(c, "P", None, wi, 102, {1: 101, 2: 102})
    assert c.patches == []


def test_forward_links_are_restored_and_remapped():
    c = RecordingClient()
    wi = {"id": 1, "relations": [_link("System.LinkTypes.Hierarchy-Forward", 2)]}
    _restore_relations(c, "P", None, wi, 101, {1: 101, 2: 102})
    assert len(c.patches) == 1
    assert c.patches[0]["value"]["url"].endswith("/_apis/wit/workItems/102")


def test_symmetric_links_added_only_from_lower_id():
    lower = RecordingClient()
    wi_low = {"id": 1, "relations": [_link("System.LinkTypes.Related", 2)]}
    _restore_relations(lower, "P", None, wi_low, 101, {1: 101, 2: 102})
    assert len(lower.patches) == 1

    higher = RecordingClient()
    wi_high = {"id": 2, "relations": [_link("System.LinkTypes.Related", 1)]}
    _restore_relations(higher, "P", None, wi_high, 102, {1: 101, 2: 102})
    assert higher.patches == []


def test_links_to_unrestored_items_are_dropped():
    c = RecordingClient()
    wi = {"id": 1, "relations": [_link("System.LinkTypes.Hierarchy-Forward", 99)]}
    _restore_relations(c, "P", None, wi, 101, {1: 101})
    assert c.patches == []


def test_hyperlinks_pass_through():
    c = RecordingClient()
    wi = {"id": 1, "relations": [{"rel": "Hyperlink", "url": "https://example.com",
                                  "attributes": {"comment": "docs"}}]}
    _restore_relations(c, "P", None, wi, 101, {1: 101})
    assert len(c.patches) == 1
    assert c.patches[0]["value"]["rel"] == "Hyperlink"
