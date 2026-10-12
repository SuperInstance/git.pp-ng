"""Deterministic fold from a record of task completions to current state.

This is the reference implementation; src/fold.ts is the TypeScript port the
Worker runs, held bit-for-bit identical by the conformance harness.

Resolution, per key:

CLASS PRIORITY is ABSOLUTE. override > task > witness. A witness never outranks
a task or an override, even when the witness is a descendant: seeing a value is
not deciding it. Ancestry decides only *within* the highest class present for a
key. Record kinds outside {"task","witness","override"} rank as witnesses — we
cannot verify that they carry intent.

MAXIMALITY. Within that class, a candidate dominated by another (it is a
transitive ancestor of a fellow candidate) is superseded; only the most-derived
survive.

TIE-BREAK — the cleanest chain wins. Among surviving candidates where none is an
ancestor of another, prefer the record whose ancestry holds the most intent
records (task + override); if equal, the fewest witness records; if still equal,
the lexicographically smallest id. More deeds behind a value makes it cleaner;
more bare observations, weaker.
"""

import heapq
import json

# Lower is stronger. Unknown kinds fall to the most humble class (see docstring).
CLASS_RANK = {"override": 0, "task": 1, "witness": 2}
_FALLBACK_RANK = 2

# Record numbers are JSON values, which the JS side (the live Worker) holds as
# IEEE-754 float64. Integers outside the JS safe range round when they cross
# JSON.parse (9007199254740993 -> 9007199254740992), so an int above 2**53-1 is
# not representable identically on both sides. The referee canonicalises every
# such int to float64 AT INGEST, so Python and TypeScript fold the same number
# bit-for-bit. Safe-range ints (every value in the live record sets) are left
# as-is, so derived state for poc-quilt-1 / poc-scale-1 is byte-identical.
_JS_SAFE_INT = 2**53 - 1


