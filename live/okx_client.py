"""OKX REST v5 客户端: 公共行情 + 私有交易, 支持模拟盘(demo)。

密钥从环境变量读取:
  OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE, OKX_DEMO(1/0)
"""
import os, time, json, hmac, base64, hashlib, ssl, urllib.request, urllib.parse

ssl._create_default_https_context = ssl._create_unverified_context

BASE = os.environ.get("OKX_BASE", "https://www.okx.com")


class OKXError(Exception):
    pass


class OKXClient:
    def __init__(self, key=None, secret=None, passphrase=None, demo=None):
        self.key = key or os.environ.get("OKX_API_KEY", "")
        self.secret = secret or os.environ.get("OKX_API_SECRET", "")
        self.passphrase = passphrase or os.environ.get("OKX_API_PASSPHRASE", "")
        if demo is None:
            demo = os.environ.get("OKX_DEMO", "1") == "1"
        self.demo = demo
        self._inst_cache = {}

    # ---------- 底层 ----------
    def _ts(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%03dZ" % (int(time.time() * 1000) % 1000)

    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

    def _headers(self, method, path, body):
        h = {"Content-Type": "application/json", "User-Agent": self.UA}
        if not self.key:
            return h
        ts = self._ts()
        msg = ts + method + path + body
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        h.update({
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        })
        if self.demo:
            h["x-simulated-trading"] = "1"
        return h

    # OKX 认为"可稍后重试"的顶层错误码
    RETRYABLE = ("50011", "50013", "50026", "50004", "50061")

    def _request(self, method, path, params=None, body=None, signed=False,
                 tries=5, retry=True):
        """retry=False 时只请求一次(用于下单等非幂等操作, 避免重复成交)。"""
        params = params or {}
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        full = path + qs
        data = json.dumps(body) if body is not None else ""
        url = BASE + full
        attempts = tries if retry else 1
        last = None
        for i in range(attempts):
            try:
                req = urllib.request.Request(
                    url, data=data.encode() if data else None, method=method,
                    headers=self._headers(method, full, data))
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = json.loads(r.read())
            except urllib.error.HTTPError as e:
                last = "HTTP %s %s" % (e.code, e.read()[:200])
                time.sleep(0.5 * (i + 1))
                continue
            except Exception as e:
                last = str(e)
                time.sleep(0.5 * (i + 1))
                continue
            # 拿到响应(不再被 except 吞掉业务错误)
            code = d.get("code")
            if code == "0":
                return d["data"]
            last = "%s %s" % (code, d.get("msg", ""))
            if retry and code in self.RETRYABLE:
                time.sleep(0.5 * (i + 1))
                continue
            raise OKXError(last)
        raise OKXError("request failed: %s %s" % (full, last))

    # ---------- 公共 ----------
    def candles(self, inst_id, bar, limit=300, after=None):
        p = {"instId": inst_id, "bar": bar, "limit": limit}
        if after:
            p["after"] = after
        return self._request("GET", "/api/v5/market/history-candles", p)

    def ticker(self, inst_id):
        return self._request("GET", "/api/v5/market/ticker", {"instId": inst_id})

    def instrument(self, inst_type, inst_id):
        key = (inst_type, inst_id)
        if key not in self._inst_cache:
            d = self._request("GET", "/api/v5/public/instruments",
                              {"instType": inst_type, "instId": inst_id})
            self._inst_cache[key] = d[0] if d else None
        return self._inst_cache[key]

    # ---------- 私有 ----------
    def balance_usdt(self):
        d = self._request("GET", "/api/v5/account/balance", {"ccy": "USDT"}, signed=True)
        if not d:
            return 0.0
        for det in d[0].get("details", []):
            if det["ccy"] == "USDT":
                return float(det.get("availEq") or det.get("eq") or 0)
        return 0.0

    def positions(self, inst_id):
        return self._request("GET", "/api/v5/account/positions", {"instId": inst_id}, signed=True)

    def set_leverage(self, inst_id, lever, mgn_mode="cross"):
        return self._request("POST", "/api/v5/account/set-leverage",
                             body={"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode},
                             signed=True)

    def place_order(self, inst_id, side, ord_type, sz, td_mode="cross", pos_side=None,
                    px=None, reduce_only=None, cl_ord_id=None):
        """下单。成功返回 ack dict(含 ordId); 被拒抛 OKXError。不自动重试(防重复)。"""
        body = {"instId": inst_id, "tdMode": td_mode, "side": side, "ordType": ord_type, "sz": str(sz)}
        if pos_side:
            body["posSide"] = pos_side
        if px is not None:
            body["px"] = str(px)
        if reduce_only:
            body["reduceOnly"] = "true"
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        data = self._request("POST", "/api/v5/trade/order", body=body, signed=True, retry=False)
        r = data[0]
        if r.get("sCode") not in (None, "0", ""):
            raise OKXError("下单被拒 sCode=%s %s" % (r.get("sCode"), r.get("sMsg")))
        return r

    def order_status(self, inst_id, ord_id):
        d = self._request("GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id}, signed=True)
        return d[0] if d else None

    def cancel_order(self, inst_id, ord_id=None, cl_ord_id=None):
        body = {"instId": inst_id}
        if ord_id:
            body["ordId"] = ord_id
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        data = self._request("POST", "/api/v5/trade/cancel-order", body=body, signed=True, retry=False)
        r = data[0]
        # 已成交/已撤的单撤单会报错, 视为"已不在挂单中", 不抛异常
        if r.get("sCode") not in (None, "0", ""):
            return {"ordId": ord_id, "sCode": r.get("sCode"), "sMsg": r.get("sMsg")}
        return r

    def pending_orders(self, inst_id):
        return self._request("GET", "/api/v5/trade/orders-pending", {"instId": inst_id}, signed=True)

    def account_config(self):
        return self._request("GET", "/api/v5/account/config", signed=True)[0]
