#!/usr/bin/env python3
"""BOSS MQTT 长连接 daemon — 保持一条 WebSocket/MQTT 连接反复发送。

背景：每次发送都重新 get_credentials(3个HTTP) + get_max_msg_id(1个HTTP)
+ 新建 WebSocket + MQTT CONNECT 握手，重复请求易触发风控且慢。
本 daemon 启动时建立连接并保持，通过 Unix socket 接收发送任务，
串行发送（尊重 45s 冷却），断线自动重连。

用法：
  python3 boss-mqtt-daemon.py            # 前台运行
  python3 boss-mqtt-daemon.py --once     # 发送完 stdin 上的任务后退出(测试用)

客户端：boss-send.py 检测 socket 存在时自动走 daemon，否则直连。
"""
from __future__ import annotations

import json
import os
import random
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

# ⛔ 强制直连 BOSS：清掉 gateway 继承的 Clash 代理，urllib 显式 NoProxy
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

import websockets.sync.client as wsc

STATE = Path.home() / ".hermes" / "scripts" / "boss-zhipin-state.json"
SOCKET_PATH = "/tmp/boss-mqtt-daemon.sock"
PID_PATH = "/tmp/boss-mqtt-daemon.pid"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
MIN_SEND_INTERVAL = 45  # 秒；低于此间隔直接拒绝（服务端风控）
KEEPALIVE = 60          # MQTT keepalive 秒

# ---------- protobuf 编码（与 boss-send.py 相同） ----------

def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out

def _key(field, wire):
    return _varint((field << 3) | wire)

def f_varint(field, val):
    return _key(field, 0) + _varint(val)

def f_bytes(field, data):
    return _key(field, 2) + _varint(len(data)) + data

def f_str(field, s):
    return f_bytes(field, s.encode("utf-8"))

def encode_tuser(uid, encrypt_uid, source=0):
    b = f_varint(1, uid)
    if encrypt_uid:
        b += f_str(2, encrypt_uid)
    b += f_varint(7, source)
    return b

def encode_message(frm, to, text, temp_id, time_ms):
    body = f_varint(1, 1) + f_varint(2, 1) + f_str(3, text)
    return (f_bytes(1, frm) + f_bytes(2, to) + f_varint(3, 1)
            + f_varint(4, temp_id) + f_varint(5, time_ms)
            + f_bytes(6, body) + f_varint(11, temp_id))

def encode_chat_protocol(messages, ptype=1):
    b = f_varint(1, ptype)
    for m in messages:
        b += f_bytes(3, m)
    return b

# ---------- MQTT ----------

def _enc_remaining(n):
    out = b""
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out += bytes([d])
        if n == 0:
            return out

def mqtt_connect(client_id, username, password, keepalive=KEEPALIVE):
    payload = b""
    payload += b"\x00\x06MQIsdp\x03\xc2"
    payload += struct.pack(">H", keepalive)
    cid = client_id.encode()
    payload += struct.pack(">H", len(cid)) + cid
    un = username.encode()
    payload += struct.pack(">H", len(un)) + un
    pw = password.encode()
    payload += struct.pack(">H", len(pw)) + pw
    return b"\x10" + _enc_remaining(len(payload)) + payload

def mqtt_pingreq():
    return b"\xc0\x00"

def mqtt_publish(topic, payload_bytes, qos=1, msg_id=1):
    body = b""
    t = topic.encode()
    body += struct.pack(">H", len(t)) + t
    if qos > 0:
        body += struct.pack(">H", msg_id)
    body += payload_bytes
    flags = 0x30 | (qos << 1)
    return bytes([flags]) + _enc_remaining(len(body)) + body

def parse_mqtt_packets(buf):
    packets = []
    pos = 0
    while pos < len(buf):
        if len(buf) - pos < 2:
            break
        mult = 1
        rl = 0
        p = pos + 1
        while True:
            if p >= len(buf):
                return packets, buf[pos:]
            b = buf[p]
            p += 1
            rl += (b & 0x7F) * mult
            if not (b & 0x80):
                break
            mult *= 128
        end = p + rl
        if end > len(buf):
            return packets, buf[pos:]
        packets.append(buf[pos:end])
        pos = end
    return packets, b""

