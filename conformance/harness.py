#!/usr/bin/env python3
"""Conformance harness: prove the TypeScript port (src/fold.ts) equals the
Python referee (referee/fold.py) on every fixture we can throw at both.

Cases come from: property fixtures over small hand-built DAGs, randomized
stress generators (seeded), the live record sets (conformance/live-records.json),
and hand-built edge cases (cycles, duplicate ids, unknown kinds, bool/int value
collisions, unicode/non-BMP ids, non-JS-safe integers, checkpoint forgery).
A separate FRONTIER family (set-form checkpoints, docs/FRONTIER-CHECKPOINTS.md)
is compared and counted apart so the classic totals stay literally unchanged;
a FORMAT-V2 family (records born with digests, docs/FORMAT-V2.md) is counted
apart the same way, and additionally sweeps the whole corpus for the
born-ignored-by-fold law and root-assembly equivalence;
every case in both families also has its auto-frontier checkpoint checked
(delta == full derive, covered set == applied set, cross-port identical),
plus tail-dup scenarios over that checkpoint: a tail record identical to a
covered record is a duplicate serving (delta engages, equals the full
derive); a divergent copy under a covered id is a re-version (delta None).

The Python referee is the live oracle: expected outputs are computed by running
fold.py at harness time, never hardcoded. A divergence is a bug in the port, by
definition. Beyond the port differential, the harness also runs three property
checks in the referee: a brute-force maximality oracle with parents-order
invariance, the non-finite transport pin, and live-golden byte-identity.

Usage: python3 harness.py [--keep]
Exit code 0 iff all cases agree exactly (derive outputs compared with
Python ==; verify_checkpoint booleans compared exactly) and every property
check passes.
"""
import hashlib
import importlib.util
import itertools
import json
import random
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUB = HERE.parent
# Packaged layout: referee lives at <repo>/referee/fold.py. An alternate
# layout keeps it one level up under poc/. Take whichever exists.
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


# --- Property fixtures: small hand-built DAGs exercising each rule ---------
base = [
    R("a", [], "task", {"x": 1, "shared": "a"}),
    R("b", ["a"], "task", {"y": 2, "shared": "b"}),
    R("c", ["a"], "task", {"z": 3, "shared": "c"}),
    R("w", ["b"], "witness", {"y": 99, "seen": True}),
    R("o", ["c"], "override", {"shared": "o"}, reason="captain said so"),
    R("d", ["b", "o"], "task", {"w2": 1}),
]
add("prop-base", base, ats=["b", "d"])
rng42 = random.Random(42)
for i in range(10):
    perm = [dict(r) for r in base]
    rng42.shuffle(perm)
    add(f"prop-base-perm{i}", perm, ats=["b"])
ghost = base + [R("g", ["missing"], "task", {"x": 5}),
                R("h", ["g"], "task", {"x": 6})]
add("prop-ghost", ghost, ats=["b"])
add("prop-override-graft", [R("r", [], "task", {"k": "base"}),
                            R("t", ["r"], "task", {"k": "taskval"}),
                            R("o", ["t"], "override", {"k": "over"}, reason="because"),
                            R("s", ["t"], "task", {"k": "sib"}),
                            R("g", ["o", "s"], "task", {"other": 1})], ats=["g", "o"])
pair = [R("r", [], "task", {}),
        R("m", ["r"], "task", {"k": "m"}),
        R("n", ["r"], "task", {"k": "n"})]
for i, perm in enumerate(itertools.permutations(pair)):
    add(f"prop-tiebreak-perm{i}", [dict(x) for x in perm], ats=["r"])

# --- Live record set (the poc-quilt-1 scene) -------------------------------
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

# --- Witness-freshness cases (exercise witness_freshness / witnessFreshness).
# The harness computes freshness for EVERY case above too (poc-live,
# prop-base, dom-witness-desc, edge-witness-only, the stress DAGs); these add
# hand-checkable shapes with known recordsBehind. Each is also compared
# Python-vs-TS, so a port divergence fails here exactly as a derive one would.
# wf-linear: genesis -> w(witness) -> t1 -> t2 -> t3. Applied=5; w's past is
# {genesis,w}=2, so recordsBehind(w)=3.
add("wf-linear", [R("genesis", [], "task", {"k": 0}),
                  R("w", ["genesis"], "witness", {"seen": 1}),
                  R("t1", ["w"], "task", {"k": 1}),
                  R("t2", ["t1"], "task", {"k": 2}),
                  R("t3", ["t2"], "task", {"k": 3})], ats=["w", "t3"])
# wf-two-witnesses: an early witness and a late one on one chain.
# Applied=6. w-early past {g,w-early}=2 -> behind 4. w-late past
# {g,a,b,w-late}=4 -> behind 2.
add("wf-two-witnesses", [R("g", [], "task", {"k": 0}),
                         R("w-early", ["g"], "witness", {"e": 1}),
                         R("a", ["g"], "task", {"k": 1}),
                         R("b", ["a"], "task", {"k": 2}),
                         R("w-late", ["b"], "witness", {"l": 1}),
                         R("c", ["b"], "task", {"k": 3})], ats=["w-early", "w-late"])
# wf-current: a witness at the single current frontier sees everything.
# Applied=3, w's past {g,a,w}=3 -> recordsBehind 0.
add("wf-current", [R("g", [], "task", {"k": 0}),
                   R("a", ["g"], "task", {"k": 1}),
                   R("w", ["a"], "witness", {"seen": 1})], ats=["w"])
# wf-orphan-witness: an orphaned witness is not applied, so it is absent from
# freshness (as it is from state) — exactly the derive() exclusion.
add("wf-orphan-witness", [R("g", [], "task", {"k": 0}),
                          R("w", ["ghost"], "witness", {"seen": 1}),
                          R("t", ["g"], "task", {"k": 1})], ats=["g"])

# --- ADDENDUM #3: ancestry-reduction (exact maximality) -------------------
# The witness-dominance repro the referee confirmed. Witnesses r->a->b all set
# key k; c (parents [b,a] / [a,b]) also sets k. c dominates r,a,b, so exact
# maximality is {c} and the winner is c REGARDLESS of c's parents-array order.
# The pre-fix fold marked only the first DFS-reached ancestor, so [b,a] left
# {b,c} contested and the witness tie-break shipped b — a parents-order flip.
add("ancestry-witness-flip-ba", [R("r", [], "witness", {"k": "r"}),
                                 R("a", ["r"], "witness", {"k": "a"}),
                                 R("b", ["a"], "witness", {"k": "b"}),
                                 R("c", ["b", "a"], "witness", {"k": "c"})],
    ats=["c"])
add("ancestry-witness-flip-ab", [R("r", [], "witness", {"k": "r"}),
                                 R("a", ["r"], "witness", {"k": "a"}),
                                 R("b", ["a"], "witness", {"k": "b"}),
                                 R("c", ["a", "b"], "witness", {"k": "c"})],
    ats=["c"])
# A multi-ancestor task diamond, both parent orders, to pin maximality in the
# task pool as well (masked pre-fix, but now exact and order-independent).
add("ancestry-task-diamond-ba", [R("a", [], "task", {"k": "a"}),
                                 R("b", ["a"], "task", {"k": "b"}),
                                 R("c", ["a"], "task", {"k": "c"}),
                                 R("d", ["b", "c"], "task", {"k": "d"})], ats=["d"])
add("ancestry-task-diamond-ab", [R("a", [], "task", {"k": "a"}),
                                 R("b", ["a"], "task", {"k": "b"}),
                                 R("c", ["a"], "task", {"k": "c"}),
                                 R("d", ["c", "b"], "task", {"k": "d"})], ats=["d"])

# --- ADDENDUM #1: id ordering by Unicode code point -----------------------
# "a￿" (BMP) vs "a\U0001F600" (astral). By code point the BMP id is
# smaller (0xFFFF < 0x1F600) — Python's native order. The pre-fix TS compared
# UTF-16 code units, where the astral id's leading high surrogate (0xD83D) is
# LESS than 0xFFFF, so TS elected the opposite winner. Both now agree: the
# contested key's winner is the lexicographically (code-point) smaller id.
add("edge-nonbmp-id", [R("a￿", [], "task", {"k": "bmp"}),
                       R("a\U0001F600", [], "task", {"k": "astral"})], ats=[])
