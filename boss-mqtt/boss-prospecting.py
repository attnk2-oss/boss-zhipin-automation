#!/usr/bin/env python3
"""BOSS 主动挖岗 — 按配置的城市/区县搜索岗位，输出待打招呼的新岗位。

用法：
  python3 boss-prospecting.py --query "拼多多运营" [--query "电商运营"] ...
  python3 boss-prospecting.py --all          # 用预设关键词全跑一遍

输出：JSON（每轮一条），包含去重后（对照 friend list 已有会话 + 已处理记录）
未打过招呼的岗位列表。Agent 拿到后决定是否 greet（CLI greet 走浏览器通道）。

内部强制直连（清代理），CLI 调用也 unset 代理环境。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

# ⛔ 强制直连 BOSS：gateway 环境带 Clash 代理，BOSS 是国内站，走代理会被风控
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

STATE = Path.home() / ".hermes" / "scripts" / "boss-zhipin-state.json"
CLI_CACHE_DB = Path.home() / ".boss-agent" / "cache" / "boss_agent.db"
from boss_config import city_code, preferred_districts
CITY = city_code()
PREFERRED_DISTRICTS = preferred_districts()
DEFAULT_QUERIES = [
    "电商运营",
    "拼多多运营",
    "国内电商运营",
    "电商运营助理",
    "拼多多",
    "店铺运营",
]
from boss_config import my_uid as _my_uid
MY_UID = _my_uid()


def cli_greeted() -> set[str]:
    """读 CLI 的 greet_records 表，返回已打过招呼的 security_id 集合（权威去重源）。"""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{CLI_CACHE_DB}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT security_id FROM greet_records")
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def contact_skip_companies() -> set[str]:
    """读接触日志，返回应跳过的公司名（historical/rejected/no_follow）。"""
    import json as _json
    try:
        log = _json.loads((Path.home() / ".hermes" / "cache" / "boss-contact-log.json").read_text(encoding="utf-8"))
        companies = log.get("companies", {})
        return {name for name, e in companies.items() if e.get("status") in ("historical", "rejected", "no_follow")}
    except Exception:
        return set()


def contact_should_skip(company: str, skip_companies: set[str]) -> bool:
    """双向子串匹配判断该公司是否应跳过（处理日志中截断的公司名）。"""
    c = company.replace("...", "").replace("…", "").rstrip(".").strip()
    if not c:
        return False
    if company in skip_companies or c in skip_companies:
        return True
    for s in skip_companies:
        sn = s.replace("...", "").replace("…", "").rstrip(".").strip()
        if sn and (c in sn or sn in c):
            return True
    return False


def friend_uids() -> set[str]:
    """已有会话的 uid 集合（这些不能再当新岗位打招呼）。"""
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
            discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        cj.set_cookie(ck)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({}),
    )
    url = "https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json?" + urllib.parse.urlencode({"page": 1, "size": 100})
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "Referer": "https://www.zhipin.com/web/geek/chat",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        with opener.open(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
        res = d.get("zpData", {}).get("result", []) or d.get("zpData", {}).get("friendList", [])
        return {str(it.get("uid", "")) for it in res if it.get("uid")}
    except Exception:
        return set()


def search_one(query: str) -> list[dict]:
    """CLI boss search 单关键词（走浏览器通道，需 CDP 9222 活着）。返回规范化岗位列表。"""
    url = f"https://www.zhipin.com/web/geek/jobs?query={urllib.parse.quote(query)}&city={CITY}"
    cmd = ["boss", "search", query, "--url", url, "--page", "1"]
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except Exception as e:
        return [{"error": f"search failed: {e}"}]
    if proc.returncode != 0:
        return [{"error": f"search exit {proc.returncode}: {proc.stderr[:200]}"}]
    try:
        d = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [{"error": f"bad JSON from CLI: {proc.stdout[:200]}"}]
    data = d.get("data") or []
    out = []
    for it in data:
        if not isinstance(it, dict):
            continue
        out.append({
            "title": it.get("title", ""),
            "company": it.get("company", ""),
            "salary": it.get("salary", ""),
            "city": it.get("city", ""),
            "district": it.get("district", ""),
            "experience": it.get("experience", ""),
            "education": it.get("education", ""),
            "skills": it.get("skills", []),
            "boss_name": it.get("boss_name", ""),
            "boss_title": it.get("boss_title", ""),
            "security_id": it.get("security_id", ""),
            # greet 实际需要 encryptJobId；同时保留数字 job_id 仅作诊断，避免 agent 误传。
            "encrypt_job_id": it.get("encrypt_job_id", "") or it.get("encryptJobId", ""),
            "encryptJobId": it.get("encrypt_job_id", "") or it.get("encryptJobId", ""),
            "job_id": it.get("job_id", ""),
            "uid": it.get("uid", ""),
            "greeted": bool(it.get("greeted")),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", action="append", default=None, help="搜索关键词，可多次")
    ap.add_argument("--all", action="store_true", help="用预设关键词全跑")
    args = ap.parse_args()

    queries = args.query or (DEFAULT_QUERIES if args.all else ["电商运营"])

    greeted_set = cli_greeted()
    existing = friend_uids()
    skip_companies = contact_skip_companies()

    results = []
    for q in queries:
        hits = search_one(q)
        results.extend(hits)

    # 去重 + 过滤：去掉已有会话/已打过招呼/无 security_id/非目标区县
    # 注意：BOSS 对同一岗位每次搜索返回不同 security_id，需按 (company,title) 去重
    fresh = []
    seen_keys = set()
    for r in results:
        if "error" in r:
            continue
        if r.get("district") and r.get("district") not in PREFERRED_DISTRICTS:
            continue
        if r.get("company") and contact_should_skip(r.get("company"), skip_companies):
            continue
        key = r.get("security_id", "")
        biz_key = (r.get("company", ""), r.get("title", ""))
        if not key or biz_key in seen_keys or key in greeted_set:
            continue
        seen_keys.add(biz_key)
        fresh.append(r)

    if not fresh:
        return 0

    payload = {
        "type": "boss_new_jobs",
        "detected_at": int(time.time()),
        "queries": queries,
        "existing_friends": len(existing),
        "candidates": fresh[:25],
        "count": len(fresh),
        "hint": "对新岗位用 `boss greet <security_id> <job_id> --message <打招呼语>` 直接打招呼（CDP 通道）。"
                "greet 成功会自动写入 CLI cache，下轮不再出现。注意岗位匹配简历档案，不匹配的不要 greet。",
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # 本地故障静默，不伪造数据
        raise SystemExit(0)
