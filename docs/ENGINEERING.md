# Engineering audit — git.pp

A function-by-function audit of the fold (`src/fold.ts`, `referee/fold.py`) and
the Worker collection path (`src/worker.ts`), plus a line-by-line check of every
security claim `DEPLOY.md` makes against the code. Written to be checked: every
"handled" claim names the conformance case that proves it, and every
possible silent drop is named.

Complexity is in **R** = record count, **E** = total parent edges, **D** = DAG
depth. "Parity" states whether the TypeScript port and the Python referee agree
**by construction** (the same algorithm over the same data, no float/encoding
room to differ) or **only by the harness** (they *could* differ in principle;
the conformance differential is what proves they don't).

---

## Part 1 — the fold

### Guaranteeing parity: the three places the ports *could* have diverged

Two independent implementations of one algorithm agree trivially on integers and
ASCII. They diverge on exactly three things, each of which is now pinned:

1. **String ordering.** Python compares strings by Unicode code point; JS `<`
   and `Array.sort` compare UTF-16 code units. They disagree for non-BMP
   characters. → `cmpCodePoints` (below). Case: `edge-nonbmp-id`, `edge-nonbmp-key`.
2. **Number domain.** JS holds all JSON numbers as float64; Python keeps exact
   ints. They disagree above 2^53. → float64 canonicalisation at ingest (below).
   Cases: `edge-bigint-contested`, `edge-bigint-winner`,
   `edge-bigint-collapse-equal`, `ckpt-bigint-domain`.
3. **Canonical serialization.** The duplicate-id tie-break and value-equality
   both lean on a byte-exact reproduction of Python's `json.dumps(sort_keys=True,
   ensure_ascii=True)`. → `pyCanonical`/`pyEscapeString`/`pyNumber` (below).
   Cases: `edge-dup-id-*`, `edge-unicode`, `edge-floats`.

### `pyEscapeString`, `pyNumber`, `pyCanonical` (ts) / `json.dumps` (py)

- **Invariant.** `pyCanonical(v)` equals Python `json.dumps(v, sort_keys=True,
  ensure_ascii=True, separators=(", ", ": "))` byte-for-byte. `ensure_ascii`
  escapes every non-ASCII code point to `\uXXXX`, so the canonical form is pure
  ASCII — which is why a code-unit vs code-point compare of two canonical forms
  cannot differ, and only *raw* ids/keys need `cmpCodePoints`.
- **Complexity.** O(size of value).
- **Breakpoints.** `pyNumber` is the delicate part: integral float64 render
  without a trailing `.0` (`String(9007199254740992)` → `"9007199254740992"`),
  which is *not* how Python prints the float `9007199254740992.0`. The two
  canonical strings therefore differ for a non-safe integer used as a *duplicate-
  id tie-break key* at exponential magnitudes — documented as out of the pinned
  domain below (duplicate ids are already degenerate; non-safe ints are float64).
- **Parity.** Harness (string construction mirrored by hand); pinned by the
  `edge-dup-id-*`, `edge-floats`, `edge-unicode` cases.

### `pyValueKey` (ts) / `v not in distinct` (py) — conflict value-equality

- **Invariant.** Two effect values count as "the same value" (no conflict) iff
  Python `==` says so: numbers and booleans share one bucket (`1 == 1.0 ==
  True`), strings and null are their own, compound values compare by canonical
  form. In TS this is a string key `"n:"/"s:"/"z:"/"c:"`; in Python it is `==`
  over a list.
- **Breakpoint.** Under the float64 domain, two non-safe ints that round to the
  same double are one value (no conflict); two that round to distinct doubles
  are a conflict. Pinned by `edge-bigint-collapse-equal` (2^53 and 2^53+1 → one
  double, no conflict) and `edge-bigint-contested` (2^53+1 vs 2^53+3 → two
  doubles, conflict).
- **Parity.** Harness. (`NaN != NaN` would split the two — see the transport pin.)

### `cmpCodePoints` (ts) — **fix, ADDENDUM #1**

- **Invariant.** Orders strings by Unicode code-point sequence, matching
  Python's native order. Every outcome-deciding comparison in the TS fold
  (the index tie-break, effect-key order, the Kahn `MinHeap`, every id `.sort`)
  routes through it.
- **Complexity.** O(len) via one `Array.from` per string (iterates code points).
- **Why.** `"a￿"` vs `"a\u{1F600}"`: by code point the BMP id is smaller
  (0xFFFF < 0x1F600); by UTF-16 unit the astral id's leading high surrogate
  (0xD83D) is smaller — the ports elected opposite winners. Case
  `edge-nonbmp-id` (contested winner), `edge-nonbmp-key` (object-key order).
- **Parity.** This *creates* by-construction parity where there was a harness-
  catchable divergence.

### `_canon_num` / `_canonical_record` (py) — **fix, ADDENDUM #1/#4**

- **Invariant.** At ingest, every integer with `|n| > 2^53−1` becomes `float(n)`,
  recursively through list/dict effect values; `bool` and safe-range ints are
  untouched. So the Python referee folds the same float64 the Worker's
  `JSON.parse` already produced. Safe-range ints (all live data) are unchanged,
  so `/api/state` for the live projects is byte-identical (cases
  `live-poc-quilt-1`, `live-poc-scale-1`, and the `live_golden` byte-identity
  assertion).
- **Complexity.** O(size of effects).
- **Parity.** By construction (both sides now in float64); pinned by the
  `edge-bigint-*` and `ckpt-bigint-domain` cases.

### `indexRecords` (ts) / `_index` (py)

- **Invariant.** Builds `id -> record` in a canonical order — sort by `(id,
  pyCanonical(record))` — so input permutation cannot change which record
  survives a duplicate id. First key `id` (code-point), second the canonical
  record.
- **Complexity.** O(R log R · size).
- **Breakpoint.** Duplicate ids whose records differ only in a non-safe integer
  at exponential magnitude (see `pyNumber`): the canonical secondary keys can
  render differently across ports. Out of the pinned domain — documented, not
  claimed handled. Near-2^53 magnitudes agree in outcome (checked:
  `edge-bigint-*` have distinct ids).
- **Parity.** Harness; pinned by `edge-dup-id-diff/identical/numeric`.

### `MinHeap` (ts) / `heapq` (py), `cycleAffected`/`_cycle_affected`, `topological`/`_topological`

- **Invariant.** `topological` is Kahn's algorithm always popping the smallest
  ready id (code-point) → one canonical order. `cycleAffected` returns nodes that
  never reach in-degree 0 (in, or blocked behind, a parent cycle). Self-parents
  are cycles of length 1.
- **Complexity.** O((R + E) log R).
- **Breakpoints.** Self-parent (`edge-self-parent`), 2-cycles (`edge-cycle2`),
  a node blocked behind a cycle (`edge-cycle-child`), deep chains
  (`edge-deep-chain`, 60), wide fan-in (`edge-wide-hotkey`, 40). The `MinHeap`
  comparisons were UTF-16 pre-fix → now `cmpCodePoints`, so the order matches
  `heapq` even with non-BMP ids.
- **Parity.** By construction for the algorithm; the heap's string compare is
  by-construction parity only after the `cmpCodePoints` fix.

### `derive` — orphan pass

- **Invariant.** A record is orphaned if any parent is absent, or if it descends
  from an orphan, or if it is blocked behind a parent cycle. Worklist
  propagation, O(R + E) — deliberately not the O(R²) repeated-rescan a naive
  version would do when ids sort children before parents.
- **Breakpoint.** A ghost parent (`bout`→`prop-ghost`), a missing ancestor deep
  in a chain. Orphans are reported, never silently dropped from the output.
- **Parity.** By construction (set logic over the same graph).

### `derive` — ancestor memoisation — **fix, ADDENDUM #4 P5**

- **Invariant.** `anc[rid]` is the full applied-ancestor set of each applied
  record, built once in topological order (`anc[rid] = ∪_{p∈applied parents}
  ({p} ∪ anc[p])`). It is naturally applied-restricted: an applied record's
  parents are all applied, or it would be an orphan.
- **Complexity.** O(Σ|anc|). Linear in R+E to *drive*, but the sets are
  superlinear to *materialise* — up to O(R·D), O(R²) on a deep, wide contested
  key. This replaced per-candidate re-walks in both the dominance loop and the
  tie-break, which were O(pool · D) *each*. Measured on a 2,000-candidate,
  depth-1,000 contested key: **1,567 ms → 838 ms**. A count-only DP (carrying
  intent/witness tallies, not full sets) would drop the materialisation cost
  further — since superseded by the bitset ancestry engine (PR #14,
  `RESULTS.md` §1.2).
- **Parity.** By construction; outputs unchanged (the whole suite — 855 cases
  at the time — and `live_golden`, prove byte-identity to before).

### `derive` — per-key resolution + **ancestry-reduction fix, ADDENDUM #3**

- **Invariant.** Per key: take the candidates of the strongest class present
  (override > task > witness, absolute); drop any that is a transitive ancestor
  of another candidate (**exact maximality**); if several survive, the
  cleanest-chain tie-break (most intent ancestors, then fewest witnesses, then
  smallest id). The winner's value is the state; all other candidates are losers
  in the conflict report iff ≥2 distinct values exist.
- **The bug (confirmed, now fixed).** The pre-fix dominance loop marked only the
  *first* pool ancestor a DFS reached (`_first_ancestor_in`), so a dominated
  candidate could survive into the tie-break. Masked in task/override pools (a
  descendant wins the intent-count tie-break anyway) but live in **witness**
  pools, where the tie-break *penalises* witness ancestry. Witnesses r→a→b all
  setting k, plus c (parents of b and a) setting k: the fold shipped **b** when c
  listed parents `[b,a]` and **c** when `[a,b]` — the winner flipped with the
  parents-array order. Fixed by marking *every* pool member in each candidate's
  full ancestry (`anc[d] ∩ pool`), so maximality is exact and order-independent.
- **Complexity.** O(Σ_keys Σ_pool |anc|) with the memo; the set intersections
  dominate.
- **Parity & proof.** By construction (identical set logic in both ports), and
  proven three ways: the named fixtures `ancestry-witness-flip-ba/-ab` and
  `ancestry-task-diamond-ba/-ab` (both parent orders); a **brute-force maximality
  oracle** that recomputes winners from full ancestor sets and asserts the fold
  agrees, over 300 random DAGs **plus 4 parents-order permutations each**
  (`differential_oracle`, 2,812 checks); and `live_golden` byte-identity.

### `verify_checkpoint` — **fix, ADDENDUM #4 P1**

- **Invariant.** Returns true iff re-deriving from exactly the ancestors of
  `checkpoint["at"]` (itself included) reproduces `checkpoint["state"]`. A
  missing `at`, any missing ancestor, a non-dict state → false. The declared
  state is float64-canonicalised the same way record effects are.
- **The bug (confirmed, now fixed).** Pre-fix, a checkpoint asserting
  `k=9007199254740993` against a true state of `9007199254740992` returned
  **true** in TS (JSON.parse rounds 993→992) but **false** in Python (exact
  `992 != 993`) — a cross-port split, and TS silently accepting a value it
  cannot represent. The fix canonicalises the checkpoint's declared values to
  float64 before comparing, so both ports treat `993` as the double `992` and
  **agree**. Under the declared IEEE-754 domain this is not a forgery (993 and
  992 *are* the same number); a value denoting a genuinely **distinct** double
  (e.g. 2^53+2) is still rejected by both. Case `ckpt-bigint-domain` pins both
  the accept (same double) and the reject (distinct double); the pre-existing
  `FORGED-VALUE` probes pin string-vs-number rejection.
- **Caveat (documented, DESIGN.md §1).** Verification is relative to the record
  set passed in; a later-arriving ancestor grows the closure of `at`. Closure-
  root pinning is the v2 direction.
- **Complexity.** O(closure + derive(closure)).
- **Parity.** By construction after the canonicalisation fix.

### `witnessFreshness` / `witness_freshness`

- **Invariant.** `recordsBehind(w) = |applied| − |ancestorsInclusive(w)|` for
  each applied witness; a pure sibling of `derive()` that does not touch its
  output. Absent for orphaned witnesses (as they are absent from state).
- **Complexity.** O(R·D) worst case (walks each witness's ancestry); could reuse
  the `anc` memo in a future pass.
- **Parity.** Harness; 3,224 witness-freshness checks (at the time) compared
  Python-vs-TS, plus the `wf-*` fixtures with hand-checked values.

---

## Part 2 — the Worker collection path (silent-loss audit)

`collectProject` reads `record.json` from the project repo (one per commit) and
each `<project>--*` task repo (HEAD), dedupes by id, and hands the records to the
fold. Every point a record can be dropped:

| # | Drop site | Cause | Verdict |
|---|---|---|---|
| 1 | `JSON.parse` throws | `record.json` is not JSON a JS reader accepts — including **NaN/Infinity**, which Python's lenient `json.loads` *would* accept | **Named, documented.** The Worker's reader is the authority (the fold is the Worker's). A record a JS reader rejects is not folded; a Python-only reader would disagree. NaN/Infinity are out of the record domain (DESIGN.md §5). |
| 2 | `typeof rec.id !== "string"` | missing or non-string id | **Documented.** A record with no string id has no identity to fold; skipped. |
| 3 | identical duplicate id | same record in the project repo AND at a task HEAD | **Safe by design.** First-seen wins; the dropped copy is byte-identical. This is the intended dedupe. |
| 4 | **divergent** duplicate id | same id, *different content*, in two repos | **Surfaced.** First-seen wins, but the drop is now reported in `collectionWarnings` on `/api/records` (`{id, keptFrom, droppedFrom}`) rather than lost. This also means the fold's own duplicate-id tie-break is not exercised at the Worker layer — only one record per id reaches `derive()`. In the real topology each id has one repo, so this is an upstream-corruption signal. |
| 5 | binding read error / null blob | repo unreachable, no `record.json` at that ref | **Documented.** Caught → skipped, same as the sequential version. A whole project whose repo is unready derives over what exists (the projection degrades, it does not error). |
| 6 | `listAllRepos` cursor cap | the paginator stops after 50 pages (5,000 repos) | **Documented bound.** Beyond 5,000 matching repos, later ones are not seen. Far past demo scale; noted as a known ceiling. |

**Concurrency & determinism.** Reads now run at `COLLECT_CONCURRENCY = 24` in
flight (`mapBounded`), but results are written at their item index and **ingested
in the original deterministic order** (project commits first, then task repos in
sorted name order). So dedupe/first-seen, origin attribution, the records array,
and every derived output are byte-identical to the sequential version — proven
old-vs-new on both live projects for `/api/state`, `/api/records`, and the HTML
(modulo the HTML's pre-existing `derived <timestamp>` line). Measured effect of
the concurrency (100 ms/read model, 62 reads): **6,389 ms → 409 ms**; the live
sequential baseline for `poc-scale-1 /api/state` was ~14 s warm / ~31 s cold.

**Transport note (document, not fix).** `python-urllib` clients on some network
paths deterministically `IncompleteRead` large Worker JSON responses (observed:
the ~19 KB `/api/repos` listing failed 6/6 while `curl` succeeded in ~1 s). The
suspected mechanism is a chunked-transfer / connection-reuse interaction between
`http.client` and the edge that truncates the body before the terminal chunk.
Operator guidance: page large listings, or fetch via `curl`; the Worker response
itself is well-formed (curl and browsers read it whole).

**Public derivation cost (the honest price of the public face).** The read
routes are public by design, and a state read is not free: every
`GET /api/state` runs the full collection + fold for its project, and no
route authenticates or throttles the caller. The measured shape at the
62-record scale project: **126 binding calls** per read (2 list + 61 get +
1 log + 62 readFile) and **2.9–5.7 s** live latency, almost all of it
collection — an unauthenticated caller can amplify that cost at will, and
the namespace's 5,000-repo ceiling (drop site #6) bounds the worst case
only loosely. Mitigations, as they land: the bounded-concurrency
collection above took the read from ~14 s warm / ~31 s cold sequential to
the 2.9–5.7 s band; the bitset ancestry engine (PR #14) removed the fold
as the scaling term — a 5,000-record derive fell from 2,712 ms / 1,266 MB
peak RSS to 110 ms / 117.5 MB — leaving the collection reads as the
remaining cost; checkpoint+tail derivation (shipped, `docs/DELTA-READS.md`)
serves the records a verified checkpoint covers without re-reading them and
reads only the tail, which cuts the collection itself — on `poc-scale-1` a
frontier checkpoint reads at ≈0.93 s against 2.42 s for the full fold
(`RESULTS.md` §1.4). Deliberately not done: response caching. The
projection is derived fresh on every request by doctrine — a cached
projection is a stale projection, and staleness here is a correctness bug,
not a performance feature. If throttling is ever needed it belongs at the
edge in front of the Worker, not as a cache inside it.

---

## Part 3 — gated-routes & `DEPLOY.md` security claims, line by line

Each claim in `DEPLOY.md` checked against `src/worker.ts`:

| Claim (DEPLOY.md) | Code | Verdict |
|---|---|---|
| "`X-Admin-Key` must equal the `ADMIN_KEY` secret" | `authed()`: `provided !== null && provided === env.ADMIN_KEY` | **True** at audit time; production pass compares via the constant-time `keyEq()` (F1). |
| "Fails closed (401) when the secret is unset" | `authed()`: `if (!env.ADMIN_KEY) return false` | **True** — unset secret denies every gated route. |
| "…or the header is wrong" | `gated()` returns `json({error:"unauthorized"}, 401)` when `!authed` | **True.** |
| "create/fork responses never carry repo tokens" | `publicMeta()` destructures `{ token: _token, ...rest }` | **True for the top-level token** the binding returns. Finding F3 below: it strips only the top level. |
| "`/api/repos/<name>/token` → `{plaintext, expiresAt}`" | `handleToken` returns the minted token | **True, and intended** — this is the one route that returns a token, and it is admin-gated. |
| Routes table auth column (GET `/`, `/api/state`, `/api/repos` public; POST/DELETE/token/debug/last-event admin; POST `/api/events` public) | router in `fetch` | **Matched exactly** at audit time. Production pass: the `/api/events` route no longer exists, so every mutating route is now admin-gated. |
| "nothing mutates without the key" | all of `create`/`fork`/`delete`/`token` under `gated()` | **True.** (Was: true for repo mutation, with F2's `/api/events` an unauthenticated write to one KV key — route removed in the production pass, below.) |

**Findings**

- **F1 (low) — non-constant-time admin compare.** `authed()` uses `===` on the
  key, which can short-circuit on the first differing byte. A timing side channel
  in principle; negligible over the public internet against a high-entropy secret,
  but a hardened build would use a constant-time compare. Not fixed (behaviour
  change to a security primitive, out of this pass's scope); named.
  **Fixed in the production pass (2026-10-14):** `authed()` now compares via
  `keyEq()`, a constant-time scan that folds in the length difference.
- **F2 (low, by design) — public `POST /api/events`.** Unauthenticated; writes
  one KV key (`last-push`). Bounded (one key, overwrite), observability-only, and
  derivation never reads it, so it cannot affect state — but it is a public write
  and could be spammed. Documented as an accepted demo trade-off.
  **Fixed in the production pass (2026-10-14):** the route and its handler are
  removed entirely (no tree caller existed — scene/tests never used it). The
  only writer of `last-push` is now the Queue consumer. `POST /api/events`
  answers 404; `GET /api/last-event` is unchanged.
- **F3 (informational) — `publicMeta` strips only the top-level `token`.** The
  Artifacts binding returns the token at the top level, so this is correct today;
  a nested token field in a future binding shape would not be stripped. Named for
  the next binding upgrade.
  **Fixed in the production pass (2026-10-14):** `publicMeta` is now a deep
  scrub (credential-shaped keys removed at every depth), and the public
  `GET /api/repos` list — which previously returned raw binding metadata —
  goes through the same scrub. Output is byte-identical for today's binding
  shapes (live list items carry no credential keys).

**Production pass (2026-10-14) — further findings, all fixed, all pinned by
`tests/security-probes.mjs` (18 probes, mock-binding, before/after):**

- **P1 (medium) — the router's try/catch never caught async errors.**
  Dispatch returned handler promises un-awaited, so any binding rejection
  during a request escaped as an uncontrolled runtime 500; the catch only
  ever saw synchronous throws — and answered those with `err.message` in
  the body, leaking internals. Fix: every dispatch is awaited inside the
  try; the catch answers a bare `{error:"internal"}` and logs server-side.
- **P2 (low) — unvalidated path parameters.** `fork`/`delete`/`token`
  names were `decodeURIComponent`'d unchecked: malformed encodings threw
  (500), and traversal-shaped names (`../evil`) reached the binding
  verbatim. Fix: `pathName()` (defensive decode + the create route's
  NAME_RE) inside the gate; bad names are a clean 400 and the binding is
  never touched. The debug route's `?repo=` is validated the same way.
- **P3 (low) — read routes accepted any `?project=`.** Garbage names
  derived over an empty collection (200, no crash, no leak — but not a
  contract a production surface should keep). Fix: the three read routes
  validate via the same NAME_RE (absent = default project, malformed =
  400). Valid names are byte-identical, verified against the golden.
- **P4 (low) — token route took any `ttl`.** Negative or astronomical
  values were passed to `createToken`. Fix: `ttl` must be a positive
  finite number of seconds, max 30 days; `scope` a short string.
  `pp.py`'s own values (read/write, operator-chosen seconds) are
  unaffected.

Probed and clean in the same pass (no change): a key supplied only in the
query string never authenticates (header-only, verified live and in
probes); no key/wrong key fail closed on every gated route, and the gate
runs before any input is processed; `GET /api/repos` pagination
parameters are not consumed at all (internal 50-page cap), so fuzzing
them is inert; record ingest stays defensive (unparseable records are
skipped, never fatal); the HTML projection escapes every interpolated
value; nothing in the Worker logs secrets (there is no logging of
request data at all — the one `console.error` added by P1 logs the
server-side exception object only, never request material).

No high- or critical-severity findings in the gated routes.

---

## Findings summary

| Severity | Count | Items |
|---|---|---|
| Critical | 1 | Checkpoint forgery/accept split in `verify_checkpoint` (ADDENDUM #4 P1) — **fixed**, pinned by `ckpt-bigint-domain` |
| High | 2 | Ancestry-reduction winner flip (ADDENDUM #3) — **fixed**; cross-port id-order split (ADDENDUM #1) — **fixed** |
| Medium | 2 | Numeric-domain conflict split (ADDENDUM #4 P3) — **fixed**; collection latency O(reads) sequential (ADDENDUM #2) — **fixed** |
| Low | 4 | Non-constant-time admin compare (F1) — **fixed** (production pass); public `/api/events` write (F2) — **fixed** (route removed, production pass); divergent-dup silent drop (collection #4) — **surfaced**; harness `allow_nan` crash (ADDENDUM #4 P4) — **fixed** |
| Medium (production pass) | 1 | Router try/catch never caught async rejections; sync throws leaked `err.message` (P1) — **fixed**, probe-pinned |
| Low (production pass) | 3 | Unvalidated path params (P2), unvalidated `?project=` (P3), unvalidated token `ttl` (P4) — **fixed**, probe-pinned |
| Informational | 3 | dead `depth` map (py) — **removed**; `publicMeta` top-level strip (F3) — **fixed** (deep scrub, production pass); `listAllRepos` 5,000-repo ceiling — documented |

Everything marked **fixed/surfaced/removed** is on this branch with a conformance
or integration case that proves it; everything **named/documented** is left as-is
with the reason stated.

## Conformance & integration totals (this branch)

- Conformance: **863/863 cases identical** — derive + **1,029 checkpoint
  probes** + **3,231 witness-freshness checks** + **16,290 closure-root
  digests**; plus the brute-force maximality oracle & parents-order
  invariance (**2,812 checks**), the non-finite transport pin (**3**), and
  live-golden byte-identity (**8**). `python3 conformance/harness.py`
  (totals re-verified on this HEAD, 2026-10-14).
- Integration: **22 passed, 0 failed** against the live base URL, including the
  `poc-scale-1` scale assertions (62 records, 3 conflicts, 0 orphans, winners
  t30/t09/t59) and the `/api/state` latency line.