# A non-BMP effect KEY (object key order in the canonical form) — exercises the
# code-point key sort in pyCanonical too.
add("edge-nonbmp-key", [R("x", [], "task", {"z": 1, "a\U0001F600": 2}),
                        R("y", [], "task", {"a￿": 3})], ats=[])

# --- ADDENDUM #2: numeric domain (IEEE-754 float64) -----------------------
# Ints above 2**53-1 are not JS-safe. The Python referee float64-canonicalises
# them at ingest so both sides fold the same number. 2**53+1 collapses to
# 2**53; 2**53+3 collapses to 2**53+4 (nearest even double) — distinct, so a
# conflict. These pin agreement in both the winning value and conflict detection.
add("edge-bigint-contested", [R("a", [], "task", {"k": 2**53 + 1}),
                              R("b", [], "task", {"k": 2**53 + 3})], ats=[])
add("edge-bigint-winner", [R("base", [], "task", {}),
                           R("big", ["base"], "task", {"k": 2**53 + 1}),
                           R("small", ["base"], "task", {"k": 7})], ats=["big"])
# Two different large ints that collapse to the SAME double (2^53 and 2^53+1
# both round to 9007199254740992) are NOT a conflict in either port.
add("edge-bigint-collapse-equal", [R("a", [], "task", {"k": 2**53}),
                                   R("b", [], "task", {"k": 2**53 + 1})], ats=[])

# --- ADDENDUM #4 P1: checkpoint comparison under the float64 domain --------
# A checkpoint asserting k=2^53+1 against a state of k=2^53. Pre-fix, TS
# accepted (JSON.parse rounds 2^53+1 to 2^53) while the Python referee rejected
# (exact int 2^53+1 != 2^53) — a cross-port split, and TS accepting a value it
# could not represent. Under the declared IEEE-754 domain both values denote
# the SAME double (9007199254740992), so verify_checkpoint canonicalises the
# checkpoint's declared state the same way derive() canonicalises records, and
# BOTH ports now agree. The harness compares the verify booleans, so a port
# split fails here. (The probe is added via a dedicated records+probe case.)
CASES.append({
    "name": "ckpt-bigint-domain",
    "records": [R("a", [], "task", {"k": 2**53})],
    "probes": [
        # same number under the domain -> accepted by both ports
        {"at": "a", "state": {"k": 2**53 + 1}},
        # a genuinely DISTINCT double (2^53+2 = 9007199254740994) -> rejected by both
        {"at": "a", "state": {"k": 2**53 + 2}},
    ],
})

# --- Live record sets: the fixed fold must reproduce the DEPLOYED golden ---
# conformance/live-records.json holds the exact records /api/records returns
# for poc-quilt-1 (8) and poc-scale-1 (62); the byte-identity assertion lives
# in main() against conformance/live-golden.json. Adding them as cases also
# runs the Python-vs-TS differential on the real data.
_LIVE = json.loads((HERE / "live-records.json").read_text())
for _proj, _recs in _LIVE.items():
    add(f"live-{_proj}", _recs, ats=[])

# --- SPIKE: checkpoint v2 — closure-root pinning --------------------------
# Each case pins a v2 probe (state + closureRoot) and the harness compares the
# three-way verify result Python-vs-TS, plus every applied id's digest (see
# main()'s `roots` comparison). The expected roots/states are computed here by
# the referee so a divergence in either port shows up as a verify mismatch.

def _v2_case(name, records, at, state, closure_root):
    CASES.append({"name": name, "records": [dict(r) for r in records],
                  "probes": [{"at": at, "state": state, "closureRoot": closure_root}]})

# (a) VERIFIED — root pinned over the very set being verified.
_Sa = [R("g", [], "task", {"k": 1}),
       R("a", ["g"], "task", {"k": 1}),
       R("c", ["a"], "witness", {})]
_root_a = ref.checkpoint_root([dict(r) for r in _Sa], "c")
_state_a = closure_state(_Sa, "c")
_v2_case("ckpt-v2-verified", _Sa, "c", _state_a, _root_a)

# (b) STATE-ONLY — a later-arriving ancestor grows at's closure; the derived
# state is unchanged, but the pinned root (taken over the smaller S0) no longer
# matches. Never a silent VERIFIED. S0: closure(R) = {g, R}. S1: R re-versioned
# to also parent m, so closure(R) = {g, m, R}; m (task, k=1) keeps state {k:1}.
_S0 = [R("g", [], "task", {"k": 1}), R("R", ["g"], "witness", {})]
_S1 = [R("g", [], "task", {"k": 1}),
       R("m", ["g"], "task", {"k": 1}),
       R("R", ["g", "m"], "witness", {})]
_root_b0 = ref.checkpoint_root([dict(r) for r in _S0], "R")   # pinned over S0
_state_b = closure_state(_S1, "R")                            # {k:1} in S1 too
_v2_case("ckpt-v2-state-only-grown", _S1, "R", _state_b, _root_b0)

# (c) MISMATCH — the declared state is wrong (root present).
_v2_case("ckpt-v2-mismatch-state", _Sa, "c", {"k": 999}, _root_a)

# (d) tampered root — state matches but the pinned root is forged; must be
# STATE-ONLY (not VERIFIED), never silently accepted.
_v2_case("ckpt-v2-tampered-root", _Sa, "c", _state_a,
         "0000000000000000000000000000000000000000000000000000000000000000")

# (e) the frozen live project: VERIFIED at task-c over the exact live set, and
# a regression pin on the digest value stated in the PR.
_POC = _LIVE["poc-quilt-1"]
_root_poc = ref.checkpoint_root([dict(r) for r in _POC], "task-c")
_v2_case("ckpt-v2-live-poc-quilt-1", _POC, "task-c",
         closure_state(_POC, "task-c"), _root_poc)
# Whole-content leaf (FIX 3): the closure root over task-c's closure. Changed
# from the fold-relevant-only spike value (891aa330…) because the leaf now
# covers each record's whole canonical content (reason included).
POC_QUILT1_TASK_C_ROOT = "60effe23970d8a5cc837636c44685502361288c1f00577e9addfa10cbd4fc91d"

# --- FIX 1: leaf-number canonical form, cross-port --------------------------
# Record effects carrying the float values that split Python's json.dumps from
# a naive JS String(): exponent-range floats (Python zero-pads the exponent —
# 1e-05, 2.5e-07 — where JS String gives 0.00001 / 2.5e-7), signed zero
# (-0.0 → "0" in the leaf, not "-0.0"), and a small-exponent float (1e-100).
# main()'s `roots` check compares every applied id's closure-root digest
# Python-vs-TS, so the corpus now SAMPLES these splitters.
# (Integral doubles in [1e16, 1e21) also split — Python's exact int() vs JS's
# shortest round-trip — and the leaf fix pins them to BigInt exact digits; that
# path is proven by the 40k-value cross-port fuzz quoted in the PR rather than a
# state-visible case here, since such a value in STATE exposes an unrelated JSON
# round-trip subtlety outside this change's scope.)
add("ckpt-v2-leaf-floats", [
    R("g", [], "task", {"tiny": 1e-5, "tinier": 2.5e-7, "negzero": -0.0}),
    R("a", ["g"], "task", {"frac": 1.5}),
    R("c", ["a"], "witness", {"sci": 1e-100}),
], ats=["g", "a", "c"])

# --- FIX 3: whole-record leaf catches a reason-only re-version --------------
# The leaf now covers `reason`, which the fold ignores, so re-versioning a
# closure member's reason leaves derived state unchanged but changes the
# closure root. The result is STATE-ONLY (the member re-versioned), never the
# silent VERIFIED the fold-relevant-only spike leaf gave. A state-affecting
# tamper stays MISMATCH (see ckpt-v2-mismatch-state).
_Sr = [R("g", [], "task", {"k": 1}),
       R("o", ["g"], "override", {"k": 2}, reason="captain: original reason"),
       R("t", ["o"], "task", {"j": 3})]
_Sr_tampered = [R("g", [], "task", {"k": 1}),
                R("o", ["g"], "override", {"k": 2}, reason="FORGED: different reason"),
                R("t", ["o"], "task", {"j": 3})]
