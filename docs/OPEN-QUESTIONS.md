# Open Questions — git.pp design working notes

The fold's settlement makes one deliberately narrowed claim (DESIGN.md §6):

> Every conflict **detectable from the record** is labelled. Semantic
> incompatibilities between individually valid completions are not detectable
> from the record alone.

This document works the open questions behind that claim to implementation
specificity. For each: a recommended position, the strongest counter-position,
and the evidence that would move the recommendation.

**Status legend.** 1b is **implemented and prototyped on this branch**
(`src/fold.ts`, `referee/fold.py`, `src/worker.ts`, conformance + integration
tests). 1a, 1c, 1d are **design only** — schemas and fold changes are proposals,
not code. Everything stated about 1b is true of the branch and covered by the
conformance harness; nothing in 1a/1c/1d is claimed to exist in code.

A load-bearing constraint runs through all four: **`derive()`'s output dict is
frozen.** The conformance harness compares the whole dict Python-vs-TS, and
`/api/state` is literally `json(derive(records))`, which must stay byte-identical
for `poc-quilt-1`. So every extension here adds a **sibling pure function**
(`witnessFreshness`, a proposed `readHazards`, …) or a new record **kind** that
`derive` already folds — never a new key in `derive`'s return.

---

## 1a. The detection boundary

### What the record can and cannot detect today

The fold detects **exactly one** conflict class: same-key write–write
divergence. Formally — among applied records (ancestry present, not cycle-
excluded), for each key `K`, collect the records whose `effects` set `K`; if two
or more set `K` to values that are distinct under Python `==` semantics
(`src/fold.ts` `pyValueKey`), emit `{key, winner, losers}`. The winner is chosen
by class (override > task > witness) then ancestry/tie-break; **the loser list
is the detection**. Nothing else is flagged.

| # | Conflict class | Example | Detected? | Why |
|---|---|---|---|---|
| 1 | Same-key write–write | A sets `config.timeout=30`, B sets `=60` | **Yes** | distinct values on one key |
| 2 | Read–write hazard | A completes *using* `timeout=30`; B later sets `timeout=99`; A never re-derived | No | records don't declare what they **read** |
| 3 | Cross-key semantic | A sets `feature.cache=on` (needs `timeout≥30`), B sets `timeout=5` | No | fold has no cross-key invariants |
| 4 | Value-level staleness | A overwrites `K` from a stale read of `K` | No | special case of #2 |
| 5 | Same value, two authors | A and B both set `K="on"` | Correctly **not** a conflict | no divergence to report |

The settlement's claim is precisely the boundary between row 1 and rows 2–4.

### Extension: declared read-sets (preconditions)

Add an optional field to a record declaring the keys it read and the values it
saw:

```jsonc
{
  "id": "task-a", "parents": ["genesis"], "kind": "task",
  "effects": { "feature.cache": "on" },
  "reads":   { "config.timeout": 30 }     // NEW: key -> value observed at completion
}
```

Backward-compatible: absent `reads` ⇒ `{}` ⇒ no preconditions ⇒ today's
behaviour. The live `poc-quilt-1` records are unaffected (none carry `reads`).

**Value-form, not key-only.** `reads: ["config.timeout"]` can say *touched* but
not *changed*; `reads: {key: value}` lets the fold tell a still-valid read from a
superseded one. Recommend the value-form.

