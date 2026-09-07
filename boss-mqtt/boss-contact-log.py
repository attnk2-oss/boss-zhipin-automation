#!/usr/bin/env python3
"""BOSS 接触日志 — 公司粒度记录所有打招呼/接触状态，供 prospecting 过滤与 heartbeat 判断。

数据文件：~/.hermes/cache/boss-contact-log.json
状态机：greeted(打过招呼) / historical(历史接触，主人面试过/接触过) / rejected(已拒绝)
        / no_follow(主人明确没希望，不跟进) / interviewing(面试推进中)

用法：
  add    --company 公司名 [--name 联系人] [--title 岗位] [--uid uid] [--job-id jobid] [--msg 打招呼语]
  mark   --company 公司名 --status greeted|historical|rejected|no_follow|interviewing [--reason 原因]
  list   [--status greeted] [--json]
  check  --company 公司名           # 输出状态（heartbeat 处理前判断用）
  skip-companies                    # 输出应跳过的公司名（historical/rejected/no_follow），一行一个

过滤规则（被 prospecting.py 和 heartbeat prompt 共用）：
  historical / rejected / no_follow = 禁止再 greet、禁止再回复新消息（静默 ack）
  greeted = 同一公司其他岗位不再 greet（避免重复骚扰），但新消息正常处理
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

LOG_PATH = Path.home() / ".hermes" / "cache" / "boss-contact-log.json"
SKIP_STATUSES = ("historical", "rejected", "no_follow")


def load() -> dict:
    try:
        d = json.loads(LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("companies"), dict):
            d.setdefault("version", 1)
            return d
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "companies": {}}


def save(data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="boss-contact-log-", dir=LOG_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            json.dump(data, h, ensure_ascii=False, separators=(",", ":"))
            h.write("\n")
        os.replace(tmp_name, LOG_PATH)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def cmd_add(args) -> int:
    data = load()
    companies = data["companies"]
    company = args.company.strip()
    if not company:
        print("company required", file=sys.stderr)
        return 1
    entry = companies.setdefault(company, {
        "status": "greeted",
        "reason": "",
        "contacts": [],
        "first_seen_at": time.time(),
        "last_contact_at": time.time(),
        "notes": "",
    })
    if entry.get("status") == "greeted" or not entry.get("status"):
        entry["status"] = "greeted"
    contact = {
        "name": args.name or "",
        "title": args.title or "",
        "uid": args.uid or "",
        "job_id": args.job_id or "",
        "greet_msg": args.msg or "",
        "at": time.time(),
    }
    # 同 uid 不重复追加
    if not any(c.get("uid") == contact["uid"] and c.get("uid") for c in entry["contacts"]):
        entry["contacts"].append(contact)
    entry["last_contact_at"] = time.time()
    save(data)
    print(f"logged: {company} [{entry['status']}] contacts={len(entry['contacts'])}")
    return 0


def cmd_mark(args) -> int:
    data = load()
    companies = data["companies"]
    company = args.company.strip()
    entry = companies.setdefault(company, {
        "status": "", "reason": "", "contacts": [], "first_seen_at": time.time(),
        "last_contact_at": time.time(), "notes": "",
    })
    entry["status"] = args.status
    if args.reason:
        entry["reason"] = args.reason
    entry["last_contact_at"] = time.time()
    save(data)
    print(f"marked: {company} -> {args.status}" + (f" ({args.reason})" if args.reason else ""))
    return 0


def cmd_list(args) -> int:
    data = load()
    companies = data["companies"]
    items = []
    for name, e in companies.items():
        if args.status and e.get("status") != args.status:
            continue
        items.append({
            "company": name,
            "status": e.get("status", ""),
            "reason": e.get("reason", ""),
            "contacts": len(e.get("contacts", [])),
            "last_contact_at": e.get("last_contact_at", 0),
        })
    items.sort(key=lambda x: x["last_contact_at"], reverse=True)
    if args.json:
        print(json.dumps(items, ensure_ascii=False))
        return 0
    if not items:
        print("(empty)")
        return 0
    for it in items:
        ts = time.strftime("%m-%d %H:%M", time.localtime(it["last_contact_at"])) if it["last_contact_at"] else "-"
        print(f"{it['status']:12s} {it['company']:24s} contacts={it['contacts']:2d} last={ts} {it['reason']}")
    return 0


def _norm(name: str) -> str:
    """去掉省略号与尾部点，便于截断名与完整名互配。"""
    return name.replace("...", "").replace("…", "").rstrip(".").strip()


def _match_company(companies: dict, company: str) -> tuple[str, dict] | None:
    """精确匹配优先，否则做双向子串匹配（处理日志中截断的公司名）。"""
    if company in companies:
        return company, companies[company]
    c = _norm(company)
    if not c:
        return None
    for name, e in companies.items():
        n = _norm(name)
        if n and (c in n or n in c):
            return name, e
    return None


def cmd_check(args) -> int:
    data = load()
    companies = data["companies"]
    company = args.company.strip()
    hit = _match_company(companies, company)
    if not hit:
        print("unknown")
        return 0
    name, e = hit
    print(json.dumps({"company": name, "status": e.get("status", ""), "reason": e.get("reason", "")},
                     ensure_ascii=False))
    return 0


def cmd_skip(args) -> int:
    data = load()
    companies = data["companies"]
    for name, e in companies.items():
        if e.get("status") in SKIP_STATUSES:
            print(name)
    return 0


def status_of(company: str) -> str:
    """供其他脚本 import：返回公司状态，unknown 表示未记录。"""
    data = load()
    hit = _match_company(data["companies"], company)
    if not hit:
        return "unknown"
    return hit[1].get("status", "") or "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="记录一次打招呼")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--name", default="")
    p_add.add_argument("--title", default="")
    p_add.add_argument("--uid", default="")
    p_add.add_argument("--job-id", default="")
    p_add.add_argument("--msg", default="")
    p_add.set_defaults(fn=cmd_add)

    p_mark = sub.add_parser("mark", help="标记公司状态")
    p_mark.add_argument("--company", required=True)
    p_mark.add_argument("--status", required=True,
                        choices=["greeted", "historical", "rejected", "no_follow", "interviewing"])
    p_mark.add_argument("--reason", default="")
    p_mark.set_defaults(fn=cmd_mark)

    p_list = sub.add_parser("list", help="列出接触日志")
    p_list.add_argument("--status", default="")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(fn=cmd_list)

    p_check = sub.add_parser("check", help="查询公司状态")
    p_check.add_argument("--company", required=True)
    p_check.set_defaults(fn=cmd_check)

    p_skip = sub.add_parser("skip-companies", help="输出应跳过的公司名")
    p_skip.set_defaults(fn=cmd_skip)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
