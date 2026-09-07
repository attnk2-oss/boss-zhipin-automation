#!/usr/bin/env python3
"""BOSS 聊天 — 纯 Python MQTT 收发一体化（不依赖浏览器/CDP）。

连接后进入监听循环：服务器有新消息会通过 PUBLISH topic=chat 推送，
本脚本解码 protobuf 并打印。Ctrl-C 退出。

用法：python3 boss-listen.py [--seconds 30]
"""
import json
import random
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
import websockets.sync.client as wsc

try:
    from boss_config import state_path
    STATE = str(state_path())
except ImportError:
    from pathlib import Path as _P
    STATE = str(_P.home() / '.hermes' / 'scripts' / 'boss-zhipin-state.json')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'

# ---------- protobuf 编解码 ----------

def _varint(n):
    out = b''
    while True:
        b = n & 0x7f
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
    return f_bytes(field, s.encode('utf-8'))

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

def _parse_varint(buf, pos):
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7f) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7

def decode_proto(buf):
    """返回 {field: [(wire, value)]}，bytes 值为字节串。"""
    out = {}
    pos = 0
    while pos < len(buf):
        key, pos = _parse_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _parse_varint(buf, pos)
        elif wire == 2:
            ln, pos = _parse_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        else:
            break
        out.setdefault(field, []).append((wire, val))
    return out

def decode_tuser(buf):
    d = decode_proto(buf)
    uid = d.get(1, [(0, 0)])[0][1]
    enc = b''
    if 2 in d:
        enc = d[2][0][1]
    return {'uid': uid, 'encryptUid': enc.decode('utf-8', 'replace') if enc else ''}

def decode_message(buf):
    d = decode_proto(buf)
    msg = {}
    if 1 in d:
        msg['from'] = decode_tuser(d[1][0][1])
    if 2 in d:
        msg['to'] = decode_tuser(d[2][0][1])
    if 3 in d:
        msg['type'] = d[3][0][1]
    if 4 in d:
        msg['tempId'] = d[4][0][1]
    if 5 in d:
        msg['time'] = d[5][0][1]
    if 6 in d:
        bd = decode_proto(d[6][0][1])
        text = bd.get(3, [(0, b'')])[0][1]
        if isinstance(text, bytes):
            text = text.decode('utf-8', 'replace')
        msg['body'] = {'type': bd.get(1, [(0, 0)])[0][1], 'text': text}
    if 11 in d:
        msg['sourceUid'] = d[11][0][1]
    return msg

def decode_chat_protocol(buf):
    d = decode_proto(buf)
    msgs = []
    for _, m in d.get(3, []):
        msgs.append(decode_message(m))
    return {'type': d.get(1, [(0, None)])[0][1], 'messages': msgs}

# ---------- MQTT ----------

def _enc_remaining(n):
    out = b''
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out += bytes([d])
        if n == 0:
            return out

def mqtt_connect(client_id, username, password):
    payload = b''
    payload += b'\x00\x06MQIsdp\x03\xc2\x00\x19'
    cid = client_id.encode()
    payload += struct.pack('>H', len(cid)) + cid
    un = username.encode()
    payload += struct.pack('>H', len(un)) + un
    pw = password.encode()
    payload += struct.pack('>H', len(pw)) + pw
    return b'\x10' + _enc_remaining(len(payload)) + payload

def mqtt_publish(topic, payload_bytes, qos=1, msg_id=1):
    body = b''
    t = topic.encode()
    body += struct.pack('>H', len(t)) + t
    if qos > 0:
        body += struct.pack('>H', msg_id)
    body += payload_bytes
    flags = 0x30 | (qos << 1)
    return bytes([flags]) + _enc_remaining(len(body)) + body

def mqtt_pingreq():
    return b'\xc0\x00'

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
            rl += (b & 0x7f) * mult
            if not (b & 0x80):
                break
            mult *= 128
        end = p + rl
        if end > len(buf):
            return packets, buf[pos:]
        packets.append(buf[pos:end])
        pos = end
    return packets, b''

# ---------- HTTP 凭据 ----------

def _build_opener():
    state = json.load(open(STATE))
    cj = http.cookiejar.CookieJar()
    for c in state.get('cookies', []):
        ck = http.cookiejar.Cookie(
            version=0, name=c.get('name', ''), value=c.get('value', ''),
            port=None, port_specified=False,
            domain=c.get('domain', ''), domain_specified=bool(c.get('domain')),
            domain_initial_dot=c.get('domain', '').startswith('.'),
            path=c.get('path', '/'), path_specified=True,
            secure=bool(c.get('secure')), expires=c.get('expirationDate'),
            discard=False, comment=None, comment_url=None, rest={},
            rfc2109=False,
        )
        cj.set_cookie(ck)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def _http_get(url):
    opener = _build_opener()
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Referer': 'https://www.zhipin.com/web/geek/chat',
        'X-Requested-With': 'XMLHttpRequest',
    })
    with opener.open(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))

