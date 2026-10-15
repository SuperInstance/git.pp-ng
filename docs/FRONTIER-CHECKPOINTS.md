# Frontier checkpoints — set-form closure roots

Post-competition design work, **fold level only**: implemented in both ports
(`referee/fold.py`, `src/fold.ts`) in lockstep and pinned by the conformance
harness, exactly as checkpoint v2 landed. It is **not** wired into the Worker
or `pp.py` — `readCheckpoint` still accepts only the string form, and the
runner still places single-record checkpoints. Wiring (Worker stored-checkpoint
widening, runner frontier placement + refresh) is a separate PR; this one lands
the primitive and its proofs.

## Why: coverage is a frontier property, not a chain property

Checkpoint v2 pins the ancestor closure of **one** record. On the topology
this system exists for — many agents working in parallel strands that share
little ancestry — any single record's closure is one strand. On the frozen
`poc-scale-1` fixture (62 records), the largest single closure covers **21 of
62**, so a single-record delta read still re-reads 41 records as tail. The
project's read cost is governed by its **frontier** — the set of maximal
applied records — not by any chain. The frontier of `poc-scale-1` is four
records (`t40`, `t50`, `t60`, `witness-1`), and the union of their closures
is **62 of 62**.

## The format delta

One field widens. A checkpoint is `{at, state, closureRoot, closure}` as in
v2, but `at` may be a **list of record ids** — the frontier members:

```
C           = ⋃ closure(f)   over all frontier members f
closureRoot = SHA-256( "\n".join( id + ":" + leaf(record[id])
                       for id in sorted(C, by Unicode code point) ) )
state       = derive(C).state
```

The root is computed by the **identical construction** as v2 — same leaf,
same join, same sort. This is not a new digest: v2's root was always a digest
of a *set* of ids; the singleton closure was just the set it happened to be
computed over. The frontier form widens the domain of `at` from one id to a
set of ids.

- **Singleton equivalence is byte-exact.** A frontier of one has
  `C = closure(at)`, so its root and state are bit-identical to the v2
  checkpoint at that record. The harness pins this over the whole corpus:
  a one-member array builds exactly the string-form checkpoint object.
- **Canonical form rule.** Writers MUST emit the **string form when
  |F| = 1**; the array form always means |F| ≥ 2. Every existing
  single-record checkpoint — including every deployed `checkpoint.json` —
  therefore remains exactly v2, byte-for-byte. Frontier members are emitted
  sorted by Unicode code point.
- **Structural dispatch.** Readers branch on the type: `string` → the v2
  path; `array` → the frontier path. A malformed array — empty, duplicate
  members, non-string members — is rejected: `verifyCheckpoint` answers
  `MISMATCH` (v2) / `false` (v1), `deriveDelta` returns `null` (fall back
  to the full fold), `checkpointRoot` returns `null`.
- **Stored-file version.** Frontier `checkpoint.json` files bump `v` to `3`
  as documentation when wiring lands. No fold-level reader inspects `v`;
  the structural check above is the discriminator, and it fails safe in
  both directions (see Rollout safety).

## The C == applied-set theorem

For the **auto frontier** — the maximal applied records, computed by
`buildFrontierCheckpoint(records)` with no member list — the covered set is
not a partial cover:

> **C is exactly derive's applied set.** Every ancestor of an applied record
> is applied (⊆). And every applied record is an ancestor-or-self of a
> maximal applied record — follow children within the finite applied DAG
> until maximal (⊇).

So a frontier checkpoint's pinned `state` is **the** derived state of the
whole set at creation, and its `deriveDelta` tail is exactly the records
outside the applied set plus whatever arrived after creation. Records
outside C are precisely the never-coverable ones — orphan-affected records
(incomplete closure) and cycle members; under the singleton scheme they
could never enter a checkpoint closure either, and the frontier form
changes nothing for them. The harness asserts C == applied set and
pinned state == full derived state for **every** case in the corpus.

When members are given explicitly (`buildFrontierCheckpoint(records, ids)`),
C is the union of those members' closures — which may be a proper subset of
the applied set (e.g. one strand pinned deliberately). The theorem is a
statement about the auto frontier.

## What the verdict means for a frontier checkpoint

`verifyCheckpoint` generalises the same way: recompute C within the
presented set S from the member list, then
`stateOk = derive(C).state == pinned`, root compared as in v2.

| Result | Frontier meaning |
|---|---|
| `VERIFIED` | S reproduces the pinned covered set exactly (root matches) and its derived state matches. Stronger than the singleton case: *the entire pinned past — the whole applied set as of creation — is byte-intact.* The checkpoint partitions the set into a verified past and an unpinned present. |
| `STATE-ONLY` | Root differs but `derive(C).state` still equals the pinned state: covered content moved without moving the state — e.g. a `reason`-only re-version of a covered record (the whole-record leaf catches it; the fold ignores it; v2's FIX-3 case, now over the whole set), or an effect change to a record whose value the fold never selects (a dominated record's losing value). A tamper that *does* reach the state is `MISMATCH`. |
| `MISMATCH` | The state differs, **or C cannot be reproduced at all**: a frontier member absent from S, or any member's closure incomplete in S (a covered record was removed). This is the v2-consistent ruling — singleton v2 already answers `MISMATCH` when the closure can't be re-derived — applied member-wise. |
| `false` (v1, no `closureRoot`) | The v1 bool over C: `derive(C).state == pinned`, or the form is malformed. |

