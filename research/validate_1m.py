"""1 分钟周期: 策略引擎 vs TradingView 导出逐笔对拍。

已验证与 TV 一致的出场约定(check_first):
  - 信号在 K 线 i 收盘确认, 入场成交在 K 线 i+1 开盘价
  - atrRef = 入场那根 K 线(i+1)的 ATR(与 Pine 一致)
  - 出场: 先用"截至上一根为止的止损"对照本根 开盘/低(多) 或 开盘/高(空),
          命中即以该价成交; 未命中才用本根极值更新移动止损
"""
import os, sys, csv, datetime, math, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "research"))

import experiment as E

TV = os.path.join(ROOT, "tv", "SQZ_DIV_SW_OKX_BTCUSDT_2026-09-30.csv")
DATA = os.path.join(ROOT, "data", "BTC-USDT_1m.csv")
LB = 2


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def load_bars(path):
    bars = []
    with open(path) as f:
        r = csv.reader(f); next(r)
        for row in r:
            bars.append({"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                         "low": float(row[3]), "close": float(row[4])})
    return bars


def load_tv(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    ent, exi = {}, {}
    for r in rows:
        if not num(r["价格 USDT"]):
            continue
        ts = int(datetime.datetime.strptime(r["日期和时间"], "%Y-%m-%d %H:%M")
                 .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        if r["类型"].endswith("进场"):
            ent[ts] = {"px": num(r["价格 USDT"]),
                       "dir": 1 if r["类型"].startswith("多头") else -1,
                       "id": r["交易编号"]}
        else:
            exi[r["交易编号"]] = num(r["价格 USDT"])
    return ent, exi


def detect_signals(bars, mom):
    n = len(bars); sig = [0] * n
    plM1 = plM2 = plP1 = plP2 = phM1 = phM2 = phP1 = phP2 = float("nan")
    for i in range(2 * LB, n):
        pl = bars[i - LB]["low"] == min(b["low"] for b in bars[i - 2 * LB:i + 1])
        ph = bars[i - LB]["high"] == max(b["high"] for b in bars[i - 2 * LB:i + 1])
        bull = bear = False
        if pl:
            plM2, plP2, plM1, plP1 = plM1, plP1, mom[i - LB], bars[i - LB]["low"]
        if ph:
            phM2, phP2, phM1, phP1 = phM1, phP1, mom[i - LB], bars[i - LB]["high"]
        if pl and not math.isnan(plM2) and plP1 < plP2 and plM1 > plM2:
            bull = True
        if ph and not math.isnan(phM2) and phP1 > phP2 and phM1 < phM2:
            bear = True
        if bull:
            sig[i] = 1
        elif bear:
            sig[i] = -1
    return sig


def run(bars, sig, atr, sl_atr=2.0, trail_atr=1.0):
    n = len(bars); out = []
    pos = 0; entry = 0.0; ar = 0.0; best = 0.0; on = False; tsi = 0
    for i in range(1, n):
        if pos != 0:
            H, L, O = bars[i]["high"], bars[i]["low"], bars[i]["open"]
            if pos > 0:
                hs = entry - sl_atr * ar
                s = max(best - trail_atr * ar, hs) if on else hs
                if O <= s:
                    out.append((tsi, pos, entry, O)); pos = 0; continue
                if L <= s:
                    out.append((tsi, pos, entry, s)); pos = 0; continue
                if not on and H - entry >= trail_atr * ar:
                    on = True
                if on:
                    best = max(best, H)
            else:
                hs = entry + sl_atr * ar
                s = min(best + trail_atr * ar, hs) if on else hs
                if O >= s:
                    out.append((tsi, pos, entry, O)); pos = 0; continue
                if H >= s:
                    out.append((tsi, pos, entry, s)); pos = 0; continue
                if not on and entry - L >= trail_atr * ar:
                    on = True
                if on:
                    best = min(best, L)
        if pos == 0 and sig[i] != 0 and i + 1 < n:
            entry = bars[i + 1]["open"]; ar = atr[i + 1]; pos = sig[i]
            best = entry; on = False; tsi = bars[i + 1]["ts"]
            if math.isnan(ar):
                ar = entry * 0.005
    return out


def main():
    ent, exi = load_tv(TV)
    bars = load_bars(DATA)
    mom, atr = E.indicators(bars, 20, 14)
    sig = detect_signals(bars, mom)
    out = run(bars, sig, atr)

    t0, t1 = min(ent), max(ent)
    out = [o for o in out if t0 <= o[0] <= t1]
    md = mp = matched = 0
    diffs = []
    for tsi, pos, entry, fill in out:
        tv = ent.get(tsi)
        if tv is None:
            continue
        matched += 1
        if tv["dir"] == pos:
            md += 1
        if abs(tv["px"] - entry) < 1e-6:
            mp += 1
        tvx = exi.get(tv["id"])
        if tvx is not None:
            diffs.append(fill - tvx)

    print("引擎 %d 笔 | TV %d 笔 | 入场时间匹配 %d" % (len(out), len(ent), matched))
    print("入场: 方向一致 %.1f%% | 价格一致 %.1f%%" % (md / matched * 100, mp / matched * 100))
    k = len(diffs)
    print("出场 %d 笔:" % k)
    for tol in (0.05, 0.5, 1, 5, 20, 50):
        print("  |差| < %-5s : %5.1f%%" % (tol, sum(1 for x in diffs if abs(x) < tol) / k * 100))
    print("  中位误差 %.2f | 平均 %.2f" % (
        statistics.median(abs(x) for x in diffs), statistics.mean(abs(x) for x in diffs)))


if __name__ == "__main__":
    main()
