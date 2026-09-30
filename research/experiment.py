import csv, math, statistics, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "BTC-USDT_5m.csv")
INIT_CAP = 100000.0
LB = 2


def load_tf(bar_ms, path=SRC):
    bars = []
    with open(path) as f:
        r = csv.reader(f); next(r)
        cur = None
        for row in r:
            ts = int(row[0]); o, h, l, c, v = map(float, row[1:6])
            bucket = ts // bar_ms * bar_ms
            if cur is None or bucket != cur["ts"]:
                if cur: bars.append(cur)
                cur = {"ts": bucket, "open": o, "high": h, "low": l, "close": c}
            else:
                cur["high"] = max(cur["high"], h); cur["low"] = min(cur["low"], l)
                cur["close"] = c
        if cur: bars.append(cur)
    return bars


def _hi(v, i, n): return max(v[max(0, i - n + 1):i + 1])
def _lo(v, i, n): return min(v[max(0, i - n + 1):i + 1])
def _sma(v, i, n): return sum(v[max(0, i - n + 1):i + 1]) / len(v[max(0, i - n + 1):i + 1])


def _linreg(v, i, n, off):
    w = v[i - n + 1:i + 1]; m = len(w)
    sx = m * (m - 1) / 2; sy = sum(w)
    sxy = sum(k * w[k] for k in range(m)); sx2 = sum(k * k for k in range(m))
    den = m * sx2 - sx * sx
    slope = (m * sxy - sx * sy) / den if den else 0.0
    return (sy - slope * sx) / m + slope * (m - 1 - off)


def indicators(bars, len_kc=20, atr_len=14):
    n = len(bars)
    H = [b["high"] for b in bars]; L = [b["low"] for b in bars]; C = [b["close"] for b in bars]
    base = [float("nan")] * n
    for i in range(len_kc, n):
        base[i] = C[i] - ((_hi(H, i, len_kc) + _lo(L, i, len_kc)) / 2 + _sma(C, i, len_kc)) / 2
    mom = [float("nan")] * n
    for i in range(len_kc * 2, n):
        mom[i] = _linreg(base, i, len_kc, 0)
    tr = [0.0] * n
    for i in range(n):
        tr[i] = H[i] - L[i] if i == 0 else max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
    atr = [float("nan")] * n
    for i in range(n):
        if i < atr_len: continue
        atr[i] = sum(tr[1:i + 1]) / atr_len if i == atr_len else (atr[i - 1] * (atr_len - 1) + tr[i]) / atr_len
    return mom, atr


def run(bars, sl_atr=2.0, trail_atr=1.0, atr_len=14, fee_rt=0.0, entry_maker=False,
        fee_maker=0.0, log=None, conservative=False, lev=1.0):
    """fee_rt: 出场那一腿的费率; 若 entry_maker 则进场按 fee_maker, 否则进场也按 fee_rt."""
    mom, atr = indicators(bars, 20, atr_len)
    n = len(bars)
    equity = INIT_CAP; pos = 0; entry = 0.0; atrref = 0.0; best = 0.0
    trail_on = False; size = 0.0
    rets = []  # 每笔价格边际 %
    plMom1 = plMom2 = plPx1 = plPx2 = phMom1 = phMom2 = phPx1 = phPx2 = float("nan")
    in_fee = fee_maker if entry_maker else fee_rt
    for i in range(n):
        if math.isnan(mom[i]) or math.isnan(atr[i]): continue
        if i >= 2 * LB:
            pl = bars[i - LB]["low"] == min(b["low"] for b in bars[i - 2 * LB:i + 1])
            ph = bars[i - LB]["high"] == max(b["high"] for b in bars[i - 2 * LB:i + 1])
        else:
            pl = ph = False
        bull = bear = False
        if pl:
            plMom2, plPx2, plMom1, plPx1 = plMom1, plPx1, mom[i - LB], bars[i - LB]["low"]
        if ph:
            phMom2, phPx2, phMom1, phPx1 = phMom1, phPx1, mom[i - LB], bars[i - LB]["high"]
        if pl and not math.isnan(plMom2) and plPx1 < plPx2 and plMom1 > plMom2: bull = True
        if ph and not math.isnan(phMom2) and phPx1 > phPx2 and phMom1 < phMom2: bear = True

        if pos != 0:
            hi, lo, op = bars[i]["high"], bars[i]["low"], bars[i]["open"]
            if pos > 0:
                stop = entry - sl_atr * atrref
                if trail_on: stop = max(stop, best - trail_atr * atrref)
                fill = None
                if op <= stop:
                    fill = op
                elif lo <= stop:
                    fill = stop
                elif not conservative:
                    if not trail_on and hi - entry >= trail_atr * atrref: trail_on = True
                    if trail_on:
                        best = max(best, hi); stop = max(stop, best - trail_atr * atrref)
                    if lo <= stop: fill = stop
                else:
                    if not trail_on and hi - entry >= trail_atr * atrref: trail_on = True
                    if trail_on: best = max(best, hi)
                if fill is not None:
                    ret = (fill - entry) / entry * 100
                    equity += size * (fill - entry) - size * fill * fee_rt
                    if log is not None: log.append((pos, entry, fill, atrref, ret))
                    pos = 0; trail_on = False; rets.append(ret)
            else:
                stop = entry + sl_atr * atrref
                if trail_on: stop = min(stop, best + trail_atr * atrref)
                fill = None
                if op >= stop:
                    fill = op
                elif hi >= stop:
                    fill = stop
                elif not conservative:
                    if not trail_on and entry - lo >= trail_atr * atrref: trail_on = True
                    if trail_on:
                        best = min(best, lo); stop = min(stop, best + trail_atr * atrref)
                    if hi >= stop: fill = stop
                else:
                    if not trail_on and entry - lo >= trail_atr * atrref: trail_on = True
                    if trail_on: best = min(best, lo)
                if fill is not None:
                    ret = (entry - fill) / entry * 100
                    equity += size * (entry - fill) - size * fill * fee_rt
                    if log is not None: log.append((pos, entry, fill, atrref, ret))
                    pos = 0; trail_on = False; rets.append(ret)

        if pos == 0 and (bull or bear):
            idx = i  # maker: 收盘价成交; 否则下一根开盘
            if entry_maker:
                px = bars[i]["close"]
            else:
                if i + 1 >= n: continue
                px = bars[i + 1]["open"]; idx = i + 1
            atrref = atr[idx] if not math.isnan(atr[idx]) else px * 0.005
            size = equity * 0.80 * lev / px
            equity -= size * px * in_fee
            entry = px
            pos = 1 if bull else -1; best = px; trail_on = False
        if equity <= 0:
            equity = 0.0; break
    if not rets:
        return {"n": 0}
    wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
    return {
        "n": len(rets), "edge": statistics.mean(rets), "median": statistics.median(rets),
        "winrate": len(wins) / len(rets) * 100,
        "avg_win": statistics.mean(wins) if wins else 0,
        "avg_loss": statistics.mean(losses) if losses else 0,
        "equity": equity,
    }


