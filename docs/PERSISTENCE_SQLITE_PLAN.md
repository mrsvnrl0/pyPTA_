# SQLite persistence implementation contract

Prepared 11 September 2026 for GPT-Luna Reserve. Implementation is now complete; see `SQLITE_PERSISTENCE.md` for operation and verification. Production migration has not been performed. The implemented namespace schema adds `context_json` to persist the configuration used for validation and rollback atomically with state; the draft schema below records the original design.

## 1. Decision and scope

Implement SQLite WAL using Python's `sqlite3`, retaining the current in-memory committed snapshots and synchronous transaction API. Persist changed records instead of replacing a complete JSON ledger. Keep JSON as an explicit compatibility and export backend during rollout. Select SQLite after an explicit, verified migration.

This single-machine Windows dashboard already has a single-process ownership model. SQLite fits that deployment without an additional database service. Redis is outside this change: adding a server, client dependency, persistence policy, and deployment lifecycle would broaden the work without an established need for distributed writers.

Use `synchronous=FULL`. WAL does not eliminate disk I/O, and FULL still syncs each changed commit. The expected gains are fewer rewritten bytes, removal of temporary-file replacement on normal commits, and no writes for unchanged transactions. Measure the improvement; do not promise a particular speedup. Full-document copying and validation remain CPU costs in this first implementation. [SQLite durability settings](https://www.sqlite.org/pragma.html#pragma_synchronous)

Preserve strategy formulas, cash and collateral calculations, stored targets, historical evidence, IDs, request deduplication, array order, and delivery semantics. Keep the existing per-store transaction boundaries; placing namespaces in one database does not make a whole market scan atomic. Do not add an ORM, asynchronous write queue, automatic event pruning, generalized repository framework, or trading changes.

## 2. Evidence from this checkout

| Location | Current behavior and implementation consequence |
| --- | --- |
| `adaptive_crypto/ledger.py:20` | `atomic_json` pretty-prints the whole document, flushes, calls `os.fsync`, then `os.replace`. Retain it for JSON compatibility and exported backups. |
| `adaptive_crypto/ledger.py:181` | `StateStore` serializes copy/mutate/validate/save/publish with an RLock. It currently writes even on a no-op and returns a potentially aliased callback result. |
| `adaptive_crypto/smc_ledger.py:96` | `SMCStore` skips equal-document writes and returns a deep copy. Startup handles settings changes, spot-mode restrictions, disabled assets, and interrupted deliveries. |
| `adaptive_crypto/positions.py:115` | `PositionStore` owns holdings, watches, buy watches, and alerts together. It skips unchanged writes. Damaged holdings fail without overwrite. |
| `adaptive_crypto/positions.py:148` | Explicit spot correction saves an audit backup and records its path in corrected positions. It must back up the current database snapshot after cutover. |
| `adaptive_crypto/notifications.py:67` | `dispatch_once` durably claims an event, releases the transaction before sending, then persists the result. Idle-worker prechecks already avoid transactions. |
| `adaptive_crypto/cli.py:20` | `SingleInstance` guards the base JSON path across processes. Keep that protection and add canonical database ownership. |
| `adaptive_crypto/cli.py:43` | Selects legacy or SMC paper state plus the independent holdings sidecar. `--once` starts no notification workers. |
| `adaptive_crypto/runtime.py:24` | Its implicit holdings constructor derives a sidecar from `store.path`. Explicitly inject holdings for SQLite so a second JSON backend cannot appear accidentally. |
| `adaptive_crypto/spot_correction.py` | Offline maintenance opens holdings independently; route it through the same backend selection and locks. |
| Existing tests | Many patch `atomic_json`, read/write JSON paths, or construct another store to simulate restart. Retain those JSON regressions and add real SQLite coverage. Do not make them pass by interpreting a SQLite file as JSON. |

No applicable AGENTS.md was found during this inspection. The worktree already contains extensive staged, unstaged, and untracked user work. Preserve it; do not reset, clean, restage everything, or use a different checkout that omits it.

Observed baseline: `python -B -m unittest discover -s .` passed **390 tests in 12.422 seconds**; `node --test test_gex_ui.js` passed **8 tests**. Python is **3.14.7**, linked SQLite is **3.50.4**. These are pre-change results, not validation of the future backend. README currently supports Python 3.10+.

## 3. Runtime prerequisite

The installed SQLite 3.50.4 predates the WAL-reset fix. SQLite documents a rare corruption race involving multiple connections writing/checkpointing; fixes are in 3.51.3+, and backported to 3.50.7 and 3.44.6. [SQLite WAL-reset bug](https://www.sqlite.org/wal.html#walresetbug)

Before creating or opening the application SQLite backend for writing, check `sqlite3.sqlite_version_info`. Accept `>= (3, 51, 3)`, the `(3, 50, patch >= 7)` branch, or the `(3, 44, patch >= 6)` branch. Reject other older builds with the linked version and a clear upgrade instruction. Reject `sqlite3.threadsafety == 0` as well. Unit-test the version predicate without substituting a fake version for actual database integration tests.

Use an available fixed Python/SQLite runtime for database integration and crash tests. Do not install a global runtime, replace DLLs, silently add an alternate database package, or bypass the gate. If no fixed runtime is available, finish the implementation and tests that can run, record the exact blocked checks, and do not call SQLite cutover ready. Python's version alone does not establish the linked SQLite version.

## 4. Backend interface and ownership

Add `adaptive_crypto/persistence.py`, `adaptive_crypto/state_codec.py`, `adaptive_crypto/state_paths.py`, and `adaptive_crypto/state_lock.py`. Keep the current store classes and their positional arguments. Add a keyword-only injected persistence backend; omitted injection continues to mean JSON for existing Python callers and tests.

Keep `.path` as the logical legacy JSON path; expose `.database_path` separately for SQLite. A SQLite filename must never be fed to code deriving `.positions.json`. Preserve `adaptive_crypto_dashboard.py` exports, including `cli.SingleInstance` through re-export if its implementation moves.

Use a small shared contract: load current document, create initial document, commit a validated replacement, obtain a detached snapshot, export a detached document, and close. Domain initialization, validation, and migrations remain in the domain store modules. The backend owns committed state and serialization, and receives the domain validator. Keep signatures simple; no plugin registry is needed.

`SQLiteDatabase` owns one connection, one `threading.RLock`, and one committed cache per namespace. Give domain stores namespace adapters for `legacy`, `smc`, or `positions`. One process creates one coordinator per physical database; duplicate binding of the same namespace is an error. Do not introduce a process-global cache. SQLite store restart tests close the old coordinator before opening the new one.

Create the shared connection with `check_same_thread=False` and protect **every** connection operation and cache access with the coordinator lock. This flag alone does not provide serialization. Do not use per-thread connections or a connection pool in this version. Use `isolation_level=None`; on Python versions exposing `LEGACY_TRANSACTION_CONTROL`, explicitly select it so future Python defaults cannot change this behavior. Use SQL `BEGIN IMMEDIATE`, `COMMIT`, and `ROLLBACK` consistently. [Python sqlite3 transaction and threading behavior](https://docs.python.org/3/library/sqlite3.html)

Configure and verify WAL outside a transaction, plus `PRAGMA synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`, and `wal_autocheckpoint=1000`. Keep the standard cache and page-size defaults initially. Do not checkpoint every commit or run a periodic checkpoint thread. Only use a local filesystem; reject obvious UNC paths and document the local-disk requirement. WAL supports one writer and does not support databases shared over a network filesystem. [SQLite WAL](https://www.sqlite.org/wal.html)

Move reusable process locking out of `cli.py` to avoid circular imports. Resolve paths before deriving ownership identities. Application and maintenance entrypoints retain the legacy base-path lock, then acquire a lock derived from the canonical database path, always in that order and releasing in reverse. Both JSON and SQLite entrypoints use these locks so backend selection cannot race migration. SQLite direct use acquires database ownership through the coordinator. The database lock also prevents two distinct base suffixes mapping to the same `.sqlite3` from creating independent writers.

Within supported entrypoints only one process owns mutable state. Other processes must not edit the database through ad hoc connections. Transaction revision checking below detects a stale cache if an unsupported writer has changed a namespace; it is not a claim of arbitrary multi-writer application support.

## 5. On-disk representation

Use one database derived from `base_state.with_suffix('.sqlite3')`. Keep all application payload numbers in JSON with their current Python representation; do not round monetary values, convert floats to SQL text manually, or recalculate historical values.

Use schema version 1, separate from the existing domain versions and fingerprints. Set an application-specific `PRAGMA application_id`, and `PRAGMA user_version=1`. Reject an unknown application ID, unsupported schema version, malformed document, or corrupt existing database without resetting it. Creation is explicit; an arbitrary existing empty SQLite file is not permission to initialize it.

```sql
CREATE TABLE namespaces (
    namespace TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    root_json TEXT NOT NULL
);

CREATE TABLE records (
    namespace TEXT NOT NULL REFERENCES namespaces(namespace) ON DELETE CASCADE,
    collection TEXT NOT NULL,
    owner TEXT NOT NULL,
    record_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    payload_json TEXT NOT NULL,
    PRIMARY KEY (namespace, collection, owner, record_key)
);

CREATE TABLE imports (
    namespace TEXT PRIMARY KEY REFERENCES namespaces(namespace),
    source_path TEXT,
    source_sha256 TEXT,
    imported_ms INTEGER NOT NULL,
    manifest_json TEXT NOT NULL
);
```

Represent documents with a deterministic, lossless codec:

| Part | Representation |
| --- | --- |
| Root | Copy all top-level fields. Replace only recognized split containers that are present with empty containers of the same type. Store this skeleton in `root_json`. |
| `assets` | One `asset` record per asset name, owner `''`, key = name. Its payload retains every field, with `trades` and `consumed` replaced by empty lists if present. |
| Each asset's `trades` / `consumed` | One `trade` / `consumed` record per list item; owner = asset name. |
| Top-level `warnings` / `outbox` / `positions` / `buy_watches` | One `warning` / `outbox` / `position` / `buy_watch` record per list item; owner `''`. |
| Top-level `watches` | One `watch` record per dictionary key; owner `''`. |

For every split list use its decimal index as `record_key` and its numeric index as `ordinal`. For dictionaries use the actual string key and its enumeration index. Order lists by numeric ordinal, never by textual key. This preserves list order and legacy duplicate values without inventing identity constraints stricter than the existing validators. Outbox and request uniqueness continue to be enforced by existing validators inside the serialized transaction. Preserve absent optional containers, empty containers, unknown metadata, and nested payload fields exactly in meaning.

Encode using strict JSON (`allow_nan=False`, compact separators, deterministic keys). Reconstruct only known collections; reject duplicate ordinals, gaps in list indices, orphaned asset rows, unexpected collection/owner combinations, and a malformed skeleton. Run the original domain validator after decoding. Do not flatten arbitrary nested measurements or require SQLite JSON extensions.

Compare old/new encoded rows and apply only inserts, updates, and deletes that differ, plus a revision increment. Update `root_json` only if its contents differ. Do not delete/reinsert an entire namespace, write one whole-document blob per transaction, or rewrite all outbox/history rows when one event changes. Array insertion/removal may update shifted indices; normal append and single-record updates must remain bounded in SQL write count.

## 6. Transaction contract

Preserve `snapshot()` and `transaction(operation)` for engines, holdings methods, and dispatchers. All SQLite snapshots are detached copies of committed in-memory state and require no SQL or JSON file reads on the hot path.

Implement each transaction in this order:

1. Acquire the coordinator RLock; reject a closed/poisoned backend and recursive transactions. A callback may request a snapshot, but may not start another mutation or call another store's transaction.
2. Deep-copy the committed document. Call `operation(proposed)` once, synchronously. Callbacks perform no network requests, filesystem writes, or irreversible side effects.
3. Run the existing domain validator. Prepare detached copies of the result and next committed document, and their row encodings, before any commit. A retained callback argument or returned nested object must not mutate the published cache later.
4. If the proposed document equals committed state, return the detached result with no SQL write transaction, no revision change, and no checkpoint. Make legacy JSON `StateStore` use this no-op rule too.
5. Issue `BEGIN IMMEDIATE`. Read the namespace revision and compare it to the cached revision. A mismatch aborts with a clear stale-state error and invalidates the backend; do not overwrite or replay the callback automatically.
6. Apply changed rows, atomically update the revision, then issue SQL `COMMIT`. Never acknowledge success before this completes.
7. Publish the precomputed committed cache and revision by assignment, release the lock, and return the detached result.

Callback, validation, encoding, DML, or definitely-failed commit errors leave both the visible cache and durable state unchanged after successful rollback. Catch exceptions sufficiently broadly to roll back interrupted operations, then re-raise. A rollback failure or uncertain commit outcome poisons the backend until it is reopened and reconciled; do not report an unknown outcome as a successful rollback. Never fall back to writing JSON after a database failure. Use a narrow internal commit seam for failure injection; do not patch `atomic_json` and claim it tests SQLite.

Unify result detachment in all three stores, including legacy StateStore. Keep JSON save-error behavior and existing patch points working. Refactor the initializers into load/normalize/validate/backup/commit stages so SQLite shares the actual business rules instead of duplicating them.

## 7. Startup, historical state, and outbox behavior

Apply startup transformations once per owning runtime, before starting any scanner or sender. Opening a read-only inspector, exporting, or taking a snapshot must not apply restart recovery. Building another adapter within the same runtime must not turn an actively claimed event into an interrupted delivery.

Preserve all existing startup rules:

- Legacy compatible engine upgrades preserve their recorded history and exact original JSON backup. Incompatible legacy JSON follows its existing archive-and-new-ledger policy, with archived bytes and warnings preserved. Database structural corruption always fails closed; it is never handled as a legacy configuration reset.
- SMC strategy-setting changes save a backup and cancel pending limits while preserving active trades and their costs. Notification/GEX selection preferences retain their current exemptions. Preserve pair-change rejection, disabled-asset behavior, and spot-mode restrictions.
- Holdings stay independent of paper settings, retain their schema migrations and metadata, and fail without overwriting malformed records. Spot correction stays an explicit maintenance operation.
- Running Telegram deliveries become uncertain on a real restart; running AI work is requeued where the current store allows it. Existing expiry, fresh-evidence cancellation, terminal statuses, attempts, retry deadlines, and rate-limit handling remain intact.

Retain `dispatch_once`: commit claim (`queued -> running`, attempts increment) before calling the sender; send outside every store/database lock; persist the result in a second transaction. A failed claim calls no sender. A failed result save leaves durable `running` state, recovered under existing restart policy. A network API and SQLite cannot be made one atomic transaction; do not claim exactly-once delivery or automatically retry ambiguous Telegram deliveries.

Backups after cutover must come from committed database documents. Never read a stale JSON sidecar as the current backup source. Logical domain backups can remain JSON via `atomic_json`; physical database backups use `Connection.backup()` under ownership. A failed required backup prevents its associated state mutation.

## 8. Backend selection, migration, and rollback

Add `--state-backend {auto,json,sqlite}` to dashboard and spot-correction CLIs. Default `auto`: an existing application database selects SQLite; otherwise existing base/SMC/holdings JSON selects JSON; a genuinely fresh state selects SQLite. An existing invalid database is an error, never a reason to choose JSON. Explicit JSON mode refuses a base with a database, directing the operator to export to a new base. Explicit SQLite mode with existing JSON but no database requires the import command below. Preserve both launcher forms and `--once` behavior.

Centralize path resolution and store construction. SQLite runtime receives both paper and holdings adapters from the same coordinator. If DashboardRuntime receives a SQLite paper store without explicit holdings, fail with a clear construction error rather than creating a JSON sidecar. JSON callers retain the present implicit holdings behavior. `start_dashboard.ps1` may forward the backend selection but keeps `auto` as default.

Implement `python -B -m adaptive_crypto.state_migration` with `import`, `verify`, `export`, and `backup` subcommands. These are offline maintenance operations and take the same ownership locks as the dashboard. They never start market providers, notification workers, or AI clients.

Import requirements:

1. Resolve the base, settings, active paper source, holdings source, and database paths. Accept `--dry-run` to parse, validate, and report proposed transformations/counts without creating directories, databases, backups, or lock files; require the app stopped and mark a dry run as advisory. The real import acquires locks and repeats all checks.
2. Read selected source bytes once, compute SHA-256, parse BOM-compatible JSON, and run the same pure domain initialization rules. Missing holdings can initialize an empty document; missing paper state can initialize a fresh study. Existing unreadable/malformed selected SMC or holdings files abort the entire import.
3. Import the active paper namespace and holdings together. Leave an inactive legacy/SMC source byte-for-byte untouched; do not reinterpret it with the active strategy's settings. Record which sources were imported or deliberately left inactive. On a later strategy switch, an absent namespace with an existing JSON source requires an explicit import using matching settings; absence of both permits a fresh namespace. Existing database namespaces are never replaced by leftover JSON.
4. Save exact source-byte backups and a manifest in a uniquely named sibling backup directory. Include settings hash, source hashes, versions, counts, selected backend and transformations. Backups precede state publication. Required-backup failure aborts publication. Leftover backup directories are audit evidence, not active state.
5. For a new database, build a uniquely named staging database in the same directory. Import both namespaces, rows, and import markers in one SQL transaction. Validate decoded documents and `PRAGMA integrity_check`, checkpoint the staging WAL with TRUNCATE and require completion, close it, then publish to the previously absent final path while holding ownership locks. Never rename a database that still has required WAL frames. Do not replace an existing target.
6. Failure before publication leaves source files and the final database selection unchanged. Preserve or clean only the tool's verified staging paths. A rerun against a matching completed import reports its provenance without replaying startup recovery or rewinding subsequent database revisions. Changed sources conflict; never merge them automatically. Adding a previously absent namespace to an existing database uses one SQL transaction and leaves existing namespaces unchanged.
7. Verify reports application/schema identity, integrity result, namespace revisions, counts, and import provenance. Post-runtime verification must not demand equality with stale source JSON. Import-time roundtrip equality uses the normalized source, with an explicit list of allowed startup changes.

Export requires a new `--output-dir`, writes coherent namespace snapshots using the original base filenames into a staging directory, and publishes that directory only after all files and a manifest are complete. It preserves outbox statuses verbatim and performs no startup recovery. Missing namespaces are reported, not fabricated. Include a matching settings-file copy when a valid source is supplied. Refuse to overwrite existing exports. After export, rollback runs the JSON backend against the exported base and matching settings; old pre-migration JSON is not a current rollback copy.

Physical backup uses SQLite's backup API, validates the resulting standalone file, and refuses an existing output. Do not copy only the active `.sqlite3` file or manually delete `-wal`/`-shm` files.

## 9. Lifecycle and files to change

| File | Required work |
| --- | --- |
| New persistence/codec/path/lock modules | Connection ownership, schema, lossless codec, incremental commits, error handling and deterministic close. |
| `ledger.py`, `smc_ledger.py`, `positions.py` | Backend injection and shared transaction contract; pure startup normalization; backup source corrections. |
| `cli.py`, `runtime.py`, `spot_correction.py` | Shared factory, selection, locks, explicit holdings injection, shutdown. |
| New `state_migration.py` | Offline import/verify/export/backup commands and dry-run. |
| `notifications.py` | Preserve claim/send/finish; only minimal integration changes if needed. |
| New `test_persistence.py`, `test_state_migration.py`, `test_persistence_integration.py` | Storage, migration, crash and domain parity coverage. |
| New `benchmarks/persistence_benchmark.py` | Reproducible disposable benchmark using real adapters and validators. |
| README, implementation notes, test results, `.gitignore` | Backend operation, prerequisites, exact observed results, runtime database/WAL/SHM/staging/backup exclusions. |

Track owned worker threads. On shutdown signal stop, join workers so no callback/sender completion can use a closed backend, then close the connection and release ownership last. Honor existing network timeout bounds; do not close/release state beneath a surviving writer. Make close idempotent and close partial initialization on error. A best-effort passive shutdown checkpoint is permitted after workers stop; durable committed state must not depend on it. On Windows tests must close handles before TemporaryDirectory cleanup. Normal import of the package creates no database, threads, or workers.

## 10. Acceptance tests and implementation order

Implement these phases sequentially; each is complete only when its relevant checks pass:

1. Characterize existing store behavior and extract pure startup transformations without changing domain outcomes. Retain JSON regressions.
2. Implement codec, database ownership, version gate, schema, transaction and snapshot behavior. Test roundtrip fidelity and fault handling before wiring the runtime.
3. Inject the backend into the three stores and wire factory, CLIs, maintenance, and shutdown. Exercise legacy, SMC, and holdings behavior against SQLite.
4. Implement import, provenance verification, export, and backup. Prove interrupted operations cannot select partial state.
5. Run targeted SQLite tests, full offline suite, benchmark, then document cutover commands and measured limits. Do not perform the production cutover as part of implementation.

Required test matrix:

| Area | Evidence required |
| --- | --- |
| Fidelity | Roundtrip all three documents; preserve optional-key absence, unknown fields, every history element, ordered consumed values (including legacy duplicates), finite numeric values and nested evidence. Reject NaN/Infinity and malformed row layouts. |
| Writes | No-op transactions issue no DML/revision update. Changing one event writes that event and namespace revision only. Appending a history item does not rewrite prior history. Snapshot performs no SQL or JSON read. |
| Isolation | Mutating snapshots, transaction return values, or a retained callback document cannot alter committed state. Recursive mutations fail without partial writes. |
| Rollback | Callback, validator, encoder, row-write, and commit failures preserve prior cache and durable documents after verified rollback. Test poisoned-state behavior separately for uncertain outcomes. |
| Threads | Use barriers/events for simultaneous mutations and competing dispatchers; assert no lost updates, coherent snapshots, one claim per event, and no deadlocks. |
| Processes | A second application/coordinator is rejected for the same DB, including canonical path aliases. Separately exercise bounded SQLite busy behavior using a deliberate external lock in an isolated test. |
| Crash | Terminate a subprocess before commit and after acknowledged commit; reopen from disk and check old/new state respectively. Use IPC synchronization rather than sleep timing. Test committed claim followed by process death: Telegram becomes uncertain and is not sent again. |
| Outbox | Sender observes committed running state through an independent read; failed claim never sends; slow sender holds no persistence lock; rate limits, expiry, restart revalidation and failed acknowledgement persistence retain present semantics. |
| Domain parity | Existing real-formula fixture flows for legacy and SMC entry/fill/exit, positions open/close/request IDs, buy watches, target metadata, spot correction and settings changes produce equal business documents on JSON and SQLite, apart from backup paths. |
| Migration | Legacy upgrade/archive rules, malformed SMC/holdings rejection, inactive-state preservation, byte-exact backups, dry-run no writes, interrupted staging, idempotent import after later DB writes, changed-source conflict, new namespace import, unsupported schema and no fallback. |
| Operations | Export/reimport preserves all stored namespaces and statuses; physical backup is independently readable; runtime never opens JSON holdings with SQLite paper; both launchers, custom state paths, mocked `--once`, and failure/shutdown release resources. |

Keep existing JSON tests that patch `atomic_json` as JSON tests. Add a reusable backend fixture for domain parity; do not duplicate the entire test suite or weaken assertions. Replace direct `.data` mutation only in SQLite fixture setup with validated transactions. Use temporary directories, mocked public-data providers and fake senders throughout. Neither a live `--once` scan nor enabled credentials are part of the test procedure.

Run commands from the repository root using a fixed SQLite runtime where required:

```powershell
python -B -c "import sys, sqlite3; print(sys.version); print(sqlite3.sqlite_version)"
python -B -m unittest test_persistence test_state_migration test_persistence_integration
python -B -m unittest discover -s .
node --test test_gex_ui.js
python -B qualification_audit/check_formulas.py
python -B adaptive_crypto_dashboard.py --help
python -B -m adaptive_crypto --help
python -B -m adaptive_crypto.state_migration --help
git diff --check
```

The three new test modules and maintenance command above do not exist yet. Run them after implementation; do not represent this command list as successful execution.

## 11. Benchmark and cutover evidence

Benchmark the original/current JSON behavior and the new SQLite backend under the same durability setting and fixtures. Capture the JSON baseline before optimizing legacy no-ops; also compare against the final JSON compatibility backend so the gains are attributed correctly. Use small, 1,000-event and 10,000-event histories, warming each case before repeated measurements.

Measure snapshot, unchanged transaction, one changed position/trade, outbox claim+finish, and a mocked multi-asset scan. Include end-to-end validation/copying time, median/p95 latency, SQL rows changed, bytes serialized for changed payloads, and database/WAL growth. Distinguish measured file growth/logical bytes from physical disk write counts. Include checkpoint cost in a complete workload and report startup separately. Compare equal final states. No persistent source ledger or network calls may enter the benchmark.

Deterministic acceptance: unchanged transactions write zero rows; a single event mutation writes a bounded number of rows independent of history length; old history is not rewritten. Performance acceptance: report measured latency and write-volume differences, including small-state regressions and variance. Investigate meaningful regressions; do not weaken FULL durability or present an unmeasured speedup as a result.

After code, offline tests and fixed-runtime verification, provide these operator commands with actual paths substituted. They are future cutover instructions, not permission to stop the running app during implementation:

```powershell
# With the dashboard stopped and a fixed Python/SQLite runtime selected:
python -B -m adaptive_crypto.state_migration import --state .\adaptive_crypto_reclaim_state.json --settings .\adaptive_crypto_settings.json --dry-run
python -B -m adaptive_crypto.state_migration import --state .\adaptive_crypto_reclaim_state.json --settings .\adaptive_crypto_settings.json
python -B -m adaptive_crypto.state_migration verify --state .\adaptive_crypto_reclaim_state.json
# Normal launch now chooses the existing SQLite database:
python -B -m adaptive_crypto --state .\adaptive_crypto_reclaim_state.json --state-backend auto
```

Deliver a concise result stating changed files, test counts, benchmark results, any runtime blocker, and exact rollback/export commands. Keep the production ledgers and active settings untouched during implementation.
