"""The outside half of the bridge into the running game (`bl live`). The inside half is src/Bridge.cs of a project
made with `bl new`.

A request is a small text file the module picks up twice a second on the game's main thread; the answer comes back as a
JSON file. Files instead of a socket: nothing to open, nothing to block, and a request can be written by hand.
    <module>/bridge/in/<id>.txt     the command on the first line, then one key=value per line
    <module>/bridge/out/<id>.json   {"id": ..., "ok": true, "result": {...}}  or  {"ok": false, "error": ...}
    <module>/bridge/on              makes the module listen (or the environment variable BANNERLORD_BRIDGE=1)

    send(module, "state")                                   -> {"screen": ..., "scene": ..., "player": [x, y, z], ...}
    send(module, "spawn", {"prefab": "rock_cliff_b", "x": 600, "y": 640})
The commands are listed at the top of Bridge.cs. The C# half compiles; it has not been run in the game yet.
"""
import json, os, time, uuid

COMMANDS = ("ping", "state", "ground", "raycast", "teleport", "look", "screenshot", "spawn", "remove", "say", "quit")


def folder(module): return os.path.join(module, "bridge")


def enable(module, on=True):
    """Creates or removes the file that makes the module listen. It is read once, when the game starts."""
    d = folder(module); flag = os.path.join(d, "on")
    if on:
        os.makedirs(os.path.join(d, "in"), exist_ok=True); os.makedirs(os.path.join(d, "out"), exist_ok=True)
        open(flag, "w").close()
    elif os.path.isfile(flag): os.remove(flag)
    return os.path.isfile(flag)


def send(module, cmd, args=None, timeout=10.0, poll=0.1):
    """Sends one command and waits for the answer. Returns the module's result; raises RuntimeError when the module says
    no and TimeoutError when nothing answers (the game is not running, the bridge is off, or it is in a loading screen)."""
    d = folder(module)
    if not os.path.isdir(os.path.join(d, "in")): raise RuntimeError("no bridge folder in %s: enable the bridge and start the game" % module)
    rid = "%d_%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6])
    lines = [cmd] + ["%s=%s" % (k, v) for k, v in (args or {}).items() if v is not None]
    for line in lines[1:]:
        if "\n" in line or "\r" in line: raise ValueError("a value holds a line break: %r" % line)
    tmp = os.path.join(d, "in", rid + ".tmp"); req = os.path.join(d, "in", rid + ".txt"); ans = os.path.join(d, "out", rid + ".json")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f: f.write("\n".join(lines) + "\n")
    os.replace(tmp, req)                                          # the module never sees half a request
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isfile(ans):
            with open(ans, encoding="utf-8") as f: a = json.load(f)
            os.remove(ans)
            if not a.get("ok"): raise RuntimeError(a.get("error") or "the module refused")
            return a.get("result") or {}
        time.sleep(poll)
    if os.path.isfile(req): os.remove(req)                        # nobody took it: do not leave it for a later session
    raise TimeoutError("no answer to %s within %.0f s" % (cmd, timeout))
