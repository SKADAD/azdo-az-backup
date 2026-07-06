"""End-to-end tests for the archival (offline zip) path: verify command,
checksums, tamper detection, dry-run, restore idempotency, cleanup."""
from __future__ import annotations

import json
import shutil
import tempfile
import zipfile

import pytest

import azdo_backup.cli as cli
from azdo_backup.verify import CHECKSUM_FILE, verify_backup
from tests.azdo_stub import start_stub
from tests.test_e2e import _git


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    root = tmp_path_factory.mktemp("stub2")
    src_work = root / "src_work"
    src_work.mkdir()
    _git("init", "-q", "-b", "main", str(src_work))
    (src_work / "app.py").write_text("print('hello')\n")
    _git("-C", str(src_work), "add", ".")
    _git("-C", str(src_work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "initial")
    src_bare = root / "alpha_web.git"
    _git("clone", "-q", "--bare", str(src_work), str(src_bare))

    state, base_url, shutdown = start_stub(root)
    state.src_repo_url = src_bare.as_uri()
    yield state, base_url
    shutdown()


@pytest.fixture(scope="module")
def archive(stub, tmp_path_factory):
    state, base_url = stub
    out = tmp_path_factory.mktemp("bk2") / "backup"
    zip_path = tmp_path_factory.mktemp("bk2-zip") / "alpha-2026-01-01.zip"
    rc = cli.main(["backup", "--org", f"{base_url}/myorg", "--pat", "t",
                   "--project", "Alpha", "-o", str(out),
                   "--archive-path", str(zip_path)])
    assert rc == 0
    return out, zip_path


# ------------------------------------------------------------------ verify


def test_archive_written_to_requested_path_with_manifest(archive):
    out, zip_path = archive
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        manifest = json.loads(zf.read(CHECKSUM_FILE))
    assert any(k.endswith("project.json") for k in manifest)
    # Manifest covers the attachment binary and the git mirror.
    assert any("/attachments/" in k for k in manifest)
    assert any("repos/web.git/" in k for k in manifest)


def test_archive_path_refuses_overwrite(archive, stub):
    _, zip_path = archive
    _, base_url = stub
    rc = cli.main(["backup", "--org", f"{base_url}/myorg", "--pat", "t",
                   "--project", "Alpha",
                   "-o", str(zip_path.parent / "other-out"),
                   "--archive-path", str(zip_path)])
    assert rc == cli.EXIT_ERROR


def test_verify_good_archive_and_dir(archive):
    out, zip_path = archive
    assert cli.main(["verify", "--source", str(zip_path)]) == 0
    assert cli.main(["verify", "--source", str(out)]) == 0


def test_verify_reports_counts(archive):
    _, zip_path = archive
    report = verify_backup(zip_path)
    assert report.ok
    assert report.counts["work_items"] == 3
    assert report.counts["attachments"] >= 1
    assert report.counts["repos"] == 1
    assert report.counts["test_plans"] == 1


def test_verify_detects_tampered_attachment(archive, tmp_path):
    _, zip_path = archive
    tampered_dir = tmp_path / "tampered"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tampered_dir)
    att = next(p for p in tampered_dir.rglob("*") if "/attachments/1/" in
               p.as_posix() and p.is_file())
    att.write_bytes(b"bit rot")
    report = verify_backup(tampered_dir)
    assert not report.ok
    assert any("checksum mismatch" in p for p in report.problems)
    assert cli.main(["verify", "--source", str(tampered_dir)]) == cli.EXIT_PARTIAL


def test_verify_detects_missing_work_item(archive, tmp_path):
    _, zip_path = archive
    broken_dir = tmp_path / "broken"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(broken_dir)
    (broken_dir / CHECKSUM_FILE).unlink()  # isolate the structural check
    (broken_dir / "projects" / "Alpha" / "work_items" / "2.json").unlink()
    report = verify_backup(broken_dir)
    assert any("work item 2 indexed but not saved" in p for p in report.problems)


def test_verify_detects_incomplete_backup_no_summary(archive, tmp_path):
    _, zip_path = archive
    broken_dir = tmp_path / "nosummary"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(broken_dir)
    (broken_dir / CHECKSUM_FILE).unlink()
    (broken_dir / "projects" / "Alpha" / "summary.json").unlink()
    report = verify_backup(broken_dir)
    assert any("never completed" in p for p in report.problems)


def test_verify_truncated_zip_is_a_clean_error(archive, tmp_path):
    _, zip_path = archive
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(zip_path.read_bytes()[: zip_path.stat().st_size // 2])
    report = verify_backup(truncated)
    assert not report.ok
    rc = cli.main(["verify", "--source", str(truncated)])
    assert rc == cli.EXIT_PARTIAL


def test_restore_truncated_zip_is_a_clean_error(archive, stub, tmp_path):
    _, zip_path = archive
    _, base_url = stub
    truncated = tmp_path / "truncated2.zip"
    truncated.write_bytes(zip_path.read_bytes()[: zip_path.stat().st_size // 2])
    rc = cli.main(["restore", "--org", f"{base_url}/myorg", "--pat", "t",
                   "--source", str(truncated), "--project", "Beta"])
    assert rc == cli.EXIT_ERROR  # message, not a traceback


# ------------------------------------------------------------------ dry run


def test_restore_dry_run_needs_no_credentials(archive, monkeypatch, capsys):
    _, zip_path = archive
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.delenv("AZDO_PAT", raising=False)
    rc = cli.main(["restore", "--org", "https://dev.azure.com/ignored",
                   "--source", str(zip_path), "--project", "Beta",
                   "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verify"]["ok"] is True
    plan = payload["would_restore"][0]
    assert plan["source_project"] == "Alpha"
    assert plan["work_items"] == 3
    assert plan["repos"] == ["web"]
    assert plan["test_plans"] == ["Release plan"]
    assert plan["process_template"] == "Agile"


# ------------------------------------------------------- restore idempotency


def test_restore_twice_from_archive_is_idempotent(archive, stub):
    _, zip_path = archive
    state, base_url = stub
    args = ["restore", "--org", f"{base_url}/myorg", "--pat", "t",
            "--source", str(zip_path), "--project", "Beta"]
    assert cli.main(args) == 0
    wi_after_first = dict(state.created_work_items)
    plans_after_first = len(state.created_plans)
    comments_after_first = {k: len(v) for k, v in state.comments.items()}

    # Second run must load id_map.Beta.json (next to the zip) and the
    # existing plan list, and create nothing new.
    assert cli.main(args) == 0
    assert state.created_work_items == wi_after_first
    assert len(state.created_plans) == plans_after_first
    assert {k: len(v) for k, v in state.comments.items()} == comments_after_first

    id_map_file = zip_path.parent / "id_map.Beta.json"
    assert id_map_file.is_file()
    assert set(json.loads(id_map_file.read_text())) == {"1", "2", "3"}


def test_zip_restore_cleans_up_extraction_dir(archive, stub, monkeypatch, tmp_path):
    _, zip_path = archive
    _, base_url = stub
    scratch = tmp_path / "scratch-tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    try:
        rc = cli.main(["restore", "--org", f"{base_url}/myorg", "--pat", "t",
                       "--source", str(zip_path), "--project", "Beta"])
        assert rc == 0
        leftovers = [p for p in scratch.iterdir()
                     if p.name.startswith("azdo-restore-")]
        assert leftovers == []
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
