# Luna implementation handoff: paper-informed NN improvements

Prepared 28 September 2026. **Preparation only: this document does not implement, train, activate or restart anything.** The user requested this new handoff; ignore the unrelated, unfilled `C:\Users\CxN\Documents\ChatGPT\handoff_.md`.

Workspace: `C:\Users\CxN\Documents\ChatGPT\pyPTA`. This brief is for the Luna implementation session when the user starts it. It does not change the model serving the current conversation.

## 1. Implementation objective

Improve the existing candidate experiment and deliver real, reproducible model versions. Implement bounded class-imbalance and learning-rate comparisons for the four existing neural architectures; add a same-history OHLCV MLP candidate and an offline XGBoost control. Preserve the existing selector, original models and all trading behavior. Make successfully trained and technically validated neural versions manually selectable through Save -> Apply, with their evidence limitations visible.

This is an implementation contract, not a request to produce another plan. Complete independent software, regression, training, export and disposable-preview work. An improvement in research quality is a valid outcome even when performance does not improve. Do not manufacture a winner or require a positive result to finish.

The decisions below are engineering choices for pyPTA, not hyperparameters or guarantees from the paper. Keep the scope bounded: no new trading target, automatic model selection, portfolio optimizer, reinforcement learning, paid compute, historical-text ingestion or wholesale dashboard redesign.

## 2. Research basis