**Fold use — a sibling function, advisory, not state-deciding.** Mirroring the
1b pattern, add `readHazards(records)` (NOT a key in `derive`'s dict):

```
readHazards(records) -> [ { record, key, saw, now } ]

for each applied record r with r.reads:
    seen = derive(closure(r)).state           # the state r actually saw;
                                               # same closure verify_checkpoint uses
    for (key, expected) in r.reads:
        if key in current_state and current_state[key] != expected:
            emit { record: r.id, key, saw: expected, now: current_state[key] }
```

A hazard flags that `r`'s completion rests on a read that the current state has
since moved. It **does not** change who wins `key` — winner selection stays the
class+ancestry rule, so determinism and `/api/state` are untouched. Surface
hazards in `/api/records` beside `recordsBehind`.

- **Cost.** Record authors must declare reads. A runner that proxies state
  access can capture them automatically (log every key consulted + value seen);
  hand-authored records pay a discipline cost. Compute cost: naïve O(R) closures
  × O(R) = O(R²); memoisable by topological level to O(R+E).
- **Still cannot catch:** undeclared reads (author forgot, or read outside the
  substrate — a file, the network); cross-key invariants with no shared key
  (row 3 — needs invariant specs, not reads); semantic drift where the value is
  unchanged but its meaning changed.

### Position

- **Recommended.** Ship `reads` (value-form) feeding an advisory
  `readHazards()` sibling surface. It moves the entire read–write class (row 2,
  and row 4 as a special case) from undetectable to detectable, is
  backward-compatible, composes with witness freshness (both answer "what did
  this record see vs. now"), and keeps `derive()` frozen so the determinism and
  byte-identity guarantees hold.
- **Strongest counter.** Read-sets are false comfort: authors under-declare, so
  a green "no hazards" is not safety, and the incompatibilities that actually
  bite are cross-key semantic ones (row 3) that reads never touch. The effort
  belongs in an explicit **invariant record** kind instead.
- **Evidence that would change it.** Measure, on real task streams, the split of
  post-hoc-discovered incompatibilities: (a) read–write on a declarable key vs
  (b) cross-key semantic. If (b) dominates, deprioritise `reads` in favour of an
  invariant kind.

---

## 1b. Witness freshness — **IMPLEMENTED on this branch**

The design calls for witnesses to expose their staleness. Shipped as a pure
function in both implementations and surfaced in `/api/records`.

**Definition.** For an applied witness `w`:

```
recordsBehind(w) = |applied| − |ancestorsInclusive(w) ∩ applied|
```

the number of applied records **not** in `w`'s causal past (itself included) —
the records that landed without `w` in their ancestry, i.e. what `w` did not
witness. `0` means `w` saw everything currently applied; larger is staler.

**Why this shape.** `ancestorsInclusive(w)` is exactly everything reachable from
`w`'s parents frontier; the applied set is the current frontier's closure. So the
count is the record-position distance between the witness's parents-frontier and
the current frontier, as specified.

**Where it lives.** `witnessFreshness(records)` / `witness_freshness(records)`
(`src/fold.ts`, `referee/fold.py`) return `{witnessId: recordsBehind}` for applied
witness-kind records only. Orphaned/cycle-excluded witnesses are absent — exactly
as they are absent from `derive().state`. **`derive()`'s dict is untouched**, so
`/api/state` for `poc-quilt-1` is byte-identical (verified against the pre-change
Worker build). The Worker annotates each `/api/records` entry with
`recordsBehind` (a non-negative integer on applied witnesses, `null` on everything
else) and the HTML projection shows `witness (N behind)`.

**Worked values (all asserted by the conformance harness):**

| Fixture | witness | recordsBehind |
|---|---|---|
| `poc-live` (7 records) | `witness-1` | 5 (`{genesis, witness-1}` seen; 5 landed without it) |
| `wf-linear` | `w` | 3 |
| `wf-two-witnesses` | `w-early` / `w-late` | 4 / 2 |
| `wf-current` | `w` | 0 (witness at the frontier) |
| `wf-orphan-witness` | `w` | absent (orphan, not applied) |

**Conformance.** `runner.ts` emits `freshness`; the harness compares it
Python-vs-TS for every case. Current totals: **843/843 cases, 980 checkpoint
probes, 3213 witness-freshness checks**, all identical.

### Position

- **Recommended (shipped).** `recordsBehind` as the record-count distance above,
  advisory only, out of `derive()`.
- **Strongest counter.** Record-count is a weak proxy: 5 behind on keys the
  witness never touched is harmless; 1 behind on the exact key it reported is
  fatal. A **key-scoped** freshness (was any key this witness's effects also set
  overwritten after its frontier?) would be truer.
- **Evidence that would change it.** If operators act on `recordsBehind` and are
  misled (false-stale or false-fresh), ship the v2 key-scoped variant — cheap
  follow-on: `witnessFreshness` additionally returns `keysSupersededSince`. Not
  shipped here; flagged as a candidate.

---

## 1c. The completion standard — receipt model (design only)

### What makes a completion *count* today

A `task` record counts iff it is **applied** (ancestry present, not in a cycle)
and its class wins the key. Nothing attests that the work actually happened — the
record is an unverified *claim*.

### Receipt schema

```jsonc
{
  "id": "task-a", "parents": ["genesis"], "kind": "task",
  "effects": { "feature.cache": "on" },
  "receipt": {
    "tool":        { "name": "bash", "version": "5.2" },
    "command":     "make build && make test",       // argv or stable description
    "exitStatus":  0,                                 // 0 = success
    "startedAt":   "2026-10-12T22:00:00Z",
    "endedAt":     "2026-10-12T22:03:11Z",
    "artifacts":   [ { "path": "dist/app.js", "sha256": "…", "bytes": 24990 } ],
    "inputsDigest": "sha256:…",                       // hash over declared reads/inputs
    "demarcation": "leaf"                             // "leaf" | "composite"
  }
}
```

- **Demarcation leaf** = a direct tool execution: one command/tool run that
  produced `artifacts`. Carries `command` + `exitStatus`.
- **Demarcation composite** = an aggregate whose evidence is its children's
  receipts. Its `parents` ARE the subtask records; it carries **no** `command`/
  `exitStatus` of its own. This is how "a task's state is its completed subtasks"
  bottoms out: composites all the way down to leaves.

### How the fold treats a receipt that fails verification

The fold is a **pure function of the record** — it cannot run commands or hash
artifacts. So verification splits:

- **(a) Self-consistency the fold can check** (record-only, deterministic): a
  leaf claiming success has `exitStatus == 0`; a leaf has `command`, a composite
  has children and no `command`; `artifacts` well-formed. A record failing these
  is **malformed** ⇒ exclude from state like an orphan and report in a sibling
  `unverifiable(records)` list (not in `derive`'s dict).
- **(b) External verification** (re-run the command, re-hash artifacts): done
  **out of band** by a verifier that writes a **companion record**. On failure
  the verifier emits an `override` ("receipt X failed: hash mismatch") that moves
  the contested key off the failed completion — reusing the existing class, and
  **the loser is preserved** (its repo still exists), consistent with the
  conflict doctrine.

Recommended treatment of a **failed external verification**: the override path
above. Do not invent a demotion rule inside the fold — the fold stays pure and
the escalation is a reasoned, inspectable record.

### Cost / failure modes

- **Cost.** Every completion carries a receipt; leaves need deterministic
  artifact hashing.
- **Failure modes.** Non-deterministic builds break artifact hashes → hash
  declared **outputs**, never logs/timestamps. **Forged receipts** (a lying
  record claims `exitStatus:0`) are caught only by external verification, and
  only for reproducible commands; non-reproducible tools cannot be verified from
  the record at all — the honest ceiling of this model.

### Position

- **Recommended.** Receipts as a schema the fold checks for **self-consistency
  only**; external verification via companion `override` records; leaf/composite
  demarcation explicit. The fold stays pure.
- **Strongest counter.** Self-consistency checks are theatre — only externally
  verified receipts matter — so verification belongs in the Worker's **ingest
  path** (gate record acceptance on a verifier), not in the fold. Don't fold
  unverified completions at all.
- **Evidence / whose call.** The rate of forged/incorrect completions in
  practice. Low ⇒ self-consistency + spot verification suffices. High or
  adversarial ⇒ move verification to ingest — but that **reintroduces a trusted
  service in the write path**, which the design deliberately avoided (DESIGN §7,
  "No Resolution Service in State Path"). That boundary is the **captain's call**:
  it trades the purity of derivation for stronger acceptance guarantees.

---

## 1d. Scoped defaults as records

Doctrine: defaults cascade Global → Organisation → User → Repo → Folder → File;
narrower wins; a level may lock.

### Scope declaration as a record kind

```jsonc
{
  "id": "repo-default-1", "parents": ["genesis"], "kind": "default",
  "scope":   { "level": "repo", "path": "poc-quilt-1" },
  "effects": { "config.timeout": 30 },
  "locks":   ["config.timeout"]            // optional: narrower defaults may not override
}
```

`level ∈ {global, org, user, repo, folder, file}`; `path` identifies the node.

### Resolution

For a key `K` and a target node, among **default**-class records that set `K` and
are **applicable** (their `scope.path` is an ancestor/prefix of the target):

1. pick the **narrowest** level (file > folder > repo > user > org > global);
2. within a level, fall back to the existing ancestry + grafted tie-break.

This is a **scope-precedence axis layered under the class axis**. Complexity
stays O(record) with a level index.

### Class interaction and locking

Proposed ladder: **override > task > default > witness.** A deed (task) overrides
a default; an override beats everything; defaults decide only keys no task/
override set. A `locks` entry means **narrower defaults** cannot override that key
— locking binds *within the default class only*.

**The hard question — lock vs. override:**

- **(A)** `override` always wins, even over a locked global default. Locks
  constrain defaults, not deeds; a policy that must bind even the captain is
  modelled **as an override itself** (highest reasoned authority).
- **(B)** a locked high-level default (org/global) outranks `override` for that
  key — a **mandate** tier *above* override, for compliance/security that no
  local deed may violate.

### Cost / failure modes

- **Cost.** Path/level discipline; the fold gains a scope-applicability test and
  a per-key narrowest-level pick.
- **Failure modes.** Ambiguous paths; two defaults at the same level+path
  diverging (a conflict — same machinery); a lock meeting an override (the (A)/(B)
  choice decides the outcome).

### Position

- **Recommended — (A).** Defaults are a class below task; `locks` bind narrower
  defaults only; `override` stays sovereign; a mandatory policy is expressed *as*
  an override. One clean ladder, with nothing that silently beats a reasoned
  captain decision — matching the override doctrine (README "Inheritance").
- **Strongest counter — (B).** Compliance/security locks exist precisely to bind
  even the captain; if any local override can break a locked global policy, the
  lock is meaningless. Real orgs need un-overridable mandates.
- **Evidence / whose call.** This is a **governance decision, the captain's to
  make**, not a technical one. It flips to (B) the moment one real mandate must
  hold against a local override (e.g. "no plaintext secrets", "max token TTL")
  that the org will not encode as an override. If such mandates are real, add a
  `mandate` tier above override; otherwise keep (A).

---

## Still genuinely open (and whose call)

| Question | Open point | Whose call |
|---|---|---|
| 1a | `reads` value-form vs key-form; whether hazards may ever influence winner selection (recommend **never**) | Engineering — resolvable by the (a)/(b) measurement above |
| 1b | record-count vs key-scoped freshness (`keysSupersededSince`) | Engineering — ship v2 if operators are misled |
| 1c | verification in the fold (self-consistency + companion overrides) **vs** an ingest gate | **Captain** — it is the "no trusted service in the state path" boundary (DESIGN §7) |
| 1d | lock-vs-override: option (A) **vs** (B)/mandate tier | **Captain** — governance sovereignty |

The two engineering questions are decidable with data from real task streams.
The two captain questions are trust-model choices the fold can implement either
way but should not decide on its own — they set how much authority derivation is
allowed to concede.
