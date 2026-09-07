#!/usr/bin/env python3
"""CDP helper for BOSS zhipin automation via websocket-client."""
import json
import sys
import time
import websocket

WS_URL = "ws://127.0.0.1:9222/devtools/page/654F4B169127B6B6EF275E50D320FF08"

class CDP:
    def __init__(self, url=WS_URL, timeout=30):
        self.ws = websocket.create_connection(url, timeout=timeout)
        self.msg_id = 0

    def send(self, method, params=None):
        self.msg_id += 1
        mid = self.msg_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error {method}: {msg['error']}")
                return msg.get("result", {})
            # else: event, ignore

    def eval(self, expr):
        res = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in res:
            return {"error": res["exceptionDetails"].get("text"), "result": res.get("result", {}).get("value")}
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ping"
    cdp = CDP()
    if cmd == "ping":
        print(json.dumps(cdp.eval("document.title"), ensure_ascii=False))
    elif cmd == "eval":
        expr = sys.argv[2]
        print(json.dumps(cdp.eval(expr), ensure_ascii=False))
    elif cmd == "text":
        print(json.dumps(cdp.eval("document.body.innerText"), ensure_ascii=False))
    cdp.close()
