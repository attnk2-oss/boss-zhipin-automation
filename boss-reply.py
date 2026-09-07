#!/usr/bin/env python3
"""Reply to an existing BOSS conversation (candidate side).

Strategy (v2, CDP-free targeting):
  1. CLI friend_list -> fullList with fresh securityId + friendId
  2. CDP page: call Vue handleClickItem(item, idx) to switch conversation
  3. Type into #chat-input via Input.insertText (real keyboard events)
  4. Default dry-run; --send required to actually click 发送

This replaces the old DOM-scroll matching (unstable with virtual lists).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

CDP_URL = "http://127.0.0.1:9222"
BOSS_CHAT_URL = "https://www.zhipin.com/web/geek/chat"
CLI_PY = os.environ.get("BOSS_CLI_PYTHON", str(Path.home() / ".local/share/uv/tools/boss-agent-cli/bin/python3"))
CLI_DIR = os.environ.get("BOSS_CLI_SITE_PACKAGES", str(Path.home() / ".local/share/uv/tools/boss-agent-cli/lib/python3.11/site-packages"))


def cdp(ws_url: str, method: str, params: dict | None = None, msg_id: int = 1) -> dict:
    with connect(ws_url, max_size=8 * 1024 * 1024, open_timeout=15, close_timeout=5) as ws:
        ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == msg_id:
                return msg


def evaluate(ws_url: str, expression: str) -> object:
    result = cdp(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    })
    if "exceptionDetails" in result.get("result", {}):
        raise RuntimeError(str(result["result"]["exceptionDetails"])[:400])
    return result.get("result", {}).get("result", {}).get("value")


def boss_tab() -> dict:
    tabs = json.loads(urllib.request.urlopen(CDP_URL + "/json", timeout=5).read())
    for tab in tabs:
        if tab.get("type") == "page" and "zhipin.com" in tab.get("url", ""):
            return tab
    raise RuntimeError("没有找到 BOSS Chrome 标签页")


def ensure_chat_tab() -> dict:
    try:
        tab = boss_tab()
    except RuntimeError:
        browser = json.loads(urllib.request.urlopen(CDP_URL + "/json/version", timeout=5).read())
        ws = browser["webSocketDebuggerUrl"]
        cdp(ws, "Target.createTarget", {"url": BOSS_CHAT_URL})
        time.sleep(4)
        tab = boss_tab()
    if "/web/geek/chat" not in tab.get("url", ""):
        cdp(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": BOSS_CHAT_URL})
        time.sleep(4)
        tab = boss_tab()
    return tab


def friend_list() -> list[dict]:
    """Fetch conversations via `boss chat --export json` (uses CLI auth + dedup)."""
    import subprocess
    proc = subprocess.run(
        ["boss", "chat", "--page", "1", "--export", "json"],
        capture_output=True, text=True, timeout=120,
        env={"PATH": CLI_DIR.rsplit("/site-packages", 1)[0] + "/bin:" + __import__("os").environ.get("PATH", "")},
    )
    if proc.returncode != 0:
        raise RuntimeError("boss chat failed: " + proc.stderr[-400:])
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    items = data if isinstance(data, list) else (data.get("data") or {}).get("items") or []
    if not items and isinstance(data, dict):
        path = (data.get("data") or {}).get("path")
        if path and Path(path).exists():
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("items") or raw.get("data") or []
    return items


def switch_conversation(tab: dict, name: str, brand: str = "") -> dict:
    """Call Vue handleClickItem to open the conversation; returns input state."""
    ws = tab["webSocketDebuggerUrl"]
    clicked = evaluate(ws, f"""(() => {{
      const vm = document.querySelector('.chat-user.v2')?.__vue__;
      if (!vm) return {{ok:false, reason:'no list vm'}};
      const found = vm.fullList.find(x => (x.name === {json.dumps(name, ensure_ascii=False)}) && (!{json.dumps(brand, ensure_ascii=False)} || (x.brandName === {json.dumps(brand, ensure_ascii=False)})));
      if (!found) return {{ok:false, reason:'friend not in fullList', names: (vm.fullList||[]).slice(0,10).map(x=>x.name)}};
      vm.handleClickItem(found, vm.fullList.indexOf(found));
      return {{ok:true, friendId: found.friendId}};
    }})()""")
    time.sleep(1.8)
    state = evaluate(ws, """(() => {
      const el = document.querySelector('#chat-input');
      const btns = [...document.querySelectorAll('button, .btn-sure-v2')].filter(b => b.offsetWidth && (b.innerText||'').trim() === '发送');
      return {
        input: !!el,
        inputText: el ? el.innerText : '',
        sendButtons: btns.length,
        header: document.querySelector('.chat-header, [class*=chat-header]')?.innerText?.trim()?.slice(0,120) || ''
      };
    })()""")
    return {"clicked": clicked, "state": state}


def send_message(tab: dict, message: str) -> dict:
    ws = tab["webSocketDebuggerUrl"]
    evaluate(ws, """(() => {
      const el = document.querySelector('#chat-input');
      if (!el) return false;
      el.focus();
      el.innerText = '';
      el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
      return true;
    })()""")
    time.sleep(0.3)
    cdp(ws, "Input.insertText", {"text": message})
    time.sleep(0.8)
    state = evaluate(ws, """(() => {
      const el = document.querySelector('#chat-input');
      const btn = [...document.querySelectorAll('button, .btn-sure-v2')].find(b => b.offsetWidth && (b.innerText||'').trim() === '发送');
      return {inputText: el ? el.innerText : '', btnClass: btn ? btn.className : '', hasBtn: !!btn};
    })()""")
    if not state.get("inputText"):
        raise RuntimeError("消息没有进入 BOSS 输入框")
    clicked = evaluate(ws, """(() => {
      const b = [...document.querySelectorAll('button, .btn-sure-v2')].find(x => x.offsetWidth && (x.innerText||'').trim() === '发送');
      if (!b) return false;
      b.click();
      return true;
    })()""")
    time.sleep(2.5)
    after = evaluate(ws, """(() => {
      const el = document.querySelector('#chat-input');
      return {inputText: el ? el.innerText : ''};
    })()""")
    return {"typed": state.get("inputText"), "btnClass": state.get("btnClass"),
            "clicked": bool(clicked), "after": after}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("name", help="联系人姓名，如 张女士")
    p.add_argument("message")
    p.add_argument("--brand", default="", help="公司名，辅助定位（可选）")
    p.add_argument("--send", action="store_true")
    args = p.parse_args()

    friends = friend_list()
    item = next((x for x in friends if x.get("name") == args.name
                 and (not args.brand or x.get("brand_name") == args.brand)), None)
    if item is None:
        print(json.dumps({"ok": False, "stage": "friend", "name": args.name,
                          "known": sorted({x.get("name") for x in friends})[:30]}, ensure_ascii=False))
        return 2

    tab = ensure_chat_tab()
    sw = switch_conversation(tab, args.name, args.brand)
    if not sw.get("clicked", {}).get("ok"):
        print(json.dumps({"ok": False, "stage": "switch", "detail": sw}, ensure_ascii=False))
        return 3
    if not sw.get("state", {}).get("input"):
        print(json.dumps({"ok": False, "stage": "input", "detail": sw}, ensure_ascii=False))
        return 4

    if not args.send:
        print(json.dumps({
            "ok": True, "dry_run": True,
            "target": {"name": item.get("name"), "brand": item.get("brand_name"),
                       "initiated_by": item.get("initiated_by"), "last_msg": item.get("last_msg")},
            "switch": sw,
        }, ensure_ascii=False))
        return 0

    result = send_message(tab, args.message)
    ok = bool(result.get("clicked")) and not result.get("after", {}).get("inputText")
    print(json.dumps({"ok": ok, "dry_run": False, "target": args.name, "result": result},
                     ensure_ascii=False))
    return 0 if ok else 5


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
