# pyPTA

pyPTA combines a live market dashboard, an NN paper simulation, and a tracker for trades you place yourself. It does not submit exchange orders.

## Home: market context

Home always shows enabled assets, live Kraken prices, 4H candles, visible NN BUY / HOLD / SELL probabilities, momentum, SMC entry and exit guidance, and Deribit GEX charts including gamma across index prices. It does not read paper balances, open orders or consumed paper setups to decide what market information to show.

The SMC guide shows the lowest available entry among the current qualified setup's methods, the highest eligible untaken upper liquidity target in the available history, and the structural stop. These are strategy measurements, not promised future prices. Pending levels mean that current candles, price or qualification are missing. Passed checks and remaining prerequisites are in collapsible qualification lists.

A separate **4H swing structure** panel shows the latest confirmed wick highs/lows and protected levels. Pivots require strictly lower highs or higher lows on two candles on each side and become usable only after the second following candle closes. A close above a confirmed swing high protects the lowest intervening wick; a close below a swing low protects the highest intervening wick. New minor pivots do not replace protected levels. Wick-only excursions are potential sweeps, not confirmed breaks. These 4H measurements are also shown as context checks and do not gate the configured 30M/5M (or 15M/1M) SMC entry model or NN paper trades.

NN probabilities describe the latest completed **4H candle**, not a tick-by-tick forecast. Quotes refresh between model updates. Missing or stale inputs show waiting states.

Home, Positions, Paper Trading and Trade Records update their readings in place. Charts, open disclosures and form edits remain intact. Editing pauses the surrounding page updates while chart prices and expiry labels continue to update. A settings or database change reloads the page when no form is being edited; otherwise a notice asks you to copy edits and reload before submitting an expired form.

## Paper Trading: NN only

The **NN limitations** checkbox in Settings starts on: BUY opens one simulated spot position per asset subject to cash, risk, spread and allocation limits. HOLD and repeated BUY retain it. The configured NN protective stop remains active between classifications; its history monitoring uses fixed 5M candles. SMC and GEX neither approve nor reject paper orders.

Uncheck **NN limitations**, save and apply to remove position-count, risk, allocation, equity-floor, spread and minimum-order limits. Each new completed 4H BUY uses all available paper cash including the entry fee, with no automatic stop on that new trade. The first eligible BUY can use the whole balance; further entries need available cash. SELL closes all open NN positions for that asset in either mode. Fees, slippage, fresh-data checks and once-per-candle execution still apply. Existing trades retain their recorded stop policy and costs when the checkbox changes.

The execution window after each 4H close is controlled by NN signal max age. Expired entries and exits are not backfilled. Existing recorded fills, stops, costs and consumed candle identities remain fixed.

Legacy, combined and SMC paper strategies are no longer selectable or launched by the application. Their saved records remain available in Trade Records, including open records explicitly marked as no longer monitored.

## Positions: your actual trades

Every monitored position has three separate guides:

- **NN:** an adverse SELL prediction for a long (BUY for a short) gives an exit signal. Green TAKE PROFIT means the current executable price is profitable relative to your entry; red STOP LOSS means at or beyond entry in the losing direction. Costs are excluded. Supporting and neutral predictions show HOLD.
- **SMC TP1:** the nearest eligible untaken SMC liquidity target, including your sweep extension.
- **SMC + GEX TP2:** a farther eligible SMC target supported by current GEX evidence. It remains pending when no supported target exists and retries when data becomes available. An SMC-only fallback is never saved as TP2.

Both target guides use your editable position stop. You can enter it when adding a position or use the suggested SMC stop afterward. The suggestion only becomes your stop when saved. Target hits do not override NN guidance, and an NN exit does not change either numerical target.

Saved target levels stay fixed after selection. Settings change future selections; they do not move an existing target. The two targets have separate alerts. Stops take precedence over target alerts. A position remains open until you record its actual exit.

