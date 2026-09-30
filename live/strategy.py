"""SQZMOM 背离波段策略引擎 —— 精确复刻 tv/SQZMOM.pine 的信号与动态移动止损。

用法(实盘/模拟通用):
    eng = StrategyEngine()
    for bar in bars:            # 已收盘的 10m K线
        sig = eng.on_bar(bar)   # 返回 'L' / 'S' / None
    # 盘中每次拿到最新价:
    act = eng.on_price(price)   # 返回 'EXIT_LONG' / 'EXIT_SHORT' / None
"""


class Bar:
    __slots__ = ("ts", "open", "high", "low", "close", "vol")
    def __init__(self, ts, open_, high, low, close, vol=0.0):
        self.ts = ts; self.open = open_; self.high = high
        self.low = low; self.close = close; self.vol = vol


class StrategyEngine:
    def __init__(self, length_kc=20, lb=2, atr_len=14, sl_atr=2.0, trail_atr=1.0,
                 buffer=600):
        self.length_kc = length_kc
        self.lb = lb
        self.atr_len = atr_len
        self.sl_atr = sl_atr
        self.trail_atr = trail_atr
        self.bars = []
        self.buffer = buffer

        # 枢轴历史
        self.plMom1 = self.plMom2 = self.plPx1 = self.plPx2 = None
        self.phMom1 = self.phMom2 = self.phPx1 = self.phPx2 = None

        # 持仓状态
        self.pos = 0            # 0 flat, 1 long, -1 short
        self.entry_px = 0.0
        self.atr_ref = 0.0
        self.best = 0.0
        self.trail_on = False

    # ---------- 指标 ----------
    @staticmethod
    def _linreg(vals, n, offset):
        w = vals[-n:]; m = len(w)
        sx = m * (m - 1) / 2; sy = sum(w)
        sxy = sum(k * w[k] for k in range(m)); sx2 = sum(k * k for k in range(m))
        den = m * sx2 - sx * sx
        slope = (m * sxy - sx * sy) / den if den else 0.0
        return (sy - slope * sx) / m + slope * (m - 1 - offset)

    def _mom_series(self):
        C = [b.close for b in self.bars]; H = [b.high for b in self.bars]; L = [b.low for b in self.bars]
        n = self.length_kc
        base = [None] * len(C)
        for i in range(n - 1, len(C)):
            hh = max(H[i - n + 1:i + 1]); ll = min(L[i - n + 1:i + 1])
            sm = sum(C[i - n + 1:i + 1]) / n
            base[i] = C[i] - ((hh + ll) / 2 + sm) / 2
        mom = [None] * len(C)
        for i in range(2 * n - 2, len(C)):
            if base[i - n + 1] is None:
                continue
            mom[i] = self._linreg(base[i - n + 1:i + 1], n, 0)
        return mom

    def _atr_series(self):
        C = [b.close for b in self.bars]; H = [b.high for b in self.bars]; L = [b.low for b in self.bars]
        n = self.atr_len; m = len(C)
        tr = [0.0] * m
        for i in range(m):
            tr[i] = H[i] - L[i] if i == 0 else max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
        atr = [None] * m
        for i in range(n, m):
            atr[i] = sum(tr[1:i + 1]) / n if i == n else (atr[i - 1] * (n - 1) + tr[i]) / n
        return atr

    # ---------- 收盘后处理 ----------
    def on_bar(self, bar):
        """喂入一根已收盘 K 线, 返回入场信号 'L'/'S'/None。"""
        self.bars.append(bar)
        if len(self.bars) > self.buffer:
            self.bars = self.bars[-self.buffer:]

        n = len(self.bars)
        if n < self.length_kc * 2 + self.lb + 2:
            return None

        mom = self._mom_series()
        i = n - 1
        lb = self.lb
        L = [b.low for b in self.bars]; H = [b.high for b in self.bars]

        pl = (i >= lb + 2 * lb) and (L[i - lb] == min(L[i - 2 * lb:i + 1]))
        ph = (i >= lb + 2 * lb) and (H[i - lb] == max(H[i - 2 * lb:i + 1]))

        if pl and mom[i - lb] is not None:
            self.plMom2, self.plPx2 = self.plMom1, self.plPx1
            self.plMom1, self.plPx1 = mom[i - lb], L[i - lb]
        if ph and mom[i - lb] is not None:
            self.phMom2, self.phPx2 = self.phMom1, self.phPx1
            self.phMom1, self.phPx1 = mom[i - lb], H[i - lb]

        bull = (pl and self.plMom2 is not None
                and self.plPx1 < self.plPx2 and self.plMom1 > self.plMom2)
        bear = (ph and self.phMom2 is not None
                and self.phPx1 > self.phPx2 and self.phMom1 < self.phMom2)

        self._atr_now = self._atr_series()[-1]
        if self.pos == 0:
            if bull:
                return "L"
            if bear:
                return "S"
        return None

    def atr(self):
        a = self._atr_series()
        return a[-1]

    # ---------- 入场/出场状态 ----------
    def mark_entry(self, price):
        self.pos = 1 if self.pos_signal > 0 else -1
        self.entry_px = price
        self.atr_ref = self.atr() or price * 0.005
        self.best = price
        self.trail_on = False

    def enter(self, signal, price):
        self.pos_signal = 1 if signal == "L" else -1
        self.mark_entry(price)
        return self.pos

    def stop_level(self):
        if self.pos == 0:
            return None
        if self.pos > 0:
            stop = self.entry_px - self.sl_atr * self.atr_ref
            if self.trail_on:
                stop = max(stop, self.best - self.trail_atr * self.atr_ref)
            return stop
        else:
            stop = self.entry_px + self.sl_atr * self.atr_ref
            if self.trail_on:
                stop = min(stop, self.best + self.trail_atr * self.atr_ref)
            return stop

    def on_price(self, price):
        """盘中用最新价更新移动止损, 返回 'EXIT' / None。"""
        if self.pos == 0:
            return None
        act = self.trail_atr * self.atr_ref
        if self.pos > 0:
            if not self.trail_on and price - self.entry_px >= act:
                self.trail_on = True
            if self.trail_on:
                self.best = max(self.best, price)
            stop = self.stop_level()
            if price <= stop:
                return "EXIT"
        else:
            if not self.trail_on and self.entry_px - price >= act:
                self.trail_on = True
            if self.trail_on:
                self.best = min(self.best, price)
            stop = self.stop_level()
            if price >= stop:
                return "EXIT"
        return None

    def flat(self):
        self.pos = 0; self.entry_px = 0.0; self.atr_ref = 0.0
        self.best = 0.0; self.trail_on = False
