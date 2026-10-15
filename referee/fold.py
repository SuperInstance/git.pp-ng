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

import hashlib
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


# --------------------------------------------------------------------------
# checkpoint v2 — closure-root pinning (see docs/CHECKPOINT-V2.md, DESIGN §6).
# A checkpoint v1 is relative to the record SET it is verified against; a
# closure-root digest pins the exact closure of `at` so a later-changed closure
# is reported as STATE-ONLY instead of silently re-verifying. The digest is
# SHA-256 over a canonical, float64-domain encoding so a third implementation
# (and src/fold.ts) reproduces it byte-for-byte.
# --------------------------------------------------------------------------

def _leaf_num(v):
    """Encode a number for the leaf in the float64 domain, one canonical form
    both ports compute identically. An integral value with |v| < 1e21 is written
    as its EXACT decimal integer (``int(v)``, no trailing ``.0``); the TS port
    pairs this with ``BigInt(v)`` (not ``String(v)``, whose shortest round-trip
    diverges from the exact integer for integral doubles in ``[1e16, 1e21)``).
    Other finite values keep Python's ``repr``/``json.dumps`` float form
    (shortest round-trip, scientific with a zero-padded exponent when
    ``decpt <= -4`` or ``> 16``), which ``src/fold.ts`` reproduces from
    ``Number.toExponential()``. Recurses through containers; bool is left as a
    distinct JSON type."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e21:
        return int(v)
    if isinstance(v, list):
        return [_leaf_num(x) for x in v]
    if isinstance(v, dict):
        return {k: _leaf_num(x) for k, x in v.items()}
    return v


def _record_leaf(rec):
    """SHA-256 hex of a record's WHOLE canonical content: id, parents (as a set,
    sorted by code point), kind (raw), effects (float64-canonicalised and
    leaf-number-encoded), and reason (raw, null if absent). Hashing the whole
    record — reason included — means any content tamper changes the leaf, so it
    is caught: a tamper that alters derived state is MISMATCH, and a reason
    re-version (which the fold ignores, so derived state is unchanged) is
    STATE-ONLY rather than a silent VERIFIED."""
    normalized = {
        "id": str(rec.get("id", "")),
        "parents": sorted(_parents(rec)),
        "kind": rec.get("kind"),
        "effects": {k: _leaf_num(_canon_num(v)) for k, v in _effects(rec).items()},
        "reason": rec.get("reason"),
    }
    canon = json.dumps(normalized, sort_keys=True, ensure_ascii=True,
                       separators=(", ", ": "))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _closure_ids(at, idx, parents):
    """`at` plus all transitive parents within idx. None if any ancestor is
    absent (an incomplete closure cannot be verified or digested)."""
    closure, stack = set(), [at]
    while stack:
        n = stack.pop()
        if n in closure:
            continue
        closure.add(n)
        for p in parents.get(n, ()):
            if p not in idx:
                return None
            stack.append(p)
    return closure


def checkpoint_root(records, at):
    """Deterministic digest of `at`'s full ancestor closure as seen in `records`.
    None if `at` is absent or its closure is incomplete.

        closureRoot = SHA-256( "\\n".join( id + ":" + leaf(rec)
                               for id in sorted(closureIds, code-point) ) )

    where leaf(rec) = SHA-256( json.dumps({id, parents sorted, kind, effects,
    reason}, sort_keys=True, ensure_ascii=True) ), both as lowercase hex."""
    idx = _index(records)
    at = str(at)
    if at not in idx:
        return None
    parents = {rid: _parents(rec) for rid, rec in idx.items()}
    closure = _closure_ids(at, idx, parents)
    if closure is None:
        return None
    entries = [f"{rid}:{_record_leaf(idx[rid])}" for rid in sorted(closure)]
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


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
    """Verify a checkpoint against `records`.

    v1 (no `closureRoot` in the checkpoint): returns a bool — True iff deriving
    from exactly the ancestors of checkpoint['at'] (incl. it) reproduces
    checkpoint['state']. A missing 'at', a missing ancestor, or a bad shape
    returns False. Unchanged from before.

    v2 (a `closureRoot` is present): returns a three-way string —
      "VERIFIED"   state matches AND the closure digest matches the pinned root;
      "STATE-ONLY" state matches but the closure has changed since the checkpoint
                   (the honest v1 answer, now labelled — never a silent VERIFIED);
      "MISMATCH"   the state differs (or the checkpoint shape is unusable).
    Backwards compatible: callers that pass no root get exactly v1 behaviour."""
    if not isinstance(checkpoint, dict):
        return False
    expected_root = checkpoint.get("closureRoot")
    v2 = expected_root is not None
    at, want = checkpoint.get("at"), checkpoint.get("state")
    if at is None or not isinstance(want, dict):
        return "MISMATCH" if v2 else False
    at = str(at)
    # Compare the declared state in the SAME number domain the fold uses:
    # float64-canonicalise the checkpoint's values exactly as derive()
    # canonicalises record effects (ADDENDUM #4 P1).
    want = {k: _canon_num(v) for k, v in want.items()}

    idx = _index(records)
    parents = {rid: _parents(rec) for rid, rec in idx.items()}
    state_ok = False
    if at in idx:
        closure = _closure_ids(at, idx, parents)
        if closure is not None:
            state_ok = derive([idx[r] for r in sorted(closure)])["state"] == want

    if not v2:
        return state_ok
    if not state_ok:
        return "MISMATCH"
    actual_root = checkpoint_root(records, at)
    return "VERIFIED" if actual_root == str(expected_root) else "STATE-ONLY"


# --------------------------------------------------------------------------
# delta derivation (checkpoint + tail) — the read-path optimisation.
# A checkpoint pins a record's ancestor closure (state + closure root +
# the closure records themselves). The read path can then re-derive without
# re-reading the closure: supply the checkpoint and only the TAIL (records
# outside the closure), and derive() runs over closure ∪ tail — which is the
# full record set, so the result is byte-identical to derive(all). The win is
# entirely on READS (the closure is not re-read); the fold itself is unchanged.
# --------------------------------------------------------------------------

def build_checkpoint(records, at):
    """Construct a checkpoint for `at` over `records`, or None if `at`'s closure
    is incomplete (no valid checkpoint there). The closure records are emitted
    in canonical (code-point id) order so both ports build an identical object."""
    idx = _index(records)
    at = str(at)
    if at not in idx:
        return None
    parents = {rid: _parents(rec) for rid, rec in idx.items()}
    closure = _closure_ids(at, idx, parents)
    if closure is None:
        return None
    closure_records = [idx[rid] for rid in sorted(closure)]
    return {
        "at": at,
        "state": derive(closure_records)["state"],
        "closureRoot": checkpoint_root(records, at),
        "closure": closure_records,
    }


def derive_delta(checkpoint, tail_records):
    """Delta derivation: reconstruct the full record set from a checkpoint's
    stored closure plus the freshly-read `tail_records`, then derive() it.
    Returns a derive()-shaped dict, or None to signal "fall back to the full
    fold" — the full fold stays the reference implementation.

    None (fall back) on any doubt about the checkpoint or the partition:
      * a malformed checkpoint, or a duplicate id within the stored closure;
      * the stored closure is not exactly `at`'s ancestor closure (extra or
        missing records), or its recomputed root != the pinned closureRoot
        (tampered / stale checkpoint);
      * deriving the closure does not reproduce the pinned state (not VERIFIED);
      * a tail record shares an id with the closure (ambiguous partition).

    When it returns a dict, that dict equals derive(closure ∪ tail) exactly —
    and since a well-formed caller passes tail = (all current records) minus the
    closure, that is derive(all current records), byte-for-byte."""
    if not isinstance(checkpoint, dict):
        return None
    at = checkpoint.get("at")
    want = checkpoint.get("state")
    expected_root = checkpoint.get("closureRoot")
    closure = checkpoint.get("closure")
    if (at is None or not isinstance(want, dict)
            or not isinstance(expected_root, str) or not isinstance(closure, list)):
        return None
    at = str(at)
    closure_ids = set()
    for r in closure:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            return None
        if r["id"] in closure_ids:
            return None  # duplicate id in the closure — ambiguous
        closure_ids.add(r["id"])
    if at not in closure_ids:
        return None
    # The stored closure must be exactly `at`'s ancestor closure within itself
    # (no extra, no missing), and its root must match the pinned value.
    idx = _index(closure)
    parents = {rid: _parents(rec) for rid, rec in idx.items()}
    cl = _closure_ids(at, idx, parents)
    if cl is None or cl != closure_ids:
        return None
    if checkpoint_root(closure, at) != expected_root:
        return None
    if derive(closure)["state"] != {k: _canon_num(v) for k, v in want.items()}:
        return None
    # The tail must be disjoint from the closure by id.
    for r in tail_records:
        if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] in closure_ids:
            return None
    return derive(list(closure) + list(tail_records))


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
