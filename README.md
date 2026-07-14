# azdo-az-backup

A Python CLI for backing up and restoring Azure DevOps projects and
organizations. Built on top of the Azure DevOps REST API (and `git` for
repositories), with auth aligned to the `az devops` CLI convention.

## Scope

Backs up:

- **Work items** — every revision, all comments, all `AttachedFile` binaries
  (including attachments later removed from the item), links and relations.
- **Git repositories** — bare mirror clones (all branches, tags, refs).
- **Test plans, suites, test cases, configurations and variables.**
- **Area/iteration trees** (including iteration dates).

Intentionally skipped (per requirements): boards configuration, wiki, package
feeds, build artifacts. Not covered by design: TFVC repositories (the tool
warns loudly if a project uses TFVC), Git LFS objects, test run results.

Restores into **new projects** (same org or a different org) — a single
project or a whole org backup at once. Azure DevOps does not let you set
work-item IDs, so an `id_map.<target>.json` (old → new) is written next to
the backup; re-running a restore loads it and resumes instead of duplicating.

## Install

```bash
python -m pip install -e .          # tool only
python -m pip install -e .[dev]     # + pytest, ruff
```

Requires Python 3.9+ and `git` on `PATH`.

## Auth

Generate a Personal Access Token with at least these scopes:

- `Work Items (Read & Write)`
- `Code (Read & Write)` (`Full` if you also want to restore)
- `Test Management (Read & Write)`
- `Project and Team (Read & Write)` (only needed for restore)

Pass it via `--pat` or, like `az devops`, export it:

```bash
export AZURE_DEVOPS_EXT_PAT=xxxxxxxxxxxxxxxxxxxx   # or AZDO_PAT
```

An invalid or expired PAT is detected explicitly (the service answers with a
sign-in page, not a 401) and aborts the run with a clear error.

## Usage

### List projects

```bash
azdo-backup list-projects --org https://dev.azure.com/myorg
```

### Back up a single project

```bash
azdo-backup backup \
  --org https://dev.azure.com/myorg \
  --project Contoso \
  --output ./backups
```

### Back up the entire organization

```bash
azdo-backup backup \
  --org https://dev.azure.com/myorg \
  --all-projects \
  --output ./backups
```

Both write to `<output>/projects/<name>`, so restore commands are identical
either way. Re-running a backup is incremental for git repos, attachments
and unchanged work items.

Work items are fetched with 4 concurrent workers by default; use
`--workers N` to raise it for large projects (or lower it when hitting
rate limits). In `--all-projects` mode, `--exclude-projects "Sandbox,Temp"`
skips projects you don't want in the archive. Incremental re-runs prune
work items, attachments and repo mirrors that were deleted in the org,
so long-lived backup directories mirror the current state.

Add `--archive` to also produce a single self-contained `<output>.zip` —
an offline artifact containing everything (work item JSON, attachment
binaries, git mirrors, test plans) plus a sha256 manifest
(`checksums.json`). Use `--archive-path backups/contoso-2026-01-01.zip`
to name the artifact (existing files are never overwritten, so dated
rotation is safe).

### Verify an archival backup (offline)

```bash
azdo-backup verify --source ./backups/contoso-2026-01-01.zip
```

No credentials needed. Checks zip integrity (CRCs), the sha256 manifest,
the completion marker (`summary.json`, error-free), that every indexed
work item and recorded attachment binary is present, and that every git
mirror is a valid bare repository containing its default branch. Exit
code 0 = trustworthy, 3 = problems found (listed in the JSON output).

### Rehearse a restore

```bash
azdo-backup restore --source ./backups/contoso-2026-01-01.zip \
  --project Contoso-Restored --dry-run
```

Runs verification and prints what would be created (work items,
attachments, repos, test plans, process template) without credentials
and without touching any organization.

### Restore a project

```bash
azdo-backup restore \
  --org https://dev.azure.com/another-org \
  --source ./backups/projects/Contoso \
  --project Contoso-Restored
```

`--source` also accepts a `.zip` produced by `backup --archive` (extracted
to a temp dir automatically; the resume id-map is kept next to the archive).
The target can be a different org or **the same collection** — e.g. clone
`Contoso` to `Contoso-Copy` in place. If an archive contains several
projects, pick one with `--source-project`.

### Restore an entire org backup

```bash
azdo-backup restore \
  --org https://dev.azure.com/another-org \
  --source ./backups \
  --all-projects \
  --prefix "Restored-"
```

Target names default to the original project names (plus the optional
prefix). Flags to selectively skip categories: `--skip-work-items`,
`--skip-repos`, `--skip-test-plans`. The process template defaults to the
source project's process; override with `--process`.

