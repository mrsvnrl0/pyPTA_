**NN recorded-position exit lights — 15 September 2026**

Recorded positions with NN selected now show STOP LOSS, HOLD and TAKE PROFIT. An adverse classification (SELL for a long, BUY for a short) selects the red STOP LOSS light; a supportive or neutral classification selects HOLD. Reaching the saved numerical target selects the green TAKE PROFIT light and takes priority. Position alert labels include the exit reason and retain SELL TO EXIT LONG or BUY TO COVER SHORT. These are exit alerts, with no new stop price or automatic close. Existing freshness checks, notification preferences, target calculations and automatic paper BUY/HOLD/SELL execution are preserved.

Verification: 35 focused Python tests and 5 browser-script tests passed, covering both position sides, target precedence, absent targets, invalid or expired readings, alert wording and deduplication, manual ledger preservation, NN-only rendering, default dashboard lights and automatic paper execution. Verification used disposable state and did not restart the dashboard or dispatch external alerts. Relaunch the dashboard to load the updated Python alert labels.

---

**Single complete backup — 14 September 2026**

All application backup triggers now replace `backup/latest.zip` beside the logical state base (the project root with the default launch settings). The ZIP combines saved settings, all current SQLite namespaces or existing JSON sidecars, active configuration contexts when available, and a manifest with hashes. Imports and startup archives preserve original source bytes. SQLite copies use the online backup API, including committed WAL data. New archives are flushed and verified before atomic replacement; failures abort the associated operation and preserve the previous archive. Historical backup files are retained, and explicit exports remain separate.

The offline backup command accepts JSON and SQLite state, defaults to the single archive, and accepts a custom `--settings` path. The settings page explains replacement behavior. The new backup module is used by settings saves, model selection, settings application, purge, startup normalization, imports and spot correction. Trading formulas and execution rules are unchanged by this update.

Verification: **529 offline Python tests passed in 25.748 seconds**, with no skips. Coverage includes all strategy namespaces, SQLite restore and provenance, committed WAL data, exact source bytes, repeated replacement, custom filenames, and write/verification/replacement failures that leave settings and trading data intact. Maintenance CLI help and diff whitespace checks passed. Production data was not backed up or modified during verification; stop and relaunch the dashboard process to load these Python changes.

---

**Selectable neural-network strategy — 12 September 2026**

Completed the interrupted Parente 5/2 implementation as a `neural_network` strategy selected from dashboard settings. Added local inference using the authors' converted weights, 36 causal 4H features, independent JSON/SQLite paper state, BUY/HOLD/SELL execution, fixed stops, saved costs, durable notifications, and offline model conversion/backtesting. Model selection is backed up and takes effect after restart; existing holdings keep their SMC monitors. Active production settings remain `smc_video`.

Resolved the settings-reload import, strengthened neural accounting and chronology validation, required current stop-monitoring candles for entries, preserved legacy fingerprint compatibility, and corrected archive provenance to Figshare version 2. Feature measurements match three archived BTC reference rows within 5.56e-16; converted network probabilities match independently summed H5 weights within 2.99e-7. Normalization stays frozen after historical calibration, so this implementation does not claim the research paper's returns.

Verification: **496 offline Python tests passed in 22.587 seconds**, including 31 neural tests, with no skips on Python 3.12.14 / SQLite 3.53.1. **8 browser-script tests passed**. Desktop and 390px browser previews verified model selection and neural trade rendering without horizontal overflow or console errors. Model conversion, synthetic CSV backtesting, launcher/tool help, JavaScript syntax and whitespace checks passed. Tests used disposable state; the production dashboard was not restarted and no Telegram or paid AI call was sent. See `docs/NEURAL_STRATEGY.md` for activation, exact formulas, model provenance and commands.

---

**SQLite persistence implementation — 11 September 2026**

Implemented a shared document-store contract with an incremental SQLite WAL backend for legacy paper state, SMC paper state and holdings. The backend keeps committed snapshots in memory, uses FULL durability, applies only changed records, skips no-op writes, detaches callback results/arguments, and preserves per-store atomic state/outbox commits. Existing direct Python callers retain JSON compatibility. Saved validation contexts are committed with state to distinguish damaged records from intentional settings changes and to support accurate export.

The dashboard and maintenance tools share canonical ownership locks and backend selection. Fresh databases and imports are staged, verified and checkpointed before publication. Offline import, verify, export and backup commands retain source provenance and avoid stale-JSON fallback. Startup recovery and explicit spot correction use current database snapshots for backups. Shutdown joins scanner and notification threads before closing state. Generic SMC events with a null trade payload now survive startup.

