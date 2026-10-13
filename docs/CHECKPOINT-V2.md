# Checkpoint v2 — closure-root pinning (spike)

Post-competition design work, landed early as a **spike** for timestamping. It
is implemented in both ports (`referee/fold.py`, `src/fold.ts`) in lockstep and
pinned by the conformance harness, but it is **not** wired into the Worker or
`pp.py`, and the referee reviews it for correctness of the spec, not to merge.

## The problem (v1 is relative to the record set)

Checkpoint v1 verifies `(at, state)` by re-deriving over `at`'s ancestor closure
**as seen in the record set passed to `verify_checkpoint`**. That is honest but
*relative*: if the set presented at verification differs from the set the
checkpoint was taken over — a later-arriving ancestor grows `at`'s closure, or a
closure member is re-versioned (same id, new content) — the derived state can be
unchanged while the thing it was derived from is not. v1 answers a bare `true`
and cannot tell "verified against the same history" from "the state still
matches, but the history moved."

## What v2 adds

A **closure root**: a deterministic digest of `at`'s full ancestor closure,
pinned when the checkpoint is taken. Verification recomputes it over the current
set and returns a three-way result:

| Result | Meaning |
|---|---|
| `VERIFIED` | state matches **and** the closure digest matches the pinned root |
| `STATE-ONLY` | state matches but the closure has changed since the checkpoint (grown, shrunk, or a member re-versioned) — the honest v1 answer, now **labelled**, never a silent `VERIFIED` |
| `MISMATCH` | the state differs (or the checkpoint shape is unusable) |

A tampered or stale root with a matching state is `STATE-ONLY` (not `VERIFIED`) —
the digest cannot distinguish a forged root from an honestly-grown closure, and
either way it is "not the pinned closure," which is the safe answer.

**Backwards compatible.** A checkpoint with no `closureRoot` gets exactly the v1
bool. Existing callers (`pp.py verify`, the scene) are unchanged.

## The digest construction (reproduce it byte-for-byte)

All hashing is **SHA-256**, lowercase hex, over UTF-8 bytes. A sync pure-JS
SHA-256 is used in `fold.ts` (the Worker's WebCrypto is async; the fold is
sync); it is validated against Python's `hashlib` on the standard vectors.

**Record leaf** — a digest of one record's *fold-relevant* content (fields the
fold ignores, e.g. a task's `reason`, are excluded, so the leaf pins exactly
what derivation depends on):

```
normalized = {
  "id":      str(record.id),
  "parents": record.parents as strings, sorted by Unicode code point,
  "kind":    record.kind if it is a string, else null,
  "effects": record.effects, with values in the float64 domain and
             leaf-number-encoded (see below),
}
canon = json.dumps(normalized, sort_keys=True, ensure_ascii=True,
                   separators=(", ", ": "))        # == src/fold.ts pyCanonical
leaf  = SHA-256( UTF-8(canon) )                    # lowercase hex
```

- Top-level keys sort to `effects, id, kind, parents`.
- `parents` is treated as a **set**, sorted by code point, so the leaf is
  independent of the order a record happened to list its parents.
- **Float64 domain + leaf-number encoding.** Record numbers are IEEE-754
  float64 (DESIGN.md §6). An integer outside the JS-safe range is
  float64-canonicalised first (`9007199254740993` → the double
  `9007199254740992`). For the leaf, an **integral** value with `|v| < 1e21` is
  written as a plain decimal integer (no trailing `.0`), matching JavaScript's
  `Number`→string; other finite values use the shortest round-trip decimal.
  This makes Python and TypeScript emit the same bytes for the entire safe range
  and the 2⁵³-boundary integers. (Values `≥ 1e21` or non-finite are outside the
  pinned domain, as in DESIGN.md §6.)

**Closure root** — over `at`'s closure (`at` plus all transitive parents within
the set; if any ancestor is absent the closure is incomplete and the root is
`null`, exactly as v1 cannot verify it):

```
closureRoot(at) = SHA-256( UTF-8(
    "\n".join( id + ":" + leaf(record[id])
               for id in sorted(closureIds(at), by Unicode code point) ) ) )
```

(Record ids are expected to be free of `\n` and `:`-ambiguity — the record-id
convention in this system is simple tokens.)

### Worked value

For the frozen `poc-quilt-1` project (the 8 records `/api/records` returns), the
closure root at `task-c` is:

```
891aa3308ae3e1dd4e32c1f8f20ec9093ef8d69bf9e67a1be6179d73fdbdb884
```

Both ports produce this; the harness pins the literal value.

## Migration story

1. **Today (v1).** `verify_checkpoint(records, {at, state})` → bool. Nothing
   changes for it.
2. **Taking a v2 checkpoint.** When a checkpoint is created at `R` over set `S`,
   also store `closureRoot = checkpoint_root(S, R)` alongside `(at, state)`.
3. **Verifying.** Pass the stored root:
   `verify_checkpoint(records, {at, state, closureRoot})` →
   `VERIFIED | STATE-ONLY | MISMATCH`. A caller that wants the old boolean
   simply omits `closureRoot`.
4. **Wiring (not in this spike).** A Worker route could return the current root
   for a project at a record, and `pp.py verify` could store/compare it; neither
   is built here. The fold-level primitive is what this spike lands.

## Conformance

The harness (`python3 conformance/harness.py`) pins, all cross-checked
Python-vs-TS:

- **same set → VERIFIED** (`ckpt-v2-verified`, and `ckpt-v2-live-poc-quilt-1`
  at `task-c`);
- **a later-arriving ancestor grows the closure → STATE-ONLY**, never a silent
  VERIFIED (`ckpt-v2-state-only-grown`);
- **tampered state → MISMATCH** (`ckpt-v2-mismatch-state`);
- **tampered root → not VERIFIED** (STATE-ONLY) (`ckpt-v2-tampered-root`);
- **identical digests across ports** on every applied id of the whole case
  corpus **and** the live `poc-quilt-1` set — 16,281 closure-root digests
  compared, plus the pinned literal above.

v1 behaviour is untouched: `derive` is unchanged, `/api/state` is unaffected,
and checkpoints without a root still return a bool.
