"""Drive the clud-desktop MCP server over stdio, the way Claude Code does, and save what
it returns. Used by tests/pi-vm.sh; runnable by hand too:

    python3 mcp_probe.py OUTDIR PYTHON SERVER.py [TOOL JSON-ARGS]...
    python3 mcp_probe.py OUTDIR --cmd "COMMAND LINE" [TOOL JSON-ARGS]...   (any stdio server)
With no tool calls it runs a short smoke test: Status, Screenshot, Move, Shortcut, Type.
"""
import base64
import json
import os
import shlex
import subprocess
import sys

sys.stdout.reconfigure(errors="replace")  # Windows consoles can't print every character
out_dir, python, server, *calls = sys.argv[1:]
os.makedirs(out_dir, exist_ok=True)
if python == "--cmd":
    argv = server if os.name == "nt" else shlex.split(server)
else:
    argv = [python, "-I", "-B", server]
p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
n = 0


def rpc(method, params=None, notify=False):
    global n
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        n += 1
        msg["id"] = n
    p.stdin.write(json.dumps(msg) + "\n")
    p.stdin.flush()
    if notify:
        return None
    while True:  # skip the server's own notifications and log messages
        line = p.stdout.readline()
        if not line:
            raise SystemExit(f"{method}: the server exited")
        reply = json.loads(line)
        if reply.get("id") == n:
            break
    if "error" in reply:
        raise SystemExit(f"{method}: {reply['error']}")
    return reply["result"]


init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "probe", "version": "0"}})
rpc("notifications/initialized", notify=True)
print("server:", init["serverInfo"])
print("tools:", [t["name"] for t in rpc("tools/list")["tools"]])
if not calls:
    calls = ["Status", "{}", "Screenshot", "{}", "Move", '{"x": 100, "y": 100}',
             "Shortcut", '{"keys": "esc"}']
failed = False
for i in range(0, len(calls), 2):
    name, args = calls[i], json.loads(calls[i + 1])
    res = rpc("tools/call", {"name": name, "arguments": args})
    for j, c in enumerate(res["content"]):
        if c["type"] == "image":
            path = os.path.join(out_dir, f"{i // 2:02d}-{name}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(c["data"]))
            print(f"{name}: image -> {path}")
        else:
            print(f"{name}: {c['text']}")
    if res.get("isError"):
        failed = True
p.stdin.close()
p.wait()
sys.exit(1 if failed else 0)