Verification: **431 offline Python tests passed in 15.836 seconds**, including 41 new codec, transaction, thread/process, crash, migration, recovery and integration tests. **8 Node browser-script tests passed**. The formula audit, both launcher help paths, maintenance help paths, PowerShell syntax and diff whitespace checks passed. Tests used the local environment with Python 3.12.14 and SQLite 3.53.1; system Python/SQLite was not replaced.

Seven-repetition benchmarks produced equal final business documents on original JSON, current JSON and SQLite. Single-event commit medians were 1.48x–2.79x faster than original JSON over 10/1,000/10,000-event histories. SQLite changed two rows at every size; at 10,000 events its 339-byte event payload replaced a 6,807,925-byte whole-JSON rewrite (logical bytes, excluding SQLite page/checkpoint overhead). Snapshot time did not improve and full-document validation/copying remain CPU costs. Current JSON changed commits incur extra copy cost to preserve alias isolation. Details and full timings: docs/SQLITE_PERSISTENCE.md and docs/persistence_benchmark.json.

Implementation left the running dashboard, production ledgers and active settings untouched. The operating guide provides explicit cutover and export-based rollback commands. start_dashboard.ps1 prefers the fixed local .venv when present. No live market scan, Telegram send or paid AI request was used for verification.

---

**GEX mapping and position alert refinements â€” 7 September 2026**

The Naive GEX map now displays signed percentage distances to Kraken price, the nearest order-block edge, and the Deribitâ€“Kraken index basis. Price-axis labels and spaced wall labels make nearby levels readable; a collapsible gamma curve shows the existing model's detected sign changes. Options, quotes and SMC evidence have separate browser expiry deadlines, so a slow fetch or a paused page cannot keep stale confluence visible. Fresh Kraken prices remain available during an SMC candle outage. Duplicate options records reject the snapshot; malformed or future blocks are isolated to the affected side. The GEX formula, paper qualification and recorded trade levels are unchanged.

Undelivered momentum alerts now refresh only while the originating signal remains the latest completed candle under the same formula. Restart or feed/clock loss cancels pending delivery until evidence is revalidated. Formula changes and newer/neutral candles retire previous actions. Refresh preserves identity, attempts and Telegram retry deadlines; sent, failed and uncertain events remain terminal. Quoted signals expire at the earlier of quote expiry or the next candle close. Supporting momentum cannot issue HOLD at a saved target that is currently reached.

Historical target touches consistently carry REVIEW EXIT in message and API payload. Alert cards use the latest observation time, show cancellations directly, and expire live-price labels during form editing. Holding marks share the executable-quote validator. Targets and holdings remain manually closed, with their original levels preserved.

Verification: **350 offline Python tests passed** in 7.608 seconds, including 13 new regressions, and **4 Node browser-behavior tests passed**. JavaScript syntax and tracked-file whitespace checks pass. A disposable browser preview verified long advance warnings, short target hits, historical review labels, GEX distances, both mapped blocks and the gamma curve. A 390-pixel viewport had no horizontal page overflow; no JavaScript console errors were observed. Verification used temporary ledgers and synthetic market data, with network requests blocked in the preview and no Telegram/AI workers. The running dashboard requires a restart to load the Python changes.

**Telegram take-profit proximity alerts â€” 7 September 2026**

The selected warning range is 0.10% of target (`smc_tp_alert_bps: 10.0`) for both active SMC paper trades and recorded external holdings. The saved take-profit is compared with a fresh executable bid for longs or ask for shorts after exit processing. At/beyond-target observations cannot create a late approaching warning. The existing Telegram outbox records the asset, side, quote, target, distance and underlying liquidity; no AI request is created for these warnings.

Recorded holdings select one untaken confirmed setup-timeframe opposing swing beyond entry and current price, using the existing liquidity and sweep-buffer formulas. Selection needs current setup/entry feeds and a fresh quote. The saved target survives restart and settings changes. Completed-candle or live target reaches mark that target reached without closing the holding or automatically selecting another. Partial selection-candle wicks cannot establish a later reach; their completed close can. Quotes can continue monitoring an already saved target during feed outages.

