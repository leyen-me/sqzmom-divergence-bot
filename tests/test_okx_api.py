"""OKX API 集成测试(真实调用模拟盘)。

无 OKX_API_KEY 时自动跳过; 需要网络。
运行: python3 -m unittest discover -s tests -p 'test_okx_api.py' -v
"""
import os
import time
import unittest

from _paths import ROOT_DIR
from okx_client import OKXClient, OKXError

HAS_KEY = bool(os.environ.get("OKX_API_KEY") and os.environ.get("OKX_API_SECRET")
               and os.environ.get("OKX_API_PASSPHRASE"))
INST = os.environ.get("OKX_TEST_INST", "BTC-USDT-SWAP")


class TestPublicAPI(unittest.TestCase):
    def setUp(self):
        self.c = OKXClient()

    def test_ticker(self):
        t = self.c.ticker(INST)
        self.assertTrue(t)
        self.assertGreater(float(t[0]["last"]), 0)

    def test_candles(self):
        d = self.c.candles(INST, "5m", limit=5)
        self.assertEqual(len(d), 5)
        self.assertGreaterEqual(len(d[0]), 6)   # ts,o,h,l,c,vol,...
        self.assertGreater(float(d[0][1]), 0)

    def test_instrument(self):
        info = self.c.instrument("SWAP", INST)
        self.assertGreater(float(info["ctVal"]), 0)
        self.assertGreater(float(info["lotSz"]), 0)
        self.assertGreater(float(info["minSz"]), 0)


class TestSigning(unittest.TestCase):
    def test_timestamp_format(self):
        c = OKXClient(key="k", secret="s", passphrase="p", demo=True)
        ts = c._ts()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_headers_present(self):
        c = OKXClient(key="k", secret="s", passphrase="p", demo=True)
        h = c._headers("GET", "/api/v5/x", "")
        for k in ("OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP",
                  "OK-ACCESS-PASSPHRASE", "x-simulated-trading", "User-Agent"):
            self.assertIn(k, h)
        self.assertEqual(h["x-simulated-trading"], "1")


@unittest.skipUnless(HAS_KEY, "需要 OKX_API_KEY/SECRET/PASSPHRASE")
class TestPrivateAPI(unittest.TestCase):
    def setUp(self):
        self.c = OKXClient()
        self.hedge = self.c.account_config().get("posMode") == "long_short_mode"

    def test_balance(self):
        self.assertGreaterEqual(self.c.balance_usdt(), 0)

    def test_account_config(self):
        cfg = self.c.account_config()
        self.assertIn("posMode", cfg)

    def test_positions_returns_list(self):
        self.assertIsInstance(self.c.positions(INST), list)

    def test_pending_orders_list(self):
        self.assertIsInstance(self.c.pending_orders(INST), list)

    def test_place_and_cancel_limit(self):
        px = float(self.c.ticker(INST)[0]["last"])
        far = round(px * 0.85, 1)               # 远离市价, 不会成交
        info = self.c.instrument("SWAP", INST)
        sz = float(info["minSz"])
        pos_side = ("long" if self.hedge else None)
        r = self.c.place_order(INST, "buy", "limit", sz, td_mode="cross",
                               pos_side=pos_side, px=far)
        self.assertEqual(r[0]["sCode"], "0")
        oid = r[0]["ordId"]
        try:
            pend_ids = [p["ordId"] for p in self.c.pending_orders(INST)]
            self.assertIn(oid, pend_ids)
        finally:
            self.c.cancel_order(INST, ord_id=oid)
        time.sleep(0.5)
        pend_ids = [p["ordId"] for p in self.c.pending_orders(INST)]
        self.assertNotIn(oid, pend_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
