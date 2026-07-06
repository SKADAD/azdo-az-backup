"""End-to-end verification against a stub Azure DevOps server.

Scenario 1: back up project "Alpha" — work items with attachments, full
revision history, links and comments; a git repo with real commits/tags;
classification trees; a test plan with suites and cases — into an offline
zip archive, and verify every artifact.

Scenario 2: restore that backup into project "Beta" in the same collection
and verify what was created server-side (fields remapped, links deduped and
remapped, attachments re-uploaded byte-identical, code pushed, plan/suite/
case wired to the new work item IDs).
"""
from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

import azdo_backup.cli as cli
from tests.azdo_stub import StubState, start_stub


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    root = tmp_path_factory.mktemp("stub")
    # Real source repo with a commit on main and a tag.
    src_work = root / "src_work"
    src_work.mkdir()
    _git("init", "-q", "-b", "main", str(src_work))
    (src_work / "app.py").write_text("print('hello')\n")
    _git("-C", str(src_work), "add", ".")
    _git("-C", str(src_work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "initial")
    _git("-C", str(src_work), "tag", "v1")
    src_bare = root / "alpha_web.git"
    _git("clone", "-q", "--bare", str(src_work), str(src_bare))

    state, base_url, shutdown = start_stub(root)
    state.src_repo_url = src_bare.as_uri()
    yield state, base_url
    shutdown()


@pytest.fixture(scope="module")
def backup_result(stub, tmp_path_factory):
    """Run a full CLI backup of Alpha with --archive; shared by the tests."""
    state, base_url = stub
    out = tmp_path_factory.mktemp("bk") / "backup"
    rc = cli.main(["backup", "--org", f"{base_url}/myorg", "--pat", "test-pat",
                   "--project", "Alpha", "-o", str(out), "--archive"])
    return rc, out, Path(str(out) + ".zip")


# ---------------------------------------------------------------- scenario 1


def test_backup_succeeds_completely(backup_result):
    rc, out, _ = backup_result
    assert rc == 0
    summary = json.loads((out / "projects" / "Alpha" / "summary.json").read_text())
    assert summary["error_count"] == 0, summary["errors"]
    assert summary["counts"]["work_items"] == 3
    assert summary["counts"]["repos"] == 1
    assert summary["counts"]["test_plans"] == 1


def test_backup_work_items_history_links_attachments(backup_result):
    _, out, _ = backup_result
    wi_dir = out / "projects" / "Alpha" / "work_items"
    assert json.loads((wi_dir / "index.json").read_text())["ids"] == [1, 2, 3]

    bug = json.loads((wi_dir / "1.json").read_text())
    # Full history: all 3 revisions captured.
    assert bug["rev"] == 3
    assert [r["rev"] for r in bug["revisions"]] == [1, 2, 3]
    # Linking: parent link present in the saved relations.
    rels = {r["rel"] for r in bug["relations"]}
    assert "System.LinkTypes.Hierarchy-Reverse" in rels
    assert "AttachedFile" in rels
    # Comments captured.
    assert bug["comments"][0]["text"] == "Repro attached."
    # Attachment binary saved byte-identical, at the recorded path.
    att = bug["attachments_local"][0]
    att_file = out / "projects" / "Alpha" / att["file"]
    assert att_file.read_bytes() == StubState.ATTACHMENT_CONTENT

    story = json.loads((wi_dir / "2.json").read_text())
    rels2 = {r["rel"] for r in story["relations"]}
    assert {"System.LinkTypes.Hierarchy-Forward",
            "System.LinkTypes.Related"} <= rels2


def test_backup_code_mirror_has_branches_and_tags(backup_result):
    _, out, _ = backup_result
    mirror = out / "projects" / "Alpha" / "repos" / "web.git"
    assert mirror.is_dir()
    refs = _git("-C", str(mirror), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/main" in refs
    assert "refs/tags/v1" in refs
    index = json.loads((out / "projects" / "Alpha" / "repos" / "index.json").read_text())
    assert index["repos"][0]["backup_dir"] == "web.git"
    assert index["repos"][0]["defaultBranch"] == "refs/heads/main"


def test_backup_test_plans_and_classification(backup_result):
    _, out, _ = backup_result
    tp = out / "projects" / "Alpha" / "test_plans"
    plan = json.loads((tp / "1" / "plan.json").read_text())
    assert plan["name"] == "Release plan"
    suite11 = json.loads((tp / "1" / "suites" / "11.json").read_text())
    assert suite11["test_cases"][0]["workItem"]["id"] == 3
    assert json.loads((tp / "configurations.json").read_text())[
        "configurations"][0]["name"] == "Windows"
    trees = json.loads((out / "projects" / "Alpha" /
                        "classification_nodes.json").read_text())
    sprint = trees["iterations"]["children"][0]
    assert sprint["name"] == "Sprint 1"
    assert sprint["attributes"]["startDate"].startswith("2025-01-01")


def test_backup_offline_zip_is_self_contained(backup_result):
    _, _, zip_path = backup_result
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "projects/Alpha/project.json" in names
        assert "projects/Alpha/summary.json" in names
        # Attachment binary inside the archive, byte-identical.
        att_names = [n for n in names
                     if "/attachments/1/" in n and not n.endswith("/")]
        assert len(att_names) == 1
        assert zf.read(att_names[0]) == StubState.ATTACHMENT_CONTENT
        # Git mirror travels inside the archive too.
        assert any("repos/web.git/" in n for n in names)


# ---------------------------------------------------------------- scenario 2


@pytest.fixture(scope="module")
def restore_result(stub, backup_result):
    """Restore the archived backup into project Beta on the same collection."""
    state, base_url = stub
    _, _, zip_path = backup_result
    rc = cli.main(["restore", "--org", f"{base_url}/myorg", "--pat", "test-pat",
                   "--source", str(zip_path), "--project", "Beta"])
    return rc, state, zip_path


def test_restore_exit_code_and_project_created(restore_result):
    rc, state, _ = restore_result
    assert rc == 0
    assert state.beta_created


def test_restore_work_items_with_remapped_fields(restore_result):
    _, state, _ = restore_result
    created = state.created_work_items
    by_title = {wi["fields"]["System.Title"]: wi for wi in created.values()}
    assert set(by_title) == {"Crash on save", "Save documents", "Save works"}
    bug = by_title["Crash on save"]
    assert bug["type"] == "Bug"
    # Paths re-rooted from Alpha to Beta.
    assert bug["fields"]["System.AreaPath"] == "Beta\\Team A"
    assert bug["fields"]["System.IterationPath"] == "Beta\\Sprint 1"
    # Server-managed / board fields never sent.
    assert "System.Parent" not in bug["fields"]
    assert not any(k.startswith("WEF_") for k in bug["fields"])
    # Test case steps travel along.
    assert "steps" in by_title["Save works"]["fields"]["Microsoft.VSTS.TCM.Steps"]


def test_restore_links_deduped_and_remapped(restore_result):
    _, state, _ = restore_result
    new_by_title = {wi["fields"]["System.Title"]: wid
                    for wid, wi in state.created_work_items.items()}
    bug_id = new_by_title["Crash on save"]
    story_id = new_by_title["Save documents"]
    case_id = new_by_title["Save works"]

    def links(wid):
        return [p["value"] for p in state.relation_patches.get(wid, [])
                if p["path"] == "/relations/-"
                and p["value"]["rel"].startswith("System.LinkTypes.")]

    # The parent<->child link exists exactly once, from the Forward side.
    story_links = links(story_id)
    hier = [v for v in story_links if v["rel"] == "System.LinkTypes.Hierarchy-Forward"]
    assert len(hier) == 1
    assert hier[0]["url"].endswith(f"/workItems/{bug_id}")
    assert links(bug_id) == []  # reverse side adds nothing
    # Symmetric Related link added only from the lower-old-ID endpoint (2).
    related = [v for v in story_links if v["rel"] == "System.LinkTypes.Related"]
    assert len(related) == 1
    assert related[0]["url"].endswith(f"/workItems/{case_id}")
    assert links(case_id) == []


def test_restore_attachment_reuploaded_and_linked(restore_result):
    _, state, _ = restore_result
    assert state.uploaded_attachments["log.txt"] == StubState.ATTACHMENT_CONTENT
    new_by_title = {wi["fields"]["System.Title"]: wid
                    for wid, wi in state.created_work_items.items()}
    bug_patches = state.relation_patches[new_by_title["Crash on save"]]
    att = [p["value"] for p in bug_patches
           if p["value"].get("rel") == "AttachedFile"]
    assert len(att) == 1
    assert "new-att-" in att[0]["url"]


def test_restore_comments_and_provenance(restore_result):
    _, state, _ = restore_result
    new_by_title = {wi["fields"]["System.Title"]: wid
                    for wid, wi in state.created_work_items.items()}
    texts = state.comments[new_by_title["Crash on save"]]
    assert any("Restored from work item #1" in t for t in texts)
    assert any("Repro attached." in t for t in texts)


def test_restore_classification_nodes_created_with_dates(restore_result):
    _, state, _ = restore_result
    areas = [n for n in state.classification_nodes if n[0] == "areas"]
    iterations = [n for n in state.classification_nodes if n[0] == "iterations"]
    assert any(n[2] == "Team A" for n in areas)
    sprint = next(n for n in iterations if n[2] == "Sprint 1")
    assert (sprint[3] or {}).get("startDate", "").startswith("2025-01-01")


def test_restore_code_pushed_with_default_branch(restore_result):
    _, state, _ = restore_result
    repo = state.created_repos["web"]
    refs = _git("-C", str(repo["dir"]), "for-each-ref", "--format=%(refname)")
    assert "refs/heads/main" in refs
    assert "refs/tags/v1" in refs
    # Original file content survives the round-trip.
    content = _git("-C", str(repo["dir"]), "show", "main:app.py")
    assert content == "print('hello')"
    assert state.repo_patches[repo["id"]] == {"defaultBranch": "refs/heads/main"}


def test_restore_test_plan_suites_and_cases_rewired(restore_result):
    _, state, _ = restore_result
    plan = state.created_plans[0]
    assert plan["name"] == "Release plan"
    assert plan["iteration"] == "Beta\\Sprint 1"
    suite = next(s for s in state.created_suites if s["name"] == "Regression")
    assert suite["parentSuite"]["id"] == plan["rootSuite"]["id"]
    new_by_title = {wi["fields"]["System.Title"]: wid
                    for wid, wi in state.created_work_items.items()}
    adds = [ids for (_p, _s, ids) in state.suite_case_adds]
    assert [new_by_title["Save works"]] in adds
    assert any(c["name"] == "Windows" for c in state.created_configurations)
    assert any(v["name"] == "Browser" for v in state.created_variables)


def test_restore_id_map_written_next_to_archive(restore_result):
    _, _, zip_path = restore_result
    id_map_file = zip_path.parent / "id_map.Beta.json"
    assert id_map_file.is_file()
    id_map = json.loads(id_map_file.read_text())
    assert set(id_map) == {"1", "2", "3"}