Per-trade/holding identities prevent repeat delivery. Undelivered alerts cancel on closure, target reach, stale data, disablement or departure from the alert range. Quote-based expiry is checked atomically at dispatch, and restart requires a fresh observation. Fresh evidence can requeue an undelivered cancelled warning while preserving Telegram rate-limit retry deadlines; sent/failed/uncertain events remain terminal. Notification-only setting changes preserve pending paper orders and balances. Holding target metadata is validated on load without overwriting malformed files.

Verification: **307 offline tests passed in 6.559 seconds**, including 19 new tests for both sides, the inclusive 0.10% boundary, executable quote sides, feed/clock failures, pending/closed trades, cancellation, expiry, rollback, settings migration, frozen holding targets, target reach chronology, restart, malformed records, runtime/HTML integration and mocked Telegram acknowledgement/rate-limit handling. JavaScript syntax checking passes. Verification sent no Telegram or AI requests and used disposable ledgers.

**Sweep take-profit update â€” 7 September 2026 â€” smc-video-v1**

Completed the follow-up request to take profit on a sweep beyond the nearest untaken opposing 30M liquidity swing. Long take-profit is the key high multiplied by `1 + smc_tp_sweep_buffer_bps / 10000`; short take-profit is the key low multiplied by `1 - smc_tp_sweep_buffer_bps / 10000`. The independent default buffer is 1 bp (0.01%). Filled paper trades exit at that price without a reversal confirmation. The selected swing and buffer remain visible in qualification evidence, order/trade cards and limit notifications.

Before entry, freshness and cancellation still use the unextended liquidity level. A live observation there retires an unplaced setup or cancels its pending limit. A completed candle touching both that level and the pending midpoint cannot produce a backfilled profit. After entry, touching the key level alone does not close the trade; target exits, modeled reward and P/L use the extended price. Stop precedence for ambiguous bars remains in force.

New records save the original liquidity price and buffer and validate their relationship to the target. Restart retains those values. Adopting or changing the buffer backs up the study and cancels pending limits through the existing settings-change path; filled trades retain all recorded levels and costs. Older filled records without the new fields continue to load without extending their targets. The source mapping identifies the user's follow-up and separates the chosen numerical extension from the video's rules.

Verification: **288 offline tests passed** in 5.922 seconds, including 10 dedicated sweep-target regressions covering both sides, configurable formulas and units, raw-level retirement/cancellation, same-bar ambiguity, quote and completed-wick exits, P/L, restart deduplication, settings changes, older records and damaged-record rejection. Existing HTML integration checks pass. The standalone legacy qualification audit and `git diff --check` also pass. Verification used disposable ledgers and sent no Telegram messages or AI requests.

**Video strategy adaptation â€” 7 September 2026 â€” smc-video-v1**

The active settings now select the original Smart Risk video's 30M setup / 5M entry combination and both entry methods. Long and short qualification share mirrored sweep, return, structure-break, originating order-block and first-revisit rules. Conservative entry requires a lower-timeframe structure shift plus a reversal-leg FVG; aggressive entry requires inversion of an opposing FVG, with simultaneous MSS optional. Entries use gap-midpoint limits and one nearest untaken opposing 30M liquidity target. The selected stop is beyond the 5M reversal extreme, with a configurable initial 1 bp (0.01%) buffer. The original captions and chart frames were reviewed; `video_strategy_audit/rules.md` maps source timestamps and separates numerical automation choices from narrated rules.

The SMC model replaces the prior ATR sweep-depth gate, ROC/EMA entry, RSI/RVOL/body filters, fixed-R partial targets and trailing stop. Existing execution costs and portfolio-risk constraints remain, with fees on both sides and stop slippage; liquidity targets cannot be moved to manufacture net R. `adaptive_crypto_settings.pre-smc.json` preserves the previous settings. The new `.smc.json` study ledger keeps prior paper history separate, while existing `.positions.json` holdings and alerts are preserved. Legacy fingerprints remain compatible. Restart the running dashboard to load the new implementation.

SMC limits are recorded before prospective fills. Qualification and order creation share exact evidence. Live missed-midpoint/consumed-target observations retire the setup across quote recovery and restart. Completed setup-timeframe invalidation cancels pending orders after honoring earlier verified fills. Missing history, partial entry candles, clock/feed failures, same-bar ambiguity, collateral accounting for shorts, portfolio limits and atomic rollback have explicit handling. Settings changes back up the SMC ledger, clear pending limits and preserve active trade levels and costs.

