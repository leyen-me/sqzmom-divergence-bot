import csv, math, sys, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "BTC-USDT_5m.csv")
OUTDIR = os.path.join(ROOT, "out")
INIT_CAP = 100000.0
LB = 2
LEN_KC = 20
ATR_LEN = 14
SL_ATR = 2.0
TRAIL_ATR = 1.0
QTY_PCT = 0.80


def load_10m(path):
    bars = []
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        cur = None
        for row in r:
            ts = int(row[0]); o, h, l, c, v = map(float, row[1:6])
            bucket = ts // 600000 * 600000
            if cur is None or bucket != cur["ts"]:
                if cur is not None:
                    bars.append(cur)
                cur = {"ts": bucket, "open": o, "high": h, "low": l, "close": c, "vol": v}
            else:
                cur["high"] = max(cur["high"], h)
                cur["low"] = min(cur["low"], l)
                cur["close"] = c
                cur["vol"] += v
        # 丢弃最后一根不完整的 10m
        if cur is not None:
            bars.append(cur)
    return bars


def highest(vals, i, n):
    return max(vals[max(0, i - n + 1):i + 1])


def lowest(vals, i, n):
    return min(vals[max(0, i - n + 1):i + 1])


def sma(vals, i, n):
    lo = max(0, i - n + 1)
    w = vals[lo:i + 1]
    return sum(w) / len(w)


def linreg(vals, i, n, offset):
    lo = max(0, i - n + 1)
    w = vals[lo:i + 1]
    m = len(w)
    sx = m * (m - 1) / 2
    sy = sum(w)
    sxy = sum(k * w[k] for k in range(m))
    sx2 = sum(k * k for k in range(m))
    den = m * sx2 - sx * sx
    slope = (m * sxy - sx * sy) / den if den else 0.0
    inter = (sy - slope * sx) / m
    return inter + slope * (m - 1 - offset)


def compute_indicators(bars):
    n = len(bars)
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    closes = [b["close"] for b in bars]

    mom = [0.0] * n
    for i in range(n):
        if i < LEN_KC:
            mom[i] = float("nan"); continue
        hh = highest(highs, i, LEN_KC)
        ll = lowest(lows, i, LEN_KC)
        avg1 = (hh + ll) / 2
        avg2 = sma(closes, i, LEN_KC)
        base = closes[i] - (avg1 + avg2) / 2
        # 需要 base 的历史序列来回归
        mom[i] = base  # 先存 base，后面再回归

    base = mom[:]
    momval = [float("nan")] * n
    for i in range(n):
        if i >= LEN_KC * 2:
            momval[i] = linreg(base, i, LEN_KC, 0)

    atr = [float("nan")] * n
    tr = [0.0] * n
    for i in range(n):
        if i == 0:
            tr[i] = highs[i] - lows[i]
        else:
            tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    for i in range(n):
        if i < ATR_LEN:
            continue
        if i == ATR_LEN:
            atr[i] = sum(tr[1:i + 1]) / ATR_LEN
        else:
            atr[i] = (atr[i - 1] * (ATR_LEN - 1) + tr[i]) / ATR_LEN

    return momval, atr, highs, lows, closes


