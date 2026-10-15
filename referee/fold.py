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


# --------------------------------------------------------------------------
# SPIKE (retraction-spike branch — NOT merged semantics): dispute +
# settlement records. Two new kinds that let the record say "wrong" as a
# first-class, fold-visible fact, per the design duel's winning spine
# (research/deep/retraction-design-duel.md §5) and the Grid convergence
# (an open dispute and an open proposal are the same shape: fold-visible,
# fold-inert until a settling record closes it).
#
#   dispute    {target, reason (mandatory), author?} — claims the target's
#              effects should not stand. While OPEN (unsettled, settled
#              "rejected", settled by nobody with authority, or malformed)
#              it changes NOTHING in derivation.
#   settlement {dispute, resolution: "upheld"|"rejected", author?} — the
#              settling record. It bites only if AUTHORITATIVE: its author
#              is the target record's author (the author is the authority
#              on their own intent — self-disavowal and author rebuttal)
#              or a captain-class author (has authored an applied override
#              in this set — the spike's set-internal operationalisation of
#              the duel's "captain-only" settlement). Anonymous settlements
#              never bite.
#
# An upheld dispute RETRACTS its target: the target's effects are voided
# and derivation re-runs, but the target keeps its graph position — it is
# NOT removed from the set, so its descendants are not orphaned and not
# auto-disputed; their ancestry still runs through it. A retracted record
# lends no cleanliness: in descendants' tallies it counts as NEITHER an
# intent nor a witness ancestor (the bounded stain — floor "counts as
# nothing"; stacking penalties were considered and not taken).
#
# Meta records (dispute/settlement) are never derivation steps: they are
# excluded from `order`, from candidacy (any `effects` on them are void),
# and from ancestry tallies, but they remain in the parents graph for
# orphan/cycle analysis and for ancestry transitivity.
#
# Disputes may target meta records too: settled upheld, they VOID the
# targeted dispute/settlement (a voided settlement never ruled; a voided
# dispute never bites). Voiding is evaluated on the dispute-dependency
# graph; a dispute that sits in, or depends on, a voiding cycle has no
# well-founded settlement basis and is inert (doubt -> no bite).
# --------------------------------------------------------------------------

_META_KINDS = ("dispute", "settlement")


def _author(rec):
    """A record's author, if it carries a non-empty string one. Records in
    the shipped sets carry no author at all; absence means unattributed."""
    a = rec.get("author")
    return a if isinstance(a, str) and a != "" else None


def _settlement_pass(idx, orphan):
    """First pass of the fold: compute, from the record set alone, which
    records are retracted and the status of every dispute.

    Returns (retracted, statuses): `retracted` is a set of non-meta record
    ids whose effects are voided; `statuses` maps EVERY dispute id in idx
    to "open" | "upheld" | "rejected" | "inert" (malformed, orphaned,
    voided, or cycle-entangled disputes are "inert")."""
    kind = {rid: idx[rid].get("kind") for rid in idx}
    author = {rid: _author(idx[rid]) for rid in idx}
    captains = {author[r] for r in idx
                if kind[r] == "override" and r not in orphan and author[r]}

    # Well-formed disputes: target names a record in the set, reason is a
    # non-empty string. A dispute that never landed (orphan) is inert.
    target_of = {}
    for rid in idx:
        if kind[rid] != "dispute":
            continue
        tgt, reason = idx[rid].get("target"), idx[rid].get("reason")
        if isinstance(tgt, str) and tgt in idx \
                and isinstance(reason, str) and reason != "":
            target_of[rid] = tgt
    live = {d for d in target_of if d not in orphan}

    # Candidate settlements per dispute: well-formed (names a live-able
    # dispute, resolution in domain), landed (not orphan), attributed, and
    # authoritative for that dispute.
    settlements_on = {}
    for rid in idx:
        if kind[rid] != "settlement" or rid in orphan or author[rid] is None:
            continue
        d = idx[rid].get("dispute")
        if not (isinstance(d, str) and d in target_of):
            continue
        if idx[rid].get("resolution") not in ("upheld", "rejected"):
            continue
        tgt_author = author[target_of[d]]
        if author[rid] == tgt_author or author[rid] in captains:
            settlements_on.setdefault(d, []).append(rid)

    # Dispute-dependency graph: d depends on every dispute whose status
    # decides whether d or one of d's candidate settlements is voided.
    def deps(d):
        out = set()
        for d2 in live:
            if target_of[d2] == d or target_of[d2] in settlements_on.get(d, ()):
                out.add(d2)
        return out

    dep_map = {d: deps(d) for d in live}

    def voided(x, status):
        return any(target_of[d2] == x and status.get(d2) == "upheld"
                   for d2 in live)

    # Evaluate in dependency order (Kahn-style over the dependency graph).
    # Whatever cannot be evaluated — it sits in, or depends on, a voiding
    # cycle — has no well-founded settlement basis and stays inert.
    status = {}
    remaining = set(live)
    while True:
        ready = [d for d in sorted(remaining) if dep_map[d] <= status.keys()]
        if not ready:
            break
        for d in ready:
            if voided(d, status):
                status[d] = "inert"          # voided dispute never bites
            else:
                cands = [s for s in settlements_on.get(d, ())
                         if not voided(s, status)]
                if not cands:
                    status[d] = "open"
                else:
                    ruling = min(cands)      # smallest id rules (code point)
                    status[d] = idx[ruling]["resolution"]
            remaining.discard(d)

    statuses = {}
    for rid in idx:
        if kind[rid] == "dispute":
            statuses[rid] = status.get(rid, "inert")
    retracted = {target_of[d] for d in live
                 if status.get(d) == "upheld"
                 and kind[target_of[d]] not in _META_KINDS}
    return retracted, statuses


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