# ---------- HTTP 凭据 ----------

def _build_opener():
    state = json.load(open(STATE))
    cj = http.cookiejar.CookieJar()
    for c in state.get("cookies", []):
        ck = http.cookiejar.Cookie(
            version=0, name=c.get("name", ""), value=c.get("value", ""),
            port=None, port_specified=False,
            domain=c.get("domain", ""), domain_specified=bool(c.get("domain")),
            domain_initial_dot=c.get("domain", "").startswith("."),
            path=c.get("path", "/"), path_specified=True,
            secure=bool(c.get("secure")), expires=c.get("expirationDate"),
            discard=False, comment=None, comment_url=None, rest={},
            rfc2109=False,
        )
        cj.set_cookie(ck)
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({}),
    )

def _http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
    })
    with _build_opener().open(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))

def get_credentials():
    user = _http_get("https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json").get("zpData", {})
    wt = _http_get("https://www.zhipin.com/wapi/zppassport/get/wt").get("zpData", {})
    servers = _http_get("https://www.zhipin.com/wapi/zpchat/config/ws").get("zpData", {}).get("result", ["ws.zhipin.com"])
    return {
        "token": user.get("token", ""),
        "userId": str(user.get("userId", "")),
        "name": user.get("name", ""),
        "encryptUserId": user.get("encryptUserId", ""),
        "wt": wt.get("wt2") or wt.get("wt", ""),
        "server": servers[0],
    }

def get_max_msg_id():
    params = urllib.parse.urlencode({"page": 1, "size": 100})
    d = _http_get(f"https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?{params}")
    res = d.get("zpData", {}).get("result", []) or d.get("zpData", {}).get("friendList", [])
    mx = 0
    for it in res:
        mx = max(mx, (it.get("lastMessageInfo") or {}).get("msgId", 0))
    return mx

def find_friend(uid=None, encrypt_uid=None):
    """从 friend list 拿对方的 encryptUid（uid 精确匹配）。"""
    params = urllib.parse.urlencode({"page": 1, "size": 100})
    d = _http_get(f"https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?{params}")
    res = d.get("zpData", {}).get("result", []) or d.get("zpData", {}).get("friendList", [])
    for it in res:
        if uid and str(it.get("uid", "")) == str(uid):
            return {"uid": str(it.get("uid", "")), "encryptUid": it.get("encryptUid", ""),
                    "name": it.get("name", ""), "brandName": it.get("brandName", "")}
    return None

# ---------- 连接管理器 ----------