Position structure alerts now require an adverse completed **4H** close through a protected low for a long or a protected high for a short. They identify the recorded position, entry price, protected level and confirming close. They replace the former general momentum-change notifications; favourable changes and wick sweeps do not alert. The **Structure / NN alerts** control applies to these alerts and NN alerts. Startup establishes a baseline, each breach alerts once per eligible open position, and old events are not replayed when a position is added or alerts are re-enabled. Structural messages use a candle reference rather than a live exit quote and expire from the delivery queue after five minutes. Protected levels are saved across restarts and rolling candle windows. Your editable stop and TP alerts remain independent.

Momentum on the display still uses the configured SMC entry timeframe. The requested display colours are **bearish green / bullish red**; those colours are independent of the red/yellow/green exit lights.

## Settings and backup

Settings are grouped into paper account/risk, NN simulation, and SMC market analysis. Both forms of analysis run together; there is no strategy selector. Save, then apply changes.

**Settings → NN model** now selects the applied classifier within the NN paper strategy: bundled Parente 5/2, four genuinely trained LSTM/GRU/CNN-LSTM/grouped attention-LSTM candidates, or their fixed equal-weight probability ensemble. The catalogue shows each immutable version, supported assets, weight-training and conservative live-eligibility cutoffs, and a validation report. Saving a choice does not switch the running model; use **Apply** after reviewing the saved/applied labels. The independent **NN limitations** checkbox retains its current value. Existing positions keep their recorded stops and costs; a newly applied model controls future classifications and exits. Candidates without valid weights, runtime dependency or required asset coverage are unavailable, and applying one that cannot classify an active paper position is blocked. No new model was selected automatically: the [controlled evaluation](docs/NN_CANDIDATE_EVALUATION.md) found **no demonstrated winner** over Parente under its predefined evidence gate.

A backup replaces **backup/latest.zip**, keeping one complete backup of settings and trading data. Existing historical backup folders are not deleted. Applying settings and the existing purge operation take backups. Purge is optional and explicitly clears records; it is not needed for this update.

Settings includes an expandable **Feed status** panel for Kraken, NN, SMC and Deribit GEX. It reports last success, current errors, the most recent failure after recovery, and retry eligibility where known. Reading diagnostics does not request new provider data. History is retained for the current running configuration.