_root_r = ref.checkpoint_root([dict(r) for r in _Sr], "t")
_state_r = closure_state(_Sr, "t")
_v2_case("ckpt-v2-reason-verified", _Sr, "t", _state_r, _root_r)
_v2_case("ckpt-v2-reason-tamper", _Sr_tampered, "t", _state_r, _root_r)

# Explicit three-way result pins for the semantic cases (the harness otherwise
# only proves the two ports AGREE; these pin WHICH answer is correct). Checked
# in main() against the referee.
CKPT_V2_EXPECTED = {
    "ckpt-v2-verified": "VERIFIED",
    "ckpt-v2-state-only-grown": "STATE-ONLY",
    "ckpt-v2-mismatch-state": "MISMATCH",
    "ckpt-v2-tampered-root": "STATE-ONLY",
    "ckpt-v2-reason-verified": "VERIFIED",
    "ckpt-v2-reason-tamper": "STATE-ONLY",
}

# --- FRONTIER CHECKPOINTS (docs/FRONTIER-CHECKPOINTS.md) -------------------
# The set-form generalisation of checkpoint v2: `at` may be a LIST of
# frontier ids pinning the union of their ancestor closures, root computed
# by the identical construction. These cases form a SEPARATE family so the
# classic totals stay literally unchanged; the frontier family is compared
# cross-port and counted on its own line. Independently, main() checks the
# AUTO frontier checkpoint of EVERY case in both families (delta == full,
# covered set == applied set, cross-port identical).
FRONTIER_CASES = []


def _f_case(name, records, probes):
    FRONTIER_CASES.append({"name": name,
                           "records": [dict(r) for r in records],
                           "probes": probes})


# A wide, shallow DAG — three strands off one genesis, a frontier of three.
_F = [
    R("g", [], "task", {"project": "frontier-demo"}),
    R("a1", ["g"], "task", {"x": 1}),
    R("ov", ["a1"], "override", {"x": 2}, reason="captain: x is 2 on strand a"),
    R("a2", ["ov"], "task", {"y": "a"}),
    R("b1", ["g"], "task", {"z": 1}),
    R("b2", ["b1"], "task", {"z": 2}),
    R("w", ["g"], "witness", {"seen": True}),
]
_F_MEMBERS = ["a2", "b2", "w"]  # the maximal applied records, code-point order
_F_ROOT = ref.checkpoint_root([dict(r) for r in _F], _F_MEMBERS)
_F_STATE = derive([dict(r) for r in _F])["state"]  # C == applied: the full state
_F_PROBE = {"at": _F_MEMBERS, "state": _F_STATE, "closureRoot": _F_ROOT}

# (a) VERIFIED over the very set; the members are listed UNSORTED in the
# probe — `at` is a set, its order in the file carries no meaning.
_f_case("frontier-verified", _F,
        [{"at": ["b2", "w", "a2"], "state": _F_STATE, "closureRoot": _F_ROOT}])
# (b) Grown with descendants on top of the frontier: descendants never enter
# a member's closure, so C, its root, and its derived state are unchanged —
# still VERIFIED (the normal case for a frontier checkpoint).
_F_GROWN = _F + [R("a3", ["a2"], "task", {"y": "a3"}),
                 R("b3", ["b2"], "task", {}),
                 R("w2", ["w"], "witness", {"seen2": True})]
_f_case("frontier-growth-verified", _F_GROWN, [dict(_F_PROBE)])
# (c) A record OUTSIDE C (an orphan beside the covered set) does not move the
# verdict: verification ranges over C only and never claimed the rest.
_F_BESIDE = _F + [R("orph", ["ghost"], "task", {"lost": 1})]
_f_case("frontier-orphan-beside", _F_BESIDE, [dict(_F_PROBE)])
# (d) A frontier member removed: C cannot be reproduced -> MISMATCH.
_f_case("frontier-member-removed", [r for r in _F if r["id"] != "b2"],
        [dict(_F_PROBE)])
# (e) A covered ancestor removed: a member's closure is incomplete in the
# presented set -> MISMATCH.
_f_case("frontier-ancestor-removed", [r for r in _F if r["id"] != "a1"],
        [dict(_F_PROBE)])
# (f) A reason-only re-version of a covered record: the whole-record leaf
# moves, the fold ignores `reason`, derive(C).state is unchanged -> STATE-ONLY.
_F_REASON = [dict(r, reason="FORGED: a different reason") if r["id"] == "ov"
             else dict(r) for r in _F]
_f_case("frontier-reason-only", _F_REASON, [dict(_F_PROBE)])
# (g) A covered record's effects tampered where the value REACHES the state
# (b2 wins z): derive(C).state differs -> MISMATCH.
_F_TAMPER = [dict(r, effects={"z": 999}) if r["id"] == "b2" else dict(r)
             for r in _F]
_f_case("frontier-covered-tamper", _F_TAMPER, [dict(_F_PROBE)])
# (g2) The same tamper on a DOMINATED record (b1's z never reaches the state
# — b2 supersedes it) is state-invisible: the leaf moves, the fold's answer
# does not -> STATE-ONLY, exactly like a reason-only re-version.
_F_DOM = [dict(r, effects={"z": 999}) if r["id"] == "b1" else dict(r)
          for r in _F]
_f_case("frontier-dominated-tamper", _F_DOM, [dict(_F_PROBE)])
# (h) Singleton equivalence at the verify surface: an array of ONE is the
# string form — same root, same verdicts (VERIFIED here; STATE-ONLY against
# a forged root). Builder-level equivalence is pinned in main().
_f_case("frontier-singleton-verified", _Sa,
        [{"at": ["c"], "state": _state_a, "closureRoot": _root_a}])
_f_case("frontier-singleton-state-only", _Sa,
        [{"at": ["c"], "state": _state_a,
          "closureRoot":
          "0000000000000000000000000000000000000000000000000000000000000000"}])
# (i) Malformed arrays are rejected (MISMATCH under a root): empty member
# list, duplicated members, non-string members.
_f_case("frontier-malformed-empty", _Sa,
        [{"at": [], "state": _state_a, "closureRoot": _root_a}])
_f_case("frontier-malformed-dup", _Sa,
        [{"at": ["c", "c"], "state": _state_a, "closureRoot": _root_a}])
_f_case("frontier-malformed-nonstr", _Sa,
        [{"at": [123], "state": _state_a, "closureRoot": _root_a}])
# (j) v1 (no closureRoot) with the array form: the plain bool over C.
_f_case("frontier-v1-array-true", _F,
        [{"at": _F_MEMBERS, "state": _F_STATE}])
_f_case("frontier-v1-array-false", _F,
        [{"at": _F_MEMBERS, "state": {"forged": 1}}])
# (k) The frozen scale fixture at its true frontier: four members covering
# 62/62, root pinned literally below (POC_SCALE1_FRONTIER_ROOT) and
# recomputed from the construction in main().
_SCALE = _LIVE["poc-scale-1"]
_f_case("frontier-live-scale", _SCALE,
        [{"at": ["t40", "t50", "t60", "witness-1"],
          "state": derive([dict(r) for r in _SCALE])["state"],
          "closureRoot": ref.checkpoint_root(
              [dict(r) for r in _SCALE], ["t40", "t50", "t60", "witness-1"])}])
# The frontier closure root over poc-scale-1's frontier {t40, t50, t60,
# witness-1}: the union of the four closures is all 62 records.
POC_SCALE1_FRONTIER_ROOT = \
    "3a0919f1cee5e312f5913941116ead057d2b7ad8df998a09025157affaad99ac"

# Explicit three-way / bool result pins for the frontier cases (the harness
# otherwise only proves the two ports AGREE; these pin WHICH answer is
# correct). Checked in main() against the referee.
FRONTIER_EXPECTED = {
    "frontier-verified": "VERIFIED",
    "frontier-growth-verified": "VERIFIED",
    "frontier-orphan-beside": "VERIFIED",
    "frontier-member-removed": "MISMATCH",
    "frontier-ancestor-removed": "MISMATCH",
    "frontier-reason-only": "STATE-ONLY",
    "frontier-covered-tamper": "MISMATCH",
    "frontier-dominated-tamper": "STATE-ONLY",
    "frontier-singleton-verified": "VERIFIED",
    "frontier-singleton-state-only": "STATE-ONLY",
    "frontier-malformed-empty": "MISMATCH",
    "frontier-malformed-dup": "MISMATCH",
    "frontier-malformed-nonstr": "MISMATCH",
    "frontier-v1-array-true": True,
    "frontier-v1-array-false": False,
    "frontier-live-scale": "VERIFIED",
}

