# runner/ — drive git.pp like a judge

Everything here runs on **bash + git + curl + python3 (stdlib only)**.
No installs, no build step, no wrangler. You are talking to the live
Worker (`https://gitpp.casey-digennaro.workers.dev`) and real
Cloudflare Artifacts repos through it.

- `pp.py` — the agent-side CLI (one file).
- `scene.sh` — a one-command replay of the whole demo scene, with an
  assertion per claim and a final verdict.
- `SCENE-RUN-LOG.md` — a captured run: **26 passed, 0 failed**.

**Why `pp.py` and not a shell script:** python3 is already required —
checkpoint verification runs the Python referee (`../referee/fold.py`),
and the TS fold in the Worker is proven identical to it (see
`../conformance/`). JSON authoring/parsing in pure bash means leaning
on `jq` (not in the minimal toolset) or hand-rolled quoting around
record files and API bodies. One stdlib-only Python file keeps the
record format exact everywhere and the tool list at four.

## Setup

Mutating commands are gated by the Worker's admin key. `pp.py` reads
it, in order, from:

1. `PP_ADMIN_KEY` (environment), or
2. `PP_ADMIN_KEY_FILE`, or
3. `../.admin-key.local` — the mode-600 file beside this folder in
   the build tree.

It is read at runtime, never printed and never written by the tools.
Read-only commands (`state`) need no key.

```bash
cd runner/
python3 pp.py state poc-quilt-1     # the seeded demo project
```

## The commands

```bash
# create a project repo (gated Worker route -> Artifacts binding)
python3 pp.py project create my-project

# fork a task repo: my-project--task-a (gated)
python3 pp.py task fork my-project task-a

# mint a repo token (gated). Prints git-config export lines — this is
# the ONE place a token value reaches stdout: you asked for it, it's
# yours, it's short-lived. Everywhere else tokens travel inside git's
# environment only and never touch files or .git/config.
python3 pp.py token my-project--task-a --scope write

# complete a task: clone the repo, write record.json (+ any work
# files), commit, push. --set values parse as JSON when they can
# (30 -> number, true -> boolean, on -> string); quote to force a
# string: --set 'k="30"'.
python3 pp.py complete my-project--task-a \
  --id task-a --parents genesis --kind task \
  --set config.timeout=30 --set feature.cache=on \
  --work ./my-work-dir

# derived state + conflicts for a project (public, no key)
python3 pp.py state my-project

# checkpoint verification against the Python referee: collect the
# records from the repos, derive the ancestor-closure state at a
# record, and check it. --tamper flips one value and must be rejected.
python3 pp.py verify my-project --at task-a
python3 pp.py verify my-project --at task-a --tamper

# dump the collected records as JSON (what scene.sh shuffles)
python3 pp.py collect my-project
```

`complete` works for all three record kinds (`task`, `witness`,
`override` — overrides take `--reason "..."`), and for the project
repo itself, which accumulates one record per commit (genesis,
witnesses, overrides) while each task repo carries its task's record
at HEAD. Git runs wherever you invoke the command; the fleet runs it
from Oracle because that network route to Artifacts git is healthy.
Temp clones live under `$TMPDIR` (`PP_TMPDIR` to override) and are
deleted after each command.

## The scene — `bash scene.sh`

One command, a fresh project `demo-<random suffix>` every run
(idempotent-by-suffix; nothing to reset). What each step proves:

| Step | Proves |
|---|---|
| create project + genesis record | repos are the record; a project is an Artifacts repo whose history is task completions |
| fork task-a / task-b; both complete against the same parent | multiple agents work concurrently in isolation — one repo per task, no shared working tree, no locks |
| task-a sets `config.timeout=30`, task-b sets `=60` | real divergence lands, in both repos, unmerged and unrewritten |
| witness-1 records `df` disk % from the machine running the scene | ambient truth enters the record as a *witness* — observed, attributable, and (by the fold's class rule) never able to outvote a task |
| override-1 sets `=45` **with a reason** | a captain's decision is a record like any other — with its rationale attached |
| task-d completes `=99` *not* descended from the override | later work on an uninformed branch cannot silently undo the decision |
| task-c completes descended from override-1 | work that builds on the decision proceeds normally |
| **assert:** `/api/state` → timeout `45`, winner `override-1`, losers = task-a, task-b, task-d | **override inheritance**: resolution is a deterministic function of ancestry, evaluated fresh on every request; the losers are preserved (their repos still clone; their values are named in the conflict) — losing is free |
| **assert:** witness key present with the observed value; both features on; no orphans | nothing was dropped on the floor on the way through |
| collect + shuffle the record 50 ways, re-derive each time | **determinism**: the derived state is a pure function of the record *set* — arrival order, push order, and collection order cannot change the answer |
| verify honest checkpoint at task-a; verify with one value flipped | **checkpoints are honest or rejected**: any prefix of the record can be re-derived and checked; a forged state fails |

Latest captured run: `SCENE-RUN-LOG.md` — 26/26, ~44 s wall,
including two real pushes per repo and the full assertion battery.

## Cleanup

Demo repos persist in the Artifacts namespace until deleted — they
are cheap, and they are the evidence. Delete them through the gated
route (exact commands; the scene prints these for its own repos at
the end of every run):

```bash
export PP_ADMIN_KEY="$(cat ../.admin-key.local)"   # from runner/
curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" \
  https://gitpp.casey-digennaro.workers.dev/api/repos/demo-XXXXXX
curl -X DELETE -H "X-Admin-Key: $PP_ADMIN_KEY" \
  https://gitpp.casey-digennaro.workers.dev/api/repos/demo-XXXXXX--task-a
# …and the --task-b, --task-d, --task-c forks, same shape.
```

## Notes / limits

- The projection (`/api/state`, the HTML page) is derived fresh per
  request from the record — there is no cache to invalidate, so the
  runner never has to "wait for propagation" after a push.
- `pp.py` sends an explicit `User-Agent`: Cloudflare refuses
  python-urllib's default signature (error 1010).
- Collection for `verify`/`collect` mints a short-lived read token
  per repo through the gated route and clones locally; nothing is
  written back.
- Repo names: letters, digits, `. _ -`, max 100 chars (Worker rule).
  Task repo names are always `<project>--<task>`.
