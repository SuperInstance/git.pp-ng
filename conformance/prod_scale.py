#!/usr/bin/env python3
"""Production-scale conformance: the fold at 62 / 250 / 1,000 records.

The shipped harness proves the two ports agree on scenes of at most 62
records. This sibling generator builds production-shaped projects — many
parallel task lanes (deep chains), cross-lane diamonds, witnesses
interleaved, overrides landing late over wide frontiers, and one hot key
written by ~30% of all records — and asserts, per size:

  * TypeScript derive() == Python referee derive() (state, order,
    conflicts, orphans — full structural equality), and
  * both agree with an independent brute-force oracle (per-key winners
    from full ancestor sets; canonical order from a from-scratch Kahn
    min-id implementation written against DESIGN.md, not the fold code).

It also reports derive() wall-times (median of timed reps, one warmup)
for both ports. Exploratory probes beyond the required sizes run under
hard timeouts; a timeout or OOM is reported as a finding, not a failure
of this script.

Usage: python3 prod_scale.py [--sizes 62,250,1000] [--probes 2500,5000]
Exit code 0 iff every required size passes all three-way assertions.
"""
import heapq
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path("/tmp/prod-scale-work")
WORK.mkdir(exist_ok=True)

SEED = 20261014
LANES = 8
WARM_KEYS = [f"cfg.k{i}" for i in range(8)]


