# SQLite state: operation and recovery

The backend is implemented for legacy paper state, SMC paper state, and recorded holdings. It uses one owned connection, SQLite WAL with `synchronous=FULL`, and incremental records. The current dashboard's live state was not migrated or restarted during implementation.

## Runtime and selection

Use `.\.venv\Scripts\python.exe` from this checkout. This local environment uses the existing bundled Python 3.12.14 with SQLite 3.53.1 and the project's requirements. No system Python or SQLite DLL was replaced. On another machine, create an environment from a Python interpreter whose **linked SQLite** has the WAL-reset fix, then install `requirements.txt`.

```powershell
.\.venv\Scripts\python.exe -B -c "import sys, sqlite3; print(sys.executable); print(sqlite3.sqlite_version)"
```

The backend accepts SQLite 3.51.3+, 3.50.7+ within the 3.50 branch, or 3.44.6+ within the 3.44 branch. Older builds fail before opening mutable database state. The previously used system Python links 3.50.4. The gate follows the [documented WAL-reset fix](https://www.sqlite.org/wal.html#walresetbug).

`auto` selects SQLite if its database exists, JSON if only JSON sources exist, and SQLite for a new base. Explicit `sqlite` with existing unmigrated JSON requires import first. Explicit `json` refuses a base with an existing database. Invalid databases fail without resetting state or using stale JSON. The database must live on a local disk; obvious UNC paths are rejected.

`--state .\example.json` derives `example.sqlite3`, `example.smc.json`, and `example.positions.json`. Keep using the logical base with the dashboard and maintenance tools. Passing the `.sqlite3` filename as `--state` is rejected. One process owns the base and canonical database locks throughout its lifetime; migration, backup, export, and spot correction take the same locks.

## Cutover

Run these commands from the repository root after stopping the dashboard. The first command is an advisory dry run and makes no files; the real import repeats its checks under ownership locks.

```powershell
.\.venv\Scripts\python.exe -B -m adaptive_crypto.state_migration import --state .\adaptive_crypto_reclaim_state.json --settings .\adaptive_crypto_settings.json --dry-run
.\.venv\Scripts\python.exe -B -m adaptive_crypto.state_migration import --state .\adaptive_crypto_reclaim_state.json --settings .\adaptive_crypto_settings.json
.\.venv\Scripts\python.exe -B -m adaptive_crypto.state_migration verify --state .\adaptive_crypto_reclaim_state.json
.\start_dashboard.ps1 -StateBackend auto
```

Import saves exact source bytes, settings and provenance in `backup/latest.zip`, replacing the previous backup. If a database already exists, the archive also contains its complete current state before import. Import validates the active paper study and holdings, normalizes their existing startup rules, writes namespaces and provenance together, verifies the staged database, checkpoints and closes it, then publishes the final filename. A failed staging operation leaves the previous JSON selection intact. Interrupted database staging files are retained for diagnosis; they are never selected as active state.

Repeat import with matching source/settings provenance reports `already_imported`, even if database revisions have subsequently advanced. It does not rewind those revisions or reapply restart recovery. Changed input files conflict rather than replacing database data. Holdings provenance is independent of paper strategy settings, allowing a later explicit import of a previously inactive study. An inactive study remains untouched until selected with matching settings.

On a genuinely fresh base, complete initial namespaces are also staged before publication. A missing namespace with an existing JSON source requires explicit import; a namespace with neither stored state nor a source can initialize fresh. Runtime startup validates stored documents against their saved configurations before applying current settings, so damaged fingerprints are not treated as a settings reset.

## Backup and rollback

pyPTA keeps **one complete backup**, `backup/latest.zip`, in the project root. A custom `--state` in a separate study directory keeps its `backup` folder beside that state base. Settings saves, model selection, settings application, purge, startup archives, imports and spot corrections all replace the same archive before changing their data. Existing historical backups are left in place; future backups do not create timestamped copies.

With the dashboard stopped, create or replace the archive manually (works with JSON or SQLite):

```powershell
.\.venv\Scripts\python.exe -B -m adaptive_crypto.state_migration backup --state .\adaptive_crypto_reclaim_state.json --settings .\adaptive_crypto_settings.json
```

The archive contains the saved file as `settings/settings.json`, current trading data under `state/`, and a manifest with file hashes. SQLite uses its online backup API, including committed WAL data, to produce an independently readable database containing every stored strategy and holdings namespace. It also includes JSON snapshots and each strategy's saved settings context. JSON backups include every existing strategy sidecar, including inactive studies. Startup and import archives retain exact original source bytes, even for a damaged legacy file. If no settings file exists, the manifest records that absence; use `--settings` for a custom configuration.

The new archive is written to temporary storage, flushed, and verified before atomically replacing `latest.zip`. A failed backup aborts the associated change and retains the last good archive. Only the latest backup is retained: a subsequent settings save or apply also replaces a pre-purge backup.

To inspect or restore, stop the dashboard and extract the archive into a new recovery directory. Use the logical state base named in `manifest.json` under its `state` subfolder with `--state`, and `settings/settings.json` with `--settings`. For a pre-apply backup where the saved settings differ from the database's active context, use the corresponding `settings/smc.settings.json`, `settings/neural.settings.json` or `settings/legacy.settings.json` to reopen the earlier strategy configuration. SQLite restoration uses the archived database, not any source JSON retained for import evidence. For example, an archive extracted to `recovery` can be opened with:

```powershell
.\.venv\Scripts\python.exe -B -m adaptive_crypto --state .\recovery\state\adaptive_crypto_reclaim_state.json --settings .\recovery\settings\smc.settings.json --state-backend auto
```

For a JSON rollback from the current database, export committed namespaces into a new directory. This preserves delivery statuses exactly and does not run restart recovery:

```powershell
.\.venv\Scripts\python.exe -B -m adaptive_crypto.state_migration export --state .\adaptive_crypto_reclaim_state.json --output-dir .\sqlite-rollback --settings .\adaptive_crypto_settings.json
.\.venv\Scripts\python.exe -B -m adaptive_crypto --state .\sqlite-rollback\adaptive_crypto_reclaim_state.json --settings .\sqlite-rollback\settings.json --state-backend json
```

The export command's optional `--settings` must match the saved active study configuration. Export also writes per-strategy settings from the corresponding saved contexts, so each stored study can be reopened with its own settings. An export manifest names missing namespaces; no historical study is fabricated. Explicit export directories are separate from the single backup archive and are never overwritten. Startup of the rollback app applies the normal interrupted-delivery rules.

Keep settings and the physical backup/export together. Do not restore old pre-import sidecars after the database has advanced. Do not copy only an actively open database file or manually delete WAL/SHM files. Domain backups for settings changes and explicit spot correction are generated from current database snapshots, not the retained JSON sources.

## Integrity and failure behavior

State mutation and its outbox entries commit together. Callback, validation, encoding, and failed SQL writes roll back before new memory state is published. A failed rollback or uncertain commit poisons the coordinator until close/reopen; it never falls back to JSON. Returned objects, snapshots and retained callback arguments cannot modify committed SQLite state.

The dispatcher commits `running` before contacting the sender, releases persistence locks during delivery, and then saves the result. A failed claim sends nothing. A failed result commit leaves the durable claim for recovery; an interrupted Telegram delivery becomes `uncertain` and is not automatically resent. This retains the existing delivery contract; it is not an exactly-once network protocol.

The SQLite schema stores an independent schema version plus domain versions. Each namespace also stores the configuration used to validate it, committed atomically with its document. This `context_json` column is the one schema addition to the planning draft: it makes verification and rollback independent of whichever settings happen to be active later.

No-op transactions perform no SQL writes. A changed outbox event updates one record and its namespace revision, without rewriting other events or history. Snapshots are detached copies from committed memory. Full-document validation and copying remain CPU costs; WAL FULL still performs durable commit synchronization. Latency gains are measurements, not a promise that all workloads or physical disks will improve equally.

## Verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_persistence test_state_migration test_persistence_integration
.\.venv\Scripts\python.exe -B -m unittest discover -s .
node --test test_gex_ui.js
.\.venv\Scripts\python.exe -B qualification_audit/check_formulas.py
.\.venv\Scripts\python.exe -B benchmarks/persistence_benchmark.py --output .\docs\persistence_benchmark.json
```

All state used by these tests and the benchmark is disposable. Providers and senders are mocked; no live market scans, Telegram sends or paid AI requests occur. The benchmark compares JSON and SQLite on the same interpreter and fixtures, checks equality of final business state, and reports snapshot, no-op, single-event, single-trade, claim/finish, and three-asset scan timings. It includes durability and checkpoint costs. Its payload-byte measurements are logical serialization amounts, not physical device I/O.

## Measured results, 11 September 2026

Seven measured repetitions per case, after warming, used Python 3.12.14 / SQLite 3.53.1. The original JSON baseline was loaded from the source captured before implementation. Full results, including p95, claim/finish, trade updates, snapshots, three-asset scans and checkpoints, are in `persistence_benchmark.json`.

| History events | Original JSON event commit, median | Current JSON event commit | SQLite event commit | SQLite vs original |
| --- | --- | --- | --- | --- |
| 10 | 6.241 ms | 6.751 ms | 4.228 ms | 1.48x faster |
| 1,000 | 121.081 ms | 142.510 ms | 43.365 ms | 2.79x faster |
| 10,000 | 838.325 ms | 1,094.082 ms | 480.867 ms | 1.74x faster |

One event mutation updated two SQLite rows at every history size: the event and its namespace revision. Its record payload was 339 bytes, versus a complete 6,807,925-byte JSON rewrite at 10,000 events. This excludes SQLite namespace metadata, page writes and checkpoint amplification; physical disk write reduction was not measured. Every no-op produced zero SQLite writes. Final business documents matched across all three backends.

The current JSON compatibility backend can be slower for changed commits because it now detaches retained callback arguments and results before publishing state. No-op commits improve substantially. SQLite still copies and validates full documents: the 10,000-event snapshot median was 217.959 ms versus 192.736 ms for original JSON, so this change does not claim faster snapshots. These local samples are not a general throughput guarantee.
