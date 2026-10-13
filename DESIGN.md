# DESIGN.md for git.pp Submission

## 1. Fold's Contract

### Inputs
- **Record Set**: A collection of records, each containing:
  - `id`: Unique identifier for the record.
  - `parents`: A list of record IDs this record completed against — its ancestry.
  - `kind`: One of `task` (a completion), `witness` (an observation), or `override` (a reasoned human decision).
  - `effects`: A map of state keys to the values this record sets.
  - `reason`: Present on overrides — the stated reason, carried in the record.

### Outputs
- **State**: The final state after processing all records, mapping keys to their resolved values.
- **Order**: The topological order in which records are processed.
- **Conflicts**: A list of keys for which conflicting values were detected.
- **Orphans**: Records excluded from derivation because a parent is missing from the record set, or because they are blocked behind such a record or a parent cycle.

### VerifyCheckpoint Semantics
- `verify_checkpoint(records, checkpoint)` performs exactly one check, and it is the strong one: it re-derives state from exactly the ancestors of the record named in `checkpoint["at"]` and returns true only if that derived state equals the checkpoint's claimed state. A checkpoint that summarises anything else — a superset, a subset, a tampered value — fails. Checkpoints are therefore legal only when provenance-stamped: they name the record position they summarise and are reproducible from it.
- **Relative to the record set.** The closure of `at` is computed over the records passed in, so verification is a statement about *this* record set. A later-arriving ancestor of `at` grows that closure and can change what `at` verifies to — honest behaviour (the past was genuinely incomplete before), but it means a checkpoint is only as pinned as the set it is checked against. Binding a checkpoint to the exact closure root it was taken over (a closure-root hash) so a grown closure is detectable is **checkpoint v2**, implemented as a spike (see §6 and `docs/CHECKPOINT-V2.md`): verification then reports VERIFIED, STATE-ONLY, or MISMATCH instead of a bare bool.
- The value comparison is in the number domain of §6: the checkpoint's declared values are float64-canonicalised exactly as record effects are, so the two implementations verify a given checkpoint identically rather than splitting on a non-JS-safe integer that one port has already rounded.

## 2. Resolution Algorithm

### Step-by-Step Process

1. **Canonical Indexing by ID**:
   - Records are indexed by their unique `id` to ensure constant-time access.

2. **Orphan Worklist and Cycle-Affected Exclusion**:
   - Identify and collect records with no valid dependencies into an orphan worklist.
   - Exclude records that are part of dependency cycles from the processing order.

3. **Kahn Order (Smallest-ID-First)**:
   - Apply Kahn's algorithm to determine a topological order of record processing.
   - In the case of ties (records ready at the same time), process the record with the smallest `id` first. "Smallest" is by Unicode **code-point** sequence in both implementations — JavaScript's default UTF-16 code-unit order disagrees with Python's for non-BMP ids, so the TypeScript port compares code points explicitly.

4. **Per Key Resolution**:
   - **Absolute class precedence (override > task > witness)**:
     - For each key, only the candidates of the highest class present compete; a witness never outranks a task or an override, even as a descendant.
   - **Exact maximality (ancestry reduction)**:
     - Within that class, a candidate that is a (transitive) ancestor of ANY other candidate is dominated and dropped; the survivors are the most-derived. Maximality is computed from full ancestor sets, so it is exact and independent of the order a record lists its parents.
   - **Cleanest-chain tie-break**:
     - If multiple candidates survive, apply tie-breaks in order:
       1. **Most intent ancestors**: prefer the candidate whose ancestry holds the most intent records (`task` + `override`).
       2. **Fewest witness ancestors**: if still tied, the fewest `witness` records.
       3. **Smallest id**: if still tied, the lexicographically (code-point) smallest `id`.

5. **Conflict Detection**:
   - If, after resolution, there are two or more distinct values for a key, a conflict is emitted.
   - The losers are all candidates except the winner, which is the value chosen by the resolution algorithm.

## 3. Determinism Argument

Every step of the algorithm is a function of the record set only:
- The order of processing is determined by the parents graph and the `id`s of records (compared by Unicode code point in both ports).
- The resolution of values is based on a fixed precedence hierarchy and deterministic tie-breaks.
- Conflict detection is based on the presence of multiple distinct values for a key.
- Therefore, given the same record set, the algorithm will always produce the same outputs.

## 4. Witness-Absolute vs. Ancestry-First Reading

