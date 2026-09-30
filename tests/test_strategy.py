"""策略引擎函数测试: 指标 / 枢轴 / 背离 / 动态移动止损。"""
import os
import unittest

from _paths import ROOT_DIR
from strategy import StrategyEngine, Bar


class TestIndicators(unittest.TestCase):
    def test_linreg_on_perfect_line(self):
        vals = [float(i) for i in range(10)]
        self.assertAlmostEqual(StrategyEngine._linreg(vals, 10, 0), 9.0, places=6)
        self.assertAlmostEqual(StrategyEngine._linreg(vals, 10, 1), 8.0, places=6)

    def test_linreg_flat(self):
        vals = [5.0] * 10
        self.assertAlmostEqual(StrategyEngine._linreg(vals, 10, 0), 5.0, places=6)

    def _mk(self, eng, o, h, l, c):
        eng.bars.append(Bar(len(eng.bars) * 600000, o, h, l, c))

    def test_atr_constant_range(self):
        eng = StrategyEngine(atr_len=14)
        for _ in range(30):
            self._mk(eng, 100, 105, 95, 100)   # TR = 10 恒
        self.assertAlmostEqual(eng.atr(), 10.0, places=6)

    def test_mom_zero_on_flat_market(self):
        eng = StrategyEngine(length_kc=20)
        for _ in range(60):
            self._mk(eng, 100, 100, 100, 100)
        mom = eng._mom_series()
        self.assertAlmostEqual(mom[-1], 0.0, places=6)


class TestTrailingStop(unittest.TestCase):
    def setUp(self):
        self.eng = StrategyEngine(sl_atr=2.0, trail_atr=1.0)
        self.eng.pos_signal = 1
        self.eng.mark_entry(100.0)         # atr() 返回 None -> 用 100*0.005
        self.eng.atr_ref = 10.0            # 手动固定便于验证

    def test_initial_stop(self):
        self.assertEqual(self.eng.stop_level(), 80.0)      # 100 - 2*10

    def test_no_trail_before_activation(self):
        self.assertIsNone(self.eng.on_price(105.0))        # < 100+1*10
        self.assertFalse(self.eng.trail_on)
        self.assertEqual(self.eng.stop_level(), 80.0)

    def test_trail_activation_and_follow(self):
        self.eng.on_price(111.0)                            # >= 110 激活
        self.assertTrue(self.eng.trail_on)
        self.assertEqual(self.eng.stop_level(), 101.0)      # 111 - 10
        self.eng.on_price(120.0)
        self.assertEqual(self.eng.stop_level(), 110.0)      # 120 - 10

    def test_exit_on_trail_hit(self):
        self.eng.on_price(120.0)
        self.assertEqual(self.eng.on_price(109.0), "EXIT")  # <= 110

    def test_long_hard_stop_exit(self):
        self.assertEqual(self.eng.on_price(79.0), "EXIT")   # <= 80

    def test_short_symmetric(self):
        eng = StrategyEngine(sl_atr=2.0, trail_atr=1.0)
        eng.pos_signal = -1
        eng.mark_entry(100.0)
        eng.atr_ref = 10.0
        self.assertEqual(eng.stop_level(), 120.0)
        eng.on_price(89.0)                                  # <= 90 激活
        self.assertTrue(eng.trail_on)
        self.assertEqual(eng.stop_level(), 99.0)            # 89 + 10
        eng.on_price(80.0)
        self.assertEqual(eng.stop_level(), 90.0)
        self.assertEqual(eng.on_price(91.0), "EXIT")

    def test_flat_resets(self):
        self.eng.on_price(120.0)
        self.eng.flat()
        self.assertEqual(self.eng.pos, 0)
        self.assertIsNone(self.eng.stop_level())
        self.assertIsNone(self.eng.on_price(1000.0))


try:
    import experiment as E
    import backtest as B
    HAVE_DATA = os.path.exists(E.SRC)
except Exception:
    E = B = None
    HAVE_DATA = False


@unittest.skipUnless(HAVE_DATA, "需要 data/BTC-USDT_5m.csv")
class TestDivergenceSignal(unittest.TestCase):
    """用真实 10m 数据切片验证信号能正常产生(数据缺失则跳过)。"""

    def test_signals_both_directions(self):
        bars = E.load_tf(600000)[-4000:]
        eng = StrategyEngine()
        counts = {"L": 0, "S": 0}
        for b in bars:
            sig = eng.on_bar(Bar(b["ts"], b["open"], b["high"], b["low"], b["close"]))
            if sig and eng.pos == 0:
                counts[sig] += 1
                eng.enter(sig, b["close"])
            if eng.pos != 0:
                seq = (eng.bars[-1].high, eng.bars[-1].low) if eng.pos > 0 \
                    else (eng.bars[-1].low, eng.bars[-1].high)
                if any(eng.on_price(p) == "EXIT" for p in seq):
                    eng.flat()
        self.assertGreater(counts["L"], 0)
        self.assertGreater(counts["S"], 0)

    def test_engine_matches_backtest(self):
        """引擎与回测的交易笔数应接近(允许 25% 偏差, 因入场时点不同)。"""
        bars = E.load_tf(600000)[-3000:]
        eng = StrategyEngine()
        n_eng = 0
        for b in bars:
            sig = eng.on_bar(Bar(b["ts"], b["open"], b["high"], b["low"], b["close"]))
            if sig and eng.pos == 0:
                eng.enter(sig, b["close"])
            if eng.pos != 0:
                seq = (eng.bars[-1].high, eng.bars[-1].low) if eng.pos > 0 \
                    else (eng.bars[-1].low, eng.bars[-1].high)
                if any(eng.on_price(p) == "EXIT" for p in seq):
                    eng.flat(); n_eng += 1
        mom, atr, _, _, _ = B.compute_indicators(bars)
        _, trades = B.backtest(bars, mom, atr, 0.0)
        n_bt = len(trades)
        self.assertGreater(n_bt, 0)
        self.assertLess(abs(n_eng - n_bt) / n_bt, 0.25,
                        "引擎 %d 笔 vs 回测 %d 笔 偏差过大" % (n_eng, n_bt))


if __name__ == "__main__":
    unittest.main(verbosity=2)
