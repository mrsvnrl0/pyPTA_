# GEX/SMC target selection

Enabled for paper targets with `smc_gex_targets: true`. Recorded positions and
BUY watches save their own `target_mode`: `smc` (default) or `gex_smc`, independently
of the paper setting and identically across assets.

The selector ranks existing untaken confirmed SMC liquidity pools. It selects
the nearest pool with an eligible GEX match; if there is no match, it uses the
existing SMC target calculation. A farther aligned pool can replace the nearest
unaligned pool. This is a configurable heuristic, not a backtested improvement.

| GEX level | SMC counterpart | Eligible target preference |
| --- | --- | --- |
| Call wall above price | BSL or fresh premium order block | Positive modeled gamma; BSL within the alignment tolerance, or both the wall and target liquidity inside the block. |
| Put wall below price | SSL or fresh discount order block | Positive modeled gamma; SSL within the tolerance, or both the wall and target liquidity inside the block. |
| Gamma flip | Confirmed MSS | Latest completed entry-timeframe close reverses the prior non-neutral structural direction, crosses the flip, and breaks a structure level within tolerance. Prefer the next eligible liquidity pool in that direction. |

The last confirmed setup-timeframe high/low define the dealing-range midpoint.
A premium block must be entirely above it; a discount block entirely below it.
Already revisited or invalidated blocks cannot qualify. A continuation BOS,
neutral baseline, wick-only flip touch, or equality is not a confirmed flip MSS.
Flip confluence maps the current options snapshot to the latest completed MSS;
it does not claim that historical dealer positions were known at that candle.

The new parameters are explicit automation choices:

- `smc_gex_alignment_bps: 10.0`: maximum point-to-GEX distance, relative to the
  GEX level (0.10%). Order-block alignment requires actual containment.
- `smc_gex_max_basis_bps: 50.0`: maximum difference between the Deribit index
  and the fresh Kraken bid/ask midpoint (0.50%). Original venue prices are used.
- `smc_tp_sweep_buffer_bps: 5.0`: remains the final 0.05% extension beyond the
  selected liquidity pool. Long TP = high × 1.0005; short TP/lower buy = low × 0.9995.

Both GEX tolerance settings accept finite, non-negative JSON numbers without an
upper cap. Values remain basis points: 200 means 2%, and 500 means 5%. The separate
stop, sweep-buffer and take-profit-alert limits are unchanged.

GEX observations and calculation times must be no more than five minutes old,
must not be future-dated, must match the asset, and must precede included option
expiry. Current completed SMC candles and a fresh executable quote are required.
Negative/neutral-gamma wall overlaps remain context; they do not qualify as
support/resistance target preferences. Missing or invalid GEX uses SMC fallback.

The GEX preference covers paper targets, including the lower entry and upper
exit of bearish-source spot paper buys. Recorded holdings and automatic SHORT
Buy Watches use their selected method: SMC selects nearest liquidity; SMC + GEX
prefers eligible confluence, falling back to nearest SMC if unavailable. Holdings
apply this to take-profit; watches apply it to the lower buy level. The scanner
fetches options for personal GEX selections even when paper GEX is disabled.
Existing saved levels remain fixed, with no migration. Qualified paper
entries are re-evaluated prospectively at order placement; an already-taken
original target cannot be revived by a farther GEX level. Manual watch prices
and saved targets remain fixed across GEX refreshes and preference changes.
New selections retain the reason, matching POI, GEX price and observation time.

The options feed refreshes in background threads through a cache shared with
the chart. Network delays cannot block exit processing. On a cold start, the
first scan can use SMC fallback while options warm up; that saved target remains
fixed. The chart labels up to eight nearest/matched POIs. The three-column table
shows only the most recently formed matching SMC point for each GEX level, using
its pivot timestamp, with that point's alignment explanation. The full candidate
set remains available to target selection and in the API. Browser expiry removes confluence highlights
even when editing a form pauses page reloads.

Sources reviewed: [FlashAlpha playbook](https://flashalpha.com/articles/gex-trading-system-playbook-negative-gamma-flip-rules)
for regime-dependent wall behavior; the supplied YouTube URL and ACY page were
not readable through the available web tool. The existing options estimator is
a signed open-interest proxy, not observed dealer inventory. No change was made
to its call-positive/put-negative assumption or exchange coverage.
