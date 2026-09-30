import urllib.request, urllib.error, json, ssl, time, sys, os
from concurrent.futures import ThreadPoolExecutor, as_completed

ssl._create_default_https_context = ssl._create_unverified_context

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INST = os.environ.get("INST", "BTC-USDT")
BAR = os.environ.get("BAR", "5m")
OUT = os.environ.get("OUT", os.path.join(ROOT, "data", f"{INST}_{BAR}.csv"))
START_MS = int(os.environ.get("START_MS", "1533081600000"))  # 2018-08-01
WORKERS = int(os.environ.get("WORKERS", "8"))

BASE = "https://www.okx.com/api/v5/market/history-candles"


def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
            if d["code"] == "0":
                return d["data"]
            if d["code"] == "51001":
                return []
            time.sleep(0.5 * (i + 1))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            time.sleep(0.4 * (i + 1))
        except Exception:
            time.sleep(0.4 * (i + 1))
    raise RuntimeError("fetch failed: " + url)


def fetch_range(end_ms, stop_ms):
    """返回 ts in (stop_ms, end_ms) 的K线, 向前翻页。"""
    rows = []
    cur = end_ms
    while cur > stop_ms:
        data = get(f"{BASE}?instId={INST}&bar={BAR}&after={cur}&limit=300")
        if not data:
            break
        data = [r for r in data if stop_ms < int(r[0]) <= end_ms]
        if not data:
            break
        rows.extend(data)
        oldest = int(data[-1][0])
        if oldest <= stop_ms:
            break
        cur = oldest
    return rows


def main():
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    now = int(time.time() * 1000)
    n_seg = WORKERS * 4
    step = (now - START_MS) // n_seg
    segs = []
    for i in range(n_seg):
        lo = START_MS + i * step
        hi = now if i == n_seg - 1 else START_MS + (i + 1) * step
        segs.append((hi, lo))

    all_rows = []
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(fetch_range, hi, lo) for hi, lo in segs]
        for f in as_completed(futs):
            all_rows.extend(f.result())
            done += 1
            sys.stderr.write(f"\rseg {done}/{n_seg} rows={len(all_rows)} {time.time()-t0:.0f}s")
    sys.stderr.write("\n")

    uniq = {int(r[0]): r for r in all_rows}
    ts_sorted = sorted(uniq)
    with open(OUT, "w") as f:
        f.write("ts,open,high,low,close,vol\n")
        for ts in ts_sorted:
            r = uniq[ts]
            f.write(f"{ts},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")
    print(f"wrote {len(ts_sorted)} rows -> {OUT}")
    print("range", time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts_sorted[0]/1000)),
          "->", time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts_sorted[-1]/1000)))


if __name__ == "__main__":
    main()
