# Checkpoint + tail derivation (delta reads)

The read-path latency fix. `/api/state` and `/api/records` re-derive from the
record on every request; the fold is ~12 ms, but **collecting** the record is
not — it reads one `record.json` per repo through the Artifacts binding, with no
batch read offered (metal profile: a 62-record project is 126 binding calls —
2 `list` + 1 `get` + 1 `log` + 60 `get` + 62 `readFile` — and lands at a
2.0–5.7 s median). This wires checkpoint v2 (PR #9) into the read path so a
request reads only the **tail**.

## What a checkpoint pins, and where it lives

A checkpoint is a `checkpoint.json` committed to the **project repo's HEAD** —
an artefact the system already knows how to make and verify (`docs/CHECKPOINT-V2.md`).
It is created by the runner (`pp.py checkpoint <project>`, a gated write — there
is **no** unauthenticated creation route), never on the read path. If a project
has no `checkpoint.json`, the read path is exactly today's full fold.

```jsonc
{
  "v": 2,
  "project": "poc-scale-1",
  "at": "t60",                       // the record whose closure this pins
  "createdAt": "…Z",
  "state": { … },                    // derived state of at's closure
  "closureRoot": "<sha-256>",        // checkpoint v2 closure root
  "closure": [ {record}, … ],        // at's full ancestor closure (raw records)
  "coverProjectCommits": { "<commit>": "<recordId>", … },  // project-repo commits in the closure
  "coverTaskRepos": { "<repo>": { "updatedAt": "…", "id": "<recordId>" }, … }
}
```

The runner places the checkpoint at the applied record with the **largest
closure** (most coverage) unless `--at` names one.

## The read path

`projectDerivation` reads `checkpoint.json` (one `readFile` on the project repo
it already `get`s). With a checkpoint present it collects the project with the
cover manifest:

- **`list` the namespace** (unchanged — 2 calls) — this already returns every
  repo's `updatedAt`.
- **Project-repo commits**: a commit whose id is in `coverProjectCommits` is
  served from the checkpoint's stored closure (commit ids are immutable, so this
  is a sound skip); a new commit is read (tail).
- **Task repos**: a repo in `coverTaskRepos` whose **current `updatedAt` still
  matches** the pinned value is served from the stored closure — **no `get`, no
  `readFile`**. A changed `updatedAt`, or a repo not in the manifest, is read
  (tail).

The records are then assembled in the *same* order the full collection uses
(project commits, then task repos by name), with the *same* first-seen dedupe
and origin attribution — so `/api/records` (origins, warnings) is byte-identical
too, not only `/api/state`.

Finally `deriveDelta(checkpoint, tail)` (in `src/fold.ts`, refereed against
`referee/fold.py`) re-derives over `closure ∪ tail`. Because `closure ∪ tail`
is the full current record set, its output is **byte-identical to the full
fold**. The JSON body carries no new fields; an `x-gitpp-derivation: delta|full`
response header reports which path ran.

## Correctness — the full fold stays the reference

`deriveDelta` returns `null` (→ silent full-fold fallback) on **any** doubt, and
the full fold re-reads everything:

- the checkpoint is malformed, or has a duplicate id in its closure;
- the stored closure is not exactly `at`'s ancestor closure, or its recomputed
  closure root ≠ the pinned `closureRoot` (tampered / corrupt);
- deriving the closure does not reproduce the pinned `state` (not VERIFIED);
- a tail record shares an id with the closure (a re-versioned covered record, or
  an ambiguous partition);
- a covered record's source repo/commit has vanished (the collector could not
  serve the whole closure from cache).

The one trust assumption is that a task repo's `updatedAt` advances whenever its
`record.json` changes (the same assumption any mtime/etag cache makes); a
covered task repo is skipped only while its `updatedAt` is unchanged. Everything
*detectable* falls back. The closure root still guards the stored closure's
integrity against tampering of `checkpoint.json` itself.

Conformance pins the equivalence directly: for **every** corpus case, at **every
legal checkpoint position**, `derive_delta` equals the full `derive`
(state / order / conflicts / orphans) and matches the TypeScript port —
16,290 delta-vs-full derivations, alongside the existing 863-case /
16,290-closure-root suite. `tests/worker-delta.mjs` proves the Worker path
itself byte-identical to the full fold (and the fallbacks correct) against an
in-memory Artifacts mock.

## Read cost

For a checkpointed project a state read is `list` + `get(project)` +
`log(project)` + `readFile` for the checkpoint and for **tail records only** —
the covered task repos cost neither a `get` nor a `readFile`.

| project | full fold (`readFile`) | delta (`readFile`, incl. checkpoint.json) |
|---|---|---|
| poc-quilt-1 (closure 5/8 @ task-c) | 9 | 4 |
| poc-scale-1 — frozen fixture (closure 21/62) | 63 | 42 |
| poc-scale-1 — live chain (closure 61/62 @ t60) | ~63 | **~3** |

(Counts from `tests/worker-delta.mjs`; the live chain figure is the same
arithmetic at the live topology — one project `get`/`log`, the two project
commits, the checkpoint, and the single off-chain witness as tail.) The win
grows with closure coverage and with project size; a freshly-appended task is
tail, so an append keeps the delta path active and reads only the new record.

## Deploy

No new routes, no binding changes — the Worker upload is exactly as in
`DEPLOY.md` (multipart upload of the bundle with the artifacts + KV bindings in
the metadata, then re-set the `ADMIN_KEY` secret). After deploying, create the
checkpoints:

```
pp.py checkpoint poc-quilt-1
pp.py checkpoint poc-scale-1
pp.py checkpoint demo-race-1
```

Then verify: `/api/state?project=poc-quilt-1` byte-identical to the frozen
golden, `x-gitpp-derivation: delta`, the conformance live-golden 8/8, and
`pp.py state` on all three demo projects unchanged from the full fold.
