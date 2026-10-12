#!/usr/bin/env python3
"""Conformance harness: prove the TypeScript port (src/fold.ts) equals the
Python referee (poc/fold.py) on every fixture we can throw at both.

Cases come from: the Bout 1 property tests (research/bout-1/test_fold.py),
the stress generators (research/bout-1/stress_fold.py, copied verbatim),
the live PoC record set (poc/scene.py), and hand-built edge cases (cycles,
duplicate ids, unknown kinds, bool/int value collisions, unicode).

The Python referee is the live oracle: expected outputs are computed by
running fold.py at harness time, never hardcoded. A divergence is a bug
in the port, by definition.

Usage: python3 harness.py [--keep]
Exit code 0 iff all cases agree exactly (derive outputs compared with
Python ==; verify_checkpoint booleans compared exactly).
"""
import importlib.util
import itertools
import json
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUB = HERE.parent
# Packaged layout: referee lives at <repo>/referee/fold.py.
# Dev layout (R&D staging): <rd>/poc/fold.py. Take whichever exists.
_candidates = [SUB / "referee" / "fold.py", SUB.parent / "poc" / "fold.py"]
REFEREE = next((p for p in _candidates if p.exists()), _candidates[0])


def load_referee():
    spec = importlib.util.spec_from_file_location("fold_referee", REFEREE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ref = load_referee()
derive, verify = ref.derive, ref.verify_checkpoint


def R(id, parents, kind, effects, **kw):
    d = {"id": id, "parents": parents, "kind": kind, "effects": effects}
    d.update(kw)
    return d


def closure_state(records, at):
    """Derived state of exactly the ancestors of `at` (incl. it), by referee."""
    by = {r["id"]: r for r in records}
    closure, stack = set(), [at]
    while stack:
        n = stack.pop()
        if n in closure:
            continue
        closure.add(n)
        stack.extend(by[n].get("parents") or [])
    return derive([by[r] for r in sorted(closure)])["state"]


def probes_for(records, ats):
    probes = []
    applied = derive([dict(r) for r in records])["order"]
    for at in ats:
        if at not in applied:
            continue
        honest = closure_state(records, at)
        probes.append({"at": at, "state": honest})
        forged = dict(honest)
        if forged:
            k = sorted(forged)[0]
            forged[k] = "FORGED-VALUE"
        else:
            forged = {"forged": 1}
        probes.append({"at": at, "state": forged})
    probes.append({"at": "__missing__", "state": {}})
    probes.append({"at": ats[0] if ats else "__missing__", "state": ["not-a-dict"]})
    return probes


CASES = []


def add(name, records, ats=()):
    recs = [dict(r) for r in records]
    CASES.append({"name": name, "records": recs,
                  "probes": probes_for(recs, list(ats))})


# --- Bout 1 fixtures (from research/bout-1/test_fold.py) -------------------
base = [
    R("a", [], "task", {"x": 1, "shared": "a"}),
    R("b", ["a"], "task", {"y": 2, "shared": "b"}),
    R("c", ["a"], "task", {"z": 3, "shared": "c"}),
    R("w", ["b"], "witness", {"y": 99, "seen": True}),
    R("o", ["c"], "override", {"shared": "o"}, reason="captain said so"),
    R("d", ["b", "o"], "task", {"w2": 1}),
]
add("bout-base", base, ats=["b", "d"])
rng42 = random.Random(42)
for i in range(10):
    perm = [dict(r) for r in base]
    rng42.shuffle(perm)
    add(f"bout-base-perm{i}", perm, ats=["b"])
ghost = base + [R("g", ["missing"], "task", {"x": 5}),
                R("h", ["g"], "task", {"x": 6})]
add("bout-ghost", ghost, ats=["b"])
add("bout-p4", [R("r", [], "task", {"k": "base"}),
                R("t", ["r"], "task", {"k": "taskval"}),
                R("o", ["t"], "override", {"k": "over"}, reason="because"),
                R("s", ["t"], "task", {"k": "sib"}),
                R("g", ["o", "s"], "task", {"other": 1})], ats=["g", "o"])
pair = [R("r", [], "task", {}),
        R("m", ["r"], "task", {"k": "m"}),
        R("n", ["r"], "task", {"k": "n"})]
for i, perm in enumerate(itertools.permutations(pair)):
    add(f"bout-p6-perm{i}", [dict(x) for x in perm], ats=["r"])

# --- Live PoC record set (poc/scene.py) ------------------------------------
poc = [
    R("genesis", [], "task", {"project": "poc-quilt-1", "status": "open"}),
    R("task-a", ["genesis"], "task", {"config.timeout": 30, "feature.cache": "on"}),
    R("task-b", ["genesis"], "task", {"config.timeout": 60, "feature.search": "on"}),
    R("witness-1", ["genesis"], "witness", {"env.oracle_disk_pct": 93}),
    R("override-1", ["task-a", "task-b"], "override", {"config.timeout": 45},
      reason="captain: 45 is the safe value"),
    R("task-d", ["task-b"], "task", {"config.timeout": 99}),
    R("task-c", ["override-1"], "task", {"feature.inherited": True}),
]
add("poc-live", poc, ats=["task-a", "task-c"])

# --- Stress generators (copied verbatim from stress_fold.py) --------------
rng = random.Random(20261011)
KEYS = ["k1", "k2", "k3"]
KINDS = ["task", "task", "task", "witness", "override"]


def rand_dag(n):
    recs = []
    for i in range(n):
        rid = f"r{i:03d}"
        k = rng.randint(0, min(3, i))
        parents = sorted(rng.sample([r["id"] for r in recs], k)) if recs and k else []
        effects = {}
        for key in rng.sample(KEYS, rng.randint(0, 2)):
            effects[key] = rng.randint(0, 5)
        recs.append(R(rid, parents, rng.choice(KINDS), effects))
    return recs


for t in range(200):
    recs = rand_dag(rng.randint(2, 40))
    out = derive([dict(r) for r in recs])
    ats = out["order"][-1:] if out["order"] else []
    add(f"stress-dag{t}", recs, ats=ats)
    for j in range(3):
        perm = [dict(r) for r in recs]
        rng.shuffle(perm)
        CASES.append({"name": f"stress-dag{t}-perm{j}", "records": perm,
                      "probes": []})

# --- S2 dominance cases (from stress_fold.py) ------------------------------
add("dom-chain", [R("a", [], "task", {"k": 1}), R("b", ["a"], "task", {"k": 2}),
                  R("c", ["b"], "task", {"k": 3})], ats=["c"])
add("dom-witness-desc", [R("a", [], "task", {"k": "done"}),
                         R("w", ["a"], "witness", {"k": "seen", "extra": 1})],
    ats=["w"])
add("dom-override", [R("r", [], "task", {"k": "root"}),
                     R("t", ["r"], "task", {"k": "task"}),
                     R("o", ["r"], "override", {"k": "over"})], ats=["o"])
add("dom-twin-content", [R("c1", [], "task", {}),
                         R("x", ["c1"], "task", {"k": "same"}),
                         R("y", ["c1"], "task", {"k": "same"})], ats=["x", "y"])

# --- Edge cases -------------------------------------------------------------
add("edge-empty", [], ats=[])
add("edge-cycle2", [R("a", ["b"], "task", {"k": 1}),
                    R("b", ["a"], "task", {"k": 2}),
                    R("c", [], "task", {"k": 3})], ats=["c"])
add("edge-self-parent", [R("a", ["a"], "task", {"k": 1}),
                         R("b", [], "task", {"k": 2})], ats=["b"])
add("edge-cycle-child", [R("a", ["b"], "task", {"k": 1}),
                         R("b", ["a"], "task", {"k": 2}),
                         R("d", ["a"], "task", {"k": 4})], ats=[])
add("edge-dup-id-diff", [R("a", [], "task", {"k": "first", "shared": 1}),
                         R("a", [], "task", {"k": "second", "shared": 2}),
                         R("b", ["a"], "task", {"k": "b"})], ats=["b"])
add("edge-dup-id-identical", [R("a", [], "task", {"k": "v"}),
                              R("a", [], "task", {"k": "v"}),
                              R("b", ["a"], "task", {"k": "w"})], ats=["b"])
add("edge-dup-id-numeric", [R("a", [], "task", {"k": 10}),
                            R("a", [], "task", {"k": 9}),
                            R("b", ["a"], "task", {"j": 1})], ats=["b"])
add("edge-unknown-kind", [R("a", [], "robot", {"k": "bot"}),
                          R("b", [], "task", {"k": "human"}),
                          R("c", ["a"], "witness", {"k": "seen"})], ats=["b"])
add("edge-no-parents-field", [{"id": "a", "kind": "task", "effects": {"k": 1}},
                              {"id": "b", "parents": ["a"], "kind": "task",
                               "effects": {"k": 2}}], ats=["b"])
add("edge-bool-int-collide", [R("a", [], "task", {"k": 1}),
                              R("b", [], "task", {"k": True}),
                              R("c", [], "task", {"k": 0}),
                              R("d", [], "task", {"k": False}),
                              R("e", [], "task", {"k": "1"})], ats=["a"])
add("edge-floats", [R("a", [], "task", {"k": 1.5}),
                    R("b", [], "task", {"k": 2.25}),
                    R("c", ["a"], "task", {"k": 1.5})], ats=["c"])
add("edge-unicode", [R("café", [], "task", {"kéy": "vå∫"}),
                     R("b", ["café"], "task", {"kéy": "other"})], ats=["b"])
add("edge-witness-only", [R("w1", [], "witness", {"k": "s1"}),
                          R("w2", ["w1"], "witness", {"k": "s2"})], ats=["w2"])
add("edge-deep-chain", [R(f"c{i:03d}", [f"c{i-1:03d}"] if i else [], "task",
                          {"k": i}) for i in range(60)], ats=["c059"])
add("edge-wide-hotkey", [R("root", [], "task", {})] +
    [R(f"h{i:03d}", ["root"], "task", {"hot": i}) for i in range(40)],
    ats=["root"])


def main():
    cases_path = HERE / "cases.json"
    results_path = HERE / "results.ts.json"
    cases_path.write_text(json.dumps(CASES))
    print(f"harness: {len(CASES)} cases, "
          f"{sum(len(c['probes']) for c in CASES)} checkpoint probes")

    proc = subprocess.run(
        ["node", str(HERE / "runner.ts"), str(cases_path), str(results_path)],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print("RUNNER FAILED:", proc.stdout, proc.stderr)
        return 2
    print(proc.stdout.strip())

    ts_results = {r["name"]: r for r in json.loads(results_path.read_text())}
    divergences = []
    for case in CASES:
        name = case["name"]
        exp_derive = derive([dict(r) for r in case["records"]])
        exp_verify = [verify([dict(r) for r in case["records"]],
                             {"at": p["at"], "state": p["state"]})
                      for p in case["probes"]]
        got = ts_results.get(name)
        if got is None:
            divergences.append((name, "missing from TS results"))
            continue
        if got["derive"] != exp_derive:
            divergences.append((name, "derive differs"))
            for field in ("state", "order", "conflicts", "orphans"):
                if got["derive"][field] != exp_derive[field]:
                    print(f"  [{name}] field {field}:")
                    print(f"    py: {json.dumps(exp_derive[field], sort_keys=True)[:400]}")
                    print(f"    ts: {json.dumps(got['derive'][field], sort_keys=True)[:400]}")
        if got["verify"] != exp_verify:
            divergences.append((name, f"verify differs: py={exp_verify} ts={got['verify']}"))

    total_probes = sum(len(c["probes"]) for c in CASES)
    if divergences:
        print(f"\nDIVERGENCES: {len(divergences)} case(s):")
        for name, why in divergences[:20]:
            print(f"  - {name}: {why}")
        return 1
    print(f"\nCONFORMANCE: {len(CASES)}/{len(CASES)} cases identical "
          f"(derive + {total_probes} checkpoint probes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
