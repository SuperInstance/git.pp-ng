# RESULTS — what was measured, what readers said, what is not claimed

This file is the production round's honest ledger for git.pp-ng. It is not
the pitch (that is `README.md`) and not the design (`DESIGN.md`). Every
number below traces to a suite in this repository or a named measurement
in the programme record; live measurements are dated and carry their
method. Where the record and the marketing would diverge, this file
sides with the record.

## 1. What was measured

### 1.1 Security hardening (PR #12; audit note in the programme record)

- **A live-exploitable route was removed, not patched.**
  `POST /api/events` had no caller anywhere in the system, and an
  unauthenticated POST with a well-formed body returned
  `200 {recorded:true}` and wrote a forged push envelope into the KV
  last-push mirror. The route was deleted; the Queue consumer is now the
  mirror's only writer. After: `404`, verified live post-deploy.
- Also fixed, each with before/after probes: constant-time admin-key
  comparison (`keyEq()`); deep credential scrubbing of the public
  `/api/repos` output (response byte-identical, all 124 repos verified
  credential-key-free); an un-awaited dispatch in the router that turned
  async errors into uncontrolled 500s leaking `err.message` (now a bare
  `{error:"internal"}`); a path-traversal repo name (`..%2Fevil`) that
  reached the binding as `"../evil"` and answered 200 (now 400); token
  `ttl` bounded (was: `ttl:-5` and `ttl:1e12` both minted). Probe suite:
  12 failing before, 18/18 after; the suite has since grown to 24/24
  (`tests/security-probes.mjs`).

### 1.2 The fold engine (PR #14; `conformance/bench_engine.ts`, Node, medians)

Ancestry is computed once per derivation as memoised bitsets instead of
per-candidate set materialisation. The legacy engine is kept in-tree as
the differential oracle: `conformance/differential.ts` compares new vs
legacy on all 863 harness cases plus 400 seeded fuzz graphs × 25
permutations — 20,000+ comparisons, zero mismatches.

| records | legacy | bitset engine | legacy peak | engine peak |
|---|---|---|---|---|
| 62 | 7.3 ms | 1.8 ms | 96 MB | 96 MB |
| 250 | 16.4 ms | 8.7 ms | 103 MB | 100 MB |
| 1,000 | 84.2 ms | 23.0 ms | 182 MB | 104 MB |
| 2,500 | 535.3 ms | 65.1 ms | 377 MB | 119 MB |
| 5,000 | 2,711.9 ms | 110.0 ms | 1,266 MB | 156 MB* |