def get_credentials():
    user = _http_get('https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json').get('zpData', {})
    wt = _http_get('https://www.zhipin.com/wapi/zppassport/get/wt').get('zpData', {})
    servers = _http_get('https://www.zhipin.com/wapi/zpchat/config/ws').get('zpData', {}).get('result', ['ws.zhipin.com'])
    return {
        'token': user.get('token', ''),
        'userId': str(user.get('userId', '')),
        'name': user.get('name', ''),
        'avatar': user.get('tinyAvatar', ''),
        'encryptUserId': user.get('encryptUserId', ''),
        'wt': wt.get('wt2') or wt.get('wt', ''),
        'server': servers[0],
    }

def cookie_str():
    state = json.load(open(STATE))
    return '; '.join(c['name'] + '=' + (c.get('value') or '')
                     for c in state.get('cookies', []))

# ---------- 主逻辑 ----------

class BossChat:
    def __init__(self):
        self.creds = None
        self.sock = None
        self.my_uid = None

    def connect(self):
        self.creds = get_credentials()
        self.my_uid = int(self.creds['userId'])
        client_id = 'ws-' + str(int(time.time() * 1000)) + str(random.randint(10, 99))
        uri = f'wss://{self.creds["server"]}:443/chatws'
        username = self.creds['token'] + '|0'
        hdrs = {'Origin': 'https://www.zhipin.com',
                'User-Agent': UA,
                'Cookie': cookie_str()}
        self.sock = wsc.connect(uri, additional_headers=hdrs,
                                ssl_context=ssl.create_default_context(),
                                open_timeout=10)
        self.sock.send(mqtt_connect(client_id, username, self.creds['wt']))
        buf = b''
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                msg = self.sock.recv(timeout=3)
                if isinstance(msg, bytes):
                    buf += msg
            except Exception:
                break
            packets, buf = parse_mqtt_packets(buf)
            for pkt in packets:
                if pkt[0] == 0x20:
                    if len(pkt) < 4 or pkt[3] != 0:
                        raise RuntimeError(f'CONNACK refused code={pkt[3] if len(pkt) > 3 else "?"}')
                    return True
        raise RuntimeError('CONNACK timeout')

    def recv_publish(self, timeout=30):
        """等一个 PUBLISH，返回 (topic, payload_bytes) 或 None。"""
        buf = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.sock.recv(timeout=3)
                if isinstance(msg, bytes):
                    buf += msg
            except Exception:
                continue
            packets, buf = parse_mqtt_packets(buf)
            for pkt in packets:
                first = pkt[0]
                if (first & 0xf0) == 0x30:  # PUBLISH
                    pos = 1
                    rl = 0
                    mult = 1
                    while True:
                        b = pkt[pos]
                        pos += 1
                        rl += (b & 0x7f) * mult
                        if not (b & 0x80):
                            break
                        mult *= 128
                    tlen = struct.unpack('>H', pkt[pos:pos + 2])[0]
                    pos += 2
                    topic = pkt[pos:pos + tlen].decode()
                    pos += tlen
                    qos = (first >> 1) & 3
                    if qos > 0:
                        pos += 2  # packet id
                    payload = pkt[pos:]
                    return topic, payload
        return None

    def send_text(self, to_uid, to_encrypt_uid, text):
        import urllib.parse as up
        params = up.urlencode({'page': 1, 'size': 100})
        d = _http_get(f'https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?{params}')
        res = d.get('zpData', {}).get('result', []) or d.get('zpData', {}).get('friendList', [])
        mx = 0
        for it in res:
            mx = max(mx, (it.get('lastMessageInfo') or {}).get('msgId', 0))
        temp_id = mx + 1
        time_ms = int(time.time() * 1000)
        frm = encode_tuser(self.my_uid, self.creds.get('encryptUserId', ''))
        to = encode_tuser(int(to_uid), to_encrypt_uid)
        msg = encode_message(frm, to, text, temp_id, time_ms)
        payload = encode_chat_protocol([msg])
        mid = random.randint(1, 65535)
        self.sock.send(mqtt_publish('chat', payload, qos=1, msg_id=mid))
        # 等 PUBACK
        buf = b''
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                m = self.sock.recv(timeout=3)
                if isinstance(m, bytes):
                    buf += m
            except Exception:
                continue
            packets, buf = parse_mqtt_packets(buf)
            for pkt in packets:
                if pkt[0] == 0x40:
                    return True
        return False

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


if __name__ == '__main__':
    seconds = 30
    if '--seconds' in sys.argv:
        seconds = int(sys.argv[sys.argv.index('--seconds') + 1])
    bc = BossChat()
    bc.connect()
    print(f'[ok] connected, my_uid={bc.my_uid}, listening {seconds}s...', file=sys.stderr)
    end = time.time() + seconds
    while time.time() < end:
        got = bc.recv_publish(timeout=5)
        if got is None:
            print('[.] nothing in 5s...', file=sys.stderr)
            continue
        topic, payload = got
        print(f'[+] PUBLISH topic={topic} len={len(payload)}', file=sys.stderr)
        try:
            proto = decode_chat_protocol(payload)
            for m in proto.get('messages', []):
                frm = m.get('from', {})
                who = 'ME' if frm.get('uid') == bc.my_uid else f"BOSS({frm.get('uid')})"
                body = m.get('body', {})
                print(f'[{who}] type={m.get("type")} time={m.get("time")} text={(body.get("text") or "")[:80]}')
        except Exception as e:
            print(f'[!] decode failed: {e}', file=sys.stderr)
    bc.close()
