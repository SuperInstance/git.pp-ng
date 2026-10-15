# Checkpoint v2 — closure-root pinning

Post-competition design work. It is implemented in both ports
(`referee/fold.py`, `src/fold.ts`) in lockstep and pinned by the conformance
harness. It is **not** wired into the Worker or `pp.py` — the fold-level
primitive is what lands; `derive` and `/api/state` are untouched.

This is the **graduation** of the original spike with the referee's fix list
applied: the leaf-number rendering is pinned to one canonical form across both
ports (FIX 1) and the leaf covers the whole record so a `reason` re-version is
caught (FIX 3). The three-way shrunk/swapped classification is called out as an
open captain's call (see the last section). The referee verifies independently,
including recomputing the `poc-quilt-1` root from this doc.

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

**Record leaf** — a digest of one record's **whole canonical content**. The
leaf covers every field, `reason` included, so *any* content tamper changes it
(see "Why the whole record" below):

```
normalized = {
  "id":      str(record.id),
  "parents": record.parents as strings, sorted by Unicode code point,
  "kind":    record.kind (raw; null if absent),
  "effects": record.effects, with values in the float64 domain and
             leaf-number-encoded (see below),
  "reason":  record.reason (raw; null if absent),
}
canon = json.dumps(normalized, sort_keys=True, ensure_ascii=True,
                   separators=(", ", ": "))        # == src/fold.ts pyCanonical
leaf  = SHA-256( UTF-8(canon) )                    # lowercase hex
```

- Top-level keys sort to `effects, id, kind, parents, reason`.
- `parents` is treated as a **set**, sorted by code point, so the leaf is
  independent of the order a record happened to list its parents. (Parent
  *order* carries no meaning in this system, so it is deliberately not content
  the leaf pins — reordering parents is not a tamper.)
- **Float64 domain + leaf-number encoding (one canonical form, both ports).**
  Record numbers are IEEE-754 float64 (DESIGN.md §6). An integer outside the
  JS-safe range is float64-canonicalised first (`9007199254740993` → the double
  `9007199254740992`). The number is then rendered into **one** canonical
  decimal both ports compute identically:
  - an **integral** value with `|v| < 1e21` → its **exact** integer digits
    (Python `int(v)`; TypeScript `BigInt(v)` — not `String(v)`, whose shortest
    round-trip diverges from the exact integer for integral doubles in
    `[1e16, 1e21)`, e.g. `724818294626474393600`, not `…400000`);
  - signed zero (`-0.0`) → `0`;
  - every other finite value → Python's `repr`/`json.dumps` float form
    (shortest round-trip digits; scientific with a sign-prefixed, ≥2-digit
    zero-padded exponent when the decimal point position `decpt ≤ -4` or
    `> 16`), which `src/fold.ts` reproduces from `Number.toExponential()`.
  This pins the bytes identically across the **whole** float64 domain, not only
  the safe range — e.g. `1e-5` → `1e-05` (not JS's `0.00001`), `2.5e-7` →
  `2.5e-07`. (Values `≥ 1e21` or non-finite are outside the pinned domain, as in
  DESIGN.md §6.) A 40,002-value cross-port fuzz (targeted splitters + random
  doubles across every magnitude + random bit patterns) confirms Python
  `json.dumps` and the TypeScript renderer emit identical bytes for all of them.

### Why the whole record (not fold-relevant content only)

An earlier spike hashed only *fold-relevant* content and excluded `reason`. That
left a hole: re-versioning a record's `reason` (same id, new reason) left the
leaf — and the root — unchanged, so a checkpoint over the old reason verified a
bare **VERIFIED** against the tampered set. Hashing the whole record closes it.
The three-way result is then exact:

- a tamper that **changes derived state** (effects, kind, parents) → `MISMATCH`
  (the state comparison fails);
- a tamper the fold **ignores** (`reason`) → `STATE-ONLY`: derived state is
  unchanged, but the closure root no longer matches — a "member re-versioned",
  which the table below already names. It is **caught**, never a silent
  `VERIFIED`.

`reason` cannot produce `MISMATCH` on its own precisely *because* the fold does
not read it (and `derive` is untouched by this spike); `STATE-ONLY` is the
correct, honest label for a closure that changed without changing the state.

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
closure root at `task-c` — over `task-c`'s closure `{genesis, task-a, task-b,
override-1, task-c}` — is:

```
60effe23970d8a5cc837636c44685502361288c1f00577e9addfa10cbd4fc91d
```

Both ports produce this; the harness pins the literal value. (This changed from
the earlier fold-relevant-only spike value `891aa330…` when the leaf was
extended to the whole record — `override-1` carries a `reason`, which the leaf
now covers.)

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
- **a reason-only re-version → STATE-ONLY** (caught, not a silent VERIFIED) —
  `ckpt-v2-reason-tamper`, with `ckpt-v2-reason-verified` pinning the honest set
  to VERIFIED (the whole-record leaf, FIX 3);
- **leaf-number splitters** `1e-5`, `2.5e-7`, `-0.0` carried in record effects,
  so the corpus samples the values that split Python's `json.dumps` from a naive
  JS `String()` (`ckpt-v2-leaf-floats`, FIX 1);
- the three-way result for each semantic case is **pinned to the expected
  answer** (not only cross-port agreement) via `CKPT_V2_EXPECTED`;
- **identical digests across ports** on every applied id of the whole case
  corpus **and** the live `poc-quilt-1` set — 16,290 closure-root digests
  compared, plus the pinned literal above.

Totals (`python3 conformance/harness.py`): 863/863 cases, 1029 checkpoint
probes, 3231 witness-freshness checks, 16,290 closure-root digests;
oracle+invariance 2812, transport-pin 3, live-golden 8 — all pass.

v1 behaviour is untouched: `derive` is unchanged, `/api/state` is unaffected,
and checkpoints without a root still return a bool.

## Open: the shrunk/swapped classification (a captain's call, not resolved here)

The semantics **as built**: `verify_checkpoint` returns `STATE-ONLY` on *any*
closure change when the state still matches — the closure **grown** (a
later-arriving ancestor), **shrunk**, or a member **swapped/re-versioned**. The
original order for this work put *shrunk* and *swapped* closures under
`MISMATCH` rather than `STATE-ONLY`. That classification is a **doctrine call
left open for the captain**; this spike keeps the as-built behaviour and does
not decide it.

Referee's structural note on why it is not a free change: a single closure
**root** is a hash of the closure's content — it tells you *that* the closure
differs from the pinned one, not *how*. A root alone cannot distinguish a
*grown* closure from a *shrunk* or *swapped* one, so routing shrunk/swapped to
`MISMATCH` would require pinning **more** than the root — e.g. the closure's
size or its full id set — alongside it, so verification can compare shapes and
not just a single digest. Until that is specified, `STATE-ONLY` is the only
answer the root can honestly give for "the closure is not the one I pinned."

## Addendum: frontier checkpoints (set-form `at`)

`at` generalises from one record id to a **set** of ids — the frontier —
with the root computed by the identical construction over the union of the
members' closures: [`FRONTIER-CHECKPOINTS.md`](FRONTIER-CHECKPOINTS.md).
Singleton equivalence is byte-exact (a frontier of one *is* the v2
checkpoint at that record; writers emit the string form when |F| = 1), the
verdict semantics above carry over member-wise, and for the auto frontier
(maximal applied records) the covered set is exactly derive's applied set.
Fold level only, like this doc's original landing; Worker/runner wiring is
a separate step.
