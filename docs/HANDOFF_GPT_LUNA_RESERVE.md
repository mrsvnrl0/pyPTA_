# Implementation handoff: GPT-Luna Reserve

Requested target: **GPT-Luna Reserve**. Retain **High** reasoning from the original request if that target exposes the setting. This document replaces the earlier GPT-5.6 Solar target. It is a prompt, not a model-selection command; it does not change the current session or claim that “Reserve” is an API model ID.

Select the requested model in the app and give it the following prompt, with this checkout available. The architectural contract defines the intended implementation; a prompt cannot guarantee identical code from different models.

---

Implement the SQLite persistence migration in this repository to completion. The repository is `C:\Users\CxN\Documents\ChatGPT\pyACCS`.

First read `docs/PERSISTENCE_SQLITE_PLAN.md`. Treat it as the acceptance contract. It contains the inspected call sites, chosen schema and codec, transaction algorithm, backend selection rules, migration/rollback behavior, and required tests. Follow those decisions; if current source contradicts an assumption, make the smallest necessary adjustment and document the evidence and its effect on the contract.

The required result is a working SQLite WAL backend for the legacy StateStore, SMCStore, and PositionStore, wired into both launchers and offline maintenance, with migration/export/backup tools and verified behavioral parity. Keep JSON compatibility during explicit cutover. This is implementation work, not a request to produce another plan.

The following decisions are fixed:

- Use the standard `sqlite3` interface, WAL, `synchronous=FULL`, a single owned connection protected by one RLock, and committed in-memory snapshots.
- Preserve the existing `snapshot()` / `transaction(operation)` domain interface and existing Python constructor compatibility. Domain validation and startup recovery stay shared between backends.
- Use the plan's lossless root/record codec and write only changed rows. Preserve all list order, optional fields, history, IDs, formulas, targets, and numeric meaning. A no-op writes nothing.
- Commit every state transition and its outbox changes together before publishing memory. Prepare detached result/cache copies before commit. Failed commits do not publish proposed state; uncertain outcomes invalidate the backend.
- Preserve the claim/send/finish delivery sequence. No sender runs under a database lock. Telegram uncertainty must never become an automatic resend.
- Keep the process ownership lock and database ownership guard. Do not introduce arbitrary multi-process writers, Redis, an ORM, deferred durability, a background save queue, or unrelated refactors.
- Import explicitly and atomically, retain exact source backups and provenance, and export current database state for rollback. Never use stale JSON as live state after SQLite selection.

The installed runtime observed during planning was Python 3.14.7 linked to SQLite 3.50.4. It fails the required WAL-fix version gate. Implement the gate and use an available fixed runtime for actual SQLite integration tests; do not fake the runtime version to make tests pass, replace system DLLs, or bypass the gate. If that runtime is unavailable, complete all independent implementation and verification, then identify the exact remaining runtime-dependent checks without claiming cutover readiness.

Work through the plan's five phases. Use the existing JSON tests as regressions and reusable fixtures for additional SQLite tests. Add failure injection at the actual database boundary, deterministic concurrency/crash tests, migration/rollback tests, and end-to-end store/dispatcher integration. Tests that only patch `atomic_json` do not verify SQLite. Keep all tests offline and use disposable state.

Run the plan's validation commands after the relevant modules exist. At planning time the baseline was 390 passing Python tests and 8 passing browser-script tests. Re-establish that baseline if source has changed, report the actual final counts, and distinguish existing failures from regressions. Measure the benchmark; do not infer a speedup from WAL alone or trade away durability to improve results.

The checkout contains substantial pre-existing user changes. Work in place and preserve them. Do not reset or clean the repository, replace work with HEAD, stage unrelated files, or commit/push unless separately requested. Keep implementation changes limited to the persistence integration and its tests/docs.

Implement and verify autonomously within that scope. Do not start a live dashboard, stop/restart an existing process, migrate production ledgers, alter active settings, send Telegram messages, make paid AI calls, or run live market scans as part of implementation. Prepare concrete cutover and rollback commands for the operator. Do not create another task or delegate to another model; this handoff is intended for the selected GPT-Luna Reserve session.

Before finishing, inspect the diff against these specific failure modes: full-ledger rewrites hidden in a SQL blob; writes on no-ops; mutable result aliases; stale-cache overwrite; startup recovery triggered by ordinary reads; JSON holdings paired with SQLite paper state; partial migration publication; backup of stale JSON; claim sent before commit; sender holding a lock; and database closure while a writer is still active.

Completion requires the implemented code and maintenance commands, passing relevant tests on a fixed SQLite runtime, measured benchmark evidence, and documented cutover/rollback instructions. If an external prerequisite prevents any of these, state exactly what is implemented and what remains unverified. Do not label skipped database tests as passed.

Final response: explain the resulting behavior, list the small set of changed modules, report actual test/benchmark results, and give exact cutover and rollback commands. Mention any runtime blocker plainly. Avoid a long activity log.

---

The prompt separates concrete outcome, constraints and evidence instead of asking the model to imitate another model's reasoning. This follows the emphasis on clear outcomes and representative validation in [OpenAI's GPT-5.6 prompting guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6#prompting-best-practices).
