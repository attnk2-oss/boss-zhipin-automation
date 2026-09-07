#!/usr/bin/env python3
"""BOSS 聊天 — 纯 Python MQTT 发送单条消息（不依赖浏览器/CDP）。

用法：
  python3 boss-send.py <对方uid> <对方encryptUid> <消息文本>

流程：HTTP 拿凭据 → friend list 计算 maxMsgId（tempID 基准）→
      WebSocket 连 /chatws → MQTT 3.1 CONNECT → PUBLISH topic=chat
      （protobuf TechwolfChatProtocol）→ 等 PUBACK → 历史接口确认落库。
"""
import json
import os
import random
import ssl
import struct
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
import websockets.sync.client as wsc

# ⛔ 强制直连 BOSS：gateway 环境带 Clash 代理(7897)，BOSS 是国内站，走代理会被
# 风控(code 7/37)且 cookie/IP 不匹配。必须在此清掉所有代理环境变量 + urllib/ws 显式 NoProxy。
import os as _os
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    _os.environ.pop(_k, None)

try:
    from boss_config import state_path
    STATE = str(state_path())
except ImportError:
    from pathlib import Path as _P
    STATE = str(_P.home() / '.hermes' / 'scripts' / 'boss-zhipin-state.json')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'

# ---------- protobuf 编码 ----------

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

# ---------- 凭据 ----------

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
    # 显式 NoProxy：即使环境变量在调用方设置，urllib 也绝不走代理
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({}),
    )

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

def get_max_msg_id():
    """所有会话 lastMessageInfo.msgId 的最大值（tempID 基准）。"""
    params = urllib.parse.urlencode({'page': 1, 'size': 100})
    d = _http_get(f'https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?{params}')
    res = d.get('zpData', {}).get('result', []) or d.get('zpData', {}).get('friendList', [])
    mx = 0
    for it in res:
        mx = max(mx, (it.get('lastMessageInfo') or {}).get('msgId', 0))
    return mx

def find_friend(security_id=None, name=None, brand=None):
    """从 friend list 找联系人，返回 {uid, encryptUid, securityId, name, brandName}。"""
    params = urllib.parse.urlencode({'page': 1, 'size': 100})
    d = _http_get(f'https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?{params}')
    res = d.get('zpData', {}).get('result', []) or d.get('zpData', {}).get('friendList', [])
    for it in res:
        if security_id and it.get('securityId') == security_id:
            return {'uid': str(it.get('uid', '')), 'encryptUid': it.get('encryptUid', ''),
                    'securityId': it.get('securityId', ''), 'name': it.get('name', ''),
                    'brandName': it.get('brandName', '')}
    for it in res:
        if name and it.get('name') == name and (not brand or it.get('brandName') == brand):
            return {'uid': str(it.get('uid', '')), 'encryptUid': it.get('encryptUid', ''),
                    'securityId': it.get('securityId', ''), 'name': it.get('name', ''),
                    'brandName': it.get('brandName', '')}
    return None

def _last_send_ts():
    p = Path(__file__).resolve().parent / '.last_send_ts'
    try:
        return float(p.read_text().strip())
    except Exception:
        return 0.0

def _mark_send():
    Path(__file__).resolve().parent.joinpath('.last_send_ts').write_text(str(time.time()))

MIN_SEND_INTERVAL = 45  # 秒；低于此间隔直接拒绝（服务端风控，实测连续5条后被踢）

DAEMON_SOCKET = '/tmp/boss-mqtt-daemon.sock'

def send_via_daemon(to_uid, to_encrypt_uid, text):
    """走常驻 daemon（长连接，不重复握手）。daemon 存在时必须走 daemon——
    双连接会互踢触发风控。daemon 不存在时返回 None 走直连。"""
    if not os.path.exists(DAEMON_SOCKET):
        return None
    s = None
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(30)
        s.connect(DAEMON_SOCKET)
        req = json.dumps({'uid': str(to_uid), 'encrypt_uid': to_encrypt_uid, 'text': text}, ensure_ascii=False)
        s.sendall((req + '\n').encode('utf-8'))
        buf = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b'\n' in buf:
                break
        s.close()
        resp = json.loads(buf.decode('utf-8', errors='replace').strip())
        if resp.get('ok'):
            print('[ok] via daemon', file=sys.stderr)
            return True
        if 'cooldown' in resp.get('error', ''):
            raise RuntimeError(resp['error'])
        # daemon 报其他错（如被风控踢）：不回退直连，直接抛错让调用方知道
        raise RuntimeError(f"daemon send failed: {resp.get('error', 'unknown')}")
    except Exception as e:
        # daemon 在但发送失败：不回退直连（双连接互踢触发风控），抛错
        print(f'[warn] daemon send failed: {e}', file=sys.stderr)
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        raise

