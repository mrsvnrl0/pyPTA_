# Neural-network model and paper simulation

pyPTA uses the published Parente 5/2 model on completed 4H candles. Paper Trading always runs the neural strategy. With the default **NN limitations** checkbox on, BUY opens one paper spot position per asset, HOLD/repeated BUY keeps it open, and SELL or the saved stop closes it. With limitations off, each new BUY can add a position using available cash, and new trades have no automatic stop. SELL closes every open NN position for that asset. It uses local inference; no paid AI service is needed.

Home displays market classifications and their BUY / HOLD / SELL probabilities independently of paper positions, balances or trade eligibility. Positions applies the classification to each manually recorded holding separately from its SMC and SMC + GEX targets. Those targets and the separate 4H protected-level monitor do not control neural paper orders.

## Use it

The project `.venv` already has the numerical dependencies installed. For another environment:

```powershell
python -m pip install -r requirements-neural.txt
```

Open **Settings**, adjust the NN simulation and paper account/risk fields, save, then apply changes. SMC settings tune the independent market and position analysis. There is no strategy selector: legacy, combined and SMC paper strategies are no longer launched by the application.

Alternatively, edit the existing settings JSON's `strategy` object:

```json
{
  "strategy_model": "neural_network",
  "nn_model_path": "",
  "nn_limitations": true,
  "nn_stop_loss": 0.10,
  "nn_signal_max_age_seconds": 60
}
```

Keep the other existing asset and strategy settings. An empty model path uses `adaptive_crypto/models/parente_5_2.npz`; a relative custom path resolves beside the settings file. `nn_stop_loss` must be greater than zero and at most 0.10; it applies to new trades only while limitations are on. The execution window is 1–300 seconds. SMC target and displayed momentum calculations use their own configured timeframes (30M/5M or 15M/1M); position protected-level alerts use a separate 4H, 2-left/2-right monitor. Neural paper stop history uses 5M candles. With limitations off and no existing positions with stops, the execution path does not wait for 5M stop history. Neural paper entries are long-only even when margin mode is selected for other workflows.

The model needs at least 100 contiguous completed 4H candles and frozen volume calibration for the asset's base symbol. The bundled calibration includes BTC, ETH and SOL. Missing dependencies, invalid models, uncalibrated assets and stale/broken feeds appear on the dashboard. Existing stops continue to be monitored from valid quotes or completed stop-monitoring candles even when inference is unavailable.

## State and execution

Home, paper trading and manual-position guidance share one loaded model and cached classification. A replaced model file takes effect for all three only after applying settings or restarting. Each scan processes NN execution for all assets before advisory SMC requests, obtains a fresh executable quote after required inputs/inference, and checks the actual execution time.

Position NN alerts retain their last valid classification across temporary quote/model outages. Current undelivered exits can recover with a fresh quote; sent or uncertain deliveries are never replayed. These alerts remain independent of SMC targets and do not close recorded positions.

