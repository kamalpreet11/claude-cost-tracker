# Claude Project Tracker — Design Spec

- **Status:** approved (pending user review of this doc)
- **Created:** 2026-06-13
- **Repo home:** `~/claude-project-tracker`
- **Runs:** 100% locally. GitHub is used only as the source repo for this tool's code.

## 1. Goal

A small local tool that turns Claude Code's raw logs into a single offline dashboard
spanning **all** projects you run Claude in. It answers: how much have I spent, how long
did it take (API time + wall time), which models did I use, and how many tokens —
broken down **overall**, **per project**, and **per session**.

## 2. Load-bearing facts (verified, not assumed)

These observed facts drive the entire architecture. They were confirmed by inspecting a
live statusline blob and the on-disk transcripts on 2026-06-13.

1. **The statusline input blob is the only place cost + API-time are exposed.** Regular
   hooks (Stop, SessionStart, etc.) do **not** receive cost data. The blob piped to the
   statusline command contains:
   - `cost.total_cost_usd`, `cost.total_duration_ms` (wall), `cost.total_api_duration_ms` (API time)
   - `cost.total_lines_added`, `cost.total_lines_removed`
   - `model.display_name`, `session_id`, `version`
   - `workspace.current_dir`, `workspace.repo {host, owner, name}`
   - `context_window` (current-context snapshot only — **not** cumulative tokens)
2. **The cost blob is process-cumulative.** Values climb across an entire `claude` run,
   **survive `/clear` and `/compact`**, and reset to ~0 only when a fresh `claude`
   process launches. (Confirmed: a single long run spanned multiple `/clear`s.)
3. **A "session" = one `claude` process, launch → exit** — matching what `/usage` shows.
   This boundary is **not recorded on disk**: each `/clear` writes a *new* transcript
   file with a *new* `sessionId`, and nothing links those files to one process.
4. **Cumulative tokens are not in the blob.** Per-request token counts live only in the
   transcript `.jsonl` files (`message.usage`, with the model on the same entry). They
   must be deduped by `requestId` (the same request appears on multiple streamed lines).
5. Because of #2/#3, **historical data cannot be regrouped into true process-sessions.**
   Decision: **no backfill.** Today is day one; future projects are added on Day 0.

### What each data source provides

| Metric | Source |
|---|---|
| Cost, API time, wall time, lines ± | statusline blob (exact, matches `/usage`) |
| Session boundary (process launch→exit) | statusline blob: cost-reset detection |
| Tokens (overall / per model / per session) | transcript `.jsonl` (dedupe by `requestId`) |
| Models used | both (blob = current; transcripts = all, incl. background haiku) |

## 3. Prerequisites & first-time setup

