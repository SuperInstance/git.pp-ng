# Format v2 — records born with digests

A record may carry one extra sibling field:

```jsonc
{
  "id": "task-c",
  "parents": ["override-1"],
  "kind": "task",
  "effects": { "feature.inherited": true },
  "digest": "sha256:<64 lowercase hex>"
}
```

`digest` is `"sha256:"` + the record's **checkpoint-v2 record leaf**
(`docs/CHECKPOINT-V2.md`): the SHA-256 of the canonical whole-record form
over the five content fields — `id`, `parents` (as a set), `kind`,
`effects` (in the float64 domain), and `reason`. The leaf does not cover
`digest` itself; there is no second dialect to learn, implement, or drift
from. Format v1 is everything written before this field existed: records
without it, unchanged, first-class forever.

## Who writes, who verifies, who never looks

- **The runner writes it.** `pp.py complete` computes the digest at the
  single `record.json` write point, after every refusal has passed, using
  the referee's leaf implementation (`referee/fold.py`) — imported, never
  reimplemented, so the writer cannot drift from the dialect checkpoints
  pin. A record that already carries the correct digest keeps it; the
  runner writes new records only and never rewrites history.
- **The Worker verifies it at collection.** Every collected copy carrying
  a *well-formed* digest (`sha256:` + 64 lowercase hex) is checked against
  the TS leaf over its own content. A disagreement produces a
  `collectionWarnings` entry —

  ```json
  { "id": "task-a", "kind": "digest-mismatch",
    "claimed": "sha256:…", "computed": "sha256:…" }
  ```

  — and nothing else changes: the record is treated exactly as v1. This is
  **degrade-and-warn, never reject**, by design: a digest is a claim
  *about* a record, and letting a bad claim reject the record would hand
  anyone who can flip one byte a lever to make records vanish. A
  malformed `digest` value is not a claim at all; it is ignored like any
  other extra field. `/api/records` passes the digest through, so an API
  consumer can verify each record itself, without trusting the Worker.
- **The fold never reads it.** Derivation, witness freshness, checkpoint
  roots, and verify verdicts are functions of the five content fields
  alone. This includes the subtle corner: duplicate-id resolution in the
  canonical index orders records by a whole-record serialisation, and
  both ports exclude `digest` from that ordering key, so which twin the
  fold keeps follows content alone. Checkpoint verification likewise
  keeps its standing law — it **always recomputes** leaves from content.
  No shipped path takes a born digest on trust.

Two corollaries are pinned by the conformance family:

- **A digest-only difference is not divergence.** Collection's
  divergent-duplicate judgement compares content with the claim set
  aside. Two copies of a record that differ only in `digest` dedupe
  silently apart from any digest-mismatch warning the lying copy earns;
  two copies with *identical* digests over *different* content are
  divergent, and the copied digest is itself a mismatch for the content
  it rides on (the `fmt-dup-copied-digest` case). "Digests match" never
  stands in for "contents match".
- **Roots are a function of content alone.** A closure root computed
  over v2 records equals the root over the same records stripped, and
  over any mixed writing. The frozen `poc-quilt-1@task-c` root
  (`60effe23970d…`) does not move when the live set is re-expressed with
  born digests. The harness additionally proves, as an oracle result,
  that a root *assembled* from honest born digests equals the recomputed
  root across the whole corpus — and that flipping one digest observably
  breaks the assembled root while the recomputed root, the only one
  verification trusts, cannot move.

## Migration: writers-first, no flag day

There is no conversion event. From the runner release that writes the
field, new records are born v2; every record already written stays as it
is — frozen projects are never rewritten, and a project whose records
predate v2 derives, verifies, and checkpoints exactly as before, forever.
Mixed sets are the normal case during the (indefinite) transition: a v2
parent with a v1 child, a v1 parent with a v2 child, and checkpoints
taken over either all root and verify identically, because the root was
always a digest of content and the leaf the digest names is the leaf the
checkpoint already used. Readers that predate v2 see one more extra
field, which they already ignore: the fold reads only the five content
fields (DESIGN §1, Inputs).

What the field buys today is integrity annotation where records are
gathered (collection names a lying copy at read time instead of letting
it pass as fact) and self-verifying API responses. What it deliberately
does not do is speed anything up by trust: verification still recomputes
every leaf it relies on. The conformance equivalence above is the receipt
that a future reader *could* assemble roots from born digests under a
verify-then-trust rule without changing any answer — an engineering
option the format now carries, not a shortcut this release takes.

## Conformance

- `conformance/harness.py`, FORMAT-V2 family (separate totals line; the
  classic and frontier totals are unchanged): born-ignored-by-fold over
  the whole corpus (garbage digests leave every derivation
  byte-identical), born-correct incl. the float splitter values,
  born-stale (STATE-ONLY / MISMATCH exactly as the stripped writing),
  born-forged (`fmt-dup-copied-digest`), mixed v1+v2 closures in both
  parent directions, twin-naming with swapped digests, and the
  root-assembly equivalence + negative sweeps.
- `tests/format-v2.mjs`: the worker/collection level — the
  digest-mismatch warning shape, the full duplicate table
  (identical/divergent × correct/wrong/copied digests, exact warning
  multisets), degrade-and-warn on the delta and full read paths alike.
- `runner/test_digest.py`: the writer — the digest is the referee's leaf
  by construction, correct digests are kept, stale ones are recomputed
  for the content actually written.
