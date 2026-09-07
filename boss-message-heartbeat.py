#!/usr/bin/env python3
"""Collect BOSS inbound conversation changes for an Agent cron.

Pure HTTP (friend list API), no CLI, no CDP, no browser. Compares a small
local state file and prints a compact JSON event when a conversation's last
message changes AND the last message was sent by the other side (fromId !=
my uid). Empty output means no event.

The friend list API returns uid/encryptUid/securityId directly, so the Agent
can reply via boss-mqtt/boss-send.py without ever touching CDP.
"""
from __future__ import annotations

# ⛔ 强制直连 BOSS：gateway 环境带 Clash 代理(7897)，BOSS 是国内站，走代理会被
# 风控(code 7/37)且 cookie/IP 不匹配。必须在此清掉所有代理环境变量 + urllib 显式 NoProxy。
import os as _os
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    _os.environ.pop(_k, None)
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path
from typing import Any

try:
    from boss_config import my_uid
except ImportError:  # script run from elsewhere without repo root on path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from boss_config import my_uid

STATE_PATH = Path.home() / ".hermes" / "cache" / "boss-message-heartbeat.json"
MAX_EVENTS = 10

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def load_state() -> dict[str, Any]:
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("conversations"), dict):
            d.setdefault("pending", {})
            return d
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"conversations": {}, "pending": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="boss-heartbeat-", dir=STATE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(tmp_name, STATE_PATH)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def build_opener() -> Any:
    state_path = Path.home() / ".hermes" / "scripts" / "boss-zhipin-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
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
    # 显式 NoProxy：即使环境变量在调用方设置，urllib 也绝不走代理
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({}),
    )


def fetch_friends() -> list[dict[str, Any]]:
    """HTTP friend list，返回规范化字段。"""
    url = ("https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?"
           + urllib.parse.urlencode({"page": 1, "size": 100}))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        with build_opener().open(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    res = d.get("zpData", {}).get("result", []) or d.get("zpData", {}).get("friendList", [])
    out = []
    for it in res:
        lmi = it.get("lastMessageInfo") or {}
        out.append({
            "uid": str(it.get("uid", "")),
            "encrypt_uid": it.get("encryptUid", ""),
            "security_id": it.get("securityId", ""),
            "name": it.get("name", ""),
            "brand_name": it.get("brandName", ""),
            "title": it.get("title", ""),
            "encrypt_job_id": it.get("encryptJobId", ""),
            "last_msg": it.get("lastMsg", ""),
            "last_ts": it.get("lastTS"),
            "unread": it.get("unreadMsgCount", 0),
            "last_msg_id": lmi.get("msgId"),
            "last_from_id": lmi.get("fromId"),
        })
    return out


def stable_key(item: dict[str, Any]) -> str:
    """uid 是稳定身份；encryptJobId 备用。"""
    return str(item.get("uid") or item.get("encrypt_job_id"))


def fingerprint(item: dict[str, Any]) -> str:
    return "|".join(str(item.get(k, "")) for k in ("last_msg_id", "last_from_id", "last_msg", "unread"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ack", nargs="+", default=None,
                    help="标记 uid 已处理（Agent 回复验证送达后调用），从 pending 移除")
    args = ap.parse_args()

    state = load_state()
    if args.ack:
        pending = state.get("pending", {})
        for uid in args.ack:
            pending.pop(uid, None)
        state["pending"] = pending
        save_state(state)
        print(f"acked: {', '.join(args.ack)}; pending left: {len(pending)}")
        return 0

    old = state.get("conversations", {})
    if not isinstance(old, dict):
        old = {}
    pending = state.get("pending", {})
    if not isinstance(pending, dict):
        pending = {}
    is_baseline = not bool(old)

    items = fetch_friends()
    if not items:
        # 拉取失败：静默退出，不伪造事件
        return 0

    now = time.time()
    new_events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    for item in items:
        if not item.get("uid"):
            continue
        key = stable_key(item)
        current[key] = {
            "fingerprint": fingerprint(item),
            "uid": item.get("uid"),
            "encrypt_uid": item.get("encrypt_uid"),
            "security_id": item.get("security_id"),
            "name": item.get("name", ""),
            "brand_name": item.get("brand_name", ""),
            "title": item.get("title", ""),
            "last_msg": item.get("last_msg", ""),
            "last_ts": item.get("last_ts"),
            "last_from_id": item.get("last_from_id"),
            "seen_at": now,
        }
        previous = old.get(key, {})
        changed = current[key]["fingerprint"] != previous.get("fingerprint")
        # 只报"最后一条是对方发的"变化（fromId != 我的 uid）
        is_from_them = (item.get("last_from_id") is not None
                        and my_uid() and int(item.get("last_from_id")) != my_uid())
        if changed and is_from_them and not is_baseline:
            new_events.append({
                "uid": item.get("uid"),
                "encrypt_uid": item.get("encrypt_uid"),
                "security_id": item.get("security_id"),
                "name": item.get("name", ""),
                "company": item.get("brand_name", ""),
                "title": item.get("title", ""),
                "last_msg": item.get("last_msg", ""),
                "last_ts": item.get("last_ts"),
                "last_msg_id": item.get("last_msg_id"),
                "first_scan": False,
            })

    # 新事件进 pending：未 ack 前不丢（Agent 因 429/超时挂掉时，下轮仍会重新输出）
    for ev in new_events:
        pending[str(ev["uid"])] = ev
    save_state({"updated_at": now, "conversations": current, "pending": pending})

    # 输出 = pending 里所有未 ack 事件（旧挂起 + 本轮新事件）
    out_events = list(pending.values())[:MAX_EVENTS]
    if not out_events:
        return 0

    payload = {
        "type": "boss_inbound_messages",
        "detected_at": now,
        "events": out_events,
        "count": len(out_events),
        "pending_uids": list(pending.keys()),
        "state_file": str(STATE_PATH),
        "ack_hint": "每条消息回复并验证送达后，运行 "
                    "`python3 ~/.hermes/scripts/boss-message-heartbeat.py --ack <uid>` 标记完成。"
                    "未 ack 的会在下轮重新出现。",
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # 任何本地瞬时故障都静默，不 wake Agent 不伪造事件
        raise SystemExit(0)