- The active neural simulation has its own cash and paper history. JSON uses a `.neural.json` sidecar; SQLite uses a `neural` namespace in the existing WAL database. Manually recorded positions remain in their separate positions store.
- Legacy, combined and SMC paper records remain readable in Trade Records. Their open records are marked as no longer monitored. The NN-only application does not switch back to those strategies.
- Signals use the largest of BUY/HOLD/SELL probabilities. These are model outputs, not calibrated probabilities of profit. Inputs, signal close, model identity, trade costs and accounting remain inspectable in the dashboard/API.
- A classifier entry or exit needs a fresh executable quote observed after the 4H close and inside the execution window. A late startup displays the last classification without backfilling a trade. Missing quotes can recover within the original window. With limitations on, BUY also requires the latest completed stop-monitoring candle, acceptable spread, and sufficient cash/risk capacity.
- BUY uses ask plus configured slippage. SELL uses bid minus recorded slippage. Both sides charge the recorded fee. With limitations on, the entry stop is fixed at `entry * (1 - nn_stop_loss)`; with limitations off, new trades have no automatic stop and exit on NN SELL. There is no fixed take-profit.
- Completed candles wholly after entry may establish a stop. A gap below the stop uses the adverse candle open; current quotes below the stop use that bid. Stops precede classifier exits, and an observation that stops a position cannot re-enter it. The entry-containing candle cannot establish retrospective intrabar ordering.
- With limitations on, one active position per asset and the allocation, total risk, per-trade risk, equity-floor, spread and minimum-notional settings constrain entries. With limitations off, each new 4H BUY spends all available paper cash including the entry fee, with no borrowing. The first eligible asset can use the whole balance; later entries need cash released by exits. There is no position-count limit. Open risk for a trade without a stop is its full invested cash including the entry fee.
- The signal candle is consumed durably. Repeated scans, restarts and model changes cannot trade it twice. The trade and notification outbox commit together; failed commits publish neither. Interrupted Telegram delivery becomes uncertain and is not automatically resent.
- Applying settings retains existing neural trade levels, costs and consumed candle identities. Switching limitations off preserves stops on existing trades; switching on does not add stops to earlier stopless trades or discard multiple positions. SELL closes all active lots for the asset in either mode. Purge and migration/export/backup support the neural namespace using the existing maintenance rules. Backups replace the single complete `backup/latest.zip` archive.
- NN position guidance is advisory: an adverse classification shows TAKE PROFIT when the executable price is profitable relative to entry, or STOP LOSS otherwise, excluding costs. Supporting and neutral classifications show HOLD. Recorded positions stay open until you record your actual exit.

## Research mapping and differences

