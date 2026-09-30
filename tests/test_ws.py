"""WebSocket 行情解析测试(不联网)。"""
import json
import unittest

from _paths import ROOT_DIR
from ws import OKXFeed


class TestOKXFeedParse(unittest.TestCase):
    def _candle(self, confirm):
        return json.dumps({"arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
                           "data": [["1000", "10", "12", "9", "11", "0", "0", "0", confirm]]})

    def test_price_from_candle(self):
        f = OKXFeed("BTC-USDT-SWAP", "1m")
        f._handle(self._candle("0"))
        self.assertEqual(f.price, 11.0)
        self.assertEqual(f.drain_closed(), [])      # 未收盘不入队

    def test_closed_bar_pushed(self):
        f = OKXFeed("BTC-USDT-SWAP", "1m")
        f._handle(self._candle("1"))
        self.assertEqual(f.drain_closed(), [(1000, 10.0, 12.0, 9.0, 11.0)])
        self.assertEqual(f.drain_closed(), [])       # drain 清空

    def test_ignore_bad_message(self):
        f = OKXFeed("BTC-USDT-SWAP", "1m")
        f._handle("not-json")
        f._handle(json.dumps({"event": "subscribe"}))
        self.assertEqual(f.drain_closed(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
