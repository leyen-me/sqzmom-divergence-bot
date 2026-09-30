"""校验 live/strategy.py 实盘引擎 vs TradingView 回测逐笔是否一致。

模拟规则(与实盘 trader 一致):
  - 信号在 K 线 i 收盘确认, 入场成交在 K 线 i+1 开盘价
  - 出场用引擎 on_price: 每根 K 线先喂最高价(更新移动止损)再喂最低价(判触发)
输出: 入场时间/方向/价格 与 TV 逐笔对比; 出场价格对比。
"""
import os, sys, csv, datetime, statistics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "live"))
sys.path.insert(0, os.path.join(ROOT, "research"))

import experiment as E
from strategy import StrategyEngine, Bar
TV = os.path.join(ROOT, "tv", "SQZ_DIV_SW_OKX_BTCUSDT_2026-09-30-10min.csv")


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def load_tv():
    rows = list(csv.DictReader(open(TV, encoding="utf-8-sig")))
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


def run_engine(bars, mode="next_bar"):
    eng = StrategyEngine(buffer=150)
    pend = None
    out = []
    for i, b in enumerate(bars):
        if pend and eng.pos == 0:
            eng.enter(pend, b["open"])
            out.append([b["ts"], eng.pos, b["open"], None])
            pend = None
        sig = eng.on_bar(Bar(b["ts"], b["open"], b["high"], b["low"], b["close"]))
        if sig and eng.pos == 0:
            pend = sig
        if eng.pos != 0:
            hi, lo, op = b["high"], b["low"], b["open"]
            if mode == "same_bar":
                # 同根K线: 先用极值更新移动止损, 再判触发(偏乐观)
                seq = (hi, lo) if eng.pos > 0 else (lo, hi)
                for p in seq:
                    if eng.on_price(p) == "EXIT":
                        out[-1][3] = eng.stop_level()
                        eng.flat()
                        break
            else:
                # 保守: 只用上一根为止的止损判触发, 之后才更新极值(符合实盘收盘后移动)
                cur = eng.stop_level()
                fill = None
                if eng.pos > 0:
                    if op <= cur:
                        fill = op
                    elif lo <= cur:
                        fill = cur
                    else:
                        eng.on_price(hi)
                else:
                    if op >= cur:
                        fill = op
                    elif hi >= cur:
                        fill = cur
                    else:
                        eng.on_price(lo)
                if fill is not None:
                    out[-1][3] = fill
                    eng.flat()
    return out


def main():
    ent, exi = load_tv()
    bars = E.load_tf(600000)

    out = run_engine(bars, mode="next_bar")
    lo, hi = out[0][0], out[-1][0]
    tv_inrange = sum(1 for t in ent if lo <= t <= hi)
    print("引擎成交笔数: %d | TV 同区间笔数: %d (偏差 %.2f%%)" %
          (len(out), tv_inrange, abs(len(out) - tv_inrange) / tv_inrange * 100))

    m_dir = m_px = matched = 0
    for ts, d, px, _ in out:
        tv = ent.get(ts)
        if tv is None:
            continue
        matched += 1
        if tv["dir"] == d:
            m_dir += 1
        if abs(tv["px"] - px) < 1e-6:
            m_px += 1
    print("\n=== 入场对拍 (按成交时间戳匹配 %d / %d) ===" % (matched, len(out)))
    print("方向一致:      %d (%.1f%%)" % (m_dir, m_dir / matched * 100))
    print("入场价完全一致: %d (%.1f%%)" % (m_px, m_px / matched * 100))

    for mode in ("next_bar", "same_bar"):
        o = run_engine(bars, mode=mode)
        diffs = []
        for ts, d, px, expx in o:
            tv = ent.get(ts)
            if not tv or expx is None:
                continue
            tvx = exi.get(tv["id"])
            if tvx is None:
                continue
            diffs.append(expx - tvx)
        n = len(diffs)
        label = "保守(收盘后才移止损)" if mode == "next_bar" else "乐观(同根K线)"
        print("\n=== 出场对拍 [%s] %d 笔 ===" % (label, n))
        print("完全一致 |差|<0.05: %.1f%%" % (sum(1 for x in diffs if abs(x) < 0.05) / n * 100))
        for tol in (1, 5, 20, 100):
            print("  |差| < %-4d : %.1f%%" % (tol, sum(1 for x in diffs if abs(x) < tol) / n * 100))
        print("中位误差 %.2f | 平均误差 %.2f" %
              (statistics.median(abs(x) for x in diffs), statistics.mean(abs(x) for x in diffs)))


if __name__ == "__main__":
    main()