def gen_project(n, seed=SEED):
    """A production-shaped record set of exactly n records (see docstring)."""
    import random
    rng = random.Random(seed + n)
    recs = []

    def R(rid, parents, kind, effects):
        d = {"id": rid, "parents": parents, "kind": kind, "effects": effects}
        if kind == "override":
            d["reason"] = f"ruling at {rid}"
        return d

    recs.append(R("r0000", [], "task",
                  {"project": "prod-scale", "config.hot": 0}))
    lane_heads = ["r0000"] * LANES
    for i in range(1, n):
        rid = f"r{i:04d}"
        lane = i % LANES
        roll = rng.random()
        if roll < 0.18:
            kind = "witness"
        elif i >= int(0.7 * n) and roll < 0.28:
            kind = "override"
        else:
            kind = "task"
        parents = [lane_heads[lane]]
        if rng.random() < 0.22:
            other = lane_heads[rng.randrange(LANES)]
            if other not in parents:
                parents.append(other)
        if rng.random() < 0.08 and len(recs) > 4:
            deep = recs[rng.randrange(0, len(recs) // 2)]["id"]
            if deep not in parents:
                parents.append(deep)
        effects = {}
        if rng.random() < 0.30:
            effects["config.hot"] = i  # unique value: a genuine wide conflict
        for _ in range(rng.randint(0, 2)):
            if rng.random() < 0.35:
                effects[rng.choice(WARM_KEYS)] = rng.randint(0, 99)
        if kind == "witness" and rng.random() < 0.5:
            effects[f"seen.lane{lane}"] = i
        if kind == "task" and rng.random() < 0.15:
            effects[f"task.{rid}"] = 1
        recs.append(R(rid, parents, kind, effects))
        lane_heads[lane] = rid
    return recs


# ---------------------------------------------------------------------------
# Independent oracle: written against DESIGN.md's rules, sharing no code
# with referee/fold.py beyond the record format itself.
# ---------------------------------------------------------------------------
RANK = {"override": 0, "task": 1}  # everything else ranks as witness (2)


def oracle_applied_order(records):
    """Orphans (missing-parent propagation + cycle-blocked) and the
    canonical smallest-id-first Kahn order, computed from scratch."""
    idx = {r["id"]: r for r in records}
    parents = {rid: [str(p) for p in (r.get("parents") or [])]
               for rid, r in idx.items()}
    children = {rid: [] for rid in idx}
    for rid, ps in parents.items():
        for p in ps:
            if p in children:
                children[p].append(rid)
    orphan = set()
    work = [rid for rid in idx if any(p not in idx for p in parents[rid])]
    orphan.update(work)
    head = 0
    while head < len(work):
        for c in children[work[head]]:
            if c not in orphan:
                orphan.add(c)
                work.append(c)
        head += 1
    live = [rid for rid in idx if rid not in orphan]
    indeg = {rid: sum(1 for p in parents[rid] if p in idx) for rid in live}
    ready = [rid for rid in live if indeg[rid] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for c in children[n]:
            if c in indeg:
                indeg[c] -= 1
                if indeg[c] == 0:
                    heapq.heappush(ready, c)
    cyc = set(live) - set(order)
    orphan |= cyc
    return sorted(orphan), [r for r in order if r not in cyc]


def oracle_winners(records, applied):
    """Brute-force per-key winners: full ancestor sets, exact maximality,
    documented tie-break (most intent ancestors, fewest witness ancestors,
    smallest id)."""
    idx = {r["id"]: r for r in records}
    parents = {rid: [str(p) for p in (r.get("parents") or [])]
               for rid, r in idx.items()}
    applied_set = set(applied)
    anc = {}

    def ancestors(rid):
        if rid in anc:
            return anc[rid]
        seen, stack = set(), list(parents[rid])
        while stack:
            x = stack.pop()
            if x in seen or x not in applied_set:
                continue
            seen.add(x)
            stack.extend(parents[x])
        anc[rid] = seen
        return seen

    def rank(r):
        return RANK.get(r.get("kind"), 2)

    setters = {}
    for rid in applied:
        for k in (idx[rid].get("effects") or {}):
            setters.setdefault(str(k), set()).add(rid)
    winners = {}
    for k, cs in setters.items():
        best = min(rank(idx[c]) for c in cs)
        pool = {c for c in cs if rank(idx[c]) == best}
        dominated = set()
        for d in pool:
            dominated |= ancestors(d) & pool
        maximal = sorted(pool - dominated)
        if len(maximal) == 1:
            winners[k] = maximal[0]
        else:
            def clean(c):
                a = ancestors(c)
                intent = sum(1 for x in a if rank(idx[x]) < 2)
                wit = sum(1 for x in a if rank(idx[x]) == 2)
                return (-intent, wit, c)
            winners[k] = min(maximal, key=clean)
    return winners


def check_size(name, records, timeout=900):
    """Run both ports under subprocess timeouts; compare; oracle-check.
    Returns a result dict (never raises on port failure — that IS data)."""
    case_path = WORK / f"{name}.cases.json"
    py_path = WORK / f"{name}.py.json"
    ts_path = WORK / f"{name}.ts.json"
    case_path.write_text(json.dumps([{"name": name, "records": records}]))
    reps = {62: 7, 250: 5, 1000: 3}.get(len(records), 1)
    res = {"name": name, "n": len(records), "reps": reps}

    def run(cmd, timeout):
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout)
            return p, time.time() - t0
        except subprocess.TimeoutExpired:
            return None, timeout

    p, wall = run([sys.executable, str(HERE / "prod_scale_referee.py"),
                   str(case_path), str(py_path), str(reps)], timeout)
    if p is None or p.returncode != 0:
        res["py_error"] = ("timeout" if p is None
                           else (p.stderr or "")[-400:])
        res["py_wall_s"] = wall
        return res
    p2, wall2 = run(["node", str(HERE / "prod_scale_driver.ts"),
                     str(case_path), str(ts_path), str(reps)], timeout)
    if p2 is None or p2.returncode != 0:
        res["ts_error"] = ("timeout" if p2 is None
                           else (p2.stderr or "")[-400:])
        res["ts_wall_s"] = wall2
        return res

    py = json.loads(py_path.read_text())[0]
    ts = json.loads(ts_path.read_text())[0]
    res["py_times"] = py["timesMs"]
    res["ts_times"] = ts["timesMs"]
    res["py_median_ms"] = statistics.median(py["timesMs"])
    res["ts_median_ms"] = statistics.median(ts["timesMs"])
    res["py_rss_mb"] = py["maxRssMb"]
    res["ts_rss_mb"] = ts["maxRssMb"]
    res["ports_equal"] = py["derive"] == ts["derive"]

    orphans, order = oracle_applied_order(records)
    winners = oracle_winners(records, order)
    out = py["derive"]
    problems = []
    if out["orphans"] != orphans:
        problems.append("orphans != oracle")
    if out["order"] != order:
        problems.append("order != oracle Kahn")
    cmap = {c["key"]: c["winner"] for c in out["conflicts"]}
    idx = {r["id"]: r for r in records}
    for k, w in winners.items():
        want = (idx[w].get("effects") or {})[k]
        if out["state"].get(k) != want:
            problems.append(f"state[{k}] != oracle winner value")
            break
    for k, w in cmap.items():
        if winners.get(k) != w:
            problems.append(f"conflict winner {k}: fold {w} != oracle "
                            f"{winners.get(k)}")
            break
    res["oracle_ok"] = not problems
    res["oracle_problems"] = problems
    res["conflicts"] = len(out["conflicts"])
    res["keys"] = len(out["state"])
    res["hot_key_candidates"] = sum(
        1 for r in records if "config.hot" in (r.get("effects") or {}))
    depth = {}
    for rid in order:
        ps = [p for p in (idx[rid].get("parents") or []) if p in depth]
        depth[rid] = 1 + max((depth[p] for p in ps), default=-1)
    res["max_depth"] = max(depth.values(), default=0)
    return res


def main():
    def _int_list(flag, default):
        if flag not in sys.argv:
            return default
        i = sys.argv.index(flag) + 1
        if i >= len(sys.argv) or sys.argv[i].startswith("--"):
            return []
        return [int(x) for x in sys.argv[i].split(",") if x]

    sizes = _int_list("--sizes", [62, 250, 1000])
    probes = _int_list("--probes", [2500, 5000])

    results = []
    ok = True
    # Apples-to-apples with the documented 12 ms: the real poc-scale-1 set.
    live = json.loads((HERE / "live-records.json").read_text())
    results.append(check_size("live-poc-scale-1", live["poc-scale-1"]))
    for n in sizes:
        results.append(check_size(f"gen-{n}", gen_project(n)))
    for r in results:
        print(json.dumps({k: v for k, v in r.items()
                          if k not in ("py_times", "ts_times")}, indent=1))
        if r.get("ports_equal") is not True or r.get("oracle_ok") is not True:
            ok = False
    print("\n== probes beyond required sizes (timeout = finding) ==")
    for n in probes:
        r = check_size(f"probe-{n}", gen_project(n), timeout=240)
        print(json.dumps({k: v for k, v in r.items()
                          if k not in ("py_times", "ts_times")}, indent=1))
    print("\nSCALE: " + ("ALL REQUIRED SIZES PASS" if ok else "FAILURES — see above"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
