#!/usr/bin/env python3
"""Claude Project Tracker - statusline capture helper.

Reads the statusline JSON blob on stdin and, if the current project is opted in,
appends one throttled line to that project's usage.jsonl.

Contract (do not break):
  - NEVER write to stdout (that would corrupt the statusline display).
  - NEVER exit non-zero in a way that disrupts rendering; swallow every error.
"""
import sys
import os
import json
import glob
from datetime import datetime

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


def _resolve_project_dir(session_id, cwd):
    """Find the canonical ~/.claude/projects/<dir> for this session.

    Primary: glob for the session's own transcript (authoritative, no guessing
    about how Claude encodes the path). Fallback: encode the cwd.
    """
    if session_id:
        hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
        if hits:
            return os.path.dirname(hits[0])
    enc = cwd.replace("/", "-").replace(".", "-")
    return os.path.join(PROJECTS, enc)


def _last_cost(path):
    """Return the cost_usd on the last recorded line, or None."""
    try:
        last = None
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        if last:
            return json.loads(last).get("cost_usd")
    except Exception:
        pass
    return None


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        return
    blob = json.loads(raw)

    cost = blob.get("cost") or {}
    total_cost = cost.get("total_cost_usd")
    if total_cost is None:
        return

    ws = blob.get("workspace") or {}
    cwd = ws.get("current_dir") or ""
    session_id = blob.get("session_id") or ""

    proj_dir = _resolve_project_dir(session_id, cwd)
    tracker_dir = os.path.join(proj_dir, "usage-tracker")
    if not os.path.exists(os.path.join(tracker_dir, "enabled")):
        return  # project not opted in

    usage_path = os.path.join(tracker_dir, "usage.jsonl")

    # Throttle: cost is monotonic within a process, so only write when it moves.
    prev = _last_cost(usage_path)
    if prev is not None and abs(prev - total_cost) < 1e-9:
        return

    repo = ws.get("repo") or {}
    repo_str = None
    if repo.get("owner") and repo.get("name"):
        repo_str = "{}/{}".format(repo["owner"], repo["name"])

    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_id": session_id,
        "model": (blob.get("model") or {}).get("display_name"),
        "cost_usd": total_cost,
        "api_ms": cost.get("total_api_duration_ms"),
        "wall_ms": cost.get("total_duration_ms"),
        "lines_added": cost.get("total_lines_added"),
        "lines_removed": cost.get("total_lines_removed"),
        "cwd": cwd,
        "repo": repo_str,
        "ctx_pct": (blob.get("context_window") or {}).get("used_percentage"),
    }
    with open(usage_path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never break the statusline
