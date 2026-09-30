"""SQZMOM 背离波段 —— OKX 实盘/模拟盘交易程序。

运行:
    python3 trader.py                 # 用 config.json, 默认 dry_run(不下单)
    DRY_RUN=0 python3 trader.py       # 实盘下单(demo 盘由 OKX_DEMO 控制)
    OKX_DEMO=1 python3 trader.py      # OKX 模拟盘(需 demo 的 API key)

密钥(实盘/模拟盘)从环境变量读取:
    OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE / OKX_DEMO
"""
import os, sys, json, time, math, datetime, csv

from okx_client import OKXClient, OKXError
from strategy import StrategyEngine, Bar
from mailer import Mailer
from ws import OKXFeed

ROOT = os.path.dirname(os.path.abspath(__file__))           # live/
PROJ = os.path.dirname(ROOT)                                 # 项目根
LOG = os.path.join(PROJ, "out", "trader.log")


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load_config():
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    # 环境变量覆盖(方便容器部署)
    if os.environ.get("DRY_RUN") is not None:
        cfg["dry_run"] = os.environ["DRY_RUN"] == "1"
    if os.environ.get("OKX_INSTID"):
        cfg["instId"] = os.environ["OKX_INSTID"]
    if os.environ.get("OKX_LEVERAGE"):
        cfg["leverage"] = float(os.environ["OKX_LEVERAGE"])
    if os.environ.get("OKX_QTY_PCT"):
        cfg["qty_pct"] = float(os.environ["OKX_QTY_PCT"])
    return cfg


def fetch_recent(client, inst_id, bar, n):
    """取最近 n 根 K 线(升序返回 [Bar]), 用于主循环。"""
    raw = client.candles_recent(inst_id, bar, limit=n)[::-1]
    return [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]


def fetch_history(client, inst_id, bar, need):
    """取最近 need 根已收盘 K 线, 升序返回 [Bar]."""
    raw = []
    after = None
    while len(raw) < need:
        d = client.candles(inst_id, bar, limit=300, after=after)
        if not d:
            break
        raw.extend(d)
        after = d[-1][0]
    raw = raw[::-1]  # 升序
    bars = [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in raw]
    return bars


def aggregate(bars_5m, tf_min=10):
    """把 5m 聚合成 tf_min 的 K 线, 返回 [(bar, closed_bool)]。"""
    ms = tf_min * 60 * 1000
    out = {}
    order = []
    for b in bars_5m:
        bucket = b.ts // ms * ms
        if bucket not in out:
            out[bucket] = Bar(bucket, b.open, b.high, b.low, b.close, b.vol)
            order.append(bucket)
        else:
            c = out[bucket]
            c.high = max(c.high, b.high); c.low = min(c.low, b.low)
            c.close = b.close; c.vol += b.vol
    order.sort()
    res = []
    now_ms = time.time() * 1000
    for i, ts in enumerate(order):
        closed = (now_ms >= ts + ms)
        res.append((out[ts], closed))
    return res


