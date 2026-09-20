"""Engine-side guards added after the 2026-09-20 review.

- discard_unbound_fill(): an entry the bridge decided not to send must not
  linger as a ghost ticket (it counted towards MAXPOS/margin and its stop
  became the run's stop).
- sl_changed must be true when ANY ticket's stop moved, not only the
  primary's: a stacked add trails on closes that leave the primary alone and
  the broker has to receive that SL.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mt5_live_engine as eng  # noqa: E402
from test_engine_rules import LIVE_ENV, _Feed, set_env  # noqa: E402


class DiscardUnboundFillTests(unittest.TestCase):
    def setUp(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        self.e = eng.LatestModsEngine()
        self.e.balance = 2000.0
        self.f = _Feed(self.e).warm()

    def test_unsent_entry_is_forgotten_and_spread_refunded(self):
        bal = self.e.balance
        r = self.f.bar(4002, 4010, 4001, 4008, 3980, 20.0)
        self.assertEqual(r["filled_action"], "BUY")
        self.assertEqual(len(self.e.positions), 1)
        self.assertEqual(self.e.discard_unbound_fill(), 1)
        self.assertEqual(self.e.positions, [])
        self.assertEqual(self.e.pos, 0)
        self.assertAlmostEqual(self.e.balance, bal)
        r2 = self.e.result_after_discard(r)
        self.assertIsNone(r2["filled_action"])
        self.assertEqual(r2["n_total"], 0)
        self.assertEqual(r2["position"], "FLAT")
        self.assertEqual(r2["action"], "NONE")
        # Calling it again is a no-op.
        self.assertEqual(self.e.discard_unbound_fill(), 0)

    def test_bound_ticket_is_never_discarded(self):
        self.f.bar(4002, 4010, 4001, 4008, 3980, 20.0)
        self.assertTrue(self.e.bind_ticket(555))
        self.assertEqual(self.e.discard_unbound_fill(), 0)
        self.assertEqual(len(self.e.positions), 1)

    def test_only_this_push_is_discarded(self):
        self.f.bar(4002, 4010, 4001, 4008, 3980, 20.0)
        self.e.bind_ticket(1)
        r = self.f.bar(4008, 4014, 4007, 4012, 3980, 20.0)  # stacked add
        self.assertEqual(r["filled_action"], "BUY_ADD")
        self.assertEqual(len(self.e.positions), 2)
        self.assertEqual(self.e.discard_unbound_fill(), 1)
        self.assertEqual([p.ticket for p in self.e.positions], [1])
        self.assertEqual(self.e.pos, 1)


class SlChangedTests(unittest.TestCase):
    def test_add_trailing_alone_flags_sl_changed(self):
        set_env({**LIVE_ENV, "ENTRY_EVERY_CANDLE": "1"})
        e = eng.LatestModsEngine()
        e.balance = 2000.0
        f = _Feed(e).warm()
        f.bar(4002, 4010, 4001, 4008, 3980, 20.0)          # primary, best 4008
        e.bind_ticket(1)
        f.bar(4008, 4030, 4007, 4028, 3980, 20.0)          # add; primary best 4028
        e.bind_ticket(2)
        # Push the primary's stop far ahead so a later close cannot move it.
        pri, add = e.positions[0], e.positions[1]
        pri.best_price, pri.sl = 4100.0, 4100.0 - pri.tsl
        add.best_price, add.sl = 4000.0, 4000.0 - add.tsl
        pri_sl_before = pri.sl
        r = f.bar(4028, 4031, 4027, 4029, 3980, 20.0)      # close 4029: add trails, primary does not
        self.assertAlmostEqual(pri.sl, pri_sl_before)
        self.assertAlmostEqual(add.sl, 4029 - add.tsl, places=6)
        self.assertTrue(r["sl_changed"])
        self.assertEqual(r["n_total"], 2)


if __name__ == "__main__":
    unittest.main()