# --- FORMAT V2: records born with digests (docs/FORMAT-V2.md) --------------
# A record may carry a sibling `digest` field: "sha256:" + the checkpoint-v2
# record leaf of its content, written at birth by the runner. The field is a
# claim ABOUT the record, never an input to it: THE FOLD NEVER READS
# `digest`, so derivation, verdicts, and roots are functions of the five
# content fields alone. These cases form a SEPARATE family (like FRONTIER)
# so the classic totals stay literally unchanged; main() additionally runs
# the family-2 and family-8 sweeps over the WHOLE corpus (every classic and
# frontier case), and the worker-level pins (warning shape, the §3.3 dup
# table, degrade-and-warn) live in tests/format-v2.mjs.

FORMAT_V2_CASES = []


def _fmt(name, records, probes=()):
    FORMAT_V2_CASES.append({"name": name,
                            "records": [dict(r) for r in records],
                            "probes": list(probes)})


def _born(records):
    """Copies carrying CORRECT born digests, computed by the referee leaf —
    the same function the runner's writer uses (the dialect is inherited)."""
    out = []
    for r in records:
        r = dict(r)
        r["digest"] = "sha256:" + ref._record_leaf(r)
        out.append(r)
    return out


def _born_some(records, ids):
    """Copies where only the named records carry (correct) born digests."""
    out = []
    for r in records:
        r = dict(r)
        if r["id"] in ids:
            r["digest"] = "sha256:" + ref._record_leaf(r)
        out.append(r)
    return out


def _v2(records, mode, seed=0):
    """Copies carrying digests of a chosen untrustworthiness."""
    rng_v = random.Random(seed)
    out = []
    for r in records:
        r = dict(r)
        if mode == "garbage":        # well-formed hex, wrong for everything
            r["digest"] = "sha256:" + "0" * 64
        elif mode == "random-wrong":  # well-formed, wrong per record
            r["digest"] = "sha256:" + "".join(
                rng_v.choice("0123456789abcdef") for _ in range(64))
        elif mode == "malformed":     # not claims at all
            r["digest"] = rng_v.choice(["garbage", "", "sha256:xyz", 42, None])
        out.append(r)
    return out


_WELL_FORMED = re.compile(r"^sha256:[0-9a-f]{64}$")


