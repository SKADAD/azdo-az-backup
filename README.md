# azdo-az-backup

A Python CLI for backing up and restoring Azure DevOps projects and
organizations. Built on top of the Azure DevOps REST API (and `git` for
repositories), with auth aligned to the `az devops` CLI convention.

## Scope

Backs up:

- **Work items** — every revision, all comments, all `AttachedFile` binaries,
  plus links and relations.
- **Git repositories** — bare mirror clones (all branches, tags, refs).
- **Test plans, suites and test cases.**

Intentionally skipped (per requirements): boards configuration, wiki, package
feeds, build artifacts.

Restores into a **new project** (in the same org or a different org). Azure
DevOps does not let you set work-item IDs, so an `id_map.json` from old →
new IDs is written alongside the backup after restore.

## Install

```bash
python -m pip install -e .
# or just:
python -m pip install -r requirements.txt
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
export AZURE_DEVOPS_EXT_PAT=xxxxxxxxxxxxxxxxxxxx
```

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

### Restore into a new project

```bash
azdo-backup restore \
  --org https://dev.azure.com/another-org \
  --source ./backups/projects/Contoso \
  --project Contoso-Restored \
  --process Agile
```

Flags to selectively skip categories: `--skip-work-items`, `--skip-repos`,
`--skip-test-plans`.

## On-disk layout

```
<output>/
  org.json                              # only when --all-projects
  projects/
    <project-name>/
      project.json
      work_items/
        index.json
        <id>.json                       # fields + relations + revisions + comments
        attachments/<id>/<filename>
      repos/
        index.json
        <repo-name>.git/                # bare mirror clone
      test_plans/
        index.json
        <plan-id>/
          plan.json
          suites/<suite-id>.json        # suite + ordered test cases
```

## Restore caveats

Azure DevOps imposes some limitations that no third-party tool can work around:

- **Work item IDs cannot be preserved.** New items get new IDs; the mapping is
  written to `id_map.json` in the source directory after restore.
- **Revision history cannot be replayed verbatim.** Only the final field
  values are restored. The full revision payload remains in the backup JSON,
  and a provenance comment (`Restored from work item #<old-id>`) is added to
  each restored item.
- **Authors/timestamps** are set using `bypassRules=true` where the server
  allows it; some collections will still rewrite them.
- **Test runs / test points / results** are not restored — only the plans,
  suites, and test-case associations.

## Repository clones

Repos are cloned with `git clone --mirror`, so every ref (branches, tags,
notes) is captured. Re-running a backup against an existing output directory
performs a `git remote update --prune` instead of re-cloning.

Git authentication uses a per-invocation `http.extraheader` — the PAT is
**never written into the backup** (not in remote URLs, not in `.git/config`).

## Development

```bash
python -m pip install -e . pytest
pytest
```

CI runs the test suite on Python 3.9 and 3.12 for every push and pull
request (`.github/workflows/ci.yml`).

## Logging

Set `AZDO_BACKUP_LOG=DEBUG` to see request-level logs. Failures are
non-fatal at the per-item level — the tool keeps going and logs the
offending IDs so you can re-run targeted backups later.
