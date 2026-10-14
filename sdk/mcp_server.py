"""The SDK as an MCP server: every `bl` command is a tool.

    python sdk/mcp_server.py                 speaks MCP over stdin and stdout (JSON-RPC 2.0, one message per line)

Register it with an agent, for example in a project's .mcp.json:
    {"mcpServers": {"bannerlord": {"command": "python", "args": ["<this repository>/sdk/mcp_server.py"],
                                   "env": {"BANNERLORD_DIR": "<game folder>", "BL_PROJECT": "<project folder>"}}}}

The tools are the commands of sdk/bl.py with the dot written as an underscore (scene.info becomes scene_info); their
argument schemas come from the same registry. Every call runs `bl <command>` in a process of its own, so a tool that
fails or exits cannot take the server down and every call sees the files as they are now. The answer is the JSON
object bl prints. A tool that would start the game (run) does so only with launch=true, as on the command line.
No dependencies: the protocol is small enough to speak directly.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bl

PROTOCOL = "2024-11-05"
SERVER = {"name": "bannerlord-editor-free-sdk", "version": "0.2.0"}
INSTRUCTIONS = ("Tools for building Mount & Blade II: Bannerlord mods without the official editor. Start with `doctor`. Every tool "
                "answers with one JSON object: ok, result, log, and on failure error and hint. Tools that change files take dry=true "
                "to say what they would do. Nothing touches the game folder while the game runs.")


def tool_name(cmd): return cmd.replace(".", "_")


def tools():
    out = []
    for c in bl.COMMANDS.values():
        note = (" Changes files; dry=true only reports." if c["writes"] else "") + (" Starts the game when launch=true." if c["launches"] else "")
        out.append({"name": tool_name(c["name"]), "description": c["summary"] + note, "inputSchema": bl.schema(c),
                    "annotations": {"readOnlyHint": not c["writes"]}})
    return out


def call(name, arguments):
    """Runs one command in its own process. Returns (text, is_error)."""
    by = {tool_name(c): c for c in bl.COMMANDS}
    if name not in by: return json.dumps({"ok": False, "error": "no tool %s" % name}), True
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "bl.py"), by[name], "--args", json.dumps(arguments or {})],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    except OSError as e:
        return json.dumps({"ok": False, "error": "could not start bl: %s" % e}), True
    text = r.stdout.strip() or json.dumps({"ok": False, "error": "bl printed nothing", "stderr": r.stderr[-2000:]})
    return text, r.returncode != 0


def handle(msg):
    """The answer to one JSON-RPC message, or None for a notification."""
    mid = msg.get("id"); method = msg.get("method"); params = msg.get("params") or {}
    def ok(result): return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER, "instructions": INSTRUCTIONS})
    if method == "ping": return ok({})
    if method == "tools/list": return ok({"tools": tools()})
    if method == "tools/call":
        text, bad = call(params.get("name"), params.get("arguments"))
        return ok({"content": [{"type": "text", "text": text}], "isError": bad})
    if mid is None: return None                                # notifications (initialized, cancelled ...) need no answer
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}


def main():
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line: continue
        try: msg = json.loads(line.decode("utf-8"))
        except ValueError:
            ans = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            ans = handle(msg)
        if ans is not None:
            out.write(json.dumps(ans, ensure_ascii=False).encode("utf-8") + b"\n"); out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
