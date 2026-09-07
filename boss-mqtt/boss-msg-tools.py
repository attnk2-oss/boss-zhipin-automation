#!/usr/bin/env python3
"""BOSS 聊天辅助工具 — 纯 Python HTTP（无 CDP/浏览器）。

子命令：
  history --uid <uid> [--security-id <sid>] [--limit N]
      读取某会话完整聊天历史（内部先用 friend list 拿最新 securityId）
  friends [--name 关键词]
      列出会话列表（uid/encryptUid/securityId/lastMsg）
  job --uid <uid> [--security-id <sid>]
      读取该会话对应岗位的完整 JD（职位职责/薪资/标签/地址）。
      纯 urllib + state 里的 cookies/stoken 调 /wapi/zpgeek/job/card.json，
      不需要浏览器、不需要 CLI。返回 JSON。
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

try:
    from boss_config import my_uid
except ImportError:
    import sys as _sys
    _sys.path.insert(0, __import__('pathlib').Path(__file__).resolve().parent.parent)
    from boss_config import my_uid
from pathlib import Path

# ⛔ 强制直连 BOSS：gateway 环境带 Clash 代理(7897)，BOSS 是国内站，走代理会被
# 风控(code 7/37)且 cookie/IP 不匹配。必须在此清掉所有代理环境变量 + urllib 显式 NoProxy。
import os as _os
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    _os.environ.pop(_k, None)

STATE = Path.home() / ".hermes" / "scripts" / "boss-zhipin-state.json"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def build_opener():
    state = json.loads(STATE.read_text(encoding="utf-8"))
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


def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
    })
    with build_opener().open(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def friend_list():
    url = ("https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?"
           + urllib.parse.urlencode({"page": 1, "size": 100}))
    d = http_get(url)
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
            "last_msg": it.get("lastMsg", ""),
            "last_ts": it.get("lastTS"),
            "last_msg_id": lmi.get("msgId"),
            "last_from_id": lmi.get("fromId"),
        })
    return out


def history(uid, security_id=None, limit=30):
    """读取聊天历史。gid=对方uid + securityId（必须最新，每次请求会变）。"""
    if not security_id:
        for f in friend_list():
            if f["uid"] == str(uid):
                security_id = f["security_id"]
                break
    if not security_id:
        return {"ok": False, "error": f"uid {uid} not in friend list"}
    params = urllib.parse.urlencode({
        "gid": uid, "securityId": security_id, "page": 1, "c": limit, "src": 0,
    })
    d = http_get("https://www.zhipin.com/wapi/zpchat/geek/historyMsg?" + params)
    msgs = (d.get("zpData") or {}).get("messages") or []
    out = []
    for m in msgs:
        frm = (m.get("from") or {})
        body = (m.get("body") or {})
        out.append({
            "from_uid": frm.get("uid"),
            "from_name": frm.get("name", ""),
            "type": m.get("type"),
            "time": m.get("time"),
            "text": body.get("text", ""),
        })
    return {"ok": d.get("code") == 0, "count": len(out), "messages": out}


def job_jd(uid, security_id=None):
    """读取会话对应岗位的完整 JD（card.json 纯 httpx 通道，0.3s）。"""
    if not security_id:
        for f in friend_list():
            if f["uid"] == str(uid):
                security_id = f["security_id"]
                break
    if not security_id:
        return {"ok": False, "error": f"uid {uid} not in friend list"}
    state = json.loads(STATE.read_text(encoding="utf-8"))
    stoken = state.get("stoken", "")
    params = urllib.parse.urlencode({"securityId": security_id, "__zp_stoken__": stoken})
    d = http_get("https://www.zhipin.com/wapi/zpgeek/job/card.json?" + params)
    zp = d.get("zpData") or {}
    card = zp.get("jobCard") or {}
    if not card:
        return {"ok": False, "code": d.get("code"), "error": "no jobCard in response"}
    return {
        "ok": d.get("code") == 0,
        "job": {
            "title": card.get("jobName", ""),
            "salary": card.get("salaryDesc", ""),
            "brand": card.get("brandName", ""),
            "city": card.get("cityName", ""),
            "experience": card.get("experienceName", ""),
            "education": card.get("degreeName", ""),
            "labels": card.get("jobLabels", []),
            "description": card.get("postDescription", ""),
            "address": card.get("address", ""),
            "boss_name": card.get("bossName", ""),
            "boss_title": card.get("bossTitle", ""),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("history")
    p1.add_argument("--uid", required=True)
    p1.add_argument("--security-id", default=None)
    p1.add_argument("--limit", type=int, default=30)

    p2 = sub.add_parser("friends")
    p2.add_argument("--name", default=None, help="按姓名/公司关键词过滤")

    p3 = sub.add_parser("job")
    p3.add_argument("--uid", required=True)
    p3.add_argument("--security-id", default=None)

    args = ap.parse_args()

    if args.cmd == "friends":
        friends = friend_list()
        if args.name:
            friends = [f for f in friends if args.name in (f["name"] + f["brand_name"])]
        for f in friends:
            print(f"{f['name']} | {f['brand_name']} | uid={f['uid']} | "
                  f"from={'我' if f['last_from_id'] == my_uid() else '对方'} | "
                  f"{str(f['last_msg'])[:40]}")
        print(f"total: {len(friends)}")
    elif args.cmd == "history":
        r = history(args.uid, args.security_id, args.limit)
        if not r.get("ok"):
            print(json.dumps(r, ensure_ascii=False))
            return 1
        for m in r["messages"]:
            who = "我" if m["from_uid"] == my_uid() else "对方"
            ts = time.strftime("%m-%d %H:%M", time.localtime(m["time"] / 1000)) if m["time"] else "?"
            print(f"[{ts}] {who}: {m['text']}")
        print(f"(total {r['count']})")
    elif args.cmd == "job":
        r = job_jd(args.uid, args.security_id)
        if not r.get("ok"):
            print(json.dumps(r, ensure_ascii=False))
            return 1
        j = r["job"]
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