if __name__ == "__main__":
    TFS = [("10m", 600000), ("15m", 900000), ("30m", 1800000),
           ("1H", 3600000), ("2H", 7200000), ("4H", 14400000), ("1D", 86400000)]
    print(f"{'TF':>4} {'笔数':>6} {'毛边际%':>8} {'中位%':>7} {'胜率%':>6} {'均盈%':>7} {'均亏%':>7} "
          f"{'PF':>5} {'0费收益%':>9}")
    for name, ms in TFS:
        bars = load_tf(ms)
        r = run(bars, fee_rt=0.0)
        if r["n"] == 0: continue
        pf = (r["avg_win"] * r["winrate"]) / (-r["avg_loss"] * (100 - r["winrate"])) if r["avg_loss"] else 0
        print(f"{name:>4} {r['n']:>6} {r['edge']:>8.4f} {r['median']:>7.3f} {r['winrate']:>6.1f} "
              f"{r['avg_win']:>7.3f} {r['avg_loss']:>7.3f} {pf:>5.2f} {(r['equity']/INIT_CAP-1)*100:>9.1f}")


def run_tp(bars, sl_atr=1.0, tp_atr=2.0, atr_len=14, fee_rt=0.0, entry_maker=False,
           fee_maker=0.0, lev=1.0):
    """固定止盈止损版: 用 atr 倍数做 SL/TP, 保守成交(先判止损)."""
    mom, atr = indicators(bars, 20, atr_len)
    n = len(bars)
    equity = INIT_CAP; pos = 0; entry = 0.0; atrref = 0.0; size = 0.0
    rets = []
    plMom1 = plMom2 = plPx1 = plPx2 = phMom1 = phMom2 = phPx1 = phPx2 = float("nan")
    for i in range(n):
        if math.isnan(mom[i]) or math.isnan(atr[i]): continue
        if i >= 2 * LB:
            pl = bars[i - LB]["low"] == min(b["low"] for b in bars[i - 2 * LB:i + 1])
            ph = bars[i - LB]["high"] == max(b["high"] for b in bars[i - 2 * LB:i + 1])
        else: pl = ph = False
        bull = bear = False
        if pl: plMom2, plPx2, plMom1, plPx1 = plMom1, plPx1, mom[i - LB], bars[i - LB]["low"]
        if ph: phMom2, phPx2, phMom1, phPx1 = phMom1, phPx1, mom[i - LB], bars[i - LB]["high"]
        if pl and not math.isnan(plMom2) and plPx1 < plPx2 and plMom1 > plMom2: bull = True
        if ph and not math.isnan(phMom2) and phPx1 > phPx2 and phMom1 < phMom2: bear = True
        if pos != 0:
            hi, lo, op = bars[i]["high"], bars[i]["low"], bars[i]["open"]
            if pos > 0:
                sl = entry - sl_atr * atrref; tp = entry + tp_atr * atrref
                fill = None
                if op <= sl: fill = op
                elif lo <= sl: fill = sl
                elif op >= tp: fill = op
                elif hi >= tp: fill = tp
                if fill is not None:
                    ret = (fill - entry) / entry * 100
                    equity += size * (fill - entry) - size * fill * fee_rt
                    pos = 0; rets.append(ret)
            else:
                sl = entry + sl_atr * atrref; tp = entry - tp_atr * atrref
                fill = None
                if op >= sl: fill = op
                elif hi >= sl: fill = sl
                elif op <= tp: fill = op
                elif lo <= tp: fill = tp
                if fill is not None:
                    ret = (entry - fill) / entry * 100
                    equity += size * (entry - fill) - size * fill * fee_rt
                    pos = 0; rets.append(ret)
        if pos == 0 and (bull or bear):
            idx = i
            if entry_maker: px = bars[i]["close"]
            else:
                if i + 1 >= n: continue
                px = bars[i + 1]["open"]; idx = i + 1
            atrref = atr[idx] if not math.isnan(atr[idx]) else px * 0.005
            size = equity * 0.80 * lev / px
            in_fee = fee_maker if entry_maker else fee_rt
            equity -= size * px * in_fee
            entry = px; pos = 1 if bull else -1
        if equity <= 0: equity = 0.0; break
    if not rets: return {"n": 0}
    wins = [r for r in rets if r > 0]; losses = [r for r in rets if r <= 0]
    return {"n": len(rets), "edge": statistics.mean(rets), "winrate": len(wins)/len(rets)*100,
            "equity": equity}
