"""trader 工具函数测试: 聚合 / 仓位计算 / 历史拉取 / 配置覆盖 / 邮件开关。"""
import os
import unittest
from unittest import mock

from _paths import ROOT_DIR
import trader as T
from strategy import Bar


class FakeClient:
    """按 OKX 语义返回分页 K 线(最新在前)。"""
    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    def candles(self, inst_id, bar, limit=300, after=None):
        d = self.chunks[self.calls] if self.calls < len(self.chunks) else []
        self.calls += 1
        return d


class TestAggregate(unittest.TestCase):
    def test_two_5m_make_one_10m(self):
        bars = [
            Bar(0, 100, 110, 90, 105),
            Bar(300000, 105, 108, 95, 96),
            Bar(600000, 96, 120, 94, 118),
            Bar(900000, 118, 121, 100, 101),
        ]
        agg = T.aggregate(bars, tf_min=10)
        self.assertEqual(len(agg), 2)
        b0, _ = agg[0]
        self.assertEqual((b0.open, b0.high, b0.low, b0.close), (100, 110, 90, 96))
        b1, _ = agg[1]
        self.assertEqual((b1.open, b1.high, b1.low, b1.close), (96, 121, 94, 101))

    def test_closed_flag(self):
        now = 1_700_000_000_000
        bars = [Bar(now // 600000 * 600000, 1, 1, 1, 1)]
        with mock.patch("trader.time.time", return_value=now / 1000):
            agg = T.aggregate(bars, tf_min=10)
        self.assertFalse(agg[0][1])   # 当前桶未收盘


class TestPositionSizing(unittest.TestCase):
    def _trader(self, equity=10000.0):
        t = T.Trader.__new__(T.Trader)          # 跳过 __init__(避免建 client/ mailer)
        t.cfg = {"qty_pct": 0.8, "leverage": 3, "_sim_equity": equity}
        t.dry = True
        t.inst_type = "SWAP"
        t.inst_info = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01"}
        return t

    def test_contract_size(self):
        t = self._trader()
        # 10000*0.8*3 / (0.01*50000) = 48 张
        self.assertAlmostEqual(t.size_contracts(50000), 48.0, places=8)

    def test_below_min_returns_zero(self):
        t = self._trader()
        t.inst_info["minSz"] = "100"
        self.assertEqual(t.size_contracts(50000), 0.0)

    def test_spot_size(self):
        t = self._trader()
        self.assertAlmostEqual(t.spot_size(50000), 10000 * 0.8 / 50000, places=8)


class TestFetchHistory(unittest.TestCase):
    def test_pagination_and_order(self):
        c0 = [["6000", "1", "2", "0", "1", "0"], ["5000", "1", "2", "0", "1", "0"],
              ["4000", "1", "2", "0", "1", "0"]]
        c1 = [["3000", "1", "2", "0", "1", "0"], ["2000", "1", "2", "0", "1", "0"],
              ["1000", "1", "2", "0", "1", "0"]]
        bars = T.fetch_history(FakeClient([c0, c1]), "X", "5m", need=5)
        self.assertGreaterEqual(len(bars), 5)
        ts = [b.ts for b in bars]
        self.assertEqual(ts, sorted(ts))     # 升序


class TestLoadConfig(unittest.TestCase):
    def test_env_override(self):
        with mock.patch.dict(os.environ, {"OKX_LEVERAGE": "5", "OKX_INSTID": "ETH-USDT-SWAP",
                                          "DRY_RUN": "0", "OKX_QTY_PCT": "0.5"}):
            cfg = T.load_config()
        self.assertEqual(cfg["leverage"], 5.0)
        self.assertEqual(cfg["instId"], "ETH-USDT-SWAP")
        self.assertEqual(cfg["qty_pct"], 0.5)
        self.assertFalse(cfg["dry_run"])


class TestMailer(unittest.TestCase):
    def test_disabled_by_flag(self):
        with mock.patch.dict(os.environ, {"ENABLE_EMAIL_MSG": "false",
                                          "MSG_SMTP_USERNAME": "a@qq.com",
                                          "MSG_SMTP_PASSWORD": "x"}):
            from mailer import Mailer
            m = Mailer(log=lambda *a: None)
        self.assertFalse(m.enabled)
        m.send("测试", "内容")   # 不应抛异常


if __name__ == "__main__":
    unittest.main(verbosity=2)