- **Grown — the normal case — stays VERIFIED.** Verification ranges over
  ancestor closures only. Normal growth is appends *on top of* the frontier:
  new records are descendants of the members, and descendants never enter
  any member's closure. Growth in the ancestor direction is impossible for
  an honestly created frontier checkpoint — a new record could enter
  `closure(f)` only by being a previously missing parent of something in
  it, but frontier members are applied, which means their closures were
  already complete at creation. (Contrast the singleton case, where an `at`
  chosen mid-chain can gain late ancestors in exactly this way.) The only
  events that move a frontier root are re-versioning of a covered record
  and shrinkage inside C.
- **Shrinkage is asymmetric, correctly.** If S has lost only *non-covered*
  records (tail/orphan material), the verdict is unchanged — VERIFIED
  stands, because the pinned past is intact and verification never claimed
  anything about the rest. Only shrinkage *inside* C moves the verdict.
- **Carried-over limitation, stated plainly:** a root still cannot
  distinguish *how* C changed (grown-by-healing vs shrunk vs swapped) — the
  open captain's call in `CHECKPOINT-V2.md` applies unchanged, over a
  bigger set. If that call is ever resolved by pinning the closure's size
  or id set alongside the root, the frontier form benefits for free: the
  id set is already named (it is the members' closure), and C's size is one
  more integer in the file.

## deriveDelta with a frontier checkpoint

Set-union semantics, unchanged from the singleton path: `deriveDelta` never
folds *from* the pinned state; it re-derives `derive(closure ∪ tail)` from
the record set, using the pinned state and root as integrity checks over
the stored artefact (the stored set must be exactly the members' ancestor
closure within itself, its recomputed root must equal the pinned root, it
must re-derive to the pinned state, and no tail record may RE-VERSION a
closure record — same id, divergent content; a deep-equal tail copy of a
covered record is a duplicate serving and is dropped). Any doubt →
`null` → silent full fold, exactly as today. A frontier checkpoint changes only the partition — which subset is
served from the stored artefact — so the delta path's correctness argument
is the singleton one, with the builder as the only new code under test.

## Building one

```
buildFrontierCheckpoint(records)          -> auto frontier (maximal applied)
buildFrontierCheckpoint(records, ids)     -> the named members' union closure
```

Both return the v2 checkpoint object shape (`{at, state, closureRoot,
closure}`) with `at` the string form for |F| = 1 and the sorted member
array otherwise, or `null` when no valid checkpoint exists (no applied
records; an absent member; an incomplete member closure; a malformed
member list). Creation cost is one `derive` plus one root over C — cheaper
than the singleton default's largest-closure hunt over every record.

## Rollout safety (against the shipped code)

A frontier file read by today's Worker hits `readCheckpoint`'s
`typeof c.at !== "string"` check → returns `null` → the project reads as
*no checkpoint* → full fold. Today's fold-level `verifyCheckpoint` would
`String()` the array, find no such id, and answer `MISMATCH` — never a
false `VERIFIED`. So a frontier file can land in a project repo before any
reader understands it, and the worst outcome is the slow path.

## Conformance

The harness (`python3 conformance/harness.py`) pins, all cross-checked
Python-vs-TS:

- the classic family is untouched: 863/863 cases, 1029 checkpoint probes,
  3231 witness-freshness checks, 16,290 closure-root digests, 16,290
  delta-vs-full derivations — the same numbers as before this change;
- a frontier family of verdict cases (VERIFIED over the set with unsorted
  members; descendant growth → VERIFIED; non-covered records beside C →
  VERIFIED; member removed → MISMATCH; covered ancestor removed →
  MISMATCH; reason-only re-version → STATE-ONLY; a state-reaching effect
  tamper → MISMATCH; a dominated record's effect tamper → STATE-ONLY;
  malformed arrays → MISMATCH; the v1 bool over the array form),
  each pinned to its expected answer, not only cross-port agreement;
- **delta-vs-full over frontier checkpoints across the whole corpus**:
  for every case, the auto-frontier checkpoint's `deriveDelta` equals the
  full `derive` byte-for-byte in both ports, its covered set equals the
  applied set, and its pinned state equals the full derived state;
- **tail-dup over the whole corpus**: over every case's auto-frontier
  checkpoint, a tail record identical to a covered record engages and
  equals the full derive in both ports, and a divergent copy under the
  same covered id answers `null` in both ports; the poc-scale-1 incident
  shape (witness-1 arriving as the checkpoint commit's inherited tail) is
  pinned by name;
- **singleton equivalence over the whole corpus**: for every applied id,
  building with `[id]` yields exactly the string-form checkpoint object,
  and `checkpointRoot(records, [id])` equals `checkpointRoot(records, id)`;
- the frozen `poc-scale-1` fixture at its true frontier
  `{t40, t50, t60, witness-1}`, covering 62/62, with the root pinned
  literally:

```
3a0919f1cee5e312f5913941116ead057d2b7ad8df998a09025157affaad99ac
```

## Open items (unchanged, deliberately not decided here)

The shrunk/swapped classification (captain's call, carried over from
`CHECKPOINT-V2.md`); any caching of the namespace listing. Wiring — Worker
`readCheckpoint` widening, runner frontier placement and refresh-on-write,
live re-measurement — is PR 2. The `deriveDelta` disjointness reclassification
named above as a follow-up has LANDED separately: a tail record deep-equal
to the closure's copy of its id is a duplicate serving (dropped); only
divergent content under a covered id still answers `null`. It was forced by
the live layout — a checkpoint commit's inherited record.json otherwise
defeated every frontier delta read of poc-scale-1.