Reference: Parente, Rizzuti and Trerotola, *A profitable trading algorithm for cryptocurrencies using a Neural Network model*, Expert Systems with Applications 238 (2024), 121806. [Paper](https://doi.org/10.1016/j.eswa.2023.121806), [published source/data/model archive, version 2](https://figshare.com/articles/code/CryptoTrading_zip/22953377/2).

The network is 36 → 128 → 64 → 32 → 3, with LeakyReLU slope 0.01 and softmax. Output order follows the authors' label encoder: -1/0/1 = BUY/HOLD/SELL. The bundled weights are the authors' trained `model_final_5_2.h5` Dense arrays converted to a data-only NPZ. No TensorFlow, pickle or downloaded Python scripts execute during loading. This is the published model, not a newly trained model.

The saved weights need the authors' executable feature definitions where they differ from the prose:

| Component | Implemented definition from the source |
| --- | --- |
| Z-score | Log returns, rolling 20-candle mean and sample standard deviation; prose describes 30 closing prices. |
| Oscillators | RSI(14)/100; ULTOSC(7,14,28)/100; position inside BBANDS(5, 2σ); simple close percentage change. |
| Volume | Base volume normalized by each asset's archived sample mean/std, then frozen. |
| Four MA ratios | Relative differences of price/SMA21, SMA21/SMA50, SMA50/SMA100, price/SMA50; prose describes EMA and a 20-period average. |
| Candlesticks | The exact 23 TA-Lib pattern functions and order from the archive, divided by 100. See `neural.FEATURES`. |
| Time | UTC weekday, month, and hour divided by 4. |
| Training labels | Adjusted EMA over the backward window versus the forward close; strict alpha/beta bounds, alpha 0.038. Source beta is `0.24 * (1 + forward * 0.1)`, so 0.288 for forward=2; prose's adjustment gives 0.264. Source labels omit fees. `labels(..., convention="paper")` exposes the beta discrepancy; it does not change trained weights. Unknown future tails remain NaN. |

The archive does not save the StandardScaler. The importer reconstructs it from the final-training pool: drop invalid rows, exclude BTCUSDT/ETHUSDT/ALGOUSDT, retain `pct_change < .24`, and fit before balancing and the random train/test split. The final-training script deliberately includes all available dates. The reconstructed scaler has **1,533,301 rows across 407 assets**. Its population standard deviation matches StandardScaler conventions.

The authors' backtest refits normalization on evaluation data, and volume z-scores use each complete asset series. Here all normalization is frozen from the archive, so future candles cannot alter an earlier feature row. Live inference and the offline trading tool require timestamps after the last training/calibration candle: **2022-12-04 15:59:59.999 UTC**. Historical pre-cutoff data can still be used to verify feature arithmetic, without presenting it as out-of-sample trading.

Live Kraken/USD versus historical Binance/USDT, frozen venue-specific volume statistics, subsequent market changes, next-observation execution and portfolio limits materially change results. The implementation does not reproduce or establish the paper's reported returns.

## Offline tools

Inspect the bundled model:

```powershell
.\.venv\Scripts\python.exe -m adaptive_crypto.neural_tools inspect
```

Reproduce conversion from the downloaded version-2 archive to a **new** output path:

```powershell
.\.venv\Scripts\python.exe -m adaptive_crypto.neural_tools import-author .\.neural-work\CryptoTrading.zip --output .\reproduced_model.npz
```

The importer verifies archive MD5 `012f76ea14b5f95fff2b77f3c4e5441c`, reads data/weights without executing scripts, and writes the NPZ plus JSON provenance. The bundled NPZ SHA-256 is `8907c6f395dc8cf385619f1cace6e27c3b23a04a820fe980cde65898cad56c87`. Conversion needs h5py in addition to the inference dependencies.

Backtest an operator-supplied single-asset CSV with `Date,Open,High,Low,Close,Volume`, contiguous UTC 4H candles and base-asset volume:

```powershell
.\.venv\Scripts\python.exe -m adaptive_crypto.neural_tools backtest .\btc_4h.csv --asset BTC --output .\btc_neural_backtest.json --fee 0.001 --slippage 0.0005 --stop-loss 0.10
```

Only feature-ready signals after calibration are eligible. Signal at t executes at t+1 open, gap stops take precedence, fees/slippage apply on both sides, and remaining positions liquidate at the final close. The single-asset simulator reinvests available capital and uses its own CLI stop/cost options; the dashboard checkbox does not change offline backtests. Its report contains trades, equity, net return, drawdown sampled at candle closes, a seeded 15/70/15 dummy baseline, and a buy-and-hold-with-stop baseline with the same costs/stop. OHLC cannot reconstruct intrabar order; this is an explicit execution model. For an intrabar stop, the report uses candle close as its confirmation timestamp (`closed_ms`, `exit_time_basis: candle_close_confirmation`) and sets `exit_time_exact` to false; it does not claim the stop filled at that precise time. Gap stops and classification exits retain the modeled candle-open timestamp, and terminal liquidation uses the final close.

## Verification

Historical full-suite run on 12 September 2026: **496 Python tests passed in 22.587 seconds**, including 31 neural regressions, with no skips on Python 3.12.14 / SQLite 3.53.1. All **8 Node browser-script tests** passed. Desktop and 390px browser checks showed no page overflow or JavaScript console errors. Model conversion and a 300-candle synthetic CSV backtest completed; this smoke test is not historical profitability evidence.

Run all tests with the fixed project runtime:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s . -q
node --test test_gex_ui.js test_nn_charts_ui.js
```

`test_neural_strategy.py` covers inference/schema rejection, causal feature prefixes, source/paper labels, execution chronology, fees/risk/accounting, failed writes and failed SQLite claims, restarts, settings/reload, purge, independent strategy namespaces and migration round trips. A fixture compares 36 measurements for three archived BTC candles: maximum absolute error **5.56e-16**. An independent per-node calculation from published H5 weights agrees with converted inference to **2.99e-7** maximum probability error.

The model/data source is attributed under GPL-3.0-or-later; the archive also points to Binance terms for financial-data usage. See `adaptive_crypto/models/NOTICE.md` and the JSON provenance file. Source datasets and the large research archive are not required by the application.
