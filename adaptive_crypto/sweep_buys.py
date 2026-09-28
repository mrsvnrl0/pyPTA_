"""Turn a qualified bearish SMC projection into a lower spot-buy limit."""
import copy
from math import isclose

from .core import DataError, check, finite
from .gex_targets import select_target, validate_selection


def strategy_key(order):
    return order.get("source_strategy", "smc_"+order["side"])


def validate_sweep_buy(order):
    if order.get("entry_origin") != "bearish_sweep":
        return
    source = order.get("source_signal")
    if (not isinstance(source, dict) or source.get("side") != "short" or order["side"] != "long"
            or order.get("source_side") != "short" or order.get("source_strategy") != "smc_short"):
        raise DataError("Invalid bearish spot-buy source")
    buy = finite(order["limit"], "sweep buy", 1e-15)
    level = finite(order.get("buy_liquidity_price"), "buy liquidity", 1e-15)
    validate_selection(order.get("buy_target_selection"), level, "short")
    buffer = finite(order.get("buy_sweep_buffer_bps"), "buy sweep buffer", 1e-15)
    stop_buffer = finite(order.get("stop_buffer_bps"), "buy stop buffer", 1e-15)
    if (buffer > 100 or stop_buffer > 100 or not buy < level < order.get("reference_price", 0) < order.get("source_stop", 0)
            or order.get("reference_price") != source.get("limit") or order.get("source_stop") != source.get("stop")
            or buy != source.get("target") or level != source.get("target_liquidity_price")
            or not isclose(buy, level*(1-buffer/10000), rel_tol=1e-12)
            or not isclose(order["stop"], buy*(1-stop_buffer/10000), rel_tol=1e-12)):
        raise DataError("Spot-buy limit or stop does not match its saved bearish sweep")


def sweep_buy(source, high, low, quote, rules, now, gex_context=None):
    if source["side"] != "short":
        raise DataError("A lower sweep buy requires a bearish source setup")
    if quote["bid"] >= source["stop"]:
        raise DataError("Bearish source is beyond its invalidation stop; wait for a new setup")
    buy = source["target"]
    # All exit evidence is known when this new limit is placed, never looked ahead.
    upper = select_target(high, low, rules, "long", max(buy, quote["ask"]), now, gex_context)
    if upper is None:
        raise DataError("Lower spot-buy level identified; waiting for an untaken upper liquidity high for its SELL target")
    stop = buy*(1-rules.smc_stop_buffer_bps/10000)
    target = upper["price"]*(1+rules.smc_tp_sweep_buffer_bps/10000)
    if not 0 < stop < buy < target:
        raise DataError("Spot buy requires stop below buy price below SELL target")
    order = copy.deepcopy(source)
    order.update(side="long", source_side="short", source_strategy="smc_short", entry_origin="bearish_sweep",
                 source_signal=copy.deepcopy(source), reference_price=source["limit"], source_stop=source["stop"],
                 buy_liquidity_price=source["target_liquidity_price"], buy_sweep_buffer_bps=source["target_sweep_buffer_bps"],
                 limit=buy, stop=stop, stop_basis="buy_level", stop_buffer_bps=rules.smc_stop_buffer_bps,
                 target=target, target_ms=upper["bar_ms"], target_liquidity_price=upper["price"],
                 target_sweep_buffer_bps=rules.smc_tp_sweep_buffer_bps, gross_r=(target-buy)/(buy-stop),
                 buy_target_selection=copy.deepcopy(source.get("target_selection")), target_selection=upper["target_selection"])
    order["checks"] = [c for c in order["checks"] if c["key"] not in {"smc_target", "smc_stop"}]
    order["checks"].extend([
        check("smc_buy_level", "Bearish sweep becomes the SPOT-BUY limit", True,
              {"reference_price": source["limit"], "liquidity_price": source["target_liquidity_price"], "buy_price": buy},
              "Buy at the lower liquidity sweep; no short sale or purchase at the higher reference", source["target_ms"]),
        check("smc_stop", "Spot-buy stop below the buy level", True, {"entry_price": buy, "stop_price": stop},
              f"Buy price × (1 − {rules.smc_stop_buffer_bps:g} / 10000)", source["signal_ms"]),
        check("smc_target", "SELL take-profit beyond the next untaken upper liquidity high", True,
              {"liquidity_price": upper["price"], "target_price": target},
              f"High × (1 + {rules.smc_tp_sweep_buffer_bps:g} / 10000)", upper["bar_ms"]),
    ])
    return order
