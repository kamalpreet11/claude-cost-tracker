#!/usr/bin/env python3
"""Claude Project Tracker - aggregator + local dashboard server.

Usage:
    python3 tracker.py

Steps: preflight (verify statusline capture is wired) -> build data.js from all
opted-in projects -> serve the dashboard on localhost -> open the browser.
The page's Refresh button hits /refresh, which rebuilds data.js and reloads.
"""
import sys
import os
import json
import glob
import http.server
import socketserver
import webbrowser
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE = os.path.join(HOME, ".claude")
PROJECTS = os.path.join(CLAUDE, "projects")
SETTINGS = os.path.join(CLAUDE, "settings.json")
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
INSTALL = os.path.join(HERE, "install.sh")
CAPTURE = os.path.join(HERE, "capture.py")
MARKER = "claude-project-tracker"
PORT_RANGE = range(8787, 8798)

# Per-1M-token rates (USD). Per-model COST is an estimate from tokens; the headline
# totals use the exact figures Claude Code records. Matched by substring of model id.
PRICING = {
    "opus":   {"input": 15.0, "output": 75.0, "cache_read": 1.5,  "cache_write": 18.75},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_read": 0.3,  "cache_write": 3.75},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25},
}


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def _statusline_command():
    try:
        with open(SETTINGS) as f:
            cfg = json.load(f)
    except Exception:
        return None
    sl = cfg.get("statusLine")
    return sl.get("command") if isinstance(sl, dict) else None


def preflight():
    """Return None if capture is wired up, else a help string to print."""
    cmd = _statusline_command()
    if not cmd:
        return _help_no_statusline()
    path = os.path.expanduser(os.path.expandvars(cmd.split()[0]))
    try:
        with open(path) as f:
            content = f.read()
    except Exception:
        return _help_unreadable(path)
    if MARKER not in content:
        return _help_no_capture(path)
    return None


def _sample_script():
    return (
        "#!/bin/bash\n"
        "input=$(cat)\n"
        "# {m} capture  (do not remove this marker line)\n"
        "printf '%s' \"$input\" | python3 \"{c}\" 2>/dev/null\n"
        "echo \"$input\" | python3 -c 'import sys,json; d=json.load(sys.stdin); "
        "print(d.get(\"model\",{{}}).get(\"display_name\",\"\"),\"|\","
        "d.get(\"workspace\",{{}}).get(\"current_dir\",\"\"))'\n"
    ).format(m=MARKER, c=CAPTURE)


def _help_no_statusline():
    return (
        "\nClaude Project Tracker - setup needed\n"
        "-------------------------------------\n"
        "No statusline is configured in {s}, so Claude Code never emits the cost /\n"
        "API-time data this tool records. Nothing can be captured until one exists.\n\n"
        "Fix it automatically (recommended):\n"
        "    {i} --setup-statusline\n\n"
        "Or by hand - save this as ~/.claude/statusline-command.sh (chmod +x):\n\n"
        "{sample}\n"
        "and add to ~/.claude/settings.json:\n"
        '    "statusLine": {{ "type": "command", "command": "~/.claude/statusline-command.sh" }}\n\n'
        "Then run  {i}  inside a project and re-run this tool.\n"
    ).format(s=SETTINGS, i=INSTALL, sample=_sample_script())


def _help_no_capture(path):
    snippet = (
        "# {m} capture  (do not remove this marker line)\n"
        "printf '%s' \"$input\" | python3 \"{c}\" 2>/dev/null\n"
    ).format(m=MARKER, c=CAPTURE)
    return (
        "\nClaude Project Tracker - setup needed\n"
        "-------------------------------------\n"
        "Your statusline ({p}) does not include the capture step, so no usage is\n"
        "being recorded.\n\n"
        "Fix it automatically (recommended):\n"
        "    {i} --setup-statusline\n\n"
        "Or by hand - add this line to that script (it relies on $input holding the\n"
        "stdin blob):\n\n"
        "{snippet}\n"
        "Then re-run this tool.\n"
    ).format(p=path, i=INSTALL, snippet=snippet)