Recorded holdings now monitor completed 5M structural direction. A changed strategy/timeframe quietly rebaselines; later transitions into bullish or bearish generate persistent per-asset/candle alerts for open holdings. Saved direction survives the originating break leaving the rolling feed. A 30M feed failure does not suppress a healthy 5M holding monitor. The chart, qualification cards, pending-order levels, trade exits and alert explanations show the selected timeframes and formulas.

Verification: **278 offline tests passed** in 5.136 seconds: the prior 188 plus 39 formula tests, 39 order/accounting tests and 12 integration tests. Coverage includes mirrored real-formula scans through pending limit and fill, source chronology, both FVG methods, fresh first visits, liquidity consumed inside a forming 30M candle, stop math, holdings migration/restart and chart/HTML rendering. The standalone legacy qualification audit and `git diff --check` pass. A disposable browser preview verified long/short pending limits, active trade levels, sweep evidence, both holding-alert directions, open/close forms and closed P/L, with no JavaScript console errors. A public-data `--once` scan against an isolated ledger returned valid 30M/5M feeds for BTC, ETH and SOL with no feed errors and no new qualifying entries. That snapshot is an integration check, not a profitability validation. No production ledger was opened by verification and no Telegram message or AI request was sent.

**Earlier implementation history (legacy model below)**

**Recorded holdings and 15M momentum alerts â€” 7 September 2026**

The dashboard now records external long and short holdings with asset, entry price, quantity and manual exit price. The holding ledger is an independent `.positions.json` sidecar, unaffected by paper-ledger fingerprints, cash or strategy-setting resets. Gross P/L uses fresh bid/ask marks with an explicitly timestamped completed-15M fallback. Closed records retain the entered exit price.

The existing momentum strategy and holding monitor share ROC, EMA, RSI and ATR calculations. Holding direction uses completed 15M candles: bullish when ROC exceeds zero and its signal, bearish when ROC is below both, neutral otherwise. Entry qualification gates remain separate. The initial reading and a changed ROC/signal formula establish quiet baselines. Later transitions into bullish or bearish queue one persistent alert per asset/candle, containing the associated open holdings and P/L. Neutral transitions do not alert.

Holdings, candle progress and alerts commit atomically. Duplicate form submissions are idempotent across restarts. Closing holdings cancels queued alerts with no remaining associated open holding; the last close removes the asset watch. Restart replay processes available complete history in order; unavailable history establishes a new baseline and reports the gap. Invalid/stale/forming data and clock failures cannot advance alert progress. Holding monitoring continues through a separate 4H strategy-feed failure. Damaged holding files fail without overwrite, and interrupted deliveries become uncertain instead of being automatically resent.

The dashboard alert feed uses the existing durable Telegram dispatcher in a separate worker. Desktop notifications are optional and require browser permission and an open page. Position forms use session-bound CSRF tokens, finite positive values and request identities. Automatic reloads pause while the user edits a form; background monitoring continues.

Verification: **188 offline tests passed**, including 27 new holding and route regressions. The standalone qualification audit also passes. Browser checks against a disposable offline preview confirmed opening/closing a holding, correct P/L and closed history, alert rendering, form preservation during refresh, and no JavaScript console errors. `git diff --check` passes. No real holding was added, no live ledger/settings were changed, and no Telegram message or AI request was sent. The running production dashboard requires a restart to load the implementation.

**Qualification consistency fixes â€” 6 September 2026**

- Reclaim evaluation skips candidates without the configured number of preceding structure candles. A short current history produces an explicit waiting check instead of an empty-slice exception.
- The 15M price comparisons and their evidence now share one evaluator. Failed candidates retain their OHLC, bullish price change, close location and required structure high. A failure candle explicitly indicates that it cannot confirm its own replacement break.
- Every displayed break reports the age and opening timestamp of its associated retest. A newer retest is labelled when it needs its own break; it cannot hide an expired retest belonging to an older trigger.
- Momentum exposes the existing entry-age gate in minutes, using the same millisecond comparison that controls entry. Historical entry evidence is retained for active trades, including after restart.

Verification: 161 offline tests pass, including the 15 new consistency regressions and two HTML-rendering regressions. The standalone audit arithmetic checks and all four corrected scenarios pass; results are in `qualification_audit/verification.json`. `git diff --check` passes. The settings and live ledger were not edited, and no background dashboard or notification worker was started. The existing ledger format and engine fingerprint are retained; evidence dictionaries remain compatible.

**Code review implementation â€” engine kraken-closed-bar-v9.3**

