#!/bin/bash
# Claude Project Tracker - installer.
#   ./install.sh                  wire the statusline (once) + opt this project in
#   ./install.sh --setup-statusline   only wire the statusline capture
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDEDIR="$HOME/.claude"
PROJECTS="$CLAUDEDIR/projects"
SETTINGS="$CLAUDEDIR/settings.json"
SL_DEFAULT="$CLAUDEDIR/statusline-command.sh"
MARKER="claude-project-tracker"
CAP="$HERE/capture.py"

ensure_alias() {
  local rc
  case "${SHELL:-}" in
    *zsh)  rc="$HOME/.zshrc" ;;
    *bash) rc="$HOME/.bashrc" ;;
    *)     rc="$HOME/.zshrc" ;;
  esac
  if [ -f "$rc" ] && grep -q "alias tracker=" "$rc"; then
    echo "[=] 'tracker' alias already in $rc"
  else
    printf "\n# Claude Project Tracker dashboard\nalias tracker='python3 \"%s/tracker.py\"'\n" "$HERE" >> "$rc"
    echo "[+] added 'tracker' alias to $rc  (open a new terminal, or: source $rc)"
  fi
}

setup_statusline() {
  CAP="$CAP" SETTINGS="$SETTINGS" SL_DEFAULT="$SL_DEFAULT" MARKER="$MARKER" \
  python3 - <<'PY'
import os, json, shutil, time

settings   = os.environ["SETTINGS"]
sl_default = os.environ["SL_DEFAULT"]
marker     = os.environ["MARKER"]
cap        = os.environ["CAP"]
os.makedirs(os.path.dirname(settings), exist_ok=True)

def write_minimal(path):
    script = (
        "#!/bin/bash\n"
        "input=$(cat)\n"
        f"# {marker} capture  (do not remove this marker line)\n"
        f"printf '%s' \"$input\" | python3 \"{cap}\" 2>/dev/null\n"
        "echo \"$input\" | python3 -c 'import sys,json; d=json.load(sys.stdin); "
        "print(d.get(\"model\",{}).get(\"display_name\",\"\"),\"|\","
        "d.get(\"workspace\",{}).get(\"current_dir\",\"\"))'\n"
    )
    open(path, "w").write(script)
    os.chmod(path, 0o755)

def wrap(path):
    # keep the user's script intact; run it after capturing the same stdin
    orig = (path[:-3] if path.endswith(".sh") else path) + ".orig.sh"
    shutil.copy(path, path + ".bak." + time.strftime("%Y%m%d%H%M%S"))
    shutil.move(path, orig)
    wrapper = (
        "#!/bin/bash\n"
        f"# {marker} capture  (do not remove this marker line)\n"
        "input=$(cat)\n"
        f"printf '%s' \"$input\" | python3 \"{cap}\" 2>/dev/null\n"
        f"printf '%s' \"$input\" | \"{orig}\"\n"
    )
    open(path, "w").write(wrapper)
    os.chmod(path, 0o755)
    os.chmod(orig, 0o755)
    return orig

cfg = {}
if os.path.exists(settings):
    try:
        cfg = json.load(open(settings))
    except Exception:
        cfg = {}

sl = cfg.get("statusLine")
cmd = sl.get("command") if isinstance(sl, dict) else None
path = os.path.expanduser(os.path.expandvars(cmd.split()[0])) if cmd else None

if not cmd or not (path and os.path.exists(path)):
    write_minimal(sl_default)
    cfg["statusLine"] = {"type": "command", "command": sl_default}
    json.dump(cfg, open(settings, "w"), indent=2)
    print(f"[+] created statusline at {sl_default} and updated settings.json")
else:
    content = open(path).read()
    if marker in content:
        print("[=] capture already wired into your statusline; nothing to do")
    else:
        orig = wrap(path)
        print(f"[+] wrapped your statusline to add capture")
        print(f"    original kept at {orig} (timestamped .bak also saved)")
PY
  ensure_alias
}

opt_in_project() {
  local cwd enc proj found
  cwd="$(pwd)"
  enc="$(printf '%s' "$cwd" | sed 's#[/.]#-#g')"
  proj="$PROJECTS/$enc"
  if [ ! -d "$proj" ]; then
    found="$(grep -ls "\"cwd\":\"$cwd\"" "$PROJECTS"/*/*.jsonl 2>/dev/null | head -1 || true)"
    [ -n "$found" ] && proj="$(dirname "$found")"
  fi
  mkdir -p "$proj/usage-tracker"
  touch "$proj/usage-tracker/enabled"
  echo "[+] tracking enabled for: $cwd"
  echo "    data dir: $proj/usage-tracker"
  if [ ! -d "$PROJECTS/$enc" ] && [ "$proj" = "$PROJECTS/$enc" ]; then
    echo "    note: this project has no Claude history yet; start Claude here so"
    echo "          transcripts land in the same folder."
  fi
}

case "${1:-}" in
  --setup-statusline) setup_statusline ;;
  "") setup_statusline; opt_in_project ;;
  *) echo "usage: $0 [--setup-statusline]"; exit 1 ;;
esac