def _help_unreadable(path):
    return (
        "\nClaude Project Tracker - setup needed\n"
        "-------------------------------------\n"
        "settings.json points statusLine at {p}, but that file can't be read.\n"
        "Run  {i} --setup-statusline  to (re)create a working statusline.\n"
    ).format(p=path, i=INSTALL)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def read_jsonl(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return out


def segment_sessions(samples):
    """Split a project's cost samples into sessions.

    `cost_usd` is the cumulative total for ONE `claude` process and climbs
    monotonically while that process runs; each process is identified by its
    `session_id` (resuming a session keeps the id and keeps cost cumulative).

    We group by `session_id` first. Sorting every sample together by `ts` and
    splitting on cost drops (the old approach) is unsafe: a stray cost-$0 sample
    from another session briefly opened in the same project interleaves into the
    real session's climbing series, forges a false boundary, and — because a
    session's cost is its high-water mark — makes the cumulative climb get
    recounted in every fragment (e.g. $32 reported as $89).

    Within a single session we still split on any cost DROP as defense-in-depth:
    if a resumed id ever reset its cost to 0, that genuinely is a second run."""
    by_sid = {}
    for s in samples:
        by_sid.setdefault(s.get("session_id"), []).append(s)
    sessions = []
    for group in by_sid.values():
        group = sorted(group, key=lambda s: s.get("ts") or "")
        cur, prev = None, None
        for s in group:
            c = s.get("cost_usd")
            if c is None:
                continue
            if cur is None or (prev is not None and c < prev - 1e-9):
                cur = []
                sessions.append(cur)
            cur.append(s)
            prev = c
    sessions.sort(key=lambda seg: seg[0].get("ts") or "")
    return sessions


def _maxval(samples, key):
    vals = [s.get(key) for s in samples if s.get(key) is not None]
    return max(vals) if vals else 0


def _rate(model_id):
    mid = (model_id or "").lower()
    for k, v in PRICING.items():
        if k in mid:
            return v
    return None


def token_totals(session_ids, proj_dir):
    """Sum tokens per model across a session's member conversations, deduped by
    requestId. Returns {model_id: {input, output, cache_read, cache_write, est_cost}}."""
    by_model, seen = {}, set()
    for sid in session_ids:
        for e in read_jsonl(os.path.join(proj_dir, sid + ".jsonl")):
            if e.get("type") != "assistant":
                continue
            rid = e.get("requestId")
            if not rid or rid in seen:
                continue
            msg = e.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            seen.add(rid)
            model = msg.get("model") or "unknown"
            m = by_model.setdefault(model, {"input": 0, "output": 0,
                                            "cache_read": 0, "cache_write": 0})
            m["input"] += usage.get("input_tokens") or 0
            m["output"] += usage.get("output_tokens") or 0
            m["cache_read"] += usage.get("cache_read_input_tokens") or 0
            m["cache_write"] += usage.get("cache_creation_input_tokens") or 0
    for model, m in by_model.items():
        r = _rate(model)
        if r:
            m["est_cost"] = (m["input"] * r["input"] + m["output"] * r["output"]
                             + m["cache_read"] * r["cache_read"]
                             + m["cache_write"] * r["cache_write"]) / 1_000_000
        else:
            m["est_cost"] = 0.0
    return by_model


def summarize_session(samples, proj_dir):
    sids = sorted({s.get("session_id") for s in samples if s.get("session_id")})
    by_model = token_totals(sids, proj_dir)
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for m in by_model.values():
        for k in tok:
            tok[k] += m[k]
    return {
        "start": samples[0].get("ts"),
        "end": samples[-1].get("ts"),
        "cost": _maxval(samples, "cost_usd"),
        "api_ms": _maxval(samples, "api_ms"),
        "wall_ms": _maxval(samples, "wall_ms"),
        "lines_added": _maxval(samples, "lines_added"),
        "lines_removed": _maxval(samples, "lines_removed"),
        "models": sorted({s.get("model") for s in samples if s.get("model")}),
        "conversations": len(sids),
        "tokens": tok,
        "tokens_by_model": by_model,
    }


def _sum_tokens(items):
    tok = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for it in items:
        for k in tok:
            tok[k] += it["tokens"][k]
    return tok


def build_data():
    projects = []
    for marker in sorted(glob.glob(os.path.join(PROJECTS, "*", "usage-tracker", "enabled"))):
        tracker_dir = os.path.dirname(marker)
        proj_dir = os.path.dirname(tracker_dir)
        samples = read_jsonl(os.path.join(tracker_dir, "usage.jsonl"))
        sessions = [summarize_session(s, proj_dir) for s in segment_sessions(samples)]
        cwd = next((u.get("cwd") for u in reversed(samples) if u.get("cwd")), None) \
            or os.path.basename(proj_dir).replace("-", "/")
        repo = next((u.get("repo") for u in reversed(samples) if u.get("repo")), None)
        tok = _sum_tokens(sessions)
        projects.append({
            "name": os.path.basename(cwd.rstrip("/")) or cwd,
            "cwd": cwd,
            "repo": repo,
            "sessions": sessions,
            "totals": {
                "cost": sum(s["cost"] for s in sessions),
                "api_ms": sum(s["api_ms"] for s in sessions),
                "wall_ms": sum(s["wall_ms"] for s in sessions),
                "lines_added": sum(s["lines_added"] for s in sessions),
                "lines_removed": sum(s["lines_removed"] for s in sessions),
                "sessions": len(sessions),
                "tokens": tok,
                "token_total": sum(tok.values()),
            },
        })

    # spend-by-model across all projects (estimated from tokens)
    model_spend = {}
    for p in projects:
        for s in p["sessions"]:
            for model, m in s["tokens_by_model"].items():
                d = model_spend.setdefault(model, {"est_cost": 0.0, "tokens": {
                    "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}})
                d["est_cost"] += m["est_cost"]
                for k in d["tokens"]:
                    d["tokens"][k] += m[k]

    grand_tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for p in projects:
        for k in grand_tokens:
            grand_tokens[k] += p["totals"]["tokens"][k]

    grand = {
        "cost": sum(p["totals"]["cost"] for p in projects),
        "api_ms": sum(p["totals"]["api_ms"] for p in projects),
        "wall_ms": sum(p["totals"]["wall_ms"] for p in projects),
        "lines_added": sum(p["totals"]["lines_added"] for p in projects),
        "lines_removed": sum(p["totals"]["lines_removed"] for p in projects),
        "sessions": sum(p["totals"]["sessions"] for p in projects),
        "tokens": grand_tokens,
        "token_total": sum(p["totals"]["token_total"] for p in projects),
        "projects": len(projects),
        "model_spend": model_spend,
    }
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totals": grand,
        "projects": projects,
    }


def write_data():
    os.makedirs(WEB, exist_ok=True)
    data = build_data()
    with open(os.path.join(WEB, "data.js"), "w") as f:
        f.write("window.TRACKER_DATA = " + json.dumps(data, indent=1) + ";\n")
    return data


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB, **k)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/refresh":
            try:
                write_data()
                body = b'{"ok":true}'
                self.send_response(200)
            except Exception as e:
                body = json.dumps({"ok": False, "error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass  # keep the terminal quiet


def serve():
    socketserver.TCPServer.allow_reuse_address = True
    for port in PORT_RANGE:
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
        except OSError:
            continue
        url = "http://127.0.0.1:{}/".format(port)
        print("Claude Project Tracker serving at {}  (Ctrl-C to stop)".format(url))
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            httpd.server_close()
        return
    print("Could not bind any port in {}-{}.".format(PORT_RANGE.start, PORT_RANGE.stop - 1))
    sys.exit(1)


def main():
    problem = preflight()
    if problem:
        print(problem)
        sys.exit(1)
    data = write_data()
    n = data["totals"]["projects"]
    if n == 0:
        print("No projects are opted in yet. Run  {}  inside a project first."
              .format(INSTALL))
    serve()


if __name__ == "__main__":
    main()
