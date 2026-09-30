"""极简 WebSocket 客户端 + OKX 行情订阅(纯标准库, 无第三方依赖)。

只实现客户端必需的子集: 文本帧收发 / ping-pong / 分片 / 自动重连。
OKXFeed 后台线程维护最新价与已收盘 K 线, 供交易主循环读取。
"""
import os, ssl, json, time, struct, socket, base64, hashlib, threading, random

# OKX K线频道在 business 端点; 模拟盘用 wspap
WS_BUSINESS = os.environ.get(
    "OKX_WS_URL",
    "wss://wspap.okx.com:8443/ws/v5/business?brokerId=9999" if os.environ.get("OKX_DEMO", "1") == "1"
    else "wss://ws.okx.com:8443/ws/v5/business")


class WSClient:
    def __init__(self, url, timeout=20):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    def connect(self):
        from urllib.parse import urlparse
        u = urlparse(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw, server_hostname=host)
        else:
            self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\nUser-Agent: okx-bot\r\n\r\n") % (path, host, key)
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake closed")
            resp += chunk
        if b"101" not in resp.split(b"\r\n")[0]:
            raise ConnectionError("handshake failed: %r" % resp[:120])
        self._buf = b""

    def send(self, text):
        data = text.encode()
        header = bytearray([0x81])          # FIN + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126); header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127); header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, n):
        buf = self._buf
        while len(buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        self._buf = buf[n:]
        return buf[:n]

    def recv(self):
        """返回一个完整文本消息(str), 处理 ping/分片; 无消息时阻塞。"""
        data = b""
        while True:
            b0, b1 = self._read_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            payload = self._read_exact(ln)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:                # close
                raise ConnectionError("server close")
            if opcode == 0x9:                # ping -> pong
                self._send_control(0xA, payload); continue
            if opcode == 0xA:                # pong
                continue
            data += payload
            if fin:
                return data.decode("utf-8", "ignore")

    def _send_control(self, opcode, payload=b""):
        header = bytearray([0x80 | opcode])
        header.append(0x80 | len(payload))
        mask = os.urandom(4); header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header))

    def close(self):
        try:
            self._send_control(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class OKXFeed:
    """订阅 OKX candle{bar} + tickers, 维护最新价与已收盘K线。"""

    def __init__(self, inst_id, bar="1m"):
        self.inst_id = inst_id
        self.bar = bar
        self.price = None
        self._closed = []          # [(ts,o,h,l,c)]
        self._lock = threading.Lock()
        self._last_msg = 0.0
        self._stop = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="okx-ws")
        self._thread.start()

    def stop(self):
        self._stop = True

    def stale(self, sec=15):
        return (time.time() - self._last_msg) > sec

    def drain_closed(self):
        with self._lock:
            out = self._closed
            self._closed = []
        return out

    def _run(self):
        backoff = 1
        while not self._stop:
            try:
                ws = WSClient(WS_BUSINESS)
                ws.connect()
                ws.send(json.dumps({"op": "subscribe", "args": [
                    {"channel": "candle" + self.bar, "instId": self.inst_id}]}))
                backoff = 1
                while not self._stop:
                    msg = ws.recv()
                    self._handle(msg)
            except Exception:
                if self._stop:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle(self, msg):
        if not msg or msg == "pong":
            return
        try:
            d = json.loads(msg)
        except Exception:
            return
        self._last_msg = time.time()
        arg = d.get("arg", {}); data = d.get("data") or []
        ch = arg.get("channel", "")
        if ch.startswith("candle") and data:
            row = data[0]
            ts = int(row[0]); o, h, l, c = map(float, row[1:5])
            confirm = row[8] if len(row) > 8 else "0"
            self.price = c
            if confirm == "1":
                with self._lock:
                    self._closed.append((ts, o, h, l, c))


if __name__ == "__main__":
    import sys
    inst = sys.argv[1] if len(sys.argv) > 1 else "BTC-USDT-SWAP"
    f = OKXFeed(inst, "1m")
    f.start()
    t0 = time.time()
    while time.time() - t0 < 20:
        time.sleep(2)
        cl = f.drain_closed()
        print("price=%s closed=%s stale=%s" % (f.price, cl[-2:], f.stale()))