class Trader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = OKXClient()
        self.eng = StrategyEngine(
            length_kc=cfg["length_kc"], lb=cfg["lb"], atr_len=cfg["atr_len"],
            sl_atr=cfg["sl_atr"], trail_atr=cfg["trail_atr"])
        self.inst = cfg["instId"]
        self.inst_type = "SWAP" if self.inst.endswith("-SWAP") else "SPOT"
        self.dry = cfg["dry_run"]
        self.last_bar_ts = 0
        self.sim_pos = 0.0      # dry-run 持仓数量
        self.sim_entry = 0.0
        self.inst_info = None
        self.entry_order_id = None
        self.hedge = False          # 账户是否多空双向持仓(long_short_mode)
        self.mailer = Mailer(log=log)
        self._trades = []          # 当日/运行期成交记录
        self._summary_sent = None
        self.state_path = os.path.join(PROJ, "out", "state.json")
        self.stop_algo = None       # 交易所侧保护性止损 algoId
        self.trail_algo = None      # 交易所侧原生移动止损 algoId(exchange 模式)
        self.stop_trigger = None    # 交易所侧止损当前触发价
        self.pos_sz = 0.0           # 当前持仓数量(用于保护止损)
        self.exit_mode = cfg.get("exit_mode", "local")   # local=内存移动止损 / exchange=交易所原生
        self.use_ws = cfg.get("use_ws", False)           # 用 WebSocket 行情(带 REST 兜底)
        self.feed = None

    # ---------- 状态落盘 ----------
    def load_state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self):
        st = {
            "last_bar_ts": self.last_bar_ts,
            "pos": self.eng.pos,
            "entry_px": self.eng.entry_px,
            "atr_ref": self.eng.atr_ref,
            "best": self.eng.best,
            "trail_on": self.eng.trail_on,
            "stop_algo": self.stop_algo,
            "trail_algo": self.trail_algo,
            "stop_trigger": self.stop_trigger,
            "pos_sz": self.pos_sz,
        }
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            log("状态落盘失败: %s" % e)

    # ---------- 工具 ----------
    def price(self):
        return float(self.client.ticker(self.inst)[0]["last"])

    def equity(self):
        if self.dry:
            return self.cfg.get("_sim_equity", 10000.0)
        return self.client.balance_usdt()

    def size_contracts(self, price):
        info = self.inst_info
        ctval = float(info["ctVal"])
        lot = float(info["lotSz"])
        min_sz = float(info["minSz"])
        notional = self.equity() * self.cfg["qty_pct"] * self.cfg["leverage"]
        raw = notional / (ctval * price)
        sz = math.floor(raw / lot) * lot
        if sz < min_sz:
            sz = 0.0
        return round(sz, 8)

    def spot_size(self, price):
        notional = self.equity() * self.cfg["qty_pct"]
        return round(notional / price, 8)

    # ---------- 下单 ----------
    def _wait_fill(self, oid, timeout=20):
        """轮询限价单, 返回 (成交均价, 已成交数量, 状态)。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            o = self.client.order_status(self.inst, oid)
            if o:
                st = o.get("state")
                fs = float(o.get("accFillSz") or 0)
                if st in ("filled", "canceled"):
                    return float(o.get("avgPx") or 0), fs, st
            time.sleep(1.0)
        o = self.client.order_status(self.inst, oid) or {}
        return float(o.get("avgPx") or 0), float(o.get("accFillSz") or 0), o.get("state", "unknown")

    def open_position(self, signal, price):
        if self.dry:
            sz = (self.spot_size(price) if self.inst_type == "SPOT" else self.size_contracts(price))
            if sz <= 0:
                log("[dry] 仓位算出来是 0, 跳过")
                return
            self.sim_pos = sz if signal == "L" else -sz
            self.sim_entry = price
            self.pos_sz = abs(sz)
            self.eng.enter(signal, price)
            log("[dry] 开仓 %s @ %.1f sz=%s atrRef=%.2f stop=%.1f" %
                (signal, price, sz, self.eng.atr_ref, self.eng.stop_level()))
            self.notify_entry(signal, price, sz)
            self.save_state()
            return
        try:
            if self.inst_type == "SPOT":
                if signal != "L":
                    log("现货不支持做空, 忽略 S 信号")
                    return
                sz = self.spot_size(price)
                td_mode, pos_side = "cash", None
                side = "buy"
            else:
                sz = self.size_contracts(price)
                if sz <= 0:
                    log("仓位算出来是 0(资金不足/低于最小张数), 跳过")
                    return
                side = "buy" if signal == "L" else "sell"
                pos_side = ("long" if signal == "L" else "short") if self.hedge else None
                td_mode = self.cfg["td_mode"]
            ack = self.client.place_order(self.inst, side, "limit", sz, td_mode=td_mode,
                                          pos_side=pos_side, px=price)
            oid = ack["ordId"]
            log("[live] 挂单 %s @ %.1f sz=%s ordId=%s" % (signal, price, sz, oid))
            fill_px, fill_sz, st = self._wait_fill(oid, timeout=self.cfg.get("entry_timeout_sec", 20))
            if st != "filled":
                # 未完全成交: 撤掉剩余, 有部分成交则按部分建仓
                self.client.cancel_order(self.inst, ord_id=oid)
            if fill_sz <= 0:
                log("[live] 限价单未成交, 已撤销, 放弃本次信号")
                self.mailer.send("[OKX] 限价单未成交, 信号放弃",
                                 "%s @ %.1f\n状态=%s" % (signal, price, st))
                return
            if st != "filled":
                log("[live] 部分成交 %s/%s" % (fill_sz, sz))
            self.pos_sz = fill_sz
            self.eng.enter(signal, fill_px or price)
            self.place_protective_stop()
            self.notify_entry(signal, fill_px or price, fill_sz)
            self.save_state()
        except OKXError as e:
            log("开仓失败: %s" % e)
            self.mailer.send("[OKX][异常] 开仓失败", "%s\n价格=%.1f\n%s" % (signal, price, e))

    # ---------- 交易所侧保护性止损 ----------
    def _round_tick(self, px):
        tick = float(self.inst_info["tickSz"]) if self.inst_info else 0.1
        return round(px / tick) * tick

    def _last_fill_px(self):
        try:
            fl = self.client.fills(self.inst, limit=1)
            if fl:
                return float(fl[0]["fillPx"])
        except (OKXError, KeyError, ValueError):
            pass
        return None

    def _place_hard_stop(self):
        side = "sell" if self.eng.pos > 0 else "buy"
        pos_side = ("long" if self.eng.pos > 0 else "short") if self.hedge else None
        trig = self._round_tick(self.eng.stop_level())
        try:
            self.stop_algo = self.client.place_algo_stop(
                self.inst, side, pos_side, self.pos_sz, trig, self.cfg["td_mode"])
            self.stop_trigger = trig
            log("[live] 已挂交易所保护止损 @ %.1f algoId=%s" % (trig, self.stop_algo))
        except OKXError as e:
            self.stop_algo = None
            log("挂保护止损失败: %s" % e)
            self.mailer.send("[OKX][异常] 交易所保护止损挂单失败",
                             "止损价=%.1f 数量=%s\n%s\n仓位未受交易所侧保护!" % (trig, self.pos_sz, e))

    def _place_trailing(self):
        side = "sell" if self.eng.pos > 0 else "buy"
        pos_side = ("long" if self.eng.pos > 0 else "short") if self.hedge else None
        off = self._round_tick(self.cfg["trail_atr"] * self.eng.atr_ref)
        act = self._round_tick(self.eng.entry_px + self.eng.pos * self.cfg["trail_atr"] * self.eng.atr_ref)
        try:
            self.trail_algo = self.client.place_trailing_stop(
                self.inst, side, pos_side, self.pos_sz, off, act, self.cfg["td_mode"])
            log("[live] 已挂交易所原生移动止损 激活价=%.1f 回撤=%.1f algoId=%s" % (
                act, off, self.trail_algo))
        except OKXError as e:
            self.trail_algo = None
            log("挂移动止损失败: %s" % e)
            self.mailer.send("[OKX][异常] 交易所移动止损挂单失败",
                             "激活价=%.1f 回撤=%.1f\n%s" % (act, off, e))

    def ensure_protective_stops(self):
        """核对交易所挂单: 撤孤儿、补齐缺失(可安全重复调用, 不会重复挂单)。"""
        if self.dry or self.inst_type != "SWAP" or self.eng.pos == 0 or self.pos_sz <= 0:
            return
        try:
            pend = self.client.algo_pending_all(self.inst)
        except OKXError as e:
            log("查询挂单失败: %s" % e)
            return
        ids = {a.get("algoId") for a in pend}
        for a in pend:
            if a.get("algoId") not in (self.stop_algo, self.trail_algo):
                self.client.cancel_algo(self.inst, a.get("algoId"))
        if not (self.stop_algo and self.stop_algo in ids):
            self.stop_algo = None
            self._place_hard_stop()
        if self.exit_mode == "exchange" and not (self.trail_algo and self.trail_algo in ids):
            self.trail_algo = None
            self._place_trailing()

    def place_protective_stop(self):
        """开仓后确保交易所侧保护单到位(硬止损 + exchange 模式的原生移动止损)。"""
        self.stop_algo = None
        self.trail_algo = None
        self.ensure_protective_stops()

    def sync_protective_stop(self):
        """local 模式: 移动止损激活后把交易所侧硬止损同步上移( >0.1ATR 才重挂)。

        exchange 模式: 移动止损由交易所原生单执行, 硬止损保持 -2ATR 不动。
        """
        if self.dry or self.inst_type != "SWAP" or self.eng.pos == 0 or self.pos_sz <= 0:
            return
        if self.exit_mode != "local":
            return
        if not self.eng.trail_on:
            return
        trig = self._round_tick(self.eng.stop_level())
        if self.stop_algo and self.stop_trigger is not None \
                and abs(trig - self.stop_trigger) < 0.1 * self.eng.atr_ref:
            return
        self.client.cancel_algo(self.inst, self.stop_algo)
        self.place_protective_stop()

    def notify_entry(self, signal, price, sz):
        side = "做多 LONG" if signal == "L" else "做空 SHORT"
        stop = self.eng.stop_level()
        self._open_info = {"signal": signal, "entry": price, "sz": sz,
                           "atr": self.eng.atr_ref, "stop": stop,
                           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.mailer.send(
            "[OKX] 开仓 %s @ %.1f" % (side, price),
            "%s\n合约/标的: %s\n数量: %s\n入场价: %.1f\n初始止损: %.1f (ATR=%.2f)\n"
            "模式: %s | 杠杆: %s\n时间: %s UTC" % (
                side, self.inst, sz, price, stop or 0, self.eng.atr_ref or 0,
                "模拟盘" if self.dry else "实盘", self.cfg["leverage"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))

    def notify_exit(self, price, reason):
        info = getattr(self, "_open_info", None) or {}
        entry = info.get("entry") or self.eng.entry_px
        chg = (price - entry) / entry * 100 * (1 if info.get("signal", "L") == "L" else -1) if entry else 0
        self._trades.append({"signal": info.get("signal"), "entry": entry,
                             "exit": price, "chg": chg, "reason": reason})
        self.mailer.send(
            "[OKX] 平仓(%s) @ %.1f" % (reason, price),
            "标的: %s\n入场: %.1f -> 出场: %.1f (%+.2f%%)\n止损原因: %s\n"
            "模式: %s\n时间: %s UTC" % (
                self.inst, entry, price, chg, reason,
                "模拟盘" if self.dry else "实盘", time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
        self._open_info = None

    def close_position(self, price):
        if self.dry:
            log("[dry] 平仓 @ %.1f  (盈亏 %.2f%%) -> 权益 %.2f" % (
                price, (price - self.sim_entry) / self.sim_entry * 100 * (1 if self.sim_pos > 0 else -1),
                self.equity()))
            self.notify_exit(price, "移动止损")
            self.sim_pos = 0.0
            self.eng.flat()
            return
        # 先撤交易所侧挂单(硬止损 + 移动止损), 避免残留/重复触发
        self.client.cancel_algo(self.inst, self.stop_algo)
        self.client.cancel_algo(self.inst, self.trail_algo)
        self.stop_algo = None; self.stop_trigger = None; self.trail_algo = None
        try:
            live = [p for p in self.client.positions(self.inst) if abs(float(p.get("pos", 0))) > 0]
            close_oids = []
            for p in live:
                side_p = float(p["pos"])
                close_side = "sell" if side_p > 0 else "buy"
                pos_side = p.get("posSide") if self.hedge else None
                try:
                    ack = self.client.place_order(self.inst, close_side, "market", abs(side_p),
                                                  td_mode=self.cfg["td_mode"], pos_side=pos_side,
                                                  reduce_only=True)
                    close_oids.append(ack.get("ordId"))
                    log("[live] 平仓 sz=%s ordId=%s" % (abs(side_p), ack.get("ordId")))
                except OKXError as e:
                    # 可能已被交易所侧止损平掉/或重复平仓, 稍后复查持仓为准
                    log("平仓下单异常(将复查持仓): %s" % e)
            time.sleep(1.0)
            remain = [p for p in self.client.positions(self.inst) if abs(float(p.get("pos", 0))) > 0]
            if remain:
                log("平仓后仍有持仓, 保留状态下一轮重试")
                if not getattr(self, "_close_alerted", False):
                    self._close_alerted = True
                    self.mailer.send("[OKX][紧急] 平仓未完成",
                                     "价格=%.1f\n剩余持仓: %s\n请检查!" % (
                                         price, [(p.get("posSide"), p.get("pos")) for p in remain]))
                return
            self._close_alerted = False
            # 用真实成交均价记账/通知
            exit_px = price
            for oid in close_oids:
                o = self.client.order_status(self.inst, oid)
                if o and float(o.get("avgPx") or 0) > 0:
                    exit_px = float(o["avgPx"])
            self.pos_sz = 0.0
            self.notify_exit(exit_px, "移动止损")
            self.eng.flat()
            self.save_state()
        except OKXError as e:
            log("平仓失败: %s" % e)
            self.mailer.send("[OKX][紧急] 平仓失败, 请人工处理",
                             "价格=%.1f\n%s\n当前持仓可能未平!" % (price, e))

    # ---------- 主循环 ----------
    def warmup(self):
        if self.inst_type == "SWAP":
            self.inst_info = self.client.instrument("SWAP", self.inst)
            if not self.dry:
                try:
                    self.hedge = self.client.account_config().get("posMode") == "long_short_mode"
                    self.client.set_leverage(self.inst, self.cfg["leverage"], self.cfg["td_mode"])
                    log("账户持仓模式: %s | 杠杆已设为 %sx" % (
                        "多空双向" if self.hedge else "净持仓", self.cfg["leverage"]))
                except OKXError as e:
                    log("账户配置获取/设杠杆失败(继续): %s" % e)
        need = self.cfg["warmup_bars"] + 60   # 多取一点保证指标预热
        raw = fetch_history(self.client, self.inst, self.cfg["bar"], need)
        agg = aggregate(raw, self.cfg["tf_min"])
        fed = 0
        for b, closed in agg[:-1]:
            if closed:
                self.eng.on_bar(b)
                self.last_bar_ts = b.ts
                fed += 1
        log("warmup: 喂入 %d 根 %dm K线, 当前价 %.1f, ATR=%.2f" %
            (fed, self.cfg["tf_min"], self.price(), self.eng.atr() or 0))
        self.reconcile()

    def reconcile(self):
        """启动时核对交易所持仓, 有则接管; 优先用落盘状态精确恢复。"""
        if self.dry:
            return
        try:
            live = [p for p in self.client.positions(self.inst) if abs(float(p.get("pos", 0))) > 0]
        except OKXError as e:
            log("核对持仓失败: %s" % e)
            return
        saved = self.load_state()
        if not live:
            if saved.get("pos"):
                log("启动核对: 状态记录有仓但交易所无仓, 以交易所为准清空状态")
            else:
                log("启动核对: 当前无持仓")
            self.eng.flat()
            # 清理可能残留的算法单
            try:
                for a in self.client.algo_pending_all(self.inst):
                    self.client.cancel_algo(self.inst, a.get("algoId"))
            except OKXError:
                pass
            self.stop_algo = None; self.trail_algo = None
            self.stop_trigger = None; self.pos_sz = 0.0
            self.save_state()
            return
        p = live[0]
        q = float(p["pos"]); entry = float(p.get("avgPx") or 0)
        self.pos_sz = abs(q)
        sig = 1 if q > 0 else -1
        if saved.get("pos") == sig and saved.get("entry_px"):
            # 精确恢复(保住 best/trail_on, 移动止损不倒退)
            self.eng.pos = sig
            self.eng.pos_signal = sig
            self.eng.entry_px = saved["entry_px"]
            self.eng.atr_ref = saved["atr_ref"]
            self.eng.best = saved["best"]
            self.eng.trail_on = saved["trail_on"]
            self.stop_algo = saved.get("stop_algo")
            self.trail_algo = saved.get("trail_algo")
            self.stop_trigger = saved.get("stop_trigger")
            log("启动核对: 按落盘状态恢复持仓 %+d 数量=%s (entry=%.1f best=%.1f trail=%s)" % (
                sig, q, self.eng.entry_px, self.eng.best, self.eng.trail_on))
        else:
            self.eng.enter("L" if sig > 0 else "S", entry)
            log("启动核对: 无落盘状态, 按均价接管持仓 %+d 数量=%s 均价=%.1f" % (sig, q, entry))
        self.mailer.send("[OKX] 启动接管持仓",
                         "方向=%+d 数量=%s 均价=%.1f\nATR=%.2f best=%.1f trail_on=%s\n时间=%s UTC" % (
                             sig, q, entry, self.eng.atr_ref, self.eng.best, self.eng.trail_on,
                             time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
        self.ensure_protective_stops()
        self.save_state()

    def _feed_bars(self):
        """喂入新收盘 K 线并返回当前价。优先 WebSocket, 失效时回退 REST。"""
        if self.use_ws and self.feed and not self.feed.stale(20):
            for (ts, o, h, l, c) in self.feed.drain_closed():
                if ts <= self.last_bar_ts:
                    continue
                self.last_bar_ts = ts
                sig = self.eng.on_bar(Bar(ts, o, h, l, c))
                if sig:
                    log("信号 %s @ bar %s close=%.1f" % (
                        sig, datetime.datetime.utcfromtimestamp(ts / 1000), c))
                    if self.eng.pos == 0:
                        self.open_position(sig, self.feed.price or self.price())
            return self.feed.price or self.price()
        raw = fetch_recent(self.client, self.inst, self.cfg["bar"], 30)
        agg = aggregate(raw, self.cfg["tf_min"])
        for b, closed in agg:
            if not closed or b.ts <= self.last_bar_ts:
                continue
            self.last_bar_ts = b.ts
            sig = self.eng.on_bar(b)
            if sig:
                log("信号 %s @ bar %s close=%.1f" % (
                    sig, datetime.datetime.utcfromtimestamp(b.ts / 1000), b.close))
                if self.eng.pos == 0:
                    self.open_position(sig, self.price())
        return self.price()

    def loop(self):
        log("启动 trader | inst=%s demo=%s dry_run=%s lev=%s qty=%s" %
            (self.inst, self.client.demo, self.dry, self.cfg["leverage"], self.cfg["qty_pct"]))
        if self.use_ws:
            self.feed = OKXFeed(self.inst, self.cfg["bar"])
            self.feed.start()
            log("WebSocket 行情已启动(带 REST 兜底)")
        self.warmup()
        self.mailer.send(
            "[OKX] 交易程序启动",
            "标的: %s\n模式: %s | 杠杆: %s | 仓位: %s%%\n参数: KC=%s lb=%s ATR=%s SL=%sATR Trail=%sATR\n时间: %s UTC" % (
                self.inst, "模拟盘" if self.dry else "实盘", self.cfg["leverage"],
                self.cfg["qty_pct"] * 100, self.cfg["length_kc"], self.cfg["lb"],
                self.cfg["atr_len"], self.cfg["sl_atr"], self.cfg["trail_atr"],
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
        max_iter = int(os.environ.get("MAX_ITER", "0")) or None
        it = 0
        while True:
            it += 1
            if max_iter and it > max_iter:
                log("达到 MAX_ITER=%d, 退出" % max_iter)
                break
            try:
                px = self._feed_bars()
                # 盘中止损检查
                if self.eng.pos != 0:
                    if self.dry or self.inst_type != "SWAP":
                        # 纯本地模拟: 内存移动止损驱动
                        act = self.eng.on_price(px)
                        if act == "EXIT":
                            log("触发移动止损 @ %.1f (stop=%.1f)" % (px, self.eng.stop_level()))
                            self.close_position(px)
                    else:
                        live = [p for p in self.client.positions(self.inst)
                                if abs(float(p.get("pos", 0))) > 0]
                        if not live:
                            # 交易所侧已平仓(硬止损或原生移动止损触发)
                            log("交易所侧已平仓")
                            self.client.cancel_algo(self.inst, self.stop_algo)
                            self.client.cancel_algo(self.inst, self.trail_algo)
                            exit_px = self._last_fill_px() or px
                            self.stop_algo = None; self.trail_algo = None
                            self.stop_trigger = None; self.pos_sz = 0.0
                            self.notify_exit(exit_px, "交易所止损")
                            self.eng.flat()
                            self.save_state()
                        elif self.exit_mode == "local":
                            act = self.eng.on_price(px)
                            if act == "EXIT":
                                log("触发移动止损 @ %.1f (stop=%.1f)" % (px, self.eng.stop_level()))
                                self.close_position(px)
                            else:
                                self.sync_protective_stop()
                        else:
                            # exchange 模式: 出场交给交易所, 本地仅更新记录
                            self.eng.on_price(px)
                else:
                    log("等待信号 | 价 %.1f | ATR %.2f | bar %s" % (
                        px, self.eng.atr() or 0,
                        datetime.datetime.utcfromtimestamp(self.last_bar_ts / 1000).strftime("%H:%M")))
                self.save_state()
            except OKXError as e:
                log("接口错误: %s" % e)
            time.sleep(self.cfg["poll_sec"])


if __name__ == "__main__":
    cfg = load_config()
    t = Trader(cfg)
    try:
        t.loop()
    except KeyboardInterrupt:
        log("手动停止")
        t.mailer.send("[OKX] 交易程序已手动停止", "时间: %s UTC" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:
        log("致命错误: %r" % e)
        t.mailer.send("[OKX][致命] 交易程序崩溃退出", "%r\n时间: %s UTC" % (e, time.strftime("%Y-%m-%d %H:%M:%S")))
        raise