def backtest(bars, mom, atr, fee_rate, slippage=0.0, use_next_open=True):
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    n = len(bars)
    equity = INIT_CAP
    pos = 0            # 0 flat, 1 long, -1 short
    entry = 0.0
    entry_idx = -1
    atrref = 0.0
    best = 0.0
    trail_on = False
    size = 0.0
    trades = []

    plMom1 = plMom2 = plPx1 = plPx2 = float("nan")
    phMom1 = phMom2 = phPx1 = phPx2 = float("nan")

    for i in range(n):
        if math.isnan(mom[i]) or math.isnan(atr[i]):
            continue
        # --- 枢轴（在 bar i 收盘确认） ---
        if i >= LB and i >= 2 * LB:
            pl = bars[i - LB]["low"] == lowest(lows, i, 2 * LB + 1)
            ph = bars[i - LB]["high"] == highest(highs, i, 2 * LB + 1)
        else:
            pl = ph = False

        bull = bear = False
        if pl:
            plMom2, plPx2 = plMom1, plPx1
            plMom1, plPx1 = mom[i - LB], bars[i - LB]["low"]
        if ph:
            phMom2, phPx2 = phMom1, phPx1
            phMom1, phPx1 = mom[i - LB], bars[i - LB]["high"]
        if pl and not math.isnan(plMom2) and plPx1 < plPx2 and plMom1 > plMom2:
            bull = True
        if ph and not math.isnan(phMom2) and phPx1 > phPx2 and phMom1 < phMom2:
            bear = True

        # --- 已持仓: 检查止损/移动止损 ---
        if pos != 0:
            hi, lo = bars[i]["high"], bars[i]["low"]
            op = bars[i]["open"]
            if pos > 0:
                stop = entry - SL_ATR * atrref
                if trail_on:
                    stop = max(stop, best - TRAIL_ATR * atrref)
                fill = None
                if op <= stop:
                    fill = op
                else:
                    if not trail_on and hi - entry >= TRAIL_ATR * atrref:
                        trail_on = True
                    if trail_on:
                        best = max(best, hi)
                        stop = max(stop, best - TRAIL_ATR * atrref)
                    if lo <= stop:
                        fill = stop
                if fill is not None:
                    _close_trade(trades, bars, i, pos, entry, fill, size, fee_rate, slippage, entry_idx)
                    equity += _pnl(pos, entry, fill, size) - size * fill * fee_rate
                    pos = 0; trail_on = False
            else:
                stop = entry + SL_ATR * atrref
                if trail_on:
                    stop = min(stop, best + TRAIL_ATR * atrref)
                fill = None
                if op >= stop:
                    fill = op
                else:
                    if not trail_on and entry - lo >= TRAIL_ATR * atrref:
                        trail_on = True
                    if trail_on:
                        best = min(best, lo)
                        stop = min(stop, best + TRAIL_ATR * atrref)
                    if hi >= stop:
                        fill = stop
                if fill is not None:
                    _close_trade(trades, bars, i, pos, entry, fill, size, fee_rate, slippage, entry_idx)
                    equity += _pnl(pos, entry, fill, size) - size * fill * fee_rate
                    pos = 0; trail_on = False

        # --- 开仓 ---
        if pos == 0 and (bull or bear) and i + 1 < n:
            fill_idx = i + 1 if use_next_open else i
            px = bars[fill_idx]["open"] if use_next_open else bars[i]["close"]
            slip = px * slippage
            entry = px + slip if bull else px - slip
            atrref = atr[fill_idx] if not math.isnan(atr[fill_idx]) else entry * 0.005
            size = equity * QTY_PCT / entry
            equity -= size * entry * fee_rate
            pos = 1 if bull else -1
            best = entry
            trail_on = False
            entry_idx = fill_idx
    return equity, trades


def _pnl(pos, entry, exitp, size):
    return (exitp - entry) * size * pos


def _fee(pos, entry, exitp, size, rate):
    return (entry + exitp) * size * rate


def _close_trade(trades, bars, i, pos, entry, exitp, size, rate, slip, entry_idx):
    trades.append({"idx": i, "exit_idx": i, "entry_idx": entry_idx, "dir": pos,
                   "entry": entry, "exit": exitp,
                   "entry_ts": bars[entry_idx]["ts"], "exit_ts": bars[i]["ts"],
                   "pnl": _pnl(pos, entry, exitp, size)})


def run(fee_rate, slippage=0.0):
    bars = load_10m(SRC)
    mom, atr, highs, lows, closes = compute_indicators(bars)
    eq, trades = backtest(bars, mom, atr, fee_rate, slippage)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = gp / gl if gl else float("inf")
    return {
        "bars": len(bars), "trades": len(trades), "equity": eq,
        "ret_pct": (eq / INIT_CAP - 1) * 100,
        "winrate": len(wins) / len(trades) * 100 if trades else 0,
        "pf": pf, "fee": fee_rate,
    }


if __name__ == "__main__":
    import time
    t0 = time.time()
    for fee in [0.0, 0.0002, 0.0005, 0.0010]:
        r = run(fee)
        print(f"fee={fee*100:.3f}%/side  trades={r['trades']}  ret={r['ret_pct']:.1f}%  "
              f"win={r['winrate']:.1f}%  PF={r['pf']:.3f}  eq={r['equity']:.0f}  bars={r['bars']}")
    print("elapsed", round(time.time() - t0, 1), "s")