def _orphan_set(idx, parents):
    """Records excluded from derivation: any ancestor absent, or blocked
    behind such a record, or in/behind a parent cycle. Computed over ALL
    records (meta kinds included — the parents graph is kind-blind)."""
    ids = set(idx)
    children = {rid: [] for rid in idx}
    for rid, ps in parents.items():
        for p in ps:
            if p in children:
                children[p].append(rid)
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
    orphan |= _cycle_affected(ids - orphan, parents)
    return orphan


def _topological(nodes, parents):
    """Kahn's algorithm, always taking the smallest ready id: one canonical
    order. Indegree counts only parents inside `nodes`, so a subgraph (e.g.
    the derivation records, whose meta parents are not derivation steps)
    gets its own canonical order; when every parent is inside — the only
    shape the pre-spike fold ever passed — the count is len(parents)."""
    nodes = set(nodes)
    indeg = {r: sum(1 for p in parents[r] if p in nodes) for r in nodes}
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
    orphan = _orphan_set(idx, parents)

    # SPIKE: the settlement pass runs first, over the whole landed set;
    # meta records are consumed by it and are never derivation steps.
    retracted, _statuses = _settlement_pass(idx, orphan)
    meta = {rid for rid in idx if idx[rid].get("kind") in _META_KINDS}

    applied = sorted(ids - orphan - meta)
    applied_set = set(applied)
    order = _topological(applied_set, parents)

    # Full ancestor set of every landed record, memoised once per call in
    # topological order (parents precede children). Computed over the whole
    # landed graph — meta and retracted records included — so ancestry
    # transitivity runs through them exactly as through any record; only
    # candidacy (below) and the cleanliness tallies treat them specially.
    # Precomputing means no ancestor walk is repeated across candidates or
    # keys — the per-candidate re-walks were superlinear on deep contested
    # keys (ADDENDUM #4 P5).
    anc = {}
    for rid in _topological(ids - orphan, parents):
        a = set()
        for p in parents[rid]:
            if p in anc:
                a.add(p)
                a |= anc[p]
        anc[rid] = a

    # Who sets each key, among applied records only. A retracted record's
    # effects are voided: it is never a candidate, never a winner or loser.
    setters = {}
    for rid in applied:
        if rid in retracted:
            continue
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
                # Meta records count as neither (they are not derivation);
                # a retracted ancestor counts as neither either — the
                # bounded stain: it stops lending intent weight, and is
                # not demoted to a witness penalty (floor: counts as
                # nothing; stacking penalties were not taken).
                tallied = [a for a in anc[c]
                           if a not in meta and a not in retracted]
                intent = sum(1 for a in tallied if _rank(idx[a]) < CLASS_RANK["witness"])
                wit = sum(1 for a in tallied if _rank(idx[a]) == CLASS_RANK["witness"])
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


def retraction_status(records):
    """SPIKE: the settlement pass as data — a sibling surface to
    witness_freshness, not a change to derive()'s output shape.

    Returns {"disputes": {dispute_id: status}, "retracted": [record_id]}
    where status is one of "open" (no authoritative ruling yet),
    "upheld"/"rejected" (the smallest-id authoritative, unvoided
    settlement's ruling), or "inert" (malformed, orphaned, voided, or
    cycle-entangled — the dispute cannot bite). Pure function of the
    record set, like derive()."""
    records = [_canonical_record(r) for r in records]
    idx = _index(records)
    parents = {rid: _parents(rec) for rid, rec in idx.items()}
    orphan = _orphan_set(idx, parents)
    retracted, statuses = _settlement_pass(idx, orphan)
    return {"disputes": statuses, "retracted": sorted(retracted)}
