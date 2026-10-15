# SPIKE: Retraction (dispute + settlement) — branch only, NOT merged semantics

> **Status: exploratory spike on the `retraction-spike` branch.** Nothing in
> this document is shipped semantics. DESIGN.md as merged on `main` does not
> know these kinds exist. The design source is the post-competition design
> duel (`research/deep/retraction-design-duel.md` §5 in the R&D repo): GLM's
> winning spine — `dispute` + `settlement` kinds, disputes fold-inert until
> settled, settlement is itself a record — which independently converged
> with the Grid note's open/settled lifecycle (an open proposal and an open
> dispute are the same object: fold-visible, fold-inert, settled only by a
> settling record). The fold therefore becomes two passes: a settlement
> pass computes the retracted set from the record set alone; the derivation
> pass is the classic fold over what stands.

## The two kinds

| kind | fields (beyond `id`, `parents`) | meaning |
|---|---|---|
| `dispute` | `target` (record id), `reason` (**mandatory**, non-empty), `author` (optional) | "This record's effects should not stand, because: …" |
| `settlement` | `dispute` (dispute id), `resolution`: `"upheld"` \| `"rejected"`, `author` (optional), `reason` (optional) | The ruling on a dispute. |

Any record may additionally carry `author` (a non-empty string). The
shipped record format has no author field; the spike adds it as optional
and reads it **only** for settlement authority (below). `effects` on a
meta record are void — meta records are never candidates.

## Pinned semantics (both ports, conformance-enforced)

1. **An open dispute changes nothing.** Unsettled, settled `rejected`,
   settled by nobody with authority, or malformed — all are exactly
   inert. Proof: the harness sprinkles all three shapes over the whole
   pre-existing corpus (258 variants) and asserts `derive()` is
   byte-identical, field for field.
2. **Authority.** A settlement bites only if its author is (a) the target
   record's author — the author is the authority on their own intent, for
   both self-disavowal and rebutting a third party's dispute — or (b)
   **captain-class**: the author of at least one applied `override` in
   the set. (b) is the spike's set-internal operationalisation of the
   duel's "captain-only settlement"; a real deployment would bind it to
   the token layer. Anonymous settlements never bite. A corollary: a set
   with no attributed records at all is dispute-inert — every pre-spike
   record set, including the live projects, behaves exactly as before.
3. **Upheld ⇒ retracted.** The target's effects are voided and derivation
   re-runs. The target is *not* removed: it keeps its graph position and
   its slot in `order`; it is never a candidate, winner, or loser while
   retracted. (This exclusion semantics is the spike's brief; the duel's
   two designs both stopped at a cleanliness-only stain — the divergence
   is recorded in the spike report, with the mitigation that the record
   is preserved and the retraction is itself reversible by record.)
4. **Descendants are not auto-disputed.** They are not orphaned (ancestry
   still runs *through* the retracted record — domination flows across
   it), their own effects stand or fall on their own merits, but the
   retracted record lends them **no cleanliness**: in tie-break tallies
   it counts as *neither* an intent nor a witness ancestor (the bounded
   stain — floor "counts as nothing"; GLM's stacking −1 penalty was
   considered and not taken, per the duel synthesis: stacking lets
   settlement-count outweigh ancestry magnitude, a quasi-vote).
5. **Meta records are not derivation steps.** Disputes and settlements
   never appear in `order`, never set keys, never count in ancestry
   tallies or in `recordsBehind`. They *are* in the parents graph:
   orphan/cycle analysis, ancestry transitivity, and checkpoint closures
   all see them. A meta record that never landed (missing ancestor, or
   behind a cycle) is inert and cannot bite or rule.
6. **Disputes of disputes / of settlements.** A dispute may target a meta
   record; settled upheld, the target is **voided** (a voided settlement
   never ruled; a voided dispute never bites). Evaluation runs in
   dependency order over the dispute graph. A dispute that sits in, or
   depends on, a **voiding cycle** has no well-founded settlement basis
   and is inert — doubt resolves to no-bite, never to a coin flip.
7. **Conflicting settlements** on one dispute: among authoritative,
   unvoided settlements, the **smallest id** (code-point) rules. (GLM's
   "latest by ID" was considered and not taken: id order is arbitrary
   either way, and smallest-id is this fold's house idiom.) Multiple
   *disputes* on one target are independent: any one upheld retracts; a
   rejected one does not shield the target from another.
8. **Citation is by field, not parentage.** A settlement settles the
   dispute its `dispute` field names, wherever it sits in the graph.
   But a checkpoint's closure contains only ancestors of `at`, so a
   settlement outside the closure does not retract inside it
   (set-relativity — the standing checkpoint doctrine, unchanged).
   Authoring guidance: parent a settlement on its dispute, and a dispute
   on its target, so closures that should see the ruling do.
9. **Targets.** Any record: task, witness (an observation can be
   retracted), override (the captain is disputable — but only a
   captain-class settlement can retract an override), dispute,
   settlement, or an unknown kind.
10. **Surface.** `derive()`'s output shape is unchanged. The settlement
    pass is exposed as a sibling pure function,
    `retraction_status` / `retractionStatus`, returning
    `{disputes: {id: "open"|"upheld"|"rejected"|"inert"}, retracted: […]}`,
    mirroring `witness_freshness`'s pattern.

## ⚠ Doctrine flag — OPEN, deliberately unresolved

Duel §6, the captain's question: **may a third party's accusation, once
ratified, stain a record its author still stands behind — or is "wrong" a
word only an author may say about their own work?** This spike implements
the *mechanics* (a captain-class settlement upholding a third party's
dispute does retract the target — see harness case `ret-captain-upheld`)
**without any stain**: no output marks a retraction as contested, and no
provenance flag distinguishes self-retraction from ratified accusation.
Awaiting the captain's ruling; if the ruling is no-stain, what changes is
the authority rule (item 2b for third-party disputes), not the
bookkeeping.

## Known gaps named by the spike

- **Closure-root leaf blind spot.** The checkpoint-v2 leaf covers
  `{id, parents, kind, effects, reason}`. A dispute's `target` and any
  record's `author` are outside the leaf, so re-targeting a dispute
  inside a closure changes derived state without changing the root
  (the state comparison still catches it as MISMATCH; a target swap
  between two effect-identical records would not change either). Belongs
  to the format-v2 work, not this spike.
- **Runner / Worker untouched.** `pp.py` gains no dispute/settlement
  authoring here; the Worker inherits the semantics automatically because
  it runs this fold. Collection, refusals, and transport are unchanged.
