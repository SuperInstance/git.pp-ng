#!/usr/bin/env bash
# scene.sh — one-command judge replay of the git.pp scene against the
# LIVE Worker + real Cloudflare Artifacts.
#
#   bash scene.sh
#
# Needs: bash, git, curl, python3, and the admin key (pp.py reads
# PP_ADMIN_KEY, or ../.admin-key.local next to this script's folder).
# Git pushes go to Artifacts from wherever this script runs — run it
# on a machine whose git can reach Artifacts.
#
# Every run uses a fresh project `demo-<random suffix>`, so it is
# idempotent-by-suffix and re-runnable. The demo repos are left in the
# namespace as evidence; the cleanup commands are printed at the end.
#
# What it proves, step by step (see runner/README.md for the mapping):
# two agents complete conflicting work in forked task repos; the fold
# derives one deterministic state; the conflict is labelled and the
# losers preserved; a reasoned captain's override is inherited by all
# later derivation — even on branches that never descended from it;
# a witness records a real observation; checkpoints verify honestly
# and reject forgery.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PP=(python3 "$HERE/pp.py")
WORKER="${PP_WORKER:-https://gitpp.superinstance.dev}"
export PP_POC_DIR="${PP_POC_DIR:-$(cd "$HERE/../referee" && pwd)}"

SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
PROJECT="demo-$SUFFIX"
REPOS=("$PROJECT" "$PROJECT--task-a" "$PROJECT--task-b" "$PROJECT--task-d" "$PROJECT--task-c")

PASS=0; FAIL=0; FAILED=()
say() { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "PASS — $1"; }
no()   { FAIL=$((FAIL+1)); FAILED+=("$1"); say "FAIL — $1"; }
step() { # step <name> <cmd...>
  local name="$1"; shift
  say "·· $name"
  local out rc
  out="$("$@" 2>&1)" && rc=0 || rc=$?
  printf '%s\n' "$out" | sed 's/^/   /'
  if [ "$rc" -eq 0 ]; then ok "$name"; else no "$name"; fi
}
assert_state() { # assert_state <name> <python-expr over S=state, C=conflicts> <want>
  local name="$1" expr="$2" want="$3" got
  got="$(printf '%s' "$STATE_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
S, C = d.get('state', {}), {c['key']: c for c in d.get('conflicts', [])}
print($expr)")" || got="__error__"
  if [ "$got" = "$want" ]; then ok "$name"; else no "$name (want $want, got $got)"; fi
}

# Temp space under $HOME, never /tmp: some machines run a tight quota
# on /tmp and the clones below would flake.
export TMPDIR="${PP_TMPDIR:-$HOME/.pp-scene-tmp}"
mkdir -p "$TMPDIR"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
mkdir -p "$TMPD/work-a" "$TMPD/work-b" "$TMPD/work-c" "$TMPD/work-d"
echo "Agent A's work product: cache layer."  > "$TMPD/work-a/work-a.txt"
echo "Agent B's work product: search."       > "$TMPD/work-b/work-b.txt"
echo "Agent D's work product: aggressive timeout tuning." > "$TMPD/work-d/work-d.txt"
echo "Agent C's work product: inherits the override."      > "$TMPD/work-c/work-c.txt"
if [ -s "$TMPD/work-a/work-a.txt" ] && [ -s "$TMPD/work-b/work-b.txt" ] \
   && [ -s "$TMPD/work-c/work-c.txt" ] && [ -s "$TMPD/work-d/work-d.txt" ]; then
  ok "work files staged for all four agents"
else
  no "work files staged for all four agents"
fi

say "=== git.pp scene replay — project $PROJECT (worker $WORKER) ==="

# -- 1. project + genesis -------------------------------------------------
step "create project repo $PROJECT" "${PP[@]}" project create "$PROJECT" \
  --description "judge replay scene (runner/scene.sh) — safe to delete"
step "genesis record lands in the project repo" "${PP[@]}" complete "$PROJECT" \
  --id genesis --parents "" --kind task --genesis \
  --set "project=$PROJECT" --set status=open

# -- 2. two agents, conflicting completions -------------------------------
step "fork task-a" "${PP[@]}" task fork "$PROJECT" task-a
step "fork task-b" "${PP[@]}" task fork "$PROJECT" task-b
step "task-a completes: config.timeout=30, feature.cache=on" \
  "${PP[@]}" complete "$PROJECT--task-a" \
  --id task-a --parents genesis --kind task \
  --set config.timeout=30 --set feature.cache=on --work "$TMPD/work-a"
step "task-b completes: config.timeout=60, feature.search=on" \
  "${PP[@]}" complete "$PROJECT--task-b" \
  --id task-b --parents genesis --kind task \
  --set config.timeout=60 --set feature.search=on --work "$TMPD/work-b"

# -- 3. witness: a real observation from this machine ----------------------
DISK="$(df -P / | tail -1 | awk '{print $5}' | tr -d '%')"
case "$DISK" in ''|*[!0-9]*) DISK="" ;; esac
if [ -n "$DISK" ]; then ok "witness observation taken (root disk ${DISK}% via df)"; else no "witness observation taken"; fi
step "witness-1 committed to the project repo" "${PP[@]}" complete "$PROJECT" \
  --id witness-1 --parents genesis --kind witness \
  --set "env.disk_pct=$DISK"