The tool has a **hard dependency on Claude Code's statusline**, because that is the only
place Claude Code exposes cost and API-time data (fact #1). Not every user already has a
statusline configured, so this must be handled explicitly — both documented here and
enforced at runtime by `tracker.py`.

### 3.1 Runtime prerequisites
- **Python 3.8+** — runs `tracker.py` and the capture helper. Standard library only; no pip.
- **A configured Claude Code statusline** whose script includes the capture snippet. The
  installer can create or amend this for you (see §3.3).
- **No `jq` requirement.** The capture helper is pure Python (`capture.py`), so capture
  works even with no `jq` installed. (A user's *own* existing statusline may use `jq` for
  display — that is their concern, independent of our capture.)

### 3.2 Why the statusline is required
Claude Code invokes the command configured under `statusLine` in `~/.claude/settings.json`
on every render, piping it a JSON blob on stdin. That blob is the sole carrier of
`cost.total_cost_usd` / `cost.total_api_duration_ms`. Therefore:
- If **no** `statusLine` is configured, the blob is never produced and **capture is
  impossible** until one exists.
- If a `statusLine` **is** configured, we add a one-line capture call to its script.

Three states the tooling must detect and handle:

| State | Detection | Action |
|---|---|---|
| No statusline configured | `settings.json` has no `statusLine.command` | Offer to create a minimal statusline script (prints a basic line **and** captures) and add the `settings.json` entry. |
| Statusline exists, no capture | script lacks the `claude-project-tracker` marker | Back up the script, append the capture snippet. |
| Statusline + capture present | marker found | Nothing to do. |

### 3.3 The capture snippet
A single guarded line added to the statusline script:

```bash
# claude-project-tracker capture  (do not remove this marker line)
printf '%s' "$input" | python3 "$HOME/claude-project-tracker/capture.py" 2>/dev/null
```

`capture.py` reads the blob on stdin, checks the per-project opt-in marker, throttles on
cost change, and appends one line to that project's `usage.jsonl`. It **never** writes to
stdout (that would corrupt the statusline) and never breaks rendering — all failures are
swallowed.

If Claude Code has **no** statusline at all, the minimal one the installer offers is:

```bash
#!/bin/bash
input=$(cat)
# claude-project-tracker capture  (do not remove this marker line)
printf '%s' "$input" | python3 "$HOME/claude-project-tracker/capture.py" 2>/dev/null
# minimal visible status line:
echo "$input" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["model"]["display_name"],"|",d["workspace"]["current_dir"])'
```

### 3.4 Runtime preflight (in `tracker.py`)
Before anything else, `tracker.py` runs a **preflight check** and, if capture is not wired
up, **stops and shows the user exactly what to add** instead of silently rendering an empty
dashboard:
1. Read `~/.claude/settings.json`; locate `statusLine.command` (or note its absence).
2. Read that script (if any); check for the `claude-project-tracker` marker.
3. On any missing piece, print: which state was detected, the exact snippet / sample script
   from §3.3, the target file path, and the one-command auto-fix
   (`./install.sh --setup-statusline`). Then exit without opening the dashboard.

## 4. Architecture

Four small, independent pieces.

```
statusline render ──(project opted-in?)──> append blob to project's usage.jsonl
                                                         │
                          tracker (one command) ─────────┤
                            • parse all projects' usage.jsonl + transcripts
                            • segment into sessions (cost-reset)
                            • join token counts from transcripts
                            • write data.js
                            • start localhost listener + open dashboard
                                                         │
                          index.html (reads data.js) ────┘
                            • Refresh button → rings listener → rebuild → reload
```

### 4.1 Capture (global, gated per-project)

- A single guarded line (see §3.3) in the configured statusline script, piping the blob to
  `capture.py`. All capture logic lives in `capture.py` (pure Python, no `jq`).
- On each render: compute the project's tracker dir from `workspace.current_dir`.
  Claude Code stores per-project data at `~/.claude/projects/<encoded-cwd>/`, where
  `<encoded-cwd>` is the abs path with `/` → `-`.
- **Opt-in gate:** only act if `~/.claude/projects/<encoded-cwd>/usage-tracker/enabled`
  exists. Otherwise do nothing (project is untracked).
- **Throttle:** append a line to `usage.jsonl` only when `total_cost_usd` differs from the
  last logged value (cost is monotonic within a process; this collapses the many renders
  per turn down to ~one line per assistant turn, and still captures the reset on relaunch).
- The capture is a pure tee — it never alters what the statusline displays. It must be
  fast and must never error out the statusline (guard every step; fail silent).

`usage.jsonl` line shape (one JSON object per line):

```json
{"ts":"2026-06-13T21:18:00Z","session_id":"00000000-0000-0000-0000-000000000000","model":"Opus 4.8 (1M context)",
 "cost_usd":42.00,"api_ms":7102175,"wall_ms":35305025,"lines_added":2914,"lines_removed":226,
 "cwd":"/Users/me/code/web-app","repo":"acme/web-app","ctx_pct":6}
```

### 4.2 Install (Day 0, per project)

- `install.sh`, run from inside a project directory.
- Computes the encoded project dir, creates `~/.claude/projects/<encoded>/usage-tracker/`
  and the `enabled` marker. Idempotent.
- Runs the §3.2 statusline setup (the three-state handler): creates a minimal statusline if
  none exists, or appends the capture snippet to the existing one (back up first;
  detect-and-skip if the marker is already present).
- A standalone `./install.sh --setup-statusline` performs only the statusline wiring (this
  is the auto-fix `tracker.py`'s preflight points users to).

### 4.3 tracker.py (aggregator + local server)

Single command (aliased to `tracker`). Steps when run:

0. **Preflight** (§3.4): verify a statusline is configured and carries the capture marker.
   If not, print the detected state + exact fix and exit — do not render an empty dashboard.
1. **Discover** all opted-in projects: glob `~/.claude/projects/*/usage-tracker/enabled`.
2. **Per project, build sessions:**
   - Read `usage.jsonl`, sort by `ts`.
   - **Segment into sessions:** start a new session whenever `cost_usd` drops below the
     previous line's value (= a new `claude` process). Within a segment:
     - `cost = max(cost_usd)`, `api_ms = max(api_ms)`, `wall_ms = max(wall_ms)`,
       `lines_± = max(...)`, `start = first ts`, `end = last ts`
     - `models = set(model)`, `member_session_ids = set(session_id)`
   - **Tokens:** for each `member_session_id`, read its transcript
     `~/.claude/projects/<encoded>/<session_id>.jsonl`, dedupe assistant entries by
     `requestId`, sum `input/output/cache_read/cache_write` grouped by `message.model`.
     Sum across the session's member conversations → tokens per model per session.
3. **Aggregate** to per-project and all-projects totals.
4. **Write `data.js`**: `window.TRACKER_DATA = { generated_at, totals, projects:[...] }`.
5. **Serve**: start a minimal `http.server` on a fixed localhost port; expose:
   - `/` → the dashboard (index.html, app.js, styles.css, data.js)
   - `/refresh` → re-runs steps 1–4, regenerates `data.js`, returns 200.
6. **Open** the browser at `http://localhost:<port>/`.

Zero third-party dependencies — Python 3 stdlib only (`http.server`, `json`, `glob`,
`pathlib`). No pip install.

### 4.4 Dashboard (index.html + app.js + styles.css)

Approved mockup is the reference (`/tmp/claude-tracker-mockup.html` during design):

- **Header:** title + Refresh button (fetches `/refresh`, then reloads).
- **KPI row:** total cost, API time, wall time, tokens, sessions — across all projects.
- **Projects list:** one expandable row per project (name, repo, cost, API time, tokens).
  Expand → per-session table: session window, wall, API, cost, models (tags), tokens,
  lines ±.
- **Spend-by-model** bar across all projects.
- Dark theme, amber/blue accents, tabular-numeric figures. Reads only
  `window.TRACKER_DATA`; no network calls except the Refresh button hitting `/refresh`.

## 5. Repo layout

```
~/claude-project-tracker/
  install.sh                 # opt-in a project + wire capture into statusline
  capture.py                 # pure-Python statusline capture helper (gated, throttled)
  tracker.py                 # preflight + aggregator + localhost server + browser open
  web/
    index.html
    app.js
    styles.css
  docs/specs/2026-06-13-claude-project-tracker-design.md   # this file
  .gitignore                 # ignores any generated data.js / sample data
  README.md
```

Raw personal data (`usage.jsonl`, transcripts) lives under `~/.claude/projects/...`,
**never** inside the repo. Generated `data.js` is local and gitignored. The repo holds
only the tool's code.

## 6. Non-goals (YAGNI)

- No historical backfill (see fact #5).
- No remote hosting, no GitHub Pages, no auth, no multi-user.
- No charts library / CDN — plain CSS bars only, fully offline.
- No real-time auto-refresh; Refresh is an explicit button.
- No editing/annotating data from the UI; it is read-only.

## 7. Rejected alternatives

- **Stop/SessionEnd hook as the capture source** — rejected: those hooks don't receive
  cost or API-time data; only the statusline blob does (fact #1).
- **Pure static `file://` dashboard, no server** — rejected: a sandboxed page cannot
  trigger the Python rebuild, so the requested in-page Refresh button is impossible
  without a listener. The one-command launch makes the server invisible, satisfying the
  "don't make me babysit a server" concern.
- **Key sessions purely by `session_id`** — rejected: `session_id` rotates on every
  `/clear`, which would shatter one real process-session into many rows. Cost-reset
  segmentation reconstructs the true `/usage` session unit.
- **Per-project isolated dashboards** (original idea) — superseded by the single all-projects
  portal the user requested; per-project *isolation of capture* is retained.
- **Embedding data directly into index.html** instead of `data.js`/server — workable for a
  static build but blocks the live Refresh button; dropped in favor of the server model.
- **Shell + `jq` capture snippet** — rejected: would add a `jq` dependency and bloat the
  statusline script. Piping the blob to a pure-Python `capture.py` keeps the snippet to one
  line and reuses the Python we already require.

## 8. Open questions

None blocking. Fixed localhost port choice (e.g., 8787) and exact accent palette can be
finalized during implementation; both are trivially changeable.
