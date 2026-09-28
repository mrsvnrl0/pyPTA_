"""Identical inputs must produce identical strategy calculations for every asset."""
import json
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import requests

from adaptive_crypto.core import Rules
from adaptive_crypto.positions import PositionStore
from adaptive_crypto.smc_engine import SMCEngine
from adaptive_crypto.smc_ledger import SMCStore, entry_plan
from test_gex_targets import fixture


class AssetFormulaConsistencyTests(unittest.TestCase):
    def test_shared_formulas_across_all_configured_assets(self):
        settings = json.loads(Path(__file__).with_name("adaptive_crypto_settings.example.json").read_text())
        # Include disabled assets without changing their enabled state in settings.
        assets = {a["name"]: {"symbol": a["name"]+"/USD", "price_decimals": a["price_decimals"]}
                  for a in settings["assets"]}
        with tempfile.TemporaryDirectory() as folder, patch.object(
                requests.sessions.Session, "request", side_effect=AssertionError("Offline test")):
            root = Path(folder)
            for side in ("long", "short"):
                high, low = fixture(side)
                now, price = low[-1].end+1, low[-1].c
                quote = {"bid": price-.00001, "ask": price, "last": price,
                         "asof_ms": now, "volume24_base": 100}
                for gex_enabled, mode in product((False, True), ("smc", "gex_smc")):
                    rules = Rules(strategy_model="smc_video", smc_pivot_strength=1,
                                  smc_stop_buffer_bps=10, smc_tp_sweep_buffer_bps=5,
                                  fee_rate=.001, smc_gex_targets=gex_enabled)
                    baseline = None
                    for asset, config in assets.items():
                        with self.subTest(asset=asset, side=side, gex=gex_enabled, mode=mode):
                            prefix = f"{asset}-{side}-{gex_enabled}-{mode}"
                            store = SMCStore(root/(prefix+".smc.json"), {asset: config}, rules)
                            profile = {"status": "ready", "stale": False, "base": asset,
                                       "spot": price, "asof_ms": now-1000, "calculated_ms": now-1000,
                                       "expires_ms": now+1000000, "regime": "positive",
                                       "call_wall": {"strike": 160 if side == "long" else 200},
                                       "put_wall": {"strike": 100 if side == "long" else 140},
                                       "gamma_flip": None}
                            result = SMCEngine({asset: config}, rules, store).evaluate(
                                asset, quote, high, low, now, gex_profile=profile)
                            self.assertFalse(result["errors"])
                            order = store.snapshot()["assets"][asset]["pending"]
                            self.assertIsNotNone(order)
                            # Identity is unique; the full formula evidence must be identical.
                            order.pop("id")
                            plan = entry_plan(order, order["limit"], rules, store.snapshot())
                            positions = PositionStore(root/(prefix+".positions.json"))
                            positions.open_position({asset: config}, asset, "long", price, 1,
                                                    uuid.uuid4().hex, now-1, target_mode=mode)
                            positions.open_buy_watch({asset: config}, asset, price+5, None, 1,
                                                     uuid.uuid4().hex, now-1, target_mode=mode)
                            positions.monitor(asset, config["symbol"], low, rules, now, quote=quote)
                            positions.monitor_take_profit(asset, config["symbol"], high, low, quote,
                                                          rules, now, gex_context=result["gex_context"])
                            positions.monitor_buy_watches(asset, config["symbol"], high, low, quote,
                                                          rules, now, gex_context=result["gex_context"])
                            saved = positions.snapshot()
                            watch = saved["buy_watches"][0]
                            if mode == "smc":
                                self.assertEqual(watch["target_selection"]["method"], "smc")
                            values = {"order": order, "costs_and_risk": plan,
                                      "holding_target": saved["positions"][0]["take_profit"],
                                      "momentum": saved["watches"][asset]["reading"],
                                      "buy_price": watch["buy_price"],
                                      "buy_selection": watch["target_selection"],
                                      "buy_direction": watch["direction"]}
                            if baseline is None:
                                baseline = values
                            else:
                                self.assertEqual(values, baseline)


if __name__ == "__main__":
    unittest.main()