def _canon_num(v):
    """float64-canonicalise non-JS-safe integers, recursively through containers.
    bool is left alone (it is an int subclass but a distinct JSON type)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and not (-_JS_SAFE_INT <= v <= _JS_SAFE_INT):
        return float(v)
    if isinstance(v, list):
        return [_canon_num(x) for x in v]
    if isinstance(v, dict):
        return {k: _canon_num(x) for k, x in v.items()}
    return v


def _canonical_record(rec):
    """A record with its effect values float64-canonicalised (see _canon_num)."""
    eff = rec.get("effects")
    if not isinstance(eff, dict):
        return rec
    out = dict(rec)
    out["effects"] = {k: _canon_num(v) for k, v in eff.items()}
    return out


def _index(records):
    """id -> record, built in a canonical order so input permutation cannot matter."""
    ordered = sorted(
        records,
        key=lambda r: (str(r.get("id", "")), json.dumps(r, sort_keys=True, default=str)),
    )
    return {str(r["id"]): r for r in ordered}


def _parents(rec):
    return [str(p) for p in (rec.get("parents") or [])]


def _effects(rec):
    return {str(k): v for k, v in (rec.get("effects") or {}).items()}


def _rank(rec):
    return CLASS_RANK.get(rec.get("kind"), _FALLBACK_RANK)


def _cycle_affected(nodes, parents):
    """Nodes that cannot be topologically ordered (in, or blocked behind, a parent cycle)."""
    indeg = {r: 0 for r in nodes}
    children = {r: [] for r in nodes}
    for r in nodes:
        for p in parents[r]:
            if p in indeg:
                indeg[r] += 1
                children[p].append(r)
    ready = [r for r in nodes if indeg[r] == 0]
    heapq.heapify(ready)
    done = set()
    while ready:
        n = heapq.heappop(ready)
        done.add(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(ready, c)
    return set(nodes) - done


def _topological(nodes, parents):
    """Kahn's algorithm, always taking the smallest ready id: one canonical order."""
    indeg = {r: len(parents[r]) for r in nodes}
    children = {r: [] for r in nodes}
    for r in nodes:
        for p in parents[r]:
            if p in children:
                children[p].append(r)
    ready = [r for r in nodes if indeg[r] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        n = heapq.heappop(ready)
        order.append(n)
        for c in children[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(ready, c)
    return order


def derive(records):
    records = [_canonical_record(r) for r in records]
    idx = _index(records)
    ids = set(idx)
    parents = {rid: _parents(rec) for rid, rec in idx.items()}

    children = {rid: [] for rid in idx}
    for rid, ps in parents.items():
        for p in ps:
            if p in children:
                children[p].append(rid)

    # Orphans: a record is orphaned if any ancestor is absent, or is itself already
    # orphaned. Propagated with a worklist (O(N+E)) rather than a repeated rescan,
    # which is quadratic when ids happen to sort dependants before their parents.
    orphan = set()
    work = [rid for rid in idx if any(p not in ids for p in parents[rid])]
    orphan.update(work)
    head = 0
    while head < len(work):
        for c in children[work[head]]:
            if c not in orphan:
                orphan.add(c)
                work.append(c)
        head += 1
    orphan |= _cycle_affected(set(ids) - orphan, parents)

    applied = sorted(set(idx) - orphan)
    applied_set = set(applied)
    order = _topological(applied, parents)

    # Full applied-ancestor set of every applied record, memoised once per call
    # in topological order (parents precede children). anc[rid] is naturally
    # applied-restricted: an applied record's parents are all applied, or it
    # would itself be an orphan. Precomputing here means no ancestor walk is
    # repeated across candidates or keys — the per-candidate re-walks were
    # superlinear on deep contested keys (ADDENDUM #4 P5).
    anc = {}
    for rid in order:
        a = set()
        for p in parents[rid]:
            if p in applied_set:
                a.add(p)
                a |= anc[p]
        anc[rid] = a

    # Who sets each key, among applied records only.
    setters = {}
    for rid in applied:
        for k in _effects(idx[rid]):
            setters.setdefault(k, set()).add(rid)

    state = {}
    conflicts = []
    for k in sorted(setters):
        cands = sorted(setters[k])
        best = min(_rank(idx[c]) for c in cands)
        pool = {c for c in cands if _rank(idx[c]) == best}
        # A candidate is dominated iff it is a (transitive) ancestor of ANY
        # other pool candidate; the survivors are the most-derived. Exact
        # maximality straight from the precomputed ancestor sets, so the result
        # cannot depend on parents-array order (ADDENDUM #3 fix).
        dominated = set()
        for d in pool:
            dominated |= anc[d] & pool
        maximal = sorted(pool - dominated)
        if len(maximal) == 1:
            winner = maximal[0]
        else:
            # Tie-break: the cleanest chain wins. Among equally-derived
            # candidates of the same class, prefer the one whose ancestry holds
            # the most intent records (task + override), then the fewest witness
            # records, then the lexicographically smallest id — more deeds
            # behind a value makes it cleaner; more bare observations, weaker.
            def _cleanliness(c):
                intent = sum(1 for a in anc[c] if _rank(idx[a]) < CLASS_RANK["witness"])
                wit = sum(1 for a in anc[c] if _rank(idx[a]) == CLASS_RANK["witness"])
                return (-intent, wit, c)

            winner = min(maximal, key=_cleanliness)

        state[k] = _effects(idx[winner])[k]
        distinct = []
        for c in cands:
            v = _effects(idx[c])[k]
            if v not in distinct:
                distinct.append(v)
        if len(distinct) >= 2:
            conflicts.append(
                {"key": k, "winner": winner, "losers": sorted(c for c in cands if c != winner)}
            )

    return {"state": state, "order": order, "conflicts": conflicts, "orphans": sorted(orphan)}


def verify_checkpoint(records, checkpoint):
    """True only if deriving from exactly the ancestors of checkpoint['at'] (incl. it)
    reproduces checkpoint['state']. A missing 'at' record, or any missing ancestor
    (so the ancestor set is incomplete and nothing can be verified), returns False."""
    if not isinstance(checkpoint, dict):
        return False
    at, want = checkpoint.get("at"), checkpoint.get("state")
    if at is None or not isinstance(want, dict):
        return False
    at = str(at)
    # Compare the declared state in the SAME number domain the fold uses:
    # float64-canonicalise the checkpoint's values exactly as derive()
    # canonicalises record effects. Without this the referee compares a
    # non-JS-safe int exactly while the TS port has already rounded it at
    # JSON.parse, so the same checkpoint verifies differently in the two ports
    # (ADDENDUM #4 P1). A value that denotes a DISTINCT double is still
    # rejected; one that denotes the same double is accepted, identically.
    want = {k: _canon_num(v) for k, v in want.items()}

    idx = _index(records)
    if at not in idx:
        return False
    parents = {rid: _parents(rec) for rid, rec in idx.items()}

    closure, stack = set(), [at]
    while stack:
        n = stack.pop()
        if n in closure:
            continue
        closure.add(n)
        for p in parents.get(n, ()):
            if p not in idx:
                return False
            stack.append(p)

    return derive([idx[r] for r in sorted(closure)])["state"] == want


def witness_freshness(records):
    """Per-witness staleness, as a record-count distance.

    recordsBehind(w) = (number of applied records) - (number of applied
    records in w's own causal past, w included). It counts the applied
    records that landed without w in their ancestry: the records w did not
    witness. A witness with recordsBehind 0 saw everything that is applied;
    a larger number is a staler observation.

    Pure function of the record, like derive(). Returns {witness_id: int}
    for every APPLIED record of kind "witness" (orphaned and cycle-excluded
    witnesses are absent, exactly as they are absent from derive()'s state).
    derive()'s own output is unchanged, so /api/state is unaffected.
    """
    records = list(records)
    idx = _index(records)
    out = derive(records)
    applied = set(out["order"])
    parents = {rid: _parents(rec) for rid, rec in idx.items()}

    def _ancestors_incl(start):
        seen, stack = {start}, list(parents.get(start, ()))
        while stack:
            n = stack.pop()
            if n in seen or n not in applied:
                continue
            seen.add(n)
            stack.extend(parents.get(n, ()))
        return seen

    total = len(applied)
    result = {}
    for rid in out["order"]:
        if idx[rid].get("kind") == "witness":
            result[rid] = total - len(_ancestors_incl(rid))
    return result