def send_message(to_uid, to_encrypt_uid, text):
    dt = time.time() - _last_send_ts()
    if dt < MIN_SEND_INTERVAL:
        raise RuntimeError(f'cooldown: last send {dt:.0f}s ago, wait {MIN_SEND_INTERVAL - dt:.0f}s more')
    creds = get_credentials()
    my_uid = int(creds['userId'])
    temp_id = get_max_msg_id() + 1
    time_ms = int(time.time() * 1000)

    frm = encode_tuser(my_uid, creds.get('encryptUserId', ''))
    to = encode_tuser(int(to_uid), to_encrypt_uid)
    msg = encode_message(frm, to, text, temp_id, time_ms)
    payload = encode_chat_protocol([msg])
    print(f'[info] temp_id={temp_id} time={time_ms} payload_len={len(payload)}', file=sys.stderr)

    client_id = 'ws-' + str(int(time.time() * 1000)) + str(random.randint(10, 99))
    uri = f'wss://{creds["server"]}:443/chatws'
    username = creds['token'] + '|0'
    state = json.load(open(STATE))
    cookie_str = '; '.join(c['name'] + '=' + (c.get('value') or '')
                           for c in state.get('cookies', []))
    hdrs = {'Origin': 'https://www.zhipin.com',
            'User-Agent': UA,
            'Cookie': cookie_str}
    sock = wsc.connect(uri, additional_headers=hdrs,
                       ssl_context=ssl.create_default_context(),
                       open_timeout=10)
    try:
        sock.send(mqtt_connect(client_id, username, creds['wt']))
        # 等 CONNACK
        buf = b''
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
                        raise RuntimeError(f'CONNACK refused code={pkt[3]}')
                    connected = True
                    break
            if connected:
                break
        if not connected:
            raise RuntimeError('CONNACK timeout')

        # PUBLISH
        mid = random.randint(1, 65535)
        sock.send(mqtt_publish('chat', payload, qos=1, msg_id=mid))
        print(f'[info] PUBLISH sent msg_id={mid}', file=sys.stderr)

        # 等 PUBACK
        buf = b''
        deadline = time.time() + 10
        acked = False
        while time.time() < deadline:
            try:
                msg = sock.recv(timeout=3)
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
            raise RuntimeError('PUBACK timeout')
        print('[ok] PUBACK received', file=sys.stderr)
        time.sleep(2)  # 等服务端落库
        _mark_send()
        return True
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='BOSS 纯 Python MQTT 发送')
    ap.add_argument('text', help='消息文本')
    ap.add_argument('--uid', help='对方 uid（数字）')
    ap.add_argument('--encrypt-uid', help='对方 encryptUid')
    ap.add_argument('--security-id', help='friend list 里的 securityId（自动映射 uid/encryptUid）')
    ap.add_argument('--name', help='按姓名查找（配合 --brand 更准）')
    ap.add_argument('--brand', default='', help='公司名辅助定位')
    args = ap.parse_args()

    if args.security_id or args.name:
        f = find_friend(security_id=args.security_id, name=args.name, brand=args.brand)
        if not f:
            print(json.dumps({'ok': False, 'stage': 'find', 'error': 'friend not found',
                              'known': [it.get('name') for it in _http_get(
                                  'https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?' +
                                  urllib.parse.urlencode({'page': 1, 'size': 100})).get('zpData', {}).get('result', [])][:30]},
                             ensure_ascii=False))
            sys.exit(2)
        to_uid, to_enc = f['uid'], f['encryptUid']
        print(f'[info] target: {f.get("name")} / {f.get("brandName")} uid={to_uid}', file=sys.stderr)
    elif args.uid and args.encrypt_uid:
        to_uid, to_enc = args.uid, args.encrypt_uid
    else:
        ap.error('需要 --uid+--encrypt-uid 或 --security-id 或 --name')

    # 优先 daemon 长连接；daemon 不在/失败 → 直连
    sent = send_via_daemon(to_uid, to_enc, args.text)
    if sent is None:
        sent = send_message(to_uid, to_enc, args.text)
    print('SENT' if sent else 'FAILED')