### Exit codes

| code | meaning |
|------|---------|
| 0    | success |
| 1    | fatal error (auth, network exhaustion) |
| 2    | usage error |
| 3    | finished, but with per-item errors (see `summary.json` / output) |

Restores report the same way: the summary JSON carries `error_count` and
the full error list, and any lossy restore (failed items, attachments,
pushes, suites) exits 3 instead of masquerading as success.

## On-disk layout

```
<output>/
  org.json                              # only when --all-projects
  summary.json                          # org-level completion marker
  projects/
    <project-name>/
      project.json
      manifest.json                     # tool version, org, timestamp
      summary.json                      # written LAST — completion marker + errors
      classification_nodes.json         # area/iteration trees (with dates)
      work_items/
        index.json
        <id>.json                       # fields + relations + revisions + comments
        attachments/<id>/<guid>_<name>  # deterministic, collision-free names
      repos/
        index.json                      # repo metadata incl. backup_dir mapping
        <repo-name>.git/                # bare mirror clone
      test_plans/
        index.json
        configurations.json
        variables.json
        <plan-id>/
          plan.json
          suites/<suite-id>.json        # suite + ordered test cases
```

A backup without a `summary.json` (or with `error_count > 0` in it) is
incomplete — the file is written only after everything else finished.

## Restore behavior

- The target project is created with the **source project's process template**
  (override with `--process`).
- The **area/iteration tree** is restored from `classification_nodes.json`
  (keeping iteration dates), and any paths referenced by work items or test
  plans are created as a fallback; all paths are re-rooted under the new
  project name.
- **Work items**: full fields first; if the server rejects them (e.g. a state
  that doesn't exist in the target process), retried without state fields,
  then with a minimal field set — so the item is never silently lost.
  Board-scoped `WEF_*` fields and server-managed fields are excluded.
  Work item types missing from the target process (cross-process restores)
  are detected up front and reported as one aggregated error per type.
- **Work-item links** are recreated once per link (directional links from the
  forward side, symmetric links from the lower-ID side) with IDs remapped.
  Unsupported link types (`ArtifactLink` commit/build links, cross-org
  remote links) are counted and reported, not silently dropped.
- **Attachments** are re-uploaded (chunked above 100 MB).
- **Repos** are recreated with their original names and default branches
  (from `repos/index.json`) and pushed as explicit `refs/heads/*` and
  `refs/tags/*` refspecs — Azure DevOps rejects pushes of its
  server-managed hidden refs (`refs/pull/*`), so a raw `--mirror` push
  would fail.
- **Test plans**: configurations and variables are recreated by name, suites
  parent-first (requirement suites remap their requirement ID; query suites
  get their WIQL rewritten to the new project name), and test cases are added
  only to static suites.
- **Resume**: `id_map.<target>.json` tracks both created work items and
  which ones finished enrichment (attachments/links/comments), so a failed
  restore — even one interrupted mid-enrichment — can simply be re-run
  without duplicating items. Progress is checkpointed every 25 items; a
  hard kill may re-post comments for at most that window. Test plans that
  already exist by name are skipped.
- **Shared-steps references** inside test case steps XML (`<compref ref>`)
  are rewritten to the new work item IDs.

## Restore caveats

Azure DevOps imposes some limitations that no third-party tool can work around:

- **Work item IDs cannot be preserved.** New items get new IDs; the mapping
  is written to `id_map.<target>.json` in the source directory.
- **Revision history cannot be replayed verbatim.** Only the final field
  values are restored. The full revision payload remains in the backup JSON,
  and a provenance comment (`Restored from work item #<old-id>`) is added to
  each restored item.
- **Authors/timestamps** are set using `bypassRules=true` where the server
  allows it; some collections will still rewrite them. The PAT identity
  needs the "Bypass rules on work item updates" permission.
- **Test runs / test points / results** are not restored.
- **Old work item IDs inside field text** (descriptions, test case steps
  referencing shared steps) are not rewritten.

## Security notes

- The PAT is never written into the backup: git authenticates through
  environment-based config (`GIT_CONFIG_*`), keeping it out of remote URLs,
  `.git/config` files, and the process table.
- Attachment downloads are atomic (temp file + rename) and verified not to
  be sign-in pages, so an expiring PAT cannot corrupt backed-up binaries.

## Development

```bash
python -m pip install -e .[dev]
ruff check azdo_backup tests
pytest
```

CI runs lint + tests on Python 3.9 and 3.12 for every push and pull request.
Set `AZDO_BACKUP_LOG=DEBUG` for request-level logs.
