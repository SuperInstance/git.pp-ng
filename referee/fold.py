"""Deterministic fold from a record of task completions to current state.

Base implementation: MiniMax's Bout 1 winning entry (branch minimax/fold,
fold-bout repo), used under the bout's terms as the build seed.

GRAFT (Bout 1 referee recommendation, 2026-10-11): the tie-break is
Claude's (chat entry) provenance-cleanliness rule, replacing the
depth rule:

TIE-BREAK (P6): among applied records of the same class that set the same key
and where none is an ancestor of another, the record whose ancestry holds the
most intent records (task + override) wins; if equal, the one whose ancestry
holds the fewest witness records; if still equal, the lexicographically
smallest record id. ("Cleanest chain" made arithmetic: more deeds behind
you is cleaner; more mere observations behind you is weaker.)

Class priority is ABSOLUTE: a witness never outranks a task or an override, even
when the witness is a descendant. Ancestry decides only *within* the highest class
present for that key. Record kinds outside {"task","witness","override"} are ranked
as witnesses, because we cannot verify that they carry intent.
"""

import heapq
import json

# Lower is stronger. Unknown kinds fall to the most humble class (see docstring).
CLASS_RANK = {"override": 0, "task": 1, "witness": 2}
_FALLBACK_RANK = 2


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


def _first_ancestor_in(start, pool, parents):
    """Nearest ancestor of `start` that is in `pool`, else None. Stops at the first hit."""
    seen = set()
    stack = list(parents[start])
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n in pool:
            return n
        stack.extend(parents.get(n, ()))
    return None


def derive(records):
    records = list(records)
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

    depth = {}
    for rid in order:
        ps = parents[rid]
        depth[rid] = max((depth[p] for p in ps), default=-1) + 1

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
        # A candidate is dominated if it is an ANCESTOR of another candidate; the
        # survivors are the most-derived. Walk up from each d to find the pool-member
        # that d supersedes, and mark it dominated.
        dominated = set()
        for d in pool:
            a = _first_ancestor_in(d, pool, parents)
            if a is not None:
                dominated.add(a)
        maximal = sorted(pool - dominated)
        if len(maximal) == 1:
            winner = maximal[0]
        else:
            # Grafted tie-break (Claude's rule): cleanest ancestry wins.
            def _ancestors(start):
                seen, stack = set(), list(parents[start])
                while stack:
                    n = stack.pop()
                    if n in seen or n not in applied_set:
                        continue
                    seen.add(n)
                    stack.extend(parents.get(n, ()))
                return seen

            def _cleanliness(c):
                anc = _ancestors(c)
                intent = sum(1 for a in anc if _rank(idx[a]) < CLASS_RANK["witness"])
                wit = sum(1 for a in anc if _rank(idx[a]) == CLASS_RANK["witness"])
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
