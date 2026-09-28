# Selectable neural candidates for pyPTA: implementation handoff

Prepared 28 September 2026 for GPT6-Solar. Status: **specification only; no implementation or training performed**.

Workspace: `C:\Users\CxN\Documents\ChatGPT\pyPTA`.

## 1. User intent and scope

The user supplied *A survey of deep learning applications in cryptocurrency* and asked for the best relevant neural application to be prepared for another model to implement, explicitly saying **do not implement** during this preparation. They then requested that candidate models be created and made selectable. This document incorporates that request. It does not authorize this preparation task to change the application or launch training.

When the user starts implementation, build genuinely trained, distinguishable candidates and a Settings selector. Keep one selected NN supplying Home, Paper Trading and Positions. Keep the NN limitations checkbox independent of model selection. Preserve records, credentials, the selected AI commentary provider, SMC/GEX analysis and the 15-minute market-summary feature.

The objective is to identify the best tested candidate for this application's data and execution rules. Do not predeclare a winner from unrelated accuracy figures, promise profitability, or replace the current model automatically.

## 2. What the supplied paper establishes

Source: Zhang, Cai and Wen, *iScience* 27 (2024), 108509, DOI [10.1016/j.isci.2023.108509](https://doi.org/10.1016/j.isci.2023.108509).

User PDF: `C:\Users\CxN\Desktop\1-s2.0-S2589004223025865-main.pdf`.

SHA-256: `e8d0d20c919c0c625db0c40b8d33e8e2bb2c15800775baebdee3563dbbb6a47c`.

Page numbers below are the PDF's printed pages, which match its 40-page file. The conclusion spans pp. 33-35. The paper is a survey, not a controlled common-dataset tournament and not a deployable model artifact.

| Evidence in supplied PDF | Implementation implication |
| --- | --- |
| pp. 16-20, Tables 7-8: recurrent, hybrid, convolutional and attention models are reviewed for price forecasting. Different datasets, tasks and metrics are used. | Build a bounded candidate comparison on one controlled dataset/protocol; do not rank numbers across these studies. |
| p. 17, Table 7: a ridge model leads one comparison, while other rows favour different neural models. | Include simple baselines. Greater complexity is not evidence of superiority. |
| p. 19, Table 8, Kim et al., reference 144: attention-based multiple LSTMs, on-chain inputs and change-point preprocessing for BTC forecasts. | An attention-LSTM is a justified research candidate, not a proven best trading system. |
| pp. 21-22, Tables 9-10: DRL and other methods address portfolio weights, risk and allocation. | Portfolio-policy learning is a different task. Do not silently replace the existing paper execution/sizing rules with reinforcement learning. |
| pp. 28, 30-31, 33-35 and Table 18: regime changes, scarce/heterogeneous data, non-stationarity, real-time timing and model selection remain challenges. Ensembles are a proposed direction. | Require chronological validation, causal preprocessing, data availability checks and measured inference time. Evaluate an ensemble, rather than assuming it helps. |

Two corrections matter. On p. 16 the survey expands SAM as “Segment Anything Model”; this is incorrect for the cited method. Its conclusion also points to reference 142 for SAM-LSTM, whereas Table 8 and the bibliography identify Kim et al. as reference 144. Use the original study to resolve these discrepancies.

The authors' [institutional publication record](https://research.knu.ac.kr/en/publications/a-deep-learning-based-cryptocurrency-price-prediction-model-that-/) defines SAM-LSTM as **self-attention-based multiple long short-term memory**. Its grouped on-chain inputs and price-regression task differ from pyPTA's three-class classifier. The [original article](https://doi.org/10.1109/ACCESS.2022.3177888), indexed from the [UNIST repository](https://scholarworks.unist.ac.kr/bitstream/201301/58587/2/000804637100001.pdf), shows feature selection and change-point segmentation/normalization in its preprocessing procedure. **Engineering inference:** applying these operations to an entire evaluation series can expose future information; require prefix-invariance tests and train-only fitting in our implementation. Direct PDF retrieval returned an HTML access page during preparation. Full architecture/hyperparameter reproduction, licensed code and pretrained weights were not verified; do not claim an exact reproduction.

Recommendation: create a selectable family of supervised 4H classifiers, with grouped attention-LSTM as the paper-inspired candidate and simpler recurrent/hybrid alternatives as controls. Treat on-chain inputs and sentiment as separately validated extensions. This is an engineering recommendation based on the survey, not the survey's declaration of a winner.

## 3. Verified application baseline

These observations were checked against current source and a read-only local API request during preparation. Recheck them before implementation because the user may change settings.

- Running dashboard settings path: `C:\Users\CxN\Documents\ChatGPT\pyPTA\adaptive_crypto_settings.json`.
- Saved and applied model path is empty, selecting bundled `parente_5_2.npz`. Artifact SHA-256: `8907c6f395dc8cf385619f1cace6e27c3b23a04a820fe980cde65898cad56c87`.
- Saved and applied `nn_limitations` is **false**. Do not restore the previous task's ON value.
- Enabled saved pairs: BTCUSD, ETHUSD, SOLUSD, SUIUSD; normalize through existing code to canonical `/USD` symbols. Do not hard-code these as the permanent universe.
- Saved/applied signal window: 60 seconds; fee: 0.004 per side; slippage: 0.0005 per side. These are observed settings, not immutable defaults.
- Parente is a 36-input, 128/64/32 hidden-unit MLP with three outputs, local inference, frozen transforms and a training/calibration cutoff of `1670169599999` ms. It has frozen volume calibration for BTC, ETH and SOL, **not SUI**.
- Current loader supports only this fixed NPZ architecture. A different network cannot be enabled merely by changing `nn_model_path`.
- Current neural tools import the author model, inspect it and run a single-asset backtest. There is no candidate training pipeline or archival market-data importer.
- Historical verification from the preceding implementation: 749 Python tests run, 748 passed, one Windows symlink skip; 50 browser tests passed. These counts were not rerun for this documentation-only task and will change with new tests.

## 4. Candidate catalogue and selector

Use stable architecture IDs plus immutable trained-artifact IDs. The following are proposed starting architectures, **not hyperparameters quoted from the paper**.

| Selector name | Architecture ID | Initial implementation |
| --- | --- | --- |
| Parente 5/2 (current) | `parente_mlp_v1` | Preserve the current loader, original features, weights, class order and provenance. |
| LSTM | `lstm_classifier_v1` | One unidirectional LSTM, 64 hidden units, final state, dropout 0.1, linear 3-class head. |
| GRU | `gru_classifier_v1` | Same input/output and training protocol, replacing the recurrent layer with a 64-unit GRU. |
| CNN-LSTM | `cnn_lstm_classifier_v1` | Causal 1D convolution, 32 channels, kernel 3, ReLU, then a 64-unit unidirectional LSTM and the same head. |
| Attention-LSTM (paper-inspired) | `grouped_attention_lstm_v1` | Three feature-group LSTMs, 32 units each; learned temporal attention per group; concatenate group summaries, Dense 32/ReLU, dropout 0.1, 3-class head. |
| Ensemble | `probability_ensemble_v1` | Fixed equal-weight average of eligible trained candidate probabilities, with member hashes and identical class/target contracts. Keep unavailable until it has its own validation report. |

For grouped attention, define explicitly: `e_t = v^T tanh(W h_t + b)`, `a_t = softmax(e)_t`, `z = sum_t a_t h_t`. Attention sees only the trailing window available at decision time. This specifies an adapted network; it is not a claim to reproduce the published SAM-LSTM. Do not add image segmentation dependencies.

Start with the four new single-network candidates. An equal-weight ensemble is a low-cost additional experiment once their probabilities are compatible. Do not include Parente in a mixed-target ensemble unless its label definition matches exactly; otherwise keep it a separate benchmark.

Every catalogue entry must show its state: **not trained**, **training**, **trained/validated**, **unavailable**, or **failed**, with a useful reason. An architecture skeleton, random weights, mock predictions or a copy of Parente under a new name is not a completed candidate. Training results must distinguish technical validity from performance: a technically valid candidate can be manually selected even if it did not beat Parente, with its measured results visible. Only evidence can justify a “recommended” badge. No candidate should auto-activate after training.

Settings needs one **NN model** selector, an immutable trained-version selection where needed, the existing independent limitations checkbox, supported-asset coverage, training/data cutoff, target horizon and a link to validation results. Preserve Save -> Apply behavior, dirty-state handling, discard, revision checks and CSRF. Show saved versus applied model when they differ. Do not add duplicate editable controls to Home.

Use one selected model family across all three consumers. A model bundle may contain independently trained per-asset members, but every member must be from the selected family and identified in the report. Do not silently fall back to another architecture for an unsupported asset. Show “model unavailable for this asset.” Block an Apply that would leave an existing active paper position without classification-exit coverage; let the user choose a covered model or explicitly resolve the position in a separate action.

Model switching follows existing execution ownership: recorded entry facts stay fixed; the newly applied model controls **future** NN classifications and exits for existing positions, while their recorded stops/costs remain unchanged. Explain this in the selector. Per-position retention of the entry model is a different feature and is outside this plan.

## 5. Dataset and feature specification

### Required first release

Use Kraken spot USD OHLCV, completed UTC 4H bars, for each enabled supported asset. A two-year contiguous training history per asset is a reasonable initial minimum for this small-network experiment, not proof of adequacy; report actual effective sample counts and class counts. Prefer longer history and multiple regimes. Insufficient history produces an explicit incomplete/unavailable status, not fabricated samples.

The current provider is not a historical archive. Kraken's [OHLC endpoint](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) returns at most 720 recent entries, includes the unfinished candle, and cannot retrieve older history by changing `since`. Use the official [historical OHLCVT files](https://support.kraken.com/in/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) or user-supplied matching data, then verified recent updates. The archive includes 240-minute bars; intervals without trades can be absent. Check archive size/storage before bulk download; the preparation task has not downloaded the market archive.

Record provider, venue, canonical pair, quote currency, interval, retrieval time, file hashes, start/end, gap ranges, duplicate/revision handling and dataset version. Do not silently substitute Binance/USDT for Kraken/USD. Do not manufacture candles across outages. Drop training sequences that cross unverified gaps and report the lost coverage; never forward-fill OHLC into executable evidence. Explain intentional no-trade gaps separately if verifiable.

All four new classifiers must initially use the same features, labels and split boundaries. A proposed compact 16-feature schema:

| Group | Features computed at completed bar t |
| --- | --- |
| Price (6) | `log(C_t/C_(t-1))`; `log(O_t/C_(t-1))`; `log(H_t/C_(t-1))`; `log(L_t/C_(t-1))`; `log(C_t/O_t)`; `(H_t-L_t)/C_t`. |
| Momentum/volatility (7) | Log close returns over 3, 6, 12 and 24 bars; `log(C_t/SMA24_t)`; `RSI14_t/100 - 0.5`; trailing 24-bar sample standard deviation of one-bar log returns. |
| Volume (3) | `log1p(V_t)`; `log((V_t+epsilon)/(SMA24(volume)_t+epsilon))`; first difference of `log1p(V_t)`. Freeze epsilon and base-volume units in the schema. |

Freeze feature order, formulas, indicator library/version, warm-up, epsilon, and float conventions. The default input is the last **64 feature rows**, with enough preceding candles for warm-up. Train and infer through the same feature builder. Validate positive OHLC, finite nonnegative volume, correct bar ordering and completeness before computing logs.

Specify recursive-indicator initialization exactly. Proposed first-release policy: each feature row is computed from a fixed 256-bar trailing raw-candle context ending at that row, including RSI initialization; assembling 64 rows therefore requires 319 contiguous raw candles. Use that identical construction in batch training and live inference, and record both lengths in the manifest. Do not compute training RSI from the full archive while live RSI starts at an arbitrary provider-window boundary. Verify the provider actually supplies the declared context; if insufficient, report unavailable rather than shortening it. A different persisted-state policy is possible only with explicit equivalent initialization/restart tests.

Use a frozen scaler fitted only to the training partition. Start with per-asset models and scalers inside a common family bundle, avoiding accidental cross-asset normalization. All enabled assets require real training/validation, including SUI; never copy BTC volume statistics to it. Shared multi-asset training is a later controlled experiment, not an undocumented substitution.

### Labels and action contract

For the first candidate comparison, reuse the repository's **source-convention Parente 5/2 label function** in `neural.labels`, with its exact alpha/beta, EMA convention and class mapping. This keeps a controlled target while changing architecture. These labels use a two-bar forward horizon; they are training targets only. Remove samples whose forward target is unknown. Never run the labeler as part of live feature construction.

Generate labels once per declared contiguous asset-history segment, with a recorded EMA initialization/burn-in policy, before assembling overlapping samples. Do not restart the adjusted EMA for every 64-row training sample. Purge samples with targets crossing gaps or split boundaries. Label construction may use the declared forward horizon; feature construction may not.

Keep `BUY/HOLD/SELL` probability outputs and deterministic argmax, including a documented tie rule. Recurrent candidates must learn their own weights against those targets. They do not inherit Parente weights. Evaluate net performance after actual configured costs regardless of the label convention. A cost-aware label variant can be a later separately versioned experiment, not a silent change during model comparison or when limitations toggles.

Do not turn a single regression estimate into invented three-class probabilities. If a faithful price-regression study is added later, it needs an explicit forecast contract and validated action mapping, with truthful UI and ledger changes. The first selectable release avoids that unrelated migration by training genuine classifiers.

### Optional data, separate later experiment

On-chain or social inputs require a real historical source with reproducible availability/revision timestamps, not just current API access. Record event time and when the datum was first available. Join only information available by the signal's decision boundary. A daily metric cannot be treated as known at each preceding 4H close. Define missingness and maximum age in the trained artifact; do not drop an input group dynamically or feed zeros into a model not trained for that policy.

The existing AI market summary is web-grounded prose, not a causal sentiment dataset. Do not feed summaries, current search results, current engagement counts or retrospectively discovered tweets into historical features. Do not add paid on-chain/social subscriptions or paid AI generation as an assumed prerequisite. Continue the complete OHLCV candidate work independently; document missing optional data precisely. Preserve the user's existing commentary/Telegram credentials without reading them into training reports.

## 6. Training and evidence for selecting a winner

Keep training outside Flask and outside the NN execution loop. Prefer a separate optional PyTorch training environment and an audited, versioned CPU inference export such as ONNX for the app. Verify current official framework/export documentation and Windows/Python compatibility before pinning dependencies. Keep the Parente path operational without optional candidate dependencies installed. No arbitrary pickle or remote-code deserialization in runtime. Do not write a second hand-maintained LSTM implementation unless export parity cannot be achieved and the tradeoff is documented.

Initial training recipe, all engineering defaults: Adam at 0.001, batch size 64, at most 100 epochs, gradient norm clipping at 1.0, early-stopping patience 10. Begin with one fixed seed as a functional smoke run, then three recorded seeds for evaluated candidates. If using class weights, calculate them from the training partition only and report their effect on probability calibration. Do not claim deterministic cross-hardware equivalence without verification.

Do not launch an unbounded architecture/hyperparameter search. Pre-register the initial configuration, data range, seeds, splits, compute estimate and ranking rules in an experiment manifest. Measure one small training run to estimate the full schedule before a long job. Save progress, logs and resumable training state outside the production model directory. Do not purchase cloud compute as an implicit step.

Use common chronological boundaries across assets. For each sufficiently long dataset, reserve the last 20% of the aligned time span as a final untouched test. Use expanding-window folds in the preceding 80% for fitting and tuning; keep a distinct validation slice for early stopping/calibration. Freeze exact UTC boundaries before training. Purge any training label whose forward horizon intersects validation/test. Prior observed bars may provide validation lookback context, but their labels must not cross the boundary. Never randomly split overlapping windows.

Fit scalers, feature selectors, class weights, calibration and ensemble choices without final-test information. Label smoothing, probability calibration and threshold choices, if introduced, must be versioned and validated separately. Do not optimize ensemble weights or choose its member set using the final test. Do not repeatedly inspect the final test to retune a failing candidate; a fresh confirmation period is then required.

Baselines: bundled Parente on supported assets and dates strictly after its cutoff; buy-and-hold; cash/no-trade; majority-class and simple linear/logistic feature baselines. Report absent Parente coverage for SUI rather than invented comparisons. Candidate-vs-candidate comparisons use identical target semantics; distinguish Parente's historical training distribution from freshly trained candidates.

Required report per model and per asset: sample/class counts, chronological splits, data/gap coverage, accuracy and balanced accuracy, macro-F1 and confusion matrix, log loss/Brier score and reliability diagnostics, net return, drawdown, fees/slippage, turnover, exposure, number of completed trades, CPU inference latency and memory. Provide fold and seed dispersion, not only the best run. Forecast-error metrics alone cannot establish trading quality. Class probabilities are not probabilities of profit.

The trading evaluator must reproduce the live ledger semantics. The existing single-asset `neural_tools.simulate` has its own all-capital/stop assumptions and is not sufficient for the new multi-asset comparison. Use a deterministic event replay of the same entry/exit/accounting policy on an isolated state store. Model the earliest next observable executable price; if only OHLC is available, disclose the bid/ask/spread assumptions and prohibit same-close fills. Monitor recorded stops on 5M data where available; flag uncertainty when intrabar ordering cannot be established.

Measure performance using **net-liquidation equity**, not the ledger's entry-cost equity: `cash + sum(quantity * observed_bid * (1 - recorded_slippage) * (1 - recorded_exit_fee_rate))`. Entry fees are already deducted from cash; do not deduct them twice. Mark all open lots on a common fixed cadence, initially 5M where the historical data supports it, with quote-proxy assumptions disclosed. Mark missing/stale valuations as incomplete rather than hiding losses. Report sampled drawdown with its cadence and limitations. Apply one predeclared terminal liquidation at the final valid observable price for every model, label it as evaluation-only, and report natural exits separately; terminal liquidations do not satisfy the minimum completed-trade evidence criterion. Add a losing-open-position fixture that visibly reduces equity and increases drawdown even when no SELL occurs.

Evaluate limitations ON and OFF separately, with the same models, date range, asset order and costs. OFF can spend all available cash on the first eligible asset; shared-cash ordering materially changes results. Preserve and disclose current order rather than silently introducing model-based portfolio allocation. Include base costs and predeclared higher-cost stress scenarios. Do not compare an unconstrained candidate against a constrained Parente and call the difference a model improvement.

Proposed recommendation rule: choose architecture/configuration using validation folds; confirm once on final test. Require no correctness failures, positive net out-of-sample results under base costs, better paired net results than Parente on comparable coverage, and drawdown no worse than Parente for a default-replacement recommendation. Report uncertainty using time-block resampling and regime/fold breakdowns. At least 30 completed out-of-sample trades is an initial evidence floor, not statistical proof. If evidence is insufficient, highly unstable, or tradeoffs conflict, return **no demonstrated winner**. Models can still be technically valid selectable research candidates. Do not change ranking criteria after seeing which model wins.

## 7. Model artifact, registry and prediction interface

Introduce a registry/factory; keep `strategy_model='neural_network'`. The new selector chooses an architecture/artifact within the NN strategy, not the removed SMC/legacy paper strategies.

Proposed settings additions: `nn_model_id` and `nn_model_bundle_path`. Preserve `nn_model_path` for existing Parente/custom-NPZ users through explicit compatibility rules. Missing new fields must select current behavior. Reject ambiguous combinations rather than choosing silently. Treat manifest-declared timeframe/lookback/feature schema as immutable model properties, not freely editable Settings numbers that can invalidate weights.

Manifest must contain schema version; architecture ID; display name; immutable artifact ID; weight/transform/member hashes; input/output shapes; ordered features/groups; class order; label version/horizon; timeframe; lookback/warm-up; per-asset coverage and units; frozen scalers; missing-data policy; inference runtime/opset; training dependencies/seeds/hyperparameters; dataset provenance/hashes; split ranges; and validation report hash. Record the latest timestamp used for **any** fitting, tuning, calibration, ensemble/model selection, separately from the untouched test range. Conservative live eligibility must begin after all model-development and promotion evidence used to choose the artifact.

Identity must cover weights, preprocessing, decision policy and ensemble membership, not just the display name or one weights file. Resolve model paths relative to the saved settings directory as today. Validate all files, tensor shapes, finite values, outputs, coverage and dependency availability before applying. Load one immutable bundle and use it until Apply/restart; file replacements do not hot-reload one consumer independently.

The adapter should expose model identity, metadata, supported assets, per-asset cutoffs, required history and a prediction method. Retain current prediction fields: `label`, `probabilities`, `features`, `signal_end`, `model_id`. Add schema/version/architecture metadata without reinterpreting old records. A sequence model's displayed `features` should be the final feature row plus lookback metadata; avoid writing a 64x16 tensor into every trade or API response. Keep reproduction inputs in a separate bounded audit artifact if required.

Cache identity must include applied artifact, asset and signal candle. Causally snapshotted auxiliary inputs need a defined revision identity; do not change a candle's prediction halfway through execution because a later provider revision arrived. A failed input/model lookup is unavailable, not HOLD, and must not fabricate probabilities.

## 8. Repository change map for the implementer

Line numbers are preparation-time navigation hints; verify function names against the current tree.

| Existing location | Required work |
| --- | --- |
| `adaptive_crypto/neural.py:95`, `NeuralModel` | Preserve Parente loader/math and add a compatible adapter boundary. Avoid making fixed 36-feature NPZ checks accept arbitrary architectures. |
| New `adaptive_crypto/neural_models.py`, `neural_features.py` | Proposed registry, typed metadata/prediction validation, frozen sequence features and candidate inference adapters. Names can change coherently. |
| New offline training/data modules under `tools/` or a dedicated package | Dataset ingestion, manifest generation, chronological splits, training/export, reports and replay commands. Do not embed training in request handlers. |
| `adaptive_crypto/neural_engine.py:23`, `read`; `:54`, `evaluate` | Replace fixed 100-bar/model assumptions with adapter requirements where necessary; preserve common reader, cutoff, quote/window checks, unavailable handling and candle consumption. |
| `adaptive_crypto/runtime.py:88`, `_scan_neural_paper` | Keep required NN data/inference before executable quote/time checks and before SMC/GEX/advisory work. Bound inference for all assets inside the configured window. |
| `adaptive_crypto/runtime.py`, Home/Positions reads | Keep all active consumers on the same applied engine. Do not reintroduce independent hot-reloads through `PositionNeuralReader`. |
| `adaptive_crypto/core.py`, `Rules` and application settings load | Add strictly validated model selection with backward-compatible defaults; retain NN-only application mode and `nn_limitations`. |
| `adaptive_crypto/settings_editor.py:58`, `validate_document`; `:104`, `check_restart` | Validate bundle selection through the same factory used at startup and Apply; saved/applicable/unsupported states need truthful errors. |
| `adaptive_crypto/administration.py:202`, `_apply_neural` | Prepare and fully validate replacement before backup/commit/swap. Preserve old runtime and records on failure. Keep maintenance locking and queued-event behavior. |
| `adaptive_crypto/neural_ledger.py`, JSON/SQLite codec/migration | Preserve historical entry metadata, stops, costs, consumed candles and outbox atomicity. Version additional prediction metadata; do not reset balances per model. |
| Settings template/JS, neural dashboard and shared probability widgets | Selector, availability/report metadata, saved/applied state and dynamic feature descriptions. Remove hard-coded claims that every network has 36 measurements. |
| `adaptive_crypto/neural_tools.py:118`, `simulate` | Keep documented legacy CLI behavior; add an explicitly separate comparable multi-asset evaluator rather than silently changing historical reports. |
| `tools/source_release.py`, model packaging and Windows specification | Extend narrow artifact/dependency allowlists deliberately when bundling candidates; exclude datasets, training checkpoints and credentials. Test packaged inference if installers are built. |

Proposed CLI operations are **new work, not commands that already exist**: ingest/validate data, train one/all candidates, export/validate bundle, compare candidates, inspect report, and run read-only shadow predictions. Each needs useful progress, nonzero failure exits and resume behavior. No training/retraining scheduler is required for the first release. Document controlled retraining; never update live weights automatically.

## 9. Execution invariants and failure cases

Preserve completed 4H signals, valid model/candle/clock/quote checks, fresh quote after inference, configured execution window, no historical backfill, and one execution opportunity per signal candle regardless of model changes. A model switch must not reset `last_signal_end`, reopen a consumed BUY, replay Telegram alerts, purge history, or change balances.

With limitations OFF, each new BUY uses available paper cash including fees; new trades have no automatic stop; SELL closes all active lots for the asset. With limitations ON, existing position/risk/spread/allocation/minimum rules apply. Trades retain their original stop policy/costs after mode or model changes. Existing stops continue when prediction data is unavailable. Fees and accounting do not disappear when limits are off. Preserve these as execution policy, outside the model adapters.

Measure per-asset and total critical-path latency on the actual Windows host. Set an engineering target comfortably below the 60-second window (for example, under 2 seconds total warm inference for the four current assets); record the measurement rather than treating it as promised. Training and optional research/data work must not starve this path. Missing optional packages must leave Parente loadable; a selected unavailable candidate should fail clearly without silently switching models.

## 10. Tests and acceptance criteria

Add meaningful regression tests before activating new behavior:

1. **Causality:** changing all future bars leaves features/predictions at t unchanged; train-only scalers; no forward labels in inputs; purge at split boundaries; unknown label tails removed; recursive indicator and label initialization fixed; batch-training versus live rolling-history fixtures agree; auxiliary as-of joins if implemented.
2. **Numerics/export:** fixed input yields native/export agreement within declared tolerance; finite probabilities sum to one; class order and tie behavior fixed; malformed or mismatched manifest rejected. Each candidate must execute its own architecture/weights.
3. **Data:** duplicates, revisions, bad units, incomplete candles, missing bars, unsupported assets and warm-up all have explicit tested behavior. Train/inference feature fixtures match.
4. **Registry/selection:** invalid bundle, missing dependency or hash mismatch preserves the prior applied engine; save alone does not apply; discard and failed-save reset work; restart uses saved valid selection; unsupported coverage displayed honestly.
5. **Shared inference:** Home/Paper/Positions agree on model ID, candle, class and probabilities. Replacing a file on disk does not independently alter one consumer.
6. **Execution/persistence:** switch after BUY in the same candle, multiple open lots, SELL all, recorded stops across switches, no-stop positions, both checkbox settings, JSON/SQLite restart/migration/backup, commit failure and exactly-once outbox handling.
7. **Training/replay:** deterministic small fixtures, independent accounting checks, mark-to-market open losses and exit costs, consistent terminal valuation, realistic next-observation fills, base/stressed costs, shared-cash asset ordering and no train/test overlap. Synthetic tests establish pipeline correctness, not model quality.
8. **UI/runtime:** selected/applied names, missing candidate, failed load, offline state, probabilities and coverage at desktop/mobile sizes; no console errors or overflow; inference timing within budget.

Existing critical suites: `test_neural_strategy.py`, `test_shared_neural_model.py`, `test_runtime_neural_timing.py`, `test_nn_limitations.py`, `test_nn_alert_recovery.py`, `test_neural_backtest_chronology.py`, settings/administration, persistence/migration, feed diagnostics, and their browser tests. Run focused suites while building, then the full Python discovery and all repository browser suites. Windows DPAPI integration tests require the real user-profile context with synthetic credentials; never print actual credential files.

Final acceptance means four genuinely trained new single-network candidates plus the retained Parente model, selectable when technically valid, with reproducible reports and a validated optional ensemble. If a data/dependency/compute constraint prevents a trained artifact, finish independent work and report precisely which candidate is incomplete. Do not call an untrained dropdown implementation complete. A negative research result is acceptable; falsely labelling an unproven candidate “best” is not.

## 11. Ordered delivery and activation

1. Recheck repository/process/settings scope and preserve existing staged/unstaged work. Capture a baseline source manifest and state/settings fingerprints. Inspect required data availability and storage/compute before the first long training run.
2. Implement and test registry/adapters with Parente parity, then data validation and causal feature/label/split pipeline.
3. Train and export LSTM, GRU, CNN-LSTM and grouped attention-LSTM; produce per-asset evidence. Evaluate the fixed ensemble without test-set tuning. Keep all training separate from the running dashboard.
4. Implement Settings selection, model metadata, shared consumer integration and isolated paper/shadow replay. Keep current selection and limitations unchanged.
5. Complete regression and disposable browser verification. Present trained artifact identities, coverage and comparison results, including any “no demonstrated winner” outcome.
6. When implementation/activation is authorized by the user, use a complete verified four-namespace backup, coordinated maintenance/restart and before/after fingerprints. Never purge to install a model selector. Activate software with the existing selected model intact; selecting a candidate remains a deliberate user action through Settings.
7. Verify all five pages/API, same-model signals across consumers, preserved records and the unchanged limitations value. Record exact checks and rebuild source/package artifacts only within the implementation's requested release scope.

Do not create an automation, buy data/compute, submit exchange orders, send a test Telegram message, change AI provider or create a separate Codex task as part of this handoff. Ordinary later implementation should continue autonomously through authorized work; ask only for genuinely missing data/provider/compute choices that prevent a concrete next step.

## 12. Preparation record and unresolved facts

Completed here: supplied PDF text/relevant visual review; conclusion/table/reference mapping; original-study identity and terminology check; current source integration audit; read-only saved/applied settings and model-coverage check; this handoff.

Not completed or claimed: training-data acquisition, candidate fitting, original SAM-LSTM reproduction, pretrained-weight/license verification, profitability comparison, UI implementation, full regression rerun, restart or deployment. No model/configuration/ledger/credential changes were made by preparation.

Resolve during implementation: actual historical coverage for every enabled pair, reproducible archive tail coverage, exact framework/export versions, hardware/compute measurements, any optional on-chain/social provider and historical as-of data, and whether additional research materially improves the predeclared benchmark. The paper's future-work suggestions are evidence to assess, not instructions overriding the user.