### Witness-Absolute Reading (implemented)
- Class precedence is absolute: an `override` outranks a `task`, a `task` outranks a `witness` — even when the witness is a *descendant* of the task. An observation can never displace a completion on a contested key, no matter how recent. "Seen by us" does not overwrite "done by us"; changing a completed value takes a task, or an override that cites the witness.
- Uncontested witness observations DO reach the state — that is their purpose: ambient truth (a disk at 93%, a load average) is part of the derived picture, labelled by kind.

### Ancestry-First Reading (the alternative)
- Would let ancestry dominate class: a descendant witness could displace its own ancestor task on the same key.
- The flip is small and localised (the class check inside the per-key reduction), and the conformance harness would arbitrate either reading identically in both implementations.

### Rationale for Choosing Absolute
- The record distinguishes "done" from "seen"; letting the seen overwrite the done silently would erase exactly the distinction the record exists to keep. Absolute precedence keeps every displacement attributable: if a value changed, a task or a reasoned override changed it.

## 5. Measured Performance

### Python Reference Implementation
- **10k Chain**: 0.6 seconds
- **2k Writers One Key**: 0.04 seconds

### TypeScript Port Conformance
- The conformance harness computes every expected output by running the Python referee at harness time and compares it to the TypeScript port; a divergence is a port bug by definition. It covers property fixtures, seeded random stress DAGs (with parents-order permutations), the live record sets, and edge cases (cycles, duplicate ids, unknown kinds, bool/int collisions, non-BMP ids, non-JS-safe integers, checkpoint forgery). It also runs a brute-force maximality oracle, a non-finite transport pin, and live-golden byte-identity in the referee.
- Current totals are reported by `python3 conformance/harness.py` (the suite grows with each pinned claim; see `docs/ENGINEERING.md`). The number domain below is what keeps the two ports bit-for-bit identical.

### Number domain (the two ports agree bit-for-bit)
- Record numbers are JSON values, held as IEEE-754 **float64** (what the Worker's `JSON.parse` yields). Integers outside the JS safe range (|n| > 2^53−1) round when they cross JSON, so the Python referee float64-canonicalises every non-safe integer at ingest; safe-range integers (all live data) are untouched, so derived state for the live projects is unchanged.
- **NaN / Infinity are out of domain.** JSON has no literal for them and the Worker's `JSON.parse` rejects them, so they cannot reach the fold; the harness sets `allow_nan=False` so a non-finite value fails loudly at construction rather than silently.

## 6. Known Limits

- **Detection Boundary**: The fold labels every conflict it can detect from the record set.
- **Semantic Incompatibilities**: Incompatibilities between individually valid completions are not detectable from the record alone and are out of scope. A read-set (precondition) extension that widens this boundary is sketched in `docs/OPEN-QUESTIONS.md`.
- **Integer precision > 2^53**: Values above the JS safe-integer range are folded as float64 (see §5, Number domain), not as exact big integers. This is the declared domain, not an unhandled case.
- **Derivation Performance**: Derivation is **not** O(record). The orphan pass, topological order, and conflict grouping are linear, but per-key resolution needs each candidate's ancestor set; materialising those sets is superlinear in the worst case — O(V·E) / up to O(V²) in both candidate count and depth on a deep, wide contested key. Ancestor computation is **memoised once per `derive()` call** so no walk is repeated across candidates or keys (measured: a 2,000-candidate, depth-1,000 contested key went 1,567 ms → 838 ms). A count-only ancestor DP (carrying intent/witness tallies instead of full sets) and incremental derivation from verified checkpoints are the next steps.
- **Checkpoints are relative to the record set** (see §1): a later-arriving ancestor grows the closure of `at`. Binding a checkpoint to a closure-root hash so a grown closure is detectable is **checkpoint v2**, implemented as a spike in both ports (`checkpoint_root` / `checkpointRoot`; `verify_checkpoint` gains an optional `closureRoot` and returns VERIFIED / STATE-ONLY / MISMATCH, backwards compatible). The digest construction and migration story are in `docs/CHECKPOINT-V2.md`; the conformance harness pins it (same-set → VERIFIED, grown closure → STATE-ONLY, tampered state → MISMATCH, tampered root → not VERIFIED) and proves the digest byte-identical across ports on the whole corpus and the live `poc-quilt-1` set.

## 7. What Was Deliberately Not Built

- **No Lock**: The algorithm does not use any locks for synchronization.
- **No Queue**: Queues are not used for processing records, except for event observability.
- **No Resolution Service in State Path**: The state path does not include a resolution service. Queues are only used for observability of events.