Read Gurgul, Lessmann and Hardle, [Forecasting Cryptocurrency Prices Using Deep Learning: Integrating Financial, Blockchain, and Text Data, arXiv:2311.14759v1](https://arxiv.org/html/2311.14759v1#S4.SS2), especially sections 3.3, 4.1-4.3 and 5-6. It compares financial, blockchain and text features, multiple targets and several models. Its aggregate results do not establish recurrent/attention models as best: MLP and XGBoost are important comparators. It applies chronological expanding evaluation and hyperparameter tuning. Minority-class reweighting is used for extrema classification. Its trading simulation omits transaction costs, so its profit figures cannot justify removing pyPTA's costs. Text benefits depend on asset and period; the study does not validate a live Kraken 4H strategy or the SOL/SUI task. These observations motivate the controlled experiments below, not adoption of a published winner.

Keep historical sentiment/on-chain work separate. Without verified publication/availability timestamps and historical coverage, there is no valid branch to train. Current AI summaries, retrospectively discovered posts and future engagement counts must not become past features.

## 3. Verified starting point; recheck before editing

The original selectable-candidate project is already implemented. `docs/GPT6_SOLAR_NN_CANDIDATES_HANDOFF.md` describes its old preparation state and must not be treated as the current implementation status. Read `docs/NN_CANDIDATE_EVALUATION.md` and current source instead.

| Existing component | Current location / behavior |
| --- | --- |
| Causal features and labels | `adaptive_crypto/candidate_features.py`; `tools/candidate_train_pipeline.py`; Kraken/USD H4, 16 features, 64 feature rows, 319 contiguous raw bars, source-convention Parente 5/2 labels, two-bar forward horizon, class order BUY/HOLD/SELL. |
| Training/export | `tools/train_nn_candidates.py`, `tools/candidate_train_pipeline.py`; train-only scalers, purged boundaries, isolated PyTorch training, CPU ONNX export. |
| Existing simple controls | `tools/train_nn_simple_baselines.py`, `test_nn_simple_baselines.py`; actual training-majority and regularized multinomial logistic fits exist. Standalone evaluator placeholder fields do not mean these controls are absent. |
| Evaluation | `tools/evaluate_nn_candidates.py`, `tools/compare_nn_candidate_reports.py`; base/stress costs, limitations ON/OFF, natural exits, terminal-liquidation distinction and paired comparison. |
| Runtime and settings | `adaptive_crypto/neural_models.py`, `neural_engine.py`, `runtime.py`, `settings_editor.py`, `administration.py`; strict registry and shared applied model; Save is distinct from Apply. |
| UI / export checks | `test_candidate_selector_ui.js`, `tools/preview_nn_model_selector.js`, `test_candidate_export.py`, `tools/verify_nn_ensemble_runtime.py`. |
| Source release | `tools/source_release.py`; explicit family/tool/doc allowlists and independent bundle validation. |

Existing trained versions are LSTM `4033451e9329`, GRU `1f83df264a1e`, CNN-LSTM `79f9860590eb`, grouped attention-LSTM `2e29d734b5b6`, and equal-weight ensemble `3801b1dabe20`. Retain every existing bundle byte-for-byte. Parente remains its original NPZ implementation and has no bundled SUI calibration.

Verified during this preparation: saved `nn_model_id` is `grouped_attention_lstm_v1@2e29d734b5b6`; saved `nn_limitations` is `false`. These are observations, not desired defaults. Re-read saved and applied settings independently at implementation time. Never revert either field to match this document. Do not print credentials when inspecting settings/API responses.

The experiment is under `.qa/candidate_runs/final_v1/`, with verified data under `.qa/candidate_data/kraken_2026q2_plus_rest/`. Preserve `experiment.json`, `fold_plan_v2.json`, `validation_folds.json`, prediction files, corrected `*.v2.*evaluation.json`, `candidate_comparison.final.json` and `simple_baselines/`. The old schema-1 fold draft is an audit artifact; use the corrected schema-2 disjoint folds. The archive members have CRC/hash verification; the full approximately 9 GB archive checksum was not independently verified. Do not upgrade that provenance claim.

The published common BTC/ETH/SOL test is approximately 94.63% HOLD. Several networks have zero or one natural exit. The existing conclusion is **no demonstrated winner**. Do not equate accuracy or one profitable terminal liquidation with a useful trading policy.

Prior delivery reported 797 Python tests with two skips and 53 browser tests. These are historical counts, not this task's results. Re-establish current results. The checkout has extensive staged/untracked user work: preserve it, do not reset/clean, and do not commit or push unless requested.

## 4. Evidence contract: the old test has already been observed

The January-September 2026 `final_v1` test results have been inspected and influenced this new experiment. Reusing them is **retrospective research**, not untouched confirmation. Relabeling a file, shifting an overlapping split or adding one new candle does not create an independent final test.

Create `.qa/candidate_runs/paper_research_v2/` as a new immutable run. Refuse collisions with incompatible files; support hash-checked resume of compatible completed work. Before fitting, save a canonical `protocol.json` and its hash, including the following fields:

- Schema/experiment ID; source code and environment fingerprints; data/provenance hashes; assets and exclusions. Freeze a named dependency set for training identity (feature/label/data/split/training/evaluator/export code); record runtime/UI fingerprints separately. Unrelated UI/doc edits do not invalidate fits, but changes to any training dependency do.
- Exact features, target, class order, representation, split boundaries, purges and missing-data policy.
- `evidence_kind` (`development`, `retrospective`, or `prospective_confirmation`), `prior_results_observed: true`, known exposure end and fresh-confirmation status.
- Trial table, seed schedule, sample/class-weight rules, loss aggregation, recipe/seed ranking, inference decision rule, costs and primary limitations mode.
- Fit/time/thread budgets, trial order, interruption/resume rules, report fields and prospective confirmation policy.

For development reuse the **boundaries** of `final_v1/fold_plan_v2.json`, with its train -> inner stopping -> outer scoring separation. Refit each model inside each fold. Never replay a later-trained deployed model on an earlier fold and call it out-of-sample. Fit scalers and class weights using only that fold's fit samples. Purge labels whose forward observation reaches the next split; allow genuinely past feature context at a boundary. Drop sequences crossing gaps without filling candles.

Outer fold scores now select a recipe and must be labelled development selection scores. They are not an unbiased final performance estimate. Preserve the existing 70-80% validation slice for final-fit seed/epoch selection, and exclude the old 80-100% test from all fitting, recipe selection and threshold decisions. An optional single replay of frozen finalists on the old test may be published as retrospective; its results cannot trigger another search or a recommendation badge.

At the end of training, freeze a `confirmation_plan.json` referencing the finalized model hashes. Set confirmation start to the first full H4 bar starting strictly after both the freeze time and the latest market observation inspected for this work. Use a fixed 90-day forward interval; score it once after it ends and labels mature. Carry the 30-natural-exit floor and all existing after-cost/stress gates forward without relaxing them. An inadequate result stays inconclusive. Do not extend the window opportunistically or repeatedly check for statistical significance. This task prepares a manual confirmation command; it does not create an automation or wait 90 days.

Predeclare exactly one primary neural challenger to Parente using the development eligibility/ranking rule below, with family ID as the final cross-family tie-break. Other families are descriptive secondary comparisons and cannot acquire a recommended badge because they happen to win the future interval. If no neural family is development-eligible, record `primary_challenger: null`; all forward results remain descriptive. Freeze the primary comparison to identical BTC/ETH/SOL coverage, costs, limitations mode, paired dates and the existing bootstrap/gates; SUI results are separate because Parente is unsupported there. Never choose the primary after seeing confirmation outcomes. This prevents treating the best of several unadjusted comparisons as a single preregistered test.

Until valid fresh confirmation exists, machine-readable and UI results must say `prospective_confirmation_pending`, `recommended_model_id: null`, and **no demonstrated winner**. Legacy reports retain their original historical meaning. New evidence metadata must never silently default missing/unknown evidence to fresh confirmation.

## 5. Fixed experiment choices

Use the existing four supported research assets BTC/ETH/SOL/SUI and verified USD data. This does not redefine the user's live asset universe. If an asset lacks usable data, record an explicit exclusion and restrict all comparisons to identical coverage; do not substitute USDT or synthetic history. Report Parente comparisons on its common BTC/ETH/SOL coverage separately.

Keep source labels, two-bar horizon, argmax decisions and current recurrent architecture widths/dropout unchanged. No threshold search, resampling, calibration, feature selection or label redesign in this iteration. Report weighted-model scores as uncalibrated softmax outputs, without changing the existing probability tensor interface.

### A. Four existing neural architectures

For each architecture, compare exactly these recipes:

| Recipe | Adam learning rate | Training class weights |
| --- | ---: | --- |
| `r0_reference` | 0.001 | all ones |
| `r1_lower_lr` | 0.0003 | all ones |
| `r2_weighted` | 0.001 | rule below |
| `r3_weighted_lower_lr` | 0.0003 | rule below |

For fit-only counts `n_c` across the three classes and `N = sum(n_c)`, compute `u_c = min(5, sqrt(N / (3 * n_c)))`, then `w_c = u_c / (sum(n_c * u_c) / N)`. The cap applies before normalization; save both vectors and counts. If any fit class is absent, mark that fold/asset trial unavailable, with its reason; never add fabricated samples or infinite weights. Validation/test labels cannot affect this calculation. Unweighted reference uses `[1,1,1]` exactly.

Use weighted cross-entropy for the weighted training objective only. Choose epochs using ordinary unweighted log loss on the disjoint stopping slice so objectives remain comparable. Correct the existing batch-mean aggregation in `fit_model`: sum per-example validation losses and divide by sample count, rather than taking a mean of unequal-sized batch means. For reported weighted training loss, aggregate the weighted numerator and sum of target weights; do not average batch means. Record selected epoch and full bounded history. Preserve reference recipe arguments/default behavior except this documented aggregation correction; existing artifacts remain unchanged.

### B. Same-history MLP

Add family `ohlcv_mlp_classifier_v1`, explicitly distinct from Parente. Input is the same scaled `[batch,64,16]` causal sequence, flattened in chronological row-major order to 1024 values inside the network. Network: `1024 -> 128 -> 32 -> 3`, ReLU and dropout 0.1 after each hidden layer, raw logits at output. Adam learning rate 0.001, weight decay 0.0001. Compare exactly unweighted versus the same fit-only weighting rule. Do not call this an exact paper reproduction.

This MLP becomes selectable only after actual training and the technical checks below. Keep the existing logistic control labelled as a **latest-row 16-feature control**: it does not consume the same history as the MLP/recurrent networks.

### C. Offline XGBoost control

Train one XGBoost family on the same flattened, train-scaled 64x16 history. Fixed CPU `hist`, multiclass `multi:softprob`, three classes, learning rate 0.05, maximum depth 3, subsample 0.8, column subsample 0.8, L2 1, L1 0, maximum 300 boosting rounds, inner-slice unweighted multiclass log-loss stopping with patience 20. Compare exactly unweighted versus the same per-example class weights. Use seed 11 and explicitly predict with the selected boosting iteration range.

Keep XGBoost an offline research control in this iteration. Save its data-only model, class order, recipe, predictions and provenance in the run directory; do not add it to the NN selector or install it in the dashboard environment. Native XGBoost probability output must not be fed to the live ONNX adapter, which expects logits and applies softmax. If the official package is unavailable, record that specific control as blocked and complete neural work independently.

### D. Selection and resource budget

Use seed 11 for recipe trials on both development folds and all covered assets. Freeze primary limitations mode from the current applied setting at protocol creation; replay both modes regardless. Use the existing costs and execution assumptions in the frozen protocol (currently documented as 0.4% fee and 0.05% slippage per side, 10 bps synthetic spread, doubled-cost stress). Do not read mutable live settings during a run.

Choose one recipe per family across assets. Require complete matching coverage for all its fold scores. A recipe qualifies for a trading-oriented development rank only with at least five natural exits across the two portfolio fold replays, positive mean base and stress net returns, and mean sampled drawdown no worse than its fold-matched unweighted reference. Rank eligible recipes by mean stressed net return, then mean macro-F1 across asset/fold pairs, then recipe ID. These small development gates only control recipe choice; they do not replace the 30-exit fresh-confirmation gate or establish statistical significance. If no recipe qualifies, retain that family's unweighted reference recipe as the technical research candidate and report that no development improvement was established. Display all results, including failing trials.

For each selected neural recipe, train per-asset final members using seeds 11, 23 and 37 on the original 0-70% fit slice and disjoint 70-80% stopping slice. Choose seed by lowest sample-weighted unweighted stopping log loss, then smaller seed. Do not cherry-pick by old-test profit. Refit the selected XGBoost recipe once per asset under those same boundaries. Refit majority/logistic controls per development fold and final-fit scope using their existing recipes.

Maximum full fits on four assets: 128 recurrent/hybrid/attention recipe fits, 16 MLP recipe fits, 16 XGBoost recipe fits, 60 final neural seed fits, four final XGBoost fits, and 24 majority/logistic control fits = **248 fits**. A majority frequency calculation counts in that bookkeeping even though it is cheap. Existing completed reference trial results can be reused only with exact code/protocol/data/seed hashes. No extra search dimensions or adaptive trial creation.

Run one training worker with at most four CPU threads; no GPU/cloud purchase. Cap cumulative experiment execution at eight hours, including interrupted/resumed fit time. Also bound a fit to 30 minutes; stop it with `budget_exhausted` rather than marking partial weights complete. Neural fits use maximum 100 epochs, patience 10, batch 64 and gradient norm cap 1. Smoke fixtures may use at most two epochs/two batches and must live outside deployable directories. A smoke result is never a trained candidate. Record elapsed time and incomplete trials; never rank unequal/incomplete coverage as comparable. A resource stop produces a concrete resume command and honest partial status, not silently reduced scientific scope.

## 6. Versioned artifacts and compatibility

Add research functionality in `tools/nn_research_protocol.py` and `tools/train_nn_research.py`, reusing current loaders/features/evaluator rather than forking their semantics. Add a small explicit recipe argument to training construction and persist it everywhere identity is formed. Avoid importing Torch/XGBoost from dashboard modules. Preserve legacy command behavior and strict NPZ validation.

Implement an explicit manifest/report **schema 2** for new neural bundles, with a separate strict validation branch; leave schema-1 validation intact. Schema 2 must carry the frozen recipe, model representation, `output_semantics: logits`, class-weight counts/vector, code/data/protocol fingerprints, actual fit cutoff, model-selection cutoff, evidence kind and confirmation status. Keep the existing input `[1,64,16]` and output `[1,3]` contracts. The artifact identity must cover operational configuration, weights, scalers, classes, policy and cutoffs. Finalize the compact report and hash before installation. Confirmation collected later belongs in a separately hashed evidence report; do not mutate the installed bundle.

Technical selectability requires complete actual training for declared coverage, finite tensors, independent labels/coverage checks, native/export/live-adapter parity, valid hashes and an honest development report. Fresh performance confirmation is a separate requirement for recommendation. Do not fabricate a final-test report just to satisfy a legacy loader gate. Compute live eligibility after every observation used in model selection/retrospective promotion; distinguish this from the date of the last gradient-training sample.

Important compatibility traps to address explicitly:

1. `neural_models.py` currently derives the fixed ensemble member order from all `FAMILIES` except the ensemble. Adding MLP would break the existing four-member ensemble. Replace this derivation with an explicit versioned four-family tuple matching existing manifests. Preserve all old hashes, weights and predictions. Do not add MLP to the old ensemble. No newly tuned ensemble is required in this scope.
2. The comparator has a hardcoded family regex; source-release validation also has family/schema allowlists. Update the exact allowed new family/schema paths without accepting arbitrary names, weakening size/path/hash validation or rewriting old reports. Research reports cannot use the old comparator to evade the new evidence gate.
3. A training subprocess currently can write directly into `adaptive_crypto/models/candidates`. The new workflow must stage fully verified bundles under its run directory first, then copy a completed immutable version into the registry deliberately. Incomplete training, timeouts and smoke runs cannot expose selector entries as valid models.
4. Preserve one `NeuralEngine.read()` applied model/cache across Home, Paper Trading and Positions, NN-first scheduling, candle deduplication, quote freshness, post-inference clock/window checks, stops/costs and pending alerts. Model selection must remain independent of limitations. Unsupported-asset Apply checks must still protect existing positions.

## 7. Required commands and delivery order

Implement the following CLI surface in `tools/train_nn_research.py`; these are **new commands to build**, not claims that they already exist:

```powershell
# Run from C:\Users\CxN\Documents\ChatGPT\pyPTA.
$researchRun = '.qa/candidate_runs/paper_research_v2'
$researchPython = '.qa/candidate_venv/Scripts/python.exe'
& $researchPython -m tools.train_nn_research prepare --data .qa/candidate_data/kraken_2026q2_plus_rest --base-run .qa/candidate_runs/final_v1 --output $researchRun
& $researchPython -m tools.train_nn_research run --output $researchRun
& $researchPython -m tools.train_nn_research verify --output $researchRun
& $researchPython -m tools.train_nn_research publish --output $researchRun
& $researchPython -m tools.train_nn_research confirm --output $researchRun --data .qa/candidate_data/prospective_confirmation
```

`prepare` writes/audits the protocol and sanitized source/settings snapshot; read-only local scope discovery is allowed. `run` supports hash-checked resume and writes trials, frozen finalists, reports and confirmation plan. `verify` validates artifacts and evidence without retraining. `publish` installs only individually complete, verified neural families, lists blocked families explicitly and never changes settings. `confirm` is a future manual command: fail clearly when the fixed prospective interval is missing/incomplete or dataset provenance is invalid; it must not synthesize evidence or launch a service. The final confirmation directory is created only when real future data are available. The eight-hour cap applies across resume calls; increasing it requires an explicit new budget instruction, not a counter reset.

Execute implementation in this order:

1. Inspect repository instructions, current source/status, process executable/command line, saved/applied model/limitations and available training environment. Identify the actual port-5000 source tree. Record only sanitized observations. Read this whole brief and the current evaluation report. No live setting changes or restart is needed for preparation/tests.
2. Capture existing bundle hashes; implement protocol/evidence validation, immutable run/resume behavior and correct loss aggregation. Add focused regressions before training.
3. Implement recipes, MLP and offline XGBoost; add train-only weights, deterministic trials, budget enforcement and same-grid baseline evaluation. Implement schema-2 registry/runtime validation and the explicit old-ensemble membership contract now, so full-adapter parity is available before genuine training. Verify tiny offline fixtures, backward compatibility and exporter integration in the isolated environment. Smoke fixtures stay outside the production registry.
4. Freeze protocol and training-dependency hashes only after that implementation passes focused checks; execute the bounded genuine training run. Finish independent families even if one fails. Check hashes at every resume; changed protocol/data/training-dependency code requires a new run identity, not overwriting an old result. Record later UI/runtime revisions separately and rerun affected parity/integration checks.
5. Produce complete development classification/after-cost reports, selected artifacts, parity and Windows CPU latency/memory measurements. Freeze confirmation plan. Preserve the truthful pending performance verdict.
6. Complete UI/evidence presentation and source-release support against the already-tested schema-2 registry. Publish only complete verified bundles. Show recipe/evidence status, actual fit and eligibility dates, unsupported assets and a report link in Settings. Preserve Save -> Apply and the independent limitations checkbox. Do not add editable model controls to Home.
7. Run relevant regression suites and a disposable desktop/mobile preview with temporary settings/stores and no scanner/credentials. Exercise Save -> Apply for the new MLP and each newly available version; prove all three consuming pages agree and the old ensemble still loads. Review browser errors, horizontal overflow and labels.
8. Write `docs/NN_PAPER_RESEARCH_V2_RESULTS.md` with commands, actual fit counts, elapsed time, versions, hashes, coverage, comparisons, failure reasons and confirmation readiness. Update release allowlists and verify the source archive excludes `.qa`, settings, credentials and trading state. Report readiness; do not automatically restart/apply the live dashboard or choose a new model.

If the user separately authorizes live software activation, recheck active settings and create/verify a complete backup with `tools/backup_live_nn_candidate_activation.py` and `tools/audit_nn_candidate_activation.py`. Confirm all four namespaces (legacy, SMC, neural, positions), fingerprint immutable record facts, and preserve current applied model/limitations through activation. Recheck APIs/pages and fingerprints afterward, distinguishing legitimate concurrent new records from loss. Do not reuse stale backup evidence from this document.

## 8. Meaningful validation and acceptance

Extend current suites and add `test_nn_research.py` for new contracts. Required cases:

- Perturbations beyond each object's allowed information boundary cannot alter it: features ignore later candles; fit-only counts/weights/scalers ignore stopping/scoring/test data; stopping data can legitimately choose epochs, and development scoring data can choose recipes. Perturbing old-test or prospective data must not alter recipes, epochs or seeds. Split purges and gap exclusion hold for every family and control.
- Hand-computable unequal-batch loss totals, including weighted denominator handling; validation loss is invariant to inference batch partition within numeric tolerance. Missing classes, nonfinite weights and invalid labels produce explicit failures.
- Recipe/seed choice is deterministic on identical completed evidence; old-test labels/predictions cannot affect selection. Missing folds/coverage cannot silently win; timeout/resume does not reset budgets or publish unfinished artifacts.
- Changing a data, protocol, code, weight, scaler or operational-policy hash invalidates inappropriate reuse. Evidence kind is mandatory for schema 2. Retrospective data, overlapping confirmation dates and immature labels cannot produce a recommendation. A secondary candidate cannot become the primary after outcomes are observed. Thirty exits alone is insufficient without all other gates.
- Actual MLP and recurrent native logits match exported ONNX within existing `1e-4` logits tolerance, and frozen/native versus full runtime probabilities within `1e-5`; class order, argmax and single softmax are checked. Include multiple assets and nontrivial inputs, not only zeros.
- Existing NPZ and every schema-1 bundle still load; existing ensemble parity/hashes survive adding MLP. Reject tampered schema-2 manifests/reports, path escapes, unsupported outputs and inconsistent cutoffs. Source-release checks cover both schemas.
- Disposable Save changes saved state only; Apply updates all consumers together; failed Apply preserves prior applied model; neither path changes limitations. Existing unsupported-position protections and timing/alert regressions pass.

Reports must include class counts and predicted-action counts, per-class precision/recall/F1, macro-F1, unweighted log loss, Brier/calibration diagnostics and one-vs-rest average precision for BUY/SELL when defined. Explicitly report absent-class metrics as undefined. Include per-asset and common-coverage portfolio returns, stress returns, fees/turnover, sampled drawdown, natural exits and terminal liquidations, seed/fold dispersion and all failed trials. Retain the existing next-H4-open execution proxy limitations; do not claim verified intrabar fill timing from 4H candles.

Useful existing test commands (add the new suites once implemented):

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_candidate_training test_candidate_features test_candidate_evaluation test_nn_simple_baselines test_candidate_registry test_shared_neural_model test_runtime_neural_timing test_source_release -q
.\.qa\candidate_venv\Scripts\python.exe -B -m unittest test_candidate_export -q
.\.venv\Scripts\python.exe -B -m unittest discover -q
node --test test_candidate_selector_ui.js test_nn_limitations_ui.js test_live_refresh_ui.js
```

Use installed Chrome/Playwright paths discovered on this host and the existing preview helper. Do not hard-code a new user's cache path. Run the full existing browser suite once after targeted checks pass. A skipped optional export test in the dashboard environment is not export validation; run actual exports in the training environment. Tests/preview use disposable data and must not send Telegram or paid AI requests.

Pinned training versions currently recorded are in `requirements-candidate-training.txt`; runtime pins are in `requirements-candidate-runtime.txt`. Recheck the actual interpreter and packages. Consult current official [PyTorch weighted cross-entropy](https://docs.pytorch.org/docs/2.14/generated/torch.nn.CrossEntropyLoss.html), [ONNX export](https://docs.pytorch.org/docs/2.14/onnx.html), and [XGBoost parameters](https://xgboost.readthedocs.io/en/stable/parameter.html) before implementation. Record exact validated versions, especially the added offline XGBoost dependency, in a separate research lock file. Do not upgrade the live dashboard environment to satisfy a training dependency or change the working exporter solely because a different API is newer.

Completion requires working research commands, actual trained neural artifacts for every claimed family, reproducible reports, strict registry/selector integration, regression results and disposable preview evidence. No placeholder weights or renamed old models count. A valid outcome is **software and development experiments complete; prospective confirmation pending; no demonstrated winner**. If data/dependencies/compute prevent a subset, finish independent work and identify exactly which fits/artifacts/checks remain blocked. Never call blocked models trained or an unfinished study complete.

The final implementation response should state changed behavior, available new versions/coverage, measured results, actual tests, evidence limitations and activation status. Do not present a long activity log or copy historical test counts as new results.