The four reviewed defects now have offline regression coverage:

- An earlier 4H invalidation closes the position before later 15M target observations.
- A full post-entry 4H candle can establish a stop breach when 15M coverage is incomplete. Fallback suppresses overlapping unreplayed 15M fragments and uses stop precedence for ambiguous bars. Complete 15M history preserves its known sequence. Replay timestamps prevent duplicate accounting after recovery or restart.
- Reclaim selection examines every eligible candle and selects the newest qualification, including a later reclaim sharing the same sweep.
- Chart failures consult current fallback data on every retry. Quote loss or age beyond 45 seconds produces a stale chart; a cached failure cannot hide subsequently available fallback data.

Stop changes now carry effective timestamps. Aggregate candle lows are checked against the stop effective at the candle open, avoiding a retrospective breach of a later trailing stop. Existing ledgers without stop history use their initial stop for older bars and flag uncertainty when the timing of a changed stop was not recorded. Aggregate candles cannot reconstruct intrabar order or recalculate already saved outcomes.

The implementation is now in the `adaptive_crypto` package, with separate core, strategy, engine, ledger, market-data, notification, runtime, web, and CLI modules. HTML, CSS, and JavaScript are separate assets. The original launcher and function/class imports remain available. Dependency mocks now target their owning modules.

Compatible v9.1 and v9.2 ledgers are backed up before upgrading to v9.3. Cash, historical trades, targets, consumed identities, and delivery records are retained. v9.2 pending confirmation resets and momentum watches survive; v9.1 pending setups are cleared as in the previous upgrade. Invalid state remains subject to the existing archive policy. No saved runtime ledger was opened or modified during implementation.

Verification includes the existing 117 tests plus regression and integration checks for the fixes, upgrade preservation and rollback, package assets, and the isolated `--once` launcher. Both launcher forms accept `--help`. The final observed test count is recorded in `TEST_RESULTS.txt`. All verification is offline; no public-data scan, Telegram message, paid AI call, or background dashboard was started.

---

**Previous qualification update â€” engine kraken-closed-bar-v9.2**

This update fixes all three defects from the qualification audit and applies all three stricter strategy rules requested afterward.

| Finding | Implemented behavior |
|---|---|
| Newly discovered setup survived a live stop breach | One terminal-validation function checks both saved and newly discovered reclaim setups. Retirement, consumed identity and state commit together. |
| 15M confirmation could outrun the latest 4H invalidation | Entry requires the expected latest completed candle in each required feed. Available validated candles remain usable for exits and setup cancellation during the boundary delay. |
| A consumed key hid a distinct later reclaim | The candidate loop skips that consumed identity and continues to later reclaim candles. |
| Older pending structure survived a newer reclaim | A fresh structural scan can supersede the pending setup. All replacement levels and evidence come from the new identity; only candles after its reclaim close count for confirmation. |
| Failed 15M breaks remained usable | A completed close at/below the break clears the trigger and previous retest. The failure candle may supply a new touch but cannot confirm a replacement break. A live bid at/below the break persists a reset time; only full candles starting after that observation can rebuild confirmation. |
| Momentum could qualify after a post-signal stop excursion | A qualified crossover is watched while entry is pending. Observed fresh-bid or completed post-signal candle breaches consume its identity, even during a 4H outage. Recovery and restart cannot revive it. |

A persisted lower bound on reclaim timestamps also prevents fallback after a superseded setup fails. Retirement excludes every block interpretation of that same reclaim candle; a different key cannot resurrect the old zone. A distinct later reclaim candle remains eligible, subject to all current checks.

Momentum continues to work without a 15M feed. Its stricter invalidation uses observed quotes and whatever valid completed post-confirmation candles are available. It cannot prove that no excursion happened during an unobserved gap. This is an evidence limit, not permission to ignore a breach that is present in the supplied data.

The configured indicator thresholds, position sizing, fees, target multiples and timing limits are unchanged. The new strategy rules can reduce or change qualifying trades. Entries still calculate targets from the observed quote and the current selected stop; active positions retain their original targets and historical evidence.

**State upgrade**

The engine fingerprint changes from v9.1 to v9.2. A compatible, valid v9.1 ledger is backed up before its first upgraded write. The upgrade preserves cash, realized P/L, trades, trade levels, consumed signal keys and queued delivery records. Historical trades are tagged with their original engine. Pending setups are cleared for a fresh evaluation under the revised rules. Retired reclaim chronology is retained.