# -- 4. the captain's override, then work on both sides of it --------------
step "override-1 lands: config.timeout=45, with a reason" \
  "${PP[@]}" complete "$PROJECT" \
  --id override-1 --parents task-a,task-b --kind override \
  --set config.timeout=45 --reason "captain: 45 is the safe value"
step "fork task-d" "${PP[@]}" task fork "$PROJECT" task-d
step "fork task-c" "${PP[@]}" task fork "$PROJECT" task-c
step "task-d completes: config.timeout=99, NOT descended from the override" \
  "${PP[@]}" complete "$PROJECT--task-d" \
  --id task-d --parents task-b --kind task \
  --set config.timeout=99 --work "$TMPD/work-d"
step "task-c completes: descended from override-1" \
  "${PP[@]}" complete "$PROJECT--task-c" \
  --id task-c --parents override-1 --kind task \
  --set feature.inherited=true --work "$TMPD/work-c"

# -- 5. assertions against /api/state --------------------------------------
say "·· fetching derived state from the Worker"
STATE_JSON="$(curl -fsS "$WORKER/api/state?project=$PROJECT" 2>/dev/null)" || STATE_JSON=""
if [ -n "$STATE_JSON" ]; then ok "GET /api/state responds"; else no "GET /api/state responds"; STATE_JSON='{}'; fi

assert_state "override inherited: config.timeout == 45" \
  "S.get('config.timeout')" "45"
assert_state "conflict winner is override-1" \
  "C.get('config.timeout', {}).get('winner')" "override-1"
assert_state "losers include all three tasks (task-a, task-b, task-d)" \
  "all(x in C.get('config.timeout', {}).get('losers', []) for x in ('task-a','task-b','task-d'))" "True"
assert_state "witness key present with the observed value ($DISK)" \
  "S.get('env.disk_pct')" "$DISK"
assert_state "task-c completed normally: feature.inherited == true" \
  "S.get('feature.inherited')" "True"
assert_state "both agents' features intact (cache + search on)" \
  "(S.get('feature.cache'), S.get('feature.search'))" "('on', 'on')"
assert_state "no orphans" \
  "len(d.get('orphans', []))" "0"

# -- 6. determinism: shuffle the collected record, re-derive ---------------
if "${PP[@]}" collect "$PROJECT" > "$TMPD/records.json" 2>/dev/null; then
  ok "records collected from the repos"
  if POC_DIR="$PP_POC_DIR" python3 - "$TMPD/records.json" <<'PYEOF'
import hashlib, json, os, random, sys
sys.path.insert(0, os.environ["POC_DIR"])
import fold
records = json.load(open(sys.argv[1]))
rng = random.Random(20261011)
hashes = set()
for _ in range(50):
    perm = records[:]
    rng.shuffle(perm)
    out = fold.derive(perm)
    hashes.add(hashlib.sha256(json.dumps(out, sort_keys=True).encode()).hexdigest())
print(f"determinism shuffle: {len(records)} records x 50 permutations -> "
      f"{len(hashes)} distinct derived output(s)")
sys.exit(0 if len(hashes) == 1 else 1)
PYEOF
  then ok "derive is deterministic under 50 permutations"; else no "derive is deterministic under 50 permutations"; fi
else
  no "records collected from the repos"
  no "derive is deterministic under 50 permutations (collection failed)"
fi

# -- 7. checkpoints: honest passes, tampered is rejected --------------------
VOUT="$("${PP[@]}" verify "$PROJECT" --at task-a 2>&1)"
printf '%s\n' "$VOUT" | sed 's/^/   /'
if printf '%s' "$VOUT" | grep -q "honest checkpoint: PASS"; then
  ok "honest checkpoint at task-a verifies"
else
  no "honest checkpoint at task-a verifies"
fi
VOUT="$("${PP[@]}" verify "$PROJECT" --at task-a --tamper 2>&1)"
printf '%s\n' "$VOUT" | sed 's/^/   /'
if printf '%s' "$VOUT" | grep -q "tamper check: PASS"; then
  ok "tampered checkpoint is rejected"
else
  no "tampered checkpoint is rejected"
fi

# -- verdict -----------------------------------------------------------------
say ""
say "=== scene verdict: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  printf 'failed: %s\n' "${FAILED[@]}"
fi
say ""
say "Demo repos left in the namespace as evidence:"
for r in "${REPOS[@]}"; do say "  $r"; done
say "Projection: $WORKER/?project=$PROJECT"
say "Cleanup (deletes all five demo repos):"
say "  export PP_ADMIN_KEY=\"\$(cat \"$HERE/../.admin-key.local\")\""
for r in "${REPOS[@]}"; do
  say "  curl -X DELETE -H \"X-Admin-Key: \$PP_ADMIN_KEY\" $WORKER/api/repos/$r"
done

[ "$FAIL" -eq 0 ]