To authenticate Deribit market-data requests, open **Settings → Deribit connection** on the computer running pyPTA (`http://127.0.0.1:5000/settings`). Enter your production **Client ID** and **Client Secret**, then save the connection. The status shows when an authenticated options fetch succeeds; rejected credentials remain visible as a connection error. Tokens renew automatically. This uses the existing options and index feeds only. The [Deribit authentication guide](https://docs.deribit.com/articles/authentication) documents the Client ID/Secret flow and bearer tokens.

Saved Deribit credentials are encrypted for your Windows user in `deribit-credentials.dat`, beside the saved settings. The form never reads them back. They are excluded from settings exports, backups and source releases. Saving a connection takes effect without restarting. **Use public requests** disables the saved connection, including any environment fallback. For environments without Windows credential storage, set both `DERIBIT_CLIENT_ID` and `DERIBIT_CLIENT_SECRET` before launch. A saved connection takes precedence over these variables; an incomplete pair reports an error.

**Settings → AI commentary** supports **OpenAI** and **Google Gemini**. Choose a provider, enter a text model ID available to your account, and save its API key. Each provider retains its own key and model; leave the key blank when switching back to an already configured provider. The selection supplies the Home market summary and optional signal commentary. The bundled NN model, deterministic signals, and order simulation remain separate; NN trades continue to queue Telegram alerts rather than signal-commentary events.

**Home → Crypto market pulse** researches the general crypto market, recent news, original public X/Twitter posts and official updates every **15 minutes**, using the selected provider's paid generation and web-search quota. Choose a text model supporting [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search) or [Gemini Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search). The panel shows inline source links and the price-snapshot timestamp; Google search suggestions appear when supplied. Web search can miss or delay public posts, and the brief is instructed to distinguish reported developments from inferred price catalysts. Responses without usable search citations are withheld.

The research worker runs independently of scans and alerts and sends only public tracked-asset quotes and explicitly timestamped candle baselines. It does not send positions, balances or alert records. Fresh quotes are required before a request. **Refresh summary** requests an early update, limited to once per minute from the local dashboard. Multiple tabs share one result and one worker. Disable the AI connection to stop future research requests. Provider changes discard the old provider's result; failures preserve the prior brief with its timestamp and stale/error label. Results stay in memory for display; only the last-request time and provider/model are saved in `market-brief-timing.json` to prevent duplicate automatic charges after restart. After a restart the panel may wait until the next scheduled update, or you can refresh it manually.

**Settings → Telegram alerts** accepts a bot token and chat ID. Leave either field blank to retain its existing value. Save takes effect after any current delivery finishes, without restarting or replaying completed alerts. **Disable** stops future requests and retains encrypted credentials; Save enables the connection again. **Check credentials** retrieves AI model information or Telegram bot/chat details without generating commentary or sending a message. It does not establish generation quota or guarantee Telegram delivery permissions.

AI and Telegram credentials are protected for the current Windows user in `ai-credentials.dat` and `telegram-credentials.dat`, beside the settings, and are excluded from backups and source archives. Both forms are editable only through the local loopback dashboard address. Existing environment settings remain supported until locally overridden: `AI_PROVIDER` (`openai` by default, or `gemini`), `OPENAI_API_KEY` / `OPENAI_MODEL`, `GEMINI_API_KEY` / `GEMINI_MODEL`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Both AI providers use native HTTP APIs and need no optional SDK. See the official [OpenAI Responses documentation](https://developers.openai.com/api/docs/guides/migrate-to-responses), [Gemini API reference](https://ai.google.dev/api), and [Telegram Bot API](https://core.telegram.org/bots/api).

Use **Restore a backup → Preview backup** to inspect the backup date, saved settings and record counts. Confirming restores the complete SQLite study and saved settings; the current study is first saved to **backup/latest.zip**. Every archived namespace is restored, including inactive strategy histories. Historical undelivered alert identities are retired so the next scan cannot send them again. Interrupted restores recover the previous settings and database at startup. Preview expires after ten minutes and must be repeated if the archive or saved settings changes. Guided restore requires a compatible complete SQLite backup; it does not partially restore JSON archives. A backup may contain saved edits that were not yet applied; the preview identifies these separately from the archived active settings.

Run from this project folder:

```powershell
.\.venv\Scripts\python.exe adaptive_crypto_dashboard.py --host 127.0.0.1 --port 5000
```

For a fresh Python environment, install requirements.txt and requirements-neural.txt. The bundled frozen model needs the neural dependencies. Existing launcher scripts and Windows desktop startup use the same NN-only application configuration.

To use an installed candidate or ensemble, also install `requirements-candidate-runtime.txt` in the dashboard environment; it pins the validated ONNX Runtime CPU exporter target. Offline retraining uses a separate environment from `requirements-candidate-training.txt` and the [evaluation/reproduction guide](docs/NN_CANDIDATE_EVALUATION.md). Parente can run without ONNX Runtime.

## Implementation and verification

- Market analysis: adaptive_crypto/market_analysis.py
- Independent position guides: adaptive_crypto/position_guidance.py
- NN simulator: adaptive_crypto/neural_engine.py
- Runtime orchestration: adaptive_crypto/runtime.py
- Pages and settings: adaptive_crypto/templates, adaptive_crypto/web.py

Run the offline regression suite:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -q
node --test test_gex_ui.js test_nn_charts_ui.js test_feed_status_ui.js test_deribit_ui.js test_connection_settings_ui.js
```

The browser regression suite is `node --test test_live_refresh_ui.js` and requires Playwright with Chromium or Chrome. See [source baseline and release verification](docs/RELEASE_BASELINE.md) for setup, pinned runtime dependencies and deterministic source archives. Source archives exclude local settings, trading data, backups and generated installers. Existing Git staging is preserved.

See [NN model details](docs/NEURAL_STRATEGY.md) and [GEX formula details](gex_target_audit/FORMULA.md). Those research notes may describe older simulator configurations; the application behavior above is current. The pre-separation README is retained as [historical documentation](docs/HISTORICAL_DASHBOARD.md).

© 2026 EternuLL Organisation