The upgrade only accepts the matching v9.1 fingerprint for the same assets and settings. Malformed state and configuration changes still trigger the existing archive-and-start-fresh handling. Upgrade write failures leave the original ledger intact. Reopening an upgraded ledger does not repeat the migration.

**Verification**

The offline suite covers the original 93 tests plus qualification safety, supersession, failed-break recovery, momentum invalidation, and migration regressions. It verifies that invalid entries produce neither trades nor queued signal notifications, that later genuinely fresh confirmations can qualify, and that boundary waiting does not stop exit monitoring. `TEST_RESULTS.txt` records the final observed counts.

No live market-data scan, exchange order, Telegram message or paid AI request was used to validate this change. No background dashboard or notification worker was started during installation.

GEX target confluence is now enabled in the active settings. Shared prospective target selection covers paper exits, holding targets and automatic lower buy watches; recorded prices remain fixed. Alignment is 10 bps, maximum venue basis 50 bps, and the existing sweep extension remains 5 bps. See gex_target_audit/FORMULA.md and verification.json. All 383 Python tests and 5 browser-script tests passed; the three-column chart table was visually checked on the running dashboard.


## Current application roles — 20 September 2026

The application startup normalizes legacy saved model selections to NN-only paper trading, while low-level historical readers remain compatible. Home renders read-only market analysis and never consumes paper order status. NN probabilities use completed 4H candles; NN protective-stop history uses fixed 5M candles, independent of SMC configuration.

Manual positions retain their recorded trades and gain separate nn_guidance and position_targets fields. TP1 is nearest SMC liquidity; TP2 requires farther GEX-supported liquidity and remains pending when unavailable. An editable saved stop supplies both target guides. Historical target fields remain intact for compatibility; new target provenance is retained in each target box and closed-trade record. No guide automatically closes a manual holding.

The application no longer offers Legacy, combined or SMC simulation model choices. Their records are retained, with inactive open paper records visible in Trade Records. See README.md for the current user workflow. Earlier sections below/above describing selectable paper strategies are historical.

## 2026-09-20: separate 4H structure and position alerts

Added structure_context.py for strict closed-candle 2-left/2-right 4H pivots, protected highs/lows, close-only breaches and potential wick sweeps. The bounded state is persisted in the positions namespace as market_structure, separately from the existing SMC entry-timeframe readings. The protected level is the opposite wick extreme between the broken pivot and its break candle, including that break candle. Minor pivots alone do not replace it. Rolling data preserves saved levels; missing continuity rebuilds a baseline without exit alerts.

Home shows the latest swing prices and pivot/confirmation timestamps, protected levels and status, last BOS, protected breach and sweeps. Collapsible bullish/bearish qualification lists include explicitly labelled 4H context checks; these do not gate SMC entry logic or NN paper trades. Positions show the side-appropriate protected level separately from saved stops and existing three guides.

position_structure.py replaces active generic position_momentum delivery with position_structure alerts: only an adverse completed 4H protected-level breach, tied to an eligible open manual position whose tracking/alert-enable time predates the close. Position alerts identify entry, protected level and confirming close, use a candle reference rather than a live executable quote, expire after five minutes (or the next 4H close), and never close records. First runtime scan establishes a baseline; each breach is queued once; closed/off positions cancel delivery. Existing Structure / NN preference governs the new alert type. NN, TP and saved stop guidance remain independent.

The old lower-timeframe monitor still populates display momentum with notify=False in the active runtime. Its prior notifications are retired; historical records remain. No strategy settings or position prices were changed.

## 2026-09-22: NN implementation audit and fixes

NeuralEngine.read now supplies the active NN model/cache to Home, paper execution and position guidance. Models reload together on apply/restart; displayed predictions cannot hot-reload separately. Runtime runs all assets' required NN/stop work before advisory SMC requests in a scan and refreshes executable quotes/clock after inputs and inference.

Independent position NN alerts now retain notification state across missing quotes/model readings and revalidate current undelivered exits when inputs recover. Supporting signals can reset that baseline without an executable quote. Delivered, uncertain, disabled and superseded events are not replayed. Offline backtest reports mark intrabar stop timestamps as candle-close confirmation with unknown exact fill time.

Validated with 599 Python and 15 JavaScript tests. Live activation preserved all 8 manual records and the existing neural paper trade. All three assets returned identical Home/paper predictions and no runtime errors. Full audit: docs/NN_AUDIT_2026-09-22.md.
