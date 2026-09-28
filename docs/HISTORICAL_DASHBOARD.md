# Historical documentation

Superseded application behavior. See the project README for the current separation of market analysis, NN simulation and positions.

# Adaptive Crypto dashboard

The active **smc-video-v1** strategy combines the pullback methods from [Smart Risk's liquidity sweep, order block and FVG video](https://www.youtube.com/watch?v=RjR2kTErlq4) with your optional **sweep-reclaim breakout entry**. Your selected settings are **30M setup / 5M entry**, both pullback methods, breakout entries enabled, and stops beyond the reversal swing. It uses public Kraken spot data for BTC/USD, ETH/USD and SOL/USD. It records paper scenarios and external holdings; it submits no exchange orders.

All assets use one shared strategy configuration and the same formulas for qualification, entries, stops, liquidity targets, momentum, fees and risk. Each recorded position and BUY watch can choose **SMC** or **SMC + GEX**, with identical behavior across assets. Asset-specific price decimals affect display only; market data, available liquidity and portfolio capacity can produce different results. Formula updates do not migrate saved positions or watches; reset or re-add them yourself after restarting the updated dashboard.

See [the GEX target formula](gex_target_audit/FORMULA.md) for alignment rules, active settings, freshness gates and fallback behavior.

The **Naive GEX × SMC** panel adds Deribit options context for BTC, ETH and SOL: signed exposure by strike, maximum-positive call wall, minimum-negative put wall, estimated gamma flip, and exact overlap with current higher-timeframe order blocks. The map uses actual price ordering; a call wall below price is not labeled an overhead ceiling. `smc_gex_targets` controls the paper strategy's GEX preference. Recorded positions and SHORT buy watches each save their own target method, defaulting to SMC, independently of that setting. Existing recorded prices remain fixed.

Wall and flip cards show their percentage distance above or below the fresh Kraken price. Order blocks show whether price is inside the block or how far it is from the nearest edge. The map includes a price axis, separated labels for nearby levels, and distinct reference-block styling. Expand **Gamma across index prices** to inspect the modeled curve and its detected zero crossings. The displayed Deribit–Kraken basis keeps differences between the two venues visible; levels retain their original prices.

The model uses zero-rate Black–Scholes gamma from mark IV, with calls positive and puts negative: `GEX = sign × gamma × OI_base × index² × 0.01`. Deribit option open interest is already in base coins, so no contract-size multiplier is applied. Walls use **net** exposure per strike, summed over all active expiries. The flip reprices the complete chain at fixed IV and OI over ±30% of its index (0.5% grid, then bisection at sign changes); it is not a cumulative sum across strikes. The nearest detected root is displayed and multiple/no-root cases are explicit. Very narrow crossings between grid points may be missed.

BTC/ETH coverage is coin-settled Deribit options; SOL is USDC-settled options filtered to SOL. SOL values are in USDC, approximately mapped to Kraken USD prices; currency and venue basis differences apply. Options refresh independently in the background while paper GEX target selection is enabled or a new personal SMC + GEX target needs selection, and on demand for the chart, with a shared 120-second cache. Missing or invalid positive-OI data makes the snapshot unavailable rather than publishing a partial profile. Cached failure results retain their original timestamp and are marked stale; age over five minutes or an included expiry also marks the profile stale. Confluence requires healthy SMC/quote data no older than 45 seconds and a current options snapshot. Options failures cannot stall the signal scanner. Read measurements at `/api/gex/BTC`, `/api/gex/ETH`, or `/api/gex/SOL`.

Missed, expired, retired, or history-incomplete SMC setups appear as dashed reference blocks and cannot receive fresh confluence labels. This is an open-interest positioning proxy, not observed dealer inventory or institutional order flow. Positive/negative gamma regime descriptions are conditional on the assumed signs. The exact model and coverage appear in each panel. Data definitions: [Deribit option summaries](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency) and [instrument metadata](https://docs.deribit.com/api-reference/market-data/public-get_instrument).

The browser checks expiry every second, including while a slow request is pending or form editing pauses reloads. Expired quotes remove live distances; expired SMC evidence removes blocks; expired options remove current confluence and mark retained levels as historical. Healthy Kraken prices can still show wall distances during an SMC candle outage. Invalid quotes and future or malformed blocks cannot create confluence, and duplicate options records reject the snapshot instead of double-counting exposure.

Run in PowerShell from this directory, using Python 3.10 or newer:

```powershell
python -m pip install -r requirements.txt
python .\adaptive_crypto_dashboard.py
```

Open [the local dashboard](http://127.0.0.1:5000/). `start_dashboard.ps1` launches the same application. Restart an already running dashboard to load changes. Install the launcher and the complete `adaptive_crypto` directory, including templates and static files.

Inside **Active strategy**, **Apply settings from JSON** validates and reloads the file selected by `--settings`, including enabled assets, SMC timeframes, costs and refresh frequency. Existing recorded positions keep their saved levels and costs. Changed strategy rules cancel pending paper limits, and queued messages are retired until fresh evidence arrives. Invalid or missing JSON leaves the running configuration unchanged. Switching `strategy_model` requires a restart.

**Purge database… → Back up and purge** saves a backup beside the active state file, then clears paper orders/trades, recorded holdings, buy watches, alerts and history. Monitoring pauses until **Apply settings from JSON** is clicked, including after a restart. Applying after a purge initializes paper cash from the newly saved `paper_equity`. SQLite backups are complete `.pre-purge-*.sqlite3` databases; JSON backups contain the saved documents and their configuration contexts. The result displays the backup path. These controls affect the local tracker and do not submit exchange orders. Scans, deliveries and form submissions finish before maintenance begins; old forms must be reloaded afterward.

To start a hidden background dashboard with Tailscale Funnel, stop any existing dashboard and run:

```powershell
.\start_dashboard_background.ps1
```

The launcher prefers `.venv`, binds to `127.0.0.1:5000`, waits for the listener, then runs `tailscale funnel --bg http://127.0.0.1:5000`. Tailscale must be installed, signed in, and permitted to use Funnel; follow any setup link it prints. Funnel exposes the dashboard publicly, including its controls. The command prints the public URL and dashboard process ID. Logs go to `dashboard.log` and `dashboard.error.log` (replaced on each launch). Optional parameters are `-Port`, `-StateBackend`, and `-Python`.

You can close PowerShell after startup. The dashboard requires the computer to stay awake and does not automatically restart after logout or reboot. Funnel's background configuration persists across reboots. Run `tailscale funnel status` to view it or `tailscale funnel --https=443 off` to disable that HTTPS listener. See the [Tailscale Funnel command reference](https://tailscale.com/docs/reference/tailscale-cli/funnel).

## Neural-network strategy

The **Strategy model settings** selector now offers **Neural network · Parente 5/2** alongside SMC and legacy. Save the selection, then restart to activate it. The neural model classifies completed 4H candles as BUY, HOLD or SELL, with independent paper cash/history, a saved percentage stop, and the existing portfolio limits. The authors' trained weights and fixed historical normalization are bundled; inference runs locally. Only the selected model's paper positions are monitored.

The project `.venv` includes the required numerical packages. For another environment, install `requirements-neural.txt`. See [neural strategy setup, formulas, provenance and offline backtesting](docs/NEURAL_STRATEGY.md). The existing active selection is preserved until you change it.

## Strategy and formulas

### Breakout entry extension

`smc_breakout_entry: true` adds a faster entry alongside the existing pullback methods. The library default is false; your settings file enables it for all enabled assets. This is a user-requested extension to the video strategy.

1. A completed entry-timeframe candle sweeps a previously untaken, confirmed setup-timeframe liquidity low. The opposite logic applies to margin shorts; spot mode uses this extension for buys only.
2. Within `smc_breakout_window_bars` entry candles, including the sweep candle (currently 6), a completed candle closes back inside that liquidity and beyond an opposing entry-timeframe swing confirmed **before the sweep**. The confirming candle must close in the direction of entry. A deeper extreme after the initial reclaim invalidates that sequence. Both sweep and confirmation may happen in the same candle.
3. Enter using the current ask for a spot buy, with adverse `slippage_rate` added. There is no higher-timeframe BOS, order-block revisit, FVG, or midpoint-pullback requirement on this path. Only already confirmed higher-timeframe liquidity and completed lower-timeframe candles supply evidence; forming candles cannot qualify it.
4. The first qualifying close is eligible for 60 seconds only. The quote must be fresh and observed after confirmation, remain beyond the breakout level, and the estimated fill including slippage must be within `smc_breakout_max_chase_bps` of the confirming close (currently 20 bps / 0.20%). A failed breakout, excessive chase, or consumed target retires the event. Wide spread or insufficient costs/capacity can recover only while the original signal remains fresh.
5. Use the sweep reversal extreme plus the configured stop buffer (or the existing ATR override), and an untaken opposing setup-timeframe liquidity target with the existing GEX preference and sweep extension. Fees, spread, risk, allocation, equity-floor and minimum-notional limits still apply. A market entry and its fill commit atomically; it never becomes a resting midpoint limit.

An asset still has at most one pending paper limit or active paper position. Consuming a sweep also consumes its direction/setup-candle identity, preventing another method from entering the same sweep after exit or restart. Existing buy watches and recorded holdings use their own existing rules. Faster entries can act before a larger-timeframe reversal is confirmed and therefore also admit failed rebounds.

The 11 September BTC regression qualifies at **13:35 BST**, with a $77,114.60 confirming close. A replay quote estimated from the next 5M candle's $77,108.10 opening trade gives a **$77,146.65405** modeled buy after 0.05% slippage. This quote estimate is not a recovered historical executable ask. Historical GEX is unavailable for that instant, so the replay uses the supported nearest-SMC fallback. See `sweep_audit/spike_77000_20260911/breakout_verification.json` and the offline regression in `test_smc_breakouts.py`.

### Original pullback methods

Bullish and bearish analysis mirror each other. The default `market_mode: "spot"` executes spot buys: bullish setups buy at their gap midpoint, while qualified bearish setups place a buy limit at their lower sweep price. Explicit `market_mode: "margin"` enables the separate long/short paper study:

1. On completed 30M candles, sweep an untaken confirmed liquidity swing, return inside, and close through opposing swing structure (BOS). The sweep candle itself may close above/below the order block and confirm BOS.
2. Mark the originating last opposite-colour candle's full high/low range as the order block. Wait for its first revisit after BOS.
3. On completed 5M candles, qualify either entry method. **Conservative:** a structure shift plus a same-direction fair value gap in that reversal leg. **Aggressive:** a close through the far edge of an opposing gap creates an inverse FVG; simultaneous structure shift is optional confluence.
4. Place a paper limit at `(gap low + gap high) / 2`. One order or active paper trade per asset; both methods cannot double-enter one setup.
5. Set one full-position take-profit just beyond the selected untaken opposing confirmed 30M liquidity swing (GEX preference when eligible; nearest SMC fallback), following your sweep-target update. Longs target above the key high; shorts target below the key low. Exit at the sweep price without waiting for reversal. There are no fixed-R targets, partial exits or trailing stops in this model.

The video does not give a complete numerical algorithm. [The source mapping and explicit automation choices](video_strategy_audit/rules.md) distinguish its rules from the necessary conventions: two strictly confirming candles on each side of a pivot, strict first/third-wick FVG separation, and a return inside liquidity on the sweep candle or within two subsequent 30M candles. Evidence becomes eligible only after the confirming candles close. Equal-price closes do not break structure. A first visit can span overlapping 5M candles; after a candle fully clears the block in the reversal direction, a new overlap ends that confirmation window.

Your chosen stop uses the lowest 5M low of the bullish reversal, or highest 5M high of the bearish reversal, from order-block revisit through entry confirmation. The configurable initial buffer is **1 basis point = 0.01%**:

- Long stop = reversal low × `(1 - smc_stop_buffer_bps / 10000)`.
- Short stop = reversal high × `(1 + smc_stop_buffer_bps / 10000)`.

The separate take-profit extension, `smc_tp_sweep_buffer_bps`, also starts at **1 basis point = 0.01%**:

- Long take-profit = key high × `(1 + smc_tp_sweep_buffer_bps / 10000)`.
- Short take-profit = key low × `(1 - smc_tp_sweep_buffer_bps / 10000)`.

For example, a $76,000 key high gives a $76,007.60 long take-profit; a $76,000 key low gives a $75,992.40 short take-profit. The buffer must be greater than zero and at most 100 basis points (1%). The extension implements your follow-up request; its numerical size is an automation choice. A touch of the raw key level alone does not close a filled trade. Pending orders still cancel if that level is reached before entry, even when price has not reached the extended take-profit. Qualification evidence, order cards and trade cards show both prices and the buffer.

The prior ATR sweep-depth filter, ROC/EMA momentum entry, RSI, RVOL and candle-body thresholds no longer qualify SMC entries. Qualification cards display the actual prices, comparisons and source-candle timestamps used by the engine.

An order must be recorded before a future midpoint touch. Historical missed entries are never backfilled. Existing orders and filled trades survive restarts. Missing fill history cancels a pending order; gaps while a trade is active are flagged as unknown execution history. Ambiguous stop/target bars use stop precedence. A bar touching both a pending limit and its raw liquidity reference cancels the order because their intrabar order is unknown.

## SHORT watches for lower spot buys

In spot mode, **SHORT** can describe the bearish leg you are waiting to buy. Under **Your open positions → Add SHORT buy watch**, enter the asset, higher reference price, intended lower buy level and planned quantity. These amounts are **USD**, matching the pair. For example, a reference of `$2,465.89` and buy level of `$2,439.7795` watches for a future purchase at the lower price; it does not record a purchase at the reference price. Enter the exact desired buy level; alerts compare unrounded prices.

Choose **SMC** (the default) or **SMC + GEX** in the target-method selector. Leave the buy level blank to select an untaken confirmed setup-timeframe liquidity low below both the reference and the current ask, then extend below that low using `smc_tp_sweep_buffer_bps`. SMC selects the nearest low. SMC + GEX prefers an eligible aligned low, which can be farther away, with nearest SMC as fallback when alignment is absent or GEX is unavailable. A manually entered buy level overrides automatic selection. The selected level stays fixed. Selection requires current setup/entry candles and a fresh quote. A manual level must be below its reference.

Existing watches keep their saved levels, including earlier GEX selections. To change methods, dismiss and re-add the watch with the buy level blank and your chosen target method. Neither an options refresh nor a later GEX alignment moves a saved SMC fallback target.

A watch sends **BEARISH MOMENTUM · WAIT TO BUY** when the current completed entry-timeframe structure becomes bearish, including an initially bearish reading. It then sends **BUY SPOT · BUY LEVEL REACHED** when a fresh ask is at or below the saved lower level. These are separate conditions: a bullish reversal is not required to buy at the specified low. No BUY alert is issued at the higher reference. A watch must first observe an ask above its buy level; if its first observation is already at/below the level it is marked missed, with no backfilled BUY. Manual watch touches use fresh asks, not historical candle lows. The 15-second scan can miss a brief touch between scans.

Watch alerts use the existing Telegram and optional desktop delivery. BUY quotes expire after 30 seconds; a rebound above the level cancels an undelivered BUY. A new current touch may refresh an undelivered alert with the same identity, preserving retry deadlines. Sent/uncertain alerts do not repeat. Restart requires fresh evidence. Dismissal cancels queued watch messages. Saved targets can still trigger on fresh asks during candle outages; momentum notices need current completed candles.

A manual watch owns no crypto and has no P/L, margin balance or paper allocation. Even after the level is reached, record an actual purchase through **Open position → Spot buy** only after you buy. Existing holdings are not converted into watches. Watches also work for enabled assets with no owned holding.

For **paper trades**, a fully qualified bearish sweep/order-block/FVG signal becomes a **SPOT-BUY limit at the original short sweep target**, rather than a sale at the higher gap midpoint. Its pending card preserves the bearish reference and source evidence and says WAIT TO BUY. The new long stop is `buy limit × (1 − smc_stop_buffer_bps / 10000)`. Its SELL take-profit is beyond the next untaken upper setup-timeframe high, using the existing sweep buffer. The high must be above the buy price and current quote when the limit is placed. Missing upper liquidity or insufficient reward after fees leaves execution waiting.

A future executable ask at/below the limit, or a completed candle touch after the limit existed, can fill the paper buy. A live fill may receive a better price, provided it remains above the stop. A touch of the raw lower liquidity level alone does not fill the extended buy limit. The resulting position is long: price rises produce gains and exits SELL. It remains shown under the SHORT setup panel to preserve its origin. Pending buys cancel if the original bearish source is invalidated, the upper exit liquidity is consumed, entry history is missing, or the observed entry is already beyond its stop. Ambiguous bars use the existing conservative ordering. Once bought, the original bearish invalidation is no longer the long's stop.

Paper cash, fees, allocation, total risk and one working limit/active paper trade per asset still apply. Existing orders and filled trades retain their original recorded levels. No exchange orders are submitted.

## Recorded holdings and momentum alerts

Under **Your open positions → Open position**, enter the asset, **Spot buy**, actual purchase price in USD and quantity in asset units. A short holding period is still a spot buy, not a short sale. Margin long/short choices appear only with explicit `market_mode: "margin"`. This records a holding opened elsewhere, separately from paper cash. When you exit, record the actual exit price using **Close position**.

Spot gross P/L is `(sale price − purchase price) × quantity`; selling below the purchase price is a loss. Marks use a fresh bid for spot buys (or ask for explicit margin shorts), with the timestamped completed 5M close as fallback. It excludes fees and funding. Entry, quantity and side determine P/L; momentum describes the asset's direction independently of your entry price.

Holding momentum now uses the same **completed 5M structure-break formula** as the SMC model. A close above the latest confirmed swing high sets bullish direction; a close below the latest confirmed swing low sets bearish direction. The direction persists until an opposing break, including when its original candle leaves the rolling data window. Before any known break it is neutral. The first reading and a changed strategy/timeframe establish a quiet baseline.

Each later change into bullish or bearish creates one durable alert **per open position and candle**. A bearish change against a spot buy says **SELL TO EXIT LONG**. **BUY TO COVER SHORT** applies only to an existing margin short sale in margin mode. A supporting change says **HOLD LONG/SHORT**, not open another position. Each message includes its position ID, pair, quantity, recorded entry, reason, and a timestamped exit quote (bid for long, ask for short). Without a fresh quote, its exit price is explicitly a completed-candle reference. This is an exit signal based on the existing momentum formula, not an exchange order or a confirmed fill.

Entry-only sweep/OB/FVG gates do not suppress holding alerts. Closing a position cancels its queued messages independently of other positions in the asset. Newer momentum signals supersede older undelivered ones. Saved progress prevents duplicates after restart. Historical transitions remain in the audit trail but are not sent as current actions; missing history establishes a quiet baseline with a gap notice. A quoted action expires after 30 seconds; a candle reference expires at the next candle close. Existing sent messages are preserved, and queued legacy grouped messages are retired on upgrade. The 5M monitor continues if the 30M feed fails.

Undelivered momentum alerts can refresh their price while the originating signal is still the latest completed candle under the same formula. Restart and a 5M feed/clock failure pause delivery until that signal is revalidated. Refresh preserves the event identity and Telegram retry deadline; sent, failed or uncertain deliveries are never requeued. A newer candle, neutral direction or formula change retires the earlier action. Even a fresh quote cannot extend it beyond the next candle close. A supporting momentum change cannot issue HOLD while a fresh exit quote is at or beyond the holding's saved take-profit.

Telegram also warns when an **active paper trade or recorded holding is within 0.10% of its saved take-profit**, before the target is reached. Spot buys use a fresh bid. Explicit margin shorts use a fresh ask. The range is `smc_tp_alert_bps: 10.0` (10 basis points = 0.10% of target); zero disables these warnings. A long enters the alert range at `target × 0.999`, and a short at `target × 1.001`. Each warning includes the asset, side, current executable price, target, remaining distance, liquidity reference and quote time.

For a recorded holding, choose the target method when adding the position. SMC (default) selects the nearest untaken confirmed 30M liquidity swing; SMC + GEX prefers eligible aligned liquidity, with nearest SMC as fallback. The selected swing must be beyond both entry and the current price, then the app applies the same sweep buffer as the paper strategy. This choice controls take-profit; the entered purchase price remains the actual recorded entry. Older records without a method retain their existing selection behavior. Selection requires current 30M/5M candles and a fresh quote. This target is saved once and shown beside the holding's mark price. It remains fixed across restarts and later setting changes; there is no automatic replacement after it is reached. An approaching warning explicitly says **PREPARE** and includes the planned exit price; it is not a target-hit instruction. Reaching the target marks it **Reached** and creates a separate position-specific **SELL TO EXIT LONG** alert (or **BUY TO COVER SHORT** for an explicit margin short) with the current exit quote. This also catches jumps past the advance-warning range. If only historical candle evidence exists, the message says **TARGET TOUCHED EARLIER · REVIEW EXIT** and labels the reference price. The holding stays open until you record the actual exit. Targets already marked reached before this update are not replayed.

Near-target warnings use current quotes, never historical wick proximity. Saved targets continue to be monitored through a candle-feed outage when a fresh quote is available. Closed trades, reached targets, unavailable quotes and prices outside the range cancel undelivered warnings. A quote expires after 30 seconds, including while a Telegram warning waits for delivery. A later fresh observation can refresh an undelivered warning; sent or uncertain deliveries cannot repeat for the same trade/holding. Restart waits for a fresh observation before delivering a pending warning. The default scan interval is 15 seconds, so a fast move can cross the target between scans without an advance warning.

Holding alerts appear beside each open position and in **Position alerts · momentum and take-profit**; expired prices are labeled as past alerts. Paper entries, fills and exits spell out BUY/SELL, position ID, and entry/exit prices, and appear in **Signal delivery and optional commentary**. Both use the existing Telegram configuration. Optional **Enable desktop alerts** uses the same action title and a short price summary; it needs browser permission and the page open. The dashboard server must remain running to monitor assets and deliver Telegram alerts. Form editing pauses page reloads while background monitoring continues.

The latest-alert card uses the most recent observation, including a refreshed target recross. A cancelled alert remains visibly cancelled instead of revealing an older instruction. Its current-price label expires in the browser even while you edit a form. Historical target touches use **REVIEW EXIT** consistently in the displayed message and API payload. Holding marks validate bid/ask values and quote time before labeling a price live.

Spot purchases misrecorded as Short can be corrected while the app is stopped using `python -B -m adaptive_crypto.spot_correction --state <base-state.json>`. This explicit correction keeps every position ID, purchase price, quantity, recorded close and lifecycle timestamp. It changes the side to long, recalculates displayed P/L, archives the old target under `spot_correction`, and replaces the complete backup at `backup/latest.zip`. Prior short alerts remain in the audit trail with their delivery status but are retired from current actions. Closed holdings stay closed. Open corrected holdings receive a newly selected high-side sweep target above their purchase price when fresh candles and quotes are available. New alert identities prevent old sent short warnings from suppressing the corrected SELL warnings. Restart alone never converts a historical record.

Switching to spot mode cancels pending short paper entries and undelivered short paper messages. Any previously filled margin paper short is paused with a warning and retains its original collateral and prices. Recorded shorts left uncorrected in spot mode cannot issue short alerts. Existing long holdings and filled long paper trades retain their targets.

## Settings and saved records

Edit `adaptive_crypto_settings.json` while the app is stopped. Selected values:

```json
"strategy_model": "smc_video",
"market_mode": "spot",
"smc_setup_minutes": 30,
"smc_entry_minutes": 5,
"smc_entry_method": "both",
"smc_breakout_entry": true,
"smc_breakout_window_bars": 6,
"smc_breakout_max_chase_bps": 20.0,
"smc_pivot_strength": 2,
"smc_reversal_bars": 2,
"smc_stop_basis": "swing",
"smc_stop_buffer_bps": 1.0,
"smc_tp_sweep_buffer_bps": 1.0,
"smc_tp_alert_bps": 10.0
```

The other supported video pairing is 15M/1M. Entry method may be `both`, `conservative` or `aggressive`. Numeric settings use JSON numbers. The previous configuration is preserved in `adaptive_crypto_settings.pre-smc.json`.

Existing fees, slippage and portfolio limits are retained. `fee_rate: 0.008` means **0.80% per side**. The SMC model charges fees on both sides and applies `slippage_rate: 0.0005` (**0.05%**) to stop exits and breakout market entries. Pullback limits receive their limit price or a better observed executable quote. Take-profits stay at their recorded liquidity-plus-buffer price. A target that cannot cover modeled costs leaves the setup qualified but execution waiting; it is never moved to manufacture a risk/reward ratio. Net reward/risk is reported, not a fixed-R qualification gate.

Paper size is bounded by available cash, per-trade risk, shared total risk, the paper equity floor and maximum allocation. Spot mode opens only long buy positions and rejects actual short sales in the holdings form, API, engine and sizing logic. SHORT buy watches are prospective purchases stored separately from owned holdings. In explicit margin mode, long and short scenarios reserve unlevered collateral; short-sale proceeds cannot inflate available cash. Short scenarios use spot prices and do not model borrow availability, borrow charges or funding. They are not verified exchange executions or a profitability backtest.

pyPTA retains one complete backup in `backup/latest.zip` in the project root. Settings saves, model changes, settings application, purge, startup archives, imports and corrections replace that archive after verifying the new copy. It includes saved settings and all existing strategy ledgers, holdings, orders, alerts and history. SQLite uses a complete database snapshot; JSON uses the state files together. A custom study directory keeps its backup beside its state base. Historical backups already on disk are left in place. See [backup and recovery instructions](docs/SQLITE_PERSISTENCE.md#backup-and-rollback).

The JSON state files are:

| File | Purpose |
|---|---|
| `adaptive_crypto_settings.json` | Active settings |
| `adaptive_crypto_settings.pre-smc.json` | Configuration before the video adaptation |
| `adaptive_crypto_reclaim_state.smc.json` | Separate SMC paper ledger and delivery history |
| `adaptive_crypto_reclaim_state.positions.json` | Existing external holdings and their alerts |
| `adaptive_crypto_reclaim_state.json` | Preserved legacy paper ledger |

A custom `--state` supplies the base name for the `.smc.json` and `.positions.json` sidecars. SMC settings changes back up that ledger and cancel pending limits; active trades keep their recorded levels and costs. This includes adopting or changing the take-profit sweep buffer: previously filled trades keep their original targets, including targets directly at liquidity from before the update. Holdings survive strategy changes. Disabled assets pause their holding watches; disabled assets with active paper trades show a warning. Damaged SMC or holdings files fail without being overwritten.

Changing only `smc_tp_alert_bps` is a notification preference: it preserves pending orders, filled targets, cash and existing holding targets.

Legacy mode remains available using the previous settings backup. Its previous formula fingerprint is preserved. Legacy upgrade and qualification history are recorded in `IMPLEMENTATION_NOTES.md`; those older 4H/15M rules do not describe the active SMC model.

## SQLite persistence

The dashboard supports SQLite WAL with FULL durability, incremental record writes, and committed in-memory snapshots. `--state-backend auto` keeps an existing JSON installation on JSON until an explicit import, chooses an existing SQLite database after import, and initializes genuinely new state in SQLite. `--state` remains the logical JSON base path; the database uses the same base with a `.sqlite3` suffix. Paper studies and holdings have separate namespaces in that database.

SQLite requires a local disk and a Python runtime linked to SQLite 3.51.3+ or the fixed 3.50.7/3.44.6 branches. The local `.venv` uses SQLite 3.53.1. `start_dashboard.ps1` prefers that environment when present; `-Python` overrides the executable and `-StateBackend` overrides selection. The system Python was not changed. Both launchers and the spot-correction command support `--state-backend`.

Use [the SQLite operating guide](docs/SQLITE_PERSISTENCE.md) for import, verification, backup and rollback. Import runs with the dashboard stopped, backs up original bytes, and publishes the active paper namespace plus holdings atomically. It leaves inactive study files untouched. Once SQLite exists, the dashboard refuses a JSON fallback at the same base. Backups and exports use current database state; old sidecars remain migration sources, not current ledgers.

Implementation and offline verification do not migrate or restart a running dashboard. Run database tests with the fixed runtime: `.\.venv\Scripts\python.exe -B -m unittest discover -s .`. Older SQLite builds skip the database integration tests and cannot establish SQLite readiness.

## Optional services and listener

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` enable Telegram dispatch. Without them, events stay queued. Interrupted or ambiguous deliveries show `uncertain` and are not automatically resent. AI commentary remains separate from qualification and Telegram delivery; configure `OPENAI_API_KEY`, `OPENAI_MODEL` and the optional `openai` SDK in the launch environment. Missing configuration or commentary failure does not block strategy evaluation.

The default listener is localhost. `DASHBOARD_HOST`, `DASHBOARD_PORT`, `--host` and `--port` can change it. The app does not change firewall or router settings.

## Verification and code

```powershell
python -B -m unittest discover -s .
node --test test_gex_ui.js
python .\adaptive_crypto_dashboard.py --once --state .\verification_state.json
```

The Python tests and Node browser-behavior tests run offline. The final command reads public data, evaluates the strategy and saves to a separate verification ledger without starting Telegram or AI workers. `/api/state` exposes timestamped evidence and `/health` reports feed/view health. The independent live 30M chart may show a labelled forming candle; chart data never supplies qualification evidence.

`smc.py` contains the pure video rules, `smc_engine.py` handles order decisions, and `smc_ledger.py` persists and accounts for SMC paper trades. `positions.py` handles recorded holdings and direction alerts. `runtime.py`, `market_data.py`, `web.py`, templates and static files connect the feeds and dashboard. `strategy.py`, `engine.py` and `ledger.py` retain the legacy strategy. Both `python adaptive_crypto_dashboard.py` and `python -m adaptive_crypto` remain supported.