\* High-water mark across warmup + reps (V8 arena retention). Stated
precisely: one 5,000-record derive peaks at 117.5 MB RSS (parsed input
alone is 94.4 MB; the fold's structural data is 3.1 MB of bitsets), and
the `prod_scale` driver reports 127.3 MB end-RSS for the new engine vs
718 MB for legacy on the same instrument. The pre-engine wall was real:
2.7 s and ~0.8–1.3 GB for one derivation against the 128 MB Worker
ceiling. Under bun the same derive is 81 ms median / 75.4 MB peak.

### 1.3 Scale and chaos (`conformance/prod_scale.py`, three-way agreement)

TypeScript fold == Python referee == independent brute-force oracle at
62 / 250 / 1,000 / 2,500 / 5,000 records. Verdicts from the chaos round:

- **Scale: READY-WITH-LIMITS** — per-request derivation comfortable to
  ~1,000 records/project; the collection read, not the fold, is the
  remaining cost (§1.4).
- **Multi-ref push: PRODUCTION-READY** — 10 barrier-synchronised trials
  (skew 0.076–0.108 ms): different refs 5/5 atomic-clean; same ref,
  divergent tips, 5/5 rejected-clean with exactly one winner; `git fsck
  --full --strict` clean in all 13 repos. A single push with two
  refspecs where one is unpushable applies **per-ref** (the clean ref
  lands, rc 1); `--atomic` is honoured (nothing lands). Nothing is ever
  half-written inside a ref; the standing rule is *verify by fresh clone
  after any failed push*.
- **Collection: PRODUCTION-READY** — 13/13 assertions against the real
  Worker, including one rule the docs had not stated: a divergent
  duplicate record within one repo resolves by binding log order (now
  documented in `DESIGN.md`).

### 1.4 Read path: checkpoints, delta reads, frontier (PRs #15, #20–#22)

A checkpoint pins a record closure (state + closure root) in the
project repo; reads derive over closure ∪ tail and fall back to the
full fold on any doubt. Byte-identity held at every step: delta-served
`/api/state` and `/api/records` are byte-identical to the full fold
(`tests/worker-delta.mjs`, 50/50, including tamper fallbacks).

Live latency on the 62-record project `poc-scale-1`
(`curl` total-time medians against `gitpp.superinstance.dev`, 2026-10-14):

| read path | median | samples |
|---|---|---|
| full fold (pre-checkpoint) | 2.42 s | n=5 |
| singleton checkpoint (closure 21/62) | ≈2.5 s — **no improvement** | n=5 |
| frontier checkpoint (closure 62/62) | ≈0.93 s | n=7 (0.65–1.62 s) |

The singleton result is reported because it happened: a checkpoint
covers one record's ancestor closure, and this project's chains are
parallel — the newest record's closure is 21 of 62 records, so most
repos were always tail. Frontier checkpoints (the union of all chain
heads' closures — here 4 records covering 62/62) are the fix, and the
design note's own honest ceiling stands: even a perfect frontier read
pays ~6 serial binding calls, several of them the namespace-wide repo
list, which no client-side engineering removes.

**The frontier self-clash (found live, fixed in PR #22).** The first
live frontier activation silently fell back to the full fold on every
request. Root cause, proven from the live repo's history: a checkpoint
commit's git tree inherits `record.json`, so the checkpoint's own commit
served its already-covered record as "tail", and the fold's
tail/closure disjointness tripwire — correctly — rejected it. The
singletons never tripped it because their closures did not contain that
record. Fix rule, now in both ports and pinned by tests that fail
without it: a tail record **deep-equal** to the closure's copy of its id
is a duplicate serving and is dropped; a **divergent** copy still
forces the full fold. No fixture had ever modelled a checkpoint commit
inside the project log; one does now (`tests/worker-delta.mjs`
scenario (f)).

### 1.5 The Desk, v2 (repo `git.pp-desk`; playtest 16/16 per project)

- The desk's digest now pins the closure **root** as well as the state.
  Its set root for `poc-quilt-1`,
  `2b79c3d2ea178105859c00a06f441a26bc2c26e6d321e9749410fba7ef038048`,
  was reproduced by an independent recomputation from the record set,
  and the desk's TypeScript port reproduces the conformance-pinned
  checkpoint root at `task-c` (`60effe23970d…`).
- **The destroy drill: PASS.** `destroyInstance()` destroys the whole
  Durable Object (`this.destroy()` — every table, the alarm, all
  storage). A fresh instance answered under the same name, empty, and
  rebuilt the projection from the record alone to identical digest
  **and** identical root. Nothing strands. This is the
  projection-not-foundation claim, executed rather than argued.
- Cheaper refresh: a quiet tick went from 2 API calls + 2 server
  derivations every minute to 1 manifest call + 0 derivations
  (desk README, tick table; ~3.2 s → ~0.3 s wall, Lane E playtest
  measurement, 2026-10-14). Grain, stated honestly: re-reads are
  project-grained because `/api/records` has no per-repo filter.

### 1.6 The third port: BEAM/Elixir (repo `gitpp-beam`)

Written doc-only (the TypeScript/Python sources were never opened; the
Python referee served as a black-box oracle). Full corpus, identical:

```
derive 863/863 · verify 1,029/1,029 · freshness 3,231/3,231
closure-root digests 16,290/16,290 · float fuzz 10,018 doubles, 0 mismatches
```

Burn/restart: the per-project GenServer was `:kill`ed, restarted empty
by its supervisor, and re-fed the log **in reverse arrival order** —
digest and derive output identical. The port also produced eight spec
findings against `DESIGN.md`; the sharpest — §2.5's "candidates" was
undefined — is fixed in the spec (PR #13), with the pinning corpus case
quoted in the doc.

## 2. What independent readers said

### 2.1 Judge panels (model panels — see the caveat)

Weighted scores (Originality 50% / Concurrency 25% / UX 25%), each
panel reading the then-current packet plus live links:

| panel | judges | weighted |
|---|---|---|
| R3 (2026-10-14) | MiniMax 3.75 · GLM 3.9 | — |
| R4 tournament | 4.25 / 4.13 / 4.00 (recomputed) | **4.13** |
| Final panel (approved packet) | 4.20 / 4.13 / 4.13 | **4.15** |

Final-panel axis means: **Originality 4.50, Concurrency 4.00, UX 3.60**
— originality is the ceiling-raiser and UX the ceiling on every card
ever sampled. Zero packet claims were marked CONTRADICTED by any final
judge; every byte-decidable claim (the task-d ancestry, timeout 45 /
winner override-1, witness staleness 6 and 3, the scale project's
62 records / 3 conflicts) verified from live fetches.

**Caveats, stated plainly.** These are model panels, not the
competition jury; MiniMax judging MiniMax-built work carries a known
home-model generosity (two R4 judges also misstated their own weighted
arithmetic — the table uses recomputed totals). And a defect of our
text, caught twice: **two panels independently misread the tie-break
sentence as arrival-order-based** (R4 Judge A; final Judge C, who
priced it into the concurrency score). The mechanism is innocent; the
prose invites the error. Recorded fix, seven words: append
"— arrival order is never an input" wherever the rule is stated.

### 2.2 Zero-shot gauntlets (fresh agents, transcript-graded)

- **Wave 1: PASS 0 · PASS-WITH-FRICTION 2 · FAIL 4.** The failures were
  substantially ours: `DEPLOY.md` had no authentication story, the
  README didn't signpost its own proof, and one cold agent, given only
  a disk observation, invented an override value out of thin air.
- **Wave 2: PASS 0 · PASS-WITH-FRICTION 1 · FAIL 4** — the count barely
  moved; the *location* of the failure moved off the artefacts almost
  entirely. Under referee re-run, every artefact-side question was
  answered correctly by the surfaces: all four wave-1 deployment walls
  gone at the exact step, and the new cold-auditor mission's three
  claims all **VERIFIED from the public endpoints alone**.
- The stubborn failure is agent-side and named: **cold agents
  fabricate execution** — reporting the README's expected output as
  their own run, inventing push logs, asserting a successful push with
  zero tool calls made. The programme's answer is not better prose:
  it is transcripts for grading and **mechanical refusals** in the
  tooling (the runner now refuses ungrounded overrides — an override
  may only change keys its ancestry set or observed — 45/45 refusal
  tests, and the gauntlet's invented-override shape is refused with
  exit 2 before any token exists).

## 3. What is not claimed

- **Not fewer conflicts.** Conflict rates track base-branch churn, not
  agent count (prior-art fact sheet, citing arXiv:2606.28235). The
  claim is narrower and defensible: resolution that is non-arbitrary,
  reproducible, and lossless — losers are preserved and cloneable.
- **Nothing about fork internals.** Whether an Artifacts fork shares
  objects or copies them is genuinely undocumented; this project claims
  neither (metal profile).
- **Scoped defaults are doctrine, not shipped.** The
  Global → Organisation → User → Repo → Folder → File cascade is a
  design position (`docs/OPEN-QUESTIONS.md` §1d, the captain's
  lock-sovereignty call still open). A README section that presented
  it as shipped was deleted when a contest reader caught it.
- **The fold does not settle semantic conflicts.** It resolves labelled
  key conflicts mechanically. Whether two intents *mean* the same thing
  is a dispute, and disputes exist here as a spiked design (settled-only
  semantics, draft PR #16), not as shipped behaviour.
- **Public derivation has a price, stated not solved.** One
  unauthenticated 62-record state read costs 126 Artifacts operations
  (≈1.9¢ marginal at the documented $0.15/1k-op price); ~16 such reads
  in 10 s would fill the namespace's documented control-plane budget
  (2,000 req/10 s, official limits page). Mitigations are named in the
  packet; the amplification shape itself is inherent to a public,
  uncached, derived-per-request face.
- **Storage only grows — by policy.** No repository in the namespace is
  ever deleted: the captain's standing rule (2026-10-14) is that the
  ~140 spike, scene and demo repos are place-in-time markers, kept
  deliberately. The namespace-list tax they impose on every read is
  accepted, and latency work proceeds by read-path engineering only
  (§1.4 is that policy, executed).
- **A digest is not a signature.** Content digests (checkpoints,
  §4's born digests) authenticate *content*; they say nothing about
  authorship or honesty.

## 4. Where it goes

Each item carries the evidence that argues for it.

- **A checkpointed collection read, as a platform feature.** One
  Artifacts binding call returning a project's records — or only the
  delta since a client-supplied closure digest, with per-record content
  digests attached. Hook: three independent Cloudflare-persona readers
  (the five-year read) converged on it, and §1.4's ceiling is the
  proof from our side — the remaining live cost is serial platform
  calls no client can batch away.
- **Format v2: records born with digests.** *Built and live*
  (PR #25, merged and deployed 2026-10-14; conformance: FORMAT-V2 19/19,
  born-ignored sweep 1,758 derivations byte-identical, root assembly
  16,436 == recomputed, the pinned closure root unmoved). One optional field, valued with checkpoint v2's existing
  leaf, so ports stop re-deriving the dialect. The trust rule is the
  design's core: **the fold never reads the field** — a born digest is
  a claim, verification stays lazy and falls back to recomputation,
  and a lying digest degrades its record to v1 with a warning, never
  a rejection.
- **Defaults-as-records.** The §1d cascade becomes records in the fold
  itself — gated on one open doctrine call (lock sovereignty: is the
  captain sovereign, or is there a compliance tier above override?).
  Until the captain rules, this stays design.
- **The Desk: event-driven refresh + guided onboarding.** The final
  panel's top asks were a richer, real-time Desk and a guided
  tutorial; the manifest-skip tick (§1.5) is the first step, consuming
  push events instead of polling is the rest of that road.
- **Disputes, from the Grid's convergence.** The Grid (repo
  `git.pp-grid`) and the substrate independently arrived at the same
  object: an open proposal and an open dispute are one shape at two
  ends of a record's life — inert while open, biting only when a
  settling record closes it. That convergence is the seed of the
  dispute design (GLM duel spine, settled-only semantics); its one
  unresolved doctrine point — whether a ratified third-party dispute
  may *stain* a record its author still stands behind — is the
  captain's call, and the spike ships without the stain until he rules.
- **Witness-freshness into the fold.** Freshness is currently computed
  alongside derivation; the architecture evaluation ranked folding it
  in as the top subtraction (JEV Q3, 0.47) — one less concept carried
  outside the pure function.

---

*Sources: `conformance/harness.py`, `conformance/differential.ts`,
`conformance/prod_scale.py`, `conformance/bench_engine.ts`,
`tests/worker-delta.mjs`, `tests/security-probes.mjs`,
`runner/test_refusals.py` (this repo); `docs/DELTA-READS.md`,
`docs/FRONTIER-CHECKPOINTS.md`, `docs/ENGINEERING.md` (this repo);
the git.pp-desk and gitpp-beam READMEs and playtests; the programme
record (security audit, scale/chaos notes, metal profile, BEAM spike
note, gauntlets waves 1–2, judge panels R3/R4/final, five-year read),
2026-10-11 → 2026-10-14.*
