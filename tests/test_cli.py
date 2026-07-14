"""CLI dispatch, exit codes and layout tests (network calls monkeypatched)."""
import json

import pytest

import azdo_backup.cli as cli
from azdo_backup.backup import BackupStats
from azdo_backup.client import AzDoError


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    class FakeClient:
        def __init__(self, org, pat=None, **kw):
            self.org_name = "myorg"

        def list_projects(self):
            return [{"id": "1", "name": "P", "state": "wellFormed"}]

    monkeypatch.setattr(cli, "AzDoClient", FakeClient)
    return FakeClient


def test_list_projects_exit_code(capsys):
    assert cli.main(["list-projects", "--org", "https://dev.azure.com/o"]) == 0
    assert "P" in capsys.readouterr().out


def test_backup_single_project_uses_projects_layout(monkeypatch, tmp_path):
    captured = {}

    def fake_backup_project(client, project, out, stats=None, **kw):
        captured["out"] = out
        return BackupStats()

    monkeypatch.setattr("azdo_backup.backup.backup_project", fake_backup_project)
    rc = cli.main(["backup", "--org", "https://dev.azure.com/o",
                   "--project", "Contoso", "-o", str(tmp_path)])
    assert rc == 0
    assert captured["out"] == tmp_path / "projects" / "Contoso"


def test_backup_partial_failure_exit_code(monkeypatch, tmp_path):
    def fake_backup_project(client, project, out, stats=None, **kw):
        stats = BackupStats()
        stats.error("boom")
        return stats

    monkeypatch.setattr("azdo_backup.backup.backup_project", fake_backup_project)
    rc = cli.main(["backup", "--org", "https://dev.azure.com/o",
                   "--project", "Contoso", "-o", str(tmp_path)])
    assert rc == cli.EXIT_PARTIAL


def test_restore_requires_project_or_all(tmp_path):
    rc = cli.main(["restore", "--org", "https://dev.azure.com/o",
                   "--source", str(tmp_path)])
    assert rc == cli.EXIT_USAGE


def test_restore_all_projects_iterates_org_backup(monkeypatch, tmp_path):
    for name in ("Alpha", "Beta"):
        proj = tmp_path / "projects" / name
        proj.mkdir(parents=True)
        (proj / "project.json").write_text(json.dumps({"name": name}))
    restored = []

    def fake_restore_project(client, src, target, **kw):
        restored.append(target)
        return {"target_project": target}

    monkeypatch.setattr("azdo_backup.restore.restore_project", fake_restore_project)
    rc = cli.main(["restore", "--org", "https://dev.azure.com/o",
                   "--source", str(tmp_path), "--all-projects",
                   "--prefix", "Restored-"])
    assert rc == 0
    assert restored == ["Restored-Alpha", "Restored-Beta"]


def test_restore_all_projects_partial_failure(monkeypatch, tmp_path):
    proj = tmp_path / "projects" / "Alpha"
    proj.mkdir(parents=True)
    (proj / "project.json").write_text(json.dumps({"name": "Alpha"}))

    def fake_restore_project(client, src, target, **kw):
        raise AzDoError("cannot create")

    monkeypatch.setattr("azdo_backup.restore.restore_project", fake_restore_project)
    rc = cli.main(["restore", "--org", "https://dev.azure.com/o",
                   "--source", str(tmp_path), "--all-projects"])
    assert rc == cli.EXIT_PARTIAL
