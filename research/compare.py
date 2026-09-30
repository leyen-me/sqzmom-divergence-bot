import csv, math, datetime, os
import experiment as E

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TVCSV = os.path.join(ROOT, "tv", "SQZ_DIV_SW_OKX_BTCUSDT_2026-09-30-10min.csv")


def load_tv():
    rows = list(csv.DictReader(open(TVCSV, encoding="utf-8-sig")))
    ent, exi = {}, {}
    def num(x):
        try: return float(x)
        except: return None
    for r in rows:
        if not num(r["价格 USDT"]):
            continue
        ts = int(datetime.datetime.strptime(r["日期和时间"], "%Y-%m-%d %H:%M")
                 .replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
        if r["类型"].endswith("进场"):
            ent[ts] = {"px": num(r["价格 USDT"]), "id": r["交易编号"],
                       "dir": 1 if r["类型"].startswith("多头") else -1}
        else:
            exi[r["交易编号"]] = {"px": num(r["价格 USDT"]),
                                  "dir": 1 if r["类型"].startswith("多头") else -1}
    return ent, exi


def run_model(bars, sl_atr=2.0, trail_atr=1.0, atr_len=14, entry_maker=False,
              best_from="entry", act_src="high", trigger="same_bar",
              exclusive_hard=False, atr_scale=1.0, best_track="high"):
    mom, atr = E.indicators(bars, 20, atr_len)
    n = len(bars)
    pos = 0; entry = 0.0; atrref = 0.0; best = 0.0; trail_on = False; eidx = -1
    out = []
    plMom1 = plMom2 = plPx1 = plPx2 = phMom1 = phMom2 = phPx1 = phPx2 = float("nan")
    for i in range(n):
        if math.isnan(mom[i]) or math.isnan(atr[i]): continue
        if i >= 2 * E.LB:
            pl = bars[i - E.LB]["low"] == min(b["low"] for b in bars[i - 2 * E.LB:i + 1])
            ph = bars[i - E.LB]["high"] == max(b["high"] for b in bars[i - 2 * E.LB:i + 1])
        else: pl = ph = False
        bull = bear = False
        if pl: plMom2, plPx2, plMom1, plPx1 = plMom1, plPx1, mom[i - E.LB], bars[i - E.LB]["low"]
        if ph: phMom2, phPx2, phMom1, phPx1 = phMom1, phPx1, mom[i - E.LB], bars[i - E.LB]["high"]
        if pl and not math.isnan(plMom2) and plPx1 < plPx2 and plMom1 > plMom2: bull = True
        if ph and not math.isnan(phMom2) and phPx1 > phPx2 and phMom1 < phMom2: bear = True

        if pos != 0:
            hi, lo, op = bars[i]["high"], bars[i]["low"], bars[i]["open"]
            act = trail_atr * atrref
            off = trail_atr * atrref
            if pos > 0:
                hard = entry - sl_atr * atrref
                trail_lvl = (best if best_from == "entry" else best) - off
                stop = max(hard, trail_lvl) if (trail_on and not exclusive_hard) else (trail_lvl if trail_on else hard)
                fill = None
                if trigger == "same_bar":
                    if op <= stop: fill = op
                    else:
                        src = hi if act_src == "high" else bars[i]["close"]
                        if not trail_on and src - entry >= act: trail_on = True; best = src
                        if trail_on:
                            best = max(best, hi if best_track=='high' else bars[i]['close'])
                            tlv = best - off
                            stop = max(hard, tlv) if not exclusive_hard else tlv
                        if lo <= stop: fill = stop
                else:
                    if op <= stop: fill = op
                    elif lo <= stop: fill = stop
                    else:
                        src = hi if act_src == "high" else bars[i]["close"]
                        if not trail_on and src - entry >= act: trail_on = True
                        if trail_on: best = max(best, hi if best_track=='high' else bars[i]['close'])
                if fill is not None:
                    out.append((pos, entry, fill, bars[eidx]['ts'])); pos = 0; trail_on = False
            else:
                hard = entry + sl_atr * atrref
                trail_lvl = best + off
                stop = min(hard, trail_lvl) if (trail_on and not exclusive_hard) else (trail_lvl if trail_on else hard)
                fill = None
                if trigger == "same_bar":
                    if op >= stop: fill = op
                    else:
                        src = lo if act_src == "high" else bars[i]["close"]
                        if not trail_on and entry - src >= act: trail_on = True; best = src
                        if trail_on:
                            best = min(best, lo if best_track=='high' else bars[i]['close']); tlv = best + off
                            stop = min(hard, tlv) if not exclusive_hard else tlv
                        if hi >= stop: fill = stop
                else:
                    if op >= stop: fill = op
                    elif hi >= stop: fill = stop
                    else:
                        src = lo if act_src == "high" else bars[i]["close"]
                        if not trail_on and entry - src >= act: trail_on = True
                        if trail_on: best = min(best, lo if best_track=='high' else bars[i]['close'])
                if fill is not None:
                    out.append((pos, entry, fill, bars[eidx]['ts'])); pos = 0; trail_on = False

        if pos == 0 and (bull or bear):
            idx = i
            if entry_maker:
                px = bars[i]["close"]
            else:
                if i + 1 >= n: continue
                px = bars[i + 1]["open"]; idx = i + 1
            atrref = atr[idx] if not math.isnan(atr[idx]) else px * 0.005
            atrref *= atr_scale
            entry = px; best = px; trail_on = False; eidx = idx
            pos = 1 if bull else -1
    return out


def score(bars, ent, exi, **kw):
    out = run_model(bars, **kw)
    matched = 0; same = 0; diffs = []
    for pos, entry, fill, ets in out:
        v = ent.get(ets)
        if v is None or v["dir"] != pos: continue
        matched += 1
        tv = exi.get(v["id"])
        if tv is None: continue
        d = fill - tv["px"]; diffs.append(d)
        if abs(d) < 0.05: same += 1
    return matched, same, len(diffs), (sum(abs(d) for d in diffs)/len(diffs) if diffs else 0)

if __name__ == "__main__":
    ent, exi = load_tv()
    bars = E.load_tf(600000)
    print("config                                                   matched same%  mean|d|")
    for cfg in [
        dict(),  # 当前模型
        dict(trigger="next_bar"),
        dict(best_from="activate"),
        dict(act_src="close"),
        dict(exclusive_hard=True),
        dict(trigger="next_bar", exclusive_hard=True),
    ]:
        m, s, d, ad = score(bars, ent, exi, **cfg)
        print(f"{str(cfg):<55} {m:>6} {s/max(1,d)*100:>6.1f}% {ad:>8.2f}")