def _assembled_root(records, at):
    """Test-only oracle for the design's root assembly: the closure root
    computed taking each record's leaf from its BORN DIGEST where one is
    present and well-formed, recomputing otherwise. Production verify never
    does this — it always recomputes. The sweep in main() proves assembly
    and recompute agree wherever the digests are honest, and disagree
    observably where one lies, which is what makes the recompute-only law
    safe to keep."""
    idx = ref._index([ref._canonical_record(r) for r in records])
    if at not in idx:
        return None
    parents = {rid: ref._parents(idx[rid]) for rid in idx}
    closure = ref._closure_ids(at, idx, parents)
    if closure is None:
        return None
    lines = []
    for rid in sorted(closure):
        d = idx[rid].get("digest")
        leaf = (d[len("sha256:"):]
                if isinstance(d, str) and _WELL_FORMED.match(d)
                else ref._record_leaf(idx[rid]))
        lines.append(f"{rid}:{leaf}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


# Family 2 (born-ignored-by-fold), cross-port spot entries — the exhaustive
# sweep over the whole corpus is in main(). Twins included: duplicate-id
# resolution must follow content alone, with digests attached.
_fmt("fmt-ignored-garbage-poc", _v2(poc, "garbage"))
_fmt("fmt-ignored-random-poc", _v2(poc, "random-wrong", seed=7))
_fmt("fmt-ignored-malformed-base", _v2(base, "malformed", seed=3))
_fmt("fmt-ignored-garbage-scale", _v2(_LIVE["poc-scale-1"], "garbage"))
_dupdiff = [R("a", [], "task", {"k": "first", "shared": 1}),
            R("a", [], "task", {"k": "second", "shared": 2}),
            R("b", ["a"], "task", {"k": "b"})]
_fmt("fmt-ignored-twins-garbage", _v2(_dupdiff, "garbage"))

# Family 1 (born-correct): honest v2 probes over born sets — the state and
# root of the five content fields, reached identically through v2 records.
_poc_born = _born(poc)
_fmt("fmt-born-correct-poc", _poc_born,
     [{"at": "task-c", "state": closure_state(poc, "task-c"),
       "closureRoot": ref.checkpoint_root(_poc_born, "task-c")}])
_floats = [R("g", [], "task", {"tiny": 1e-5, "tinier": 2.5e-7, "negzero": -0.0}),
           R("a", ["g"], "task", {"frac": 1.5}),
           R("c", ["a"], "witness", {"sci": 1e-100})]
_fmt("fmt-born-correct-floats", _born(_floats),
     [{"at": "c", "state": closure_state(_floats, "c"),
       "closureRoot": ref.checkpoint_root(_born(_floats), "c")}])
_fmt("fmt-born-correct-twins", _born(_dupdiff))
_dupnum = [R("a", [], "task", {"k": 10}), R("a", [], "task", {"k": 9}),
           R("b", ["a"], "task", {"j": 1})]
_fmt("fmt-born-correct-twins-numeric", _born(_dupnum))
# The frozen live project, born: the pinned poc-quilt-1@task-c root must
# not move (family 8's headline pin, at the verify surface).
_fmt("fmt-born-live-quilt", _born(_LIVE["poc-quilt-1"]),
     [{"at": "task-c", "state": closure_state(_LIVE["poc-quilt-1"], "task-c"),
       "closureRoot": POC_QUILT1_TASK_C_ROOT}])

# Family 3 (born-stale): born correct, then content edited without
# recompute. The checkpoint was pinned over the ORIGINAL set; against the
# edited set it must answer exactly as it answers the stripped edited set —
# the digest changes no verdict, because no verdict reads it.
_stale0 = [R("g", [], "task", {"k": 1}),
           R("a", ["g"], "task", {"k": 1}),
           R("c", ["a"], "witness", {})]
_stale_root = ref.checkpoint_root(_born(_stale0), "c")
_stale_state = closure_state(_stale0, "c")
_stale_dom = _born(_stale0)
_stale_dom[0] = dict(_stale_dom[0], effects={"k": 7})  # fold-invisible edit
_fmt("fmt-born-stale-dominated", _stale_dom,
     [{"at": "c", "state": _stale_state, "closureRoot": _stale_root}])
_stale_vis = _born(_stale0)
_stale_vis[1] = dict(_stale_vis[1], effects={"k": 9})  # state-visible edit
_fmt("fmt-born-stale-state", _stale_vis,
     [{"at": "c", "state": _stale_state, "closureRoot": _stale_root}])

# Family 4 (born-forged) at the fold level — fmt-dup-copied-digest: record
# x carries a digest copied from record a over different content. Verify
# recomputes, so the honest checkpoint still verifies VERIFIED and the
# root equals the stripped set's; the forgery is caught by the assembly
# oracle in main(), whose assembled root diverges observably.
_forge = [R("g", [], "task", {"k": 1}),
          R("a", ["g"], "task", {"k": 2}),
          R("x", ["g"], "task", {"k": 3})]
_forge_born = _born(_forge)
_forged = [dict(r, digest=_forge_born[1]["digest"]) if r["id"] == "x" else r
           for r in _forge_born]
_fmt("fmt-dup-copied-digest", _forged,
     [{"at": "x", "state": closure_state(_forge, "x"),
       "closureRoot": ref.checkpoint_root(_forged, "x")}])

# Family 5 (mixed v1+v2 closures, both parent directions): a checkpoint
# taken when only part of the set is v2 verifies VERIFIED, and its root is
# the root of the all-v1 and all-v2 writings too (pinned in main()).
_mix_chain = [R("g", [], "task", {"k": 1}),
              R("p", ["g"], "task", {"k": 2}),
              R("c", ["p"], "task", {"k": 3}),
              R("gc", ["c"], "task", {"k": 4})]
_mix_state = closure_state(_mix_chain, "gc")
_mix_a = _born_some(_mix_chain, {"g", "p"})       # v2 ancestors, v1 below
_fmt("fmt-mixed-v2-ancestors", _mix_a,
     [{"at": "gc", "state": _mix_state,
       "closureRoot": ref.checkpoint_root(_mix_a, "gc")}])
_mix_b = _born_some(_mix_chain, {"c", "gc"})      # v1 parent, v2 below
_fmt("fmt-mixed-v2-descendants", _mix_b,
     [{"at": "gc", "state": _mix_state,
       "closureRoot": ref.checkpoint_root(_mix_b, "gc")}])
_mix_c = _born_some(_mix_chain, {"g", "c"})       # alternating generations
_fmt("fmt-mixed-alternating", _mix_c,
     [{"at": "gc", "state": _mix_state,
       "closureRoot": ref.checkpoint_root(_mix_c, "gc")}])

# Family 7 (twin-naming): two records share an id, each carrying its OWN
# correct digest. Names follow contents — the root and the derivation are
# the set's, whichever twin the canonical index keeps — and exchanging the
# twins' digests (both claims now wrong) moves nothing.
_twinK = R("twin", [], "task", {"k": "K"})
_twinL = R("twin", [], "task", {"k": "L"})
_twinKid = R("kid", ["twin"], "task", {"j": 1})
_twins_born = _born([_twinK, _twinL, _twinKid])
_fmt("fmt-twin-naming", _twins_born,
     [{"at": "kid",
       "state": closure_state([_twinK, _twinL, _twinKid], "kid"),
       "closureRoot": ref.checkpoint_root(_twins_born, "kid")}])
_fmt("fmt-twin-naming-swapped",
     [dict(_twins_born[0], digest=_twins_born[1]["digest"]),
      dict(_twins_born[1], digest=_twins_born[0]["digest"]),
      _twins_born[2]])

# Frontier checkpoints over born records: the array form is as indifferent
# to the field as everything else.
_F_born = _born(_F)
_fmt("fmt-frontier-born", _F_born, [dict(_F_PROBE)])

# Explicit verdict pins for the semantic format-v2 cases (the harness
# otherwise only proves the two ports AGREE; these pin WHICH answer is
# correct, and that the stripped writing of the same content answers the
# same). Checked in main() against the referee.
FORMAT_V2_EXPECTED = {
    "fmt-born-correct-poc": "VERIFIED",
    "fmt-born-correct-floats": "VERIFIED",
    "fmt-born-live-quilt": "VERIFIED",
    "fmt-born-stale-dominated": "STATE-ONLY",
    "fmt-born-stale-state": "MISMATCH",
    "fmt-dup-copied-digest": "VERIFIED",
    "fmt-mixed-v2-ancestors": "VERIFIED",
    "fmt-mixed-v2-descendants": "VERIFIED",
    "fmt-mixed-alternating": "VERIFIED",
    "fmt-twin-naming": "VERIFIED",
    "fmt-frontier-born": "VERIFIED",
}


def _ancestors(start, parents, restrict=None):
    """Full ancestor set of `start` (optionally restricted to `restrict`)."""
    seen, stack = set(), list(parents.get(start, []))
    while stack:
        n = stack.pop()
        if n in seen or (restrict is not None and n not in restrict):
            continue
        seen.add(n)
        stack.extend(parents.get(n, []))
    return seen


def oracle_winners(records):
    """Independent winner selection: BRUTE maximality (full ancestor sets) plus
    the documented tie-break. Deliberately does NOT reuse derive()'s dominance
    loop — it is the differential oracle the ancestry fix is checked against.
    Shares only the orphan/index machinery (not under test) with the fold."""
    recs = [ref._canonical_record(r) for r in records]
    idx = ref._index(recs)
    applied = set(derive(recs)["order"])
    parents = {rid: ref._parents(idx[rid]) for rid in idx}
    setters = {}
    for rid in sorted(applied):
        for k in ref._effects(idx[rid]):
            setters.setdefault(k, set()).add(rid)
    winners = {}
    for k, cs in setters.items():
        best = min(ref._rank(idx[c]) for c in cs)
        pool = {c for c in cs if ref._rank(idx[c]) == best}
        dominated = set()
        for d in pool:
            dominated |= (_ancestors(d, parents) & pool)
        maximal = sorted(pool - dominated)
        if len(maximal) == 1:
            winners[k] = maximal[0]
        else:
            def clean(c):
                anc = _ancestors(c, parents, applied)
                intent = sum(1 for a in anc
                             if ref._rank(idx[a]) < ref.CLASS_RANK["witness"])
                wit = sum(1 for a in anc
                          if ref._rank(idx[a]) == ref.CLASS_RANK["witness"])
                return (-intent, wit, c)
            winners[k] = min(maximal, key=clean)
    return winners, idx


def _permute_parents(records, rng):
    """A copy of `records` with every multi-parent list shuffled."""
    out = []
    for r in records:
        r = dict(r)
        ps = list(r.get("parents") or [])
        if len(ps) > 1:
            rng.shuffle(ps)
            r["parents"] = ps
        out.append(r)
    return out


def differential_oracle():
    """ADDENDUM #3 proof (a): for random DAGs (and the named maximality
    fixtures), the fold's winners match the brute-force oracle, AND derive()
    is invariant under parents-array permutation. Returns the number of checks."""
    rng_o = random.Random(0xA11CE)
    checks = 0
    dags = []
    # Reuse the stress generator's shape; witness-heavy to hit the fixed path.
    for _ in range(300):
        dags.append(rand_dag(rng_o.randint(2, 30)))
    # Plus the named maximality fixtures.
    for nm in ("ancestry-witness-flip-ba", "ancestry-witness-flip-ab",
               "ancestry-task-diamond-ba", "ancestry-task-diamond-ab"):
        dags.append(next(c["records"] for c in CASES if c["name"] == nm))
    for recs in dags:
        out = derive([dict(r) for r in recs])
        # (i) oracle agreement: every conflict winner, and every state value.
        winners, idx = oracle_winners(recs)
        for c in out["conflicts"]:
            assert winners.get(c["key"]) == c["winner"], \
                f"oracle winner != fold winner for {c['key']}: {winners.get(c['key'])} vs {c['winner']}"
            checks += 1
        for k, v in out["state"].items():
            assert k in winners, f"oracle missing key {k}"
            assert ref._effects(idx[winners[k]]).get(k) == v, \
                f"oracle winner value != state[{k}]"
            checks += 1
        # (ii) parents-order invariance over several permutations.
        for _ in range(4):
            perm = _permute_parents(recs, rng_o)
            assert derive([dict(r) for r in perm]) == out, \
                "derive() not invariant under parents-array permutation"
            checks += 1
    return checks


def transport_pin():
    """ADDENDUM: NaN/Infinity. JSON has no literal for them. The live Worker's
    transport is JSON.parse, which REJECTS them, so they cannot reach the fold —
    there is no divergence to fix, only an unreachability to pin. (Python's
    json.loads is lenient and WOULD accept them; that asymmetry is a
    collection-path finding, documented in ENGINEERING.md: a record.json a JS
    reader rejects is silently skipped by the Worker.)"""
    checks = 0
    for tok in ("NaN", "Infinity", "-Infinity"):
        probe = subprocess.run(
            ["node", "-e",
             f'try{{JSON.parse(\'{{"k":{tok}}}\');process.exit(1)}}catch{{process.exit(0)}}'],
            capture_output=True, text=True, timeout=30)
        assert probe.returncode == 0, f"expected node JSON.parse to reject {tok}"
        checks += 1
    return checks


def live_golden():
    """ADDENDUM #3 proof (b): the fixed fold reproduces the DEPLOYED derivation
    of the live record sets byte-for-byte (state/order/conflicts/orphans)."""
    records = json.loads((HERE / "live-records.json").read_text())
    golden = json.loads((HERE / "live-golden.json").read_text())
    checks = 0
    for proj, recs in records.items():
        got = derive([dict(r) for r in recs])
        for field in ("state", "order", "conflicts", "orphans"):
            assert got[field] == golden[proj][field], \
                f"live-golden mismatch for {proj}.{field}"
            checks += 1
    return checks


def main():
    cases_path = HERE / "cases.json"
    results_path = HERE / "results.ts.json"
    # allow_nan=False (ADDENDUM #4 P4): bare NaN/Infinity is invalid JSON and
    # kills the whole TS runner batch at JSON.parse. Fail loudly at
    # construction instead — non-finite numbers are out of the record domain
    # (see DESIGN.md §6).
    cases_path.write_text(json.dumps(
        CASES + FRONTIER_CASES + FORMAT_V2_CASES, allow_nan=False))
    print(f"harness: {len(CASES)} cases, "
          f"{sum(len(c['probes']) for c in CASES)} checkpoint probes "
          f"+ {len(FRONTIER_CASES)} frontier cases, "
          f"{sum(len(c['probes']) for c in FRONTIER_CASES)} frontier probes")

    proc = subprocess.run(
        ["node", str(HERE / "runner.ts"), str(cases_path), str(results_path)],
        capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        print("RUNNER FAILED:", proc.stdout, proc.stderr)
        return 2
    print(proc.stdout.strip())

    ts_results = {r["name"]: r for r in json.loads(results_path.read_text())}
    divergences = []
    frontier_deltas = 0

    def check_case(case):
        nonlocal frontier_deltas
        name = case["name"]
        exp_derive = derive([dict(r) for r in case["records"]])
        # Pass closureRoot through so v2 probes get the three-way result; probes
        # without it get the v1 bool, unchanged.
        exp_verify = [
            verify([dict(r) for r in case["records"]],
                   {k: p[k] for k in ("at", "state", "closureRoot") if k in p})
            for p in case["probes"]
        ]
        got = ts_results.get(name)
        if got is None:
            divergences.append((name, "missing from TS results"))
            return
        if got["derive"] != exp_derive:
            divergences.append((name, "derive differs"))
            for field in ("state", "order", "conflicts", "orphans"):
                if got["derive"][field] != exp_derive[field]:
                    print(f"  [{name}] field {field}:")
                    print(f"    py: {json.dumps(exp_derive[field], sort_keys=True)[:400]}")
                    print(f"    ts: {json.dumps(got['derive'][field], sort_keys=True)[:400]}")
        if got["verify"] != exp_verify:
            divergences.append((name, f"verify differs: py={exp_verify} ts={got['verify']}"))
        exp_fresh = ref.witness_freshness([dict(r) for r in case["records"]])
        if got.get("freshness") != exp_fresh:
            divergences.append(
                (name, f"freshness differs: py={exp_fresh} ts={got.get('freshness')}"))
        # checkpoint v2: every applied id's closure-root digest must be identical
        # across ports (SPIKE).
        exp_roots = {rid: ref.checkpoint_root([dict(r) for r in case["records"]], rid)
                     for rid in exp_derive["order"]}
        if got.get("roots") != exp_roots:
            divergences.append((name, "closure-root digests differ"))
        # DELTA DERIVATION (checkpoint + tail): at every legal checkpoint
        # position, delta derivation must (a) equal the FULL derive byte-for-byte
        # and (b) match the TS port. The full fold is the reference; the delta
        # path must never diverge from it.
        exp_deltas = {}
        for rid in exp_derive["order"]:
            cp = ref.build_checkpoint([dict(r) for r in case["records"]], rid)
            if cp is None:
                exp_deltas[rid] = None
                continue
            closure_ids = {r["id"] for r in cp["closure"]}
            tail = [dict(r) for r in case["records"] if r.get("id") not in closure_ids]
            d = ref.derive_delta(cp, tail)
            exp_deltas[rid] = d
            if d != exp_derive:
                divergences.append(
                    (name, f"delta@{rid} != full derive"))
        if got.get("deltas") != exp_deltas:
            divergences.append((name, "delta derivation differs across ports"))
        # FRONTIER DERIVATION: the case's AUTO frontier checkpoint (maximal
        # applied records) must (a) cover exactly the applied set — the
        # C == applied-set theorem — with the full derived state pinned,
        # (b) delta-derive to the FULL derive byte-for-byte, and (c) be
        # built identically by the TS port (members, root, coverage, delta).
        fcp = ref.build_frontier_checkpoint([dict(r) for r in case["records"]])
        got_f = got.get("frontier")
        if fcp is None:
            if got_f is not None:
                divergences.append(
                    (name, "frontier checkpoint: TS built one, referee none"))
            return
        if got_f is None:
            divergences.append(
                (name, "frontier checkpoint: referee built one, TS none"))
            return
        frontier_deltas += 1
        if {r["id"] for r in fcp["closure"]} != set(exp_derive["order"]):
            divergences.append(
                (name, "frontier covered set != applied set"))
        if fcp["state"] != exp_derive["state"]:
            divergences.append(
                (name, "frontier pinned state != full derived state"))
        fclosure_ids = {r["id"] for r in fcp["closure"]}
        ftail = [dict(r) for r in case["records"]
                 if r.get("id") not in fclosure_ids]
        exp_fd = ref.derive_delta(fcp, ftail)
        if exp_fd != exp_derive:
            divergences.append((name, "frontier delta != full derive"))
        if (got_f.get("at") != fcp["at"]
                or got_f.get("closureRoot") != fcp["closureRoot"]
                or got_f.get("covered") != len(fcp["closure"])
                or got_f.get("delta") != exp_fd):
            divergences.append(
                (name, "frontier checkpoint differs across ports"))
        # TAIL-DUP (the frontier self-clash): over the same auto-frontier
        # checkpoint, a tail serving of a COVERED record with identical
        # content is a duplicate, not a re-version — delta must ENGAGE and
        # equal the full derive; a divergent copy under the same covered id
        # must still answer None. (Live shape: a checkpoint commit's tree
        # inherits the project repo's record.json, the commit is in no
        # cover, so that record is re-read as tail on every delta read.)
        _covered0 = fcp["closure"][0]
        _dup = json.loads(json.dumps(_covered0))
        _div = json.loads(json.dumps(_covered0))
        _div["effects"] = {"__taildup__": "divergent"}
        exp_td = {"dup": ref.derive_delta(fcp, ftail + [_dup]),
                  "div": ref.derive_delta(fcp, ftail + [_div])}
        if exp_td["dup"] != exp_derive:
            divergences.append(
                (name, "tail-dup: identical duplicate of a covered record "
                       "!= full derive (referee)"))
        if exp_td["div"] is not None:
            divergences.append(
                (name, "tail-dup: divergent copy of a covered record "
                       "engaged (referee) — must be None"))
        if got.get("taildup") != exp_td:
            divergences.append(
                (name, "tail-dup scenarios differ across ports"))

    fmt = {"ignored": 0, "verdicts": 0, "assembled": 0, "flipped": 0,
           "frontier_deltas": 0}

    def check_fmt_case(case):
        # The full cross-port battery (the same assertions as check_case,
        # counted on the FORMAT-V2 line only), PLUS the format-v2 law: the
        # STRIPPED writing of the same content — every digest removed —
        # derives, verifies, freshens, and roots identically in the
        # referee. The fold's outputs are a function of the five content
        # fields alone.
        name = case["name"]
        recs = [dict(r) for r in case["records"]]
        stripped = [{k: v for k, v in r.items() if k != "digest"}
                    for r in recs]
        exp_derive = derive(recs)
        got = ts_results.get(name)
        if got is None:
            divergences.append((name, "missing from TS results"))
            return
        if got["derive"] != exp_derive:
            divergences.append((name, "derive differs"))
        if exp_derive != derive(stripped):
            divergences.append(
                (name, "derive(digest-carrying) != derive(stripped)"))
        exp_verify = [
            verify(recs, {k: p[k] for k in ("at", "state", "closureRoot")
                          if k in p})
            for p in case["probes"]
        ]
        if got["verify"] != exp_verify:
            divergences.append(
                (name, f"verify differs: py={exp_verify} ts={got['verify']}"))
        exp_verify_stripped = [
            verify(stripped, {k: p[k] for k in ("at", "state", "closureRoot")
                              if k in p})
            for p in case["probes"]
        ]
        if exp_verify != exp_verify_stripped:
            divergences.append(
                (name, "verify verdicts moved by the digest field"))
        exp_fresh = ref.witness_freshness(recs)
        if got.get("freshness") != exp_fresh:
            divergences.append((name, "freshness differs across ports"))
        if exp_fresh != ref.witness_freshness(stripped):
            divergences.append(
                (name, "freshness moved by the digest field"))
        exp_roots = {rid: ref.checkpoint_root(recs, rid)
                     for rid in exp_derive["order"]}
        if got.get("roots") != exp_roots:
            divergences.append((name, "closure-root digests differ"))
        if exp_roots != {rid: ref.checkpoint_root(stripped, rid)
                         for rid in exp_derive["order"]}:
            divergences.append((name, "closure roots moved by digests"))
        # Delta derivation at every legal checkpoint position, as in
        # check_case — mixed v1+v2 closures and tails included.
        exp_deltas = {}
        for rid in exp_derive["order"]:
            cp = ref.build_checkpoint(recs, rid)
            if cp is None:
                exp_deltas[rid] = None
                continue
            closure_ids = {r["id"] for r in cp["closure"]}
            tail = [dict(r) for r in recs if r.get("id") not in closure_ids]
            d = ref.derive_delta(cp, tail)
            exp_deltas[rid] = d
            if d != exp_derive:
                divergences.append((name, f"delta@{rid} != full derive"))
        if got.get("deltas") != exp_deltas:
            divergences.append((name, "delta derivation differs across ports"))
        # Auto-frontier checkpoint + tail-dup scenarios, as in check_case.
        fcp = ref.build_frontier_checkpoint(recs)
        got_f = got.get("frontier")
        if fcp is None:
            if got_f is not None:
                divergences.append(
                    (name, "frontier checkpoint: TS built one, referee none"))
            return
        if got_f is None:
            divergences.append(
                (name, "frontier checkpoint: referee built one, TS none"))
            return
        fmt["frontier_deltas"] += 1
        if {r["id"] for r in fcp["closure"]} != set(exp_derive["order"]):
            divergences.append((name, "frontier covered set != applied set"))
        fclosure_ids = {r["id"] for r in fcp["closure"]}
        ftail = [dict(r) for r in recs if r.get("id") not in fclosure_ids]
        exp_fd = ref.derive_delta(fcp, ftail)
        if exp_fd != exp_derive:
            divergences.append((name, "frontier delta != full derive"))
        if (got_f.get("at") != fcp["at"]
                or got_f.get("closureRoot") != fcp["closureRoot"]
                or got_f.get("covered") != len(fcp["closure"])
                or got_f.get("delta") != exp_fd):
            divergences.append(
                (name, "frontier checkpoint differs across ports"))
        _covered0 = fcp["closure"][0]
        _dup = json.loads(json.dumps(_covered0))
        _div = json.loads(json.dumps(_covered0))
        _div["effects"] = {"__taildup__": "divergent"}
        exp_td = {"dup": ref.derive_delta(fcp, ftail + [_dup]),
                  "div": ref.derive_delta(fcp, ftail + [_div])}
        if exp_td["dup"] != exp_derive:
            divergences.append(
                (name, "tail-dup: identical duplicate != full derive"))
        if exp_td["div"] is not None:
            divergences.append(
                (name, "tail-dup: divergent copy engaged — must be None"))
        if got.get("taildup") != exp_td:
            divergences.append((name, "tail-dup scenarios differ across ports"))

    for case in CASES:
        check_case(case)
    for case in FRONTIER_CASES:
        check_case(case)
    for case in FORMAT_V2_CASES:
        check_fmt_case(case)

    total_probes = sum(len(c["probes"]) for c in CASES)
    total_witnesses = sum(
        len(ref.witness_freshness([dict(r) for r in c["records"]])) for c in CASES)
    total_roots = sum(len(derive([dict(r) for r in c["records"]])["order"]) for c in CASES)
    # Legal delta positions = applied ids with a complete closure (a buildable
    # checkpoint). Each was checked delta==full AND cross-port above.
    total_deltas = 0
    for c in CASES:
        recs = [dict(r) for r in c["records"]]
        for rid in derive(recs)["order"]:
            if ref.build_checkpoint([dict(r) for r in c["records"]], rid) is not None:
                total_deltas += 1
    if divergences:
        print(f"\nDIVERGENCES: {len(divergences)} case(s):")
        for name, why in divergences[:20]:
            print(f"  - {name}: {why}")
        return 1

    # Property checks that run in the referee (and are cross-checked Python-vs-TS
    # by the cases above): the brute-force maximality oracle + parents-order
    # invariance (ADDENDUM #3), the non-finite transport pin (ADDENDUM #4 P4),
    # and the live-golden byte-identity (ADDENDUM #3 proof (b)).
    try:
        oracle_checks = differential_oracle()
        pin_checks = transport_pin()
        golden_checks = live_golden()
        # SPIKE: pin the frozen poc-quilt-1 @task-c closure-root digest value so
        # a change to the digest construction is caught (and matches the PR).
        actual_poc = ref.checkpoint_root(
            [dict(r) for r in _LIVE["poc-quilt-1"]], "task-c")
        assert actual_poc == POC_QUILT1_TASK_C_ROOT, \
            f"poc-quilt-1@task-c root changed: {actual_poc}"
        # Pin the three-way verify result for each semantic checkpoint case so
        # the suite asserts WHICH answer is correct, not only that the ports
        # agree. In particular ckpt-v2-reason-tamper must be STATE-ONLY (caught,
        # never a silent VERIFIED).
        _by_name = {c["name"]: c for c in CASES}
        for _nm, _want in CKPT_V2_EXPECTED.items():
            _c = _by_name[_nm]
            _got = verify([dict(r) for r in _c["records"]],
                          {k: _c["probes"][0][k]
                           for k in ("at", "state", "closureRoot")
                           if k in _c["probes"][0]})
            assert _got == _want, \
                f"{_nm}: expected {_want}, referee gave {_got}"
        # FRONTIER: pin the frozen poc-scale-1 frontier — four members
        # covering 62/62, the root literal recomputed from the construction.
        _scale = [dict(r) for r in _LIVE["poc-scale-1"]]
        _fcp = ref.build_frontier_checkpoint(_scale)
        assert _fcp is not None, "poc-scale-1 frontier checkpoint missing"
        assert _fcp["at"] == ["t40", "t50", "t60", "witness-1"], \
            f"poc-scale-1 frontier members: {_fcp['at']}"
        assert len(_fcp["closure"]) == 62, \
            f"poc-scale-1 frontier covers {len(_fcp['closure'])}, want 62"
        assert _fcp["closureRoot"] == POC_SCALE1_FRONTIER_ROOT, \
            f"poc-scale-1 frontier root changed: {_fcp['closureRoot']}"
        assert ref.checkpoint_root(
            _scale, ["t40", "t50", "t60",
                      "witness-1"]) == POC_SCALE1_FRONTIER_ROOT
        assert _fcp["state"] == derive(_scale)["state"]  # C == applied set
        # TAIL-DUP incident pin (the live poc-scale-1 shape): the frontier
        # checkpoint commit's tree inherits witness-1's record.json and that
        # commit is in no cover manifest, so witness-1 is re-read as tail
        # on every live delta read. Identical content must ENGAGE and equal
        # the full derive; a divergent witness-1 must still answer None.
        _w1 = next(r for r in _scale if r["id"] == "witness-1")
        assert ref.derive_delta(_fcp, [dict(_w1)]) == derive(_scale), \
            "tail-dup: identical witness-1 tail != full derive (poc-scale-1)"
        _w1_div = dict(_w1)
        _w1_div["effects"] = {"__taildup__": "divergent"}
        assert ref.derive_delta(_fcp, [_w1_div]) is None, \
            "tail-dup: divergent witness-1 tail engaged (poc-scale-1)"
        # FRONTIER verdict pins: the referee gives the PINNED answer for
        # each semantic frontier case, not only cross-port agreement.
        _fby_name = {c["name"]: c for c in FRONTIER_CASES}
        for _nm, _want in FRONTIER_EXPECTED.items():
            _c = _fby_name[_nm]
            _got = verify([dict(r) for r in _c["records"]],
                          {k: _c["probes"][0][k]
                           for k in ("at", "state", "closureRoot")
                           if k in _c["probes"][0]})
            assert _got == _want, \
                f"{_nm}: expected {_want}, referee gave {_got}"
        # Singleton equivalence of the builders, over the whole corpus: an
        # array of one builds EXACTLY the string-form checkpoint object,
        # and a one-member list digests exactly the singleton root — the
        # generalisation is a widening of the domain, not a new digest.
        for _c in CASES:
            _recs = [dict(r) for r in _c["records"]]
            for _rid in derive(_recs)["order"]:
                _single = ref.build_checkpoint([dict(r) for r in _recs], _rid)
                if _single is None:
                    continue
                assert ref.build_frontier_checkpoint(
                    [dict(r) for r in _recs], [_rid]) == _single, \
                    f"singleton frontier != build_checkpoint in {_c['name']}@{_rid}"
                assert ref.checkpoint_root([dict(r) for r in _recs], [_rid]) == \
                    ref.checkpoint_root([dict(r) for r in _recs], _rid), \
                    f"one-member root != singleton root in {_c['name']}@{_rid}"
        # FORMAT V2 verdict pins: the referee gives the PINNED answer for
        # each semantic format-v2 case.
        _fmt_by_name = {c["name"]: c for c in FORMAT_V2_CASES}
        for _nm, _want in FORMAT_V2_EXPECTED.items():
            _c = _fmt_by_name[_nm]
            _got = verify([dict(r) for r in _c["records"]],
                          {k: _c["probes"][0][k]
                           for k in ("at", "state", "closureRoot")
                           if k in _c["probes"][0]})
            assert _got == _want, \
                f"{_nm}: expected {_want}, referee gave {_got}"
        # FORMAT V2 root pins. (a) The frozen poc-quilt-1@task-c root,
        # assembled from born digests, is the pinned literal — the root is
        # a function of content alone, however the leaves are sourced.
        assert _assembled_root(
            _born([dict(r) for r in _LIVE["poc-quilt-1"]]),
            "task-c") == POC_QUILT1_TASK_C_ROOT, \
            "assembled poc-quilt-1@task-c root != pinned literal"
        # (b) Mixed closures root identically in all three writings.
        assert ref.checkpoint_root(_mix_a, "gc") == \
            ref.checkpoint_root(_mix_chain, "gc") == \
            ref.checkpoint_root(_born(_mix_chain), "gc"), \
            "mixed-closure root differs across v1/v2 writings"
        # (c) Twin-naming: the root follows the content the canonical
        # index keeps (L — its stripped serialisation sorts after K's),
        # never the digest either twin carries.
        assert ref.checkpoint_root(_twins_born, "kid") == \
            ref.checkpoint_root([_twinL, _twinKid], "kid"), \
            "twin-naming: root does not follow the content winner"
        assert ref.checkpoint_root(_twins_born, "kid") != \
            ref.checkpoint_root([_twinK, _twinKid], "kid"), \
            "twin-naming: root cannot distinguish the twins"
        # (d) The copied digest, caught by assembly: over the forged set
        # the assembled root diverges from the recomputed root — the fast
        # path can tell a claim was taken on trust, which is exactly why
        # the shipped verify path never takes one.
        _forged_recs = [dict(r) for r in _fmt_by_name["fmt-dup-copied-digest"]["records"]]
        assert _assembled_root(_forged_recs, "x") != \
            ref.checkpoint_root(_forged_recs, "x"), \
            "fmt-dup-copied-digest: assembled root must diverge from recomputed"
        assert _assembled_root(_forged_recs, "x") is not None
        # FORMAT V2 corpus sweeps, over EVERY classic and frontier case.
        for _c in CASES + FRONTIER_CASES:
            _recs = [dict(r) for r in _c["records"]]
            _want_d = derive(_recs)
            _want_f = ref.witness_freshness(_recs)
            # (i) born-ignored-by-fold: garbage digests — uniform and
            # per-record random — leave derive + freshness byte-identical.
            for _mode, _seed in (("garbage", 0), ("random-wrong", 11)):
                _v = _v2(_recs, _mode, seed=_seed)
                assert derive(_v) == _want_d, \
                    f"fmt sweep: derive moved under {_mode} digests in {_c['name']}"
                assert ref.witness_freshness(_v) == _want_f, \
                    f"fmt sweep: freshness moved under {_mode} digests in {_c['name']}"
                fmt["ignored"] += 1
            _born_c = _born(_recs)
            # (ii) born-correct verdict invariance across the whole probe
            # corpus: attaching correct digests moves no verdict.
            for _p in _c["probes"]:
                _probe = {k: _p[k] for k in ("at", "state", "closureRoot")
                          if k in _p}
                assert verify(_born_c, _probe) == verify(_recs, _probe), \
                    f"fmt sweep: verdict moved under born digests in {_c['name']}"
                fmt["verdicts"] += 1
            # (iii) root assembly: assembled from born digests ==
            # recomputed, at every applied id of every case.
            for _rid in _want_d["order"]:
                assert _assembled_root(_born_c, _rid) == \
                    ref.checkpoint_root(_recs, _rid), \
                    f"fmt sweep: assembled != recomputed in {_c['name']}@{_rid}"
                fmt["assembled"] += 1
            # (iv) negative sweep: flip one born digest per case (every
            # copy of one covered id) -> the assembled root breaks, while
            # the recomputed root — the only one verify trusts — is
            # unmoved by construction (it never read the field).
            if _want_d["order"]:
                _rid = _want_d["order"][0]
                _idx = ref._index([ref._canonical_record(r) for r in _recs])
                _parents = {i: ref._parents(_idx[i]) for i in _idx}
                _closure = ref._closure_ids(_rid, _idx, _parents) or []
                _target = sorted(_closure)[0] if _closure else None
                if _target is not None:
                    _flip = [dict(r) for r in _born_c]
                    for _r in _flip:
                        if _r["id"] == _target:
                            _d = _r["digest"]
                            _r["digest"] = (_d[:7]
                                            + ("0" if _d[7] != "0" else "1")
                                            + _d[8:])
                    assert _assembled_root(_flip, _rid) != \
                        ref.checkpoint_root(_recs, _rid), \
                        f"fmt sweep: flipped digest did not break assembly in {_c['name']}@{_rid}"
                    fmt["flipped"] += 1
    except AssertionError as e:
        print(f"\nPROPERTY FAILURE: {e}")
        return 1

    print(f"\nCONFORMANCE: {len(CASES)}/{len(CASES)} cases identical "
          f"(derive + {total_probes} checkpoint probes "
          f"+ {total_witnesses} witness-freshness checks "
          f"+ {total_roots} closure-root digests "
          f"+ {total_deltas} delta-vs-full derivations); "
          f"oracle+invariance {oracle_checks}, transport-pin {pin_checks}, "
          f"live-golden {golden_checks}, poc-quilt-1@task-c root "
          f"{POC_QUILT1_TASK_C_ROOT[:12]}… — all pass")
    print(f"FRONTIER: {len(FRONTIER_CASES)}/{len(FRONTIER_CASES)} cases "
          f"identical "
          f"({sum(len(c['probes']) for c in FRONTIER_CASES)} verdict probes, "
          f"{frontier_deltas} frontier delta-vs-full derivations over the "
          f"whole corpus); poc-scale-1 frontier root "
          f"{POC_SCALE1_FRONTIER_ROOT[:12]}… — all pass")
    print(f"FORMAT-V2: {len(FORMAT_V2_CASES)}/{len(FORMAT_V2_CASES)} cases "
          f"identical (derive + verify + freshness + roots + deltas, both "
          f"ports, and identical to the stripped writings); born-ignored "
          f"sweep {fmt['ignored']} corpus derivations byte-identical; "
          f"{fmt['verdicts']} verdicts unmoved by born digests; root "
          f"assembly {fmt['assembled']} assembled == recomputed, "
          f"{fmt['flipped']} flipped digests break assembly; "
          f"fmt-dup-copied-digest caught by assembly, VERIFIED by "
          f"recompute; poc-quilt-1@task-c root {POC_QUILT1_TASK_C_ROOT[:12]}… "
          f"unmoved — all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