class MqttSender:
    """持有长连接，串行发送，断线重连。"""

    def __init__(self):
        self._sock = None
        self._buf = b""
        self._temp_id = 0
        self._my_uid = 0
        self._creds = None
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._stop = False

    # ---- 连接 ----

    def _connect(self):
        creds = get_credentials()
        self._creds = creds
        self._my_uid = int(creds["userId"])
        self._temp_id = get_max_msg_id()
        client_id = "ws-" + str(int(time.time() * 1000)) + str(random.randint(10, 99))
        uri = f"wss://{creds['server']}:443/chatws"
        username = creds["token"] + "|0"
        state = json.load(open(STATE))
        cookie_str = "; ".join(c["name"] + "=" + (c.get("value") or "") for c in state.get("cookies", []))
        hdrs = {"Origin": "https://www.zhipin.com", "User-Agent": UA, "Cookie": cookie_str}
        sock = wsc.connect(uri, additional_headers=hdrs,
                           ssl_context=ssl.create_default_context(),
                           open_timeout=10)
        sock.send(mqtt_connect(client_id, username, creds["wt"]))
        buf = b""
        deadline = time.time() + 8
        connected = False
        while time.time() < deadline:
            try:
                msg = sock.recv(timeout=3)
                if isinstance(msg, bytes):
                    buf += msg
            except Exception:
                break
            packets, buf = parse_mqtt_packets(buf)
            for pkt in packets:
                if pkt[0] == 0x20 and len(pkt) >= 4:
                    if pkt[3] != 0:
                        sock.close()
                        raise RuntimeError(f"CONNACK refused code={pkt[3]}")
                    connected = True
                    break
            if connected:
                break
        if not connected:
            sock.close()
            raise RuntimeError("CONNACK timeout")
        self._sock = sock
        self._buf = b""
        print(f"[daemon] connected server={creds['server']} temp_id_base={self._temp_id}",
              file=sys.stderr, flush=True)

    def _ensure_connected(self):
        if self._sock is None:
            self._connect()
            return
        # 探测连接是否活着：非阻塞 peek
        try:
            self._sock.send(mqtt_pingreq())
        except Exception:
            self._sock = None
            self._connect()

    # ---- 发送 ----

    def send(self, to_uid, to_encrypt_uid, text):
        with self._lock:
            dt = time.time() - self._last_send
            if dt < MIN_SEND_INTERVAL:
                raise RuntimeError(
                    f"cooldown: last send {dt:.0f}s ago, wait {MIN_SEND_INTERVAL - dt:.0f}s more")
            self._ensure_connected()
            self._temp_id += 1
            time_ms = int(time.time() * 1000)
            frm = encode_tuser(self._my_uid, self._creds.get("encryptUserId", ""))
            to = encode_tuser(int(to_uid), to_encrypt_uid)
            msg = encode_message(frm, to, text, self._temp_id, time_ms)
            payload = encode_chat_protocol([msg])
            mid = random.randint(1, 65535)
            self._sock.send(mqtt_publish("chat", payload, qos=1, msg_id=mid))
            # 等 PUBACK（清理其他响应包）
            buf = b""
            deadline = time.time() + 10
            acked = False
            while time.time() < deadline:
                try:
                    msg = self._sock.recv(timeout=3)
                    if isinstance(msg, bytes):
                        buf += msg
                except Exception:
                    continue
                packets, buf = parse_mqtt_packets(buf)
                for pkt in packets:
                    if pkt[0] == 0x40:
                        acked = True
                        break
                if acked:
                    break
            if not acked:
                raise RuntimeError("PUBACK timeout")
            time.sleep(2)  # 等服务端落库
            self._last_send = time.time()
            return True

    # ---- 保活 ----

    def _keepalive_loop(self):
        while not self._stop:
            time.sleep(KEEPALIVE / 2)
            try:
                with self._lock:
                    if self._sock is not None:
                        self._sock.send(mqtt_pingreq())
            except Exception:
                try:
                    self._sock = None
                    self._connect()
                except Exception as e:
                    print(f"[daemon] reconnect failed: {e}", file=sys.stderr, flush=True)

    def close(self):
        self._stop = True
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass

# ---------- Unix socket 服务 ----------

def run_server(once=False):
    sender = MqttSender()
    # 启动时先连一次，失败直接退出（让上层知道）
    sender._ensure_connected()
    print("[daemon] ready", file=sys.stderr, flush=True)

    threading.Thread(target=sender._keepalive_loop, daemon=True).start()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(8)
    os.chmod(SOCKET_PATH, 0o600)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

    def handle(conn):
        try:
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 100_000:
                    break
                # 协议：一行 JSON + \n 结束
                if b"\n" in data:
                    break
            line = data.decode("utf-8", errors="replace").strip()
            if not line:
                return
            req = json.loads(line)
            try:
                ok = sender.send(req["uid"], req.get("encrypt_uid", ""), req["text"])
                conn.sendall((json.dumps({"ok": ok, "stage": "sent"}) + "\n").encode())
            except Exception as e:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()

    def on_sigterm(sig, frame):
        sender.close()
        try:
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    signal.signal(signal.SIGINT, on_sigterm)

    while True:
        try:
            conn, _ = server.accept()
        except KeyboardInterrupt:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
        if once:
            time.sleep(1)
            break

    sender.close()
    try:
        os.unlink(SOCKET_PATH)
    except Exception:
        pass


if __name__ == "__main__":
    once = "--once" in sys.argv
    try:
        run_server(once=once)
    except Exception as e:
        print(f"[daemon] fatal: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
